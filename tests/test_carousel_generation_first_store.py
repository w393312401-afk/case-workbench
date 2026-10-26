"""Generation-first keeps honest delivery provenance and distinct input contracts."""
import copy
import unittest

import test_production_carousel_images as fixture
from elevenlabs_client import VoiceError
from production_store import carousel_input_signature, carousel_requirements


class GenerationFirstStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselImageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench

    def test_generation_first_invalidates_old_inputs_and_ignores_preserving_fallback(self):
        item = self.fixture.current()
        original = copy.deepcopy(item)
        before = carousel_input_signature(item, item["slides"][0], "3:4")
        item["requirements"] = {**item.get("requirements", {}), "mode": "layout_only",
                                "preserve_slides": [1], "workflow": "generation_first"}
        group = carousel_requirements(item)
        slide = carousel_requirements(item, item["slides"][0])
        self.assertEqual(group["mode"], "generated_visual")
        self.assertNotIn("preserve_slides", group)
        self.assertEqual(slide["mode"], "generated_visual")
        self.assertEqual(slide["workflow"], "generation_first")
        self.assertNotEqual(before, carousel_input_signature(item, item["slides"][0], "3:4"))
        self.assertNotIn("workflow", carousel_requirements(original))

    def test_invalid_workflow_is_a_client_error(self):
        item = self.fixture.current()
        for value in ([], {}, None, 1, "anything"):
            with self.subTest(value=value):
                with self.assertRaises(VoiceError) as error:
                    carousel_requirements({**item, "requirements": {"workflow": value}})
                self.assertEqual(error.exception.status, 400)

    def mark_deferred(self):
        store = self.bench.production
        with store.lock, store.catalog_transaction():
            data = store.read()
            item = next(i for i in data["carousels"] if i["id"] == self.fixture.item["id"])
            for slide in item["slides"]:
                for output in slide.get("outputs", []):
                    output.update(workflow="generation_first", ai_generated=True, review_deferred=True)
            store.write(data)

    def test_package_remembers_generation_without_claiming_review(self):
        self.fixture.generate()
        self.mark_deferred()
        self.fixture.approve_all()
        result = self.bench.create_carousel_delivery(self.fixture.item["id"], self.fixture.body())
        delivery = result["delivery"]
        self.assertEqual(delivery["workflow"], "generation_first")
        self.assertTrue(delivery["review_deferred"])
        for slide in delivery["source"]["slides"]:
            self.assertTrue(slide["output"]["ai_generated"])
            self.assertTrue(slide["output"]["review_deferred"])

    def test_a_later_full_review_clears_deferred_without_regenerating(self):
        self.fixture.generate(slides=[1])
        self.mark_deferred()
        before = self.fixture.current()["slides"][0]["outputs"][-1]
        after = self.fixture.recheck(1)
        self.assertFalse(after["review_deferred"])
        self.assertEqual(after["sha256"], before["sha256"])
        self.assertEqual(after["id"], before["id"])


if __name__ == "__main__":
    unittest.main()
