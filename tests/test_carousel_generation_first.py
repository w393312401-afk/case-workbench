"""Generation-first produces real AI outputs in one batch and labels deferred review."""
from collections import Counter
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from codex_bridge import BridgeCancelled, BridgeError
from elevenlabs_client import VoiceError
from workbench_carousel_autopilot import CarouselAutopilotMixin
import test_production_carousel_images as fixture


class GenerationFirstChannel(fixture.ImageGenerator):
    def __init__(self):
        super().__init__()
        self.plans = []
        self.fail_numbers = set()
        self.block_numbers = set()
        self.parallel_barrier = None

    def run(self, prompt, **options):
        folder = Path(options["cwd"])
        snapshot = json.loads((folder / "input.json").read_text(encoding="utf-8"))
        if options.get("read_only") and "slides" in (options.get("schema") or {}).get("properties", {}):
            self.plans.append({"prompt": prompt, "snapshot": snapshot, "folder": folder})
            return json.dumps({"summary": "AI 为整组策划不同画面", "style": "Warm natural kitchen photography",
                "slides": [{"number": slide["number"], "concept": f"Distinct creative scene {slide['number']}",
                    "prompt": f"AI-planned full-scene {slide['number']}: generate the entire product scene with natural light, "
                              "a new camera angle and clean text space; preserve this exact copy: " +
                              json.dumps([slide["title_lines"], slide["body"]])}
                    for slide in snapshot["slides"]]}, ensure_ascii=False)
        number = snapshot["slide"]["number"]
        if not options.get("read_only"):
            if self.parallel_barrier:
                self.parallel_barrier.wait(timeout=5)
            if number in self.fail_numbers:
                raise BridgeError(f"第 {number} 张模拟生图服务失败")
            if number in self.block_numbers:
                self.entered.set()
                while not self.release.wait(.01):
                    if options["cancel_event"].is_set():
                        raise BridgeCancelled("cancelled")
        return super().run(prompt, **options)


class GenerationFirstBench(CarouselAutopilotMixin, fixture.ImageBench):
    pass


class GenerationFirstAutopilotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselImageTests()
        with patch.object(fixture, "ImageBench", GenerationFirstBench), patch.object(
                fixture, "ImageGenerator", GenerationFirstChannel):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.channel = self.fixture.bench, self.fixture.generator
        self.identifier = self.fixture.item["id"]
        self.addCleanup(self.stop)

    def stop(self):
        self.channel.release.set()
        self.bench.stop_carousel_autopilots()

    def start(self, **values):
        return self.bench.start_carousel_autopilot(self.identifier,
            self.fixture.body(**{"workflow": "generation_first", **values}))

    def wait(self):
        deadline = time.monotonic() + 15
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.bench.autopilot_running(), "autopilot worker did not finish")
        self.assertFalse(self.bench.image_running(), "image worker did not finish")
        return self.fixture.current()

    @staticmethod
    def selected(slide):
        return next(output for output in slide["outputs"] if output["id"] == slide["output_id"])

    def assert_deferred(self, output):
        self.assertEqual(output["workflow"], "generation_first")
        self.assertTrue(output["ai_generated"])
        self.assertTrue(output["review_deferred"])
        self.assertEqual(output["review"]["status"], "deferred")
        self.assertEqual(output["review"]["execution_status"], "not_run")
        self.assertFalse(output["review"]["completed"])
        self.assertTrue(output["approved"])
        self.assertEqual(output["approval_actor"], "assistant_on_user_authorization")

    def test_explicit_generation_first_plans_once_and_generates_every_slide_in_parallel_without_review(self):
        item = self.fixture.current()
        self.bench.production.mutate("carousel_requirements", self.fixture.body(carousel_id=self.identifier,
            requirements={**(item.get("requirements") or {}), "mode": "photo_preserving", "preserve_slides": [1, 2, 3]}))
        self.channel.parallel_barrier = threading.Barrier(3)
        with patch.object(self.bench, "start_image_generation", wraps=self.bench.start_image_generation) as generate:
            started = self.start()
            item = self.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(started["run"]["workflow"], "generation_first")
        self.assertEqual(run["status"], "completed", run)
        self.assertTrue(run["review_deferred"])
        self.assertEqual(run["result"], {"images_generated": 3, "total_slides": 3,
                                       "review_deferred": True, "product_consistency_checked": False})
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.args[1]["slides"], [1, 2, 3])
        self.assertEqual(len(self.channel.plans), 1)
        self.assertEqual(Counter(call["snapshot"]["slide"]["number"] for call in self.channel.calls),
                         Counter({1: 1, 2: 1, 3: 1}))
        self.assertFalse(any(call["read_only"] for call in self.channel.calls))
        self.assertEqual(item["requirements"]["mode"], "generated_visual")
        self.assertFalse(item["requirements"].get("preserve_slides"))
        for slide in item["slides"]:
            output = self.selected(slide)
            self.assert_deferred(output)
            self.assertIn(f"AI-planned full-scene {slide['number']}", output["generation_prompt"])
        self.assertTrue(run["delivery_id"])
        self.assertTrue(item["deliveries"][-1]["review_deferred"])
        self.assertIn("尚未核对", run["message"])
        self.assertEqual([(slide["title_lines"], slide["body"]) for slide in item["slides"]], self.fixture.words)
        self.assertEqual(self.bench.creation.public()["scripts"], self.fixture.script_before)

    def test_resume_reuses_only_current_generation_first_images_without_planning_again(self):
        self.start()
        before = self.wait()
        self.assertEqual(before["autopilot_runs"][-1]["status"], "completed", before["autopilot_runs"][-1])
        ids = [slide["output_id"] for slide in before["slides"]]
        self.channel.calls.clear()
        self.channel.plans.clear()
        with patch.object(self.bench, "start_image_generation", wraps=self.bench.start_image_generation) as generate:
            self.start()
            after = self.wait()
        self.assertEqual(after["autopilot_runs"][-1]["status"], "completed", after["autopilot_runs"][-1])
        self.assertEqual(generate.call_count, 0)
        self.assertEqual(self.channel.plans, [])
        self.assertEqual(self.channel.calls, [])
        self.assertEqual([slide["output_id"] for slide in after["slides"]], ids)

    def test_old_confirmed_frame_layouts_are_not_reused(self):
        self.fixture.generate()
        before = self.fixture.approve_all()
        old_ids = [slide["output_id"] for slide in before["slides"]]
        self.channel.calls.clear()
        self.start()
        after = self.wait()
        self.assertEqual(after["autopilot_runs"][-1]["status"], "completed", after["autopilot_runs"][-1])
        self.assertEqual(len(self.channel.plans), 1)
        self.assertEqual(len(self.channel.calls), 3)
        self.assertTrue(all(slide["output_id"] != previous for slide, previous in zip(after["slides"], old_ids)))
        self.assertEqual([len(slide["outputs"]) for slide in after["slides"]], [2, 2, 2])

    def test_partial_generation_retains_successes_without_reporting_complete_or_retrying(self):
        self.channel.fail_numbers = {2}
        self.start()
        item = self.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "failed", run)
        self.assertEqual(run["completed_slides"], [1, 3])
        self.assertEqual(run["result"]["images_generated"], 2)
        self.assertFalse(run["delivery_id"])
        self.assertEqual(item.get("deliveries", []), [])
        self.assertEqual(len(item["generations"]), 1)
        self.assertEqual(len(self.channel.plans), 1)
        for slide in (item["slides"][0], item["slides"][2]):
            self.assert_deferred(self.selected(slide))
        self.assertEqual(item["slides"][1]["outputs"], [])
        self.assertIn("2/3", run["message"])

    def test_cancel_preserves_and_counts_images_saved_by_parallel_workers(self):
        self.channel.block_numbers = {2}
        started = self.start()
        self.assertTrue(self.channel.entered.wait(5))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            item = self.fixture.current()
            if sum(len(slide["outputs"]) for slide in item["slides"]) == 2:
                break
            time.sleep(.01)
        else:
            self.fail("other parallel outputs were not saved before cancellation")
        saved = {output["id"] for slide in item["slides"] for output in slide["outputs"]}
        self.bench.cancel_carousel_autopilot(self.identifier, started["run"]["id"])
        item = self.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "cancelled", run)
        self.assertEqual(run["completed_slides"], [1, 3])
        self.assertEqual(run["result"]["images_generated"], 2)
        self.assertEqual({output["id"] for slide in item["slides"] for output in slide["outputs"]}, saved)
        self.assertEqual(item.get("deliveries", []), [])
        self.assertIn("尚未核对", run["message"])

    def test_invalid_workflow_is_rejected_before_start(self):
        for workflow in ("unknown", [], None):
            with self.subTest(workflow=workflow), self.assertRaises(VoiceError) as caught:
                self.start(workflow=workflow)
            self.assertEqual(caught.exception.status, 400)


if __name__ == "__main__":
    unittest.main()
