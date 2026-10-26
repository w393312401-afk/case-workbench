"""多图合成: the AI plans from the slide's 逐张表 row, the workbench pastes the real pictures (AI faked)."""
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))

from PIL import Image

from carousel_compose import CANVAS, compose, extract_plan, normalize_plan
from elevenlabs_client import VoiceError
import test_production_carousel_stills as stills
from workbench_carousel_images import composite_rule, direct_image_prompt


def picture(color, size=(600, 800)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


class Planner:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def run(self, prompt, *, cwd, **options):
        self.calls.append({"prompt": prompt, "cwd": cwd, "files": sorted(path.name for path in Path(cwd).glob("img*.jpg")), **options})
        return self.answer


class ComposeTests(unittest.TestCase):
    carousel = stills.StillTests.carousel

    def setUp(self):
        stills.StillTests.setUp(self)
        for color in ("red", "blue", "green"):
            self.bench.import_uploaded_image(self.item["id"], 1, f"{color}.png", picture(color), uuid4().hex)
        self.candidates = [entry["id"] for entry in self.bench.production.carousel(self.item["id"])["slides"][0]["candidates"]]

    def plan(self, answer):
        planner = Planner(answer)
        self.bench.ai = SimpleNamespace(resolve_default_settings=lambda body, effort_key: {"model": "claude-opus-5-5", effort_key: "high"},
                                        bridge=planner)
        return planner

    def test_stack_plan_becomes_a_new_candidate_and_keeps_the_sources(self):
        planner = self.plan('前后对比用上下拼。\n```json\n{"layout": "stack", "panels": [{"file": "img2.jpg", "focus_y": 0.4}, '
                            '{"file": "img1.jpg", "fit": "contain"}], "caption_space": "top", "reason": "先看钝刀再看磨好"}\n```')
        result = self.bench.compose_candidates(self.item["id"], {"request_id": uuid4().hex, "slide": 1,
                                                                 "candidates": self.candidates[:2]})
        candidate = result["candidate"]
        self.assertEqual((candidate["origin"], candidate["layout"], candidate["composed_from"]),
                         ("composite", "stack", [self.candidates[1], self.candidates[0]]))
        self.assertIn("上下拼", candidate["reason"])
        slide = self.bench.production.carousel(self.item["id"])["slides"][0]
        self.assertEqual(len(slide["candidates"]), 4)
        with Image.open(self.bench.carousel_image(self.item["id"], candidate["id"], "full")) as image:
            self.assertEqual(image.size, CANVAS["3:4"])
            self.assertEqual(image.getpixel((768, 20))[:3], (0, 0, 255))  # top row: img2 (blue) from the very top, no caption band
            self.assertEqual(image.getpixel((768, 1900))[:3], (255, 0, 0))  # bottom row: img1 (red), contained
        self.assertEqual(candidate["plan"]["caption_space"], "none")
        call = planner.calls[0]
        self.assertEqual(call["files"], ["img1.jpg", "img2.jpg"])
        self.assertIn("第 1 张｜作用：钩子", call["prompt"])
        self.assertEqual(call["model"], "claude-opus-5-5")

    def test_single_verdict_recommends_without_adding(self):
        self.plan('```json\n{"layout": "single", "panels": [{"file": "img3.jpg"}], "reason": "一张就清楚"}\n```')
        result = self.bench.compose_candidates(self.item["id"], {"request_id": uuid4().hex, "slide": 1})
        self.assertIsNone(result["candidate"])
        self.assertEqual(result["recommended"], self.candidates[2])
        self.assertEqual(len(self.bench.production.carousel(self.item["id"])["slides"][0]["candidates"]), 3)

    def test_bad_plans_and_requests_are_refused_and_release_the_slide(self):
        self.plan('```json\n{"layout": "grid", "panels": [{"file": "img1.jpg"}, {"file": "nope.jpg"}]}\n```')
        with self.assertRaises(VoiceError) as caught:
            self.bench.compose_candidates(self.item["id"], {"request_id": uuid4().hex, "slide": 1})
        self.assertEqual(caught.exception.status, 502)
        self.assertFalse(self.bench._compose_busy)
        for body in ({"slide": 1, "candidates": self.candidates[:1]}, {"slide": 2}, {"slide": 1, "candidates": ["CND-000000000000", self.candidates[0]]}):
            with self.subTest(body=body), self.assertRaises(VoiceError) as caught:
                self.bench.compose_candidates(self.item["id"], {"request_id": uuid4().hex, **body})
            self.assertEqual(caught.exception.status, 400)

    def test_composite_base_tells_the_generator_to_keep_the_panels(self):
        self.assertEqual(composite_rule({"base": {"origin": "upload"}}), "")
        rule = composite_rule({"base": {"origin": "composite", "name": "AI 合成 · 上下拼", "plan": {"panels": [1, 2]}}})
        self.assertIn("2 张实拍", rule)
        snapshot = {"aspect": "3:4", "requirements": {"mode": "photo_preserving"},
                    "slide": {"title_lines": ["A"], "base": {"origin": "composite", "plan": {"panels": [1, 2, 3]}}}}
        self.assertIn("3 张实拍按版式拼成", direct_image_prompt(snapshot))


class LayoutTests(unittest.TestCase):
    def test_every_layout_fills_the_canvas(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            sources = {}
            for index, size in enumerate(((800, 600), (600, 800), (1080, 1920), (500, 500)), 1):
                sources[f"img{index}.jpg"] = folder / f"img{index}.jpg"
                Image.new("RGB", size, (index * 50, 0, 0)).save(sources[f"img{index}.jpg"])
            for layout, count in (("single", 1), ("stack", 3), ("side_by_side", 2), ("main_inset", 3), ("grid", 3), ("grid", 4)):
                for aspect in ("3:4", "9:16"):
                    plan = normalize_plan({"layout": layout, "panels": [{"file": name} for name in list(sources)[:count]],
                                           "caption_space": "bottom"}, set(sources))
                    receipt = compose(plan, sources, folder / f"{layout}-{count}-{aspect}.png", aspect)
                    self.assertEqual(len(receipt["panels"]), count)
                    with Image.open(folder / f"{layout}-{count}-{aspect}.png") as image:
                        self.assertEqual(image.size, CANVAS[aspect])
                    # A requested caption band is ignored: the photos reach the top and bottom edges.
                    self.assertEqual(plan["caption_space"], "none")
                    self.assertEqual(min(rect[1] for rect in (panel["rect"] for panel in receipt["panels"])), 0)
                    self.assertEqual(max(rect[1] + rect[3] for rect in (panel["rect"] for panel in receipt["panels"])), CANVAS[aspect][1])

    def test_plan_parsing(self):
        self.assertEqual(extract_plan('x {"a": 1} y\n```json\n{"layout": "stack"}\n```')["layout"], "stack")
        with self.assertRaises(ValueError):
            extract_plan("没有方案")
        with self.assertRaises(ValueError):
            normalize_plan({"layout": "stack", "panels": [{"file": "img1.jpg"}, {"file": "img1.jpg"}]}, {"img1.jpg"})


if __name__ == "__main__":
    unittest.main()
