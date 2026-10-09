"""Current sales read paths and frozen AI contexts; all data is isolated and AI is fake."""
from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import test_ai_tasks as tasks
import test_performance as exports
import test_server as http
from performance_store import PerformanceStore
from sales_data import SalesDataResolver, prompt_sales_projection


LINKED = exports.LINKED
DIRECTION = {"direction_id": "202H-kit", "main_difference": "完整系统", "concern": "步骤是否清楚",
             "closing": "按步骤选择", "variant_type": "direction"}


def case(video_id=LINKED, case_id="PVR-000000000001"):
    return {"case_id": case_id, "current": {"metrics": {"video_id": video_id, "views_display": "999.99K",
            "items_sold": 999, "ctor_display": "99%"}, "evidence": {"source": "旧截图", "window": "旧窗口"}}}


class SalesDataResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sales-resolver-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / "exports"
        exports.build_exports(self.folder)
        self.performance = PerformanceStore(self.root / "runtime", [self.folder])
        self.performance.sync()
        self.resolver = SalesDataResolver(self.performance)

    def test_exact_id_sales_priority_identity_and_no_legacy_fallback(self):
        source = case()
        before = copy.deepcopy(source)
        with patch.object(self.performance, "video_brief", wraps=self.performance.video_brief) as query:
            resolved = self.resolver.resolve_cases([source, case("7999999999999999999", "PVR-000000000002"),
                                                    case("", "PVR-000000000003")])
        self.assertEqual(query.call_count, 1)
        matched = resolved[source["case_id"]]
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(matched["metrics"]["views_display"], "12000")
        self.assertEqual(matched["metrics"]["creator_handle"], "@bushcraftedge")
        self.assertEqual(matched["evidence"]["as_of"], "2026-09-13")
        self.assertNotIn("旧", matched["evidence"]["source"])
        self.assertEqual(resolved["PVR-000000000002"]["status"], "not_found")
        self.assertEqual(resolved["PVR-000000000003"]["status"], "missing_video_id")
        for identifier in ("PVR-000000000002", "PVR-000000000003"):
            self.assertNotIn("views_display", resolved[identifier]["metrics"])
            self.assertNotIn("window", resolved[identifier]["evidence"])
        self.assertEqual(source, before)
        # Same headline/account does not bind a missing or incorrect ID.
        source["current"]["metrics"].update(video_id="unknown", published_title=matched["metrics"]["published_title"])
        self.assertEqual(self.resolver.resolve_case(source)["status"], "not_found")

    def test_unavailable_store_no_creation_or_numeric_fallback(self):
        fresh = PerformanceStore(self.root / "never-imported")
        resolved = SalesDataResolver(fresh).resolve_case(case())
        self.assertEqual(resolved["status"], "unavailable")
        self.assertEqual(resolved["metrics"], {"video_id": LINKED})
        self.assertFalse(fresh.database.exists())
        with patch.object(self.performance, "video_brief", side_effect=sqlite3.DatabaseError("unreadable")):
            self.assertEqual(self.resolver.resolve_case(case())["status"], "unavailable")

    def test_true_zero_nullable_fields_and_ctor_orders_not_items(self):
        connection = self.performance.connect()
        connection.execute("UPDATE periods SET orders=0, items=22 WHERE video_id=?", (LINKED,))
        connection.execute("UPDATE periods SET views=NULL, impressions=NULL WHERE video_id=?", (LINKED,))
        connection.commit()
        connection.close()
        data = self.resolver.resolve_case(case())
        self.assertEqual(data["metrics"]["items_sold"], 44)
        self.assertEqual(data["metrics"]["ctor_display"], "0%")
        self.assertNotIn("views_display", data["metrics"])
        self.assertNotIn("product_impressions_display", data["metrics"])
        connection = self.performance.connect()
        connection.execute("UPDATE periods SET orders=NULL WHERE video_id=? AND range_start='2026-09-07'", (LINKED,))
        connection.commit()
        connection.close()
        # Incomplete numerator cannot be silently treated as zero in a ratio.
        self.assertNotIn("ctor_display", self.resolver.resolve_case(case())["metrics"])

    def test_per_video_coverage_preserves_partial_values_and_real_cutoff(self):
        data = self.resolver.resolve_case(case("7600000000000000004"))
        self.assertEqual(data["status"], "matched")
        self.assertEqual(data["metrics"]["views_display"], "50")
        self.assertEqual(data["metrics"]["items_sold"], 0)
        self.assertEqual(data["evidence"]["as_of"], "2026-08-30")
        self.assertFalse(data["video"]["coverage"]["complete_since_posting"])
        self.assertIn("缺少 10 天", data["evidence"]["context"])
        self.assertIn("不能称为发布以来完整累计", data["evidence"]["context"])

    def test_weekly_for_another_video_does_not_hide_own_daily_and_overlaps_not_added(self):
        exports.export(self.folder / "260901-own.xlsx", "2026-09-01", "2026-09-01",
                       [exports.row("7600000000000000004", 7)])
        exports.export(self.folder / "shifted-week.xlsx", "2026-09-03", "2026-09-09",
                       [exports.row(LINKED, 777, 1, 10)])
        self.performance.sync()
        brief = self.performance.video_brief([LINKED, "7600000000000000004"])
        self.assertEqual(brief["7600000000000000004"]["views"], 57)
        # Existing newer 9/7–13 is retained, shifted overlapping 9/3–9 is excluded.
        self.assertEqual(brief[LINKED]["views"], 12000)
        full = {video["video_id"]: video for video in self.performance.videos()}
        self.assertEqual(full[LINKED]["views"], brief[LINKED]["views"])
        self.assertEqual(full["7600000000000000004"]["views"], brief["7600000000000000004"]["views"])
        self.assertEqual(self.performance.video(LINKED)["views"], brief[LINKED]["views"])
        self.assertEqual(len(brief[LINKED]["excluded_overlapping_periods"]), 1)
        self.assertIn("重叠周表", self.resolver.resolve_case(case())["evidence"]["context"])


