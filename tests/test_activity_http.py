"""Activity endpoints keep the workbench's local-origin and write-token guards."""
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote
from uuid import uuid4
import task_activity
import test_ai_http as fixture


class ActivityHTTPTests(unittest.TestCase):
    setUp=fixture.AIHTTPTests.setUp
    close=fixture.AIHTTPTests.close
    request=fixture.AIHTTPTests.request

    def test_activity_feed_logs_read_claim_and_guard(self):
        activity=self.server.app.activity
        activity.record("ai:task:round",kind="ai",owner_id="task",source_id="round",product="202H",title="任务",status="running",phase="draft",message="正在起草。")
        baseline=self.request("GET","/api/activity")[1]
        event=activity.record("ai:task:round",status="completed",phase="finished",message="候选已保存。")
        status,feed=self.request("GET",f"/api/activity?after={baseline['cursor']}")
        self.assertEqual(status,200)
        self.assertEqual(feed["events"][0]["event_id"],event)
        status,logs=self.request("GET","/api/activity/"+quote("ai:task:round",safe="")+"/logs")
        self.assertEqual(status,200)
        self.assertEqual(len(logs["logs"]),2)
        status,tail=self.request("GET","/api/activity/"+quote("ai:task:round",safe="")+"/logs?tail=1&limit=1")
        self.assertEqual((status,[row["message"] for row in tail["logs"]],tail["has_earlier"]),(200,["候选已保存。"],True))
        status,filtered=self.request("GET","/api/activity?filter=attention&kind=ai&q=%E4%BB%BB%E5%8A%A1")
        self.assertEqual((status,filtered["items"]),(200,[]))
        body={"browser_id":"browser-0123456789","event_ids":[event]}
        self.assertEqual(self.request("POST","/api/activity/notify-claim",body,authenticated=False)[0],403)
        self.assertEqual(self.request("POST","/api/activity/notify-claim",body)[1]["claimed"],[event])
        self.assertEqual(self.request("POST","/api/activity/notify-claim",body)[1]["claimed"],[])
        self.assertEqual(self.request("POST","/api/activity/read",{"event_ids":[event]})[0],200)
        self.assertEqual(self.request("GET","/api/activity")[1]["counts"]["unread"],0)
        for url in ("/api/activity?after=-1","/api/activity?limit=0","/api/activity?product=a&product=b","/api/activity?unknown=1","/api/activity?kind=unknown",
                    "/api/activity/"+quote("ai:task:round",safe="")+"/logs?tail=yes"):
            self.assertEqual(self.request("GET",url)[0],400,url)
        self.assertEqual(self.request("GET","/api/activity",headers={"Origin":"https://example.com"})[0],403)

    def test_real_case_deletion_cleans_activity_without_waiting_for_restart(self):
        activity=self.server.app.activity
        event=activity.record("ai:task:round",kind="ai",owner_id="task",source_id="round",case_id=self.case_id,
                              product="202H",title="即将删除案例的任务",status="completed",message="完成。")
        activity.claim({"browser_id":"browser-0123456789","event_ids":[event]})
        self.store.set_archived(self.case_id,True)
        status,plan=self.request("GET",f"/api/cases/{self.case_id}/delete-preview")
        self.assertEqual(status,200,plan)
        status,result=self.request("POST",f"/api/cases/{self.case_id}/delete",{"request_id":uuid4().hex,
            "revision":plan["revision"],"plan_token":plan["plan_token"],"confirmation":self.case_id,"include_source":False})
        self.assertEqual(status,200,result)
        feed=self.request("GET","/api/activity")[1]
        self.assertEqual(feed["items"],[])
        self.assertEqual(feed["counts"]["unread"],0)
        self.assertEqual(self.request("GET","/api/activity/ai:task:round/logs")[0],404)

    def test_poll_answers_while_the_workbench_lock_is_held(self):
        app=self.server.app
        app.activity.record("ai:task:round",kind="ai",owner_id="AIT-000000000000",source_id="round",product="202H",title="任务",status="failed",message="失败。")
        release,held=threading.Event(),threading.Event()
        def hold():
            with app.lock:
                held.set()
                release.wait(20)
        holder=threading.Thread(target=hold,daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        started=threading.Event()
        calls=[]
        projection=task_activity.refresh_activity_projection
        def counted(owner):
            calls.append(1)
            started.set()
            with owner.lock:  # Stands in for any projection step that needs the workbench lock.
                pass
            projection(owner)
        try:
            with patch.object(task_activity,"refresh_activity_projection",counted):
                began=time.monotonic()
                status,feed=self.request("GET","/api/activity")
                self.assertEqual((status,feed["counts"]["active"]),(200,0))
                self.assertLess(time.monotonic()-began,5)
                self.assertTrue(started.wait(5))
                # The stuck projection stays the only one: later polls neither wait for it nor start another.
                self.assertEqual(self.request("GET","/api/activity")[0],200)
                self.assertEqual(len(calls),1)
                self.assertTrue(app.activity.projection_gate.locked())
                release.set()
                holder.join(5)
                deadline=time.monotonic()+5
                while app.activity.projection_gate.locked() and time.monotonic()<deadline:
                    time.sleep(.01)
                self.assertFalse(app.activity.projection_gate.locked())
        finally:
            release.set()

    def test_archive_marks_use_one_connection_and_write_only_differences(self):
        app=self.server.app
        for number in range(3):
            app.activity.record(f"ai:AIT-{number:012x}:round",kind="ai",owner_id=f"AIT-{number:012x}",source_id="round",product="202H",title="任务",status="completed",message="完成。")
        marks={f"AIT-{number:012x}":number==1 for number in range(3)}
        before=app.activity.feed()["cursor"]
        with patch.object(app.activity,"_connect",wraps=app.activity._connect) as connect:
            app.activity.archive_owners(marks)
        self.assertEqual(connect.call_count,1)
        rows={row["owner_id"]:row["owner_archived"] for row in app.activity.existing()}
        self.assertEqual(rows,{"AIT-000000000000":0,"AIT-000000000001":1,"AIT-000000000002":0})
        changed=app.activity.feed()["cursor"]
        self.assertEqual(changed,before+1)  # Only the run that differed is written.
        app.activity.archive_owners(marks)
        self.assertEqual(app.activity.feed()["cursor"],changed)

    def test_new_activity_assets_are_served(self):
        for url in ("/activity.js","/activity.css"):
            status,body=self.request("GET",url)
            self.assertEqual(status,200)
            self.assertTrue(body)
