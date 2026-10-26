"""New photo packages preserve selected image provenance without rewriting old packages."""
import copy
import unittest

import test_production_carousel_delivery as fixture


class DeliveryProvenanceSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_source_snapshot_matches_packaged_versions_and_remains_frozen_after_new_images(self):
        self.fixture.ready()
        item = self.fixture.current()
        first = self.fixture.create()["delivery"]
        snapshot = copy.deepcopy(first["source"])
        self.assertEqual(snapshot["kind"], "carousel")
        self.assertEqual(snapshot["carousel_id"], item["id"])
        self.assertEqual(snapshot["script"]["script_id"], first["script_id"])
        self.assertEqual(snapshot["script"]["script_version"], first["script_version"])
        for saved, packaged in zip(snapshot["slides"], first["slides"]):
            self.assertEqual((saved["output"]["id"], saved["output"]["sha256"]),
                             (packaged["output_id"], packaged["sha256"]))
            self.assertGreater(saved["output"]["version"], 0)
            self.assertNotIn("file", saved["output"])
            self.assertNotIn("path", saved["output"])
            original = next(slide for slide in item["slides"] if slide["number"] == saved["number"])
            self.assertEqual(saved["title_lines"], original["title_lines"])
            self.assertEqual(saved["body"], original["body"])
        self.fixture.fixture.generate(slides=[2])
        self.fixture.fixture.approve(2, self.fixture.current()["slides"][1]["outputs"][-1]["id"])
        second = self.fixture.create()["delivery"]
        self.assertNotEqual(second["id"], first["id"])
        persisted = next(row for row in self.fixture.current()["deliveries"] if row["id"] == first["id"])
        self.assertEqual(persisted["source"], snapshot)
        self.assertEqual(persisted["slides"], first["slides"])
        self.assertEqual(persisted["source"]["slides"][1]["output"]["version"], 1)
        self.assertEqual(second["source"]["slides"][1]["output"]["version"], 2)

    def test_legacy_package_without_snapshot_is_readable_and_not_rewritten_when_reused(self):
        self.fixture.ready()
        first = self.fixture.create()["delivery"]
        catalog = self.fixture.bench.production.read()
        legacy = catalog["carousels"][0]["deliveries"][0]
        legacy.pop("source")
        self.fixture.bench.production.write(catalog)
        before = copy.deepcopy(legacy)
        result = self.fixture.create()["delivery"]
        self.assertEqual(result["id"], first["id"])
        self.assertNotIn("source", result)
        self.assertEqual(self.fixture.current()["deliveries"][0], before)


if __name__ == "__main__":
    unittest.main()
