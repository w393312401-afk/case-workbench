"""Direction details share one verified index without caching across requests."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import test_direction_catalog as directions
import test_direction_loading as loading
import test_server as fixture

SERVER = fixture.SERVER


class IndexSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="direction-index-snapshot-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.skill = self.root / "skill"
        self.runtime = self.root / "runtime"
        self.video = self.root / "video.mp4"
        self.video.write_bytes(b"fixture")
        self.writer = SERVER.CaseStore(self.skill)
        current = fixture.snapshot(self.video)
        current["test_direction"] = directions.direction()
        case = self.writer.add({"title": "Initial case", "product": "202H",
                                "original": {"text": "Immutable original"},
                                "feedback": {"kind": "editorial", "text": "Fixture"},
                                "current": current})
        self.identifier = case["case_id"]
        self.store = SERVER.CachedCaseStore(self.skill)

    def update_title(self, title):
        current = self.writer.read_case(self.identifier)["current"]
        current["title"] = title
        self.writer.append(self.identifier, {"kind": "editorial", "text": "External edit", "current": current})

    def test_scope_nested_and_exception_restore_then_next_scope_rechecks(self):
        with patch.object(self.store, "fingerprint", wraps=self.store.fingerprint) as scan:
            with self.store.index_snapshot() as outer:
                outer_key = self.store.index_key
                self.update_title("External edit during scope")
                with self.assertRaisesRegex(RuntimeError, "nested fixture"):
                    with self.store.index_snapshot() as inner:
                        self.assertIs(inner, outer)
                        self.assertIs(self.store.read_index(), outer)
                        raise RuntimeError("nested fixture")
                self.assertIs(self.store.read_index(), outer)
                self.assertEqual(self.store.index_key, outer_key)
            whole_scans = [call for call in scan.call_args_list if not call.args]
            self.assertEqual(len(whole_scans), 1)
            with self.store.index_snapshot() as changed:
                self.assertEqual(changed["cases"][0]["title"], "External edit during scope")
                self.assertNotEqual(self.store.index_key, outer_key)
        self.assertFalse(hasattr(self.store._index_context, "snapshot"))
        with self.assertRaisesRegex(RuntimeError, "outer fixture"):
            with self.store.index_snapshot():
                raise RuntimeError("outer fixture")
        self.assertFalse(hasattr(self.store._index_context, "snapshot"))
        self.update_title("After exception")
        self.assertEqual(self.store.read_index()["cases"][0]["title"], "After exception")

    def test_concurrent_scope_keeps_its_index_key_and_other_thread_revalidates(self):
        started, resume = threading.Event(), threading.Event()
        captured, errors = {}, []

        def first_reader():
            try:
                with self.store.index_snapshot() as index:
                    captured["first"] = index
                    captured["first_key"] = self.store.index_key
                    started.set()
                    if not resume.wait(5):
                        raise TimeoutError("second reader did not finish")
                    captured["later"] = self.store.read_index()
                    captured["later_key"] = self.store.index_key
            except Exception as exc:
                errors.append(exc)
                started.set()

        thread = threading.Thread(target=first_reader)
        thread.start()
        try:
            self.assertTrue(started.wait(5))
            self.assertFalse(errors)
            self.update_title("Second reader sees this")
            with self.store.index_snapshot() as index:
                second_key = self.store.index_key
                self.assertEqual(index["cases"][0]["title"], "Second reader sees this")
                self.assertNotEqual(second_key, captured["first_key"])
        finally:
            resume.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertIs(captured["later"], captured["first"])
        self.assertEqual(captured["later_key"], captured["first_key"])
        self.assertEqual(self.store.index_key, second_key)

    def test_individual_case_reads_still_check_updated_and_damaged_records(self):
        with self.store.index_snapshot():
            first = self.store.read_case(self.identifier)
            self.update_title("Changed individual case")
            self.assertEqual(self.store.read_case(self.identifier)["current"]["title"], "Changed individual case")
            self.assertNotEqual(first["current"]["title"], "Changed individual case")
            unexpected = self.writer.folder(self.identifier) / "events" / "invalid.txt"
            unexpected.write_text("invalid event", encoding="utf-8")
            with self.assertRaises(SERVER.StoreError):
                self.store.read_case(self.identifier)

    def test_archive_and_new_case_refresh_and_broken_index_fails_next_scope(self):
        with self.store.index_snapshot() as first:
            self.assertFalse(first["cases"][0]["archived"])
        self.writer.set_archived(self.identifier, True, "Archive externally")
        current = fixture.new_case_snapshot(self.video)
        self.writer.add({"title": "Added externally", "product": "202H", "original": {"text": "Fixture"},
                         "feedback": {"kind": "editorial", "text": "Fixture"}, "current": current})
        with self.store.index_snapshot() as changed:
            self.assertEqual(len(changed["cases"]), 2)
            self.assertTrue(next(item for item in changed["cases"] if item["case_id"] == self.identifier)["archived"])
        (self.skill / "cases" / "index.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(SERVER.StoreError):
            with self.store.index_snapshot():
                self.fail("broken index must not enter the scope")
        self.assertFalse(hasattr(self.store._index_context, "snapshot"))
        self.writer.rebuild_index()
        with self.store.index_snapshot() as repaired:
            self.assertEqual(len(repaired["cases"]), 2)

    def test_broken_case_is_not_hidden_by_a_previous_scope(self):
        with self.store.index_snapshot():
            pass
        original = self.writer.folder(self.identifier) / "original.json"
        original.write_text("{}", encoding="utf-8")
        with self.assertRaises(SERVER.StoreError):
            with self.store.index_snapshot():
                self.fail("broken case must not enter the scope")
        self.assertFalse(hasattr(self.store._index_context, "snapshot"))

    def app(self):
        directions.install_points(self.skill)
        return SERVER.Workbench(self.skill, self.runtime, allow_open=False)

    def test_detail_scans_index_once_and_other_operations_keep_fresh_checks(self):
        app = self.app()
        app.direction_detail("202H-S1", "202H", view="page")
        with patch.object(app.store, "fingerprint", wraps=app.store.fingerprint) as scan:
            detail = app.direction_detail("202H-S1", "202H", view="page")
            self.assertEqual(len([call for call in scan.call_args_list if not call.args]), 1)
            app.direction_catalog("202H")
            self.assertEqual(len([call for call in scan.call_args_list if not call.args]), 2)
        self.assertEqual(detail["cases"][0]["current"]["title"], fixture.snapshot(self.video)["title"])
        self.update_title("Fresh next direction request")
        changed = app.direction_detail("202H-S1", "202H", view="page")
        self.assertEqual(changed["cases"][0]["current"]["title"], "Fresh next direction request")
        self.writer.set_archived(self.identifier, True, "Archive externally")
        archived = app.direction_detail("202H-S1", "202H", view="page")
        self.assertTrue(archived["cases"][0]["archived"])

    def test_detail_failure_cleans_scope_and_full_view_still_reads_cases(self):
        app = self.app()
        with patch.object(app, "creation_catalog", side_effect=RuntimeError("creation fixture")):
            with self.assertRaisesRegex(RuntimeError, "creation fixture"):
                app.direction_detail("202H-S1", "202H", view="page")
        self.assertFalse(hasattr(app.store._index_context, "snapshot"))
        self.update_title("After failed direction request")
        with patch.object(app.store, "read_case", wraps=app.store.read_case) as read:
            full = app.direction_detail("202H-S1", "202H")
        self.assertGreaterEqual(read.call_count, 1)
        self.assertEqual(full["cases"][0]["current"]["title"], "After failed direction request")

    def test_synced_catalog_uses_scope_key_when_another_reader_updates_global_key(self):
        app = self.app()
        app.direction_catalog("202H")
        with app.store.index_snapshot() as before:
            before_key = app.store.index_key
            self.update_title("Another thread's new snapshot")
            failures = []

            def outside_reader():
                try:
                    app.store.read_index()
                except Exception as exc:
                    failures.append(exc)

            thread = threading.Thread(target=outside_reader)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertFalse(failures)
            self.assertNotEqual(app.store._index_key, before_key)
            self.assertEqual(app.store.index_key, before_key)
            app.synced_directions(before["cases"])
            self.assertEqual(app.direction_sync_cache[0][0], before_key)
        self.assertNotEqual(app.store.index_key, before_key)
        refreshed = app.direction_detail("202H-S1", "202H", view="page")
        self.assertEqual(refreshed["cases"][0]["current"]["title"], "Another thread's new snapshot")


class DirectionTasksProgressiveTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    setup_direction = loading.DirectionLoadingTests.setup_direction
    write_task = loading.DirectionLoadingTests.write_task

    def test_page_core_matches_complete_detail_and_defers_only_task_reads(self):
        self.setup_direction()
        self.write_task(1, owner=self.identifier)
        app = self.server.app
        complete = app.direction_detail("202H-S1", "202H", view="page")
        self.assertEqual(len(complete["tasks"]), 1)
        with patch.object(app.ai, "list_tasks", side_effect=AssertionError("AI history must follow the main page")):
            status, _, core = self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=0")
        self.assertEqual(status, 200, core)
        self.assertEqual(core["tasks"], [])
        self.assertTrue(core["tasks_pending"])
        self.assertEqual({key: value for key, value in core.items() if key not in {"tasks", "tasks_pending"}},
                         {key: value for key, value in complete.items() if key != "tasks"})
        self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page")[2], complete)
        self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=1")[2], complete)
        full = app.direction_detail("202H-S1", "202H")
        self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&include_tasks=0")[2], full)

    def test_new_query_parameters_reject_duplicates_and_invalid_task_flag(self):
        self.setup_direction()
        for query in ("include_tasks=invalid", "include_tasks=", "include_tasks=0&include_tasks=1", "include_tasks=&include_tasks=0"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&" + query)[0], 400)
        for query in ("product=202H&product=129N", "product=&product=202H", "direction_id=202H-S1&direction_id=202H-S2",
                      "direction_id=&direction_id=202H-S1", "case_id=one&case_id=two"):
            with self.subTest(query=query):
                self.assertEqual(self.request("GET", "/api/ai/tasks?" + query)[0], 400)

    def test_filtered_task_endpoint_keeps_frozen_ownership_and_fresh_files(self):
        self.setup_direction()
        original, original_path = self.write_task(1, owner=self.identifier)
        contextual, _ = self.write_task(2, frozen="202H-S2", context="202H-S1")
        self.write_task(3, frozen="202H-S1", context="202H-S2")
        self.write_task(4, product="129N")
        app = self.server.app
        all_tasks = app.ai.list_tasks()
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], all_tasks)
        self.assertEqual([task["task_id"] for task in self.request("GET", "/api/ai/tasks?case_id=" + self.identifier)[2]["tasks"]], [original])
        path = "/api/ai/tasks?product=SHARPAL+202H&direction_id=202H-S1"
        expected = [task for task in all_tasks if task["task_id"] in {original, contextual}]
        with patch.object(app.ai, "_read", wraps=app.ai._read) as read:
            status, _, result = self.request("GET", path)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["tasks"], expected)
        self.assertEqual(read.call_count, 4)
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = directions.direction("S2")
        self.store.append(self.identifier, {"kind": "editorial", "text": "Current case moved", "current": current})
        self.assertEqual(self.request("GET", path)[2]["tasks"], expected)
        value = json.loads(original_path.read_text(encoding="utf-8"))
        value["snapshot"]["current"]["test_direction"]["direction_id"] = "202H-S2"
        original_path.write_text(json.dumps(value), encoding="utf-8")
        self.assertEqual([task["task_id"] for task in self.request("GET", path)[2]["tasks"]], [contextual])
        added, added_path = self.write_task(5)
        self.assertEqual([task["task_id"] for task in self.request("GET", path)[2]["tasks"]], [added, contextual])
        added_path.unlink()
        added_path.parent.rmdir()
        self.assertEqual([task["task_id"] for task in self.request("GET", path)[2]["tasks"]], [contextual])


if __name__ == "__main__":
    unittest.main()
