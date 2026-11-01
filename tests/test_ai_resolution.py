"""Failed or finished AI tasks that something later settled leave 待我处理; nothing stored changes."""
import unittest

from ai_resolution import ai_task_resolutions
from content_management import build_content_items, _edit_note
from task_activity import _failure_reason


def task(task_id, *, status="completed", created="2026-10-19T05:00:00+00:00", finished="2026-10-19T05:01:00+00:00",
         sha="same", subject="S1", product="202H", action="variants", fill=False, source=None, state="pending_selection"):
    return {"task_id": task_id, "status": status, "created_at": created, "latest_round_finished_at": finished,
            "instruction_sha256": sha, "subject_key": subject, "product": product, "action": action,
            "fill_request": fill, "context": {"source_production_id": source} if source else {},
            "management_state": state, "archived": False}


def job(job_id, fill_task, status="completed", started="2026-10-19T06:03:00+00:00", error=""):
    return {"id": job_id, "title": "会手磨，也可以把日常维护交给导杆",
            "ai_fill": {"task_id": fill_task, "status": status, "started_at": started,
                        "finished_at": "2026-10-19T06:04:00+00:00", "error": error}}


class FillRules(unittest.TestCase):
    def test_failed_fill_is_settled_when_the_same_job_was_filled_again(self):
        failed = task("AIT-000000000001", status="failed", fill=True, finished="2026-10-19T05:57:53+00:00")
        refill = task("AIT-000000000002", fill=True, created="2026-10-19T06:03:34+00:00")
        found = ai_task_resolutions([failed, refill], [job("PRD-000000000001", refill["task_id"])])
        self.assertEqual(found[failed["task_id"]]["kind"], "refilled")
        self.assertEqual(found[failed["task_id"]]["by_job"], "PRD-000000000001")
        self.assertIn("已重新补全", found[failed["task_id"]]["note"])
        # The refill itself is written back to that job.
        self.assertEqual(found[refill["task_id"]]["kind"], "written_back")

    def test_jobs_with_the_same_title_are_told_apart_by_number(self):
        failed = task("AIT-000000000001", status="failed", fill=True, sha="mine")
        other = task("AIT-000000000002", fill=True, sha="theirs", created="2026-10-19T06:03:34+00:00")
        # Another job with the identical title was filled with a different instruction: not this failure's job.
        found = ai_task_resolutions([failed, other], [job("PRD-000000000002", other["task_id"])])
        self.assertNotIn(failed["task_id"], found)
        named = task("AIT-000000000003", status="failed", fill=True, sha="mine", source="PRD-000000000001")
        found = ai_task_resolutions([named, other], [job("PRD-000000000002", other["task_id"])])
        self.assertNotIn(named["task_id"], found)

    def test_a_fill_started_before_the_failure_ended_does_not_settle_it(self):
        failed = task("AIT-000000000001", status="failed", fill=True, finished="2026-10-19T06:10:00+00:00")
        refill = task("AIT-000000000002", fill=True)
        found = ai_task_resolutions([failed, refill], [job("PRD-000000000001", refill["task_id"])])
        self.assertNotIn(failed["task_id"], found)

    def test_a_fill_that_could_not_be_written_stays_with_its_reason(self):
        done = task("AIT-000000000001", fill=True)
        found = ai_task_resolutions([done], [job("PRD-000000000001", done["task_id"], status="failed", error="句数不符")])
        self.assertEqual(found[done["task_id"]], {**found[done["task_id"]], "kind": "not_written", "note": "未写回：句数不符"})


