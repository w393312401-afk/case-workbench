"""Model, skill and live-response integration using isolated records and fake RPCs."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_server import SERVER
from ai_tasks import AITaskError, AITaskManager, _hash
import ai_tasks


class StreamingBridge:
    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls = []

    def status(self):
        return {"available": True, "label": "隔离测试连接"}

    def models(self):
        return {"available": True, "models": [
            {"id": "test-a", "name": "Test A", "reasoning_efforts": ["low", "high"], "is_default": True},
            {"id": "test-b", "name": "Test B", "reasoning_efforts": ["medium"], "is_default": False}]}

    def run(self, prompt, *, cwd, on_progress, cancel_event, model=None, reasoning_effort=None, skills=None, on_event=None, require_image_view=False):
        self.calls.append({"prompt": prompt, "model": model, "reasoning_effort": reasoning_effort, "skills": skills})
        on_event({"type": "session", "model": model or "resolved-default", "reasoning_effort": reasoning_effort or "medium"})
        on_event({"type": "skills_submitted", "skills": skills or []})
        on_event({"type": "message", "item_id": "note", "phase": "commentary", "text": "正在核对购买理由。", "complete": True})
        on_event({"type": "message", "item_id": "private", "phase": "reasoning", "text": "不可显示的内部推理", "complete": False})
        on_event({"type": "message", "item_id": "answer", "phase": "final_answer", "text": "候选开头", "complete": False})
        self.started.set()
        while not self.gate.wait(0.01):
            if cancel_event.is_set():
                raise RuntimeError("测试已停止")
        on_event({"type": "message", "item_id": "answer", "phase": "final_answer", "text": "候选开头与完整结果。", "complete": True})
        return "候选开头与完整结果。"


class StubSkills:
    def __init__(self, path):
        self.path = str(path)
        self.prepared = []

    def catalog(self):
        return [{"name": name, "label": name, "path": self.path, "available": True}
                for name in ("product-video-script-review",)]

    def prepare(self, action, selection="auto", *, stage=None):
        self.prepared.append((action, stage))
        skills = [] if selection == "none" else [{"name": "product-video-script-review", "label": "审稿技能", "path": self.path, "sha256": "fixture-hash"}]
        return {"selection": selection, "skills": skills, "ready": True, "unavailable": [],
                "references": [{"label": "技能规则", "path": self.path, "sha256": "fixture-ref", "text": "不虚构指标，按原文指出依据。"}] if skills else [],
                "instructions": "按本轮所选技能处理，仅输出候选。"}


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-stream-test-")
        self.root = Path(self.temp.name)
        self.store = SERVER.CaseStore(self.root / "skill")
        self.case = self.store.add({"title": "测试", "product": "测试盒", "original": {"text": "Keep tools together."},
            "feedback": {"kind": "editorial", "text": "隔离测试"},
            "current": {"title": "测试", "category": "待验证", "kind": "editorial", "conclusion": "仅作测试", "assessments": []}})
        self.bridge = StreamingBridge()
        self.manager = AITaskManager(self.store, self.root / "runtime", threading.RLock(), self.bridge)
        self.manager.skills = StubSkills(self.root / "skill" / "SKILL.md")
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.bridge.gate.set()
        self.manager.shutdown()
        self.temp.cleanup()

    def create(self, **settings):
        return self.manager.create(self.case["case_id"], {"request_id": uuid4().hex,
            "revision": _hash(self.store.read_case(self.case["case_id"])), "action": "review", "instruction": "审查当前稿", **settings})

    def wait(self, identifier):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.manager.get(identifier)
            if not task["busy"]:
                return task
            time.sleep(0.01)
        self.fail("测试任务没有结束")

    def test_live_text_actual_model_and_submitted_skills_before_completion(self):
        task = self.create(model="test-a", reasoning_effort="high", skill_selection="auto")
        self.assertTrue(self.bridge.started.wait(2))
        live = self.manager.stream_snapshot(task["task_id"])
        turn = live["task"]["rounds"][-1]
        self.assertEqual(live["task"]["status"], "running")
        self.assertEqual(turn["output"], "")
        self.assertEqual((turn["model"], turn["reasoning_effort"], turn["skills_status"]), ("test-a", "high", "submitted"))
        self.assertEqual(turn["messages"][-1]["text"], "候选开头")
        self.assertFalse(turn["messages"][-1]["complete"])
        self.assertNotIn("不可显示", json.dumps(live, ensure_ascii=False))
        self.assertIn("不虚构指标", self.bridge.calls[-1]["prompt"])
        self.assertEqual(self.bridge.calls[-1]["skills"][0]["name"], "product-video-script-review")
        self.bridge.gate.set()
        done = self.wait(task["task_id"])
        self.assertEqual(done["rounds"][-1]["output"], "候选开头与完整结果。")
        raw = json.loads((self.store.folder(self.case["case_id"]) / "collaboration" / task["task_id"] / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["rounds"][-1]["model"], "test-a")
        self.assertTrue(raw["rounds"][-1]["messages"][-1]["complete"])

    def test_variants_two_calls_keep_requested_settings_and_separate_actual_metadata(self):
        self.bridge.gate.set()
        done = self.wait(self.create(action="variants", model="test-a", reasoning_effort="high")["task_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(self.bridge.calls), 2)
        # The delivering review stage asks for the variant review bundle, not the diagnostic review.
        self.assertEqual(self.manager.skills.prepared[-1], ("variants", "review"))
        self.assertNotIn(("review", None), self.manager.skills.prepared)
        for call in self.bridge.calls:
            self.assertEqual((call["model"], call["reasoning_effort"]), ("test-a", "high"))
            self.assertEqual([skill["name"] for skill in call["skills"]], ["product-video-script-review"])
        turn = done["rounds"][-1]
        self.assertEqual(turn["messages"], [])
        for stage in turn["stages"].values():
            self.assertEqual((stage["model"], stage["reasoning_effort"], stage["skills_status"]), ("test-a", "high", "submitted"))
            self.assertNotIn("messages", stage)
        raw = json.loads((self.store.folder(self.case["case_id"]) / "collaboration" / done["task_id"] / "task.json").read_text(encoding="utf-8"))
        stages = raw["rounds"][-1]["stages"]
        self.assertEqual(stages["draft"]["messages"][-1]["item_id"], stages["review"]["messages"][-1]["item_id"])
        self.assertEqual(stages["review"]["output"], turn["output"])

    def test_default_uses_actual_reported_model_and_cancel_keeps_partial(self):
        task = self.create()
        self.assertTrue(self.bridge.started.wait(2))
        self.manager.cancel(task["task_id"], {"request_id": uuid4().hex, "round_id": task["rounds"][-1]["round_id"]})
        done = self.wait(task["task_id"])
        turn = done["rounds"][-1]
        self.assertEqual(done["status"], "cancelled")
        self.assertIsNone(turn["requested_model"])
        self.assertEqual(turn["model"], "resolved-default")
        self.assertEqual(turn["output"], "")
        self.assertEqual(turn["messages"][-1]["text"], "候选开头")
        self.assertFalse(turn["messages"][-1]["complete"])

    def test_model_changes_only_apply_to_new_round_and_validate_effort(self):
        options = self.manager.options()
        self.assertEqual([entry["id"] for entry in options["models"]], ["test-a", "test-b"])
        self.assertEqual({entry["id"] for entry in options["skills"]}, {"auto", "none", "product-video-script-review"})
        self.bridge.gate.set()
        first = self.wait(self.create(model="test-a", reasoning_effort="high")["task_id"])
        before = copy.deepcopy(first["rounds"][0])
        continued = self.manager.continue_task(first["task_id"], {"request_id": uuid4().hex,
            "task_revision": first["revision"], "instruction": "新一轮", "model": "test-b", "reasoning_effort": "medium", "skill_selection": "none"})
        done = self.wait(continued["task_id"])
        self.assertEqual(done["rounds"][0], before)
        self.assertEqual(done["rounds"][-1]["model"], "test-b")
        self.assertEqual(done["rounds"][-1]["skills_status"], "none")
        self.assertEqual(self.bridge.calls[-1]["skills"], [])
        for settings in ({"model": "missing"}, {"model": "test-b", "reasoning_effort": "high"}, {"reasoning_effort": "high"}, {"skill_selection": []}):
            with self.assertRaises(AITaskError):
                self.create(**settings)
        self.assertEqual(len(self.manager.list_tasks()), 1)

    def test_legacy_round_does_not_fabricate_model_or_skill_use(self):
        self.bridge.gate.set()
        first = self.wait(self.create()["task_id"])
        path = self.store.folder(self.case["case_id"]) / "collaboration" / first["task_id"] / "task.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        for key in ("model", "reasoning_effort", "skills", "skills_status", "messages", "skill_selection", "requested_model", "requested_reasoning_effort", "skill_bundle"):
            raw["rounds"][0].pop(key, None)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        old = self.manager.get(first["task_id"])["rounds"][0]
        self.assertIsNone(old["model"])
        self.assertIsNone(old["skills_status"])

    def test_oversized_live_checkpoint_finishes_failed_and_unblocks_case(self):
        def oversized(prompt, *, on_event, **kwargs):
            on_event({"type": "message", "item_id": "short", "phase": "commentary", "text": "已保存的进展", "complete": True})
            on_event({"type": "message", "item_id": "huge", "phase": "final_answer", "text": "x" * 20000, "complete": True})
            return "不能抵达这里"
        self.bridge.run = oversized
        with patch.object(ai_tasks, "MAX_TASK_BYTES", 10000):
            task = self.wait(self.create()["task_id"])
        self.assertEqual(task["status"], "failed")
        self.assertFalse(task["busy"])
        self.assertFalse(self.manager.active_for_case(self.case["case_id"]))
        self.assertIn("容量", task["rounds"][-1]["error"])
        self.assertEqual(task["rounds"][-1]["messages"][0]["text"], "已保存的进展")
        self.assertFalse(any(message["item_id"] == "huge" for message in task["rounds"][-1]["messages"]))

    def test_continuation_includes_cancelled_partial_reply_as_unfinished_history(self):
        task = self.create()
        self.assertTrue(self.bridge.started.wait(2))
        self.manager.cancel(task["task_id"])
        stopped = self.wait(task["task_id"])
        self.bridge.gate.set()
        self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex,
            "task_revision": stopped["revision"], "instruction": "接着刚才未完成的文字"})
        self.wait(task["task_id"])
        self.assertIn("候选开头", self.bridge.calls[-1]["prompt"])
        self.assertIn('"status": "cancelled"', self.bridge.calls[-1]["prompt"])

    def test_event_stream_delivers_partial_and_final_and_survives_reader_reconnect(self):
        server = SERVER.make_server(self.store.root, self.root / "http-runtime", port=0, allow_open=False, ai_bridge=self.bridge)
        server.app.ai.shutdown()
        server.app.ai = self.manager
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        worker.start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close(), worker.join(2)))
        with patch.object(SERVER.Handler, "log_message", lambda *args: None):
            task = self.create()
            self.assertTrue(self.bridge.started.wait(2))
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", f"/api/ai/tasks/{task['task_id']}/stream")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("text/event-stream", response.getheader("Content-Type"))
            initial = json.loads(response.readline().decode()[6:])["task"]
            self.assertEqual(initial["rounds"][-1]["messages"][-1]["text"], "候选开头")
            response.readline()
            response.close()
            connection.close()
            self.assertTrue(self.manager.get(task["task_id"])["busy"])
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", f"/api/ai/tasks/{task['task_id']}/stream")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.bridge.gate.set()
            latest = initial
            while True:
                line = response.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    latest = json.loads(line[6:])["task"]
            connection.close()
            self.assertEqual(latest["status"], "completed")
            self.assertEqual(latest["rounds"][-1]["output"], "候选开头与完整结果。")


if __name__ == "__main__":
    unittest.main()
