"""Authenticated restart handshake against temporary libraries and services only."""
from __future__ import annotations

import os
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import test_server as fixture


class ServiceShutdownTests(unittest.TestCase):
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    body = fixture.WorkbenchHTTPTests.body
    save = fixture.WorkbenchHTTPTests.save
    record_bytes = fixture.WorkbenchHTTPTests.record_bytes

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-service-shutdown-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill, self.runtime = self.root / "skill", self.root / "runtime"
        self.video = self.root / "fixture.mp4"
        self.video.write_bytes(b"temporary video fixture")
        self.store = fixture.SERVER.CaseStore(self.skill)
        case = self.store.add({"title": "测试案例", "product": "测试产品",
            "original": {"text": "Keep the original."}, "feedback": {"kind": "editorial", "text": "测试数据"},
            "current": fixture.snapshot(self.video)})
        self.identifier = case["case_id"]
        self.silence = patch.object(fixture.SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = fixture.SERVER.make_server(self.skill, self.runtime, port=0, allow_open=False,
                                                **getattr(self, "server_options", {}))

        def run_like_main():
            try:
                self.server.serve_forever(poll_interval=0.01)
            finally:
                self.server.server_close()

        self.thread = threading.Thread(target=run_like_main, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.token = self.request("GET", "/api/state")[2]["token"]

    def stop_server(self):
        if self.thread.is_alive():
            # Do not depend on an instance method patched by a failing test.
            fixture.SERVER.WorkbenchHTTPServer.shutdown(self.server)
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_status_identifies_the_responding_service_and_paths(self):
        status, _, result = self.request("GET", "/api/service/status")
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"app": "video-case-workbench", "pid": os.getpid(),
            "token": self.token, "shutdown_supported": True, "shutting_down": False, "code_stale": False,
            "paths": {"records": str(self.skill / "cases"), "library": str(self.runtime / "library")}})
        self.assertEqual(self.request("GET", "/api/service/status", headers={"Origin": "https://external.invalid"})[0], 403)

    def test_status_is_lightweight_and_never_loads_the_case_library(self):
        with patch.object(self.server.app, "state", side_effect=AssertionError("Full state must not be read")), \
             patch.object(self.server.app.store, "read_index", side_effect=AssertionError("The case index must not be read")):
            status, _, result = self.request("GET", "/api/service/status")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["pid"], os.getpid())
        self.assertEqual(result["token"], self.token)
        self.assertTrue(result["shutdown_supported"])

    def test_shutdown_rejects_missing_token_cross_origin_and_nonempty_payload(self):
        before = self.record_bytes()
        self.assertEqual(self.request("POST", "/api/service/shutdown", {}, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/service/shutdown", {}, headers={"Origin": "https://external.invalid"})[0], 403)
        for body in ({"force": True}, [], None):
            status, _, result = self.request("POST", "/api/service/shutdown", body, raw=b"null" if body is None else None)
            self.assertEqual(status, 400, result)
        self.assertFalse(self.server.shutting_down)
        self.assertEqual(self.record_bytes(), before)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_active_ai_refuses_shutdown_and_reopens_write_admission(self):
        before = self.record_bytes()
        for task in ({"status": "queued"}, {"status": "running"}, {"status": "stopping"},
                     {"status": "cancelled", "stopping": True}, {"status": "cancelled", "busy": True}):
            with self.subTest(task=task), patch.object(self.server.app.ai, "list_tasks", return_value=[task]):
                status, _, result = self.request("POST", "/api/service/shutdown", {})
                self.assertEqual(status, 409, result)
                self.assertIn("AI", result["error"])
                self.assertFalse(self.server.shutting_down)
                self.assertEqual(self.request("POST", "/api/not-a-route", {})[0], 404)
        self.assertEqual(self.record_bytes(), before)

    def test_readiness_includes_result_callbacks_and_other_background_workers(self):
        for name in ("auto_breakdown", "opportunity_skeletons", "review_scraper"):
            with self.subTest(worker=name), patch.object(getattr(self.server.app, name), "busy", return_value=True):
                status, _, result = self.request("GET", "/api/service/ready")
                self.assertEqual(status, 200, result)
                self.assertFalse(result["ready"])
                self.assertTrue(result["reason"])
                self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 409)
                self.assertFalse(self.server.shutting_down)
        with patch.object(self.server.app.ai, "background_busy", return_value=True):
            self.assertFalse(self.request("GET", "/api/service/ready")[2]["ready"])
            self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 409)
        self.assertTrue(self.request("GET", "/api/service/ready")[2]["ready"])

    def test_inflight_save_and_upload_refuse_shutdown_without_interrupting_work(self):
        for operation in ("save", "upload"):
            with self.subTest(operation=operation):
                entered, release = threading.Event(), threading.Event()
                results = []

                def blocked(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise RuntimeError("fixture operation was not released")
                    return {"ok": True} if operation == "save" else self.root / "staged.mp4"

                body = self.body() if operation == "save" else None
                target = self.server.app if operation == "save" else self.server.app.video_files
                method = "save" if operation == "save" else "stage"

                def send_write():
                    if operation == "save":
                        results.append(self.save(body))
                    else:
                        results.append(self.request("POST", "/api/upload?kind=video&name=fixture.mp4", raw=b"fixture"))

                with patch.object(target, method, side_effect=blocked):
                    writer = threading.Thread(target=send_write, daemon=True)
                    writer.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        status, _, result = self.request("POST", "/api/service/shutdown", {})
                        self.assertEqual(status, 409, result)
                        self.assertFalse(self.server.shutting_down)
                        self.assertTrue(writer.is_alive())
                    finally:
                        release.set()
                        writer.join(timeout=3)
                self.assertFalse(writer.is_alive())
                self.assertEqual(results[0][0], 200, results)
                # Reading the response can finish before the handler's finally
                # releases admission. Wait for that notification, not a sleep.
                with self.server._write_gate:
                    self.assertTrue(self.server._write_gate.wait_for(
                        lambda: self.server._active_writes == 0, timeout=2))
                    self.assertEqual(self.server._active_writes, 0)
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_accepted_shutdown_rejects_new_writes_then_stops_and_releases_port(self):
        before = self.record_bytes()
        stopping, release = threading.Event(), threading.Event()
        real_shutdown = self.server.shutdown

        def delayed_shutdown():
            stopping.set()
            release.wait(3)
            real_shutdown()

        port = self.server.server_port
        with patch.object(self.server, "shutdown", side_effect=delayed_shutdown):
            try:
                status, _, result = self.request("POST", "/api/service/shutdown", {})
                self.assertEqual(status, 200, result)
                self.assertEqual(result, {"ok": True, "pid": os.getpid()})
                self.assertTrue(stopping.wait(2))
                self.assertTrue(self.request("GET", "/api/service/status")[2]["shutting_down"])
                self.assertEqual(self.request("POST", "/api/sync", {})[0], 409)
                self.assertEqual(self.request("POST", "/api/upload?kind=video&name=rejected.mp4", raw=b"unread rejected body")[0], 409)
                self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 409)
                self.assertEqual(self.record_bytes(), before)
            finally:
                release.set()
            self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())
        replacement = fixture.SERVER.make_server(self.skill, self.runtime, port=port, allow_open=False)
        try:
            self.assertEqual(replacement.server_port, port)
            self.assertFalse(replacement.shutting_down)
        finally:
            replacement.server_close()

    def test_failed_post_releases_admission_and_ai_check_failure_does_not_lock_service(self):
        self.assertEqual(self.request("POST", "/api/not-a-route", {})[0], 404)
        # The response can reach the client before dispatch's finally runs on
        # the server thread. Wait for the cleanup notification, not scheduling.
        with self.server._write_gate:
            self.assertTrue(self.server._write_gate.wait_for(lambda: self.server._active_writes == 0, timeout=1))
        with patch.object(self.server.app.ai, "list_tasks", side_effect=OSError("fixture read failed")):
            self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 500)
        self.assertFalse(self.server.shutting_down)
        self.assertEqual(self.request("POST", "/api/not-a-route", {})[0], 404)