class RerunRules(unittest.TestCase):
    def failed(self, **values):
        return task("AIT-00000000000f", status="failed", **values)

    def test_a_later_completed_rerun_of_the_same_request_settles_the_failure(self):
        rerun = task("AIT-00000000000a", created="2026-10-19T06:00:00+00:00")
        self.assertEqual(ai_task_resolutions([self.failed(), rerun], [])["AIT-00000000000f"]["kind"], "rerun")

    def test_a_different_subject_or_instruction_is_not_a_rerun(self):
        for change in ({"subject_key": "S2"}, {"instruction_sha256": "other"}, {"product": "198H"}, {"action": "review"}):
            rerun = {**task("AIT-00000000000a", created="2026-10-19T06:00:00+00:00"), **change}
            with self.subTest(change=change):
                self.assertEqual(ai_task_resolutions([self.failed(), rerun], []), {})

    def test_success_first_then_failure_keeps_the_failure(self):
        earlier = task("AIT-00000000000a", created="2026-10-19T04:00:00+00:00")
        self.assertEqual(ai_task_resolutions([self.failed(), earlier], []), {})

    def test_an_unfinished_rerun_settles_nothing(self):
        for status in ("failed", "running", "cancelled"):
            rerun = task("AIT-00000000000a", status=status, created="2026-10-19T06:00:00+00:00")
            with self.subTest(status=status):
                self.assertEqual(ai_task_resolutions([self.failed(), rerun], []), {})

    def test_abandoned_is_reported_for_the_task_panel(self):
        self.assertEqual(ai_task_resolutions([self.failed(state="abandoned")], [])["AIT-00000000000f"]["kind"], "abandoned")


class ContentRows(unittest.TestCase):
    def rows(self, tasks, jobs):
        return {row["id"]: row for row in build_content_items(tasks, [], jobs, [])}

    def test_settled_rows_leave_attention_and_keep_their_history(self):
        failed = task("AIT-000000000001", status="failed", fill=True, finished="2026-10-19T05:57:53+00:00")
        refill = task("AIT-000000000002", fill=True, created="2026-10-19T06:03:34+00:00")
        target = {**job("PRD-000000000001", refill["task_id"]), "product": "202H", "lines": []}
        rows = self.rows([failed, refill], [target])
        for key, label in (("task:AIT-000000000001", "已重跑完成"), ("task:AIT-000000000002", "已写回制作单")):
            with self.subTest(key=key):
                self.assertFalse(rows[key]["needs_attention"])
                self.assertTrue(rows[key]["history"])
                self.assertEqual(rows[key]["stage_label"], label)
                self.assertIn("#production/PRD-000000000001", [link["hash"] for link in rows[key]["links"]])

    def test_needs_revision_brings_a_settled_task_back(self):
        done = task("AIT-000000000001", fill=True, state="needs_revision")
        target = {**job("PRD-000000000001", done["task_id"]), "product": "202H", "lines": []}
        row = self.rows([done], [target])["task:AIT-000000000001"]
        self.assertTrue(row["needs_attention"])
        self.assertEqual(row["stage"], "needs_revision")

    def test_not_written_stays_in_attention(self):
        done = task("AIT-000000000001", fill=True)
        target = {**job("PRD-000000000001", done["task_id"], status="failed", error="句数不符"), "product": "202H", "lines": []}
        row = self.rows([done], [target])["task:AIT-000000000001"]
        self.assertTrue(row["needs_attention"])
        self.assertEqual(row["stage_label"], "未写回")

    def test_edit_note_matches_the_status_chip(self):
        self.assertEqual(_edit_note({"status": "needs_material"}), "素材不足，补齐后重新剪辑")
        self.assertEqual(_edit_note({"status": "awaiting_review"}), "剪辑结果待人工审查")
        self.assertEqual(_edit_note({"status": "timeline", "recovery_operations": [{}]}), "剪辑结果待修正")
        self.assertEqual(_edit_note({"status": "timeline"}), "当前时间线尚未完成剪辑审查")


class ActivityText(unittest.TestCase):
    def test_reason_drops_the_generic_sentence(self):
        self.assertEqual(_failure_reason("本轮未完成，请查看任务详情。 服务连接中断，请在原任务中核对结果。"), "服务连接中断")
        self.assertEqual(_failure_reason("服务连接中断，请在原任务中核对结果。"), "服务连接中断")
        self.assertEqual(_failure_reason(""), "")


class VariantNumbers(unittest.TestCase):
    def test_store_messages_use_the_page_numbers(self):
        from direction_store import variant_number
        variants = [{"variant_type": "execution"}, {"variant_type": "direction"}, {"variant_type": "execution"}]
        self.assertEqual([variant_number(variants, index) for index in range(3)], ["#1", "方向定义", "#2"])


if __name__ == "__main__":
    unittest.main()
