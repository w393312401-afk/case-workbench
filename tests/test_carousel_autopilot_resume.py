"""Resume carousel fallbacks without repeating redraws or invalidating finished slides."""
from collections import Counter
import json
from pathlib import Path
import unittest

from codex_bridge import BridgeError
import test_carousel_autopilot_preserve as fixture


class ResumePixelChannel(fixture.PixelChannel):
    def __init__(self):
        super().__init__()
        self.fail_preserving_review_for = set()

    def run(self, prompt, **options):
        answer = super().run(prompt, **options)
        snapshot = json.loads((Path(options["cwd"]) / "input.json").read_text(encoding="utf-8"))
        if (options.get("read_only") and snapshot["requirements"]["mode"] == "photo_preserving" and
                snapshot["slide"]["number"] in self.fail_preserving_review_for):
            raise BridgeError("隔离测试：保留实拍成图核对暂不可用")
        return answer


class AutopilotResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.AutopilotPreserveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench, self.identifier = self.fixture.bench, self.fixture.identifier
        self.redraw = self.fixture.redraw
        self.pixels = ResumePixelChannel()
        self.fixture.pixels = self.bench.image_reviewer = self.pixels

    def run_all(self):
        return self.fixture.run_all()

    @staticmethod
    def selected(slide):
        return fixture.AutopilotPreserveTests.selected(slide)

    @staticmethod
    def generations(channel):
        return Counter(call["snapshot"]["slide"]["number"] for call in channel.calls if not call["read_only"])

    def test_resume_rechecks_saved_preserving_image_and_preserves_remaining_slides(self):
        self.pixels.fail_preserving_review_for = {1}
        before = self.run_all()
        self.assertEqual(before["autopilot_runs"][-1]["status"], "failed")
        self.assertEqual(before["requirements"]["preserve_slides"], [1])
        saved = self.bench._autopilot_output(before, before["slides"][0])
        self.assertEqual(saved["requirements_snapshot"]["mode"], "photo_preserving")
        self.assertEqual(saved["review"]["execution_status"], "failed")
        self.assertFalse(saved["approved"])

        self.pixels.fail_preserving_review_for.clear()
        after = self.run_all()

        self.assertEqual([run["status"] for run in after["autopilot_runs"]], ["failed", "completed"])
        self.assertEqual(self.generations(self.redraw), Counter({1: 1}))
        self.assertEqual(self.generations(self.pixels), Counter({1: 1, 2: 1, 3: 1}))
        recovered = self.selected(after["slides"][0])
        self.assertEqual(recovered["id"], saved["id"])
        self.assertEqual(recovered["sha256"], saved["sha256"])
        self.assertEqual(len(after["slides"][0]["outputs"]), len(before["slides"][0]["outputs"]))
        self.assertTrue(recovered["approved"])
        self.assertEqual(after["requirements"]["preserve_slides"], [1, 2, 3])

    def test_resume_keeps_previously_confirmed_redraw_contract_unchanged(self):
        self.pixels.fail_redraw_for = {2}
        self.pixels.fail_preserving_review_for = {2}
        before = self.run_all()
        self.assertEqual(before["autopilot_runs"][-1]["status"], "failed")
        self.assertEqual(before["requirements"]["preserve_slides"], [2])
        first = self.selected(before["slides"][0])
        self.assertTrue(first["approved"])
        self.assertEqual(first["requirements_snapshot"]["mode"], "generated_visual")
        second = self.bench._autopilot_output(before, before["slides"][1])

        self.pixels.fail_preserving_review_for.clear()
        after = self.run_all()

        self.assertEqual(after["autopilot_runs"][-1]["status"], "completed")
        resumed_first = self.selected(after["slides"][0])
        for key in ("id", "sha256", "input_signature", "requirements_signature", "requirements_snapshot",
                    "approved", "approved_at", "approval_actor", "review"):
            self.assertEqual(resumed_first[key], first[key], key)
        self.assertEqual(resumed_first["stages"]["confirmation"], first["stages"]["confirmation"])
        self.assertEqual(len(after["slides"][0]["outputs"]), 1)
        self.assertEqual(self.selected(after["slides"][1])["id"], second["id"])
        self.assertEqual(after["requirements"]["preserve_slides"], [2, 3])
        self.assertEqual(self.generations(self.redraw), Counter({1: 1, 2: 1}))
        self.assertEqual(self.generations(self.pixels), Counter({2: 1, 3: 1}))

    def test_existing_failed_redraw_switches_to_preserving_before_generating(self):
        self.fixture.fixture.generate(slides=[1])
        self.fixture.fixture.recheck(1)
        before = self.fixture.fixture.current()
        failed = self.selected(before["slides"][0])
        self.assertEqual(failed["review"]["status"], "needs_fix")
        self.assertTrue(failed["direct_image_edit"])
        self.assertNotIn("preserve_slides", before["requirements"])

        after = self.run_all()

        self.assertEqual(after["autopilot_runs"][-1]["status"], "completed")
        self.assertEqual(self.generations(self.redraw), Counter({1: 1}))
        self.assertEqual(self.generations(self.pixels), Counter({1: 1, 2: 1, 3: 1}))
        recovered = self.selected(after["slides"][0])
        self.assertNotEqual(recovered["id"], failed["id"])
        self.assertNotEqual(recovered["input_signature"], failed["input_signature"])
        self.assertEqual(recovered["requirements_snapshot"]["mode"], "photo_preserving")
        self.assertEqual(len(after["slides"][0]["outputs"]), 2)
        historical = next(output for output in after["slides"][0]["outputs"] if output["id"] == failed["id"])
        self.assertFalse(historical["approved"])
        self.assertEqual(historical["review"]["status"], "needs_fix")


if __name__ == "__main__":
    unittest.main()
