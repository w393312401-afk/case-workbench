"""Global read paths avoid unrelated catalogs and keep slow media off the write lock."""
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import test_server as fixture
from production_operations import ProductionOperations


class GlobalLoadingTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_script_and_direction_reads_do_not_build_full_production_catalogs(self):
        app = self.server.app
        with patch.object(app.production, "public", side_effect=AssertionError("unrelated production projection")), \
                patch.object(app, "_script_production_usage", wraps=app._script_production_usage) as usage:
            self.assertEqual(self.request("GET", "/api/creation?product=202H")[0], 200)
            self.assertEqual(usage.call_count, 1)
            self.assertEqual(self.request("GET", "/api/direction-catalog?product=202H")[0], 200)
            self.assertEqual(usage.call_count, 1)

    def test_state_and_product_caches_detect_preserved_timestamp_creation_changes(self):
        app = self.server.app
        catalog = app.creation.empty()
        catalog["materials"] = [{"product": "333H"}]
        catalog["requests"] = {"old-receipt": {"payload": "saved result" * 10000}}
        app.creation.write(catalog, backup=False)
        with patch.object(app.creation, "read", side_effect=AssertionError("full receipt clone")), \
                patch.object(app.creation, "public", side_effect=AssertionError("full public catalog")):
            first = self.request("GET", "/api/state")[2]
            self.assertIn("333H", self.request("GET", "/api/products")[2]["products"])
            before = app.creation.path.stat()
            original = app.creation.path.read_bytes()
            changed = original.replace(b"333H", b"444H")
            self.assertEqual(len(changed), len(original))
            app.creation.path.write_bytes(changed)
            os.utime(app.creation.path, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = self.request("GET", "/api/state")[2]
            self.assertNotEqual(first["digest"], second["digest"])
            self.assertIn("444H", self.request("GET", "/api/products")[2]["products"])
            self.assertNotIn("333H", app.products()["products"])

    def test_journal_parse_reuse_detects_changes_and_isolates_mutable_records(self):
        operations = ProductionOperations(self.runtime / "production")
        operations.root.mkdir(parents=True)
        path = operations.root / "one.json"
        value = {"schema_version": 1, "state": "committed", "job_id": "PRD-111111111111", "kind": "take"}
        path.write_text(json.dumps(value), encoding="utf-8")
        with patch.object(operations, "_read_file", wraps=operations._read_file) as read:
            first = operations._read(path)
            first["state"] = "uncertain"
            self.assertEqual(operations._read(path)["state"], "committed")
            self.assertEqual(operations.public(), [])
            self.assertEqual(read.call_count, 1)
            path.write_text(json.dumps({**value, "error": "new external receipt"}), encoding="utf-8")
            self.assertEqual(operations._read(path)["error"], "new external receipt")
            self.assertEqual(read.call_count, 2)
            path.write_text("invalid json", encoding="utf-8")
            with self.assertRaises(Exception):
                operations._read(path)

    def test_production_catalog_reads_recovery_journals_once_for_all_orders(self):
        app = self.server.app
        jobs = [{"id": f"PRD-{number:012x}", "edits": [], "timelines": [], "status": "draft"} for number in range(8)]
        catalog = {"jobs": jobs, "carousels": []}
        operations = app._production_operations()
        with patch.object(app.production, "public", return_value=catalog), \
                patch.object(operations, "public", return_value=[]) as records, \
                patch.object(app, "_production_edit_state"), patch.object(app, "attach_content_publications", side_effect=lambda data:data):
            self.assertEqual(len(app.production_catalog()["jobs"]), 8)
        self.assertEqual(records.call_count, 1)

    def test_same_length_inplace_journal_change_with_restored_mtime_is_visible(self):
        operations = ProductionOperations(self.runtime / "production")
        operations.root.mkdir(parents=True)
        path = operations.root / "one.json"
        path.write_text(json.dumps({"schema_version": 1, "state": "committed"}), encoding="utf-8")
        self.assertEqual(operations._read(path)["state"], "committed")
        before = path.stat()
        original = path.read_bytes()
        changed = original.replace(b"committed", b"uncertain")
        self.assertEqual(len(changed), len(original))
        path.write_bytes(changed)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(operations._read(path)["state"], "uncertain")

    def test_slow_delivery_check_does_not_block_an_independent_business_read(self):
        app = self.server.app
        entered, release = threading.Event(), threading.Event()
        failures = []
        catalog = {"jobs": [], "carousels": [{"id": "CAR-111111111111", "slides": [], "deliveries": []}]}
        def slow(item):
            entered.set()
            if not release.wait(5):
                raise AssertionError("media verification was not released")
        def read_deliveries():
            try:
                app.production_deliveries()
            except Exception as error:
                failures.append(error)
        with patch.object(app.production, "public", return_value=catalog), patch.object(app, "carousel_images_live", side_effect=slow):
            worker = threading.Thread(target=read_deliveries)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                acquired = app.lock.acquire(timeout=.5)
                self.assertTrue(acquired, "Slow file reads must not hold the global business lock")
                if acquired:
                    app.lock.release()
                self.assertEqual(self.request("GET", "/api/creation?product=202H")[0], 200)
            finally:
                release.set();worker.join(5)
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
