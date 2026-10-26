"""Read-only planner isolation with simulated App Server sessions, no model use."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import tomllib
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_bridge import BridgeCancelled, BridgeError
from codex_edit_bridge import CodexEditPlannerBridge, PLANNER_TIMEOUT
from codex_stills_bridge import DISABLED_FEATURES
from test_codex_bridge import agent
import test_codex_stills_bridge as support


def complete(process, message):
    process.begin(message)
    process.finish([agent("final", "推荐候选 A，开头使用刀刃特写。", "final_answer")])


class CodexEditPlannerBridgeTests(unittest.TestCase):
    requests = support.CodexStillsBridgeTests.requests
    assert_closed = support.CodexStillsBridgeTests.assert_closed

    def setUp(self):
        support.CodexStillsBridgeTests.setUp(self)
        self.profile = {"rules": "检查候选与意群的对应关系。", "timeout": 120}

    def bridge(self, scenario=complete, *, config_transform=None, timeout=2):
        def factory(arguments, **options):
            config = copy.deepcopy(self.config)
            for number, argument in enumerate(arguments):
                if argument == "-c":
                    support.merge(config, tomllib.loads(arguments[number + 1]))
            if len(self.processes) == 1 and config_transform:
                config_transform(config)
            process = support.ConfigProcess(scenario, config)
            self.processes.append(process)
            self.launches.append((arguments, options))
            return process
        return CodexEditPlannerBridge(timeout=timeout, startup_timeout=.5, popen_factory=factory)

    def run_bridge(self, bridge, **values):
        return bridge.run("意群 1：候选 A 是刀刃特写，候选 B 是产品展示。分析镜头对应关系。", cwd=str(self.job),
                          on_progress=self.progress.append, cancel_event=self.cancel, on_event=self.events.append,
                          profile=self.profile, **values)

    def calls(self, calls):
        def scenario(process, message):
            process.begin(message)
            for identifier, tool, arguments in calls:
                process.emit({"id": identifier, "method": "item/tool/call", "params": {
                    "threadId": "thread-1", "turnId": "turn-1", "callId": str(identifier),
                    "tool": tool, "arguments": arguments}})
            process.finish([agent("final", "候选分析完成。", "final_answer")])
        return scenario

    def responses(self):
        return {item["id"]: item["result"] for item in self.processes[-1].received
                if isinstance(item.get("id"), int) and item["id"] >= 101 and "result" in item}

    def test_planner_disables_all_mcp_and_write_access_and_keeps_selected_model(self):
        result = self.run_bridge(self.bridge(), model="selected-model", reasoning_effort="high")
        self.assertIn("推荐候选 A", result)
        self.assertEqual(len(self.processes), 2)
        self.assertFalse(self.requests("thread/start", self.processes[0]))
        self.assertFalse(self.requests("turn/start", self.processes[0]))
        config = self.processes[-1].config
        self.assertEqual(set(config["mcp_servers"]), {"davinci", "unrelated_remote"})
        self.assertTrue(all(server["enabled"] is False for server in config["mcp_servers"].values()))
        self.assertEqual(config["sandbox_mode"], "read-only")
        self.assertEqual(config["sandbox_workspace_write"]["writable_roots"], [])
        self.assertFalse(config["sandbox_workspace_write"]["network_access"])
        self.assertEqual(config["web_search"], "disabled")
        self.assertTrue(all(config["features"][name] is False for name in DISABLED_FEATURES))
        self.assertTrue(config["features"]["view_image"])
        thread, turn = self.requests("thread/start")[0], self.requests("turn/start")[0]
        self.assertEqual((thread["sandbox"], thread["approvalPolicy"], thread["ephemeral"]), ("read-only", "never", True))
        self.assertEqual(thread["model"], "selected-model")
        self.assertEqual(thread["config"]["model_reasoning_effort"], "high")
        self.assertEqual({spec["name"] for spec in thread["dynamicTools"]}, {"Read", "Glob", "Grep"})
        self.assertEqual(turn["sandboxPolicy"], {"type": "readOnly", "networkAccess": False})
        self.assertEqual((turn["model"], turn["effort"]), ("actual-stills-model", "medium"))
        self.assertIn("不连接或操作达芬奇", thread["developerInstructions"])
        self.assertIn("不保存分析结果文件", thread["developerInstructions"])
        self.assertEqual(list(self.job.iterdir()), [])
        self.assert_closed()

    def test_default_profile_works_without_mcp_or_model_override(self):
        self.profile = None
        self.config.pop("mcp_servers")
        self.run_bridge(self.bridge())
        thread = self.requests("thread/start")[0]
        self.assertNotIn("model", thread)
        self.assertNotIn("model_reasoning_effort", thread["config"])
        self.assertEqual(self.processes[-1].config["mcp_servers"], {})
        self.assert_closed()

    def test_only_job_files_can_be_read_and_searched(self):
        (self.job / "candidates.json").write_text('{"shot":"candidate A"}', encoding="utf-8")
        outside = self.folder / "outside.txt"
        outside.write_text("private", encoding="utf-8")
        self.run_bridge(self.bridge(self.calls([
            (101, "Read", {"file_path": "candidates.json"}),
            (102, "Glob", {"pattern": "*.json"}),
            (103, "Grep", {"pattern": "candidate", "glob": "*.json"}),
            (104, "Read", {"file_path": str(outside)}),
            (105, "Glob", {"pattern": "*", "path": str(self.folder)}),
        ])))
        responses = self.responses()
        self.assertTrue(all(responses[i]["success"] for i in (101, 102, 103)))
        self.assertTrue(all(responses[i]["success"] is False for i in (104, 105)))
        self.assertIn("candidate A", responses[101]["contentItems"][0]["text"])
        self.assertIn("candidates.json", responses[102]["contentItems"][0]["text"])
        self.assertIn("candidates.json:1", responses[103]["contentItems"][0]["text"])
        self.assertEqual(outside.read_text(), "private")

    def test_write_edit_and_resolve_tool_calls_are_rejected_without_writes(self):
        source = self.job / "candidate.txt"
        source.write_text("protected", encoding="utf-8")
        for tool, values in (
            ("Write", {"file_path": str(source), "content": "changed"}),
            ("Edit", {"file_path": str(source), "old_string": "protected", "new_string": "changed"}),
            ("mcp__resolve__run_script", {"script": "not executed"}),
        ):
            with self.subTest(tool=tool):
                self.processes.clear()
                self.guards.clear()
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(self.calls([(101, tool, values)])))
                self.assertFalse(self.responses()[101]["success"])
                self.assertEqual(source.read_text(), "protected")
                self.assert_closed()

    def test_unsafe_profiles_are_rejected_before_launch(self):
        for profile in (
            {"mcp": {"resolve": {"command": str(self.mcp)}}},
            {"allowed": ["Read", "Write"]}, {"allowed": ["Read", "Edit"]},
            {"allowed": ["mcp__resolve__run_script"]}, {"dirs": [str(self.job)]},
            {"timeout": 0}, {"timeout": True}, {"timeout": PLANNER_TIMEOUT + 1},
            {"rules": []}, {"denied": "Read"}, {"allowed": [None]},
        ):
            with self.subTest(profile=profile):
                self.profile = profile
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge())
        self.assertFalse(self.launches)

    def test_effective_configuration_must_retain_isolation_before_start(self):
        for field in ("server", "new_server", "sandbox", "roots", "network", "approval", "plugins", "multi_agent"):
            with self.subTest(field=field):
                self.processes.clear()
                self.guards.clear()
                def transform(config):
                    if field == "server":
                        config["mcp_servers"]["davinci"]["enabled"] = True
                    elif field == "new_server":
                        config["mcp_servers"]["unexpected"] = {"enabled": True}
                    elif field == "sandbox":
                        config["sandbox_mode"] = "workspace-write"
                    elif field == "roots":
                        config["sandbox_workspace_write"]["writable_roots"] = [str(self.job)]
                    elif field == "network":
                        config["sandbox_workspace_write"]["network_access"] = True
                    elif field == "approval":
                        config["approval_policy"] = "on-request"
                    else:
                        config["features"][field] = True
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(config_transform=transform))
                self.assertFalse(self.requests("thread/start"))
                self.assertFalse(self.requests("turn/start"))
                self.assert_closed()

    def test_cancel_and_timeout_interrupt_and_close_only_owned_session(self):
        self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.run_bridge(self.bridge())
        self.assertFalse(self.launches)
        self.cancel.clear()
        def pending(process, message):
            process.begin(message)
        def progress(value):
            if "已开始" in value:
                self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.bridge(pending).run("只读选材", cwd=str(self.job), on_progress=progress,
                                     cancel_event=self.cancel, profile=self.profile)
        self.assertEqual(self.requests("turn/interrupt"), [{"threadId": "thread-1", "turnId": "turn-1"}])
        self.assert_closed()
        self.cancel.clear()
        with self.assertRaises(BridgeError):
            self.run_bridge(self.bridge(pending, timeout=.15))
        self.assert_closed()

    def test_parallel_calls_keep_independent_sessions_results_and_roots(self):
        both_started = threading.Barrier(2)
        def scenario(process, message):
            process.begin(message)
            both_started.wait(timeout=2)
            process.finish([agent("final", message["params"]["input"][0]["text"], "final_answer")])
        bridge = self.bridge(scenario, timeout=4)
        second_job = self.folder / "second-job"
        second_job.mkdir()
        def run(directory, prompt):
            return bridge.run(prompt, cwd=str(directory), on_progress=lambda _: None,
                              cancel_event=threading.Event(), profile=self.profile)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run, self.job, "第一份只读选材")
            second = executor.submit(run, second_job, "第二份只读选材")
            self.assertEqual(first.result(timeout=5), "第一份只读选材")
            self.assertEqual(second.result(timeout=5), "第二份只读选材")
        threads = [request for process in self.processes for request in self.requests("thread/start", process)]
        self.assertEqual({thread["cwd"] for thread in threads}, {str(self.job), str(second_job)})
        self.assertEqual(len(self.processes), 4)
        self.assertTrue(all(thread["sandbox"] == "read-only" for thread in threads))
        self.assert_closed()


if __name__ == "__main__":
    unittest.main()
