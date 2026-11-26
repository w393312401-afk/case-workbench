"""A restartable gateway never owns the fake task host's workers or paid results."""
from __future__ import annotations

from email.message import Message
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import service_gateway as gateway


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "gateway-browser-test-token"
SECRET = "gateway-test-secret-" + "x" * 40


class GatewayUploadIdleTests(unittest.TestCase):
    def setUp(self):
        self.handler = gateway.GatewayHandler.__new__(gateway.GatewayHandler)
        self.handler.connection = Mock()
        self.handler.connection.gettimeout.return_value = None
        self.handler.rfile = Mock()
        self.handler._body_read = 0

    def test_expired_upload_releases_the_write_admission(self):
        handler = self.handler
        handler.command, handler.path, handler.headers = "POST", "/api/data/upload", Message()
        handler.headers["Content-Length"] = "4"
        handler.rfile.read.side_effect = [b"hi", TimeoutError("fixture idle read")]
        handler.server = Mock()
        handler.server.host_manifest.return_value = {"port": 1, "proxy_secret": SECRET}
        handler.output = Mock()
        with patch.object(gateway.http.client, "HTTPConnection") as upstream:
            handler.dispatch()
        handler.server.begin_proxy_write.assert_called_once()
        handler.server.end_proxy_write.assert_called_once()
        upstream.return_value.send.assert_called_once_with(b"hi")
        upstream.return_value.close.assert_called_once()
        self.assertEqual(handler.output.call_args.args[0], 408)
        self.assertTrue(handler.close_connection)
        self.assertEqual([call.args[0] for call in handler.connection.settimeout.call_args_list],
                         [gateway.UPLOAD_IDLE_TIMEOUT, None])

    def test_active_upload_keeps_forwarding_until_the_body_is_complete(self):
        handler, upstream = self.handler, Mock()
        handler.rfile.read.side_effect = [b"one", b"two"]
        handler.forward_body(upstream, 6)
        self.assertEqual([call.args[0] for call in upstream.send.call_args_list], [b"one", b"two"])
        self.assertEqual(handler._body_read, 6)


class FakeHostHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def out(self, status, value, headers=None):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_request(self):
        if self.headers.get(gateway.GATEWAY_HEADER) != SECRET:
            return self.out(403, {"error": "Missing gateway credential"})
        forwarded = self.headers.get(gateway.FORWARDED_HEADER) == "1"
        port = self.server.public_port if forwarded else self.server.server_port
        if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            return self.out(403, {"error": "Untrusted Host"})
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return self.out(403, {"error": "Untrusted Origin"})
        if self.path == "/api/service/status":
            return self.out(200, {"app": "video-case-workbench", "role": "task-host", "protocol_version": 1,
                "pid": os.getpid(), "launch_id": self.server.launch_id, "token": TOKEN,
                "code_stale": self.server.code_stale, "shutdown_supported": True, "shutting_down": False,
                "paths": self.server.paths})
        self.server.seen.append((self.command, self.path, dict(self.headers)))
        if self.path in {"/api/service/shutdown", "/api/service/restart"}:
            self.server.shutdown_called = True
            return self.out(500, {"error": "Gateway must never forward this operation"})
        if self.command == "POST" and self.headers.get("X-Workbench-Token") != TOKEN:
            return self.out(403, {"error": "Missing browser token"})
        if self.path == "/api/jobs" and self.command == "POST":
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            with self.server.jobs_lock:
                if data["id"] not in self.server.jobs:
                    self.server.jobs[data["id"]] = "running"
                    def work():
                        self.server.task_started.set()
                        self.server.task_release.wait(20)
                        with self.server.jobs_lock:
                            self.server.result.write_text(self.server.result.read_text() + data["id"] + "\n" if self.server.result.exists() else data["id"] + "\n")
                            self.server.jobs[data["id"]] = "completed"
                        self.server.task_done.set()
                    threading.Thread(target=work, daemon=True).start()
            return self.out(202, {"id": data["id"]})
        if self.path == "/api/jobs" and self.command == "GET":
            return self.out(200, self.server.jobs)
        if self.path == "/unknown-length":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"unframed fixture")
            self.close_connection = True
            return
        if self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"short")
            self.close_connection = True
            return
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for body in (b"data: first\n\n", b"data: second\n\n"):
                    self.wfile.write(f"{len(body):x}\r\n".encode() + body + b"\r\n")
                    self.wfile.flush()
                    if body.startswith(b"data: first"):
                        self.server.stream_started.set()
                        self.server.stream_release.wait(10)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if self.path == "/video":
            first, last = 0, len(self.server.video) - 1
            status = 200
            if self.headers.get("Range"):
                first, last = [int(value) for value in self.headers["Range"].removeprefix("bytes=").split("-")]
                status = 206
            return self.out(status, self.server.video[first:last + 1], {
                "Content-Type": "video/mp4", "Accept-Ranges": "bytes", "ETag": '"fake-video"',
                "Content-Range": f"bytes {first}-{last}/{len(self.server.video)}"})
        if self.path == "/upload" and self.command == "POST":
            total, digest = 0, hashlib.sha256()
            left = int(self.headers.get("Content-Length", 0))
            while left:
                body = self.rfile.read(min(16384, left))
                if not body:
                    return
                self.server.upload_started.set()
                digest.update(body)
                total += len(body)
                left -= len(body)
            return self.out(200, {"bytes": total, "sha256": digest.hexdigest()})
        if self.path == "/slow-save" and self.command == "POST":
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.server.save_started.set()
            if not self.server.save_release.wait(10):
                return self.out(500, {"error": "Fixture save was not released"})
            self.server.saved_count += 1
            return self.out(200, {"saved": True})
        return self.out(200, b"static fixture", {"Content-Type": "text/plain", "Cache-Control": "private, max-age=60"})

    do_GET = do_HEAD = do_POST = handle_request


class ServiceGatewayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="service-gateway-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.skill, self.runtime = self.root / "skill", self.root / "runtime"
        self.host = ThreadingHTTPServer(("127.0.0.1", 0), FakeHostHandler)
        self.host.daemon_threads = True
        self.host.launch_id, self.host.public_port, self.host.code_stale = secrets.token_hex(16), 0, False
        self.host.paths = {"records": str(self.skill / "cases"), "library": str(self.runtime / "library"),
                           "runtime": str(self.runtime), "code_root": str(self.root)}
        self.host.jobs, self.host.jobs_lock, self.host.seen = {}, threading.Lock(), []
        self.host.shutdown_called = False
        self.host.task_started, self.host.task_release, self.host.task_done = threading.Event(), threading.Event(), threading.Event()
        self.host.stream_started, self.host.stream_release, self.host.upload_started = threading.Event(), threading.Event(), threading.Event()
        self.host.save_started, self.host.save_release, self.host.saved_count = threading.Event(), threading.Event(), 0
        self.host.result, self.host.video = self.root / "saved-result.txt", bytes(range(256)) * 16000
        self.host_thread = threading.Thread(target=self.host.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.host_thread.start()
        self.gateways, self.children = [], []
        self.addCleanup(self.close_all)
        self.silence = patch.object(gateway.GatewayHandler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)

    def write_manifest(self, public_port, **overrides):
        self.host.public_port = public_port
        path = self.runtime / "service" / "task-host.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        value = {"app": "video-case-workbench", "role": "task-host", "protocol_version": 1,
                 "pid": os.getpid(), "port": self.host.server_port, "public_port": public_port,
                 "launch_id": self.host.launch_id, "proxy_secret": SECRET, "paths": self.host.paths}
        value.update(overrides)
        path.write_text(json.dumps(value), encoding="utf-8")

    def start_gateway(self):
        server = gateway.make_gateway(self.skill, self.runtime, port=0, root=self.root)
        self.write_manifest(server.server_port)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.gateways.append((server, thread))
        thread.start()
        return server

    def close_all(self):
        self.host.task_release.set()
        self.host.stream_release.set()
        self.host.save_release.set()
        for server, thread in self.gateways:
            if thread.is_alive():
                server.shutdown()
            server.server_close()
            thread.join(3)
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)
        self.host.shutdown()
        self.host.server_close()
        self.host_thread.join(3)

    def request(self, port, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        outgoing = {"X-Workbench-Token": TOKEN}
        outgoing.update(headers or {})
        if isinstance(body, dict):
            outgoing["Content-Type"] = "application/json"
            body = json.dumps(body).encode()
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_status_and_restart_never_forward_host_lifecycle_controls(self):
        server = self.start_gateway()
        status, _, raw = self.request(server.server_port, "/api/service/status")
        data = json.loads(raw)
        self.assertEqual((status, data["pid"], data["task_host_pid"]), (200, os.getpid(), os.getpid()))
        self.assertTrue(data["restart_keeps_tasks"])
        self.assertNotIn(SECRET.encode(), raw)
        process = Mock()
        process.poll.return_value = None
        with patch.object(server, "launch", return_value=process) as launch:
            for _ in range(2):
                self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 200)
            launch.assert_called_once_with()
        self.assertFalse(self.host.shutdown_called)
        self.assertEqual(self.request(server.server_port, "/api/service/shutdown", method="POST", body={})[0], 200)
        self.gateways[0][1].join(3)
        self.assertFalse(self.gateways[0][1].is_alive())
        self.assertFalse(self.host.shutdown_called)

    def test_browser_guards_internal_credentials_and_loopback_controls(self):
        server = self.start_gateway()
        cases = [({"Origin": "https://outside.invalid"}, 403), ({"Host": "outside.invalid"}, 403),
                 ({"Sec-Fetch-Site": "cross-site"}, 403), ({"X-Workbench-Token": "wrong"}, 403),
                 ({"X-Forwarded-For": "192.0.2.1"}, 403), ({gateway.GATEWAY_HEADER: SECRET}, 403),
                 ({gateway.FORWARDED_HEADER: "1"}, 403)]
        with patch.object(server, "launch") as launch:
            for headers, expected in cases:
                with self.subTest(headers=list(headers)):
                    self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={}, headers=headers)[0], expected)
            for body in ({"force": True}, b"[]", b"null", b""):
                self.assertEqual(self.request(server.server_port, "/api/service/shutdown", method="POST", body=body)[0], 400)
            launch.assert_not_called()
        self.assertEqual(self.request(server.server_port, "/api/service/ready")[0], 404)
        self.assertEqual(self.request(server.server_port, "/video", headers={gateway.GATEWAY_HEADER: "spoof"})[0], 403)
        self.assertEqual(self.request(self.host.server_port, "/video")[0], 403)
        self.assertEqual(self.request(server.server_port, "/video", headers={"Origin": "https://outside.invalid"})[0], 403)
        self.assertFalse(server.shutting_down)

    def test_sse_flushes_before_the_host_finishes_and_decodes_chunking(self):
        server = self.start_gateway()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/stream")
            response = connection.getresponse()
            self.assertTrue(self.host.stream_started.wait(2))
            self.assertEqual(response.read(len(b"data: first\n\n")), b"data: first\n\n")
            self.assertFalse(self.host.stream_release.is_set(), "The first event must arrive before the final event exists")
            self.assertIsNone(response.getheader("Transfer-Encoding"))
            self.assertEqual(response.getheader("Connection"), "close")
            # A stream waiting for more events owns only its own handler.
            self.assertEqual(self.request(server.server_port, "/ordinary")[0], 200)
            self.assertFalse(self.host.stream_release.is_set())
            self.host.stream_release.set()
            self.assertEqual(response.read(), b"data: second\n\n")
        finally:
            connection.close()

    def test_fixed_length_reads_reuse_the_client_socket_and_respect_close(self):
        server = self.start_gateway()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("GET", "/ordinary")
            first = client.getresponse()
            self.assertEqual(first.read(), b"static fixture")
            self.assertFalse(first.will_close)
            connected = client.sock
            self.assertIsNotNone(connected)
            for method, path, headers in (("GET", "/video", {"Range": "bytes=2-5"}),
                                           ("HEAD", "/ordinary", {}), ("GET", "/ordinary", {})):
                client.request(method, path, headers=headers)
                response = client.getresponse()
                expected = self.host.video[2:6] if path == "/video" else b"" if method == "HEAD" else b"static fixture"
                self.assertEqual(response.read(), expected)
                self.assertFalse(response.will_close)
                self.assertIs(client.sock, connected)
            client.request("GET", "/ordinary", headers={"Connection": "close"})
            closing = client.getresponse()
            self.assertEqual(closing.getheader("Connection"), "close")
            self.assertEqual(closing.read(), b"static fixture")
            self.assertIsNone(client.sock)
        finally:
            client.close()

    def test_unknown_and_incomplete_lengths_close_the_client_socket(self):
        server = self.start_gateway()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("GET", "/unknown-length")
            response = client.getresponse()
            self.assertEqual(response.getheader("Connection"), "close")
            self.assertEqual(response.read(), b"unframed fixture")
            self.assertIsNone(client.sock)
            client.request("GET", "/truncated")
            response = client.getresponse()
            with self.assertRaises(http.client.IncompleteRead):
                response.read()
            # Incomplete content must be followed by EOF, never another response.
            self.assertEqual(client.sock.recv(1), b"")
        finally:
            client.close()

    def test_reused_socket_checks_internal_credentials_on_each_request(self):
        server = self.start_gateway()
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            client.request("GET", "/ordinary")
            first = client.getresponse()
            self.assertEqual(first.read(), b"static fixture")
            self.assertIsNotNone(client.sock)
            before = list(self.host.seen)
            client.request("GET", "/ordinary", headers={gateway.GATEWAY_HEADER: "spoof"})
            rejected = client.getresponse()
            self.assertEqual(rejected.status, 403)
            self.assertNotIn(SECRET.encode(), rejected.read())
            self.assertEqual(self.host.seen, before)
            self.assertIsNone(client.sock)
        finally:
            client.close()

    def test_video_range_head_and_incremental_upload_keep_wire_behavior(self):
        server = self.start_gateway()
        status, headers, body = self.request(server.server_port, "/video", headers={"Range": "bytes=100-100099", "Accept-Encoding": "identity"})
        self.assertEqual((status, body), (206, self.host.video[100:100100]))
        self.assertEqual(headers["Content-Range"], f"bytes 100-100099/{len(self.host.video)}")
        self.assertEqual(headers["ETag"], '"fake-video"')
        self.assertEqual(self.request(server.server_port, "/video", method="HEAD")[2], b"")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        payload = b"fixture-upload-" * 100000
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", str(len(payload)))
            connection.putheader("X-Workbench-Token", TOKEN)
            connection.endheaders()
            connection.send(payload[:gateway.CHUNK_SIZE])
            self.assertTrue(self.host.upload_started.wait(2), "Uploads must reach the host before the complete body is buffered")
            connection.send(payload[gateway.CHUNK_SIZE:])
            response = connection.getresponse()
            self.assertEqual(json.loads(response.read()), {"bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()})
        finally:
            connection.close()
        self.assertEqual(self.host.seen[-1][2][gateway.FORWARDED_HEADER], "1")

    def wait_for_writes(self, server):
        with server._control_lock:
            self.assertTrue(server._control_lock.wait_for(lambda: server._active_writes == 0, timeout=2))

    def test_incomplete_upload_blocks_restart_and_shutdown_without_interrupting_upload(self):
        server = self.start_gateway()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        payload = b"u" * gateway.CHUNK_SIZE * 2
        child = Mock()
        child.poll.return_value = None
        try:
            connection.putrequest("POST", "/upload")
            connection.putheader("Content-Length", str(len(payload)))
            connection.putheader("X-Workbench-Token", TOKEN)
            connection.endheaders()
            connection.send(payload[:gateway.CHUNK_SIZE])
            self.assertTrue(self.host.upload_started.wait(2))
            with patch.object(server, "launch", return_value=child) as launch:
                for path in ("/api/service/restart", "/api/service/shutdown"):
                    status, _, body = self.request(server.server_port, path, method="POST", body={})
                    self.assertEqual(status, 409, body)
                    self.assertIn("上传", json.loads(body)["error"])
                launch.assert_not_called()
                self.assertFalse(server.shutting_down)
                self.assertEqual(self.request(server.server_port, "/api/service/status")[0], 200)
                self.assertEqual(self.request(server.server_port, "/video")[0], 200)
                connection.send(payload[gateway.CHUNK_SIZE:])
                response = connection.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())["bytes"], len(payload))
                self.wait_for_writes(server)
                self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 200)
                launch.assert_called_once_with()
            self.assertFalse(self.host.shutdown_called)
        finally:
            connection.close()

    def test_save_response_must_finish_before_restart_and_pending_restart_blocks_new_writes(self):
        server = self.start_gateway()
        completed = []
        writer = threading.Thread(target=lambda: completed.append(self.request(server.server_port, "/slow-save", method="POST", body={"save": "fixture"})), daemon=True)
        writer.start()
        child = Mock()
        child.poll.return_value = None
        try:
            self.assertTrue(self.host.save_started.wait(2))
            with patch.object(server, "launch", return_value=child) as launch:
                for path in ("/api/service/restart", "/api/service/shutdown"):
                    self.assertEqual(self.request(server.server_port, path, method="POST", body={})[0], 409)
                self.assertTrue(writer.is_alive())
                self.host.save_release.set()
                writer.join(3)
                self.assertEqual((completed[0][0], self.host.saved_count), (200, 1))
                self.wait_for_writes(server)
                self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 200)
                before = list(self.host.seen)
                self.assertEqual(self.request(server.server_port, "/upload", method="POST", body=b"new write")[0], 409)
                self.assertEqual(self.host.seen, before, "A write refused after restart admission must not reach the task host")
                self.assertEqual(self.request(server.server_port, "/api/jobs")[0], 200)
                self.assertTrue(json.loads(self.request(server.server_port, "/api/service/status")[2])["restart_pending"])
                launch.assert_called_once_with()
                # A failed or no-op launcher must not leave a still-running gateway permanently read-only.
                child.poll.return_value = 1
                self.assertFalse(json.loads(self.request(server.server_port, "/api/service/status")[2])["restart_pending"])
                self.assertEqual(self.request(server.server_port, "/upload", method="POST", body=b"retry")[0], 200)
                self.wait_for_writes(server)
                child.poll.return_value = None
                self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 200)
                child.poll.return_value = 0  # Another launcher may own the mutex and this helper simply exits.
                self.assertEqual(self.request(server.server_port, "/upload", method="POST", body=b"no-op helper retry")[0], 200)
                self.assertFalse(server._restart_pending)
        finally:
            self.host.save_release.set()
            writer.join(3)

    def test_failed_restart_helper_releases_admission_and_sse_never_blocks_restart(self):
        server = self.start_gateway()
        with patch.object(server, "launch", side_effect=OSError("fixture cannot launch")):
            self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 503)
        self.assertFalse(server._restart_pending)
        self.assertEqual(self.request(server.server_port, "/upload", method="POST", body=b"safe retry")[0], 200)
        self.wait_for_writes(server)
        stream = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        child = Mock()
        child.poll.return_value = None
        try:
            stream.request("GET", "/stream")
            response = stream.getresponse()
            self.assertEqual(response.read(len(b"data: first\n\n")), b"data: first\n\n")
            with patch.object(server, "launch", return_value=child):
                self.assertEqual(self.request(server.server_port, "/api/service/restart", method="POST", body={})[0], 200)
                self.assertEqual(self.request(server.server_port, "/api/service/shutdown", method="POST", body={})[0], 200)
            self.assertFalse(self.host.stream_release.is_set())
            self.assertFalse(self.host.shutdown_called)
        finally:
            self.host.stream_release.set()
            stream.close()

    def test_unavailable_or_unrelated_host_returns_503_without_exposing_credentials(self):
        server = self.start_gateway()
        for changed in ({"port": 0}, {"proxy_secret": "bad"},
                        {"paths": {**self.host.paths, "records": str(self.root / "other")}},
                        {"paths": {**self.host.paths, "runtime": str(self.root / "other")}},
                        {"paths": {**self.host.paths, "code_root": str(self.root / "other")}},
                        {"launch_id": "different"}):
            self.write_manifest(server.server_port, **changed)
            status, _, body = self.request(server.server_port, "/api/service/status")
            self.assertEqual(status, 503)
            self.assertNotIn(SECRET.encode(), body)
        self.write_manifest(server.server_port)
        self.assertEqual(self.request(server.server_port, "/api/service/status")[0], 200)
        server.manifest.unlink()
        self.assertEqual(self.request(server.server_port, "/video")[0], 503)

    def test_manifest_hard_links_are_rejected_without_using_the_host(self):
        server = self.start_gateway()
        other = self.root / "linked-control.json"
        os.link(server.manifest, other)
        try:
            with patch.object(server, "host_status", wraps=server.host_status):
                self.assertEqual(self.request(server.server_port, "/video")[0], 503)
            self.assertEqual(self.host.seen, [])
        finally:
            other.unlink()
        self.assertEqual(self.request(server.server_port, "/video")[0], 200)

    def test_engine_update_helper_is_once_per_launch_and_failed_start_can_retry(self):
        server = self.start_gateway()
        self.host.code_stale = True
        process = Mock()
        process.poll.return_value = None
        with patch.object(server, "launch", side_effect=[OSError("fixture cannot start"), process]) as launch:
            first = json.loads(self.request(server.server_port, "/api/service/status")[2])
            self.assertTrue(first["task_host_code_stale"])
            self.assertFalse(first["code_stale"])
            self.assertEqual(self.request(server.server_port, "/api/service/status")[0], 200)
            self.assertEqual(self.request(server.server_port, "/api/service/status")[0], 200)
            self.assertEqual(launch.call_count, 2)
            for call in launch.call_args_list:
                self.assertEqual(call.args, ("--refresh-task-host-on-idle",))

    def test_exited_helper_retries_even_after_zero_exit_but_observes_cooldown(self):
        server = self.start_gateway()
        host, _ = server.host_status()
        stale = {"code_stale": True}
        exited, running = Mock(), Mock()
        exited.poll.return_value = 0
        running.poll.return_value = None
        with patch.object(server, "launch", side_effect=[exited, running]) as launch, \
                patch("service_gateway.time.monotonic") as monotonic:
            monotonic.return_value = 100
            server.refresh_engine_when_idle(host, stale)
            server.refresh_engine_when_idle(host, stale)  # Observe the helper's exit, not success of an update.
            monotonic.return_value = 114
            server.refresh_engine_when_idle(host, stale)
            self.assertEqual(launch.call_count, 1)
            monotonic.return_value = 115
            server.refresh_engine_when_idle(host, stale)
            self.assertEqual(launch.call_count, 2)
            monotonic.return_value = 200
            server.refresh_engine_when_idle(host, stale)
            self.assertEqual(launch.call_count, 2, "A running helper must never be duplicated")
            server.refresh_engine_when_idle({**host, "launch_id": "new-owner"}, {"code_stale": False})
            self.assertEqual(server._engine_refreshes, {})

    def start_process_gateway(self, port):
        self.write_manifest(port, paths={**self.host.paths, "code_root": str(ROOT)})
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        child = subprocess.Popen([sys.executable, str(ROOT / "service_gateway.py"), "--port", str(port),
                                  "--skill-root", str(self.skill), "--runtime-root", str(self.runtime)],
                                 cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        self.children.append(child)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail("Fixture gateway exited during startup")
            try:
                status, _, body = self.request(port, "/api/service/status")
                if status == 200 and json.loads(body)["pid"] == child.pid:
                    return child
            except OSError:
                pass
            time.sleep(.02)
        self.fail("Fixture gateway did not start")

    def test_new_gateway_pid_preserves_host_task_and_exactly_one_saved_result(self):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        first = self.start_process_gateway(port)
        self.assertEqual(self.request(port, "/api/jobs", method="POST", body={"id": "paid-result"})[0], 202)
        self.assertTrue(self.host.task_started.wait(2))
        before = json.loads(self.request(port, "/api/service/status")[2])
        self.assertEqual(self.request(port, "/api/service/shutdown", method="POST", body={})[0], 200)
        first.wait(timeout=5)
        self.assertFalse(self.host.task_done.is_set())
        self.assertFalse(self.host.shutdown_called)
        second = self.start_process_gateway(port)
        after = json.loads(self.request(port, "/api/service/status")[2])
        self.assertNotEqual(first.pid, second.pid)
        self.assertEqual(before["task_host_pid"], after["task_host_pid"])
        self.assertEqual(before["task_host_launch_id"], after["task_host_launch_id"])
        # A retried browser submission keeps the host's existing request identity.
        self.assertEqual(self.request(port, "/api/jobs", method="POST", body={"id": "paid-result"})[0], 202)
        self.host.task_release.set()
        self.assertTrue(self.host.task_done.wait(3))
        self.assertEqual(json.loads(self.request(port, "/api/jobs")[2]), {"paid-result": "completed"})
        self.assertEqual(self.host.result.read_text().splitlines(), ["paid-result"])
        self.assertFalse(self.host.shutdown_called)

    def test_real_task_host_keeps_ai_running_across_gateway_replacement(self):
        from test_server import SERVER
        from test_ai_tasks import FakeBridge
        actual_skill, actual_runtime = self.root / "actual-skill", self.root / "actual-runtime"
        bridge = FakeBridge(blocked=True)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        host = SERVER.make_server(actual_skill, actual_runtime, port=0, allow_open=False,
                                  ai_bridge=bridge, task_host=True, public_port=port)
        host_thread = threading.Thread(target=host.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        host_thread.start()
        replacements = []
        def start():
            server = gateway.make_gateway(actual_skill, actual_runtime, port=port)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            thread.start()
            replacements.append((server, thread))
            return server, thread
        try:
            with patch.object(gateway.ServiceGateway, "launch", return_value=Mock(poll=lambda: None)):
                first, first_thread = start()
                status, _, body = self.request(port, "/api/service/status")
                before = json.loads(body)
                token = before["token"]
                supplied = {"X-Workbench-Token": token, "Origin": f"http://127.0.0.1:{port}"}
                self.assertEqual(status, 200)
                self.assertEqual(self.request(port, "/api/state", headers=supplied)[0], 200)
                status, _, body = self.request(port, "/api/ai/tasks", method="POST", headers=supplied, body={
                    "request_id": secrets.token_hex(16), "product": "202H", "action": "review",
                    "instruction": "Temporary isolated gateway continuity check.", "skill_selection": "none"})
                self.assertEqual(status, 200, body)
                task_id = json.loads(body)["task"]["task_id"]
                self.assertTrue(bridge.started.wait(3))
                stream = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
                try:
                    stream.request("GET", f"/api/ai/tasks/{task_id}/stream", headers=supplied)
                    response = stream.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertIn(b"data:", response.read1(65536))
                finally:
                    stream.close()
                self.assertEqual(self.request(port, "/api/service/shutdown", method="POST", body={}, headers=supplied)[0], 200)
                first_thread.join(3)
                first.server_close()
                self.assertFalse(host.shutting_down)
                self.assertFalse(host.app.ai._closing.is_set())
                self.assertEqual(host.app.ai.get(task_id)["status"], "running")
                start()
                after = json.loads(self.request(port, "/api/service/status")[2])
                self.assertEqual(after["task_host_launch_id"], before["task_host_launch_id"])
                self.assertEqual(after["token"], token)
                self.assertEqual(self.request(port, "/api/ai/tasks", method="POST", body={}, headers={"X-Workbench-Token": "invalid"})[0], 403)
                self.assertEqual(self.request(port, "/api/state", headers={"Origin": "https://outside.invalid"})[0], 403)
                bridge.gate.set()
                deadline = time.monotonic() + 4
                while time.monotonic() < deadline:
                    task = json.loads(self.request(port, f"/api/ai/tasks/{task_id}", headers=supplied)[2])["task"]
                    if not task["busy"]:
                        break
                    time.sleep(.01)
                self.assertEqual(task["status"], "completed")
                self.assertEqual(len(bridge.calls), 1)
                self.assertEqual(len(task["rounds"]), 1)
                self.assertTrue(task["rounds"][0]["output"])
        finally:
            bridge.gate.set()
            for server, thread in replacements:
                if thread.is_alive():
                    server.shutdown()
                server.server_close()
                thread.join(3)
            host.shutdown()
            host.server_close()
            host_thread.join(3)


if __name__ == "__main__":
    unittest.main()
