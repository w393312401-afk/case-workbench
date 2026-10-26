"""A static-photo limitation is distinct from missing evidence for an actual claim."""
import copy
import json
from pathlib import Path
import sys
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
sys.path.insert(0, str(APP_ROOT / "scripts"))

from workbench_carousel_images import normalize_review, review_prompt, REVIEW_SCHEMA


class CarouselEvidenceScopeTests(unittest.TestCase):
    def setUp(self):
        self.slide = {"title_lines": ["Follow the guide"], "body": "Keep the handle here."}
        self.answer = {
            "summary": "可见结构与操作姿态相符；静态图不证明实际性能或逐像素同一性。",
            "observed_title_lines": self.slide["title_lines"], "observed_body": self.slide["body"],
            "issues": [],
            "checks": [{"id": key, "status": "uncertain" if key == "evidence" else "pass", "note": "已看图核对"}
                       for key in ("visual", "text", "product", "evidence")],
            "evidence_assessment": {"claim_type": "appearance_or_instruction", "claim_support": "supported",
                                    "reference_match": "consistent", "limitation": "static_image_limitations"},
        }

    def normalize(self, answer=None, **options):
        return normalize_review(json.dumps(self.answer if answer is None else answer), self.slide,
                                (1080, 1440), "3:4", requirements={"mode": "photo_preserving"}, **options)

    @staticmethod
    def evidence(review):
        return next(check for check in review["checks"] if check["id"] == "evidence")

    def assert_no_static_exemption(self, review):
        self.assertNotEqual(self.evidence(review).get("limitation_code"), "static_image_limitations", review)

    def test_static_limitation_preserves_uncertainty_and_saves_explicit_basis(self):
        review = self.normalize()
        self.assertTrue(review["completed"])
        self.assertEqual(review["status"], "uncertain")
        self.assertEqual(self.evidence(review)["status"], "uncertain")
        self.assertEqual(self.evidence(review)["limitation_code"], "static_image_limitations")
        self.assertEqual(review["evidence_assessment"], self.answer["evidence_assessment"])
        self.assertEqual(review["issues"], [])

    def test_claims_and_real_uncertainty_cannot_receive_the_exemption(self):
        for field, value in (("claim_type", "visible_result"), ("claim_type", "performance_claim"),
                             ("claim_support", "unsupported"), ("claim_support", "uncertain"),
                             ("reference_match", "changed"), ("reference_match", "uncertain"),
                             ("limitation", "missing_visual_evidence"), ("limitation", "none")):
            with self.subTest(field=field, value=value):
                answer = copy.deepcopy(self.answer)
                answer["evidence_assessment"][field] = value
                self.assert_no_static_exemption(self.normalize(answer))

    def test_structured_defects_override_a_contradictory_evidence_pass(self):
        for field, value in (("claim_support", "unsupported"), ("reference_match", "changed")):
            with self.subTest(field=field):
                answer = copy.deepcopy(self.answer)
                self.evidence(answer)["status"] = "pass"
                answer["evidence_assessment"][field] = value
                review = self.normalize(answer)
                self.assertEqual(self.evidence(review)["status"], "fail")
                self.assertEqual(review["status"], "needs_fix")
                self.assertTrue(review["issues"])
                self.assert_no_static_exemption(review)

    def test_actual_effect_claim_with_static_limit_cannot_keep_a_contradictory_pass(self):
        answer = copy.deepcopy(self.answer)
        self.evidence(answer)["status"] = "pass"
        answer["evidence_assessment"]["claim_type"] = "performance_claim"
        review = self.normalize(answer)
        self.assertEqual(self.evidence(review)["status"], "uncertain")
        self.assert_no_static_exemption(review)

    def test_any_actual_issue_failed_check_or_uncertain_check_blocks_exemption(self):
        for key in ("visual", "text", "product", "evidence"):
            for status in ("fail", "uncertain"):
                if key == "evidence" and status == "uncertain":
                    continue
                with self.subTest(check=key, status=status):
                    answer = copy.deepcopy(self.answer)
                    next(check for check in answer["checks"] if check["id"] == key)["status"] = status
                    self.assert_no_static_exemption(self.normalize(answer))
        answer = copy.deepcopy(self.answer)
        answer["issues"] = ["产品品牌被改变。"]
        self.assert_no_static_exemption(self.normalize(answer))
        answer = copy.deepcopy(self.answer)
        answer["observed_body"] = "Wrong words."
        self.assert_no_static_exemption(self.normalize(answer))

    def test_missing_reference_and_partial_scope_do_not_receive_exemption(self):
        self.assert_no_static_exemption(self.normalize(has_reference=False))
        self.assert_no_static_exemption(self.normalize(scope=["visual", "text", "product"]))
        self.assert_no_static_exemption(self.normalize(scope=["evidence"]))

    def test_legacy_receipts_or_model_approval_codes_do_not_gain_exemption(self):
        answer = copy.deepcopy(self.answer)
        del answer["evidence_assessment"]
        self.evidence(answer)["limitation_code"] = "static_image_limitations"
        answer["summary"] = "appearance_or_instruction supported consistent static_image_limitations"
        review = self.normalize(answer)
        self.assertTrue(review["completed"])
        self.assert_no_static_exemption(review)

    def test_new_assessment_rejects_missing_unknown_or_extra_fields(self):
        mutations = [None, {}, {**self.answer["evidence_assessment"], "claim_support": "probably"},
                     {**self.answer["evidence_assessment"], "limitation_code": "static_image_limitations"}]
        for assessment in mutations:
            with self.subTest(assessment=assessment):
                answer = copy.deepcopy(self.answer)
                answer["evidence_assessment"] = assessment
                review = self.normalize(answer)
                self.assertFalse(review["completed"])
                self.assert_no_static_exemption(review)

    def test_schema_requires_assessment_and_prompt_limits_it_to_actual_claims(self):
        self.assertIn("evidence_assessment", REVIEW_SCHEMA["required"])
        schema = REVIEW_SCHEMA["properties"]["evidence_assessment"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(self.answer["evidence_assessment"]))
        prompt = review_prompt({"slide": self.slide})
        self.assertIn("evidence_assessment", prompt)
        self.assertIn("issues 留空", prompt)
        self.assertIn("不得用一般静态图限制掩盖", prompt)


if __name__ == "__main__":
    unittest.main()
