"""Observation ratings require the video's own usable data; SQLite files are disposable."""
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest

from performance_store import PerformanceStore


class PerformanceCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="performance-coverage-")
        self.addCleanup(self.temp.cleanup)
        self.store = PerformanceStore(Path(self.temp.name))

    def daily(self, video, days, *, account="account-a", posted="2026-08-31", views=100):
        with closing(self.store.connect()) as connection:
            connection.executemany("""INSERT OR REPLACE INTO daily
                (video_id, date, account, title, posted_at, products, views, source_file)
                VALUES (?, ?, ?, 'Fixture', ?, '202H', ?, 'isolated fixture')""",
                [(video, day, account, posted, views) for day in days])

    def week(self, video, start, end, views, *, account="account-a", posted="2026-08-31"):
        with closing(self.store.connect()) as connection:
            connection.execute("""INSERT INTO periods
                (video_id, range_start, range_end, span_days, account, title, posted_at, products, views, source_file)
                VALUES (?, ?, ?, 7, ?, 'Fixture', ?, '202H', ?, 'isolated fixture')""",
                (video, start, end, account, posted, views))

    @staticmethod
    def days(start="2026-08-31", count=14):
        first = date.fromisoformat(start)
        return [(first + timedelta(days=i)).isoformat() for i in range(count)]

    def videos(self):
        return {row["video_id"]: row for row in self.store.videos()}

    def test_other_accounts_and_same_account_videos_cannot_fill_missing_days(self):
        self.daily("complete", self.days())
        self.daily("other-account", ["2026-08-31"], account="account-b", views=50)
        self.daily("same-account", ["2026-08-31"], views=50)
        videos = self.videos()
        self.assertEqual(videos["complete"]["status"], "watch")
        for video in ("other-account", "same-account"):
            with self.subTest(video=video):
                self.assertEqual(videos[video]["last_data"], "2026-08-31")
                self.assertEqual(videos[video]["window_views"], 50)
                self.assertEqual(videos[video]["status"], "incomplete")
                self.assertNotIn("window_top_pct", self.store.video(video))
        self.assertEqual(self.store.video("complete")["judged_videos"], 1)

    def test_internal_gap_is_incomplete_and_supplement_invalidates_cached_rating(self):
        days = self.days()
        self.daily("complete", days)
        self.daily("gap", [day for day in days if day != "2026-09-04"])
        self.assertEqual(self.videos()["gap"]["status"], "incomplete")
        self.daily("gap", ["2026-09-04"])
        self.assertEqual(self.videos()["gap"]["status"], "watch")
        self.assertEqual(self.store.video("gap")["window_views"], 1400)

    def test_window_not_yet_reached_stays_pending_then_missing_data_is_incomplete(self):
        self.daily("target", self.days(count=7))
        self.assertEqual(self.videos()["target"]["status"], "pending")
        self.daily("later-other-account", ["2026-09-13"], account="account-b")
        self.assertEqual(self.videos()["target"]["status"], "incomplete")
        self.daily("target", self.days("2026-09-07", 7))
        self.assertEqual(self.videos()["target"]["status"], "watch")

    def test_missing_view_metric_is_not_a_zero_but_explicit_zero_is_complete(self):
        self.daily("complete", self.days())
        self.daily("missing-one", self.days())
        self.daily("missing-one", ["2026-09-04"], views=None)
        self.daily("missing-all", self.days(), views=None)
        self.daily("zero", self.days(), views=0)
        videos = self.videos()
        self.assertEqual(videos["missing-one"]["status"], "incomplete")
        self.assertEqual(videos["missing-one"]["window_views"], 1300)
        self.assertEqual(videos["missing-all"]["status"], "incomplete")
        self.assertIsNone(videos["missing-all"]["window_views"])
        self.assertEqual((videos["zero"]["status"], videos["zero"]["window_views"]), ("drop", 0))

    def test_weekly_precedence_and_missing_weekly_views_do_not_borrow_daily_counts(self):
        self.daily("target", self.days(), views=999999)
        self.week("target", "2026-08-31", "2026-09-06", 8000)
        self.week("target", "2026-09-07", "2026-09-13", None)
        self.assertEqual(self.videos()["target"]["status"], "incomplete")
        self.assertEqual(self.videos()["target"]["window_views"], 8000)
        with closing(self.store.connect()) as connection:
            connection.execute("UPDATE periods SET views=4000 WHERE video_id='target' AND range_start='2026-09-07'")
        target = self.videos()["target"]
        self.assertEqual((target["status"], target["window_views"], target["views"]), ("focus", 12000, 12000))

    def test_whole_week_crossing_window_end_cannot_claim_complete_observation(self):
        self.daily("target", self.days(count=10))
        self.week("target", "2026-09-10", "2026-09-16", 10000)
        target = self.videos()["target"]
        self.assertEqual(target["window_end"], "2026-09-13")
        self.assertEqual(target["window_views"], 1000)
        self.assertEqual(target["status"], "incomplete")

    def test_brief_and_full_video_use_the_same_review_window_identity(self):
        self.daily("target", ["2026-08-31"], posted="2026-08-31")
        self.daily("target", ["2026-09-14"], posted="2026-09-07")
        full = self.videos()["target"]
        brief = self.store.video_brief(["target"])["target"]
        self.assertEqual((brief["posted_on"], brief["window_end"]), ("2026-08-31", "2026-09-13"))
        self.assertEqual((brief["posted_on"], brief["window_end"]), (full["posted_on"], full["window_end"]))


if __name__ == "__main__":
    unittest.main()
