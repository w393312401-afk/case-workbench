"""Manual display labels never infer reference value from traffic or sales."""
import copy
from datetime import timedelta
import unittest
from unittest.mock import patch
from uuid import uuid4

from creation_store import CreationError
from manual_review_status import ManualReviewStatus, LABELS
from review_legacy_fixture import seed_legacy_followup
from review_policy import review_today
from workbench_video_publish import VideoPublicationStore
import test_feedback_chain as fixture


class ManualReviewStatusTests(unittest.TestCase):
    def setUp(self):
        self.video = {"product": "202H", "video_id": "7644133251522612493", "window_end": "2026-06-07",
                      "posted_on": "2026-05-26", "status": "focus", "orders": 999, "views": 999999}
        self.record = {**self.video, "state": "reviewed", "decision": "stop", "outcome": "negative",
                       "evidence": "preliminary", "note": "保留的人工结论", "at": "2026-10-25T01:00:00+00:00"}

    def test_all_six_labels_are_only_human_states(self):
        self.assertEqual(ManualReviewStatus([]).for_video(self.video), {"key": "pending", "label": "待复盘"})
        for value, key in (("", "unclassified"), ("none", "none"), ("weak", "weak"), ("medium", "medium"), ("high", "high")):
            with self.subTest(value=value):
                row = {**self.record, "reference_value": value}
                actual = ManualReviewStatus([row]).for_video(self.video)
                self.assertEqual(actual, {"key": key, "label": LABELS[key]})
                changed_metrics = {**self.video, "status": "drop", "orders": 0, "views": 0}
                self.assertEqual(ManualReviewStatus([row]).for_video(changed_metrics), actual)

    def test_exact_identity_and_unfinished_review_do_not_inherit_classification(self):
        for change in ({"product": "129N"}, {"video_id": "7644133251522612494"}, {"window_end": "2026-06-14"},
                       {"state": "pending"}, {"state": "deferred"}, {"state": "observing", "next_review_on": "2999-01-01"}):
            row = {**self.record, "reference_value": "high", **change}
            with self.subTest(change=change):
                self.assertEqual(ManualReviewStatus([row]).for_video(self.video)["key"], "pending")
        self.assertEqual(ManualReviewStatus([{**self.record, "product": "SHARPAL 202H"}]).for_video(self.video)["key"], "unclassified")

    def test_early_review_reopens_and_shanghai_midnight_is_respected(self):
        video = {**self.video, "posted_on": "2026-10-11"}
        review = {**self.record, "at": "2026-10-24T16:00:00+00:00", "reference_value": "weak"}
        self.assertEqual(ManualReviewStatus([review], today="2026-10-24").for_video(video)["key"], "pending")
        self.assertEqual(ManualReviewStatus([review], today="2026-10-25").for_video(video)["key"], "weak")
        review["at"] = "2026-10-24T15:59:59+00:00"
        self.assertEqual(ManualReviewStatus([review], today="2026-10-25").for_video(video)["key"], "pending")

    def test_legacy_completed_script_is_unclassified_only_in_its_own_product_and_current_window(self):
        script = {"product": "202H", "video_id": self.video["video_id"], "status": "reviewed",
                  "history": [{"status": "reviewed", "published_on": self.video["posted_on"]}]}
        snapshot = ManualReviewStatus([], [script])
        self.assertEqual(snapshot.for_video(self.video)["key"], "unclassified")
        self.assertEqual(snapshot.for_video(self.video, allow_legacy=False)["key"], "pending")
        self.assertEqual(snapshot.for_video({**self.video, "product": "129N"})["key"], "pending")

    def test_registered_publication_date_controls_the_fourteen_day_period(self):
        scripts = [{"product": "202H", "video_id": self.video["video_id"], "published_on": "2026-10-20"}]
        snapshot = ManualReviewStatus([self.record], scripts, today="2026-10-25")
        self.assertEqual(snapshot.for_video(self.video)["key"], "pending")
        self.assertEqual(snapshot.for_video({**self.video, "registered_on": "2026-05-26"})["key"], "unclassified")
        receipt = {"post_id": self.video["video_id"], "published_on": "2026-05-26", "source": {"script": {"product": "202H"}}}
        snapshot = ManualReviewStatus([self.record], scripts, today="2026-10-25", publications=[{"publication": receipt}])
        self.assertEqual(snapshot.for_video(self.video)["key"], "unclassified")


