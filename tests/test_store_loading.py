"""Loading caches keep external edits, fresh worker results and mutation isolation."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_ai_tasks as ai_fixture
from ai_tasks import AITaskError
import ai_tasks
from creation_store import CreationError, CreationStore
from elevenlabs_client import VoiceError
from production_store import ProductionStore


class CatalogLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="catalog-loading-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def creation(self):
        store = CreationStore(self.root)
        data = store.empty()
        data["scripts"] = [
            {"id": "SCR-1", "product": "202H", "status": "draft", "history": [{"note": "saved"}]},
            {"id": "SCR-2", "product": "129N", "status": "draft", "history": [{"note": "other"}]},
        ]
        data["requests"]["old-request"] = {"payload": "large saved retry" * 20000}
        store.write(data, backup=False)
        return store

    def production(self):
        store = ProductionStore(self.root)
        data = store.empty()
        data["jobs"] = [
            {"id": "PRD-000000000001", "script_id": "SCR-1", "product": "202H", "selected_take": None},
            {"id": "PRD-000000000002", "script_id": "SCR-2", "product": "129N", "selected_take": None},
        ]
        data["carousels"] = [{"id": "CAR-000000000001", "script_id": "SCR-3", "product": "202H",
                              "publication": {"post_id": "7600000000000000001", "published_on": "2026-10-14"}}]
        data["requests"]["old-request"] = {"payload": "large saved retry" * 20000}
        store.write(data)
        return store

    def test_catalogs_reuse_parse_and_keep_independent_public_and_raw_reads(self):
        for factory, collection in ((self.creation, "scripts"), (self.production, "jobs")):
            with self.subTest(factory=factory.__name__):
                store = factory()
                original_read = Path.read_text
                with patch.object(Path, "read_text", autospec=True, side_effect=original_read) as read:
                    first = store.public("202H")
                    first[collection][0]["product"] = "local edit"
                    raw = store.read()
                    raw[collection][0]["product"] = "unsaved raw edit"
                    raw["requests"]["old-request"]["payload"] = "mutated retry"
                    second = store.public("202H")
                    self.assertEqual(second[collection][0]["product"], "202H")
                    self.assertNotEqual(store.read()["requests"]["old-request"]["payload"], "mutated retry")
                    self.assertEqual(sum(call.args[0] == store.path for call in read.call_args_list), 1)
                    self.assertEqual(len(second[collection]), 1)

    def test_production_records_skip_receipts_and_stay_independent(self):
        store = self.production()
        records = store.records()
        self.assertNotIn("requests", records)
        self.assertEqual(store.revision(records), store.current_revision())
        records["jobs"][0]["product"] = "unsaved records edit"
        self.assertEqual(store.records()["jobs"][0]["product"], "202H")
        self.assertIn("old-request", store.read()["requests"])
        with patch("production_store.copy.deepcopy", wraps=copy.deepcopy) as cloned:
            store.records()
        self.assertNotIn("requests", cloned.call_args.args[0])

    def test_external_atomic_replacement_with_preserved_timestamp_is_visible(self):
        for factory, collection in ((self.creation, "scripts"), (self.production, "jobs")):
            with self.subTest(factory=factory.__name__):
                store = factory()
                first = store.public()
                metadata = store.path.stat()
                data = store.read()
                data[collection][0]["product"] = "333H"
                replacement = store.path.with_suffix(".restore")
                replacement.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
                os.utime(replacement, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                os.replace(replacement, store.path)
                second = store.public()
                self.assertEqual(second[collection][0]["product"], "333H")
                self.assertNotEqual(first["revision"], second["revision"])

    def test_same_length_inplace_catalog_change_with_restored_mtime_is_visible(self):
        for factory, collection in ((self.creation, "scripts"), (self.production, "jobs")):
            with self.subTest(factory=factory.__name__):
                store = factory()
                first = store.public()
                before = store.path.stat()
                original = store.path.read_bytes()
                changed = original.replace(b"202H", b"333H")
                self.assertEqual(len(changed), len(original))
                store.path.write_bytes(changed)
                os.utime(store.path, ns=(before.st_atime_ns, before.st_mtime_ns))
                current = store.public()
                self.assertEqual(current[collection][0]["product"], "333H")
                self.assertNotEqual(current["revision"], first["revision"])

    def test_current_creation_revision_matches_full_read_and_detects_external_change(self):
        store = self.creation()
        expected = store.revision(store.read())
        with patch("creation_store.copy.deepcopy", side_effect=AssertionError("revision copied full receipts")):
            self.assertEqual(store.current_revision(), expected)
        before = store.path.stat()
        store.path.write_bytes(store.path.read_bytes().replace(b"202H", b"333H"))
        os.utime(store.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        current = store.current_revision()
        self.assertNotEqual(current, expected)
        self.assertEqual(current, store.revision(store.read()))

    def test_deleted_restored_and_invalid_catalogs_do_not_return_cached_records(self):
        for factory, collection, error in ((self.creation, "scripts", CreationError),
                                            (self.production, "jobs", VoiceError)):
            with self.subTest(factory=factory.__name__):
                store = factory()
                expected = store.path.read_bytes()
                self.assertTrue(store.public()[collection])
                store.path.unlink()
                self.assertEqual(store.public()[collection], [])
                store.path.write_bytes(expected)
                self.assertTrue(store.public()[collection])
                store.path.write_text("{}", encoding="utf-8")
                with self.assertRaises(error):
                    store.public()

    def test_cross_instance_catalog_writer_invalidates_reader_snapshot(self):
        first = self.production()
        before = first.public()["revision"]
        second = ProductionStore(self.root)
        data = second.read()
        data["jobs"][0]["archived"] = True
        second.write(data)
        self.assertTrue(first.job("PRD-000000000001")["archived"])
        self.assertNotEqual(first.public()["revision"], before)

    def test_running_result_readback_stays_fresh_with_cached_catalog(self):
        store = self.production()
        data = store.read()
        job = data["jobs"][0]
        job.update(selected_take="TAK-000000000001",
                   timelines=[{"unique_id": "timeline-1", "take_id": "TAK-000000000001"}],
                   edits=[{"id": "EDT-000000000001", "status": "running", "timeline_uid": "timeline-1",
                           "completion_policy": "skill_delivery"}])
        store.write(data)
        self.assertEqual(store.public("202H")["jobs"][0]["status"], "editing")
        result = store.root / "edits" / "EDT-000000000001" / "result.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"job_id": job["id"], "edit_id": "EDT-000000000001"}), encoding="utf-8")
        self.assertEqual(store.public("202H")["jobs"][0]["status"], "needs_repair")
        self.assertEqual(store.read()["jobs"][0]["edits"][0]["status"], "running")
        result.unlink()
        self.assertEqual(store.job(job["id"])["status"], "editing")

    def test_script_usage_copies_only_provenance_and_ignores_worker_readback(self):
        store = self.production()
        with patch("production_store.public_job", side_effect=AssertionError("full job readback")):
            usage = store.script_usage("202H")
        self.assertEqual({row["kind"] for row in usage}, {"video", "carousel"})
        self.assertEqual({row["script_id"] for row in usage}, {"SCR-1", "SCR-3"})
        usage[1]["publication"]["post_id"] = "local change"
        self.assertEqual(store.script_usage("202H")[1]["publication"]["post_id"], "7600000000000000001")


class TaskLoadingTests(unittest.TestCase):
    setUp = ai_fixture.TaskTests.setUp
    tearDown = ai_fixture.TaskTests.tearDown
    new_manager = ai_fixture.TaskTests.new_manager
    body = ai_fixture.TaskTests.body
    wait = ai_fixture.TaskTests.wait
    path = ai_fixture.TaskTests.path

    def completed(self):
        return self.wait(self.manager.create(self.identifier, self.body())["task_id"])

    def test_task_lists_reuse_parsing_and_raw_reads_are_independent(self):
        done = self.completed()
        self.manager.clear_cache()
        with patch("ai_tasks.load_json", wraps=ai_tasks.load_json) as load:
            first = self.manager.list_tasks()
            first[0]["context"]["materials"].append({"unsaved": True})
            self.assertEqual(self.manager.list_tasks()[0]["context"]["materials"], [])
            raw = self.manager._read(done["task_id"])
            raw["rounds"][0]["output"] = "unsaved output"
            self.assertNotEqual(self.manager.get(done["task_id"])["rounds"][0]["output"], "unsaved output")
            self.assertEqual(load.call_count, 1)

    def test_external_task_changes_corruption_and_removal_are_visible_on_next_list(self):
        done = self.completed()
        self.manager.list_tasks()
        path = self.path(done["task_id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update(status="failed", management_state="needs_revision", revision=raw["revision"] + 1)
        raw["rounds"][-1].update(status="failed", error="external failure")
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        current = self.manager.list_tasks()[0]
        self.assertEqual((current["status"], current["management_state"], current["last_error"]),
                         ("failed", "needs_revision", "external failure"))
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(AITaskError):
            self.manager.list_tasks()
        path.unlink()
        self.assertEqual(self.manager.list_tasks(), [])

    def test_same_length_inplace_task_change_with_restored_mtime_is_visible(self):
        done = self.completed()
        path = self.path(done["task_id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["rounds"][-1]["instruction"] = "first marker"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.manager.list_tasks()[0]["instruction"], "first marker")
        before = path.stat()
        original = path.read_bytes()
        changed = original.replace(b"first marker", b"other marker")
        self.assertEqual(len(changed), len(original))
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.manager.list_tasks()[0]["instruction"], "other marker")

    def test_new_external_task_and_changed_source_are_visible_after_warm_list(self):
        done = self.completed()
        self.assertFalse(self.manager.list_tasks()[0]["stale"])
        original = json.loads(self.path(done["task_id"]).read_text(encoding="utf-8"))
        original["task_id"] = "AIT-000000000001"
        path = self.path(original["task_id"])
        path.parent.mkdir()
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        current = copy.deepcopy(self.current)
        current["conclusion"] = "externally appended source"
        self.store.append(self.identifier, {"kind": "editorial", "text": "fixture change", "current": current})
        rows = self.manager.list_tasks()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["stale"] for row in rows))

    def test_warm_task_cache_still_rejects_hardlinks(self):
        done = self.completed()
        self.manager.list_tasks()
        alias = self.root / "alias.json"
        try:
            os.link(self.path(done["task_id"]), alias)
        except OSError as exc:
            self.skipTest(str(exc))
        try:
            with self.assertRaises(AITaskError):
                self.manager.list_tasks()
        finally:
            alias.unlink()


if __name__ == "__main__":
    unittest.main()
