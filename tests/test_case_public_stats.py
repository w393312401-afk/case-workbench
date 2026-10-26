"""Case-only public observations stay visible without inventing sales data."""
from __future__ import annotations

import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import test_performance as exports
import test_sales_data as sales_fixture
import test_server as http
from performance_store import PerformanceStore
from public_stats import PublicStatsStore
from sales_data import SalesDataResolver


UNKNOWN = "7999999999999999999"
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc)
FIELDS = ("views", "likes", "comments", "shares", "saves", "observed_at", "last_seen_at")


def pushed(video_id, **extra):
    return {"id": video_id, "views": 46300, "likes": 274, "comments": 0,
            "shares": 37, "saves": 102, "observedAt": NOW.timestamp() * 1000, **extra}


class CasePublicStatsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="case-public-stats-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        exports.build_exports(self.root / "exports")
        self.performance = PerformanceStore(self.root / "runtime", [self.root / "exports"])
        self.performance.sync()
        self.public = self.performance.public = PublicStatsStore(self.root / "runtime")
        self.resolver = SalesDataResolver(self.performance)

    def save_public(self, *records):
        self.public.record(list(records), lambda ids: set(ids), now=NOW)

    def test_matched_and_case_only_share_exact_public_snapshot_without_sales_fallback(self):
        cases = [sales_fixture.case(), sales_fixture.case(UNKNOWN, "PVR-000000000002")]
        originals = copy.deepcopy(cases)
        before = self.resolver.resolve_cases(cases)
        self.save_public(pushed(exports.LINKED), pushed(UNKNOWN, views=699800, likes=1527))
        with patch.object(self.public, "latest", wraps=self.public.latest) as reads:
            result = self.resolver.resolve_cases(cases)
        # One batch inside video_brief, then one batch for IDs without sales rows.
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(set(reads.call_args_list[-1].args[0]), {UNKNOWN})
        current = self.public.latest([exports.LINKED, UNKNOWN])
        for case in cases:
            video_id = case["current"]["metrics"]["video_id"]
            actual = result[case["case_id"]]
            self.assertEqual(actual["public_stats"], {field: current[video_id].get(field) for field in FIELDS})
            self.assertEqual(actual["status"], before[case["case_id"]]["status"])
            self.assertEqual(actual["evidence"], before[case["case_id"]]["evidence"])
        only = result["PVR-000000000002"]
        self.assertEqual(only["status"], "not_found")
        self.assertIsNone(only["video"])
        self.assertEqual(only["metrics"], {"video_id": UNKNOWN})
        self.assertNotIn("window", only["evidence"])
        self.assertEqual(cases, originals)

    def test_missing_sales_database_does_not_hide_public_counts_or_create_sales(self):
        fresh = PerformanceStore(self.root / "never-imported")
        fresh.public = PublicStatsStore(self.root / "never-imported")
        fresh.public.record([pushed(UNKNOWN)], lambda ids: set(ids), now=NOW)
        result = SalesDataResolver(fresh).resolve_case(sales_fixture.case(UNKNOWN))
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["metrics"], {"video_id": UNKNOWN})
        self.assertEqual(result["public_stats"]["likes"], 274)
        self.assertIsNone(result["video"])
        self.assertFalse(fresh.database.exists())

    def test_zero_null_and_missing_snapshot_are_distinct_and_results_are_independent(self):
        self.save_public(pushed(UNKNOWN, views=0, likes=0, comments=None, shares=0, saves=None))
        cases = [sales_fixture.case(UNKNOWN), sales_fixture.case("", "PVR-000000000002"),
                 sales_fixture.case(exports.LINKED, "PVR-000000000003")]
        result = self.resolver.resolve_cases(cases)
        snapshot = result[cases[0]["case_id"]]["public_stats"]
        self.assertEqual([snapshot[key] for key in FIELDS[:5]], [0, 0, None, 0, None])
        self.assertNotIn("public_stats", result[cases[1]["case_id"]])
        self.assertNotIn("public_stats", result[cases[2]["case_id"]])
        snapshot["likes"] = 999
        self.assertEqual(self.resolver.resolve_case(cases[0])["public_stats"]["likes"], 0)

    def test_unreadable_public_store_keeps_sales_availability_and_does_not_fabricate_counts(self):
        cases = [sales_fixture.case(), sales_fixture.case(UNKNOWN, "PVR-000000000002")]
        with patch.object(self.public, "latest", side_effect=sqlite3.DatabaseError("unavailable")):
            result = self.resolver.resolve_cases(cases)
        self.assertEqual(result[cases[0]["case_id"]]["status"], "matched")
        self.assertEqual(result[cases[0]["case_id"]]["metrics"]["views_display"], "12000")
        self.assertEqual(result[cases[1]["case_id"]]["status"], "not_found")
        self.assertTrue(all("public_stats" not in row for row in result.values()))


class CasePublicStatsHTTPTests(unittest.TestCase):
    setUp = http.WorkbenchHTTPTests.setUp
    stop_server = http.WorkbenchHTTPTests.stop_server
    request = http.WorkbenchHTTPTests.request
    detail = http.WorkbenchHTTPTests.detail

    def test_case_only_push_reaches_state_and_detail_without_changing_case_history(self):
        old_case = self.store.read_case(self.identifier)
        before = self.detail()["sales_data"]
        result = self.server.app.push_public_stats({"videos": [pushed(exports.LINKED)]})
        self.assertEqual(result["saved"], 1)
        detail = self.detail()
        state = self.request("GET", "/api/state")[2]
        summary = next(item for item in state["cases"] if item["case_id"] == self.identifier)
        self.assertEqual(summary["sales_data"], detail["sales_data"])
        self.assertEqual(detail["sales_data"]["public_stats"]["saves"], 102)
        self.assertEqual({key: detail["sales_data"][key] for key in ("status", "metrics", "evidence", "video")},
                         {key: before[key] for key in ("status", "metrics", "evidence", "video")})
        self.assertEqual(self.store.read_case(self.identifier), old_case)


if __name__ == "__main__":
    unittest.main()
