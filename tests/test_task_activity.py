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

from task_activity import ActivityStore, ActivityError, activity_label, record_activity, safe_error, reconcile_activity, settle_resolved_activity
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
        self.assertEqual(feed["counts"],{"active":0,"queued":0,"unread":1})
        self.assertIn("?round=round",feed["items"][0]["url"])
        self.assertEqual(feed["items"][0]["product"],"202H")
        self.assertIsNone(self.store.record("ai:task:round",status="completed",phase="finished",message="候选已保存。"))
        reopened=ActivityStore(self.runtime)
        self.assertEqual(reopened.feed()["epoch"],baseline["epoch"])
        reopened.read({"event_ids":[completed]})
        self.assertEqual(self.store.feed()["counts"]["unread"],0)
        self.assertEqual(len(reopened.logs("ai:task:round")["logs"]),2)

    def test_restored_orphan_history_survives_reconciliation_as_archived(self):
        self.store.record("ai:old:one", kind="ai", owner_id="old", source_id="one",
                          title="旧任务", status="completed", message="完成。")
        self.store.record("ai:ordinary:one", kind="ai", owner_id="ordinary", source_id="one",
                          title="普通任务", status="completed", message="完成。")
        with self.store._connect() as db:
            db.execute("INSERT INTO meta(key,value) VALUES('restored_history_owners',?)", (json.dumps(["old"]),))
        app = SimpleNamespace(activity=self.store, lock=threading.RLock(), runtime=self.runtime,
                              production=SimpleNamespace(read=lambda: {"jobs": []}),
                              content_management=SimpleNamespace(read=lambda: {"items": {}}),
                              ai=SimpleNamespace(_locations={}))
        reconcile_activity(app)
        rows = self.store.existing()
        self.assertEqual([row["owner_id"] for row in rows], ["old"])
        self.assertEqual(rows[0]["owner_archived"], 1)
        self.assertTrue(rows[0]["title"].startswith("历史恢复 · "))

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

    def test_queued_counts_follow_admission_and_keep_waiting_tasks_current(self):
        self.start()
        run = "timeline:job:request"
        self.store.record(run, kind="timeline", owner_id="job", source_id="request", title="建立时间线",
                          status="queued", phase="等待达芬奇", message="空闲后自动开始。")
        feed = self.store.feed(filter="current")
        self.assertEqual(feed["counts"], {"active": 2, "queued": 1, "unread": 0})
        self.assertEqual({row["run_id"] for row in feed["items"]}, {"ai:task:round", run})
        self.store.record(run, status="running", phase="正在建立时间线")
        self.assertEqual(self.store.feed()["counts"], {"active": 2, "queued": 0, "unread": 0})
        self.store.record(run, status="completed", phase="已登记")
        self.assertEqual(self.store.feed()["counts"], {"active": 1, "queued": 0, "unread": 1})

    def test_retry_same_request_can_reach_same_failed_terminal_again(self):
        self.start()
        first=self.store.record("ai:task:round",status="failed",phase="failed",message="请求未完成。",event_key="safe_failed:failed")
        self.store.record("ai:task:round",status="running",phase="retry",message="正在重试原请求。")
        second=self.store.record("ai:task:round",status="failed",phase="failed",message="请求未完成。",event_key="safe_failed:failed")
        self.assertIsNotNone(second)
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.feed()["counts"],{"active":0,"queued":0,"unread":1})
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
        self.assertEqual(feed["counts"],{"active":0,"queued":0,"unread":0})
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

    def test_kind_search_and_attention_filters(self):
        self.start()
        self.store.record("edit:job:one",kind="edit",owner_id="job",source_id="one",product="202H",title="AI 剪辑 · 100%_变体",status="running",message="正在剪辑。")
        self.store.record("voice:job:two",kind="voice",owner_id="job",source_id="two",product="202H",title="生成配音 · 100x变体",status="failed",message="未完成。")
        self.store.record("validation:job:three",kind="validation",owner_id="job",source_id="three",product="202H",title="校验",status="needs_attention",message="待确认。")
        ids=lambda **kwargs:{item["run_id"] for item in self.store.feed(**kwargs)["items"]}
        self.assertEqual(ids(kind="voice"),{"voice:job:two"})
        self.assertEqual(ids(q="100"),{"edit:job:one","voice:job:two"})
        # LIKE wildcards in the search text are literal characters.
        self.assertEqual(ids(q="100%_"),{"edit:job:one"})
        self.assertEqual(ids(filter="attention"),{"voice:job:two","validation:job:three"})
        self.assertEqual(ids(filter="attention",kind="validation"),{"validation:job:three"})
        for kwargs in ({"kind":"unknown"},{"q":"x"*101}):
            with self.assertRaises(ActivityError):
                self.store.feed(**kwargs)

    def test_current_filter_keeps_open_runs_and_what_ended_since_the_bound(self):
        self.start("ai:task:running")
        for run, status in (("ai:task:failed","failed"),("ai:task:old","completed"),("ai:task:today","completed")):
            self.start(run)
            self.store.record(run,status=status,phase="finished",message="结束。")
        # Its own task: a later round of the same task would settle the failure.
        self.store.record("ai:task:failed",owner_id="other")
        with self.store._connect() as db:
            db.execute("UPDATE runs SET finished_at='2026-10-01T00:00:00.000+00:00',updated_at='2026-10-01T00:00:00.000+00:00' WHERE run_id IN ('ai:task:old','ai:task:failed')")
        ids=lambda **kwargs:{item["run_id"] for item in self.store.feed(**kwargs)["items"]}
        self.assertEqual(ids(filter="current"),{"ai:task:running","ai:task:failed"})
        self.assertEqual(ids(filter="current",since="2026-10-02T00:00:00+08:00"),{"ai:task:running","ai:task:failed","ai:task:today"})
        self.assertEqual(ids(filter="current",since="2026-10-01T00:00:00Z"),{"ai:task:running","ai:task:failed","ai:task:old","ai:task:today"})
        for kwargs in ({"since":"yesterday"},{"since":"2026-10-02T00:00:00"},{"group":"case"}):
            with self.assertRaises(ActivityError):
                self.store.feed(**kwargs)

    def test_generic_variant_titles_carry_their_opening_line(self):
        self.assertEqual(activity_label({"title":"202H-S2 变体 #4 · 身份认同","text":"不用"}),"202H-S2 变体 #4 · 身份认同")
        self.assertEqual(activity_label({"title":"第 2 条","direction_id":"202H-S2","lines":[{"spoken":"If you're into knives, you'll get it.","zh":""}]}),
                         "If you're into knives, you'll get it.")
        self.assertEqual(activity_label({"title":"第 1 条","text":"\nHook line\nsecond"}),"第 1 条「Hook line」")
        self.assertEqual(activity_label({"title":"","id":"CAR-x"}),"CAR-x")

    def test_video_activity_uses_the_same_timeline_and_topic_as_the_content_list(self):
        job={"title":"202H-S2 别再拼零散磨具","direction_id":"202H-S2","selected_take":"T2","timelines":[
            {"name":"202H-S2_旧配音","take_id":"T1"},{"name":"202H-S2_v2","take_id":"T2"},{"name":"202H-S2_试剪","take_id":"T1"}]}
        self.assertEqual(activity_label(job),"202H-S2_v2｜别再拼零散磨具")
        self.assertEqual(activity_label({**job,"selected_take":None}),"202H-S2_试剪｜别再拼零散磨具")
        self.assertEqual(activity_label({**job,"timelines":[]}),"别再拼零散磨具")

    def test_tail_pages_back_from_the_newest_lines(self):
        self.start()
        for number in range(7):
            self.store.record("ai:task:round",message=f"步骤 {number}")
        tail=self.store.logs("ai:task:round",tail="1",limit=3)
        self.assertEqual([row["message"] for row in tail["logs"]],["步骤 4","步骤 5","步骤 6"])
        self.assertTrue(tail["has_earlier"])
        self.assertEqual(tail["next_seq"],tail["logs"][-1]["seq"])
        earlier=self.store.logs("ai:task:round",before_seq=tail["first_seq"],limit=3)
        self.assertEqual([row["message"] for row in earlier["logs"]],["步骤 1","步骤 2","步骤 3"])
        oldest=self.store.logs("ai:task:round",before_seq=earlier["first_seq"],limit=3)
        self.assertEqual([row["message"] for row in oldest["logs"]],["正在起草。","步骤 0"])
        self.assertFalse(oldest["has_earlier"])
        self.store.record("ai:task:round",message="步骤 7")
        self.assertEqual([row["message"] for row in self.store.logs("ai:task:round",after_seq=tail["next_seq"])["logs"]],["步骤 7"])
        for kwargs in ({"tail":"yes"},{"tail":"1","before_seq":"3"},{"limit":"501"}):
            with self.assertRaises(ActivityError):
                self.store.logs("ai:task:round",**kwargs)

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

    def attention(self, run, kind, owner, source):
        self.store.record(run,kind=kind,owner_id=owner,source_id=source,product="202H",title="测试",status="running",phase=kind,message="处理中。")
        return self.store.record(run,status="needs_attention",phase="reconciled",message="待逐张人工确认。")

    def test_job_completion_settles_edits_but_a_newer_unselected_take_still_asks(self):
        self.attention("edit:PRD:EDT-a","edit","PRD","EDT-a")
        self.attention("voice:PRD:old","voice","PRD","TAK-old")
        self.attention("voice:PRD:new","voice","PRD","TAK-new")
        job={"id":"PRD","status":"needs_material","selected_take":"TAK-mid",
             "takes":[{"id":"TAK-old","created_at":"2026-10-13T01:00"},{"id":"TAK-mid","created_at":"2026-10-13T02:00"},{"id":"TAK-new","created_at":"2026-10-13T03:00"}]}
        app=SimpleNamespace(activity=self.store)
        settle_resolved_activity(app,"job",job)
        status=lambda run:next("resolved" if row["attention_resolved"] else row["status"] for row in self.store.existing() if row["run_id"]==run)
        self.assertEqual([status("edit:PRD:EDT-a"),status("voice:PRD:old"),status("voice:PRD:new")],["needs_attention","resolved","needs_attention"])
        settle_resolved_activity(app,"job",{**job,"status":"approved"})
        self.assertEqual([status("edit:PRD:EDT-a"),status("voice:PRD:new")],["resolved","resolved"])

    def test_settling_never_touches_pending_recovery_and_survives_an_unavailable_store(self):
        self.store.record("voice:PRD:req",kind="voice",owner_id="PRD",source_id="req",status="needs_attention",phase="结果待恢复",message="待恢复。")
        settle_resolved_activity(SimpleNamespace(activity=self.store),"job",{"id":"PRD","status":"approved"})
        self.assertEqual(self.store.existing()[0]["status"],"needs_attention")
        broken=SimpleNamespace(attention=lambda owner:(_ for _ in ()).throw(sqlite3.OperationalError("locked")))
        settle_resolved_activity(SimpleNamespace(activity=broken),"job",{"id":"PRD","status":"approved"})


if __name__=="__main__":
    unittest.main()
