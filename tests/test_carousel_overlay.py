"""Overlay-only carousels and the shared caption look: what the image prompts and checks receive."""
import json
from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from carousel_layout import CAPTION_PRESETS, RIM_COLOR, caption_style_prompt
from elevenlabs_client import VoiceError
from production_store import PRODUCT_REQUIREMENT_KEYS, carousel_requirements
from workbench_carousel_images import DEFAULT_HAND_LOOK, direct_image_prompt, generation_prompt, normalize_review, slide_copy

SLIDE = {"number": 1, "title_lines": ["ONE KIT", "NO JIG"], "body": "Clamp, rod and seven plates in one case.",
         "body_zh": "夹具、导杆和七块磨板装在一个箱子里。", "role": "开场", "source": "实拍", "forbidden": "无"}


def item(**requirements):
    return {"script_id": "S", "script_version": 1, "slides": [SLIDE], "requirements": {"style": "工坊暖光", **requirements}}


def snapshot(**requirements):
    value = carousel_requirements(item(**requirements), SLIDE, "3:4")
    return {"slide": slide_copy(SLIDE, value), "aspect": "3:4", "requirements": value}


class RequirementTests(unittest.TestCase):
    def test_overlay_only_is_optional_and_strictly_boolean(self):
        self.assertNotIn("overlay_only", carousel_requirements(item()))
        self.assertNotIn("overlay_only", carousel_requirements(item(overlay_only=False)))
        self.assertTrue(carousel_requirements(item(overlay_only=True))["overlay_only"])
        with self.assertRaises(VoiceError):
            carousel_requirements(item(overlay_only="yes"))

    def test_locked_copy_and_slide_words_drop_the_body_only_when_asked(self):
        self.assertEqual(carousel_requirements(item(), SLIDE)["locked_copy"]["body"], SLIDE["body"])
        overlay = carousel_requirements(item(overlay_only=True), SLIDE)
        self.assertEqual(overlay["locked_copy"]["body"], "")
        self.assertEqual(overlay["locked_copy"]["title_lines"], SLIDE["title_lines"])
        self.assertEqual((slide_copy(SLIDE, overlay)["body"], slide_copy(SLIDE, overlay)["body_zh"]), ("", ""))
        self.assertEqual(slide_copy(SLIDE, carousel_requirements(item()))["body"], SLIDE["body"])


class PromptTests(unittest.TestCase):
    def test_both_prompts_leave_out_the_body_for_overlay_only_groups(self):
        for build in (generation_prompt, direct_image_prompt):
            with self.subTest(build=build.__name__):
                prompt = build(snapshot(overlay_only=True))
                self.assertIn("本组只排叠字标题，不排正文", prompt)
                self.assertNotIn(SLIDE["body"], prompt)
                self.assertIn("ONE KIT", prompt)
                plain = build(snapshot())
                self.assertIn(SLIDE["body"], plain)
                self.assertNotIn("只排叠字", plain)
                self.assertNotIn("文字样式（", plain)

    def test_rugged_caption_spec_matches_the_layout_preset(self):
        spec = caption_style_prompt("rugged")
        self.assertIn("#F4EDD8", spec)
        self.assertIn("#%02X%02X%02X" % RIM_COLOR[:3], spec)
        self.assertIsNone(CAPTION_PRESETS["rugged"]["panel_color"])
        self.assertIn("不画文字底板或整条色带", spec)
        self.assertIn("Impact", spec)
        self.assertIn("#FFFFFF", caption_style_prompt("plain"))
        for build in (generation_prompt, direct_image_prompt):
            prompt = build(snapshot(caption_style="rugged"))
            self.assertIn(spec, prompt)
            self.assertIn("整组风格栏只决定场景和背景", prompt)


class HandPromptTests(unittest.TestCase):
    def test_authorized_hand_request_is_a_plain_instruction_in_both_prompts(self):
        for build in (generation_prompt, direct_image_prompt):
            with self.subTest(build=build.__name__):
                prompt = build(snapshot(hands_authorized=True, instruction="修改手部为美国大叔的手"))
                self.assertIn("本张必须改手：按「修改手部为美国大叔的手」改", prompt)
                self.assertIn("本次额外要求（优先执行）：修改手部为美国大叔的手", prompt)
                self.assertIn("握法", prompt)
                self.assertLess(prompt.index("本张必须改手"), prompt.index("本次画面要求") if "本次画面要求" in prompt else len(prompt))

    def test_authorized_hands_change_by_default_and_a_saved_look_wins_over_the_style(self):
        unrelated = generation_prompt(snapshot(hands_authorized=True, instruction="别动马克笔划线"))
        self.assertIn(f"本张必须改手：按「{DEFAULT_HAND_LOOK}」改", unrelated)
        saved = direct_image_prompt(snapshot(hands_authorized=True, hand_look="年轻女性的手，指甲干净", style="手换成粗糙的工匠手"))
        self.assertIn("本张必须改手：按「年轻女性的手，指甲干净」改", saved)
        from_style = direct_image_prompt(snapshot(hands_authorized=True, style="手换成粗糙的工匠手"))
        self.assertIn("本张必须改手：按「手换成粗糙的工匠手」改", from_style)
        this_run = direct_image_prompt(snapshot(hands_authorized=True, hand_look="年轻女性的手", instruction="这张换成美国大叔的手"))
        self.assertIn("本张必须改手：按「这张换成美国大叔的手」改", this_run)

    def test_hand_look_is_optional_and_kept_as_a_product_default(self):
        self.assertNotIn("hand_look", carousel_requirements(item(hand_look="  ")))
        self.assertEqual(carousel_requirements(item(hand_look="美国大叔的手"))["hand_look"], "美国大叔的手")
        self.assertTrue({"hand_look", "caption_style", "overlay_only"} <= set(PRODUCT_REQUIREMENT_KEYS))

    def test_no_authorization_keeps_the_hands(self):
        locked = direct_image_prompt(snapshot(instruction="修改手部为美国大叔的手"))
        self.assertIn("手部没有获准改变", locked)
        self.assertNotIn("本张必须改手", locked)


class ReviewTests(unittest.TestCase):
    def answer(self, body):
        checks = [{"id": key, "status": "pass", "note": "ok"} for key in ("visual", "text", "product", "evidence")]
        return json.dumps({"summary": "ok", "issues": [], "observed_title_lines": SLIDE["title_lines"],
                           "observed_body": body, "checks": checks})

    def test_overlay_only_render_without_body_reads_back_as_correct(self):
        value = snapshot(overlay_only=True)
        review = normalize_review(self.answer(""), value["slide"], (900, 1200), "3:4", requirements=value["requirements"])
        self.assertEqual(next(check for check in review["checks"] if check["id"] == "text")["status"], "pass")
        stray = normalize_review(self.answer(SLIDE["body"]), value["slide"], (900, 1200), "3:4", requirements=value["requirements"])
        self.assertEqual(next(check for check in stray["checks"] if check["id"] == "text")["status"], "fail")


if __name__ == "__main__":
    unittest.main()
