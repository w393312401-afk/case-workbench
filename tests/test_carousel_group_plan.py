"""Group plan (route, selling-point tags, stage and shot per slide, music) and the AI group review, with no live engines."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_production_carousel_images as images_fixture
from test_production_carousel_images import ImageBench, ImageGenerator
from codex_bridge import BridgeCancelled
from carousel_plan import GROUP_CHECKS, GROUP_REVIEW_SCHEMA, normalize_group_review, structure_checks
from variant_parse import parse_carousels
from workbench_carousel_group import CarouselGroupMixin


def table(copy):
    return "\n".join(["**图文版**", "", "| 张 | 环节 | 景别 | 作用 | 叠字 | 正文 | 中文 | 素材来源 | 禁写 |",
                      "|---|---|---|---|---|---|---|---|---|",
                      "| 1 | 首图钩子 | 近景 | 钩子 | Hook 1 | Body 1. | 标题1 | 实拍 | — |",
                      "| 2 | 中间展示 | 特写 | 机制 | Hook 2 | Body 2. | 标题2 | 实拍 | — |",
                      "| 3 | 尾图转化 | 使用场景 | 收口 | Hook 3 | Body 3. | 标题3 | 实拍 | — |", "",
                      "```json", json.dumps(copy, ensure_ascii=False), "```", ""])


def copy_json(**extra):
    return {"content_type": "photo_carousel",
            "slides": [{"number": n, "title_lines": [f"Hook {n}"], "body": f"Body {n}.", "title_zh": f"标题{n}", "body_zh": ""}
                       for n in (1, 2, 3)], "caption_en": "Caption.", "caption_zh": "文案。", **extra}


class GroupReviewer:
    """A read-only fake that answers the group schema; `mode` picks a complete, incomplete or blocking answer."""

    def __init__(self):
        self.calls, self.mode = [], "pass"
        self.entered, self.release = threading.Event(), threading.Event()

    def status(self):
        return {"available": True, "label": "Fake reviewer", "review_available": True}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, timeout=None, schema=None, read_only=False, **_):
        self.calls.append({"prompt": prompt, "images": list(images), "schema": schema, "read_only": read_only})
        on_progress("正在看整组")
        if self.mode == "block":
            self.entered.set()
            while not self.release.wait(.01):
                if cancel_event.is_set():
                    raise BridgeCancelled("cancelled")
        numbers = [slide["number"] for slide in json.loads((Path(cwd) / "input.json").read_text(encoding="utf-8"))["slides"]]
        checks = [{"id": key, "status": "pass", "note": "已看"} for key in GROUP_CHECKS]
        if self.mode == "incomplete":
            checks = checks[:3]
        if self.mode == "fail":
            checks = [{"id": "delete_test", "status": "fail", "note": "第 2 张去掉影响很小"}
                      if check["id"] == "delete_test" else check for check in checks]
        return json.dumps({"summary": "整组可读", "checks": checks, "redundant_slides": [2] if self.mode == "fail" else [],
                           "slides": [{"number": n, "observed_shot": "close", "note": "看清了"} for n in numbers],
                           "suggestions": ["第 2 张换成使用场景"] if self.mode == "fail" else []}, ensure_ascii=False)


class GroupBench(ImageBench, CarouselGroupMixin):
    pass


class PlanParseTests(unittest.TestCase):
    def test_copy_json_plan_and_slide_fields_are_parsed_with_table_labels_as_fallback(self):
        found = parse_carousels(table(copy_json(plan={"route": "product", "tags": [{"en": "7 plates", "zh": "7 块磨板"}],
                                                      "music": "利落器乐"})))[0]
        self.assertEqual(found["plan"], {"route": "product", "tags": [{"en": "7 plates", "zh": "7 块磨板"}], "music": "利落器乐"})
        self.assertEqual([(s["stage"], s["shot"]) for s in found["slides"]],
                         [("hook", "close"), ("show", "detail"), ("convert", "in_use")])
        self.assertEqual(found["problems"], [])

    def test_unknown_values_are_reported_and_extra_tags_trimmed(self):
        slides = copy_json()["slides"]
        slides[0]["shot"] = "drone"
        found = parse_carousels(table(copy_json(slides=slides, plan={"route": "unboxing", "tags": ["a", "b", "c", "d"]})))[0]
        self.assertEqual(found["plan"]["route"], "")
        self.assertEqual(len(found["plan"]["tags"]), 3)
        self.assertEqual(found["slides"][0]["shot"], "")
        self.assertEqual(len(found["problems"]), 3, found["problems"])

    def test_old_candidates_without_a_plan_parse_unchanged(self):
        from test_production_carousel import CANDIDATE
        found = parse_carousels(CANDIDATE)[0]
        self.assertEqual(found["plan"], {"route": "", "tags": [], "music": ""})
        self.assertTrue(all(s["stage"] == "" and s["shot"] == "" for s in found["slides"]))


class StructureTests(unittest.TestCase):
    def item(self, shots=("close", "detail", "in_use"), stages=("hook", "show", "convert"), tags=1, route="product", music="x"):
        return {"plan": {"route": route, "tags": [{"en": f"t{n}", "zh": ""} for n in range(tags)], "music": music},
                "slides": [{"number": n, "stage": stage, "shot": shot} for n, (stage, shot) in enumerate(zip(stages, shots), 1)]}

    def status(self, item):
        return {check["id"]: check["status"] for check in structure_checks(item)}

    def test_a_planned_group_passes_every_structure_check(self):
        self.assertEqual(set(self.status(self.item()).values()), {"pass"})

    def test_flat_only_missing_hook_and_close_and_tag_count_are_flagged(self):
        status = self.status(self.item(shots=("flat",) * 3, stages=("show",) * 3, tags=0, route="", music=""))
        self.assertEqual(status, {"count": "pass", "route": "warn", "tags": "warn", "hook": "warn", "convert": "warn",
                                  "shots": "warn", "music": "info"})
        self.assertEqual(self.status(self.item(shots=("close", "", "flat")))["shots"], "warn")


class GroupReviewNormalizeTests(unittest.TestCase):
    def test_every_check_and_slide_once_or_the_review_is_incomplete(self):
        answer = {"summary": "", "checks": [{"id": key, "status": "pass", "note": ""} for key in GROUP_CHECKS] +
                  [{"id": "hook", "status": "fail", "note": "duplicate is ignored"}],
                  "slides": [{"number": 1, "observed_shot": "wide", "note": ""}, {"number": 9, "observed_shot": "wide", "note": ""}],
                  "redundant_slides": [1, 9], "suggestions": []}
        review = normalize_group_review(json.dumps(answer), [1])
        self.assertTrue(review["completed"])
        self.assertEqual(review["status"], "pass")
        self.assertEqual(review["redundant_slides"], [1])
        self.assertFalse(normalize_group_review(json.dumps(answer), [1, 2])["completed"])
        self.assertFalse(normalize_group_review("not json", [1])["completed"])


class GroupPlanTests(unittest.TestCase):
    """Borrows the image tests' helpers (not their tests): a three-slide carousel with a base picked for every slide."""
    _images = images_fixture.CarouselImageTests
    stop, carousel, body, current, pick_new_base, wait, generate, assert_error, start = (
        _images.stop, _images.carousel, _images.body, _images.current, _images.pick_new_base, _images.wait,
        _images.generate, _images.assert_error, _images.start)

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="carousel-group-")
        self.addCleanup(temp.cleanup)
        mcp = Path(temp.name) / "ResolveMCP.exe"
        mcp.write_bytes(b"")
        environment = patch.dict(os.environ, {"RESOLVE_MCP": str(mcp)})
        environment.start()
        self.addCleanup(environment.stop)
        self.generator = ImageGenerator()
        self.reviewer = GroupReviewer()
        self.bench = GroupBench(temp.name, self.generator)
        self.bench.image_reviewer = self.reviewer
        self.addCleanup(self.stop)
        self.addCleanup(self.reviewer.release.set)
        self.item = self.carousel()
        self.originals = {}
        for number in (1, 2, 3):
            self.pick_new_base(number)

    def detail(self):
        return self.bench.carousel_group_live(self.current())

    def save_plan(self, **plan):
        body = {"route": "product", "tags": [{"en": "7 plates", "zh": "7 块磨板"}], "music": "利落器乐", **plan}
        slides = [{"number": n, "stage": stage, "shot": shot}
                  for n, stage, shot in ((1, "hook", "close"), (2, "show", "detail"), (3, "convert", "in_use"))]
        return self.bench.save_carousel_plan(self.item["id"], self.body(plan=body, slides=slides))["carousel"]

    def review(self):
        self.bench.start_carousel_group_review(self.item["id"], self.body())
        self.wait()
        return self.detail()["group_reviews"][-1]

    def test_a_new_carousel_starts_with_an_empty_plan_and_saving_it_keeps_image_signatures(self):
        item = self.current()
        self.assertEqual(item["plan"], {"route": "", "tags": [], "music": ""})
        self.assertEqual(item["group_reviews"], [])
        self.generate(slides=[1])
        before = self.current()["slides"][0]["outputs"][-1]["input_signature"]
        saved = self.save_plan()
        self.assertEqual([(s["stage"], s["shot"]) for s in saved["slides"]],
                         [("hook", "close"), ("show", "detail"), ("convert", "in_use")])
        live = self.bench.carousel_images_live(self.current())
        self.assertEqual(live["slides"][0]["outputs"][-1]["input_signature"], before)
        self.assertFalse(live["slides"][0]["outputs"][-1]["stale"])
        checks = {check["id"]: check["status"] for check in self.detail()["group_check"]["structure"]}
        self.assertEqual(set(checks.values()), {"pass"})

    def test_invalid_plans_are_refused(self):
        for plan in ({"route": "unboxing"}, {"tags": [{"en": str(n)} for n in range(4)]}, {"music": "x" * 301}):
            with self.subTest(plan=plan):
                self.assert_error(400, self.save_plan, **plan)
        self.assert_error(400, self.bench.save_carousel_plan, self.item["id"],
                          self.body(plan={}, slides=[{"number": 1, "stage": "middle", "shot": ""}] * 3))
        self.assert_error(409, self.bench.save_carousel_plan, self.item["id"],
                          {"request_id": uuid4().hex, "revision": "stale", "plan": {}, "slides": []})

    def test_group_review_reads_every_slide_in_order_and_goes_stale_when_the_group_changes(self):
        self.save_plan()
        run = self.review()
        call = self.reviewer.calls[-1]
        self.assertTrue(call["read_only"])
        self.assertIs(call["schema"], GROUP_REVIEW_SCHEMA)
        self.assertEqual([Path(path).name.split(".")[0] for path in call["images"]], ["slide-01", "slide-02", "slide-03"])
        self.assertIn("7 plates", call["prompt"])
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["review"]["status"], "uncertain")  # legacy input has no frozen meaning-group mapping
        self.assertTrue(run["current"])
        self.assertEqual(self.detail()["group_check"]["base_only"], [1, 2, 3])
        self.save_plan(music="换成轻快的原声吉他")
        self.assertFalse(self.detail()["group_reviews"][-1]["current"])
        self.assertIn("group_review", [entry["event"] for entry in self.current()["history"]])

    def test_failed_checks_and_incomplete_answers_are_kept_apart(self):
        self.reviewer.mode = "fail"
        run = self.review()
        self.assertEqual((run["status"], run["review"]["status"], run["review"]["redundant_slides"]), ("completed", "fail", [2]))
        self.reviewer.mode = "incomplete"
        run = self.review()
        self.assertEqual(run["status"], "failed")
        self.assertIn("完整结果", run["error"])

    def test_a_slide_without_any_image_blocks_the_review_and_one_review_runs_at_a_time(self):
        self.bench.choose_base(self.item["id"], self.body(slide=2, candidate=None))
        self.assert_error(409, self.bench.start_carousel_group_review, self.item["id"], self.body())
        self.pick_new_base(2)
        self.reviewer.mode = "block"
        self.bench.start_carousel_group_review(self.item["id"], self.body())
        self.assertTrue(self.reviewer.entered.wait(3))
        self.assert_error(409, self.bench.start_carousel_group_review, self.item["id"], self.body())
        self.assert_error(409, self.bench.start_image_generation, self.item["id"], self.body(slides=[1]))
        running = self.detail()["group_reviews"][-1]
        self.assertEqual(running["status"], "running")
        self.bench.cancel_image_generation(self.item["id"], running["id"])
        self.wait()
        self.assertEqual(self.detail()["group_reviews"][-1]["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
