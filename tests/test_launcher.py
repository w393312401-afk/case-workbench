"""Restart launcher checks: mocked process control, disposable files, no live restart."""
from __future__ import annotations

from contextlib import ExitStack
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from production_store import ProductionStore, VoiceError


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workbench_launcher_under_test", APP_ROOT / "launcher_control.py")
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-launcher-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "workbench with spaces"
        self.root.mkdir()
        self.config = {
            "port": 28767,
            "skill_root": str(self.base / "case skill"),
            "runtime_root": str(self.base / "case runtime"),
        }
        (self.root / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        self.executable = str(Path(sys.executable).with_name("pythonw.exe"))
        self.record = {
            "ProcessId": 101, "ExecutablePath": self.executable,
            "CreatedTicks": "134035500001234560",
            "CommandLine": subprocess.list2cmdline([self.executable, "-u", str(self.root / "server.py")]),
        }
        self.state = {
            "app": "video-case-workbench", "token": "isolated-test-token",
            "paths": {"records": str(Path(self.config["skill_root"]) / "cases"),
                      "library": str(Path(self.config["runtime_root"]) / "library")},
        }

    def process(self, arguments, **changes):
        return {**self.record, "CommandLine": subprocess.list2cmdline(arguments), **changes}

    def status(self, pid=101, **changes):
        return {**self.state, "pid": pid, "shutdown_supported": True, **changes}

    def restart_mocks(self, stack, *, processes=None, responses=None):
        mocks = {}
        for name in ["matching_processes", "ensure_no_active_tasks", "port_open",
                     "wait_for_port_release", "wait_for_process_exit", "stop_legacy_process", "read_json"]:
            mocks[name] = stack.enter_context(patch.object(LAUNCHER, name))
        mocks["matching_processes"].return_value = processes or []
        mocks["port_open"].return_value = False
        mocks["read_json"].side_effect = responses or [self.status(202)]
        mocks["spawn"] = stack.enter_context(patch.object(LAUNCHER.subprocess, "Popen"))
        child = mocks["spawn"].return_value
        child.pid, child.poll.return_value = 202, None
        mocks["browser"] = stack.enter_context(patch.object(LAUNCHER.webbrowser, "open"))
        mocks["urlopen"] = stack.enter_context(patch.object(LAUNCHER.urllib.request, "urlopen"))
        mocks["urlopen"].side_effect = lambda *args, **kwargs: io.BytesIO(b'{"ok": true}')
        mocks["clock"] = stack.enter_context(patch.object(LAUNCHER.time, "monotonic", side_effect=range(30)))
        stack.enter_context(patch.object(LAUNCHER.time, "sleep"))
        return mocks

    @unittest.skipUnless(os.name == "nt", "Uses read-only Windows command line parsing")
    def test_exact_server_arguments_exclude_other_programs_and_configurations(self):
        script = str(self.root / "server.py")
        accepted = [
            [self.executable, script],
            [self.executable, "-u", "-B", script, "--no-open"],
            [self.executable, script, "--port", str(self.config["port"]),
             "--skill-root", self.config["skill_root"], "--runtime-root", self.config["runtime_root"]],
            [self.executable, script, f'--port={self.config["port"]}',
             f'--skill-root={self.config["skill_root"]}', f'--runtime-root={self.config["runtime_root"]}'],
        ]
        for arguments in accepted:
            with self.subTest(accepted=arguments):
                self.assertTrue(LAUNCHER.matches_workbench(self.process(arguments), self.root, self.config))
        rejected = [
            [self.executable, "-c", "print('server.py')", script],
            [self.executable, "-m", "server", script],
            [self.executable, str(self.root / "other.py"), script],
            [self.executable, str(self.base / "another workbench" / "server.py")],
            [self.executable, "server.py"],
            [self.executable, script, "--port", "28768"],
            [self.executable, script, "--skill-root", str(self.base / "other skill")],
            [self.executable, script, "--runtime-root", str(self.base / "other runtime")],
            [self.executable, script, "--runtime-root", "relative-root"],
            [self.executable, script, "--port"],
            [self.executable, script, "--port", str(self.config["port"]), "--port", str(self.config["port"])],
            [self.executable, script, "--unknown"],
            [str(self.base / "different python.exe"), script],
        ]
        for arguments in rejected:
            with self.subTest(rejected=arguments):
                self.assertFalse(LAUNCHER.matches_workbench(self.process(arguments), self.root, self.config))
        for change in [{"CreatedTicks": ""}, {"CommandLine": ""}, {"ExecutablePath": str(self.base / "node.exe")}]:
            with self.subTest(missing_identity=change):
                self.assertFalse(LAUNCHER.matches_workbench({**self.record, **change}, self.root, self.config))

    @unittest.skipUnless(os.name == "nt", "Uses read-only Windows command line parsing")
    def test_inventory_filters_before_any_process_control(self):
        unrelated = self.process([self.executable, str(self.base / "different" / "server.py")], ProcessId=999)
        completed = Mock(stdout=json.dumps([self.record, unrelated]))
        with patch.object(LAUNCHER.subprocess, "run", return_value=completed) as inventory:
            self.assertEqual(LAUNCHER.matching_processes(self.root, self.config), [self.record])
        options = inventory.call_args.kwargs
        self.assertTrue(options["check"])
        self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
        self.assertNotIn("Stop-Process", inventory.call_args.args[0][-1])

    def test_changed_process_identity_is_not_terminated_and_handle_is_closed(self):
        original_ticks = int(self.record["CreatedTicks"])
        for identity in [(original_ticks + 100, self.executable),
                         (original_ticks, str(self.base / "another" / "pythonw.exe"))]:
            with self.subTest(identity=identity):
                kernel = Mock()
                kernel.OpenProcess.return_value = 123
                kernel.WaitForSingleObject.return_value = 0x102
                with patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
                     patch.object(LAUNCHER, "process_identity", return_value=identity):
                    with self.assertRaisesRegex(RuntimeError, "进程已发生变化"):
                        LAUNCHER.stop_legacy_process(self.record)
                kernel.OpenProcess.assert_called_once_with(0x100000 | 0x1000 | 0x0001, False, 101)
                kernel.TerminateProcess.assert_not_called()
                kernel.CloseHandle.assert_called_once_with(123)

    def test_verified_handle_is_held_until_old_process_exits(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.side_effect = [0x102, 0]
        kernel.TerminateProcess.return_value = True
        with patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "process_identity", return_value=(int(self.record["CreatedTicks"]), self.executable)):
            LAUNCHER.stop_legacy_process(self.record)
        kernel.TerminateProcess.assert_called_once_with(123, 0)
        kernel.WaitForSingleObject.assert_any_call(123, 10000)
        self.assertEqual(kernel.method_calls[-1][0], "CloseHandle")
        kernel.CloseHandle.assert_called_once_with(123)

    def test_already_exited_process_is_not_terminated(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "process_identity") as identity:
            LAUNCHER.stop_legacy_process(self.record)
        identity.assert_not_called()
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_graceful_wait_uses_a_readonly_handle_and_skips_reused_pid(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = 0x102
        identity = (int(self.record["CreatedTicks"]) + 100, self.executable)
        with patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "process_identity", return_value=identity):
            LAUNCHER.wait_for_process_exit(self.record)
        kernel.OpenProcess.assert_called_once_with(0x100000 | 0x1000, False, 101)
        kernel.WaitForSingleObject.assert_called_once_with(123, 0)
        kernel.TerminateProcess.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)

    def test_application_name_alone_cannot_identify_the_correct_service(self):
        self.assertTrue(LAUNCHER.correct_service(self.state, self.config))
        invalid = [
            {"app": "video-case-workbench"},
            {**self.state, "app": "other-app"},
            {**self.state, "paths": {**self.state["paths"], "records": str(self.base / "other-cases")}},
            {**self.state, "paths": {**self.state["paths"], "library": str(self.base / "other-library")}},
        ]
        for state in invalid:
            with self.subTest(state=state):
                self.assertFalse(LAUNCHER.correct_service(state, self.config))

    def test_second_shortcut_launch_does_not_queue_another_restart(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 456
        kernel.WaitForSingleObject.return_value = 0x102
        with patch.object(LAUNCHER, "ROOT", self.root), \
             patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "restart") as restart:
            self.assertEqual(LAUNCHER.main([]), 0)
        restart.assert_not_called()
        kernel.ReleaseMutex.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(456)

    def test_mutex_is_released_after_restart_failure(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 456
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(LAUNCHER, "ROOT", self.root), \
             patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "restart", side_effect=RuntimeError("fixture failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                LAUNCHER.main([])
        kernel.ReleaseMutex.assert_called_once_with(456)
        kernel.CloseHandle.assert_called_once_with(456)

    def test_main_passes_no_browser_option_to_restart(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 456
        kernel.WaitForSingleObject.return_value = 0
        with patch.object(LAUNCHER, "ROOT", self.root), \
             patch.object(LAUNCHER, "windows_kernel", return_value=kernel), \
             patch.object(LAUNCHER, "restart") as restart:
            self.assertEqual(LAUNCHER.main(["--no-browser"]), 0)
        restart.assert_called_once_with(self.root, {**self.config, "performance_sources": [], "legacy_runtime_roots": []}, no_browser=True)

    def test_unknown_port_owner_is_never_killed_and_no_service_is_spawned(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack)
            mocks["port_open"].return_value = True
            with self.assertRaisesRegex(RuntimeError, "其他程序占用"):
                LAUNCHER.restart(self.root, self.config)
            mocks["stop_legacy_process"].assert_not_called()
            mocks["spawn"].assert_not_called()
            mocks["browser"].assert_not_called()
            mocks["urlopen"].assert_not_called()

    def test_legacy_404_cleans_only_matched_instances_before_new_service_starts(self):
        processes = [self.record, {**self.record, "ProcessId": 102}]
        missing = urllib.error.HTTPError("http://fixture/api/service/status", 404, "legacy", {}, None)
        events = []
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, processes=processes,
                                       responses=[missing, self.state, self.status(202)])
            mocks["stop_legacy_process"].side_effect = lambda process: events.append(("stop", process["ProcessId"]))
            mocks["wait_for_port_release"].side_effect = lambda port: events.append(("released", port))
            child = mocks["spawn"].return_value
            def spawn(*args, **kwargs):
                events.append(("spawn", child.pid))
                return child
            mocks["spawn"].side_effect = spawn
            self.assertEqual(LAUNCHER.restart(self.root, self.config), 202)
            self.assertEqual(events, [("stop", 101), ("stop", 102), ("released", 28767), ("spawn", 202)])
            mocks["urlopen"].assert_not_called()
            mocks["browser"].assert_called_once_with("http://127.0.0.1:28767")
            requests = mocks["read_json"].call_args_list
            self.assertEqual([request.args[0].split(":28767")[-1] for request in requests],
                             ["/api/service/status", "/api/state", "/api/service/status"])
            self.assertEqual([request.kwargs["timeout"] for request in requests], [5, 30, 2])

    def test_graceful_shutdown_and_readiness_use_only_lightweight_status(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, processes=[self.record], responses=[
                self.status(101), self.status(202),
            ])
            self.assertEqual(LAUNCHER.restart(self.root, self.config), 202)
            request = mocks["urlopen"].call_args.args[0]
            self.assertEqual(request.full_url, "http://127.0.0.1:28767/api/service/shutdown")
            self.assertEqual(request.data, b"{}")
            self.assertEqual(request.get_header("X-workbench-token"), self.state["token"])
            mocks["wait_for_process_exit"].assert_called_once_with(self.record)
            mocks["stop_legacy_process"].assert_not_called()
            requests = mocks["read_json"].call_args_list
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(request.args[0].endswith("/api/service/status") for request in requests))
            self.assertEqual([request.kwargs["timeout"] for request in requests], [5, 2])

    def test_transition_service_without_status_token_uses_verified_state(self):
        status = self.status()
        del status["token"]
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, processes=[self.record], responses=[
                status, self.state, self.status(202),
            ])
            self.assertEqual(LAUNCHER.restart(self.root, self.config), 202)
            requests = mocks["read_json"].call_args_list
            self.assertEqual(requests[1].args[0], "http://127.0.0.1:28767/api/state")
            self.assertEqual(requests[1].kwargs["timeout"], 30)
            request = mocks["urlopen"].call_args.args[0]
            self.assertEqual(request.get_header("X-workbench-token"), self.state["token"])
            mocks["stop_legacy_process"].assert_not_called()

    def test_slow_legacy_state_has_longer_budget_before_matched_cleanup(self):
        processes = [self.record, {**self.record, "ProcessId": 102}, {**self.record, "ProcessId": 103}]
        missing = urllib.error.HTTPError("http://fixture/api/service/status", 404, "legacy", {}, None)
        responses = iter([missing, self.state, self.status(202)])
        def slow_state(url, timeout=3):
            if url.endswith("/api/state") and timeout < 4:
                raise TimeoutError("fixture state needs more than the former three-second budget")
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, processes=processes)
            mocks["read_json"].side_effect = slow_state
            self.assertEqual(LAUNCHER.restart(self.root, self.config, no_browser=True), 202)
            self.assertEqual([call.args[0]["ProcessId"] for call in mocks["stop_legacy_process"].call_args_list],
                             [101, 102, 103])
            state_request = next(call for call in mocks["read_json"].call_args_list
                                 if call.args[0].endswith("/api/state"))
            self.assertEqual(state_request.kwargs["timeout"], 30)

    def test_probe_and_legacy_state_timeouts_have_chinese_stage_messages_without_cleanup(self):
        missing = urllib.error.HTTPError("http://fixture/api/service/status", 404, "legacy", {}, None)
        no_token = self.status()
        del no_token["token"]
        scenarios = [
            ("status", [TimeoutError("timed out")]),
            ("legacy-state", [missing, TimeoutError("timed out")]),
            ("transition-state", [no_token, TimeoutError("timed out")]),
            ("wrapped-status", [urllib.error.URLError(TimeoutError("timed out"))]),
        ]
        messages = []
        for phase, responses in scenarios:
            with self.subTest(phase=phase), ExitStack() as stack:
                processes = [self.record] if phase == "transition-state" else [
                    self.record, {**self.record, "ProcessId": 102}, {**self.record, "ProcessId": 103},
                ]
                mocks = self.restart_mocks(stack, processes=processes, responses=responses)
                with self.assertRaises(RuntimeError) as raised:
                    LAUNCHER.restart(self.root, self.config)
                message = str(raised.exception)
                self.assertIn("超时", message)
                self.assertRegex(message, "状态|连接|识别|检查")
                messages.append(message)
                mocks["stop_legacy_process"].assert_not_called()
                mocks["spawn"].assert_not_called()
                mocks["browser"].assert_not_called()
                mocks["urlopen"].assert_not_called()
        self.assertNotEqual(messages[0], messages[1], "The failed stage should be distinguishable")

    def test_graceful_refusal_or_timeout_never_falls_back_to_force_termination(self):
        errors = [
            urllib.error.HTTPError("http://fixture/shutdown", 409, "busy", {},
                                   io.BytesIO(json.dumps({"error": "正在保存案例"}).encode())),
            TimeoutError("shutdown timed out"),
            urllib.error.URLError(TimeoutError("shutdown timed out")),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__), ExitStack() as stack:
                mocks = self.restart_mocks(stack, processes=[self.record], responses=[
                    self.status(),
                ])
                mocks["urlopen"].side_effect = error
                with self.assertRaises(RuntimeError) as raised:
                    LAUNCHER.restart(self.root, self.config)
                if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                    self.assertIn("超时", str(raised.exception))
                    self.assertRegex(str(raised.exception), "关闭|关机")
                mocks["stop_legacy_process"].assert_not_called()
                mocks["spawn"].assert_not_called()
                mocks["browser"].assert_not_called()

    def test_graceful_exit_wait_timeout_does_not_spawn_or_force_stop(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, processes=[self.record], responses=[
                self.status(),
            ])
            mocks["wait_for_process_exit"].side_effect = RuntimeError("服务还在关闭")
            with self.assertRaisesRegex(RuntimeError, "服务还在关闭"):
                LAUNCHER.restart(self.root, self.config)
            mocks["stop_legacy_process"].assert_not_called()
            mocks["spawn"].assert_not_called()
            mocks["browser"].assert_not_called()

    def test_mismatched_existing_service_identity_does_not_stop_any_process(self):
        for status in [
            self.status(paths={**self.state["paths"], "records": str(self.base / "another")}),
            self.status(999),
            self.status(shutdown_supported=False),
        ]:
            with self.subTest(status=status), ExitStack() as stack:
                mocks = self.restart_mocks(stack, processes=[self.record], responses=[status])
                with self.assertRaises(RuntimeError):
                    LAUNCHER.restart(self.root, self.config)
                mocks["stop_legacy_process"].assert_not_called()
                mocks["spawn"].assert_not_called()
                mocks["urlopen"].assert_not_called()

    def test_new_service_must_match_both_roots_and_spawned_pid_before_browser_opens(self):
        wrong_root = self.status(202, paths={**self.state["paths"], "library": str(self.base / "unrelated")})
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, responses=[
                wrong_root, self.status(999), self.status(202),
            ])
            self.assertEqual(LAUNCHER.restart(self.root, self.config), 202)
            self.assertEqual(mocks["read_json"].call_count, 3)
            self.assertTrue(all(call.args[0].endswith("/api/service/status")
                                for call in mocks["read_json"].call_args_list))
            mocks["browser"].assert_called_once_with("http://127.0.0.1:28767")
            self.assertEqual(mocks["spawn"].call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_no_browser_flag_starts_the_service_without_opening_a_tab(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack)
            self.assertEqual(LAUNCHER.restart(self.root, self.config, no_browser=True), 202)
            mocks["spawn"].assert_called_once()
            mocks["browser"].assert_not_called()
            self.assertTrue(all(call.args[0].endswith("/api/service/status")
                                for call in mocks["read_json"].call_args_list))

    def test_startup_failure_does_not_open_browser(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack)
            mocks["spawn"].return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, "启动失败"):
                LAUNCHER.restart(self.root, self.config)
            mocks["browser"].assert_not_called()

    def test_readiness_timeout_does_not_open_an_unverified_service(self):
        with ExitStack() as stack:
            mocks = self.restart_mocks(stack, responses=[self.status(999)])
            mocks["clock"].side_effect = [0, 1, 21]
            with self.assertRaisesRegex(RuntimeError, "尚未就绪"):
                LAUNCHER.restart(self.root, self.config)
            mocks["browser"].assert_not_called()
            mocks["stop_legacy_process"].assert_not_called()

    def test_desktop_error_dialog_and_log_keep_chinese_stage_context(self):
        message = "读取旧服务状态超时，尚未清理旧服务。"
        with patch.object(LAUNCHER, "ROOT", self.root), \
             patch.object(LAUNCHER, "main", side_effect=RuntimeError(message)), \
             patch.object(LAUNCHER.ctypes.windll.user32, "MessageBoxW") as dialog:
            self.assertEqual(LAUNCHER.desktop_main(), 1)
        self.assertEqual(dialog.call_args.args[1], message)
        self.assertIn(message, (self.root / "launcher-error.log").read_text(encoding="utf-8"))

    def test_active_task_records_prevent_restart_without_changing_them(self):
        path = Path(self.config["skill_root"]) / "cases" / "PVR-000000000001" / "collaboration" / "AIT-000000000001" / "task.json"
        path.parent.mkdir(parents=True)
        for status in ["queued", "running", "stopping"]:
            with self.subTest(status=status):
                path.write_text(json.dumps({"status": status}), encoding="utf-8")
                before = path.read_bytes()
                with self.assertRaisesRegex(RuntimeError, "AI 协作正在进行"):
                    LAUNCHER.ensure_no_active_tasks(self.config)
                self.assertEqual(path.read_bytes(), before)
        path.write_text(json.dumps({"status": "completed"}), encoding="utf-8")
        LAUNCHER.ensure_no_active_tasks(self.config)


class StartHousekeepingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-housekeeping-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "app"
        self.root.mkdir()
        self.config = {"skill_root": str(self.base / "skill"), "runtime_root": str(self.base / "runtime")}
        (self.base / "skill" / "cases" / "PVR-1").mkdir(parents=True)
        (self.base / "skill" / "cases" / "PVR-1" / "event.json").write_text("{}", encoding="utf-8")
        (self.base / "runtime" / "directions").mkdir(parents=True)
        (self.base / "runtime" / "directions" / "catalog.json").write_text("{}", encoding="utf-8")
        (self.base / "runtime" / "media").mkdir()
        (self.base / "runtime" / "media" / "clip.mp4").write_bytes(b"x")

    def test_backup_copies_business_records_only_and_keeps_recent_copies(self):
        folder = self.root / "archive" / "data-backups"
        folder.mkdir(parents=True)
        for index in range(LAUNCHER.BACKUP_KEEP + 2):
            with LAUNCHER.zipfile.ZipFile(folder / f"business-20260101-0000{index:02d}.zip", "w") as archive:
                archive.writestr("runtime/directions/catalog.json", "{}")
        target = LAUNCHER.backup_business_data(self.root, self.config)
        with LAUNCHER.zipfile.ZipFile(target) as archive:
            self.assertEqual(sorted(archive.namelist()), ["runtime/directions/catalog.json", "skill/cases/PVR-1/event.json"])
        self.assertEqual(len(list(folder.glob("business-*.zip"))), LAUNCHER.BACKUP_KEEP)
        self.assertTrue(target.exists())

    def test_parallel_backup_matches_zipfile_output_for_unicode_names_and_dates(self):
        records = self.base / "runtime" / "directions"
        (records / "中文 方向.json").write_text(json.dumps({"方向": "口播"}, ensure_ascii=False), encoding="utf-8")
        (records / "empty.json").write_bytes(b"")
        (records / "big.json").write_bytes(b"0123456789" * 200000)
        os.utime(records / "big.json", (1_700_000_000, 1_700_000_000))
        target = LAUNCHER.backup_business_data(self.root, self.config)
        entries = [(path, f"runtime/directions/{path.name}") for path in sorted(records.iterdir())]
        reference = self.base / "reference.zip"
        with LAUNCHER.zipfile.ZipFile(reference, "w", LAUNCHER.zipfile.ZIP_DEFLATED) as archive:
            for path, name in entries:
                archive.write(path, name)
        with LAUNCHER.zipfile.ZipFile(target) as made, LAUNCHER.zipfile.ZipFile(reference) as expected:
            self.assertIsNone(made.testzip())
            for name in expected.namelist():
                left, right = made.getinfo(name), expected.getinfo(name)
                self.assertEqual((left.CRC, left.file_size, left.date_time, left.flag_bits, left.compress_type),
                                 (right.CRC, right.file_size, right.date_time, right.flag_bits, right.compress_type))
                self.assertEqual(made.read(name), expected.read(name))
            self.assertTrue(set(expected.namelist()) <= set(made.namelist()))

    def test_backup_falls_back_to_zipfile_when_the_parallel_archive_fails_its_check(self):
        real = LAUNCHER._deflate

        def bad_checksum(entry):
            stamp, attributes, crc, size, packed = real(entry)
            return stamp, attributes, crc ^ 1, size, packed

        with patch.object(LAUNCHER, "_deflate", bad_checksum):
            target = LAUNCHER.backup_business_data(self.root, self.config)
        with LAUNCHER.zipfile.ZipFile(target) as archive:
            self.assertIsNone(archive.testzip())
            self.assertEqual(sorted(archive.namelist()), ["runtime/directions/catalog.json", "skill/cases/PVR-1/event.json"])
        self.assertEqual(list(target.parent.glob("*.partial")), [])

    def test_nothing_to_back_up_writes_nothing(self):
        self.assertIsNone(LAUNCHER.backup_business_data(self.root, {"skill_root": str(self.base / "none")}))
        self.assertFalse((self.root / "archive").exists())

    def test_large_server_log_moves_to_archive_and_small_log_stays(self):
        log = self.root / "server.log"
        log.write_bytes(b"x" * 10)
        self.assertIsNone(LAUNCHER.rotate_server_log(self.root))
        with patch.object(LAUNCHER, "LOG_LIMIT", 5):
            target = LAUNCHER.rotate_server_log(self.root)
        self.assertFalse(log.exists())
        self.assertEqual(target.read_bytes(), b"x" * 10)

    def test_housekeeping_failure_is_logged_without_blocking_start(self):
        with patch.object(LAUNCHER, "backup_business_data", side_effect=OSError("disk full")):
            LAUNCHER.prepare_start(self.root, self.config)
        self.assertIn("disk full", (self.root / "launcher-error.log").read_text(encoding="utf-8"))