class SalesDataHTTPTests(unittest.TestCase):
    setUp = http.WorkbenchHTTPTests.setUp
    stop_server = http.WorkbenchHTTPTests.stop_server
    request = http.WorkbenchHTTPTests.request
    detail = http.WorkbenchHTTPTests.detail
    body = http.WorkbenchHTTPTests.body
    save = http.WorkbenchHTTPTests.save

    def import_sales(self):
        folder = self.root / "exports"
        exports.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.server.app.performance.sync()

    def test_list_detail_directions_same_source_and_save_preserves_old_history(self):
        before = self.store.read_case(self.identifier)["current"]
        self.import_sales()
        body = self.body()
        body["current"]["test_direction"] = copy.deepcopy(DIRECTION)
        self.assertEqual(self.save(body)[0], 200)
        detail = self.detail()
        with patch.object(self.server.app.performance, "video_brief", wraps=self.server.app.performance.video_brief) as brief:
            self.server.app.state_cache = None
            state = self.request("GET", "/api/state")[2]
        self.assertEqual(brief.call_count, 1)
        summary = self.request("GET", "/api/directions?product=202H")[2]
        observation = summary["groups"][0]["observations"][0]
        self.assertEqual(state["cases"][0]["sales_data"], detail["sales_data"])
        self.assertEqual(observation["metrics"], detail["sales_data"]["metrics"])
        self.assertEqual(observation["evidence"], detail["sales_data"]["evidence"])
        self.assertEqual(summary["groups"][0]["cohorts"][0]["data_as_of"], "2026-09-13")
        self.assertEqual(summary["groups"][0]["missing_counts"]["metrics"], 0)
        self.assertEqual(summary["groups"][0]["sales_status_counts"], {"matched": 1})
        self.assertEqual(detail["current"]["metrics"], before["metrics"])
        self.assertEqual(detail["current"]["evidence"], before["evidence"])
        ordinary_save = self.body(detail)
        self.assertEqual(self.save(ordinary_save)[0], 200)
        stored = self.store.read_case(self.identifier)
        self.assertEqual(stored["current"]["metrics"], before["metrics"])
        self.assertEqual(stored["current"]["evidence"], before["evidence"])

    def test_sales_update_invalidates_cached_state_without_case_revision_change(self):
        self.import_sales()
        first = self.request("GET", "/api/state")[2]
        revision = first["cases"][0]["revision"]
        writer = sqlite3.connect(self.server.app.performance.database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE periods SET views=5000 WHERE video_id=? AND range_start='2026-09-07'", (LINKED,))
        writer.commit()
        try:
            second = self.request("GET", "/api/state")[2]
            self.assertEqual(second["cases"][0]["revision"], revision)
            self.assertNotEqual(second["signature"], first["signature"])
            self.assertNotEqual(second["digest"], first["digest"])
            self.assertEqual(second["cases"][0]["sales_data"]["metrics"]["views_display"], "13000")
            self.assertEqual(self.detail()["sales_data"], second["cases"][0]["sales_data"])
        finally:
            writer.close()


class SalesDataAITests(unittest.TestCase):
    setUp = tasks.TaskTests.setUp
    tearDown = tasks.TaskTests.tearDown
    new_manager = tasks.TaskTests.new_manager
    body = tasks.TaskTests.body
    wait = tasks.TaskTests.wait
    path = tasks.TaskTests.path

    def prepare_sales(self):
        folder = Path(self.temp.name) / "exports"
        exports.build_exports(folder)
        performance = PerformanceStore(self.runtime, [folder])
        performance.sync()
        self.manager._sales = SalesDataResolver(performance)
        current = copy.deepcopy(self.current)
        current.update(metrics={"video_id": LINKED, "views_display": "99999", "items_sold": 999},
                       evidence={"source": "旧截图", "window": "旧窗口", "status": "user_reported"},
                       test_direction=copy.deepcopy(DIRECTION))
        self.store.append(self.identifier, {"kind": "editorial", "text": "准备隔离销售绑定", "current": current})
        return performance

    def embedded(self):
        prompt = self.bridges[-1].calls[-1]["prompt"]
        return json.loads(prompt.split("<untrusted_case_snapshot>\n", 1)[1].split("\n</untrusted_case_snapshot>", 1)[0])

    def test_new_task_freezes_sales_and_peer_data_across_continuation(self):
        performance = self.prepare_sales()
        done = self.wait(self.manager.create(self.identifier, self.body("retrospective"))["task_id"])
        raw = json.loads(self.path(done["task_id"]).read_text(encoding="utf-8"))
        frozen = copy.deepcopy(raw["snapshot"])
        self.assertEqual(frozen["current"]["metrics"]["views_display"], "99999")  # Historical unchanged.
        self.assertEqual(frozen["sales_data"]["metrics"]["views_display"], "12000")
        self.assertEqual(self.embedded()["current"]["metrics"]["views_display"], "12000")
        writer = performance.connect()
        writer.execute("UPDATE periods SET views=9000 WHERE video_id=?", (LINKED,))
        writer.commit()
        writer.close()
        with patch.object(performance, "video_brief", wraps=performance.video_brief) as query:
            self.wait(self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex,
                      "task_revision": done["revision"], "instruction": "继续原窗口"})["task_id"])
        self.assertEqual(query.call_count, 0)
        latest = json.loads(self.path(done["task_id"]).read_text(encoding="utf-8"))
        self.assertEqual(latest["snapshot"], frozen)
        projected = self.embedded()
        self.assertEqual(projected["sales_data"]["metrics"]["views_display"], "12000")
        self.assertEqual(projected["direction_summary"]["data"]["groups"][0]["observations"][0]["metrics"]["views_display"], "12000")
        new = self.wait(self.manager.create(self.identifier, self.body("retrospective"))["task_id"])
        self.assertEqual(self.embedded()["sales_data"]["metrics"]["views_display"], "18000")

    def test_legacy_task_continuation_drops_legacy_current_performance_and_never_reads_live(self):
        performance = self.prepare_sales()
        done = self.wait(self.manager.create(self.identifier, self.body("retrospective"))["task_id"])
        raw = json.loads(self.path(done["task_id"]).read_text(encoding="utf-8"))
        del raw["snapshot"]["sales_data"]
        observation = raw["snapshot"]["direction_summary"]["data"]["groups"][0]["observations"][0]
        del observation["sales_data"]
        observation["metrics"] = {"video_id": LINKED, "items_sold": 999}
        with self.lock:
            self.manager._write(raw)
        done = self.manager.get(done["task_id"])
        with patch.object(performance, "video_brief", wraps=performance.video_brief) as query:
            self.wait(self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex,
                      "task_revision": done["revision"], "instruction": "继续历史任务"})["task_id"])
        self.assertEqual(query.call_count, 0)
        projected = self.embedded()
        self.assertEqual(projected["sales_data"]["status"], "unavailable")
        self.assertEqual(projected["current"]["metrics"], {"video_id": LINKED})
        self.assertNotIn("items_sold", projected["direction_summary"]["data"]["groups"][0]["observations"][0]["metrics"])


if __name__ == "__main__":
    unittest.main()
