"""Generation-first plans the real image calls once, without fidelity loops or caption fallbacks."""
import copy
import json
from pathlib import Path
import threading
import unittest

from codex_bridge import BridgeCancelled
from production_store import carousel_input_signature
import test_production_carousel_images as fixture


class PromptPlanner:
    review_available = True

    def __init__(self):
        self.calls = []
        self.mode = "valid"
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def status(self):
        return {"available": True, "label": "Fake visual planner", "review_available": True}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, schema, read_only, **kwargs):
        snapshot = json.loads((Path(cwd) / "input.json").read_text(encoding="utf-8"))
        self.calls.append({"prompt": prompt, "cwd": Path(cwd), "images": images,
                           "snapshot": snapshot, "schema": schema, "read_only": read_only, **kwargs})
        self.entered.set()
        if self.block:
            while not self.release.wait(.01):
                if cancel_event.is_set():
                    raise BridgeCancelled("cancelled")
        plan = {"summary": "按买点推进", "style": "自然厨房场景，柔和侧光，统一暖色与字体",
                "slides": [{"number": slide["number"], "concept": f"第{slide['number']}张展示独立卖点与新构图",
                            "prompt": "完整生成新场景，商品置于厨房前景，自然侧光。英文标题：" +
                                      " / ".join(slide["title_lines"]) + "。正文：" + slide.get("body", "")}
                           for slide in snapshot["slides"]]}
        if self.mode == "missing":
            plan["slides"].pop()
        elif self.mode == "duplicate":
            plan["slides"][-1] = copy.deepcopy(plan["slides"][0])
        elif self.mode == "empty":
            plan["slides"][0]["prompt"] = ""
        return json.dumps(plan, ensure_ascii=False)


