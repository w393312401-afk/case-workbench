"""Observation decisions survive restarts; retries and stale tabs cannot overwrite them."""
from datetime import date, timedelta
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from creation_store import CreationError
from review_followups import ReviewFollowupStore
from review_policy import review_context, review_today
from review_legacy_fixture import seed_legacy_followup
from direction_store import fingerprint
import test_server as fixture
import test_performance as performance


class FollowupStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewFollowupStore(self.temp.name)
        self.video = {"product":"202H", "video_id":"7600000000000000001", "window_end":"2026-09-13"}

    def body(self, **values):
        return {**self.video, "revision":self.store.public()["revision"], "request_id":uuid4().hex,
                "state":"reviewed", "note":"下轮改成先演示装夹。", **values}

    def test_restart_replay_history_and_stale_write(self):
        body = self.body()
        first = self.store.save(body, self.video)
        self.store = ReviewFollowupStore(self.temp.name)
        self.assertEqual(self.store.save(body, None), first)
        self.assertEqual(len(self.store.public("202H")["entries"]), 1)
        self.assertEqual(self.store.public("129N")["entries"], [])
        with self.assertRaises(CreationError):
            self.store.save({**body,"request_id":uuid4().hex,"note":"旧页面"}, self.video)
        changed = self.store.save(self.body(state="pending",note="重新核对"), self.video)
        self.assertEqual(len(changed["entry"]["history"]), 2)
        self.assertTrue(list((self.store.root/"backups").glob("*.json")))

    def test_bad_dates_empty_conclusions_wrong_product_and_changed_window_rejected(self):
        for values in ({"state":"reviewed","note":""}, {"state":"deferred","defer_until":"2026-02-30"},
                       {"state":"deferred","defer_until":date.today().isoformat()}, {"product":"129N"},
                       {"window_end":"2026-09-20"}, {"state":"invalid"}):
            with self.subTest(values=values), self.assertRaises(CreationError):
                self.store.save(self.body(**values), self.video)
        self.assertEqual(self.store.public()["entries"], [])
        saved = self.store.save(self.body(state="deferred",defer_until=(date.today()+timedelta(days=2)).isoformat()),self.video)
        self.assertEqual(saved["entry"]["state"],"deferred")

    def test_corrupt_catalog_is_not_overwritten(self):
        self.store.root.mkdir(parents=True)
        self.store.path.write_text('{"schema_version":1,"entries":[null],"requests":{}}',encoding="utf-8")
        before = self.store.path.read_bytes()
        with self.assertRaises(CreationError):
            self.store.public()
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_retired_actions_and_fields_are_rejected_without_writing(self):
        for values in ({"state": "observing", "decision": "observe"}, {"state": "observing"},
                       {"decision": "observe"}, {"decision": "none"},
                       {"next_review_on": "2999-01-01"}, {"observation_reason": "等待下一轮数据"},
                       {"outcome": "negative"}, {"outcome": ""}, {"evidence": "preliminary"}, {"evidence": None}):
            with self.subTest(values=values), self.assertRaises(CreationError) as caught:
                self.store.save(self.body(**values), self.video)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.store.public()["entries"], [])
        self.assertFalse(self.store.path.exists())

    def test_legacy_observation_remains_readable_and_can_close_without_erasing_history(self):
        next_day = (date.today() + timedelta(days=3)).isoformat()
        old = seed_legacy_followup(self.store, self.video, state="observing", decision="observe",
            next_review_on=next_day, observation_reason="等下一次报表", outcome="unknown", evidence="insufficient")
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.public()["entries"][0], old)
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(CreationError):
            self.store.validate_source(self.store.source_snapshot(old), "202H")
        entry = self.store.save(self.body(decision="stop", note="数据齐全，本条结束。"), self.video)["entry"]
        self.assertEqual((entry["state"], entry["decision"]), ("reviewed", "stop"))
        self.assertEqual((entry["next_review_on"], entry["observation_reason"]), ("", ""))
        self.assertEqual(entry["history"][0], old["history"][0])
        self.assertEqual((entry["outcome"], entry["evidence"]), ("unknown", "insufficient"))

    def test_changing_work_state_preserves_the_existing_assessment(self):
        seed_legacy_followup(self.store, self.video, outcome="mixed", evidence="preliminary")
        entry = self.store.save(self.body(state="pending", note="重新核对数据"), self.video)["entry"]
        self.assertEqual((entry["outcome"], entry["evidence"]), ("mixed", "preliminary"))
        self.assertEqual(entry["history"][0]["outcome"], "mixed")
        entry = self.store.save(self.body(decision="stop", note="本条结束"), self.video)["entry"]
        self.assertEqual((entry["outcome"], entry["evidence"]), ("mixed", "preliminary"))

    def test_new_review_does_not_infer_retired_assessments(self):
        self.video.update(orders=4, window_views=20000)
        entry = self.store.save(self.body(decision="stop", reference_value="weak"), self.video)["entry"]
        self.assertEqual((entry["outcome"], entry["evidence"]), ("", ""))

    def test_old_success_receipt_replays_without_reenabling_retired_writes(self):
        body = self.body(decision="none", outcome="unknown", evidence="preliminary", note="")
        old = seed_legacy_followup(self.store, self.video, decision="none", outcome="unknown", evidence="preliminary",
                                  note="本轮暂无可复用发现。")
        data = self.store.read()
        result = {"entry": old, "revision": self.store.revision(data)}
        data["requests"][body["request_id"]] = {"hash": fingerprint(["followup", body]), "result": result, "at": old["at"]}
        self.store.write(data)
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.save(body, None), result)
        self.assertEqual(self.store.path.read_bytes(), before)
        with self.assertRaises(CreationError):
            self.store.save(self.body(decision="none", note="x"), self.video)

    def test_old_records_are_read_without_rewriting_or_inventing_assessments(self):
        old = {**self.video, "state": "reviewed", "decision": "none", "note": "已看，无结论。",
               "at": "2026-09-20T12:00:00", "defer_until": "", "history": []}
        self.store.root.mkdir(parents=True)
        self.store.path.write_text(json.dumps({"schema_version": 1, "entries": [old], "requests": {}}), encoding="utf-8")
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.public()["entries"], [old])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_early_or_incomplete_review_saves_with_a_warning_but_no_manual_observation(self):
        for age, complete in ((13, True), (14, False)):
            self.video.update(posted_on=(review_today() - timedelta(days=age)).isoformat(),
                              observation_coverage_complete=complete, window_views=2000)
            context = self.video["review_context"] = review_context(self.video)
            self.assertFalse(context["closing_allowed"])
            self.assertIn("可再改", context["early_warning"])
            saved = self.store.save(self.body(reference_value="weak"), self.video)
            self.assertEqual(saved["entry"]["state"], "reviewed")
        with self.assertRaises(CreationError):
            self.store.save(self.body(state="observing", decision="observe", note=""), self.video)

    def test_complete_fourteen_days_can_close_and_replay_survives_readiness_change(self):
        self.video.update(posted_on=(review_today() - timedelta(days=14)).isoformat(),
                          observation_coverage_complete=True, window_views=2000)
        self.video["review_context"] = review_context(self.video)
        body = self.body(decision="stop", note="已完成人工复盘。")
        saved = self.store.save(body, self.video)
        self.assertEqual(saved["entry"]["state"], "reviewed")
        self.video["observation_coverage_complete"] = False
        self.video["review_context"] = review_context(self.video)
        self.assertEqual(self.store.save(body, self.video), saved)
        # Gaps found later leave the conclusion editable; the form warns instead.
        self.assertEqual(self.store.save(self.body(decision="stop", note="补充说明"), self.video)["entry"]["note"], "补充说明")



class FollowupHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_authenticated_decision_survives_read_and_replay(self):
        performance.build_exports(self.root/"exports")
        self.server.app.performance.sources=[self.root/"exports"]
        self.server.app.performance.sync()
        status, _, empty = self.request("GET","/api/review-followups?product=202H")
        self.assertEqual(status,200)
        body={"request_id":uuid4().hex,"revision":empty["revision"],"product":"202H","video_id":performance.LINKED,
              "window_end":"2026-09-13","state":"reviewed","note":"已比较实际成片。"}
        self.assertEqual(self.request("POST","/api/review-followups",body,authenticated=False)[0],403)
        status,_,saved=self.request("POST","/api/review-followups",body)
        self.assertEqual(status,200,saved)
        self.assertEqual(self.request("POST","/api/review-followups",body)[2],saved)
        self.assertEqual(self.request("GET","/api/review-followups?product=202H")[2]["entries"][0]["note"],body["note"])
        self.assertEqual(self.request("GET","/api/review-followups?product=129N")[2]["entries"],[])


if __name__ == '__main__':
    unittest.main()
