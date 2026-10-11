"""Sales result/facts caching with real SQLite transactions and isolated exports."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

import performance_store as module
from performance_store import PerformanceStore
import test_performance as exports

LINKED = exports.LINKED


class PerformanceCacheTests(unittest.TestCase):
    def setUp(self):
        exports.PerformanceStoreTests.setUp(self)
        self.store.sync()

    def all_results(self):
        return {"summary": self.store.summary(), "analytics": self.store.analytics(),
                "videos": self.store.videos(), "brief": self.store.video_brief([LINKED]),
                "video": self.store.video(LINKED)}

    def test_warm_reads_reuse_normalization_and_isolate_all_mutable_results(self):
        with patch.object(self.store, "_materialize_facts", wraps=self.store._materialize_facts) as normalize:
            expected = self.all_results()
            mutated = self.all_results()
            mutated["summary"]["weeks"][0]["gmv"] = -100
            mutated["summary"]["coverage"]["gaps"].append(["fake", "fake"])
            mutated["analytics"]["videos"][0][0] = "fake"
            mutated["analytics"]["weekly"][0][2] = -100
            mutated["analytics"]["monthly"][0][2] = -100
            mutated["analytics"]["shop_daily"][0]["gmv"] = -100
            mutated["videos"][0]["views"] = -100
            mutated["brief"][LINKED]["metric_coverage"]["orders"]["total_rows"] = -100
            mutated["brief"][LINKED]["coverage"]["gaps"].append("fake")
            mutated["video"]["series"][0]["views"] = -100
            self.assertEqual(self.all_results(), expected)
            self.assertEqual(normalize.call_count, 1)

    def test_case_links_and_source_configuration_refresh_without_new_sales_computation(self):
        self.all_results()
        with patch.object(self.store, "_read_videos", side_effect=AssertionError("unexpected query")), \
             patch.object(self.store, "_read_summary", side_effect=AssertionError("unexpected query")):
            first = self.store.videos({LINKED: "case-one"})
            second = self.store.videos({LINKED: "case-two"})
            self.assertEqual(next(row for row in first if row["video_id"] == LINKED)["case_id"], "case-one")
            self.assertEqual(next(row for row in second if row["video_id"] == LINKED)["case_id"], "case-two")
            self.assertEqual(self.store.video(LINKED, {LINKED: "case-three"})["case_id"], "case-three")
            self.assertIsNone(self.store.video(LINKED)["case_id"])
            self.store.sources = [Path("another source.xlsx")]
            self.assertEqual(self.store.summary()["sources"], ["another source.xlsx"])

    def test_external_wal_commit_invalidates_every_result_with_unchanged_main_file(self):
        writer = sqlite3.connect(self.store.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        before = self.all_results()
        main_stamp = self.store.database.stat().st_mtime_ns
        writer.execute("UPDATE periods SET views=views+1000, gmv=gmv+25 WHERE video_id=?", (LINKED,))
        writer.commit()
        self.assertEqual(self.store.database.stat().st_mtime_ns, main_stamp)
        after = self.all_results()
        for key in before:
            self.assertNotEqual(after[key], before[key], key)
        self.assertEqual(after["brief"][LINKED]["views"], 14000)
        self.assertEqual(after["video"]["gmv"], sum(row["gmv"] for row in after["video"]["series"]))

    def test_replacement_deletion_and_failed_queries_never_return_old_cached_data(self):
        before = self.all_results()
        original = self.store.database.stat()
        replacement = self.store.database.with_name("replacement.sqlite")
        reader = sqlite3.connect(self.store.database)
        target = sqlite3.connect(replacement)
        reader.backup(target)
        target.execute("UPDATE periods SET views=5 WHERE video_id=?", (LINKED,))
        target.commit()
        target.close()
        reader.close()
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, self.store.database)
        self.assertNotEqual(self.all_results()["brief"], before["brief"])
        self.assertEqual(self.store.video_brief([LINKED])[LINKED]["views"], 10)
        self.store.database.unlink()
        self.assertEqual(self.store.video_brief([LINKED]), {})
        self.assertFalse(self.store.database.exists())
        self.assertEqual(self.store.videos(), [])
        self.assertEqual(self.store.analytics()["weekly"], [])
        self.assertIsNone(self.store.summary()["coverage"]["first"])
        self.store.database.write_bytes(b"invalid SQLite data")
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.summary()

    def test_successful_imports_invalidate_but_excel_edits_wait_for_explicit_sync(self):
        before = self.all_results()
        path = self.folder / "9月" / "第二周视频.xlsx"
        exports.export(path, "2026-09-07", "2026-09-13", [exports.row(LINKED, 6000, 1, 100, 150)])
        self.assertEqual(self.all_results(), before)
        self.store.sync()
        self.assertEqual(self.store.video_brief([LINKED])[LINKED]["views"], 14000)
        self.assertNotEqual(self.store.analytics(), before["analytics"])
        summary = self.store.summary()
        # Totals-only and monthly-only imports must invalidate their related summaries.
        totals = self.folder / "totals-only.xlsx"
        exports.export(totals, "2026-10-01", "2026-10-01", [["2026-10-01", "200"]], exports.TOTALS_HEADER[:2])
        self.store.import_paths([totals])
        self.assertNotEqual(self.store.summary(), summary)
        self.assertEqual(self.store.analytics()["shop_daily"][-1]["gmv"], 200)
        monthly = self.folder / "monthly-only.xlsx"
        exports.export(monthly, "2026-09-01", "2026-09-30", [exports.row(LINKED, 90000, 30, 3000, 4500)])
        self.store.import_paths([monthly])
        self.assertEqual(self.store.analytics()["months"]["2026-09"], "月报表")

    def test_concurrent_cache_misses_share_one_query_and_one_facts_build(self):
        barrier = threading.Barrier(8)
        def read():
            barrier.wait(timeout=5)
            return self.store.summary()
        with patch.object(self.store, "_read_summary", wraps=self.store._read_summary) as load, \
             patch.object(self.store, "_materialize_facts", wraps=self.store._materialize_facts) as normalize:
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = [future.result(timeout=10) for future in [pool.submit(read) for _ in range(8)]]
            self.assertTrue(all(value == results[0] for value in results))
            self.assertEqual(load.call_count, 1)
            self.assertEqual(normalize.call_count, 1)
            self.assertEqual(len({id(value) for value in results}), 8)

    def test_external_commit_during_read_preserves_snapshot_and_never_publishes_old_cache(self):
        writer = sqlite3.connect(self.store.database)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        original = self.store._materialize_facts
        changed = False
        def normalize(connection):
            nonlocal changed
            excluded = original(connection)
            if not changed:
                writer.execute("UPDATE periods SET gmv=gmv+25 WHERE video_id=?", (LINKED,))
                writer.commit()
                changed = True
            return excluded
        with patch.object(self.store, "_materialize_facts", side_effect=normalize):
            old_snapshot = self.store.video(LINKED)
        self.assertEqual(old_snapshot["gmv"], 600)
        self.assertEqual(sum(row["gmv"] for row in old_snapshot["series"]), 600)
        latest = self.store.video(LINKED)
        self.assertEqual(latest["gmv"], 650)
        self.assertEqual(sum(row["gmv"] for row in latest["series"]), 650)
        self.assertEqual(self.store.video(LINKED), latest)

    def test_facts_reuse_preserves_overlap_latest_attributes_and_incomplete_coverage(self):
        exports.export(self.folder / "shifted-week.xlsx", "2026-09-03", "2026-09-09", [exports.row(LINKED, 777, 1, 10)])
        self.store.sync()
        connection = self.store.connect()
        connection.execute("UPDATE periods SET orders=NULL, account='latest-account' WHERE video_id=? AND range_start='2026-09-07'", (LINKED,))
        connection.close()
        self.store.summary()  # Warm facts through another aggregate.
        brief = self.store.video_brief([LINKED])[LINKED]
        self.assertEqual(brief["views"], 12000)
        self.assertEqual(brief["account"], "latest-account")
        self.assertEqual(len(brief["excluded_overlapping_periods"]), 1)
        self.assertEqual(brief["metric_coverage"]["orders"], {"provided_rows": 1, "total_rows": 2})
        self.assertIsNone(brief["ctor_pct"])
        self.assertEqual(brief["coverage"]["covered_days"], 14)
        brief["excluded_overlapping_periods"][0]["start"] = "mutated"
        self.assertEqual(self.store.video_brief([LINKED])[LINKED]["excluded_overlapping_periods"][0]["start"], "2026-09-03")
        detail = self.store.video(LINKED)
        self.assertEqual(detail["views"], brief["views"])
        self.assertIsNone(detail["ctor_pct"])
        self.assertEqual(sum(row["views"] for row in detail["series"]), brief["views"])

    def test_cache_has_byte_and_entry_limits_and_eviction_does_not_change_results(self):
        expected = self.store.video_brief([LINKED])
        with patch.object(module, "MAX_CACHE_BYTES", 12000), patch.object(module, "MAX_CACHE_ENTRIES", 4):
            for index in range(12):
                self.store.video_brief([LINKED, f"missing-{index}"])
                self.assertLessEqual(self.store._cache_bytes, 12000)
                self.assertLessEqual(len(self.store._cache), 4)
            self.assertEqual(self.store.video_brief([LINKED]), expected)
            self.assertNotIn("facts", self.store._cache)


if __name__ == "__main__":
    unittest.main()