class ManualReviewStatusHTTPTests(unittest.TestCase):
    setUp = fixture.FeedbackChainHTTPTests.setUp
    stop_server = fixture.FeedbackChainHTTPTests.stop_server
    request = fixture.FeedbackChainHTTPTests.request
    no_download = staticmethod(fixture.FeedbackChainHTTPTests.no_download)
    ok = fixture.FeedbackChainHTTPTests.ok
    chain = fixture.FeedbackChainHTTPTests.chain
    review = fixture.FeedbackChainHTTPTests.review

    def statuses(self):
        full = next(row for row in self.ok("GET", "/api/performance/videos")["videos"] if row["video_id"] == self.video_id)
        home = next(row for row in self.ok("GET", "/api/performance/videos?projection=home&product=202H")["videos"] if row["video_id"] == self.video_id)
        detail = self.ok("GET", "/api/performance/videos/" + self.video_id)["video"]
        chain = self.chain()
        return [row["review_status"] for row in (full, home, detail, chain, chain["summary"])]

    def classify(self, value):
        return self.ok("POST", "/api/review-followups", {"request_id": uuid4().hex,
            "revision": self.chain()["review_revision"], "product": "202H", "video_id": self.video_id,
            "window_end": self.window, "classification_only": True, "reference_value": value})

    def test_lists_detail_chain_and_case_snapshot_change_without_sales_resync(self):
        app = self.server.app
        original_video = copy.deepcopy(app.performance.video(self.video_id))
        self.assertTrue(all(row == {"key": "pending", "label": "待复盘"} for row in self.statuses()))
        self.review(decision="stop")
        self.assertTrue(all(row["key"] == "unclassified" for row in self.statuses()))
        before_state = app.state()
        before_source = self.chain()["source_review"]
        for value in ("none", "weak", "medium", "high", ""):
            self.classify(value)
            expected = value or "unclassified"
            self.assertTrue(all(row == {"key": expected, "label": LABELS[expected]} for row in self.statuses()))
            self.assertEqual(app.detail(self.identifier)["sales_data"]["video"]["review_status"]["key"], expected)
            self.assertEqual(app.state()["cases"][0]["sales_data"]["video"]["review_status"]["key"], expected)
        self.assertNotEqual(app.state()["signature"], before_state["signature"])
        self.assertNotEqual(app.state()["digest"], before_state["digest"])
        self.assertEqual(self.chain()["source_review"], before_source)
        self.assertEqual(app.performance.video(self.video_id), original_video)

    def test_historical_window_cannot_borrow_current_or_other_window_classification(self):
        app = self.server.app
        old_window = "2026-05-31"
        seed_legacy_followup(app.review_followups, {"product": "202H", "video_id": self.video_id, "window_end": old_window}, reference_value="high")
        self.assertTrue(all(row["key"] == "pending" for row in self.statuses()))
        historical = self.chain(window_end=old_window)
        self.assertEqual(historical["review_status"], {"key": "high", "label": "高参考价值"})
        self.assertEqual(historical["summary"]["review_status"], historical["review_status"])
        self.review(decision="stop", reference_value="weak")
        self.assertEqual(self.chain(window_end=old_window)["review_status"]["key"], "high")
        self.assertEqual(self.chain(window_end="2026-05-24")["review_status"]["key"], "pending")

    def test_failed_review_catalog_is_not_silently_presented_as_no_review(self):
        with patch.object(self.server.app.review_followups, "read", side_effect=CreationError(500, "复盘暂不可读")):
            for path in ("/api/performance/videos", "/api/performance/videos?projection=home", "/api/performance/videos/" + self.video_id):
                with self.subTest(path=path):
                    self.assertEqual(self.request("GET", path)[0], 500)

    def test_state_digest_changes_even_when_review_video_has_no_case(self):
        app = self.server.app
        before = app.state()
        seed_legacy_followup(app.review_followups, {"product": "202H", "video_id": "7000000000000000001", "window_end": self.window}, reference_value="weak")
        after = app.state()
        self.assertNotEqual(before["signature"], after["signature"])
        self.assertNotEqual(before["digest"], after["digest"])

    def test_registration_correction_keeps_all_live_status_surfaces_consistent(self):
        self.review(decision="stop", reference_value="weak")
        receipt = {"post_id": self.video_id, "published_on": (review_today() - timedelta(days=3)).isoformat(),
                   "source": {"script": {"product": "202H"}}}
        with patch.object(VideoPublicationStore, "read", return_value={"jobs": {"test": {"publication": receipt}}}):
            self.assertTrue(all(row["key"] == "pending" for row in self.statuses()))
            self.assertEqual(self.server.app.detail(self.identifier)["sales_data"]["video"]["review_status"]["key"], "pending")


if __name__ == "__main__":
    unittest.main()
