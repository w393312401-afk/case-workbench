"""First-14-day review readiness never changes the legacy report figures."""
from contextlib import closing
from datetime import date
import unittest
from unittest.mock import patch
from uuid import uuid4

from review_policy import review_context, report_window
from workbench_feedback import review_draft
import test_server as fixture
from test_performance_coverage import PerformanceCoverageTests


class ReviewPolicyTests(unittest.TestCase):
    def test_fourteen_days_are_independent_of_calendar_week(self):
        video = {"posted_on": "2026-10-18", "window_end": "2026-10-25", "window_views": 20000,
                 "orders": 3, "observation_coverage_complete": True, "last_data": "2026-11-01"}
        early = review_context(video, "2026-10-31")
        self.assertEqual(early["observation"]["ends_on"], "2026-11-01")
        self.assertEqual(early["observation"]["age_days"], 13)
        self.assertFalse(early["closing_allowed"])
        mature = review_context(video, "2026-11-01")
        self.assertTrue(mature["closing_allowed"])
        self.assertEqual(mature["evidence"]["key"], "preliminary")
        self.assertNotIn("成功", mature["performance"]["traffic"])
        self.assertEqual(report_window(video["posted_on"]), "2026-10-25")

    def test_missing_dates_coverage_and_future_dates_never_close(self):
        for video in ({}, {"posted_on": "bad"}, {"posted_on": "2026-10-26"},
                      {"posted_on": "2026-10-01", "last_data": "2026-10-25"},
                      {"posted_on": "2026-10-01", "registered_on": "2026-10-02", "observation_coverage_complete": True}):
            with self.subTest(video=video):
                context = review_context(video, "2026-10-25")
                self.assertFalse(context["closing_allowed"])
                self.assertEqual(context["evidence"]["key"], "missing")

    def test_no_orders_is_unknown_until_supplied_and_low_views_are_not_failure(self):
        video = {"posted_on": "2026-10-01", "window_views": 100, "observation_coverage_complete": True}
        context = review_context(video, "2026-10-25")
        self.assertEqual(context["performance"]["sales"], "成交数据待补")
        self.assertEqual(context["evidence"]["key"], "insufficient")
        video["orders"] = 0
        self.assertEqual(review_context(video, "2026-10-25")["performance"]["sales"], "覆盖期内暂无成交")

    def test_historical_ai_actions_do_not_reactivate_removed_controls(self):
        for label in ("继续观察", "本轮暂无发现", "无结论"):
            self.assertEqual(review_draft({"rounds": [{"status": "completed", "output": f"结论：待验证\n建议动作：{label}"}]})["action"], "")


class FourteenDayCoverageTests(unittest.TestCase):
    setUp = PerformanceCoverageTests.setUp
    daily = PerformanceCoverageTests.daily
    week = PerformanceCoverageTests.week
    days = staticmethod(PerformanceCoverageTests.days)
    videos = PerformanceCoverageTests.videos

    def orders(self, video, value=0):
        with closing(self.store.connect()) as connection:
            connection.execute("UPDATE daily SET orders=? WHERE video_id=?", (value, video))
            connection.execute("UPDATE periods SET orders=? WHERE video_id=?", (value, video))

    def test_calendar_window_can_be_complete_before_first14_coverage(self):
        self.daily("sunday", self.days("2026-09-06", 8), posted="2026-09-06")
        self.orders("sunday")
        row = self.videos()["sunday"]
        self.assertEqual(row["window_end"], "2026-09-13")
        self.assertEqual(row["status"], "drop")
        self.assertFalse(row["observation_coverage_complete"])
        self.assertEqual(row["observation_missing_days"], ["2026-09-14", "2026-09-19", 6])
        self.daily("sunday", self.days("2026-09-14", 6), posted="2026-09-06")
        self.orders("sunday")
        self.assertTrue(self.videos()["sunday"]["observation_coverage_complete"])
        self.assertEqual(self.videos()["sunday"]["window_views"], 800)

    def test_other_video_and_null_order_cannot_supply_missing_data(self):
        self.daily("target", self.days())
        self.daily("other", self.days())
        self.orders("other")
        self.assertFalse(self.videos()["target"]["observation_coverage_complete"])
        self.orders("target")
        with closing(self.store.connect()) as connection:
            connection.execute("UPDATE daily SET orders=NULL WHERE video_id='target' AND date='2026-09-05'")
        self.assertEqual(self.videos()["target"]["observation_missing_days"], ["2026-09-05", "2026-09-05", 1])

    def test_crossing_week_proves_availability_without_inventing_daily_totals(self):
        self.week("target", "2026-08-31", "2026-09-06", 1000, posted="2026-09-02")
        self.week("target", "2026-09-07", "2026-09-13", 2000, posted="2026-09-02")
        self.week("target", "2026-09-14", "2026-09-20", 7000, posted="2026-09-02")
        self.orders("target")
        row = self.videos()["target"]
        self.assertTrue(row["observation_coverage_complete"])
        self.assertEqual(row["window_views"], 3000)
        self.assertEqual(row["views"], 10000)


class RegisteredReviewHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_registered_post_is_observable_before_first_sales_report(self):
        app = self.server.app
        catalog = app.creation.read()
        video_id = "7690000000000000001"
        scripts = [{"id": "SCR-111111111111", "product": "202H", "title": "New published video",
                    "video_id": video_id, "published_on": "2026-10-24"}]
        with patch.object(app.creation, "read", return_value={**catalog, "scripts": scripts}), \
                patch("review_policy.review_today", return_value=date(2026, 10, 25)):
            status, _, home = self.request("GET", "/api/performance/videos?projection=home&product=202H")
            self.assertEqual(status, 200)
            row = next(row for row in home["videos"] if row["video_id"] == video_id)
            self.assertTrue(row["registration_only"])
            self.assertIsNone(row["orders"])
            self.assertEqual(row["review_context"]["evidence"]["key"], "observing")
            status, _, chain = self.request("GET", f"/api/feedback-chain?product=202H&video_id={video_id}")
            self.assertEqual(status, 200, chain)
            self.assertFalse(chain["review_context"]["closing_allowed"])
            body = {"request_id": uuid4().hex, "revision": chain["review_revision"], "product": "202H",
                    "video_id": video_id, "window_end": row["window_end"], "state": "reviewed", "decision": "stop", "note": "提前下阶段结论"}
            self.assertIn("可再改", chain["review_context"]["early_warning"])
            status, _, saved = self.request("POST", "/api/review-followups", {**body, "request_id": uuid4().hex,
                "state": "observing", "decision": "observe", "next_review_on": "2999-01-01", "observation_reason": "等待首14天销售数据"})
            self.assertEqual(status, 400, saved)
            self.assertEqual(app.review_followups.public()["entries"], [])
            status, _, saved = self.request("POST", "/api/review-followups", body)
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["entry"]["note"], "提前下阶段结论")
            self.assertTrue(row["review_context"]["observation"]["observing"])
            self.assertEqual(app.creation.read()["plans"], catalog["plans"])


if __name__ == "__main__":
    unittest.main()
