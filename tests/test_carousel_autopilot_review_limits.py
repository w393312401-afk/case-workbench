"""A static-image limitation must not strand a supported appearance carousel."""
import copy
import json
from unittest.mock import patch
import unittest

import test_carousel_autopilot as fixture
from workbench_carousel_autopilot import review_allows_autopilot


class StaticLimitImages(fixture.AutopilotImages):
    def run(self, prompt, **options):
        answer = super().run(prompt, **options)
        if not options.get("read_only"):
            return answer
        value = json.loads(answer)
        value["evidence_assessment"] = {
            "claim_type": "appearance_or_instruction", "claim_support": "supported",
            "reference_match": "consistent", "limitation": "static_image_limitations",
        }
        for check in value["checks"]:
            if check["id"] == "evidence":
                check.update(status="uncertain", note="外观与操作姿态一致；静态图不能额外证明实际磨刀效果。")
        return json.dumps(value, ensure_ascii=False)


class AutopilotStaticLimitTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.CarouselAutopilotTests()
        with patch.object(fixture, "AutopilotImages", StaticLimitImages):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_supported_appearance_carousel_finishes_without_repeat_generation_or_review(self):
        self.fixture.start()
        item = self.fixture.wait()
        run = item["autopilot_runs"][-1]
        self.assertEqual(run["status"], "completed", run)
        self.assertTrue(run["delivery_id"])
        self.assertTrue(all(count == 1 for count in self.fixture.generation_counts().values()))
        self.assertTrue(all(count == 1 for count in self.fixture.generator.review_counts.values()))
        for slide in item["slides"]:
            output = slide["outputs"][-1]
            self.assertTrue(output["approved"])
            self.assertEqual(output["review"]["status"], "uncertain")

    def test_legacy_uncertain_output_is_rechecked_and_reused_without_redrawing(self):
        self.fixture.fixture.generate(slides=[1])
        item = self.fixture.current()
        first = item["slides"][0]["outputs"][-1]
        with self.fixture.bench.production.lock, self.fixture.bench.production.catalog_transaction():
            data = self.fixture.bench.production.read()
            saved = next(row for row in data["carousels"] if row["id"] == item["id"])["slides"][0]["outputs"][-1]
            saved["review"].pop("evidence_assessment", None)
            for check in saved["review"]["checks"]:
                check.pop("limitation_code", None)
            self.fixture.bench.production.write(data)
        self.fixture.generator.calls.clear()
        self.fixture.start()
        item = self.fixture.wait()
        self.assertEqual(item["autopilot_runs"][-1]["status"], "completed", item["autopilot_runs"][-1])
        output = item["slides"][0]["outputs"][-1]
        self.assertEqual(output["id"], first["id"])
        self.assertEqual(output["sha256"], first["sha256"])
        self.assertEqual(self.fixture.generation_counts()[1], 0)
        self.assertEqual(len(item["slides"][0]["outputs"]), 1)

    def test_limitation_code_does_not_override_claim_reference_or_quality_failures(self):
        self.fixture.fixture.generate(slides=[1])
        item = self.fixture.current()
        slide = item["slides"][0]
        original = slide["outputs"][-1]
        self.assertTrue(review_allows_autopilot(item, slide, original))
        changes = {
            "missing_assessment": lambda r: r.pop("evidence_assessment"),
            "unsupported_claim": lambda r: r["evidence_assessment"].update(claim_support="unsupported"),
            "performance_claim": lambda r: r["evidence_assessment"].update(claim_type="performance_claim"),
            "visible_result": lambda r: r["evidence_assessment"].update(claim_type="visible_result"),
            "changed_reference": lambda r: r["evidence_assessment"].update(reference_match="changed"),
            "missing_reference": lambda r: r.update(reference_bindings=[]),
            "real_issue": lambda r: r.update(issues=["Product logo was reversed"]),
            "evidence_failure": lambda r: next(c for c in r["checks"] if c["id"] == "evidence").update(status="fail"),
        }
        for label, change in changes.items():
            with self.subTest(label=label):
                output = copy.deepcopy(original)
                change(output["review"])
                self.assertFalse(review_allows_autopilot(item, slide, output))


if __name__ == "__main__":
    unittest.main()
