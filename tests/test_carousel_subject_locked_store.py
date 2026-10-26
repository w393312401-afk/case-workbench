"""Subject-locked confirmation needs a current, complete preservation receipt."""
import copy
import unittest
from unittest.mock import patch

from elevenlabs_client import VoiceError
from production_store import (ProductionStore, carousel_confirmation_current, carousel_input_signature, carousel_output_current,
                              carousel_requirements, carousel_subject_preservation_current, SUBJECT_RENDER_CONTRACT)


class SubjectLockedStoreTests(unittest.TestCase):
    def state(self):
        slide = {"number": 1, "title_lines": ["Exact title"], "body": "Body", "role": "opening",
                 "source": "original photo", "forbidden": "", "base": {"id": "CND-source"}}
        item = {"script_id": "SCR-source", "script_version": 1, "image_aspect": "3:4",
                "requirements": {"workflow": "subject_locked"}, "slides": [slide], "history": []}
        signature = carousel_input_signature(item, slide, "3:4")
        output = {"id": "OUT-example", "workflow": "subject_locked", "ai_generated": True,
                  "aspect": "3:4", "input_signature": signature, "sha256": "final-sha", "base_sha256": "base-sha",
                  "approved": True, "approved_sha256": "final-sha", "approved_input_signature": signature,
                  "subject_preservation": {"status": "passed", "method": "masked_source_pixels",
                    "render_contract": SUBJECT_RENDER_CONTRACT,
                    "base_sha256": "base-sha", "final_sha256": "final-sha", "input_signature": signature,
                    "protected_pixel_count": 100, "mismatch_pixels": 0}}
        slide.update(outputs=[output], output_id=output["id"])
        return item, slide, output

    def test_subject_contract_enforces_product_protection_and_keeps_other_regions_editable(self):
        item, slide, _ = self.state()
        item["requirements"].update(mode="generated_visual", protected_product=False, protected_evidence=False,
            editable_regions=["product"], preserve_slides=[1], hands_authorized=False,
            authorized_hand_slides=[], no_hand_slides=[1], style="Warm kitchen")
        group = carousel_requirements(item)
        per_slide = carousel_requirements(item, slide)
        self.assertEqual(group["mode"], "photo_preserving")
        self.assertTrue(group["protected_product"])
        self.assertTrue(group["protected_evidence"])
        self.assertEqual(group["editable_regions"], ["background", "text", "hands"])
        self.assertNotIn("preserve_slides", group)
        self.assertEqual(group["render_contract"], SUBJECT_RENDER_CONTRACT)
        self.assertEqual(group["render_contract"], "segmented_background_v4")
        self.assertTrue(per_slide["hands_authorized"])
        self.assertEqual(per_slide["style"], "Warm kitchen")
        self.assertEqual(per_slide["locked_copy"]["title_lines"], slide["title_lines"])

    def test_valid_receipt_confirms_without_semantic_evidence_review(self):
        item, slide, output = self.state()
        output["review"] = {"status": "deferred", "completed": False, "execution_status": "not_run"}
        self.assertTrue(carousel_subject_preservation_current(output))
        self.assertTrue(carousel_confirmation_current(item, slide, output))

    def test_missing_stale_or_incomplete_receipts_never_confirm_or_approve(self):
        item, slide, original = self.state()
        variants = {
            "no_receipt": lambda out: out.pop("subject_preservation"),
            "old_contract": lambda out: out["subject_preservation"].update(render_contract="masked_edit_v1"),
            "v2_contract": lambda out: out["subject_preservation"].update(render_contract="background_plate_v2"),
            "missing_contract": lambda out: out["subject_preservation"].pop("render_contract"),
            "no_ai_generation": lambda out: out.update(ai_generated=False),
            "wrong_workflow": lambda out: out.update(workflow="generation_first"),
            "failed": lambda out: out["subject_preservation"].update(status="failed"),
            "unverified_method": lambda out: out["subject_preservation"].update(method="ai_says_unchanged"),
            "wrong_final": lambda out: out["subject_preservation"].update(final_sha256="old-final"),
            "wrong_base": lambda out: out["subject_preservation"].update(base_sha256="old-base"),
            "wrong_input": lambda out: out["subject_preservation"].update(input_signature="old-input"),
            "empty_mask": lambda out: out["subject_preservation"].update(protected_pixel_count=0),
            "boolean_count": lambda out: out["subject_preservation"].update(protected_pixel_count=True),
            "pixel_changed": lambda out: out["subject_preservation"].update(mismatch_pixels=1),
            "missing_count": lambda out: out["subject_preservation"].pop("mismatch_pixels"),
        }
        for name, change in variants.items():
            with self.subTest(name=name):
                output = copy.deepcopy(original)
                change(output)
                slide["outputs"] = [output]
                self.assertFalse(carousel_confirmation_current(item, slide, output))
                with self.assertRaises(VoiceError) as caught:
                    ProductionStore._approve_output(None, item, 1, output["id"], True, "", "human")
                self.assertEqual(caught.exception.status, 409)

    def test_store_and_compositor_agree_on_the_render_contract(self):
        from carousel_subject_lock import RENDER_CONTRACT
        self.assertEqual(SUBJECT_RENDER_CONTRACT, RENDER_CONTRACT)
        self.assertEqual(RENDER_CONTRACT, "segmented_background_v4")

    def test_whole_redraw_signature_is_invalidated_by_subject_lock(self):
        item, slide, output = self.state()
        whole_redraw = copy.deepcopy(item)
        whole_redraw["requirements"]["workflow"] = "generation_first"
        output.update(workflow="generation_first", input_signature=carousel_input_signature(whole_redraw, slide, "3:4"))
        self.assertFalse(carousel_confirmation_current(item, slide, output))

    def test_previous_subject_locked_signature_and_approval_expire_without_changing_source_data(self):
        for previous_contract in (None, "background_plate_v2"):
            with self.subTest(previous_contract=previous_contract):
                item, slide, output = self.state()
                previous_requirements = carousel_requirements(item, slide, "3:4")
                if previous_contract:
                    item["requirements"]["render_contract"] = previous_contract
                    previous_requirements["render_contract"] = previous_contract
                    output["subject_preservation"]["render_contract"] = previous_contract
                else:
                    previous_requirements.pop("render_contract")
                    output["subject_preservation"].pop("render_contract")
                with patch("production_store.carousel_requirements", return_value=previous_requirements):
                    previous_signature = carousel_input_signature(item, slide, "3:4")
                output.update(input_signature=previous_signature, approved_input_signature=previous_signature)
                output["subject_preservation"].update(input_signature=previous_signature)
                self.assertTrue(output["approved"])
                self.assertNotEqual(previous_signature, carousel_input_signature(item, slide, "3:4"))
                self.assertFalse(carousel_output_current(item, slide, output))
                self.assertFalse(carousel_confirmation_current(item, slide, output))

    def test_new_contract_does_not_change_other_workflow_signatures(self):
        item, slide, _ = self.state()
        for workflow in (None, "generation_first", "review_first"):
            with self.subTest(workflow=workflow):
                item["requirements"] = {} if workflow is None else {"workflow": workflow}
                original = carousel_input_signature(item, slide, "3:4")
                item["requirements"]["render_contract"] = SUBJECT_RENDER_CONTRACT
                self.assertNotIn("render_contract", carousel_requirements(item, slide))
                self.assertEqual(original, carousel_input_signature(item, slide, "3:4"))


if __name__ == "__main__":
    unittest.main()
