"""Job-specific App Server settings and file tools; never invoke a real model."""
import base64
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_stills_bridge as module
from codex_bridge import BridgeCancelled, BridgeError, CodexBridge
from codex_stills_bridge import CodexStillsBridge, DISABLED_FEATURES
from test_codex_bridge import FakeProcess, agent


def merge(left, right):
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(left.get(key), dict):
            merge(left[key], value)
        else:
            left[key] = copy.deepcopy(value)
    return left


def complete(process, message):
    process.begin(message)
    process.notice("item/completed", turnId="turn-1", item={"type": "imageView", "id": "frame-view"})
    process.finish([agent("final", "已读取本次静帧并整理候选。", "final_answer")])


class ConfigProcess(FakeProcess):
    def __init__(self, scenario, config):
        super().__init__(scenario)
        self.config = config
        self.thread_result = {"thread": {"id": "thread-1"}, "model": "actual-stills-model", "reasoningEffort": "medium"}

    def receive(self, message):
        if message.get("method") == "config/read":
            self.received.append(message)
            self.response(message["id"], {"config": self.config})
        else:
            super().receive(message)


class Guard:
    def __init__(self, process):
        self.process = process
        self.closed = False

    def close(self):
        self.closed = True


class CodexStillsBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-stills-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.job = self.folder / "job"
        self.job.mkdir()
        self.mcp = self.folder / "ResolveMCP.exe"
        self.mcp.write_bytes(b"not executed")
        self.cancel, self.progress, self.events = threading.Event(), [], []
        self.profile = {"rules": "只读已有达芬奇项目，查看后切回原时间线和播放头。", "timeout": 5400, "dirs": [],
                        "mcp": {"resolve": {"command": str(self.mcp), "args": []}},
                        "allowed": ["Read", "Write", "Edit", "Glob", "Grep", "mcp__resolve__run_script", "mcp__resolve__get_scripting_api"],
                        "denied": ["mcp__resolve__run_script_unsafe", "mcp__resolve__delete_lut"]}
        self.config = {"mcp_servers": {"unrelated_remote": {"enabled": True, "url": "https://unrelated.invalid/"},
                                       "davinci": {"enabled": True, "command": "unrelated-real-mcp"}},
                       "features": {"apps": True, "plugins": True}, "web_search": "live"}
        self.processes, self.launches, self.guards = [], [], []
        locator = patch("codex_bridge.locate_codex", return_value="fake-codex.exe")
        locator.start()
        self.addCleanup(locator.stop)
        def guard(process):
            value = Guard(process)
            self.guards.append(value)
            return value
        patcher = patch.object(module, "_OwnedProcessTree", side_effect=guard)
        patcher.start()
        self.addCleanup(patcher.stop)

    def bridge(self, scenario=complete, *, timeout=2, config_transform=None):
        def factory(arguments, **options):
            config = copy.deepcopy(self.config)
            for number, argument in enumerate(arguments):
                if argument == "-c":
                    merge(config, tomllib.loads(arguments[number + 1]))
            if len(self.processes) == 1 and config_transform:
                config_transform(config)
            process = ConfigProcess(scenario, config)
            self.processes.append(process)
            self.launches.append((arguments, options))
            return process
        return CodexStillsBridge(timeout=timeout, startup_timeout=.5, popen_factory=factory)

    def run_bridge(self, bridge, **values):
        return bridge.run("从本次素材读取候选，不改项目。", cwd=str(self.job), on_progress=self.progress.append,
                          cancel_event=self.cancel, on_event=self.events.append, profile=self.profile, **values)

    def requests(self, method, process=None):
        return [item["params"] for item in (process or self.processes[-1]).received if item.get("method") == method]

    def assert_closed(self):
        self.assertTrue(all(guard.closed for guard in self.guards))
        self.assertTrue(all(process.returncode is not None for process in self.processes))

    def test_success_disables_global_tools_and_uses_actual_model_metadata_in_job_directory(self):
        result = self.run_bridge(self.bridge(), model="requested-model", reasoning_effort="high")
        self.assertIn("整理候选", result)
        self.assertEqual(len(self.processes), 2)
        self.assertFalse(self.requests("thread/start", self.processes[0]))
        self.assertFalse(self.requests("turn/start", self.processes[0]))
        config = self.processes[1].config
        self.assertFalse(config["mcp_servers"]["unrelated_remote"]["enabled"])
        self.assertFalse(config["mcp_servers"]["davinci"]["enabled"])
        own = config["mcp_servers"]["pvr_stills_resolve"]
        self.assertEqual(own["enabled_tools"], ["get_scripting_api", "run_script"])
        self.assertEqual(own["command"], str(self.mcp.resolve()))
        self.assertTrue(all(config["features"][name] is False for name in DISABLED_FEATURES))
        self.assertEqual(config["web_search"], "disabled")
        self.assertTrue(config["features"]["view_image"])
        self.assertTrue(config["features"]["code_mode_host"])
        self.assertFalse(config["features"]["code_mode"])
        for arguments, options in self.launches:
            self.assertIn("--strict-config", arguments)
            self.assertEqual(options["cwd"], str(self.job))
            self.assertFalse(options["shell"])
        thread = self.requests("thread/start")[0]
        self.assertEqual((thread["ephemeral"], thread["sandbox"], thread["approvalPolicy"]), (True, "workspace-write", "never"))
        self.assertEqual(thread["cwd"], str(self.job))
        self.assertEqual({tool["name"] for tool in thread["dynamicTools"]}, {"Read", "Write", "Edit", "Glob", "Grep"})
        self.assertEqual(thread["model"], "requested-model")
        self.assertEqual(thread["config"]["model_reasoning_effort"], "high")
        self.assertTrue(thread["config"]["features"]["code_mode_host"])
        turn = self.requests("turn/start")[0]
        self.assertEqual((turn["model"], turn["effort"]), ("actual-stills-model", "medium"))
        self.assertEqual(turn["sandboxPolicy"]["writableRoots"], [str(self.job)])
        self.assertFalse(turn["sandboxPolicy"]["networkAccess"])
        self.assertTrue(turn["sandboxPolicy"]["excludeSlashTmp"])
        self.assertTrue(turn["sandboxPolicy"]["excludeTmpdirEnvVar"])
        session = next(event for event in self.events if event["type"] == "session")
        self.assertEqual((session["model"], session["reasoning_effort"]), ("actual-stills-model", "medium"))
        self.assertFalse(any("分析" in value for value in self.progress))
        self.assert_closed()

    def test_default_model_omission_and_unknown_actual_metadata_remain_unknown(self):
        bridge = self.bridge()
        original = bridge.popen_factory
        def factory(arguments, **options):
            process = original(arguments, **options)
            process.thread_result = {"thread": {"id": "thread-1"}}
            return process
        bridge.popen_factory = factory
        self.run_bridge(bridge)
        self.assertNotIn("model", self.requests("thread/start")[0])
        self.assertNotIn("model", self.requests("turn/start")[0])
        self.assertNotIn("effort", self.requests("turn/start")[0])
        session = next(event for event in self.events if event["type"] == "session")
        self.assertIsNone(session["model"])
        self.assertIsNone(session["reasoning_effort"])

    def test_effective_global_server_still_enabled_fails_before_thread_or_turn(self):
        bridge = self.bridge(config_transform=lambda config: config["mcp_servers"]["unrelated_remote"].update(enabled=True))
        with self.assertRaises(BridgeError):
            self.run_bridge(bridge)
        self.assertFalse(self.requests("thread/start"))
        self.assertFalse(self.requests("turn/start"))
        self.assert_closed()

    def test_effective_allowlist_or_plugin_failure_fails_before_model_turn(self):
        for field in ("allowlist", "plugins", "network"):
            with self.subTest(field=field):
                self.processes.clear()
                self.launches.clear()
                self.guards.clear()
                def transform(config):
                    if field == "allowlist":
                        config["mcp_servers"]["pvr_stills_resolve"]["enabled_tools"].append("run_script_unsafe")
                    elif field == "plugins":
                        config["features"]["plugins"] = True
                    else:
                        config["web_search"] = "live"
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(config_transform=transform))
                self.assertFalse(self.requests("turn/start"))
                self.assert_closed()

    def test_disabled_tool_host_or_image_view_fails_before_model_turn(self):
        for feature in ("code_mode_host", "view_image"):
            with self.subTest(feature=feature):
                self.processes.clear()
                self.launches.clear()
                self.guards.clear()
                bridge = self.bridge(config_transform=lambda config: config["features"].update({feature: False}))
                with self.assertRaisesRegex(BridgeError, "工具运行或看图功能未启用"):
                    self.run_bridge(bridge)
                self.assertFalse(self.requests("thread/start"))
                self.assertFalse(self.requests("turn/start"))
                self.assert_closed()

    def test_profile_rejects_unsafe_or_unrelated_tools_and_extra_write_directories(self):
        profiles = [{**self.profile, "allowed": [*self.profile["allowed"], value]} for value in
                    ("mcp__resolve__run_script_unsafe", "mcp__resolve__delete_lut", "mcp__notion__search", "Bash")]
        profiles += [{**self.profile, "dirs": [str(self.folder)]}, {**self.profile, "mcp": {**self.profile["mcp"], "unrelated": {"command": str(self.mcp)}}}]
        for profile in profiles:
            with self.subTest(profile=profile):
                self.profile = profile
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge())
        self.assertFalse(self.launches)

    def test_denied_file_tools_are_not_advertised(self):
        self.profile["denied"].append("Write")
        self.run_bridge(self.bridge())
        self.assertNotIn("Write", {tool["name"] for tool in self.requests("thread/start")[0]["dynamicTools"]})

    def test_job_file_tools_write_edit_read_glob_and_literal_grep_without_shell(self):
        def scenario(process, message):
            process.begin(message)
            for identifier, tool, values in (
                (101, "Write", {"file_path": "candidates.json", "content": "[]\n"}),
                (102, "Edit", {"file_path": "candidates.json", "old_string": "[]", "new_string": '[{"slide":1}]'}),
                (103, "Read", {"file_path": "candidates.json"}),
                (104, "Glob", {"pattern": "*.json"}),
                (105, "Grep", {"pattern": "slide", "glob": "*.json"}),
            ):
                process.emit({"id": identifier, "method": "item/tool/call", "params": {"threadId": "thread-1", "turnId": "turn-1", "callId": str(identifier), "tool": tool, "arguments": values}})
            process.finish([agent("final", "文件已保存。", "final_answer")])
        self.run_bridge(self.bridge(scenario))
        self.assertEqual(json.loads((self.job / "candidates.json").read_text()), [{"slide": 1}])
        responses = {item["id"]: item["result"] for item in self.processes[-1].received if item.get("id") in range(101, 106)}
        self.assertTrue(all(response["success"] for response in responses.values()))
        self.assertIn("slide", responses[103]["contentItems"][0]["text"])
        self.assertIn("candidates.json", responses[104]["contentItems"][0]["text"])
        self.assertIn("candidates.json:1", responses[105]["contentItems"][0]["text"])
        self.assert_closed()

    def test_file_tools_reject_escape_and_protected_input_without_writing_other_files(self):
        (self.job / "handoff.md").write_text("protected", encoding="utf-8")
        def scenario(process, message):
            process.begin(message)
            for identifier, path in ((101, "../outside.txt"), (102, "handoff.md"), (103, ".codex/config.toml")):
                process.emit({"id": identifier, "method": "item/tool/call", "params": {"threadId": "thread-1", "turnId": "turn-1", "callId": str(identifier),
                             "tool": "Write", "arguments": {"file_path": path, "content": "not allowed"}}})
            process.finish([agent("final", "已记录缺口。", "final_answer")])
        self.run_bridge(self.bridge(scenario))
        self.assertFalse((self.folder / "outside.txt").exists())
        self.assertEqual((self.job / "handoff.md").read_text(), "protected")
        self.assertFalse((self.job / ".codex").exists())
        responses = [item["result"] for item in self.processes[-1].received if item.get("id") in (101, 102, 103)]
        self.assertTrue(all(response["success"] is False for response in responses))

    def test_unlisted_dynamic_tool_or_another_turn_is_rejected_and_owned_sessions_close(self):
        for tool, turn in (("Bash", "turn-1"), ("Write", "unrelated-turn")):
            with self.subTest(tool=tool, turn=turn):
                self.processes.clear()
                self.guards.clear()
                def scenario(process, message):
                    process.begin(message)
                    process.emit({"id": 101, "method": "item/tool/call", "params": {"threadId": "thread-1", "turnId": turn, "callId": "call", "tool": tool,
                                 "arguments": {"file_path": "outside.txt", "content": "wrong"}}})
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(scenario))
                self.assertFalse((self.job / "outside.txt").exists())
                self.assert_closed()

    def test_cancel_before_start_never_launches_and_cancelled_turn_interrupts_only_own_session(self):
        self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.run_bridge(self.bridge())
        self.assertFalse(self.launches)
        self.cancel.clear()
        def scenario(process, message):
            process.begin(message)
        bridge = self.bridge(scenario)
        def progress(value):
            self.progress.append(value)
            if "已开始" in value:
                self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            bridge.run("只读取本次项目", cwd=str(self.job), on_progress=progress, cancel_event=self.cancel, profile=self.profile)
        self.assertEqual(self.requests("turn/interrupt"), [{"threadId": "thread-1", "turnId": "turn-1"}])
        self.assert_closed()

    def test_timeout_keeps_saved_job_results_and_closes_owned_children(self):
        (self.job / "candidates.json").write_text("[]", encoding="utf-8")
        def scenario(process, message):
            process.begin(message)
        with self.assertRaises(BridgeError):
            self.run_bridge(self.bridge(scenario, timeout=.15))
        self.assertEqual((self.job / "candidates.json").read_text(), "[]")
        self.assert_closed()

    def test_gap_result_without_image_view_is_allowed_and_does_not_fabricate_candidates(self):
        def gap(process, message):
            process.begin(message)
            process.finish([agent("final", "没有合格画面，保留缺口。", "final_answer")])
        self.assertIn("没有合格", self.run_bridge(self.bridge(gap)))
        self.assertFalse((self.job / "candidates.json").exists())

    def test_read_exposes_an_actual_job_image_to_the_model(self):
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aKC8AAAAASUVORK5CYII=")
        (self.job / "frame.png").write_bytes(image)
        def scenario(process, message):
            process.begin(message)
            process.emit({"id": 101, "method": "item/tool/call", "params": {"threadId": "thread-1", "turnId": "turn-1", "callId": "image",
                         "tool": "Read", "arguments": {"file_path": "frame.png"}}})
            process.finish([agent("final", "已查看图片。", "final_answer")])
        self.run_bridge(self.bridge(scenario))
        response = next(item["result"] for item in self.processes[-1].received if item.get("id") == 101)
        self.assertTrue(response["success"])
        self.assertEqual(response["contentItems"][0]["type"], "inputImage")
        self.assertEqual(base64.b64decode(response["contentItems"][0]["imageUrl"].split(",", 1)[1]), image)

    def test_default_analysis_bridge_implementation_and_model_picker_are_not_overridden(self):
        self.assertIs(CodexStillsBridge.models, CodexBridge.models)
        self.assertIs(CodexStillsBridge.status, CodexBridge.status)
        bridge = self.bridge()
        result = bridge.models()
        self.assertTrue(result["available"])
        self.assertFalse(self.requests("thread/start"))
        self.assertEqual(self.launches[0][0], ["fake-codex.exe", "app-server"])


@unittest.skipUnless(os.name == "nt", "Windows owns MCP children through a Job Object")
class OwnedProcessTreeTests(unittest.TestCase):
    def test_close_kills_owned_child_even_after_parent_exits(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        script = "import sys,subprocess; sys.stdin.readline(); child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(child.pid,flush=True); sys.stdin.readline()"
        process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        guard, child_handle = None, None
        try:
            guard = module._OwnedProcessTree(process)
            process.stdin.write("start\n")
            process.stdin.flush()
            child_pid = int(process.stdout.readline())
            child_handle = kernel.OpenProcess(0x100000, False, child_pid)  # SYNCHRONIZE only
            self.assertTrue(child_handle)
            process.stdin.write("exit\n")
            process.stdin.flush()
            process.wait(timeout=3)
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 0), 258)  # WAIT_TIMEOUT: child survived parent
            guard.close()
            self.assertEqual(kernel.WaitForSingleObject(child_handle, 3000), 0)  # WAIT_OBJECT_0: own child terminated
        finally:
            if guard:
                guard.close()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if child_handle:
                kernel.CloseHandle(child_handle)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


if __name__ == "__main__":
    unittest.main()
