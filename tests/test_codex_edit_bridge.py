"""Codex editing uses simulated App Server sessions; never edits live Resolve."""
import copy
import json
from pathlib import Path
import sys
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import BridgeCancelled, BridgeError, CodexBridge
from codex_edit_bridge import CodexEditBridge, EDIT_TIMEOUT
from codex_stills_bridge import DISABLED_FEATURES
from test_codex_bridge import agent
import test_codex_stills_bridge as support


def complete(process, message):
    process.begin(message)
    process.finish([agent("final", '已保存目标时间线。\n```json\n{"saved":true}\n```', "final_answer")])


class CodexEditBridgeTests(unittest.TestCase):
    requests = support.CodexStillsBridgeTests.requests
    assert_closed = support.CodexStillsBridgeTests.assert_closed

    def setUp(self):
        support.CodexStillsBridgeTests.setUp(self)
        self.ledger = self.folder / "davinci-ledger"
        self.ledger.mkdir()
        self.profile.update(rules="保护配音与意群标记，只修改交接的目标时间线。", timeout=EDIT_TIMEOUT, dirs=[str(self.ledger)])

    def bridge(self, scenario=complete, *, timeout=2, config_transform=None, unknown_model=False):
        def factory(arguments, **options):
            config = copy.deepcopy(self.config)
            for number, argument in enumerate(arguments):
                if argument == "-c":
                    support.merge(config, tomllib.loads(arguments[number + 1]))
            if len(self.processes) == 1 and config_transform:
                config_transform(config)
            process = support.ConfigProcess(scenario, config)
            if unknown_model:
                process.thread_result = {"thread": {"id": "thread-1"}}
            self.processes.append(process)
            self.launches.append((arguments, options))
            return process
        return CodexEditBridge(timeout=timeout, startup_timeout=.5, popen_factory=factory)

    def run_bridge(self, bridge, **values):
        return bridge.run("请按交接内容剪辑并保存目标时间线。", cwd=str(self.job), on_progress=self.progress.append,
                          cancel_event=self.cancel, on_event=self.events.append, profile=self.profile, **values)

    def calls(self, calls):
        def scenario(process, message):
            process.begin(message)
            for identifier, tool, values in calls:
                process.emit({"id": identifier, "method": "item/tool/call", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "callId": str(identifier), "tool": tool, "arguments": values}})
            process.finish([agent("final", "已保存本次任务结果。", "final_answer")])
        return scenario

    def responses(self):
        return {item["id"]: item["result"] for item in self.processes[-1].received
                if isinstance(item.get("id"), int) and item["id"] >= 101 and "result" in item}

    def test_edit_session_preserves_model_effort_and_skill_and_has_its_own_tool_policy(self):
        skill = self.job / "skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("剪辑规则", encoding="utf-8")
        self.assertIn("已保存", self.run_bridge(self.bridge(), model="codex-edit-model", reasoning_effort="xhigh",
                                              skills=[{"name": "editing", "path": str(skill)}]))
        self.assertEqual(len(self.processes), 2)
        self.assertFalse(self.requests("thread/start", self.processes[0]))
        self.assertFalse(self.requests("turn/start", self.processes[0]))
        config = self.processes[-1].config
        own = config["mcp_servers"]["pvr_edit_resolve"]
        self.assertEqual(own["enabled_tools"], ["get_scripting_api", "run_script"])
        self.assertEqual(own["command"], str(self.mcp.resolve()))
        self.assertEqual(own["tool_timeout_sec"], EDIT_TIMEOUT)
        self.assertFalse(config["mcp_servers"]["unrelated_remote"]["enabled"])
        self.assertFalse(config["mcp_servers"]["davinci"]["enabled"])
        self.assertTrue(all(config["features"][name] is False for name in DISABLED_FEATURES))
        self.assertTrue(config["features"]["view_image"])
        self.assertTrue(config["features"]["code_mode_host"])
        self.assertEqual(config["web_search"], "disabled")
        thread, turn = self.requests("thread/start")[0], self.requests("turn/start")[0]
        self.assertEqual(thread["model"], "codex-edit-model")
        self.assertEqual(thread["config"]["model_reasoning_effort"], "xhigh")
        self.assertEqual((turn["model"], turn["effort"]), ("actual-stills-model", "medium"))
        self.assertEqual(turn["input"][1], {"type": "skill", "name": "editing", "path": str(skill)})
        # Codex ignores a task-folder copy as a skill input, so its text must be in the instructions.
        self.assertIn(f'<skill name="editing">\n{skill.read_text(encoding="utf-8")}\n</skill>', thread["developerInstructions"])
        self.assertEqual(turn["sandboxPolicy"]["writableRoots"], [str(self.job), str(self.ledger)])
        self.assertFalse(turn["sandboxPolicy"]["networkAccess"])
        self.assertEqual(thread["config"]["sandbox_workspace_write"]["writable_roots"], [str(self.job), str(self.ledger)])
        self.assertIn("剪辑并保存工程", thread["developerInstructions"])
        self.assertIn("保护配音", thread["developerInstructions"])
        self.assertNotIn("不修改项目", thread["developerInstructions"])
        self.assertEqual({spec["name"] for spec in thread["dynamicTools"]}, {"Read", "Write", "Edit", "Glob", "Grep"})
        self.assertEqual(next(event for event in self.events if event["type"] == "session")["model"], "actual-stills-model")
        self.assertTrue(any("剪辑" in text for text in self.progress))
        self.assert_closed()

    def test_client_default_model_and_effort_are_omitted_and_metadata_is_not_invented(self):
        self.run_bridge(self.bridge(unknown_model=True))
        thread, turn = self.requests("thread/start")[0], self.requests("turn/start")[0]
        self.assertNotIn("model", thread)
        self.assertNotIn("model_reasoning_effort", thread["config"])
        self.assertNotIn("model", turn)
        self.assertNotIn("effort", turn)
        session = next(event for event in self.events if event["type"] == "session")
        self.assertIsNone(session["model"])
        self.assertIsNone(session["reasoning_effort"])

    def test_file_relay_reads_writes_edits_and_searches_job_and_ledger(self):
        project = str(self.ledger / "Lab.json")
        calls = [(101, "Write", {"file_path": "shots.json", "content": "[]"}),
                 (102, "Write", {"file_path": project, "content": '{"shots":1}'}),
                 (103, "Edit", {"file_path": project, "old_string": "1", "new_string": "2"}),
                 (104, "Read", {"file_path": project}),
                 (105, "Glob", {"pattern": "*.json", "path": str(self.ledger)}),
                 (106, "Grep", {"pattern": "shots", "path": str(self.ledger)}),
                 (107, "Glob", {"pattern": "*.json"})]
        self.run_bridge(self.bridge(self.calls(calls)))
        responses = self.responses()
        self.assertEqual(set(responses), set(range(101, 108)))
        self.assertTrue(all(result["success"] for result in responses.values()))
        self.assertEqual(json.loads(Path(project).read_text()), {"shots": 2})
        self.assertIn('"shots":2', responses[104]["contentItems"][0]["text"])
        self.assertEqual(responses[105]["contentItems"][0]["text"], "Lab.json")
        self.assertIn("Lab.json:1", responses[106]["contentItems"][0]["text"])
        self.assertEqual(responses[107]["contentItems"][0]["text"], "shots.json")

    def test_file_relay_rejects_other_directories_and_protected_handoff_and_skill(self):
        (self.job / "handoff.md").write_text("protected", encoding="utf-8")
        (self.job / "skill").mkdir()
        (self.job / "skill" / "SKILL.md").write_text("protected", encoding="utf-8")
        outside = self.folder / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        calls = [(101, "Write", {"file_path": str(outside), "content": "changed"}),
                 (102, "Write", {"file_path": "handoff.md", "content": "changed"}),
                 (103, "Edit", {"file_path": "skill/SKILL.md", "old_string": "protected", "new_string": "changed"}),
                 (104, "Read", {"file_path": str(outside)}),
                 (105, "Glob", {"pattern": "*", "path": str(self.folder)}),
                 (106, "Grep", {"pattern": "private", "path": str(self.folder)}),
                 (107, "Write", {"file_path": str(self.ledger / ".codex" / "config.toml"), "content": "unsafe"})]
        self.run_bridge(self.bridge(self.calls(calls)))
        self.assertEqual(outside.read_text(), "private")
        self.assertEqual((self.job / "handoff.md").read_text(), "protected")
        self.assertEqual((self.job / "skill" / "SKILL.md").read_text(), "protected")
        self.assertFalse((self.ledger / ".codex").exists())
        self.assertTrue(all(result["success"] is False for result in self.responses().values()))

    def test_profile_rejects_unsafe_tools_extra_roots_and_parent_ledger_before_launch(self):
        valid = copy.deepcopy(self.profile)
        invalid = [{**valid, "allowed": [*valid["allowed"], value]} for value in (
            "Bash", "mcp__resolve__run_script_unsafe", "mcp__resolve__launch_resolve", "mcp__unrelated__search")]
        invalid.extend({**valid, "dirs": value} for value in ([], [str(self.folder)], [str(self.ledger), str(self.folder)], ["relative"]))
        invalid.extend({**valid, "timeout": value} for value in (0, True, EDIT_TIMEOUT + 1))
        for profile in invalid:
            with self.subTest(profile=profile):
                self.profile = profile
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge())
        self.assertFalse(self.launches)

    def test_unverified_tool_or_filesystem_configuration_never_starts_model(self):
        for field in ("server", "tools", "plugins", "network", "roots", "approval", "view_image"):
            with self.subTest(field=field):
                self.processes.clear()
                self.guards.clear()
                def transform(config):
                    if field == "server":
                        config["mcp_servers"]["davinci"]["enabled"] = True
                    elif field == "tools":
                        config["mcp_servers"]["pvr_edit_resolve"]["enabled_tools"].append("run_script_unsafe")
                    elif field == "network":
                        config["sandbox_workspace_write"]["network_access"] = True
                    elif field == "roots":
                        config["sandbox_workspace_write"]["writable_roots"].append(str(self.folder))
                    elif field == "approval":
                        config["approval_policy"] = "on-request"
                    else:
                        config["features"][field] = field != "view_image"
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(config_transform=transform))
                self.assertFalse(self.requests("thread/start"))
                self.assertFalse(self.requests("turn/start"))
                self.assert_closed()

    def test_unlisted_dynamic_tool_stops_task_and_closes_sessions(self):
        with self.assertRaisesRegex(BridgeError, "剪辑范围外"):
            self.run_bridge(self.bridge(self.calls([(101, "Bash", {"command": "not executed"})])))
        self.assertFalse(self.responses()[101]["success"])
        self.assert_closed()

    def test_cancellation_preserves_type_and_interrupts_only_owned_turn(self):
        self.cancel.set()
        with self.assertRaisesRegex(BridgeCancelled, "剪辑已停止"):
            self.run_bridge(self.bridge())
        self.assertFalse(self.launches)
        self.cancel.clear()
        def scenario(process, message):
            process.begin(message)
        def progress(value):
            if "已开始" in value:
                self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.bridge(scenario).run("剪辑本次目标时间线", cwd=str(self.job), on_progress=progress,
                                      cancel_event=self.cancel, profile=self.profile)
        self.assertEqual(self.requests("turn/interrupt"), [{"threadId": "thread-1", "turnId": "turn-1"}])
        self.assert_closed()

    def test_native_model_picker_and_status_are_shared_with_normal_codex_bridge(self):
        self.assertIs(CodexEditBridge.models, CodexBridge.models)
        self.assertIs(CodexEditBridge.status, CodexBridge.status)
        self.assertTrue(self.bridge().models()["available"])
        self.assertFalse(self.requests("thread/start"))
        self.assertEqual(self.launches[0][0], ["fake-codex.exe", "app-server"])


if __name__ == "__main__":
    unittest.main()
