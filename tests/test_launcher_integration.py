"""Real launcher restart handshake, isolated to temporary paths and an unused port."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

import test_server as fixture


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("launcher_integration_under_test", APP_ROOT / "launcher_control.py")
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


@unittest.skipUnless(os.name == "nt", "Desktop launcher uses Windows process identities")
class LauncherIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-launcher-integration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app_root = self.root / "app"
        self.app_root.mkdir()
        self.skill, self.runtime = self.root / "skill", self.root / "runtime"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        self.assertNotEqual(port, 8767)
        self.config = {"skill_root": str(self.skill), "runtime_root": str(self.runtime), "port": port}
        self.state_calls = self.app_root / "forbidden-state-calls.txt"
        (self.app_root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        wrapper = "\n".join([
            "import importlib.util, json, sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(APP_ROOT)!r})",
            f"spec = importlib.util.spec_from_file_location('temporary_workbench_server', {str(APP_ROOT / 'server.py')!r})",
            "module = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(module)",
            "def forbidden_full_state(self):",
            "    with (Path(__file__).parent / 'forbidden-state-calls.txt').open('a', encoding='utf-8') as log:",
            "        log.write('full state requested\\n')",
            "    raise AssertionError('Launcher must not enumerate the full case library')",
            "module.Workbench.state = forbidden_full_state",
            "config = json.loads((Path(__file__).parent / 'config.json').read_text(encoding='utf-8'))",
            "server = module.make_server(config['skill_root'], config['runtime_root'], port=config['port'], allow_open=False)",
            "try:",
            "    server.serve_forever(poll_interval=0.02)",
            "finally:",
            "    server.server_close()",
            "",
        ])
        (self.app_root / "server.py").write_text(wrapper, encoding="utf-8")
        self.store = fixture.SERVER.CaseStore(self.skill)
        current = {"title": "临时测试案例", "category": "待验证与整理", "conclusion": "仅用于重启验证。",
                   "kind": "editorial", "assessments": [], "content_origin": "self_produced"}
        case = self.store.add({"title": "临时原始标题", "product": "测试产品",
            "original": {"text": "Immutable original fixture."}, "feedback": {"kind": "editorial", "text": "首次临时收录"},
            "current": current})
        self.case_id = case["case_id"]
        self.store.append(case["case_id"], {"kind": "editorial", "text": "保留一份历史快照。",
            "current": {**current, "conclusion": "确认重启不能改写此临时案例。"}})
        self.url = f"http://127.0.0.1:{port}"
        self.children = {}
        self.addCleanup(self.close_owned_service)

    def records(self):
        return {str(path.relative_to(self.skill)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (self.skill / "cases").rglob("*.json")}

    def close_owned_service(self):
        living = [child for child in self.children.values() if child.poll() is None]
        if living:
            status = LAUNCHER.read_json(self.url + "/api/service/status", timeout=3)
            self.assertIn(status["pid"], self.children)
            self.assertTrue(LAUNCHER.correct_service(status, self.config))
            self.assertIsInstance(status["token"], str)
            self.assertTrue(status["token"])
            request = urllib.request.Request(self.url + "/api/service/shutdown", data=b"{}",
                headers={"Content-Type": "application/json", "X-Workbench-Token": status["token"]})
            with urllib.request.urlopen(request, timeout=5) as response:
                acknowledgement = json.load(response)
            self.assertEqual(acknowledgement, {"ok": True, "pid": status["pid"]})
        for child in self.children.values():
            self.assertEqual(child.wait(timeout=10), 0)
        self.assertFalse(self.state_calls.exists(), "The launcher or cleanup requested full state")

    def test_cold_start_then_graceful_restart_keep_one_service_and_immutable_records(self):
        before = self.records()
        real_popen = subprocess.Popen

        def record_temporary_child(command, *args, **kwargs):
            child = real_popen(command, *args, **kwargs)
            if command == [sys.executable, "-u", str(self.app_root / "server.py")]:
                self.children[child.pid] = child
                self.assertEqual(kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW)
            return child

        with patch.object(LAUNCHER.subprocess, "Popen", side_effect=record_temporary_child), \
             patch.object(LAUNCHER.webbrowser, "open", side_effect=AssertionError("The integration test must not open a browser")), \
             patch.object(LAUNCHER, "stop_legacy_process", side_effect=AssertionError("Modern fixture must shut down through its authenticated API")):
            first = LAUNCHER.restart(self.app_root, self.config, no_browser=True)
            self.assertIn(first, self.children)
            first_status = LAUNCHER.read_json(self.url + "/api/service/status")
            self.assertEqual(first_status["pid"], first)
            self.assertTrue(first_status["shutdown_supported"])
            self.assertTrue(first_status["token"])
            self.assertTrue(LAUNCHER.correct_service(first_status, self.config))
            self.assertEqual([process["ProcessId"] for process in LAUNCHER.matching_processes(self.app_root, self.config)], [first])
            self.assertEqual(self.records(), before)

            second = LAUNCHER.restart(self.app_root, self.config, no_browser=True)
            self.assertNotEqual(second, first)
            self.assertIn(second, self.children)
            self.assertEqual(self.children[first].wait(timeout=5), 0)
            second_status = LAUNCHER.read_json(self.url + "/api/service/status")
            self.assertEqual(second_status["pid"], second)
            self.assertFalse(second_status["shutting_down"])
            self.assertTrue(LAUNCHER.correct_service(second_status, self.config))
            self.assertEqual([process["ProcessId"] for process in LAUNCHER.matching_processes(self.app_root, self.config)], [second])
            self.assertEqual(self.records(), before)
            self.assertFalse(self.state_calls.exists())
            self.assertEqual(self.store.read_case(self.case_id)["current"]["content_origin"], "self_produced")
            log = (self.app_root / "server.log").read_text(encoding="utf-8")
            self.assertNotIn(first_status["token"], log)
            self.assertNotIn(second_status["token"], log)

    def test_slow_legacy_state_migrates_once_without_changing_records(self):
        before = self.records()
        requests = []
        legacy_token = "temporary-legacy-fixture-token-must-stay-private"
        legacy_state = {"app": "video-case-workbench", "token": legacy_token,
                        "paths": {"records": str(self.skill / "cases"),
                                  "library": str(self.runtime / "library")}}

        class LegacyHandler(BaseHTTPRequestHandler):
            def do_GET(handler):
                requests.append((handler.path, handler.headers.get("X-Workbench-Token")))
                if handler.path == "/api/service/status":
                    code, result = 404, {"error": "Legacy service has no status endpoint"}
                elif handler.path == "/api/state":
                    time.sleep(3.5)  # Reproduces libraries slower than read_json's former 3s default.
                    code, result = 200, legacy_state
                else:
                    code, result = 404, {"error": "Unexpected fixture endpoint"}
                payload = json.dumps(result).encode("utf-8")
                handler.send_response(code)
                handler.send_header("Content-Type", "application/json")
                handler.send_header("Content-Length", str(len(payload)))
                handler.end_headers()
                handler.wfile.write(payload)

            def log_message(handler, *_args):
                pass

        legacy_server = ThreadingHTTPServer(("127.0.0.1", self.config["port"]), LegacyHandler)
        legacy_thread = threading.Thread(target=legacy_server.serve_forever,
                                         kwargs={"poll_interval": 0.02}, daemon=True)
        legacy_thread.start()
        legacy_closed = False

        def close_legacy_fixture():
            nonlocal legacy_closed
            if not legacy_closed:
                legacy_server.shutdown()
                legacy_server.server_close()
                legacy_thread.join(timeout=5)
                self.assertFalse(legacy_thread.is_alive())
                legacy_closed = True

        self.addCleanup(close_legacy_fixture)
        # Synthetic identity belongs only to this in-process HTTP fixture. It must
        # never reach a Windows process API or any real process termination path.
        legacy_process = {"ProcessId": -1, "ExecutablePath": sys.executable,
                          "CommandLine": subprocess.list2cmdline([sys.executable, str(self.app_root / "server.py")]),
                          "CreatedTicks": "1"}

        def temporary_inventory(root, config):
            self.assertEqual(root, self.app_root)
            self.assertEqual(config, self.config)
            return [legacy_process]

        def stop_only_legacy_fixture(process):
            self.assertEqual(process, legacy_process)
            close_legacy_fixture()

        real_popen = subprocess.Popen

        def record_temporary_child(command, *args, **kwargs):
            self.assertEqual(command, [sys.executable, "-u", str(self.app_root / "server.py")])
            self.assertEqual(kwargs.get("cwd"), str(self.app_root))
            self.assertEqual(kwargs.get("creationflags"), subprocess.CREATE_NO_WINDOW)
            child = real_popen(command, *args, **kwargs)
            self.children[child.pid] = child
            return child

        with patch.object(LAUNCHER, "matching_processes", side_effect=temporary_inventory), \
             patch.object(LAUNCHER, "stop_legacy_process", side_effect=stop_only_legacy_fixture) as stop_legacy, \
             patch.object(LAUNCHER, "wait_for_process_exit", side_effect=AssertionError("Synthetic legacy identity must not reach process APIs")), \
             patch.object(LAUNCHER.subprocess, "Popen", side_effect=record_temporary_child), \
             patch.object(LAUNCHER.webbrowser, "open", side_effect=AssertionError("The integration test must not open a browser")):
            started = time.monotonic()
            child_pid = LAUNCHER.restart(self.app_root, self.config, no_browser=True)
            elapsed = time.monotonic() - started

        self.assertGreater(elapsed, 3)
        self.assertIn(child_pid, self.children)
        self.assertTrue(legacy_closed)
        stop_legacy.assert_called_once_with(legacy_process)
        self.assertEqual(requests, [("/api/service/status", None), ("/api/state", None)])
        status = LAUNCHER.read_json(self.url + "/api/service/status")
        self.assertEqual(status["pid"], child_pid)
        self.assertTrue(LAUNCHER.correct_service(status, self.config))
        self.assertTrue(status["token"])
        self.assertNotEqual(status["token"], legacy_token)
        self.assertFalse(self.state_calls.exists())
        self.assertEqual(self.records(), before)
        log = (self.app_root / "server.log").read_text(encoding="utf-8")
        self.assertNotIn(legacy_token, log)
        self.assertNotIn(status["token"], log)


if __name__ == "__main__":
    unittest.main()
