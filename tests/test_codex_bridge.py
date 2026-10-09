"""Fake App Server protocol tests: no model calls, account changes, or real threads."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import CodexBridge, BridgeError, BridgeCancelled, BridgeUnavailable


class FakeReader:
    def __init__(self):
        self.queue = queue.Queue()
        self.closed = False
        self.drained = 0
        self.drained_event = threading.Event()

    def put(self, value):
        self.queue.put(value)

    def readline(self, limit=-1):
        chunk = self.queue.get(timeout=5)
        self.drained += len(chunk)
        if chunk:
            self.drained_event.set()
        return chunk

    read = readline

    def close(self):
        self.closed = True
        self.queue.put(b"")


class FakeWriter:
    def __init__(self, process):
        self.process = process
        self.pending = b""
        self.closed = False

    def write(self, data):
        self.pending += bytes(data)
        while b"\n" in self.pending:
            line, self.pending = self.pending.split(b"\n", 1)
            self.process.receive(json.loads(line))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if not self.process.stubborn:
            self.process.exit(0)


class FakeProcess:
    def __init__(self, scenario, *, stubborn=False):
        self.stdout, self.stderr = FakeReader(), FakeReader()
        self.stdin = FakeWriter(self)
        self.exit_event = threading.Event()
        self.returncode = None
        self.received = []
        self.scenario = scenario
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False
        self.thread_result = {"thread": {"id": "thread-1"}}
        self.model_pages = {None: {"data": [], "nextCursor": None}}
        self.model_list_error = None

    def emit(self, message):
        self.stdout.put((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    def response(self, identifier, result):
        self.emit({"id": identifier, "result": result})

    def notice(self, method, **params):
        self.emit({"method": method, "params": {"threadId": "thread-1", **params}})

    def receive(self, message):
        self.received.append(message)
        method = message.get("method")
        if method == "initialize":
            if self.scenario == "init_error":
                self.emit({"id": message["id"], "error": {"code": 401, "message": "unauthorized secret-token-do-not-display"}})
            else:
                self.response(message["id"], {"userAgent": "fake"})
        elif method == "thread/start":
            self.response(message["id"], self.thread_result)
        elif method == "model/list":
            if self.model_list_error is not None:
                self.emit({"id": message["id"], "error": self.model_list_error})
            else:
                self.response(message["id"], self.model_pages[message.get("params", {}).get("cursor")])
        elif method == "turn/start":
            self.scenario(self, message)

    def begin(self, message):
        self.notice("turn/started", turn={"id": "turn-1", "status": "inProgress", "items": []})
        self.response(message["id"], {"turn": {"id": "turn-1", "status": "inProgress", "items": []}})

    def finish(self, items=(), status="completed", error=None):
        self.notice("turn/completed", turn={"id": "turn-1", "status": status, "items": list(items), "error": error})

    def exit(self, code):
        if self.returncode is None:
            self.returncode = code
            self.exit_event.set()
            self.stdout.put(b"")
            self.stderr.put(b"")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.exit_event.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.exit(-1)

    def kill(self):
        self.killed = True
        self.exit(-9)


def agent(identifier, text, phase=None):
    return {"type": "agentMessage", "id": identifier, "text": text, "phase": phase}


def success(process, message):
    process.begin(message)
    process.notice("item/completed", turnId="turn-1", item=agent("commentary", "Do not store this progress", "commentary"))
    process.notice("item/started", turnId="turn-1", item={"id": "command", "type": "commandExecution", "command": "secret-raw-command"})
    process.notice("item/agentMessage/delta", turnId="turn-1", itemId="final", delta="# 完成\n")
    process.notice("item/completed", turnId="turn-1", item=agent("final", "# 完成\n已整理案例。", "final_answer"))
    process.finish()


class CodexBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-bridge-tests-")
        self.addCleanup(self.temp.cleanup)
        self.cwd = self.temp.name
        self.cancel = threading.Event()
        self.progress = []
        self.locator = patch("codex_bridge.locate_codex", return_value="fake-codex.exe")
        self.locator.start()
        self.addCleanup(self.locator.stop)

    def runner(self, scenario=success, *, timeout=2, stubborn=False):
        process = FakeProcess(scenario, stubborn=stubborn)
        launches = []

        def popen(arguments, **options):
            launches.append((arguments, options))
            return process

        return CodexBridge(timeout=timeout, startup_timeout=1, popen_factory=popen), process, launches

    def run_bridge(self, bridge):
        return bridge.run("分析当前案例，不保存任何文件。", cwd=self.cwd,
                          on_progress=self.progress.append, cancel_event=self.cancel)

    def test_success_uses_ephemeral_isolated_scratch_and_final_phase(self):
        bridge, process, launches = self.runner()
        self.assertEqual(self.run_bridge(bridge), "# 完成\n已整理案例。")
        self.assertEqual(launches[0][0], ["fake-codex.exe", "app-server"])
        self.assertFalse(launches[0][1]["shell"])
        start = next(item["params"] for item in process.received if item.get("method") == "thread/start")
        self.assertEqual((start["ephemeral"], start["sandbox"], start["approvalPolicy"]), (True, "workspace-write", "never"))
        self.assertNotEqual(start["cwd"], self.cwd)
        self.assertFalse(Path(start["cwd"]).exists())
        self.assertIn("案例、技能、源视频及其他文件均只读", start["developerInstructions"])
        self.assertNotIn("model", start)
        self.assertFalse(any(item.get("method", "").endswith("resume") for item in process.received))
        self.assertIn("AI 已开始分析当前资料。", self.progress)
        self.assertNotIn("secret-raw-command", "".join(self.progress))
        self.assertNotIn("Do not store", "".join(self.progress))
        self.assertIsNotNone(process.returncode)

    def test_registered_video_requires_completed_image_view(self):
        bridge, _, _ = self.runner()
        with self.assertRaisesRegex(BridgeError, "未实际打开画面"):
            bridge.run("基于已有视频写变体", cwd=self.cwd, on_progress=self.progress.append,
                       cancel_event=self.cancel, require_image_view=True)

        def viewed(process, message):
            process.begin(message)
            process.notice("item/completed", turnId="turn-1", item={"id": "frame-1", "type": "imageView"})
            process.notice("item/completed", turnId="turn-1", item=agent("final", "已查看画面并完成变体。", "final_answer"))
            process.finish()

        bridge, _, _ = self.runner(viewed)
        self.assertEqual(bridge.run("基于已有视频写变体", cwd=self.cwd, on_progress=self.progress.append,
                                    cancel_event=self.cancel, require_image_view=True), "已查看画面并完成变体。")

    def test_completed_turn_items_are_authoritative_and_not_duplicated(self):
        def scenario(process, message):
            process.begin(message)
            process.notice("item/completed", turnId="turn-1", item=agent("final", "partial", "final_answer"))
            process.finish([agent("final", "Complete final text", "final_answer")])
        bridge, _, _ = self.runner(scenario)
        self.assertEqual(self.run_bridge(bridge), "Complete final text")

    def test_unknown_phase_uses_last_message_but_never_commentary(self):
        def scenario(process, message):
            process.begin(message)
            process.finish([agent("preamble", "older unknown"), agent("final", "legacy answer"),
                            agent("commentary", "wrong", "commentary")])
        bridge, _, _ = self.runner(scenario)
        self.assertEqual(self.run_bridge(bridge), "legacy answer")

    def test_commentary_alone_is_not_a_completed_answer(self):
        def scenario(process, message):
            process.begin(message)
            process.finish([agent("commentary", "still working", "commentary")])
        bridge, _, _ = self.runner(scenario)
        with self.assertRaisesRegex(BridgeError, "没有返回最终回答"):
            self.run_bridge(bridge)

    def test_uncompleted_final_delta_is_not_reported_as_a_complete_answer(self):
        def scenario(process, message):
            process.begin(message)
            process.notice("item/started", turnId="turn-1", item=agent("final", "", "final_answer"))
            process.notice("item/agentMessage/delta", turnId="turn-1", itemId="final", delta="incomplete answer")
            process.finish()
        bridge, _, _ = self.runner(scenario)
        with self.assertRaisesRegex(BridgeError, "没有返回最终回答"):
            self.run_bridge(bridge)

    def test_rpc_error_is_friendly_and_does_not_report_processing(self):
        bridge, process, _ = self.runner("init_error")
        with self.assertRaisesRegex(BridgeError, "登录") as caught:
            self.run_bridge(bridge)
        self.assertNotIn("secret-token", str(caught.exception))
        self.assertFalse(any("分析当前" in message for message in self.progress))
        self.assertFalse(any(item.get("method") == "thread/start" for item in process.received))

    def test_server_approvals_are_rejected_and_unsupported_requests_fail(self):
        expectations = [
            ("item/commandExecution/requestApproval", {"decision": "cancel"}),
            ("item/fileChange/requestApproval", {"decision": "cancel"}),
            ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
            ("execCommandApproval", {"decision": "abort"}),
            ("applyPatchApproval", {"decision": "abort"}),
            ("item/tool/call", None),
        ]
        for method, expected in expectations:
            with self.subTest(method=method):
                def scenario(process, message):
                    process.begin(message)
                    process.emit({"id": "server-approval", "method": method, "params": {"command": "secret-command"}})
                bridge, process, _ = self.runner(scenario)
                with self.assertRaises(BridgeError) as caught:
                    self.run_bridge(bridge)
                reply = next(item for item in process.received if item.get("id") == "server-approval")
                if expected is None:
                    self.assertEqual(reply["error"]["code"], -32601)
                else:
                    self.assertEqual(reply["result"], expected)
                self.assertNotIn("secret-command", str(caught.exception))
                self.assertTrue(any(item.get("method") == "turn/interrupt" for item in process.received))

    def test_cancel_before_launch_and_during_turn(self):
        bridge, process, launches = self.runner()
        self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.run_bridge(bridge)
        self.assertEqual(launches, [])
        self.cancel.clear()
        bridge, process, _ = self.runner(lambda proc, msg: proc.begin(msg))
        def progress(message):
            self.progress.append(message)
            if message == "AI 已开始分析当前资料。":
                self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            bridge.run("test", cwd=self.cwd, on_progress=progress, cancel_event=self.cancel)
        self.assertTrue(any(item.get("method") == "turn/interrupt" for item in process.received))
        self.assertIsNotNone(process.returncode)

    def test_timeout_interrupts_and_terminates_its_stubborn_process(self):
        bridge, process, _ = self.runner(lambda proc, msg: proc.begin(msg), timeout=0.05, stubborn=True)
        with self.assertRaisesRegex(BridgeError, "超时"):
            self.run_bridge(bridge)
        self.assertTrue(process.terminated)
        self.assertTrue(any(item.get("method") == "turn/interrupt" for item in process.received))

    def test_abnormal_exit_and_invalid_protocol_fail_clearly(self):
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                def scenario(process, message):
                    process.begin(message)
                    if invalid:
                        process.stdout.put(b"not-json secret-debug-output\n")
                    else:
                        process.exit(2)
                bridge, _, _ = self.runner(scenario)
                with self.assertRaises(BridgeError) as caught:
                    self.run_bridge(bridge)
                self.assertNotIn("secret-debug", str(caught.exception))

    def test_stderr_is_drained_concurrently_and_not_exposed(self):
        def scenario(process, message):
            process.stderr.put(b"secret-diagnostic" * 100000)
            self.assertTrue(process.stderr.drained_event.wait(1))
            success(process, message)
        bridge, process, _ = self.runner(scenario)
        self.assertEqual(self.run_bridge(bridge), "# 完成\n已整理案例。")
        self.assertGreater(process.stderr.drained, 100000)
        self.assertNotIn("secret-diagnostic", "".join(self.progress))

    def test_failed_turn_and_retry_notifications_do_not_leak_raw_error(self):
        def scenario(process, message):
            process.begin(message)
            process.notice("error", turnId="turn-1", willRetry=True,
                           error={"message": "secret network details"})
            process.finish(status="failed", error={"codexErrorInfo": "usageLimitExceeded", "message": "secret account details"})
        bridge, _, _ = self.runner(scenario)
        with self.assertRaisesRegex(BridgeError, "额度") as caught:
            self.run_bridge(bridge)
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("连接短暂中断，Codex 正在重试。", self.progress)

    def test_unavailable_status_and_missing_executable_do_not_spawn(self):
        bridge, _, launches = self.runner()
        with patch("codex_bridge.locate_codex", return_value=None):
            self.assertFalse(bridge.status()["available"])
            with self.assertRaises(BridgeUnavailable):
                self.run_bridge(bridge)
        self.assertEqual(launches, [])

    def test_model_list_paginates_native_catalog_caches_and_never_starts_a_turn(self):
        bridge, process, launches = self.runner()
        def model(identifier, name, *, hidden=False, default=False):
            return {"id": "catalog-" + identifier, "model": identifier, "displayName": name,
                    "description": "Native server description", "hidden": hidden, "isDefault": default,
                    "defaultReasoningEffort": "server-custom-effort",
                    "supportedReasoningEfforts": [{"reasoningEffort": "light", "description": "Light"},
                                                  {"reasoningEffort": "server-custom-effort", "description": "Custom"}]}
        process.model_pages = {
            None: {"data": [model("native-alpha", "Alpha", default=True), model("hidden-model", "Hidden", hidden=True)], "nextCursor": "page-two"},
            "page-two": {"data": [model("native-beta", "Beta")], "nextCursor": None},
        }
        result = bridge.models()
        self.assertTrue(result["available"])
        self.assertEqual([item["id"] for item in result["models"]], ["native-alpha", "native-beta"])
        self.assertTrue(result["models"][0]["is_default"])
        self.assertEqual(result["models"][0]["default_reasoning_effort"], "server-custom-effort")
        self.assertEqual(result["models"][0]["reasoning_efforts"], ["light", "server-custom-effort"])
        result["models"][0]["name"] = "must not corrupt cache"
        self.assertEqual(bridge.models()["models"][0]["name"], "Alpha")
        self.assertEqual(len(launches), 1)
        requests = [item for item in process.received if item.get("method") == "model/list"]
        self.assertEqual(len(requests), 2)
        self.assertFalse(requests[0]["params"]["includeHidden"])
        self.assertEqual(requests[1]["params"]["cursor"], "page-two")
        self.assertFalse(any(item.get("method") in {"thread/start", "turn/start"} for item in process.received))

    def test_model_list_failure_returns_safe_unavailability_without_fallback_models(self):
        bridge, process, _ = self.runner()
        process.model_list_error = {"code": 401, "message": "unauthorized secret-login-material"}
        result = bridge.models()
        self.assertFalse(result["available"])
        self.assertEqual(result["models"], [])
        self.assertIn("登录", result["error"])
        self.assertNotIn("secret-login", result["error"])
        self.assertIsNotNone(process.returncode)

    def test_model_list_repeated_cursor_does_not_loop_or_return_partial_picker(self):
        bridge, process, _ = self.runner()
        process.model_pages = {None: {"data": [], "nextCursor": "same"},
                               "same": {"data": [], "nextCursor": "same"}}
        result = bridge.models()
        self.assertFalse(result["available"])
        self.assertIn("分页", result["error"])
        self.assertEqual(len([item for item in process.received if item.get("method") == "model/list"]), 2)

    def test_model_and_effort_are_session_scoped_and_report_actual_server_values(self):
        bridge, process, _ = self.runner()
        process.thread_result.update(model="server-resolved-model", reasoningEffort="server-custom-effort")
        events = []
        self.assertEqual(bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
            model="requested-alias", reasoning_effort="server-custom-effort", on_event=events.append), "# 完成\n已整理案例。")
        start = next(item["params"] for item in process.received if item.get("method") == "thread/start")
        turn = next(item["params"] for item in process.received if item.get("method") == "turn/start")
        self.assertEqual(start["model"], "requested-alias")
        self.assertEqual(start["config"], {"model_reasoning_effort": "server-custom-effort"})
        self.assertEqual(turn["model"], "server-resolved-model")
        self.assertEqual(turn["effort"], "server-custom-effort")
        self.assertEqual(events[0], {"type": "session", "model": "server-resolved-model", "reasoning_effort": "server-custom-effort", "skills": []})
        self.assertFalse(any(item.get("method", "").startswith("config/") for item in process.received))

    def test_missing_resolved_model_is_reported_unknown_not_requested_value(self):
        bridge, _, _ = self.runner()
        events = []
        bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
                   model="requested-model", reasoning_effort="requested-effort", on_event=events.append)
        self.assertIsNone(events[0]["model"])
        self.assertIsNone(events[0]["reasoning_effort"])

    def test_normalized_reasoning_effort_matches_reported_session_value(self):
        bridge, process, _ = self.runner()
        process.thread_result.update(model="resolved", reasoningEffort="normalized-effort")
        events = []
        bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
                   reasoning_effort="requested-effort", on_event=events.append)
        turn = next(item["params"] for item in process.received if item.get("method") == "turn/start")
        self.assertEqual(turn["effort"], "normalized-effort")
        self.assertEqual(events[0]["reasoning_effort"], turn["effort"])

    def test_skill_is_native_input_with_mention_and_submission_event_after_acceptance(self):
        bridge, process, _ = self.runner()
        skill = Path(self.cwd) / "example-skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("# Test skill\nRead-only analysis.", encoding="utf-8")
        selected = [{"name": "example-skill", "path": str(skill)}]
        events = []
        bridge.run("按此技能审查", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
                   skills=selected, on_event=events.append)
        turn = next(item["params"] for item in process.received if item.get("method") == "turn/start")
        self.assertTrue(turn["input"][0]["text"].startswith("$example-skill\n\n"))
        self.assertEqual(turn["input"][1], {"type": "skill", **selected[0]})
        self.assertNotIn("# Test skill", turn["input"][0]["text"])
        submitted = next(item for item in events if item["type"] == "skills_submitted")
        self.assertEqual(submitted["skills"], selected)

    def test_missing_skill_or_rejected_native_skill_never_silently_downgrades(self):
        bridge, _, launches = self.runner()
        with self.assertRaisesRegex(BridgeError, "技能文件不存在"):
            bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
                       skills=[{"name": "missing", "path": str(Path(self.cwd) / "SKILL.md")}])
        self.assertEqual(launches, [])
        skill = Path(self.cwd) / "SKILL.md"
        skill.write_text("# test", encoding="utf-8")
        def reject(process, message):
            process.emit({"id": message["id"], "error": {"code": -32602, "message": "native skill input rejected"}})
        bridge, process, _ = self.runner(reject)
        events = []
        with self.assertRaises(BridgeError):
            bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel,
                       skills=[{"name": "test", "path": str(skill)}], on_event=events.append)
        self.assertFalse(any(event["type"] == "skills_submitted" for event in events))
        self.assertEqual(len([item for item in process.received if item.get("method") == "turn/start"]), 1)

    def test_visible_message_stream_is_cumulative_with_phase_and_no_private_items(self):
        def scenario(process, message):
            process.begin(message)
            process.notice("item/started", turnId="turn-1", item=agent("commentary", "", "commentary"))
            process.notice("item/agentMessage/delta", turnId="turn-1", itemId="commentary", delta="正在")
            process.notice("item/agentMessage/delta", turnId="turn-1", itemId="commentary", delta="分析")
            process.notice("item/completed", turnId="turn-1", item=agent("commentary", "正在分析。", "commentary"))
            process.notice("item/reasoning/summaryTextDelta", turnId="turn-1", itemId="private", delta="private reasoning")
            process.notice("item/started", turnId="turn-1", item={"id": "command", "type": "commandExecution", "command": "private command"})
            process.notice("item/agentMessage/delta", turnId="turn-1", itemId="final", delta="最终")
            process.notice("item/agentMessage/delta", turnId="turn-1", itemId="final", delta="正文")
            process.notice("item/completed", turnId="turn-1", item=agent("final", "最终正文。", "final_answer"))
            process.finish([agent("final", "最终正文。", "final_answer")])
        bridge, _, _ = self.runner(scenario)
        events = []
        result = bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel, on_event=events.append)
        self.assertEqual(result, "最终正文。")
        visible = [item for item in events if item["type"] == "message"]
        commentary = [item for item in visible if item["item_id"] == "commentary"]
        self.assertEqual([item["text"] for item in commentary], ["", "正在", "正在分析", "正在分析。"])
        self.assertEqual({item["phase"] for item in commentary}, {"commentary"})
        streamed_final = [item for item in visible if item["item_id"] == "final"]
        self.assertEqual([item["phase"] for item in streamed_final], ["unknown", "unknown", "final_answer"])
        self.assertEqual([item["complete"] for item in streamed_final], [False, False, True])
        self.assertNotIn("private", json.dumps(events))

    def test_stream_consumer_failure_stops_and_interrupts_process(self):
        bridge, process, _ = self.runner()
        def fail(event):
            if event["type"] == "message":
                raise RuntimeError("simulated conversation storage full")
        with self.assertRaisesRegex(RuntimeError, "storage full"):
            bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel, on_event=fail)
        self.assertTrue(any(item.get("method") == "turn/interrupt" for item in process.received))
        self.assertIsNotNone(process.returncode)

    def test_server_model_reroute_updates_reported_model_without_invented_effort(self):
        def scenario(process, message):
            process.begin(message)
            process.notice("model/rerouted", turnId="turn-1", fromModel="resolved-first", toModel="resolved-next", reason="test")
            process.finish([agent("final", "done", "final_answer")])
        bridge, process, _ = self.runner(scenario)
        process.thread_result.update(model="resolved-first", reasoningEffort="custom")
        events = []
        bridge.run("test", cwd=self.cwd, on_progress=self.progress.append, cancel_event=self.cancel, on_event=events.append)
        sessions = [item for item in events if item["type"] == "session"]
        self.assertEqual([item["model"] for item in sessions], ["resolved-first", "resolved-next"])
        self.assertIsNone(sessions[-1]["reasoning_effort"])


if __name__ == "__main__":
    unittest.main()