class CarouselPromptPlanningTests(unittest.TestCase):
    # Reuse the disposable catalog fixture without inheriting and rerunning its suite.
    carousel = fixture.CarouselImageTests.carousel
    body = fixture.CarouselImageTests.body
    current = fixture.CarouselImageTests.current
    pick_new_base = fixture.CarouselImageTests.pick_new_base
    start = fixture.CarouselImageTests.start
    wait = fixture.CarouselImageTests.wait
    generate = fixture.CarouselImageTests.generate
    stop = fixture.CarouselImageTests.stop

    def setUp(self):
        fixture.CarouselImageTests.setUp(self)
        self.planner = self.bench.image_reviewer = PromptPlanner()
        self.addCleanup(self.planner.release.set)
        self.generator.direct_image_edit = True
        self.generator.review_available = False
        self.generator.source = "cockpit_image_edit"

    def test_one_visual_plan_precedes_full_group_image_calls_and_saves_actual_prompts(self):
        self.bench.edit_skill_dir = Path("missing-photo-skill")
        item = self.generate(workflow="generation_first", redo=True)
        run = item["generations"][-1]
        self.assertEqual((run["status"], run["added"]), ("completed", 3))
        self.assertEqual((len(self.planner.calls), len(self.generator.calls)), (1, 3))
        planning = self.planner.calls[0]
        self.assertTrue(planning["read_only"])
        self.assertEqual(planning["reasoning_effort"], "low")
        self.assertEqual(len(planning["images"]), 3)
        self.assertEqual(planning["snapshot"]["workflow"], "generation_first")
        root = self.bench._carousel_dir(item["id"])
        self.assertTrue((root / run["prompt_plan_file"]).is_file())
        for slide in item["slides"]:
            output = slide["outputs"][-1]
            call = next(value for value in self.generator.calls if value["snapshot"]["slide"]["number"] == slide["number"])
            self.assertFalse(call["read_only"])
            self.assertNotIn("reasoning_effort", call)
            self.assertEqual(output["generation_prompt"], call["prompt"])
            self.assertEqual((call["cwd"] / "prompt.txt").read_text(encoding="utf-8"), call["prompt"])
            self.assertEqual(output["prompt_plan"]["source"], "ai_visual_planner")
            self.assertTrue(output["ai_generated"])
            self.assertEqual(output["workflow"], "generation_first")
            self.assertTrue(output["review_deferred"])
            self.assertEqual(output["review"]["status"], "deferred")
            self.assertFalse(output["review"]["completed"])
            self.assertFalse(output["approved"])
            self.assertNotIn("review_runs", output)
            self.assertEqual(output["input_signature"], carousel_input_signature(item, slide, output["aspect"]))
            self.assertIn("完整的 AI 生成", call["prompt"])
            self.assertNotIn('<skill name="tiktok-photo-commerce">', call["prompt"])
            self.assertNotIn("手部没有获准改变", call["prompt"])
            self.assertNotIn("保留本张商品的形状", call["prompt"])
            for title in slide["title_lines"]:
                self.assertIn(title, call["prompt"])
            self.assertEqual(run["stages"][str(slide["number"])]["review"]["status"], "skipped")
        self.assertFalse((root / "reviews").exists())

    def test_generation_first_overrides_old_preservation_and_layout_without_skill_loading(self):
        self.generator.direct_image_edit = False
        self.bench.edit_skill_dir = Path("missing-photo-skill")
        item = self.generate(slides=[1], redo=True, workflow="generation_first",
                             requirements={"mode": "layout_only", "caption_area": "top", "preserve_slides": [1]})
        self.assertEqual(item["generations"][-1]["status"], "completed")
        self.assertEqual((len(self.planner.calls), len(self.generator.calls)), (1, 1))
        output = item["slides"][0]["outputs"][-1]
        self.assertNotEqual(output["origin"], "local_layout")
        self.assertIsNone(output["layout_receipt"])
        self.assertEqual(output["requirements_snapshot"]["mode"], "generated_visual")
        self.assertNotIn("channel", self.generator.calls[0]["snapshot"])
        self.assertIn("实际调用当前可用的内置 imagegen", self.generator.calls[0]["prompt"])
        self.assertEqual(output["review"]["status"], "deferred")

    def test_malformed_plans_do_not_generate_or_fall_back_to_caption_layout(self):
        for mode in ("missing", "duplicate", "empty"):
            with self.subTest(mode=mode):
                self.planner.mode = mode
                item = self.generate(workflow="generation_first", redo=True)
                self.assertEqual(item["generations"][-1]["status"], "failed")
                self.assertIn("提示词", item["generations"][-1]["error"])
                self.assertEqual(self.generator.calls, [])
                self.assertTrue(all(not slide["outputs"] for slide in item["slides"]))

    def test_cancel_planning_stops_before_any_image_calls(self):
        self.planner.block = True
        run = self.start(workflow="generation_first", redo=True)
        self.assertTrue(self.planner.entered.wait(2))
        self.assertEqual(self.generator.calls, [])
        self.bench.cancel_image_generation(self.item["id"], run["id"])
        item = self.wait()
        self.assertEqual(item["generations"][-1]["status"], "cancelled")
        self.assertEqual(self.generator.calls, [])

    def test_returning_reference_bytes_is_not_an_ai_generated_picture(self):
        self.generator.file_mode = "copy"
        item = self.generate(workflow="generation_first", redo=True, slides=[1])
        self.assertEqual(item["generations"][-1]["status"], "failed")
        self.assertEqual(item["slides"][0]["outputs"], [])

    def test_saved_workflow_is_used_on_resume_without_repeating_request_flag(self):
        self.generate(workflow="generation_first", redo=True, slides=[1])
        item = self.generate(redo=True, slides=[2, 3], requirements={"style": "明亮自然光"})
        self.assertEqual(item["generations"][-1]["status"], "completed")
        self.assertEqual(len(self.planner.calls), 2)
        self.assertEqual([slide["number"] for slide in self.planner.calls[-1]["snapshot"]["slides"]], [2, 3])
        self.assertTrue(all(slide["outputs"][-1]["ai_generated"] for slide in item["slides"]))
        context = self.planner.calls[-1]["snapshot"]["group_context"]
        self.assertEqual([slide["number"] for slide in context["slides"]], [1, 2, 3])
        self.assertEqual(context["visual_style"], "明亮自然光")

    def test_workflow_values_are_validated_before_starting_workers(self):
        for value in ([], {}, 3, "unknown"):
            with self.subTest(value=value), self.assertRaises(fixture.VoiceError) as caught:
                self.start(workflow=value)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.planner.calls, [])
        self.assertEqual(self.generator.calls, [])


class SubjectLockedPromptTests(unittest.TestCase):
    """The prompts describe the full-width canvas with a title strip that the program, not the model, fills."""

    def test_planning_prompt_describes_a_full_width_photo_and_a_fade_not_a_title_slab(self):
        from workbench_carousel_images import prompt_planning_prompt
        text = prompt_planning_prompt({"workflow": "subject_locked", "slides": []})
        self.assertIn("铺满整个画布宽度", text)
        self.assertIn("渐变", text)
        for removed in ("顶部是新标题区", "铺满标题区以下", "深色留边"):
            self.assertNotIn(removed, text)

    def test_background_plate_prompt_forbids_text_and_a_dark_bar(self):
        from workbench_carousel_images import direct_image_prompt
        snapshot = {"workflow": "subject_locked", "requirements": {"workflow": "subject_locked"},
                    "slide": {"title_lines": ["Keep the original product"], "body": "A body line"},
                    "prompt_plan": {"prompt": "Empty picnic table", "style": "Warm light"}}
        text = direct_image_prompt(snapshot)
        self.assertIn("BACKGROUND PLATE", text)
        self.assertIn("NO text", text)
        self.assertIn("Empty picnic table", text)
        self.assertNotIn("Keep the original product", text, "the title is typeset locally; the plate never sees it")
        self.assertNotIn("320 pixels", text)


if __name__ == "__main__":
    unittest.main()
