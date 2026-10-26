"""Home keeps follow-up semantics while omitting unrelated full-list payloads."""
import copy
import unittest
from unittest.mock import patch

import test_performance as performance


class HomeVideoLoadingTests(unittest.TestCase):
    setUp = performance.PerformanceHTTPTests.setUp
    setUp_ = performance.PerformanceHTTPTests.setUp_
    stop_server = performance.PerformanceHTTPTests.stop_server
    request = performance.PerformanceHTTPTests.request

    def test_home_keeps_all_window_and_followup_fields_without_other_catalogs(self):
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        full = self.request("GET", "/api/performance/videos")[2]["videos"]
        app = self.server.app
        with patch.object(app, "video_case_links", side_effect=AssertionError("unneeded case mapping")), \
                patch.object(app.reviews, "video_counts", side_effect=AssertionError("unneeded comment aggregation")):
            status, _, data = self.request("GET", "/api/performance/videos?projection=home")
        self.assertEqual(status, 200)
        self.assertEqual(data["published_video_ids"], [row["video_id"] for row in full])
        fields = {"video_id", "product", "title", "headline", "account", "posted_on", "window_end",
                  "status", "status_label", "flags", "reference", "missing_window", "views", "window_views", "orders",
                  "last_data", "observation_coverage_complete", "observation_missing_days", "review_status"}
        self.assertEqual([{key: value for key, value in row.items() if key in fields} for row in data["videos"]],
                         [{key: value for key, value in row.items() if key in fields} for row in full])
        self.assertTrue(all("review_context" in row for row in data["videos"]))
        # A later visit to Video Data must still receive its full figures and case links.
        self.assertEqual(self.request("GET", "/api/performance/videos")[2]["videos"], full)

    def test_product_scope_matches_home_and_never_truncates_or_loses_publication_ids(self):
        rows = [{"video_id": str(7000000000000000000 + index), "product": product,
                 "title": f"Video {index}", "window_end": "2026-09-13", "status": "incomplete"}
                for index, product in enumerate(["SHARPAL 202H"] * 350 + ["129N", "Custom Product", "", "202H + 129N"])]
        before = copy.deepcopy(rows)
        with patch.object(self.server.app.performance, "videos", return_value=rows):
            for selected in ("202H", "sharpal%20202h"):
                status, _, data = self.request("GET", "/api/performance/videos?projection=home&product=" + selected)
                self.assertEqual(status, 200)
                self.assertEqual(len(data["videos"]), 350)
                self.assertEqual(data["published_video_ids"], [row["video_id"] for row in rows])
            for suffix in ("", "&product=", "&product=all"):
                self.assertEqual(len(self.request("GET", "/api/performance/videos?projection=home" + suffix)[2]["videos"]), len(rows))
            self.assertEqual(len(self.request("GET", "/api/performance/videos?projection=home&product=custom%20product")[2]["videos"]), 1)
            self.assertEqual(self.request("GET", "/api/performance/videos?projection=home&product=unknown")[2]["videos"], [])
        self.assertEqual(rows, before)

    def test_ambiguous_projection_and_product_are_rejected(self):
        for query in ("projection=other", "projection=home&projection=full", "projection=home&product=202H&product=129N"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/performance/videos?" + query)[0], 400)


if __name__ == "__main__":
    unittest.main()
