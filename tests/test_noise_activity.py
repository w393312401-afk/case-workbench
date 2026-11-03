"""Attention is a current projection; historical outcomes and unknown recoveries survive."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from task_activity import ActivityStore, settle_ai_activity, settle_resolved_activity, refresh_activity_projection


class NoiseActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ActivityStore(Path(temporary.name))
        self.app = SimpleNamespace(activity=self.store)

    def fail(self, source="VOC-one", **fields):
        run = "voice:PRD-test:" + source
        self.store.record(run, kind="voice", owner_id="PRD-test", source_id=source,
                          status="failed", title="生成配音", phase="voice", message="连接中断", **fields)
        return run

    def test_ai_failure_settled_by_a_refill_leaves_current_and_comes_back_when_the_rule_stops_holding(self):
        run = "ai:AIT-000000000001:AIR-000000000001"
        self.store.record(run, kind="ai", owner_id="AIT-000000000001", source_id="AIR-000000000001", status="failed",
                          title="AI 写稿", phase="finished", message="本轮未完成，请查看任务详情。 服务连接中断，请在原任务中核对结果。")
        tasks = [{"task_id": "AIT-000000000001", "status": "failed", "fill_request": True, "instruction_sha256": "same",
                  "latest_round_finished_at": "2026-10-19T05:57:53+00:00", "context": {}, "management_state": "pending_selection"},
                 {"task_id": "AIT-000000000002", "status": "completed", "fill_request": True, "instruction_sha256": "same",
                  "created_at": "2026-10-19T06:03:34+00:00", "context": {}, "management_state": "pending_selection"}]
        jobs = [{"id": "PRD-000000000001", "ai_fill": {"task_id": "AIT-000000000002", "status": "completed",
                 "started_at": "2026-10-19T06:03:34+00:00", "finished_at": "2026-10-19T06:04:10+00:00"}}]
        self.app.lock = threading.RLock()
        self.app.ai = SimpleNamespace(list_tasks=lambda: tasks)
        self.app.production = SimpleNamespace(public=lambda: {"jobs": jobs})
        logs = self.store.logs(run)["logs"]
        settle_ai_activity(self.app)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        saved = self.store.feed()["items"][0]
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(saved["attention_resolved"])
        self.assertTrue(saved["attention_note"].startswith("服务连接中断，同一制作单 "))
        self.assertTrue(saved["attention_note"].endswith("已重新补全"))
        self.assertEqual(self.store.logs(run)["logs"], logs)
        jobs[0]["ai_fill"]["status"] = "failed"
        settle_ai_activity(self.app)
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)
        # Giving the task up settles it too, and only while it stays given up.
        tasks[0]["management_state"] = "abandoned"
        settle_ai_activity(self.app)
        self.assertEqual(self.store.feed()["items"][0]["attention_note"], "服务连接中断，已放弃")
        tasks[0]["management_state"] = "pending_selection"
        settle_ai_activity(self.app)
        self.assertFalse(self.store.feed()["items"][0]["attention_resolved"])

    def test_archiving_and_restoring_are_independent_of_content_archive_and_keep_logs(self):
        run = self.fail()
        logs = self.store.logs(run)["logs"]
        cursor = self.store.feed()["cursor"]
        self.store.archive_owner("PRD-test", True)
        self.store.archive_owner("PRD-test", True, source="content")
        delta = self.store.feed(after=cursor, filter="current")
        self.assertIn(run, delta["removed"])
        self.assertEqual(delta["items"], [])
        self.assertEqual(delta["counts"]["unread"], 0)
        self.assertEqual(self.store.logs(run)["logs"], logs)
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")
        self.store.archive_owner("PRD-test", False)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        self.store.archive_owner("PRD-test", False, source="content")
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)

    def test_archived_timeline_hides_edit_attention_and_restore_reopens_it(self):
        run = "edit:EDT-one"
        self.store.record(run, kind="edit", owner_id="PRD-test", source_id="EDT-one", status="failed")
        item = {"id": "PRD-test", "timelines": [{"unique_id": "uid-one", "archived": True}],
                "edits": [{"id": "EDT-one", "timeline_uid": "uid-one", "status": "failed"}]}
        settle_resolved_activity(self.app, "job", item)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")
        item["timelines"][0]["archived"] = False
        settle_resolved_activity(self.app, "job", item)
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)
