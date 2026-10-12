"""Activity persistence, event delivery and isolation, using disposable data only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from task_activity import ActivityStore, ActivityError, record_activity, safe_error, reconcile_activity
from data_backup import BackupManager


class ActivityStoreTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(prefix="activity-test-")
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.runtime=self.root / "runtime"
        self.store=ActivityStore(self.runtime)

    def start(self, run="ai:task:round", **fields):
        return self.store.record(run,kind="ai",owner_id="task",source_id=run.split(":")[-1],
            product="SHARPAL 202H",title="测试任务",case_id="case",status="running",phase="draft",message="正在起草。",**fields)

    def test_reload_cursor_and_read_state_are_independent_of_business_files(self):
        self.start()
        baseline=self.store.feed()
        self.assertEqual(baseline["events"],[])
        completed=self.store.record("ai:task:round",status="completed",phase="finished",message="候选已保存。")
        feed=self.store.feed(after=baseline["cursor"])
        self.assertEqual([event["event_id"] for event in feed["events"]],[completed])
        self.assertEqual(feed["counts"],{"active":0,"unread":1})
        self.assertIn("?round=round",feed["items"][0]["url"])
        self.assertEqual(feed["items"][0]["product"],"202H")
        self.assertIsNone(self.store.record("ai:task:round",status="completed",phase="finished",message="候选已保存。"))
        reopened=ActivityStore(self.runtime)
        self.assertEqual(reopened.feed()["epoch"],baseline["epoch"])
        reopened.read({"event_ids":[completed]})
        self.assertEqual(self.store.feed()["counts"]["unread"],0)
        self.assertEqual(len(reopened.logs("ai:task:round")["logs"]),2)

    def test_atomic_claim_across_tabs_and_explicit_recovery_event(self):
        self.start()
        first=self.store.record("ai:task:round",status="needs_attention",phase="pending",message="待恢复。",event_key="pending")
        body={"browser_id":"browser-0123456789","event_ids":[first]}
        with ThreadPoolExecutor(max_workers=6) as pool:
            claims=list(pool.map(lambda _:self.store.claim(body),range(12)))
        self.assertEqual(sum(len(item["claimed"]) for item in claims),1)
        recovered=self.store.record("ai:task:round",status="needs_attention",phase="saved",message="已恢复，待审查。",event_key="saved")
        self.assertNotEqual(first,recovered)
        self.assertIsNone(self.store.record("ai:task:round",status="needs_attention",phase="saved",message="已恢复，待审查。",event_key="saved"))
        self.assertEqual(self.store.claim({**body,"event_ids":[first,recovered]})["claimed"],[recovered])
        self.assertEqual(self.store.feed()["counts"]["unread"],1)

    def test_retry_same_request_can_reach_same_failed_terminal_again(self):
        self.start()
        first=self.store.record("ai:task:round",status="failed",phase="failed",message="请求未完成。",event_key="safe_failed:failed")
        self.store.record("ai:task:round",status="running",phase="retry",message="正在重试原请求。")
        second=self.store.record("ai:task:round",status="failed",phase="failed",message="请求未完成。",event_key="safe_failed:failed")
        self.assertIsNotNone(second)
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.feed()["counts"],{"active":0,"unread":1})
        self.assertIsNone(self.store.record("ai:task:round",status="failed",phase="failed",message="请求未完成。",event_key="safe_failed:failed"))

    def test_paginated_deltas_do_not_skip_completion_and_concurrent_logs_are_ordered(self):
        start=self.store.feed()["cursor"]
        for number in range(7):
            run=f"ai:task:round-{number}"
            self.start(run)
            self.store.record(run,status="completed",phase="finished",message="已保存。")
        seen=[]
        while True:
            page=self.store.feed(after=start,limit=3)
            seen.extend(event["event_id"] for event in page["events"])
            start=page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(len(set(seen)),7)
        self.start("ai:task:parallel")
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda number:self.store.record("ai:task:parallel",phase=f"slide-{number}",message="已完成一张处理。",event_key=f"slide-{number}"),range(20)))
        logs=self.store.logs("ai:task:parallel",limit=10)
        self.assertTrue(logs["has_more"])
        rest=self.store.logs("ai:task:parallel",after_seq=logs["next_seq"])
        seq=[item["seq"] for item in logs["logs"]+rest["logs"]]
        self.assertEqual(len(seq),21)
        self.assertEqual(seq,sorted(set(seq)))

    def test_all_read_is_bounded_and_cancellation_is_not_notified(self):
        self.start()
        first=self.store.record("ai:task:round",status="completed",phase="finished",message="已保存。")
        self.start("ai:task:round2")
        second=self.store.record("ai:task:round2",status="failed",phase="failed",message="未完成。")
        self.store.read({"all":True,"before_event_id":first})
        self.assertEqual(self.store.feed()["counts"]["unread"],1)
        self.assertEqual([event["event_id"] for event in self.store.feed(after=0)["events"]],[second])
        self.start("ai:task:cancelled")
        self.store.record("ai:task:cancelled",status="cancelled",phase="finished",message="已取消。")
        self.assertEqual(len(self.store.feed(after=second)["events"]),0)

    def test_delete_cascades_logs_notifications_claims_and_incremental_removal(self):
        self.start()
        event=self.store.record("ai:task:round",status="completed",phase="done",message="完成。")
        self.store.claim({"browser_id":"browser-0123456789","event_ids":[event]})
        before=self.store.feed()["cursor"]
        self.store.delete_case("case")
        feed=self.store.feed(after=before)
        self.assertEqual(feed["removed"],["ai:task:round"])
        self.assertEqual(feed["counts"],{"active":0,"unread":0})
        with self.store._connect() as db:
            for table in ("runs","logs","notifications","claims"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],0)

    def test_filters_pagination_and_errors(self):
        self.start()
        self.start("ai:task:round2")
        page=self.store.feed(product="202H",owner_id="task",limit=1)
        self.assertTrue(page["has_more"])
        self.assertEqual(len(self.store.feed(before=page["next_before"])["items"]),1)
        self.assertEqual(len(self.store.feed(owner_id="task",source_id="round2")["items"]),1)
        self.assertEqual(self.store.feed(product="306N")["items"],[])
        for kwargs in ({"after":"-1"},{"limit":0},{"filter":"invalid"}):
            with self.assertRaises(ActivityError):
                self.store.feed(**kwargs)
        with self.assertRaises(ActivityError):
            self.store.read({"all":True})

    def test_recording_failure_never_raises_and_error_text_is_classified(self):
        with patch.object(self.store,"record",side_effect=sqlite3.OperationalError("disk full")):
            self.assertIsNone(record_activity(SimpleNamespace(activity=self.store),"ai:task:round",status="running"))
        for value in ("token=sk_secret c:/private/secret.txt", "network token=sk_secret", "磁盘 c:/private/secret"):
            self.assertNotIn("secret",safe_error(value))
        with patch.object(self.store,"_connect",side_effect=sqlite3.OperationalError("unavailable")):
            self.assertFalse(self.store.feed()["available"])

    def test_backup_restore_preserves_logs_and_read_but_resets_feed_epoch(self):
        self.start()
        event=self.store.record("ai:task:round",status="completed",phase="done",message="完成。")
        self.store.read({"event_ids":[event]})
        old_epoch=self.store.feed()["epoch"]
        skill=self.root / "skill"
        (skill / "cases").mkdir(parents=True)
        manager=BackupManager(skill,self.runtime)
        archive=manager.create_archive(self.root / "backups")["path"]
        self.store.delete_owner("task")
        manager.restore_archive(archive,self.runtime / "data-transfer" / "recovery")
        self.store.reload(reset_epoch=True)
        self.assertNotEqual(self.store.feed()["epoch"],old_epoch)
        self.assertTrue(self.store.feed()["items"][0]["read"])
        self.assertEqual(len(self.store.logs("ai:task:round")["logs"]),2)

    def test_restart_reconciles_existing_runs_without_creating_old_history(self):
        self.start()
        self.start("ai:deleted:round")
        # The helper deliberately uses the common owner; assign the deleted owner separately.
        self.store.record("ai:deleted:round",owner_id="deleted")
        self.store.record("images:IMG-test",kind="images",owner_id="CAR-test",source_id="IMG-test",
                          status="running",phase="generation",message="正在出图。")
        catalog={"jobs":[],"carousels":[{"id":"CAR-test","generations":[{"id":"IMG-test","status":"completed"}]}]}
        app=SimpleNamespace(activity=self.store,lock=threading.RLock(),runtime=self.runtime,
            production=SimpleNamespace(read=lambda:catalog),
            ai=SimpleNamespace(_locations={"task":None},_read=lambda _: {"rounds":[{"round_id":"round","status":"interrupted"}]}))
        reconcile_activity(app)
        runs={row["run_id"]:row for row in self.store.feed()["items"]}
        self.assertEqual(set(runs),{"ai:task:round","images:IMG-test"})
        self.assertEqual(runs["ai:task:round"]["status"],"interrupted")
        self.assertEqual(runs["images:IMG-test"]["status"],"needs_attention")
        first=self.store.feed()["cursor"]
        reconcile_activity(app)
        self.assertEqual(self.store.feed()["cursor"],first)

    def test_restart_committed_voice_uses_saved_quality_and_never_resends(self):
        run="voice:PRD-test:request-12345"
        self.store.record(run,kind="voice",owner_id="PRD-test",source_id="request-12345",status="running")
        folder=self.runtime / "production" / "operations"
        folder.mkdir(parents=True)
        (folder / "operation.json").write_text(json.dumps({"job_id":"PRD-test","kind":"take","request_id":"request-12345",
            "state":"committed","take_id":"TAK-test","result":{"quality":{"status":"failed"}}}),encoding="utf-8")
        app=SimpleNamespace(activity=self.store,lock=threading.RLock(),runtime=self.runtime,
            production=SimpleNamespace(read=lambda:{"jobs":[{"id":"PRD-test","takes":[]}],"carousels":[]}),
            ai=SimpleNamespace(_locations={}))
        reconcile_activity(app)
        row=self.store.feed()["items"][0]
        self.assertEqual((row["status"],row["source_id"]),("needs_attention","TAK-test"))
        self.assertIsNone(self.store.record(run,status="stopping",phase="stop",message="正在停止。"))

    def test_validation_and_reused_package_require_this_request_receipt(self):
        for kind,owner,source in (("validation","PRD-test","EDT-test"),("package","CAR-test","DLV-test")):
            self.store.record(f"{kind}:{owner}:request-12345",kind=kind,owner_id=owner,source_id=source,status="running")
        catalog={"jobs":[{"id":"PRD-test","edits":[{"id":"EDT-test","status":"awaiting_review"}]}],
                 "carousels":[{"id":"CAR-test","deliveries":[{"id":"DLV-test"}]}],"requests":{}}
        app=SimpleNamespace(activity=self.store,lock=threading.RLock(),runtime=self.runtime,
            production=SimpleNamespace(read=lambda:catalog),ai=SimpleNamespace(_locations={}))
        reconcile_activity(app)
        self.assertEqual({row["status"] for row in self.store.feed()["items"]},{"interrupted"})

    def test_pending_recovery_with_lost_final_log_is_reconciled_from_saved_receipt(self):
        run="voice:PRD-test:request-12345"
        self.store.record(run,kind="voice",owner_id="PRD-test",source_id="TAK-test",status="needs_attention",phase="结果已保存，正在登记",message="结果已取得，资料尚未登记。")
        before=self.store.feed()["items"][0]["event_id"]
        folder=self.runtime / "production" / "operations"
        folder.mkdir(parents=True)
        (folder / "operation.json").write_text(json.dumps({"job_id":"PRD-test","kind":"take","request_id":"request-12345",
            "state":"committed","take_id":"TAK-test","result":{"quality":{"status":"failed"}}}),encoding="utf-8")
        app=SimpleNamespace(activity=self.store,lock=threading.RLock(),runtime=self.runtime,
            production=SimpleNamespace(read=lambda:{"jobs":[{"id":"PRD-test","takes":[]}],"carousels":[]}),
            ai=SimpleNamespace(_locations={}))
        reconcile_activity(app)
        row=self.store.feed()["items"][0]
        self.assertEqual(row["status"],"needs_attention")
        self.assertIn("对齐待核对",row["message"])
        self.assertNotEqual(row["event_id"],before)
        cursor=self.store.feed()["cursor"]
        reconcile_activity(app)
        self.assertEqual(cursor,self.store.feed()["cursor"])


if __name__=="__main__":
    unittest.main()
