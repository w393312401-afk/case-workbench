"""Deterministic text export and strict mask inputs, with no live image engine."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageChops

from carousel_layout import CAPTION_PRESETS, caption_style_prompt, export_caption, fit_caption, paint_caption, validate_mask
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

    def test_rugged_changes_only_letter_effects_and_keeps_the_original_photo_between_them(self):
        source = self.root / "source.png"
        image = Image.new("RGB", (720, 960), (121, 151, 181))
        image.save(source)
        titles = ["Sharp edge.", "Still want more?"]
        for area in ("top", "bottom"):
            with self.subTest(area=area):
                target = self.root / f"rugged-{area}.png"
                receipt = export_caption(source, target, titles, area=area, style="rugged")
                self.assertEqual(receipt["title_lines"], titles)
                self.assertFalse(receipt["background_panel"])
                self.assertGreaterEqual(receipt["title_font_size"], 30, 'phone-width title must remain readable')
                with Image.open(target) as result:
                    self.assertEqual(result.size, image.size)
                    delta = ImageChops.difference(result, image)
                    # The old full-width panel changed both strips, including the source inside the caption band.
                    self.assertIsNone(delta.crop((0, 0, 20, 960)).getbbox())
                    self.assertIsNone(delta.crop((600, 0, 720, 960)).getbbox())
                    top, bottom = receipt["caption_bounds"][1::2]
                    self.assertIsNone(delta.crop((0, top, 720, top + 20)).getbbox())
                    changed = sum(pixel != (0, 0, 0) for pixel in delta.crop((0, top, 720, bottom)).getdata())
                    self.assertGreater(changed, 1000, 'the title and its outline must actually be rendered')
                    self.assertLess(changed, 720 * (bottom - top) * .4, 'space between and around the letters stays original')
                    pixels = set(result.getdata())
                    for color in ((244, 237, 216), (5, 8, 10), (182, 91, 42)):
                        self.assertIn(color, pixels, 'light face, dark outline and warm rim retain contrast')
        plain = self.root / "plain.png"
        receipt = export_caption(source, plain, titles, style="plain")
        self.assertTrue(receipt["background_panel"])
        with Image.open(plain) as result:
            self.assertNotEqual(result.getpixel((0, 0)), image.getpixel((0, 0)), 'plain keeps its existing panel')

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

    def test_four_new_presets_render_distinct_larger_type_without_touching_the_rest_of_the_photo(self):
        source = self.root / "source.png"
        photo = Image.new("RGB", (720, 960), (121, 151, 181))
        photo.save(source)
        titles = ["A HOLDER ISN'T", "THE WHOLE PATH"]
        old = fit_caption(720, 960, titles, style="rugged")
        outputs = set()
        for name in ("yellow_impact", "orange_depth", "red_comic", "lime_pop"):
            with self.subTest(style=name):
                target = self.root / (name + ".png")
                receipt = export_caption(source, target, titles, style=name)
                self.assertEqual(receipt["title_lines"], titles)
                self.assertEqual(receipt["text"], titles)
                self.assertFalse(receipt["background_panel"])
                self.assertGreater(receipt["title_font_size"], old["font_size"])
                with Image.open(target) as result:
                    outputs.add(result.tobytes())
                    self.assertEqual(result.size, photo.size)
                    end = receipt["caption_bounds"][3]
                    self.assertIsNone(ImageChops.difference(result, photo).crop((0, end, 720, 960)).getbbox())
                    self.assertEqual(result.getpixel((0, 0)), photo.getpixel((0, 0)))
                    pixels = set(result.get_flattened_data())
                    preset = CAPTION_PRESETS[name]
                    for key in ("title_color", "emphasis_color", "depth_color"):
                        self.assertIn(preset[key][:3], pixels, key + " must actually render")
                    self.assertIn((5, 8, 10), pixels)
                prompt = caption_style_prompt(name)
                self.assertIn(CAPTION_PRESETS[name]["label"], prompt)
                self.assertIn("大小写", prompt)
                self.assertNotIn("A HOLDER", prompt, 'a reusable preset must not hard-code any slide')
        self.assertEqual(len(outputs), 4)

    def test_styled_lettering_keeps_its_entire_depth_and_slant_inside_measured_bounds(self):
        for name in ("yellow_impact", "orange_depth", "red_comic", "lime_pop"):
            with self.subTest(style=name):
                fit = fit_caption(720, 960, ["Sharp edge.", "Still want more?"], style=name)
                layer = Image.new("RGBA", (720, fit["used"]), (0, 0, 0, 0))
                boxes = paint_caption(layer, fit, 0, align="right")
                alpha = layer.getchannel("A")
                self.assertIsNotNone(alpha.getbbox())
                covered = Image.new("L", layer.size, 0)
                for left, top, right, bottom in boxes:
                    covered.paste(255, (left, top, right, bottom))
                outside = ImageChops.multiply(alpha, ImageChops.invert(covered))
                self.assertIsNone(outside.getbbox(), 'outline, extrusion and italic slant must not overflow')
                self.assertGreaterEqual(alpha.getbbox()[0], fit["margin"])
                self.assertLessEqual(alpha.getbbox()[2], 720 - fit["margin"])
