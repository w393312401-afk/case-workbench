"""Content image readback reuses metadata while retaining fresh file checks."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from PIL import Image

import test_production_carousel_images as fixture
from elevenlabs_client import VoiceError


class ContentFilterReadbackTests(unittest.TestCase):
    setUp = fixture.CarouselImageTests.setUp
    stop = fixture.CarouselImageTests.stop
    carousel = fixture.CarouselImageTests.carousel
    body = fixture.CarouselImageTests.body
    current = fixture.CarouselImageTests.current
    pick_new_base = fixture.CarouselImageTests.pick_new_base
    wait = fixture.CarouselImageTests.wait
    start = fixture.CarouselImageTests.start
    generate = fixture.CarouselImageTests.generate
    approve = fixture.CarouselImageTests.approve

    def test_all_slide_checks_reuse_the_supplied_snapshot_without_rereading_the_order(self):
        self.generate()
        snapshot = self.current()
        original = copy.deepcopy(snapshot)
        with patch.object(self.bench.production, "carousel", side_effect=AssertionError("redundant full order read")):
            result = self.bench.carousel_images_live(snapshot)
        self.assertTrue(all(not output["stale"] for slide in result["slides"] for output in slide["outputs"]))
        self.assertEqual(self.current(), original, "Readback must only annotate the supplied snapshot")

    def test_changed_base_bytes_are_checked_again_for_the_next_filter_readback(self):
        self.generate(slides=[1])
        self.approve(1)
        snapshot = self.current()
        before = self.bench.carousel_images_live(copy.deepcopy(snapshot))
        self.assertTrue(before["slides"][0]["outputs"][0]["approved"])
        slide = snapshot["slides"][0]
        path = self.bench.carousel_image(snapshot["id"], slide["base"]["id"], "full")
        Image.new("RGB", (108, 192), "purple").save(path)
        with patch.object(self.bench.production, "carousel", side_effect=AssertionError("redundant full order read")):
            after = self.bench.carousel_images_live(copy.deepcopy(snapshot))
        output = after["slides"][0]["outputs"][0]
        self.assertTrue(output["stale"])
        self.assertFalse(output["approved"])

    def test_cached_metadata_never_bypasses_missing_candidates_or_containment(self):
        snapshot = self.current()
        slide = snapshot["slides"][0]
        candidate_id = slide["base"]["id"]
        original = self.bench.carousel_image(snapshot["id"], candidate_id, "full")
        self.assertEqual(self.bench._base_path(snapshot["id"], slide, item=snapshot), original)
        candidate = next(row for row in slide["candidates"] if row["id"] == candidate_id)
        outside = original.parent.parent.parent / "outside.png"
        Image.new("RGB", (108, 192), "blue").save(outside)
        candidate["file"] = str(outside.resolve())
        with self.assertRaises(VoiceError):
            self.bench._base_path(snapshot["id"], slide, item=snapshot)
        slide["candidates"] = []
        with self.assertRaises(VoiceError):
            self.bench._base_path(snapshot["id"], slide, item=snapshot)
        original.unlink()
        with self.assertRaises(VoiceError):
            self.bench.carousel_image(snapshot["id"], candidate_id, "full")


if __name__ == "__main__":
    unittest.main()