class BackupCoverageTests(unittest.TestCase):
    """Production records travel with every restart backup; media stays with the full backup."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-backup-coverage-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "app"
        self.root.mkdir()
        self.skill, self.runtime = self.base / "skill", self.base / "runtime"
        self.config = {"skill_root": str(self.skill), "runtime_root": str(self.runtime)}
        (self.skill / "cases").mkdir(parents=True)
        (self.skill / "deleted_case_sources.json").write_text("[]", encoding="utf-8")

    def write(self, *names):
        for name in names:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")

    def restore(self, archive):
        """The documented recovery: runtime/ goes back to runtime_root and skill/ to skill_root."""
        with LAUNCHER.zipfile.ZipFile(archive) as source:
            for name in source.namelist():
                key, relative = name.split("/", 1)
                target = Path(self.config[key + "_root"]) / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read(name))

    def test_records_are_backed_up_while_media_caches_and_old_copies_stay_out(self):
        kept = ["production/catalog.json", "production/backups/old.json", "production/request-results/a.json",
                "production/audio/PRD-1/TAK-1/voiceover.mp3", "production/audio/PRD-1/TAK-1/alignment.json",
                "production/edits/EDT-1/candidates.json", "production/edits/EDT-1/result.json", "production/edits/EDT-1/plan.md",
                "production/operations/key/resolve.json", "production/video-publications/catalog.json",
                "production/prepublish/PPR-1/review.json", "library/PVR-1/notes.md",
                "performance/performance.sqlite", "performance/public-stats.sqlite", "performance/uploads/week.xlsx",
                "evidence/product-catalog.json", "davinci-ledger/202H.json", "task-activity.sqlite", "ai-preferences.json",
                "creation/backups/still-whole.png"]
        left_out = ["production/audio/PRD-1/TAK-1/voiceover.wav", "production/edits/EDT-1/stills/a.png",
                    "production/edits/EDT-1/stills/B.JPG", "production/prepublish/videos/1/1.mp4",
                    "production/clip-cache/key/frame.json", "production/thumb-cache/a.json", "production/catalog.lock",
                    "production/video-publications/catalog.lock", "production/.production-1.tmp",
                    "performance/performance.before-shops-20261024.sqlite", "performance/tiering-20261024-172106/performance.sqlite",
                    "performance/public-exports/public.xlsx", "evidence/video/clip.mp4",
                    "remote-access/tunnel.json", "data-transfer/settings.json", "thumbnail-cache/a.json", "work/a.json"]
        self.write(*kept, *left_out)
        target = LAUNCHER.backup_business_data(self.root, self.config)
        with LAUNCHER.zipfile.ZipFile(target) as archive:
            self.assertEqual(sorted(archive.namelist()),
                             sorted(["skill/deleted_case_sources.json", *("runtime/" + name for name in kept)]))

    def test_lost_production_data_comes_back_from_the_restart_backup(self):
        identifier = "PRD-000000000001"
        store = ProductionStore(self.runtime)
        data = store.empty()
        data["jobs"] = [{"id": identifier, "product": "202H", "title": "保留的制作单", "edits": []}]
        data["requests"]["create-job"] = {"hash": "h", "at": "2026-11-01T00:00:00+00:00",
                                          "result": {"job": {"id": identifier, "notes": "完整回执" * 8000}}}
        store.write(data)
        self.write("production/audio/" + identifier + "/TAK-1/voiceover.mp3", "production/audio/" + identifier + "/TAK-1/voiceover.wav")
        before = store.read()
        catalog = store.path.read_bytes()
        self.assertTrue(next((store.root / "request-results").glob("*.json")).is_file())
        target = LAUNCHER.backup_business_data(self.root, self.config)

        shutil.rmtree(self.runtime)
        self.assertEqual(ProductionStore(self.runtime).read()["jobs"], [])

        self.restore(target)
        restored = ProductionStore(self.runtime)
        self.assertEqual(restored.path.read_bytes(), catalog)
        self.assertEqual(restored.read(), before)
        self.assertEqual(restored.job(identifier)["title"], "保留的制作单")
        audio = self.runtime / "production" / "audio" / identifier / "TAK-1"
        self.assertTrue((audio / "voiceover.mp3").is_file())
        self.assertFalse((audio / "voiceover.wav").exists())

    def test_catalog_alone_could_not_restore_production(self):
        """Why request-results is in scope: reading the catalog fails without the receipts it points to."""
        store = ProductionStore(self.runtime)
        data = store.empty()
        data["requests"]["large"] = {"hash": "h", "at": "2026-11-01T00:00:00+00:00", "result": {"notes": "完整回执" * 8000}}
        store.write(data)
        shutil.rmtree(store.root / "request-results")
        store.clear_cache()
        with self.assertRaises(VoiceError):
            store.read()


class BackupRetentionTests(unittest.TestCase):
    """A wipe must never cost the complete archives, however often the workbench restarts afterwards."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-backup-retention-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "app"
        self.folder = self.root / "archive" / "data-backups"
        self.folder.mkdir(parents=True)
        self.runtime = self.base / "runtime"
        self.config = {"runtime_root": str(self.runtime)}
        self.seed()

    def seed(self):
        for source, count in (("production", 20), ("creation", 20), ("directions", 4)):
            (self.runtime / source).mkdir(parents=True, exist_ok=True)
            (self.runtime / source / "catalog.json").write_bytes(b"c" * 2000)
            for index in range(count - 1):
                (self.runtime / source / f"{index}.json").write_bytes(b"{}")

    def backup_at(self, stamp):
        with patch.object(LAUNCHER.time, "strftime", return_value=stamp):
            return LAUNCHER.backup_business_data(self.root, self.config)

    def age(self, path, days):
        moment = time.time() - days * 86400
        os.utime(path, (moment, moment))

    def names(self):
        return sorted(path.name for path in self.folder.glob("business-*.zip"))

    def log(self):
        return (self.root / "launcher-error.log").read_text(encoding="utf-8")

    def test_repeated_restarts_after_a_wipe_keep_every_complete_backup(self):
        complete = [self.backup_at(f"20261031-{index:06d}").name for index in range(LAUNCHER.BACKUP_KEEP)]
        shutil.rmtree(self.runtime)
        (self.runtime / "directions").mkdir(parents=True)
        (self.runtime / "directions" / "catalog.json").write_bytes(b"{}")
        thin = [self.backup_at(f"20261101-{index:06d}") for index in range(LAUNCHER.BACKUP_KEEP * 2)]
        self.assertEqual(thin[-1].name, f"business-20261101-{LAUNCHER.BACKUP_KEEP * 2 - 1:06d}-thin.zip")
        self.assertTrue(thin[-1].is_file())
        names = self.names()
        self.assertEqual([name for name in names if "-thin" not in name], complete)
        self.assertEqual([name for name in names if "-thin" in name], [path.name for path in thin[-LAUNCHER.BACKUP_THIN_KEEP:]])
        self.assertIn("少了很多", self.log())
        self.assertIn(thin[-1].name, self.log())

    def test_data_coming_back_is_complete_again_and_compared_with_the_last_complete_backup(self):
        first = self.backup_at("20261101-100000")
        shutil.rmtree(self.runtime / "creation")
        thin = self.backup_at("20261101-110000")
        self.assertIn("-thin", thin.name)
        self.assertIn("runtime/creation", self.log())
        self.assertTrue(first.is_file())
        self.seed()
        self.assertNotIn("-thin", self.backup_at("20261101-120000").name)

    def test_one_lost_source_marks_the_backup_even_when_the_others_grew(self):
        self.backup_at("20261101-100000")
        shutil.rmtree(self.runtime / "production")
        for index in range(100):
            (self.runtime / "creation" / f"new-{index}.json").write_bytes(b"{}")
        self.assertIn("-thin", self.backup_at("20261101-110000").name)
        self.assertIn("runtime/production", self.log())

    def test_emptied_catalog_is_caught_although_every_file_remains(self):
        self.backup_at("20261101-100000")
        with patch.object(LAUNCHER, "BACKUP_CORE_FILES", ("runtime/production/catalog.json",)):
            (self.runtime / "production" / "catalog.json").write_bytes(b"{}")
            self.assertIn("-thin", self.backup_at("20261101-110000").name)
        self.assertIn("runtime/production/catalog.json", self.log())

    def test_ordinary_changes_stay_complete(self):
        self.backup_at("20261101-100000")
        for index in range(5):
            (self.runtime / "production" / f"{index}.json").unlink()
        (self.runtime / "performance").mkdir()
        (self.runtime / "performance" / "performance.sqlite").write_bytes(b"new source")
        (self.runtime / "creation" / "catalog.json").write_bytes(b"c" * 1000)
        self.assertNotIn("-thin", self.backup_at("20261101-110000").name)
        self.assertFalse((self.root / "launcher-error.log").exists())

    def test_existing_unmarked_empty_backup_is_marked_and_stops_counting(self):
        self.backup_at("20261101-100000")
        with LAUNCHER.zipfile.ZipFile(self.folder / "business-20261101-110000.zip", "w") as archive:
            archive.writestr("runtime/directions/catalog.json", "{}")
        self.backup_at("20261101-120000")
        self.assertEqual(self.names(), ["business-20261101-100000.zip", "business-20261101-110000-thin.zip", "business-20261101-120000.zip"])

    def test_unreadable_archive_is_never_the_comparison_baseline(self):
        self.backup_at("20261101-100000")
        (self.folder / "business-20261101-110000.zip").write_bytes(b"not a zip")
        self.backup_at("20261101-120000")
        self.assertEqual(self.names(), ["business-20261101-100000.zip", "business-20261101-110000-thin.zip", "business-20261101-120000.zip"])
        shutil.rmtree(self.runtime / "production")
        self.assertIn("-thin", self.backup_at("20261101-130000").name)

    def test_deliberate_cleanup_is_accepted_after_the_baseline_expires(self):
        old = self.backup_at("20261001-100000")
        self.age(old, LAUNCHER.BACKUP_BASELINE_DAYS + 1)
        shutil.rmtree(self.runtime / "production")
        self.assertNotIn("-thin", self.backup_at("20261101-110000").name)
        self.assertTrue(old.is_file())

    def test_burst_of_restarts_keeps_one_complete_backup_for_each_recent_day(self):
        moments = []
        for days_ago in range(25):
            for hours in (0, 6):
                path = self.folder / f"business-{20000000 + 1000 - days_ago * 2 - (hours > 0)}-000000.zip"
                with LAUNCHER.zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("runtime/directions/catalog.json", "{}")
                moment = time.time() - days_ago * 86400 - hours * 3600
                os.utime(path, (moment, moment))
                moments.append((moment, path))
        LAUNCHER.prune_backups(self.folder)
        moments.sort(reverse=True)
        for _, path in moments[:LAUNCHER.BACKUP_KEEP]:
            self.assertTrue(path.is_file())
        kept = {LAUNCHER.date.fromtimestamp(moment) for moment, path in moments if path.exists()}
        recent = {LAUNCHER.date.fromtimestamp(moment) for moment, _ in moments if moment > time.time() - (LAUNCHER.BACKUP_DAYS - 1) * 86400}
        self.assertLessEqual(recent, kept)
        self.assertLessEqual(len(self.names()), LAUNCHER.BACKUP_KEEP + LAUNCHER.BACKUP_DAYS + 1)
        self.assertFalse([path for moment, path in moments if path.exists() and moment < time.time() - (LAUNCHER.BACKUP_DAYS + 1) * 86400])


if __name__ == "__main__":
    unittest.main()
