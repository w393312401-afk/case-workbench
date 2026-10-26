"""After a whole-image redraw fails the check, AI full-run switches to keeping the real photo pixels."""
from collections import Counter
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from workbench_carousel_autopilot import CarouselAutopilotMixin
import test_production_carousel_images as fixture


class RedrawChannel(fixture.ImageGenerator):
    """Stands in for the Cockpit image-edit channel: it redraws the whole picture and has no check of its own."""
    direct_image_edit = True
    source = "cockpit_image_edit"


class PixelChannel(fixture.ImageGenerator):
    """Stands in for Codex + compositor: reads back clean only when the slide is made as a photo-preserving one."""

    def __init__(self):
        super().__init__()
        self.fail_redraw_for = {1, 2, 3}

    def run(self, prompt, **options):
        answer = super().run(prompt, **options)
        if not options.get("read_only"):
            return answer
        snapshot = json.loads((Path(options["cwd"]) / "input.json").read_text(encoding="utf-8"))
        number, mode = snapshot["slide"]["number"], snapshot["requirements"]["mode"]
        if mode == "photo_preserving" or number not in self.fail_redraw_for:
            return answer
        parsed = json.loads(answer)
        for check in parsed["checks"]:
            if check["id"] in {"product", "evidence", "subject"}:
                check.update(status="fail", note="刀刃纹理被重绘")
        parsed["issues"] = ["刀刃纹理被重绘"]
        return json.dumps(parsed, ensure_ascii=False)


class Bench(CarouselAutopilotMixin, fixture.ImageBench):
    pass


class AutopilotPreserveTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselImageTests()
        with patch.object(fixture, "ImageBench", Bench):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.identifier = self.fixture.bench, self.fixture.item["id"]
        self.redraw, self.pixels = RedrawChannel(), PixelChannel()
        self.bench.image_generator, self.bench.image_reviewer = self.redraw, self.pixels
        item = self.bench.production.carousel(self.identifier)
        self.bench.production.mutate("carousel_requirements", {"request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90", "carousel_id": self.identifier,
            "revision": self.bench.production.current_revision(),
            "requirements": {**(item.get("requirements") or {}), "mode": "generated_visual",
                             "editable_regions": ["background", "text", "hands"], "hands_authorized": True}})
        self.addCleanup(self.stop)

    def stop(self):
        self.bench.stop_carousel_autopilots()
        deadline = time.monotonic() + 10
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)

    def run_all(self):
        self.bench.start_carousel_autopilot(self.identifier, {**self.fixture.body(), "workflow": "review_first"})
        deadline = time.monotonic() + 20
        while (self.bench.autopilot_running() or self.bench.image_running()) and time.monotonic() < deadline:
            time.sleep(.01)
        return self.bench.production.carousel(self.identifier)

    @staticmethod
    def selected(slide):
        return next(output for output in slide["outputs"] if output["id"] == slide["output_id"])

    def test_second_attempt_keeps_real_pixels_and_later_slides_start_that_way(self):
        item = self.run_all()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        # The redraw channel is tried once, on the first slide only; everything after goes through the pixel channel.
        self.assertEqual(Counter(call["snapshot"]["slide"]["number"] for call in self.redraw.calls if not call["read_only"]), Counter({1: 1}))
        made = [call for call in self.pixels.calls if not call["read_only"]]
        self.assertEqual(sorted(call["snapshot"]["slide"]["number"] for call in made), [1, 2, 3])
        for call in made:
            self.assertEqual(call["snapshot"]["channel"], "codex_preserving")
            self.assertEqual(call["snapshot"]["requirements"]["mode"], "photo_preserving")
            self.assertEqual(call["snapshot"]["requirements"]["editable_regions"], ["background", "text"])
            self.assertFalse(call["snapshot"]["requirements"]["hands_authorized"])
            self.assertIn("必须保留实拍像素", call["prompt"])
        self.assertEqual(item["requirements"]["preserve_slides"], [1, 2, 3])
        for slide in item["slides"]:
            output = self.selected(slide)
            self.assertTrue(output["approved"])
            self.assertEqual(output["requirements_snapshot"]["mode"], "photo_preserving")
            self.assertEqual(output["review"]["status"], "pass")
            self.assertNotIn("direct_image_edit", output)
        self.assertTrue(run["delivery_id"])

    def test_fallback_on_a_later_slide_does_not_invalidate_slides_already_confirmed(self):
        self.pixels.fail_redraw_for = {2}
        item = self.run_all()
        self.assertEqual(item["autopilot_runs"][-1]["status"], "completed", item["autopilot_runs"][-1])
        first = self.selected(item["slides"][0])
        self.assertTrue(first["approved"])
        self.assertEqual(first["requirements_snapshot"]["mode"], "generated_visual")
        self.assertEqual(item["requirements"]["preserve_slides"], [2, 3])
        for slide in item["slides"][1:]:
            self.assertEqual(self.selected(slide)["requirements_snapshot"]["mode"], "photo_preserving")
        self.assertEqual(Counter(call["snapshot"]["slide"]["number"] for call in self.redraw.calls if not call["read_only"]),
                         Counter({1: 1, 2: 1}))


if __name__ == "__main__":
    unittest.main()
