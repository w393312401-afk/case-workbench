"""Frozen script tasks reach visual decisions, and attractive stills cannot pass a missing action."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))
sys.path.insert(0, str(APP / "scripts"))

from workbench_carousel_clips import CarouselClipsMixin
from workbench_carousel_images import (CarouselImagesMixin, direct_image_prompt, generation_prompt,
    image_hash, normalize_review, prompt_planning_prompt, review_prompt, slide_copy, _subject_background_inputs_match)
from workbench_carousel_stills import compose_prompt, search_handoff


SOURCE = {"script_id": "SCR-original", "script_version": 3, "records": [
    {"number": 5, "spoken": "Set the angle, tighten the knob, then follow the guide.",
     "zh": "设角后锁紧，再沿导杆磨", "proof": "手拧橙色旋钮锁紧", "tags": ["旋钮动作", "角度刻度"],
     "evidence": "真实操作特写；刻度参照须符合装夹条件"},
]}
SLIDE = {"number": 3, "title_lines": ["Set the angle", "Tighten the knob"], "body": "Follow the guide.",
    "title_zh": "设角后锁紧", "body_zh": "再沿导杆磨", "role": "操作", "source": "操作片段", "forbidden": "不宣称刻度通用",
    "source_lines": [5], "proof_goal": "看清手拧旋钮锁紧与导杆引导", "visual_method": "动作特写与导向小图",
    "evidence_requirement": "手指接触旋钮、锁紧动作及刻度清楚；缺动作须补拍"}


class LineageVisualTests(unittest.TestCase):
    def setUp(self):
        self.item = {"id": "CAR-test", "title": "调角", "script_id": "SCR-original", "script_version": 3,
                     "slides": [copy.deepcopy(SLIDE)], "script_context": copy.deepcopy(SOURCE)}
        self.snapshot = {"carousel": {"id": "CAR-test"}, "script_context": copy.deepcopy(SOURCE),
                         "slide": copy.deepcopy(SLIDE), "aspect": "3:4", "requirements": {"mode": "photo_preserving"}}
        self.answer = {"summary": "文案正确，实拍对照相符", "observed_title_lines": SLIDE["title_lines"],
            "observed_body": SLIDE["body"], "issues": [],
            "checks": [{"id": key, "status": "pass", "note": "已实际看图"} for key in ("visual", "text", "product", "evidence")],
            "evidence_assessment": {"claim_type": "appearance_or_instruction", "claim_support": "supported",
                "reference_match": "consistent", "limitation": "none"},
            "lineage_assessment": {"source_alignment": "aligned", "task_support": "supported",
                "observed_evidence": "手指握旋钮，锁紧位置及导向接触清楚", "gap": ""}}

    def normalize(self, answer=None, slide=None, context=None, **options):
        return normalize_review(json.dumps(self.answer if answer is None else answer), slide or SLIDE, (1080, 1440), "3:4",
            script_context=SOURCE if context is None else context, **options)

    def assert_task_input(self, text):
        for value in (SOURCE["records"][0]["spoken"], SLIDE["proof_goal"], SLIDE["visual_method"], SLIDE["evidence_requirement"]):
            self.assertIn(value, text)
        self.assertIn("不生成", text)

    def test_clip_shortlist_includes_action_requirements_not_only_marketing_words(self):
        listing = {"clips": [{"key": "general", "label": "设备整机摆拍", "order": 0, "timeline": "素材", "label_status_text": "已核", "used_by": []},
                            {"key": "action", "label": "手拧旋钮锁紧导杆引导", "order": 1, "timeline": "素材", "label_status_text": "已核", "used_by": []}]}
        with patch("workbench_carousel_clips.CLIP_PROMPT_LIMIT", 1):
            prompt = CarouselClipsMixin._pick_prompt([SLIDE], listing, 1, "", item=self.item)
        self.assertIn("| action |", prompt)
        self.assertNotIn("| general |", prompt)
        self.assert_task_input(prompt)
        self.assertIn("不能宣布本张证据已满足", prompt)

    def test_still_and_composition_handoffs_have_frozen_source_and_task(self):
        search = search_handoff(self.item, [SLIDE], materials=["素材"], per_slide=2, instruction="", project="项目", refs=[])
        composed = compose_prompt(self.item, SLIDE, {"img1.jpg": {"size": [1080, 1440]}}, {}, "3:4", "")
        for prompt in (search, composed):
            self.assert_task_input(prompt)
            self.assertIn("整机摆拍", prompt)

    def test_generation_and_both_planners_receive_source_even_when_body_is_hidden(self):
        copied = slide_copy(SLIDE, {"overlay_only": True})
        self.assertEqual(copied["body"], "")
        self.assertEqual(copied["source_lines"], [5])
        copied["source_lines"].append(6)
        self.assertEqual(SLIDE["source_lines"], [5])
        self.snapshot["requirements"]["overlay_only"] = True
        for prompt in (generation_prompt(self.snapshot), direct_image_prompt(self.snapshot), review_prompt(self.snapshot)):
            self.assert_task_input(prompt)
        for workflow in ("generation_first", "subject_locked"):
            self.assert_task_input(prompt_planning_prompt({"workflow": workflow, "slides": [SLIDE], "script_context": SOURCE}))
            snapshot = {**self.snapshot, "workflow": workflow, "prompt_plan": {"prompt": "Keep scene readable", "concept": "操作特写", "style": "自然光"}}
            self.assert_task_input(direct_image_prompt(snapshot))

    def test_snapshot_does_not_lose_lineage(self):
        with patch("production_store.carousel_requirements", return_value={"mode": "layout_only"}), \
                patch("workbench_carousel_images.carousel_input_signature", return_value="sig"):
            snapshot = CarouselImagesMixin._snapshot(self.item, SLIDE, "3:4")
        self.assertEqual(snapshot["script_context"], SOURCE)
        self.assertEqual(snapshot["slide"]["proof_goal"], SLIDE["proof_goal"])
        snapshot["script_context"]["records"].clear()
        self.assertEqual(len(self.item["script_context"]["records"]), 1)

    def test_planning_worker_passes_mapping_to_ai_and_the_actual_image_prompt(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            work = root / "generations" / "test" / "03"
            work.mkdir(parents=True)
            base = work / "base.png"
            Image.new("RGB", (30, 40), "gray").save(base)
            snapshot = {**self.snapshot, "workflow": "generation_first", "base_sha256": image_hash(base)}
            calls = []

            def plan(prompt, **kwargs):
                calls.append({"prompt": prompt, "input": json.loads((Path(kwargs["cwd"]) / "input.json").read_text(encoding="utf-8"))})
                return json.dumps({"summary": "明确动作", "style": "自然光", "slides": [
                    {"number": 3, "concept": "手拧旋钮", "prompt": "Use clear close-up and preserve the copy."}]})

            bench = SimpleNamespace(_image_reviewer=lambda: SimpleNamespace(run=plan), _direct_image_edit=lambda: True,
                                    _carousel_dir=lambda _: root, _stage=lambda *args, **kwargs: None,
                                    _image_progress=lambda *args: None)
            CarouselImagesMixin._plan_generation_prompts(bench, "CAR-test", "test", [(snapshot, work, base)], threading.Event())
            self.assertEqual(calls[0]["input"]["script_context"], SOURCE)
            self.assertEqual(calls[0]["input"]["slides"][0]["proof_goal"], SLIDE["proof_goal"])
            self.assert_task_input(calls[0]["prompt"])
            self.assert_task_input((work / "prompt.txt").read_text(encoding="utf-8"))

    def test_whole_product_image_cannot_pass_missing_knob_action_or_get_static_exemption(self):
        self.answer["lineage_assessment"].update(task_support="missing", observed_evidence="只有整机俯拍", gap="没有手拧旋钮的动作特写")
        self.answer["evidence_assessment"]["limitation"] = "static_image_limitations"
        review = self.normalize()
        self.assertEqual(review["status"], "needs_fix")
        evidence = next(row for row in review["checks"] if row["id"] == "evidence")
        self.assertEqual(evidence["status"], "fail")
        self.assertNotIn("limitation_code", evidence)
        self.assertIn("没有手拧旋钮的动作特写", review["issues"])

    def test_source_condition_lost_cannot_pass_correct_words_and_clear_photo(self):
        self.answer["lineage_assessment"].update(source_alignment="changed", gap="省略了装夹条件，扩大刻度适用范围")
        review = self.normalize()
        self.assertEqual(review["status"], "needs_fix")
        self.assertTrue(any("装夹条件" in message for message in review["issues"]))

    def test_supported_verdict_with_a_real_gap_cannot_pass(self):
        self.answer["lineage_assessment"]["gap"] = "缺少装夹条件，无法验证刻度是否适用"
        review = self.normalize()
        self.assertEqual(review["status"], "uncertain")
        self.assertIn(self.answer["lineage_assessment"]["gap"], review["issues"])
        self.assertEqual(next(row for row in review["checks"] if row["id"] == "evidence")["status"], "uncertain")

    def test_new_mapping_without_assessment_or_observation_stays_uncertain(self):
        self.answer.pop("lineage_assessment")
        self.assertEqual(self.normalize()["status"], "uncertain")
        self.assertTrue(self.normalize()["completed"])
        self.answer["lineage_assessment"] = {"source_alignment": "aligned", "task_support": "supported", "observed_evidence": "", "gap": ""}
        self.assertEqual(self.normalize()["status"], "uncertain")

    def test_supported_action_can_pass_without_inventing_result_or_before_after_requirement(self):
        review = self.normalize()
        self.assertEqual(review["status"], "pass")
        self.assertEqual(review["issues"], [])

    def test_missing_source_cannot_be_claimed_as_verified(self):
        review = self.normalize(context={})
        self.assertEqual(review["lineage_assessment"]["source_alignment"], "unverified")
        self.assertEqual(review["status"], "uncertain")

    def test_empty_new_slide_mapping_is_not_a_verified_task(self):
        unmapped = {**SLIDE, "source_lines": [], "proof_goal": "", "evidence_requirement": ""}
        review = self.normalize(slide=unmapped)
        self.assertEqual(review["status"], "uncertain")
        self.assertEqual(review["lineage_assessment"]["task_support"], "unverified")

    def test_required_new_table_cannot_pass_when_all_mapping_fields_are_missing(self):
        unmapped = {key: value for key, value in SLIDE.items() if key not in ("source_lines", "proof_goal", "visual_method", "evidence_requirement")}
        review = self.normalize(slide=unmapped, context={**SOURCE, "lineage_required": True})
        self.assertEqual(review["status"], "uncertain")
        self.assertTrue(review["issues"])
        self.assertEqual(review["lineage_assessment"]["source_alignment"], "unverified")
        self.assertEqual(review["lineage_assessment"]["task_support"], "unverified")

    def test_blank_source_text_is_not_verifiable_despite_matching_record_number(self):
        context = copy.deepcopy(SOURCE)
        context["records"][0]["spoken"] = "   "
        review = self.normalize(context=context)
        self.assertEqual(review["status"], "uncertain")
        self.assertEqual(review["lineage_assessment"]["source_alignment"], "unverified")

    def test_generated_visual_cannot_gain_a_real_photo_static_limitation_exemption(self):
        self.answer["evidence_assessment"]["limitation"] = "static_image_limitations"
        review = self.normalize(requirements={"mode": "generated_visual"})
        evidence = next(row for row in review["checks"] if row["id"] == "evidence")
        self.assertEqual(review["status"], "uncertain")
        self.assertEqual(evidence["limitation_code"], "generated_visual_fidelity")

    def test_legacy_item_remains_usable_and_explicitly_unverified(self):
        legacy_slide = {key: value for key, value in SLIDE.items() if key not in ("source_lines", "proof_goal", "visual_method", "evidence_requirement")}
        self.answer.pop("lineage_assessment")
        review = self.normalize(slide=legacy_slide, context={})
        self.assertEqual(review["status"], "pass")
        self.assertEqual(review["lineage_assessment"]["source_alignment"], "unverified")
        self.assertEqual(review["lineage_assessment"]["task_support"], "unverified")
        prompt = CarouselClipsMixin._pick_prompt([legacy_slide], {"clips": []}, 1, "")
        self.assertIn("原视频推进与意思无法核对", prompt)
        self.assertNotIn(SOURCE["records"][0]["spoken"], prompt)

    def test_background_reuse_rejects_changed_task_or_frozen_source(self):
        current = {**self.snapshot, "workflow": "subject_locked", "base_sha256": "hash",
            "requirements": {"mode": "photo_preserving", "workflow": "subject_locked", "render_contract": "segmented_background_v4"}}
        self.assertTrue(_subject_background_inputs_match(current, copy.deepcopy(current)))
        for target in ("task", "source"):
            previous = copy.deepcopy(current)
            if target == "task":
                previous["slide"]["proof_goal"] = "旧任务"
            else:
                previous["script_context"]["records"][0]["spoken"] = "Old source"
            self.assertFalse(_subject_background_inputs_match(current, previous))


if __name__ == "__main__":
    unittest.main()