class TaskHostTests(unittest.TestCase):
    setUp = ServiceShutdownTests.setUp
    stop_server = ServiceShutdownTests.stop_server
    server_options = {"task_host": True, "public_port": 8767}

    def request(self, method, path, body=None, headers=None, authenticated=True, raw=None):
        supplied = {"X-Workbench-Gateway": self.server.proxy_secret, **(headers or {})}
        return fixture.WorkbenchHTTPTests.request(self, method, path, body, supplied, authenticated, raw)

    def test_host_manifest_is_private_local_and_matches_its_live_identity(self):
        manifest_path = self.runtime.resolve() / "service" / "task-host.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        status, _, live = self.request("GET", "/api/service/status")
        self.assertEqual(status, 200, live)
        self.assertEqual(manifest["proxy_secret"], self.server.proxy_secret)
        self.assertNotIn("proxy_secret", live)
        for name in ("app", "role", "pid", "port", "public_port", "launch_id", "protocol_version"):
            self.assertEqual(manifest[name], live[name])
        self.assertEqual(manifest["paths"]["runtime"], str(self.runtime.resolve()))
        self.assertEqual(manifest["paths"]["code_root"], str(fixture.SERVER.APP_ROOT))
        self.assertNotEqual(manifest["port"], manifest["public_port"])
        self.assertEqual(fixture.WorkbenchHTTPTests.request(self, "GET", "/api/service/status")[0], 403)
        self.assertEqual(self.request("GET", "/api/service/status", headers={"X-Workbench-Gateway": "wrong"})[0], 403)
        if os.name != "nt":
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)

    def test_authenticated_forwarding_keeps_public_host_origin_and_write_token_guards(self):
        forwarded = {"X-Workbench-Forwarded": "1", "Host": "127.0.0.1:8767", "Origin": "http://127.0.0.1:8767"}
        self.assertEqual(self.request("GET", "/api/service/status", headers=forwarded)[0], 200)
        for changed in ({"Origin": "https://external.invalid"}, {"Host": "external.invalid"},
                        {"Origin": f"http://127.0.0.1:{self.server.server_port}"},
                        {"Host": f"127.0.0.1:{self.server.server_port}"}, {"X-Workbench-Forwarded": "yes"},
                        {"X-Workbench-Gateway": "wrong"}):
            with self.subTest(changed=changed):
                self.assertEqual(self.request("GET", "/api/service/status", headers={**forwarded, **changed})[0], 403)
        self.assertEqual(self.request("POST", "/api/not-a-route", {}, headers=forwarded, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/not-a-route", {}, headers=forwarded)[0], 404)

    def test_forwarded_extension_pairing_still_uses_the_public_port(self):
        paired = self.request("GET", "/api/public-stats/link", headers={"X-Workbench-Forwarded": "1", "Host": "localhost:8767"})[2]
        self.assertEqual(paired["address"], "http://127.0.0.1:8767")
        key = paired["key"]
        headers = {"X-Workbench-Forwarded": "1", "Host": "localhost:8767",
                   "Origin": "chrome-extension://abcdefghijklmnop", "X-TKL-Key": key}
        self.assertEqual(self.request("POST", "/api/public-stats/ping", {}, headers=headers, authenticated=False)[0], 200)
        self.assertEqual(self.request("POST", "/api/public-stats/ping", {},
                                      headers={**headers, "X-TKL-Key": "wrong"}, authenticated=False)[0], 403)

    def test_browser_cannot_stop_host_and_control_shutdown_cleans_only_its_manifest(self):
        forwarded = {"X-Workbench-Forwarded": "1", "Host": "127.0.0.1:8767"}
        self.assertEqual(self.request("POST", "/api/service/restart", {})[0], 409)
        self.assertEqual(self.request("POST", "/api/service/shutdown", {}, headers=forwarded)[0], 403)
        status, _, ready = self.request("GET", "/api/service/ready")
        self.assertEqual((status, ready["ready"], ready["role"], ready["launch_id"]),
                         (200, True, "task-host", self.server.launch_id))
        self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 200)
        self.thread.join(3)
        self.assertFalse(self.thread.is_alive())
        self.assertFalse((self.runtime / "service" / "task-host.json").exists())

    def test_manifest_cleanup_does_not_delete_another_host_identity(self):
        path = self.runtime / "service" / "task-host.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["launch_id"] = "replacement-host"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.server.remove_task_host_manifest()
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["launch_id"], "replacement-host")

    def test_random_private_port_collision_is_reselected_before_business_initialization(self):
        with tempfile.TemporaryDirectory(prefix="host-port-reselection-") as temporary:
            root = Path(temporary).resolve()
            runtime, skill = root / "runtime", root / "skill"
            second = fixture.SERVER.WorkbenchHTTPServer(("127.0.0.1", 0), fixture.SERVER.Handler)
            public_port = 49123 if second.server_port != 49123 else 49124
            first = SimpleNamespace(server_port=public_port, server_close=Mock())
            app = SimpleNamespace(runtime=runtime, store=SimpleNamespace(root=skill), library=runtime / "library",
                                  data_transfer=Mock(), ai=Mock(), stop_edits=Mock())
            try:
                with patch.object(fixture.SERVER, "WorkbenchHTTPServer", side_effect=[first, second]) as bind, \
                        patch.object(fixture.SERVER, "Workbench", return_value=app) as initialize:
                    selected = fixture.SERVER.make_server(skill, runtime, port=0, task_host=True, public_port=public_port)
                self.assertIs(selected, second)
                self.assertNotEqual(selected.server_port, public_port)
                self.assertEqual(bind.call_count, 2)
                first.server_close.assert_called_once_with()
                initialize.assert_called_once()
                app.data_transfer.start.assert_called_once_with(public_port)
                manifest = json.loads((runtime / "service" / "task-host.json").read_text(encoding="utf-8"))
                self.assertEqual((manifest["port"], manifest["public_port"]), (second.server_port, public_port))
            finally:
                second.server_close()

    def test_explicit_private_port_cannot_claim_the_public_listener(self):
        with patch.object(fixture.SERVER, "WorkbenchHTTPServer") as bind, patch.object(fixture.SERVER, "Workbench") as initialize:
            with self.assertRaisesRegex(ValueError, "不能与网页入口相同"):
                fixture.SERVER.make_server(self.skill, self.runtime, port=8767, task_host=True, public_port=8767)
            bind.assert_not_called()
            initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
