"""Task presentation is action-aware and does not mutate provenance or saved text."""
import copy
import unittest

from content_management import build_content_items
from content_naming import task_display_metadata


class NoiseContentTests(unittest.TestCase):
    def task(self, action="variants", **values):
        return {"task_id": "AIT-fixture", "action": action, "status": "completed",
                "product": "202H", "management_state": "pending_selection",
                "created_at": "2026-10-23T02:30:00Z",
                "context": {"direction_id": "202H-S1", "title": "202H-S1",
                            "writing_topic": "固定角度，稳定推进"},
                "rounds": [{"round_id": "AIR-fixture", "instruction": "仅测试"}], **values}

    def row(self, task):
        return build_content_items([task], [], [], [])[0]

    def test_completed_review_keeps_attention_with_correct_next_action(self):
        original = self.task("review")
        before = copy.deepcopy(original)
        row = self.row(original)
        self.assertEqual(row["kind_label"], "审稿")
        self.assertEqual(row["stage"], "pending_confirmation")
        self.assertEqual(row["next_label"], "查看审稿结论")
        self.assertTrue(row["needs_attention"])
        self.assertNotIn("候选", row["title"])
        self.assertEqual(original, before)

    def test_retrospectives_stay_out_of_content_rows(self):
        for state in ({}, {"status": "running"}, {"status": "failed"}, {"management_state": "needs_revision"}):
            self.assertEqual(build_content_items([self.task("retrospective", **state)], [], [], []), [])

    def test_nonwriting_disposition_and_execution_remain_separate(self):
        for action in ("review",):
            self.assertTrue(self.row(self.task(action, status="failed"))["needs_attention"])
            self.assertTrue(self.row(self.task(action, management_state="needs_revision"))["needs_attention"])
            confirmed = self.row(self.task(action, management_state="adopted"))
            self.assertTrue(confirmed["history"])
            self.assertFalse(confirmed["needs_attention"])
            running = self.row(self.task(action, status="running"))
            self.assertTrue(running["busy"])
            self.assertFalse(running["needs_attention"])

    def test_pure_metadata_prefers_video_headline_and_created_time(self):
        task = self.task("retrospective", updated_at="2026-10-23T12:00:00Z",
                         context={"title": "1234567890123456789 · 复盘起草",
                                  "review_video": {"headline": "Angle guide demonstration"}})
        before = copy.deepcopy(task)
        result = task_display_metadata(task)
        self.assertEqual(result["display_title"], "Angle guide demonstration｜复盘结果")
        self.assertEqual(result["task_started_label"], "10/23 10:30 发起")
        self.assertEqual(task, before)

    def test_same_topic_does_not_settle_independent_candidates(self):
        first, second = self.task(), self.task(task_id="AIT-other")
        rows = build_content_items([first, second], [], [], [])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["needs_attention"] for row in rows))
        self.assertTrue(all(row["stage"] == "pending_selection" for row in rows))

    def test_exact_legacy_nonwriting_adoption_is_still_settled_without_guessing(self):
        task = self.task("review", management_state="adopted")
        script = {"id": "SCR-exact", "family_id": "SCR-exact", "version": 1,
                  "product": "202H", "title": "旧任务已明确采用的正文", "status": "adopted",
                  "source": {"task_id": task["task_id"], "round_id": "AIR-fixture"}}
        before = copy.deepcopy([task, script])
        rows = build_content_items([task], [script], [], [])
        self.assertNotIn("task:AIT-fixture", {row["id"] for row in rows})
        self.assertIn("script:SCR-exact", {row["id"] for row in rows})
        self.assertEqual([task, script], before)

    def test_raw_persisted_task_uses_frozen_video_without_requiring_public_context(self):
        row = self.task("retrospective")
        row.pop("context")
        row["case_title"] = "1234567890123456789 · 复盘起草"
        row["snapshot"] = {"creation_context": {"review_video": {"headline": "Frozen video title"}}}
        before = copy.deepcopy(row)
        self.assertEqual(task_display_metadata(row)["display_title"], "Frozen video title｜复盘结果")
        self.assertEqual(row, before)


if __name__ == "__main__":
    unittest.main()
