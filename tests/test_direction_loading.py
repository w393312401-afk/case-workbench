"""Direction details batch existing observations and read frozen tasks only once."""
from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch

import test_direction_catalog as directions
import test_performance as exports
import test_server as fixture
import ai_tasks

SERVER = fixture.SERVER


class DirectionLoadingTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setup_direction(self):
        directions.install_points(self.skill)
        for index in range(3):
            current = fixture.snapshot(self.video)
            current["test_direction"] = directions.direction()
            current["metrics"]["video_id"] = exports.LINKED if index == 0 else str(7600000000000000000 + index)
            if index == 0:
                self.store.append(self.identifier, {"kind": "editorial", "text": "Direction fixture", "current": current})
            else:
                self.store.add({"title": "Another direction case", "product": "202H", "original": {"text": "Fixture"},
                                "feedback": {"kind": "editorial", "text": "Fixture"}, "current": current})
        folder = self.root / "exports"
        exports.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.server.app.performance.sync()

    def write_task(self, number, *, product="202H", frozen="202H-S1", context=None, owner=None):
        identifier = f"AIT-{number:012x}"
        snapshot = {"product": product, "current": {"test_direction": {"direction_id": frozen}}}
        if context is not None:
            snapshot["creation_context"] = {"direction": {"direction_id": context}}
        value = {"schema_version": 1, "task_id": identifier, "case_id": owner, "case_title": "Frozen task",
                 "action": "review", "status": "completed", "revision": 1, "base_revision": "frozen",
                 "created_at": f"2026-10-11T00:00:{number:02d}", "snapshot": snapshot, "requests": [],
                 "rounds": [{"round_id": f"AIR-{number:012x}", "status": "completed", "instruction": "Fixture",
                             "output": "Completed fixture", "error": None}]}
        root = self.store.folder(owner) / "collaboration" if owner else self.runtime / "ai-tasks"
        path = root / identifier / "task.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return identifier, path

    def test_detail_batches_sales_and_reuses_raw_direction_without_extra_fields(self):
        self.setup_direction()
        app = self.server.app
        with patch.object(app.performance, "video_brief", wraps=app.performance.video_brief) as brief, \
             patch.object(app.directions, "read", wraps=app.directions.read) as read:
            detail = app.direction_detail("202H-S1", "202H")
        self.assertEqual(len(detail["cases"]), 3)
        self.assertEqual(detail["selling_points"], app.direction_catalog("202H")["selling_points"])
        self.assertEqual(brief.call_count, 3)  # Catalog, group observations, final video list.
        self.assertLessEqual(read.call_count, 2)
        observations = {item["case_id"]: item for item in detail["group"]["observations"]}
        for case in detail["cases"]:
            self.assertEqual(case["sales_data"], observations[case["case_id"]]["sales_data"])
            self.assertEqual(case["sales_data"], app.detail(case["case_id"])["sales_data"])
            self.assertNotIn("performance", case["resolved_direction"])
            self.assertNotIn("lead_tier", case["resolved_direction"])

    def test_frozen_task_filter_matches_original_cards_without_second_task_reads(self):
        self.setup_direction()
        same, _ = self.write_task(1, product="SHARPAL 202H", owner=self.identifier)
        chosen_context, _ = self.write_task(2, frozen="202H-S2", context="202H-S1")
        self.write_task(3, frozen="202H-S1", context="202H-S2")
        self.write_task(4, product="129N")
        self.write_task(5, frozen="202H-S3")
        app = self.server.app
        all_tasks = app.ai.list_tasks()
        expected = [task for task in all_tasks if task["task_id"] in {same, chosen_context}]
        with patch.object(app.ai, "_read", wraps=app.ai._read) as read, \
             patch.object(app.ai, "_detail", wraps=app.ai._detail) as summarize:
            detail = app.direction_detail("202H-S1", "202H")
        self.assertEqual(detail["tasks"], expected)
        self.assertEqual(read.call_count, 5)
        self.assertEqual(summarize.call_count, 2)
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = directions.direction("S2")
        self.store.append(self.identifier, {"kind": "editorial", "text": "Case moved later", "current": current})
        self.assertIn(same, [task["task_id"] for task in app.direction_detail("202H-S1", "202H")["tasks"]])

    def test_new_removed_and_changed_tasks_are_discovered_on_each_read(self):
        self.setup_direction()
        app = self.server.app
        self.assertEqual(app.direction_detail("202H-S1", "202H")["tasks"], [])
        identifier, path = self.write_task(1)
        self.assertEqual([task["task_id"] for task in app.direction_detail("202H-S1", "202H")["tasks"]], [identifier])
        value = json.loads(path.read_text(encoding="utf-8"))
        value["snapshot"]["current"]["test_direction"]["direction_id"] = "202H-S2"
        path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual(app.direction_detail("202H-S1", "202H")["tasks"], [])
        path.unlink()
        path.parent.rmdir()
        self.assertEqual(app.ai.list_tasks(), [])

    def test_sales_case_source_and_direction_changes_remain_fresh(self):
        self.setup_direction()
        app = self.server.app
        first = app.direction_detail("202H-S1", "202H")
        connection = app.performance.connect()
        connection.execute("UPDATE periods SET views=views+1000 WHERE video_id=?", (exports.LINKED,))
        connection.close()
        changed = app.direction_detail("202H-S1", "202H")
        before_case = next(item for item in first["cases"] if item["case_id"] == self.identifier)
        changed_case = next(item for item in changed["cases"] if item["case_id"] == self.identifier)
        self.assertNotEqual(before_case["sales_data"], changed_case["sales_data"])
        self.video.rename(self.video.with_suffix(".moved"))
        moved = app.direction_detail("202H-S1", "202H")
        self.assertFalse(next(item for item in moved["cases"] if item["case_id"] == self.identifier)["assets"]["video"]["exists"])
        catalog = app.directions.read()
        catalog["directions"][0]["notes"] = "External direction update"
        app.directions.write(catalog)
        refreshed = app.direction_detail("202H-S1", "202H")
        self.assertEqual(next(item for item in refreshed["cases"] if item["case_id"] == self.identifier)["resolved_direction"]["notes"], "External direction update")

    def test_missing_collaboration_dirs_skip_redundant_checks_and_files_still_fail(self):
        app = self.server.app
        missing = self.store.folder(self.identifier) / "collaboration"
        with patch.object(ai_tasks, "_check_path", wraps=ai_tasks._check_path) as check:
            self.assertEqual(app.ai.list_tasks(), [])
        self.assertFalse(any(call.args[0] == missing for call in check.call_args_list))
        self.assertTrue(any(call.args[0] == app.store.root for call in check.call_args_list))
        missing.write_text("A file occupying the directory", encoding="utf-8")
        with self.assertRaises(ai_tasks.AITaskError):
            app.ai.list_tasks()

    def test_page_view_keeps_displayed_data_and_skips_full_case_sources_and_assets(self):
        self.setup_direction()
        app = self.server.app
        full = app.direction_detail("202H-S1", "202H")
        with patch.object(app, "detail", side_effect=AssertionError("Full case should load on its own page")), \
             patch.object(app, "asset_info", side_effect=AssertionError("Direction page has no asset status")):
            page = app.direction_detail("202H-S1", "202H", view="page")
        self.assertEqual({key: value for key, value in page.items() if key != "cases"},
                         {key: value for key, value in full.items() if key != "cases"})
        for compact, original in zip(page["cases"], full["cases"]):
            self.assertEqual(compact, {"case_id": original["case_id"], "title": original["current"]["title"],
                                       "archived": original["archived"],
                                       "current": {"title": original["current"]["title"]}})
        self.assertLess(len(json.dumps(page)), len(json.dumps(full)))
        status, _, actual = self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page")
        self.assertEqual(status, 200)
        self.assertEqual(actual, page)
        self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H")[2], full)
        self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=full")[2], full)
        for query in ("view=unknown", "view=page&view=full"):
            self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&" + query)[0], 400)

    def test_page_view_immediately_tracks_case_transcript_and_sales_changes(self):
        self.setup_direction()
        app = self.server.app
        first = app.direction_detail("202H-S1", "202H", view="page")
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["title"] = "Changed title"
        current["transcript"]["text"] = "Changed complete transcript"
        self.store.append(self.identifier, {"kind": "editorial", "text": "External update", "current": current})
        connection = app.performance.connect()
        connection.execute("UPDATE periods SET views=views+1000 WHERE video_id=?", (exports.LINKED,))
        connection.close()
        second = app.direction_detail("202H-S1", "202H", view="page")
        chosen = next(item for item in second["cases"] if item["case_id"] == self.identifier)
        self.assertEqual(chosen["current"]["title"], "Changed title")
        script = next(item for item in second["scripts"] if item.get("case_id") == self.identifier)
        self.assertEqual(script["text"], "Changed complete transcript")
        self.assertNotEqual(first["group"], second["group"])

    def test_task_cards_match_full_public_rounds_without_projecting_outputs(self):
        self.setup_direction()
        identifier, path = self.write_task(1, owner=self.identifier)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["action"] = "variants"
        value["status"] = "failed"
        value["rounds"][0].update(status="completed", instruction="  Full original request  ",
                                  output="口播（配音用）\n```\nMany original words\n```",
                                  error="  Original error  ", requested_model="requested-model",
                                  stages={"draft": {"messages": ["kept in full only"], "output": "Draft"}})
        value["rounds"].append({**value["rounds"][0], "round_id": "AIR-222222222222", "status": "failed"})
        path.write_text(json.dumps(value), encoding="utf-8")
        app = self.server.app
        app.ai._live[identifier] = {"round_id": "AIR-222222222222", "model": "live-model", "error": " Live error "}
        detailed = app.ai.get(identifier)
        rounds = detailed.pop("rounds")
        expected = {**detailed, "round_count": len(rounds),
                    "latest_round_id": rounds[-1]["round_id"], "latest_round_status": rounds[-1]["status"],
                    "instruction": rounds[-1]["instruction"].strip()[:160],
                    "model": rounds[-1]["model"] or rounds[-1]["requested_model"],
                    "last_error": rounds[-1]["error"].strip()[:160]}
        with patch.object(ai_tasks, "read_estimates", side_effect=AssertionError("Card does not display word counts")):
            self.assertEqual(app.ai.list_tasks(), [expected])
        app.ai._live.pop(identifier)


if __name__ == "__main__":
    unittest.main()
