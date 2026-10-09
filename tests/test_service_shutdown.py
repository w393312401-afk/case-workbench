"""Authenticated restart handshake against temporary libraries and services only."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

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
        self.server = fixture.SERVER.make_server(self.skill, self.runtime, port=0, allow_open=False)

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
            "token": self.token, "shutdown_supported": True, "shutting_down": False,
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
        self.assertEqual(self.server._active_writes, 0)
        with patch.object(self.server.app.ai, "list_tasks", side_effect=OSError("fixture read failed")):
            self.assertEqual(self.request("POST", "/api/service/shutdown", {})[0], 500)
        self.assertFalse(self.server.shutting_down)
        self.assertEqual(self.request("POST", "/api/not-a-route", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
