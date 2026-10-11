"""Read-only queue eligibility, using only disposable image/package fixtures."""
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import test_production_carousel_delivery as delivery_fixture


class ProductionQueueStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = delivery_fixture.CarouselDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench
        self.identifier = self.fixture.identifier

    def row(self):
        # Queue reads must neither persist the computed live flags nor package.
        before = self.bench.production.path.read_bytes()
        with patch.object(self.bench.production, "mutate", side_effect=AssertionError("queue wrote data")), \
                patch.object(self.bench, "create_carousel_delivery", side_effect=AssertionError("queue packaged media")):
            rows = self.bench.production_deliveries()["items"]
        self.assertEqual(self.bench.production.path.read_bytes(), before)
        return next(row for row in rows if row["id"] == self.identifier)

    def test_confirmed_group_needs_packaging_before_collection(self):
        self.fixture.ready()
        row = self.row()
        self.assertTrue(row["ready_to_package"])
        self.assertEqual(row["current_delivery_id"], "")
        self.assertFalse(row["delivery_available"])
        self.assertEqual(row["delivery_error"], "")
        self.assertNotIn("delivery_id", row)
        self.assertFalse((self.bench._carousel_dir(self.identifier) / "deliveries").exists())

    def test_only_checked_current_package_is_collectible(self):
        self.fixture.ready()
        record = self.fixture.create()["delivery"]
        row = self.row()
        self.assertTrue(row["ready_to_package"])
        self.assertTrue(row["delivery_available"])
        self.assertEqual(row["current_delivery_id"], record["id"])
        self.assertEqual(row["delivery_id"], record["id"])
        self.assertEqual(row["delivery_error"], "")

    def test_missing_or_changed_package_is_not_collectible(self):
        self.fixture.ready()
        record = self.fixture.create()["delivery"]
        path = self.bench.carousel_delivery_file(self.identifier, record["id"])
        path.write_bytes(b"damaged fake zip")
        row = self.row()
        self.assertTrue(row["ready_to_package"])
        self.assertFalse(row["delivery_available"])
        self.assertEqual(row["current_delivery_id"], record["id"])
        self.assertIn("文件已变化", row["delivery_error"])
        path.unlink()
        row = self.row()
        self.assertFalse(row["delivery_available"])
        self.assertIn("文件不见了", row["delivery_error"])

    def test_package_contents_are_checked_even_when_zip_hash_matches(self):
        self.fixture.ready()
        record = self.fixture.create()["delivery"]
        path = self.bench.carousel_delivery_file(self.identifier, record["id"])
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("caption_en.txt", "incomplete fake package")
        data = self.bench.production.read()
        data["carousels"][0]["deliveries"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.bench.production.write(data)
        row = self.row()
        self.assertFalse(row["delivery_available"])
        self.assertIn("不完整", row["delivery_error"])

    def test_published_historical_package_keeps_legacy_fields_without_current_collection(self):
        self.fixture.ready()
        record = self.fixture.create()["delivery"]
        self.fixture.fixture.change_notes(1)
        data = self.bench.production.read()
        data["carousels"][0]["publication"] = {
            "delivery_id": record["id"], "post_id": "fixture-post", "published_on": "2026-10-11"}
        self.bench.production.write(data)
        row = self.row()
        self.assertEqual(row["delivery_id"], record["id"])
        self.assertTrue(row["published"])
        self.assertFalse(row["ready_to_package"])
        self.assertEqual(row["current_delivery_id"], "")
        self.assertFalse(row["delivery_available"])

    def test_new_confirmed_selection_does_not_collect_previous_package(self):
        self.fixture.ready()
        record = self.fixture.create()["delivery"]
        self.fixture.fixture.generate(slides=[2])
        self.fixture.fixture.approve(2, self.fixture.current()["slides"][1]["outputs"][-1]["id"])
        row = self.row()
        self.assertTrue(row["ready_to_package"])
        self.assertEqual(row["delivery_id"], record["id"])
        self.assertEqual(row["current_delivery_id"], "")
        self.assertFalse(row["delivery_available"])

    def test_archived_carousel_is_excluded(self):
        self.fixture.ready()
        self.fixture.create()
        self.bench.archive_carousel(self.identifier, self.fixture.fixture.body(archived=True))
        before = self.bench.production.path.read_bytes()
        self.assertEqual(self.bench.production_deliveries()["items"], [])
        self.assertEqual(self.bench.production.path.read_bytes(), before)

    def running_carousel(self, *, archived=False):
        data = self.bench.production.read()
        item = data["carousels"][0]
        item["archived"] = archived
        item["searches"] = [{"id": "SRC-111111111111", "status": "running"}]
        item["generations"] = [{"id": "IMG-111111111111", "status": "running"}]
        self.bench.production.write(data)

    def test_catalog_marks_restart_interrupted_carousel_workers_without_writes(self):
        self.running_carousel()
        before = self.bench.production.path.read_bytes()
        with patch.object(self.bench, "edit_live", return_value=None), \
                patch.object(self.bench, "_image_tasks", return_value={}), \
                patch.object(self.bench.production, "mutate", side_effect=AssertionError("catalog wrote data")):
            item = self.bench.production_catalog()["carousels"][0]
        self.assertEqual(item["searches"][0]["status"], "interrupted")
        self.assertEqual(item["generations"][0]["status"], "interrupted")
        self.assertEqual(self.bench.production.path.read_bytes(), before)

    def test_catalog_keeps_actual_carousel_workers_running(self):
        self.running_carousel()
        before = self.bench.production.path.read_bytes()
        live_image = {"IMG-111111111111": {"handle": SimpleNamespace(is_alive=lambda: True), "stage": "generation"}}
        with patch.object(self.bench, "edit_live", return_value={"stage": "search"}), \
                patch.object(self.bench, "_image_tasks", return_value=live_image):
            item = self.bench.production_catalog()["carousels"][0]
        self.assertEqual(item["searches"][0]["status"], "running")
        self.assertEqual(item["generations"][0]["status"], "running")
        self.assertEqual(item["searches"][0]["live"]["stage"], "search")
        self.assertEqual(item["generations"][0]["live"]["stage"], "generation")
        self.assertEqual(self.bench.production.path.read_bytes(), before)

    def test_catalog_skips_repeated_media_checks_for_idle_and_archived_carousels(self):
        with patch.object(self.bench, "carousel_live", side_effect=AssertionError("unneeded search check")), \
                patch.object(self.bench, "carousel_images_live", side_effect=AssertionError("unneeded image check")):
            self.bench.production_catalog()
            self.running_carousel(archived=True)
            self.bench.production_catalog()


if __name__ == "__main__":
    unittest.main()
