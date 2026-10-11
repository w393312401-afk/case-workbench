"""Fewer clicks between steps: image settings saved once per product become the default for new carousels."""
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import test_production_carousel as carousel_fixture
from variant_parse import carousel_text, parse_carousels


class ProductDefaultRequirementsTests(carousel_fixture.CarouselTests):
    def new_carousel(self):
        script = self.adopt(carousel_text(parse_carousels(carousel_fixture.CANDIDATE)[0]))
        return self.bench.create_carousel(self.body(carousel={"script_id": script["id"]}))["carousel"]

    def test_saved_settings_become_the_product_default_for_new_carousels(self):
        first = self.new_carousel()
        self.assertNotIn("requirements", first)
        saved = self.bench.save_carousel_requirements(first["id"], self.body(requirements={
            "mode": "generated_visual", "editable_regions": ["background"], "style": "粗犷暖光", "aspect": "9:16",
            "instruction": "只用于这一单", "caption_area": "top"}))["carousel"]["requirements"]
        self.bench.save_production_settings(self.body(settings={"product": "202H", "project": "202H", "code": "202",
                                                                "fps": "29.97", "width": 1080, "height": 1920}))
        second = self.new_carousel()
        self.assertEqual({key: second["requirements"][key] for key in ("mode", "editable_regions", "style", "aspect")},
                         {"mode": "generated_visual", "editable_regions": ["background"], "style": "粗犷暖光", "aspect": "9:16"})
        self.assertEqual(second["image_aspect"], "9:16")
        self.assertEqual(second["requirements"]["instruction"], "")
        self.assertNotIn("caption_area", second["requirements"])
        self.assertEqual(saved["instruction"], "只用于这一单")
        self.assertEqual(second["history"][-1]["note"], "沿用该产品的默认出图设置")


for case, base in ((ProductDefaultRequirementsTests, carousel_fixture.CarouselTests),):
    for name in [name for name in vars(base) if name.startswith("test_")]:
        setattr(case, name, None)
del case, base, name

if __name__ == "__main__":
    unittest.main()
