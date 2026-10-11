"""Deterministic text export and strict mask inputs, with no live image engine."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from carousel_layout import export_caption, validate_mask
from production_store import carousel_input_signature, carousel_requirements
from elevenlabs_client import VoiceError


class LayoutInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="caption-export-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_mask_rejects_wrong_channels_palette_and_dimensions(self):
        for mode, size in (("RGB", (16, 20)), ("P", (16, 20)), ("L", (20, 16))):
            path = self.root / (mode + ".png")
            Image.new(mode, size).save(path)
            with self.subTest(mode=mode, size=size), self.assertRaises(ValueError):
                validate_mask(path, (16, 20))
        correct = self.root / "correct.png"
        Image.new("L", (16, 20), 128).save(correct)
        self.assertEqual(len(validate_mask(correct, (16, 20)).tobytes()), 320)

    def test_export_does_not_crop_subject_or_change_uncovered_full_width(self):
        source = self.root / "source.png"
        # Unequal pixels catch accidental stretching, cropping or subject compositing.
        image = Image.new("RGB", (240, 320))
        image.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(320) for x in range(240)])
        image.save(source)
        target = self.root / "final.png"
        receipt = export_caption(source, target, ["Full frame"], "Exact words.", area="top")
        with Image.open(target) as exported:
            self.assertEqual(exported.size, image.size)
            self.assertEqual(exported.crop((0, 150, 240, 320)).tobytes(), image.crop((0, 150, 240, 320)).tobytes())
        self.assertTrue(receipt["preserves_full_frame"])
        self.assertEqual(receipt["text"], ["Full frame", "Exact words."])

    def test_caption_area_is_explicit_and_export_fails_before_overflow(self):
        source = self.root / "source.png"
        Image.new("RGB", (100, 120)).save(source)
        with self.assertRaises(ValueError):
            export_caption(source, self.root / "invalid.png", ["Title"], area="auto")
        with self.assertRaises(ValueError):
            export_caption(source, self.root / "overflow.png", ["A" * 300], area="top")
        self.assertFalse((self.root / "overflow.png").exists())

    def test_rugged_style_draws_real_type_and_preserves_locked_words_and_full_frame(self):
        source = self.root / "source.png"
        image = Image.new("RGB", (720, 960))
        image.putdata([(x % 256, y % 256, (x * 3 + y) % 256) for y in range(960) for x in range(720)])
        image.save(source)
        titles, body = ["A useful gift.", "Built for the workbench."], "Original body: 202H, 15° and 600 grit."
        receipts = {}
        for name in ("plain", "rugged"):
            target = self.root / (name + ".png")
            receipt = receipts[name] = export_caption(source, target, titles, body, area="bottom", style=name)
            self.assertEqual(receipt["caption_style"], name)
            self.assertEqual(receipt["title_lines"], titles)
            self.assertEqual(receipt["body"], body)
            self.assertEqual(" ".join(receipt["text"][len(titles):]), body)
            self.assertGreaterEqual(receipt["caption_bounds"][1], round(960 * .70))
            with Image.open(target) as result:
                self.assertEqual(result.size, image.size)
                self.assertEqual(result.crop((0, 0, 720, round(960 * .70))).tobytes(),
                                 image.crop((0, 0, 720, round(960 * .70))).tobytes())
        with Image.open(self.root / "plain.png") as plain, Image.open(self.root / "rugged.png") as rugged:
            self.assertNotEqual(plain.tobytes(), rugged.tobytes())
            pixels = set(rugged.get_flattened_data() if hasattr(rugged, "get_flattened_data") else rugged.getdata())
            self.assertIn((244, 237, 216), pixels)
            self.assertIn((182, 91, 42), pixels)
        if Path("C:/Windows/Fonts/impact.ttf").is_file():
            self.assertEqual(receipts["rugged"]["title_font"], "Impact")
        with self.assertRaises(ValueError):
            export_caption(source, self.root / "unknown.png", titles, body, style="unknown")

    def test_caption_preset_is_opt_in_and_changes_the_input_signature(self):
        slide = {"number": 1, "title_lines": ["Locked title"], "body": "Locked body", "base": {"id": "CAN-0123456789ab"}}
        item = {"script_id": "SCR-0123456789ab", "script_version": 1,
                "requirements": {"mode": "layout_only", "caption_area": "top"}}
        self.assertNotIn("caption_style", carousel_requirements(item))
        original = carousel_input_signature(item, slide, "3:4")
        plain = {**item, "requirements": {**item["requirements"], "caption_style": "plain"}}
        rugged = {**item, "requirements": {**item["requirements"], "caption_style": "rugged"}}
        self.assertNotEqual(original, carousel_input_signature(plain, slide, "3:4"))
        self.assertNotEqual(carousel_input_signature(plain, slide, "3:4"), carousel_input_signature(rugged, slide, "3:4"))
        with self.assertRaises(VoiceError):
            carousel_requirements({**item, "requirements": {**item["requirements"], "caption_style": "arbitrary"}})
