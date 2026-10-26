"""A group cannot certify script fidelity without the frozen script and usable mappings."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from carousel_lineage import lineage_context, mapping_issues, slide_lineage
from carousel_plan import GROUP_CHECKS, group_review_prompt, group_signature, normalize_group_review
from elevenlabs_client import VoiceError
import test_carousel_group_plan as fixture


def sample():
    return {"script_context": {"script_id": "SCR-original", "script_version": 1, "records": [
        {"number": 1, "spoken": "Choose for the blade.", "proof": "刀宽与装夹参照", "evidence": "展示"},
        {"number": 2, "spoken": "Tighten the knob.", "proof": "手动锁紧", "evidence": "动作演示"},
        {"number": 3, "spoken": "See the kit.", "proof": "整套", "evidence": "展示"}]},
        "slides": [{"number": n, "source_lines": [n], "proof_goal": goal,
                    "evidence_requirement": "必须有对应实拍", "visual_method": "动作特写",
                    "forbidden": "不能宣称自动选角"}
                   for n, goal in enumerate(["刀宽装夹条件", "手拧旋钮", "整套收口"], 1)]}


def answer():
    return {"summary": "已核对", "checks": [{"id": key, "status": "pass", "note": "核对"} for key in GROUP_CHECKS],
            "slides": [{"number": n, "observed_shot": "detail", "note": "实拍"} for n in (1, 2, 3)],
            "suggestions": [], "redundant_slides": []}


class LineageReviewTests(unittest.TestCase):
    def test_legacy_fields_are_absent_and_invalid_mapping_is_not_silently_reordered(self):
        self.assertEqual(slide_lineage({"role": "机制"}), {})
        for values in ([True], [0], [2, 1], [1, 1], "1,2"):
            with self.subTest(values=values):
                with self.assertRaises(VoiceError):
                    slide_lineage({"source_lines": values}, strict=True)
                self.assertEqual(slide_lineage({"source_lines": values}), {"source_lines": []})

    def test_optimistic_reviewer_cannot_pass_missing_context_or_task(self):
        for change in (lambda s: s.pop("script_context"), lambda s: s["slides"][1].pop("proof_goal"),
                       lambda s: s["slides"][0].pop("source_lines")):
            item = sample()
            change(item)
            review = normalize_group_review(answer(), [1, 2, 3], item)
            self.assertTrue(review["completed"])
            self.assertEqual(review["status"], "uncertain")

    def test_invalid_references_or_reordered_progression_fail(self):
        for refs in ([99], [1]):
            item = sample()
            item["slides"][2]["source_lines"] = refs
            review = normalize_group_review(answer(), [1, 2, 3], item)
            self.assertEqual(review["status"], "fail")
            self.assertEqual(next(c["status"] for c in review["checks"] if c["id"] == "script_fidelity"), "fail")

    def test_wrong_source_identity_and_incomplete_original_cannot_pass(self):
        for key, wrong in (("script_id", "SCR-other"), ("script_version", 2)):
            item = sample()
            item["carousel"] = {"script_id": "SCR-original", "script_version": 1, key: wrong}
            self.assertEqual(normalize_group_review(answer(), [1, 2, 3], item)["status"], "fail")
        item = sample()
        item["script_context"]["records"][1]["spoken"] = " "
        self.assertEqual(normalize_group_review(answer(), [1, 2, 3], item)["status"], "uncertain")
        item = sample()
        item["script_context"]["records"].append(copy.deepcopy(item["script_context"]["records"][0]))
        self.assertEqual(normalize_group_review(answer(), [1, 2, 3], item)["status"], "fail")

    def test_same_meaning_can_span_slides_without_reordering(self):
        item = sample()
        item["slides"][1]["source_lines"] = [1, 2]
        self.assertEqual(mapping_issues(item), {"missing": [], "invalid": []})
        self.assertEqual(normalize_group_review(answer(), [1, 2, 3], item)["status"], "pass")

    def test_signature_covers_original_task_conditions_and_forbidden_claims(self):
        item = sample()
        before = group_signature(item, [])
        for change in (lambda s: s["script_context"]["records"][0].update(proof="新条件"),
                       lambda s: s["slides"][1].update(proof_goal="换任务"),
                       lambda s: s["slides"][1].update(evidence_requirement="新证据"),
                       lambda s: s["slides"][1].update(forbidden="新禁写")):
            changed = copy.deepcopy(item)
            change(changed)
            self.assertNotEqual(group_signature(changed, []), before)
        frozen = lineage_context(item)
        frozen["records"][0]["spoken"] = "改动"
        self.assertEqual(group_signature(item, []), before)
        prompt = group_review_prompt(item)
        for text in ("刀宽与装夹参照", "手拧旋钮", "script_fidelity", "proof_alignment", "必要论据", "不强套痛点开头"):
            self.assertIn(text, prompt)


class LineageGroupIntegrationTests(unittest.TestCase):
    setUp = fixture.GroupPlanTests.setUp
    stop, carousel, body, current, pick_new_base, wait, generate, assert_error, start, detail, review = (
        fixture.GroupPlanTests.stop, fixture.GroupPlanTests.carousel, fixture.GroupPlanTests.body,
        fixture.GroupPlanTests.current, fixture.GroupPlanTests.pick_new_base, fixture.GroupPlanTests.wait,
        fixture.GroupPlanTests.generate, fixture.GroupPlanTests.assert_error, fixture.GroupPlanTests.start,
        fixture.GroupPlanTests.detail, fixture.GroupPlanTests.review)

    def test_actual_review_snapshot_contains_frozen_script_and_per_slide_tasks(self):
        item = self.current()
        item.update(script_context=sample()["script_context"])
        item["script_context"].update(script_id=item["script_id"], script_version=item["script_version"])
        for slide, task in zip(item["slides"], sample()["slides"]):
            slide.update(task)
        original = self.bench.production.carousel
        # Return a frozen task snapshot without changing the fixture's production database.
        from unittest.mock import patch
        with patch.object(self.bench.production, "carousel", side_effect=lambda identifier: copy.deepcopy(item)):
            self.bench.start_carousel_group_review(item["id"], self.body())
            self.wait()
        persisted = original(item["id"])
        run = persisted["group_reviews"][-1]
        self.assertEqual(run["review"]["status"], "pass")
        prompt = self.reviewer.calls[-1]["prompt"]
        self.assertIn('"script_version": 1', prompt)
        self.assertIn("手拧旋钮", prompt)
        self.assertIn("刀宽与装夹参照", prompt)


if __name__ == "__main__":
    unittest.main()
