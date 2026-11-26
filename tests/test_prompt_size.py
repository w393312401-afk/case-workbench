"""The creation context is printed once in the prompt, so a long direction cannot push a task past the length limit."""
from pathlib import Path
import sys
import types
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))
import ai_tasks as A


def prompt_for(context):
    manager = types.SimpleNamespace(_experience_rule=A.AITaskManager._experience_rule, _review_block=A.AITaskManager._review_block,
                                    store=types.SimpleNamespace(root="."))
    task = {"task_id": "AIT-x", "case_id": "PVR-x", "base_revision": "r1", "base_event_id": None, "action": "variants",
            "snapshot": {"product": "202H", "resources": [], "current": {}, "creation_context": context},
            "rounds": [{"round_id": "AIR-1", "instruction": "写稿", "skill_bundle": {}}]}
    return A.AITaskManager._prompt(manager, task, None)


class PromptSizeTests(unittest.TestCase):
    def test_a_long_context_is_not_counted_twice(self):
        marker = "唯一的方向备注" * 400
        prompt = prompt_for({"direction": {"notes": marker}})
        self.assertEqual(prompt.count(marker), 1)
        self.assertIn("untrusted_creation_context", prompt)


class RepeatedPartsTests(unittest.TestCase):
    """A draft and its review must open with the same text, or the review pays for the task material a second time."""

    def setUp(self):
        self.manager = types.SimpleNamespace(_experience_rule=A.AITaskManager._experience_rule,
                                             _review_block=A.AITaskManager._review_block, store=types.SimpleNamespace(root="."))
        references = [{"label": "写作参考", "text": "参考正文"}]
        self.review_bundle = {"instructions": "复核说明", "references": references}
        self.task = {"task_id": "AIT-x", "case_id": "PVR-x", "base_revision": "r1", "base_event_id": None, "action": "variants",
                     "snapshot": {"product": "202H", "resources": [], "current": {}, "creation_context": {"direction": {"notes": "方向备注"}}},
                     "rounds": [{"round_id": "AIR-1", "instruction": "写两条", "review_context": {"status": "attached", "text": "评论摘要正文"},
                                 "skill_bundle": {"instructions": "起稿说明", "references": references}}]}

    def prompt(self, **options):
        return A.AITaskManager._prompt(self.manager, self.task, None, **options)

    def test_draft_and_review_open_with_the_same_text(self):
        draft, review = self.prompt(), self.prompt(review_bundle=self.review_bundle, draft="初稿正文")
        fixed, shared = draft.parts
        self.assertEqual(review.parts, draft.parts)
        self.assertEqual(review[:shared], draft[:shared])
        self.assertIn("参考正文", draft[:fixed])
        for material in ("untrusted_creation_context", "评论摘要正文", "AIT-x"):
            self.assertIn(material, draft[fixed:shared])
        for own in ("起稿说明", "当前用户要求：\n写两条", "<conversation_history_complete>"):
            self.assertIn(own, draft[shared:])
            self.assertNotIn(own, draft[:shared])
        self.assertIn("复核说明", review[shared:])
        self.assertIn("初稿正文", review[shared:])
        self.assertNotIn("初稿正文", review[:shared])

    def test_a_task_without_skill_references_has_no_fixed_part(self):
        self.task["rounds"][-1]["skill_bundle"] = {}
        prompt = self.prompt()
        self.assertEqual(prompt.parts[0], 0)
        self.assertTrue(prompt.startswith("你正在处理本地案例工作台"))

    def test_history_carries_one_copy_of_a_saved_draft(self):
        saved = "上一轮保存的初稿" * 50
        self.task["rounds"].insert(0, {
            "round_id": "AIR-0", "instruction": "写两条", "status": "failed", "output": "", "error": "复核失败", "messages": [],
            "stages": {"draft": {"status": "completed", "output": saved, "error": None,
                                 "messages": [{"item_id": "m1", "phase": "final_answer", "text": saved, "complete": True},
                                              {"item_id": "m0", "phase": "commentary", "text": "先看原稿", "complete": True}]},
                       "review": {"status": "failed", "output": "", "error": "复核失败", "messages": []}}})
        self.assertEqual(self.prompt().count(saved), 1)
        review = self.prompt(review_bundle=self.review_bundle, draft=saved)
        self.assertEqual(review.count(saved), 1)
        self.assertIn("先看原稿", review)
        self.assertLess(review.index("即文末待复核的初稿"), review.index("<untrusted_draft_for_independent_review>"))

    def test_a_finished_round_keeps_its_output_once(self):
        answer = "上一轮完成稿" * 50
        self.task["rounds"].insert(0, {"round_id": "AIR-0", "instruction": "写两条", "status": "completed", "output": answer, "error": None,
                                       "messages": [{"item_id": "m1", "phase": "final_answer", "text": answer, "complete": True}]})
        prompt = self.prompt()
        self.assertEqual(prompt.count(answer), 1)
        self.assertIn("与本轮 output 相同", prompt)


if __name__ == "__main__":
    unittest.main()


import json
from uuid import uuid4
import test_ai_tasks as base
import test_manual_edit as edit


class ResumeTests(unittest.TestCase):
    setUp, tearDown = base.TaskTests.setUp, base.TaskTests.tearDown
    new_manager, body, wait, path = base.TaskTests.new_manager, base.TaskTests.body, base.TaskTests.wait, base.TaskTests.path

    def test_retrying_a_round_whose_review_failed_keeps_the_saved_draft(self):
        task = self.wait(self.manager.create(self.identifier, self.body("variants", "写两条。"))["task_id"], self.manager)
        path = self.path(task["task_id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        draft = {"status": "completed", "output": edit.CANDIDATE, "error": None, "messages": []}
        raw.update(status="failed")
        raw["rounds"][-1].update(status="failed", output="", skill_selection="none", error="复核失败", stages={"draft": draft, "review": {"status": "failed", "output": "", "messages": []}})
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.manager.clear_cache()
        before = len(self.bridges[0].calls)
        self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": self.manager.get(task["task_id"])["revision"]})
        done = self.wait(task["task_id"], self.manager)
        calls = self.bridges[0].calls[before:]
        self.assertEqual(len(calls), 1)
        self.assertIn("untrusted_draft_for_independent_review", calls[0]["prompt"])
        self.assertIn("My knives were always dull.", calls[0]["prompt"])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rounds"][-1]["stages"]["draft"]["output"], edit.CANDIDATE)
        self.assertEqual(done["status"], "completed")
