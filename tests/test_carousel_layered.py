"""Captions on the chosen base: into its caption band, else onto the calmest edge-near stretch; never pads or cuts out."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from carousel_layered import NeedsPlacement, render_caption

TITLE = ["MARK IT.", "CHECK CONTACT."]
BAND = (17, 19, 21)


class CaptionOnBaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save(self, array, name="base.png"):
        Image.fromarray(array).save(self.root / name)
        return self.root / name

    def busy(self, size, seed=3):
        rng = np.random.default_rng(seed)
        return (60 + rng.integers(0, 160, (size[1], size[0], 3))).astype("uint8")

    def render(self, base, **options):
        target = self.root / "out.png"
        receipt = render_caption(base, target, TITLE, style="rugged", **options)
        return receipt, np.array(Image.open(target).convert("RGB"))

    def assert_untouched_outside_caption(self, receipt, made, original):
        top, bottom = receipt["caption_bounds"][1], receipt["caption_bounds"][3]
        outside = np.ones(made.shape[:2], dtype=bool)
        outside[top:bottom] = False
        self.assertTrue(np.array_equal(made[outside], original[outside]))

    def test_caption_goes_into_the_base_caption_band(self):
        original = self.busy((1536, 2048))
        original[:492] = BAND
        layout = {"size": [1536, 2048], "layout": "stack", "panels": [{"rect": [0, 492, 1536, 1556]}]}
        receipt, made = self.render(self.save(original), layout=layout)
        self.assertEqual(receipt["caption_placement"], "band")
        self.assertLessEqual(receipt["caption_bounds"][3], 492 + 40)
        self.assertTrue(np.array_equal(made[492:], original[492:]))

    def test_bottom_band_is_used_too(self):
        original = self.busy((1536, 2048))
        original[1556:] = BAND
        layout = {"size": [1536, 2048], "layout": "single", "panels": [{"rect": [0, 0, 1536, 1556]}]}
        receipt, made = self.render(self.save(original), layout=layout)
        self.assertEqual(receipt["caption_placement"], "band")
        self.assertGreaterEqual(receipt["caption_bounds"][1], 1556 - 40)
        self.assertTrue(np.array_equal(made[:1556], original[:1556]))

    def test_without_a_band_the_calm_edge_wins(self):
        original = self.busy((768, 1024))
        original[760:] = (200, 190, 170)          # a plain table along the bottom; the top is all detail
        receipt, made = self.render(self.save(original), region="edge")
        self.assertEqual(receipt["caption_placement"], "edge")
        self.assertGreaterEqual(receipt["caption_bounds"][1], 760)
        self.assert_untouched_outside_caption(receipt, made, original)

    def test_calm_top_is_used_when_the_bottom_is_busy(self):
        original = self.busy((768, 1024))
        original[:260] = (230, 230, 225)
        receipt, made = self.render(self.save(original), region="edge")
        self.assertLessEqual(receipt["caption_bounds"][3], 260)
        self.assert_untouched_outside_caption(receipt, made, original)

    def test_without_a_band_someone_has_to_pick_the_empty_region(self):
        with self.assertRaises(NeedsPlacement):
            self.render(self.save(self.busy((768, 1024))))

    def test_caption_goes_into_the_picked_empty_region(self):
        original = self.busy((768, 1024))
        region = {"box": [20, 30, 520, 240], "align": "left", "reason": "左上角是空桌面"}
        receipt, made = self.render(self.save(original), region=region)
        self.assertEqual(receipt["caption_placement"], "region")
        x0, y0, x1, y1 = receipt["caption_bounds"]
        self.assertGreaterEqual(x0, round(.02 * 768))
        self.assertLessEqual(x1, round(.52 * 768) + 1)
        self.assertGreaterEqual(y0, round(.03 * 1024))
        self.assertLessEqual(y1, round(.24 * 1024) + 1)
        self.assertEqual(receipt["caption_region_reason"], "左上角是空桌面")
        outside = np.ones(made.shape[:2], dtype=bool)
        outside[y0:y1, x0:x1] = False
        self.assertTrue(np.array_equal(made[outside], original[outside]))

    def test_right_aligned_region_keeps_the_words_on_its_right(self):
        original = self.busy((768, 1024))
        receipt, made = self.render(self.save(original), region={"box": [500, 760, 980, 980], "align": "right", "reason": ""})
        changed = np.flatnonzero((made != original).any(axis=2).any(axis=0))
        self.assertGreater(changed.min(), 500 * 768 / 1000)
        self.assertLessEqual(changed.max(), 980 * 768 / 1000 + 1)

    def test_narrow_region_splits_lines_but_never_joins_them(self):
        original = self.busy((1536, 2048))
        receipt = render_caption(self.save(original), self.root / "out.png", ["SEVEN PLATES", "START WHERE NEEDED"],
                                    style="rugged", region={"box": [10, 145, 340, 305], "align": "left", "reason": ""})
        self.assertEqual(receipt["caption_placement"], "region")
        self.assertEqual(" ".join(receipt["text"]), "SEVEN PLATES START WHERE NEEDED")
        self.assertGreater(len(receipt["text"]), 2)
        self.assertEqual(receipt["text"][0], "SEVEN PLATES")
        self.assertGreaterEqual(receipt["title_font_size"], round(.045 * 1536))

    def test_too_small_a_region_falls_back_to_the_calm_edge(self):
        original = self.busy((768, 1024))
        receipt, _ = self.render(self.save(original), region={"box": [0, 0, 60, 30], "align": "left", "reason": ""})
        self.assertEqual(receipt["caption_placement"], "edge")
        self.assertTrue(receipt["caption_note"])

    def test_base_of_the_requested_aspect_keeps_every_pixel_and_size(self):
        original = self.busy((768, 1024))
        receipt, made = self.render(self.save(original), region="edge")
        self.assertEqual(made.shape, original.shape)
        self.assertFalse(receipt["cropped"])
        self.assertTrue(receipt["preserves_full_frame"])

    def test_old_portrait_frame_is_centre_cropped_without_bars(self):
        original = self.busy((1080, 1920))
        receipt, made = self.render(self.save(original), aspect="3:4", region="edge")
        self.assertEqual(made.shape, (1440, 1080, 3))
        self.assertEqual(receipt["crop_box"], [0, 240, 1080, 1440])
        self.assertTrue(receipt["cropped"])
        self.assert_untouched_outside_caption(receipt, made, original[240:1680])

    def test_landscape_frame_is_centre_cropped_across(self):
        original = self.busy((1920, 1080))
        receipt, made = self.render(self.save(original), aspect="3:4", region="edge")
        self.assertEqual(made.shape, (1080, 810, 3))
        self.assertEqual(receipt["crop_box"], [555, 0, 810, 1080])


if __name__ == "__main__":
    unittest.main()
