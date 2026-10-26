"""Variant output that misses the delivery format: loose parsing, re-layout verification and the format pass."""
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_ai_tasks as base
from variant_parse import (delivery_ready, looks_like_script, parse_carousels, parse_variants, reformat_problems,
                           variant_notes, variant_script)

# Shape of a real round (AIT-37ea44f7c61d, 2026-10-09) that ignored the code-block format.
LOOSE = """# 202H-S2 三个版本(口播候选)

核对范围:只读了 202H 产品资料。

---

## 版本 A:先认"七块太夸张",再说挑起点

测试方向:202H-S2 执行变体。
开头画面：磨板一字排开。

**English**

> Seven plates, from diamond to leather. Yeah, I get it. That sounds like overkill for a knife sharpener.
>
> But look what's in the case. The clamp holds the knife, so you're not holding the angle.
>
> Full 202H kit's down below.

**中文**

> 七块磨板,从金刚石到皮面。我懂,对一个磨刀器来说听起来太夸张了。
>
> 但看看箱子里有什么。夹具夹住刀,不用靠手腕去守角度。
>
> 整套 202H 在下方。

发布文案:
```
🔴Seven Plates for One Sharpener?#SHARPAL202H #knifesharpening
```

---

## 版本 B:崩口旧刀

**English**

> This knife had chips in the edge. Now it's like this.

**中文**

> 这把刀刃上有崩口。现在是这样。
"""

FORMATTED = """**版本 A:先认"七块太夸张",再说挑起点**

开头画面：磨板一字排开。

**口播（配音用）**

```
Seven plates, from diamond to leather.
Yeah, I get it. That sounds like overkill for a knife sharpener.
But look what's in the case.
The clamp holds the knife, so you're not holding the angle.
Full 202H kit's down below.
```

**对应中文**

```
七块磨板,从金刚石到皮面。
我懂,对一个磨刀器来说听起来太夸张了。
但看看箱子里有什么。
夹具夹住刀,不用靠手腕去守角度。
整套 202H 在下方。
```

**版本 B:崩口旧刀**

**口播（配音用）**

```
This knife had chips in the edge.
Now it's like this.
```

**对应中文**

```
这把刀刃上有崩口。
现在是这样。
```
"""


class LooseParseTests(unittest.TestCase):
    def test_english_quote_paragraphs_become_flagged_variants_without_changing_words(self):
        found = parse_variants(LOOSE)
        self.assertEqual([item["label"] for item in found], ['版本 A:先认"七块太夸张",再说挑起点', "版本 B:崩口旧刀"])
        first = found[0]
        self.assertTrue(first["loose"])
        self.assertIn("宽松识别", first["problems"][0])
        self.assertEqual([line["spoken"] for line in first["lines"]], [
            "Seven plates, from diamond to leather. Yeah, I get it. That sounds like overkill for a knife sharpener.",
            "But look what's in the case.", "The clamp holds the knife, so you're not holding the angle.",
            "Full 202H kit's down below."])
        self.assertEqual(first["lines"][1]["zh"], "但看看箱子里有什么。")
        self.assertEqual(first["lines"][0]["zh"], "七块磨板,从金刚石到皮面。我懂,对一个磨刀器来说听起来太夸张了。")
        self.assertEqual([line["spoken"] for line in found[1]["lines"]], ["This knife had chips in the edge.", "Now it's like this."])
        self.assertFalse(any("🔴" in line["spoken"] for variant in found for line in variant["lines"]))

    def test_strict_mode_and_notes_follow_the_loose_sections(self):
        self.assertEqual(parse_variants(LOOSE, loose=False), [])
        self.assertEqual(variant_notes(LOOSE, 0)["opening"], "开头画面：磨板一字排开。")
        self.assertEqual(variant_notes(LOOSE, 1)["opening"], "")

    def test_strict_candidates_are_not_flagged(self):
        found = parse_variants(FORMATTED)
        self.assertEqual(len(found), 2)
        self.assertFalse(any(item["loose"] for item in found))
        self.assertEqual(found[0]["lines"][1]["zh"], "我懂,对一个磨刀器来说听起来太夸张了。")

    def test_plain_paragraphs_under_a_spoken_label_without_variant_headings(self):
        text = "说明一行。\n\n口播：\n\nTry this once. You'll see it.\n\n中文：\n\n试一次。你就知道了。\n"
        found = parse_variants(text)
        self.assertEqual([line["spoken"] for line in found[0]["lines"]], ["Try this once.", "You'll see it."])
        self.assertEqual([line["zh"] for line in found[0]["lines"]], ["试一次。", "你就知道了。"])
        self.assertEqual(found[0]["label"], "第 1 条")

    def test_analysis_without_spoken_text_still_yields_nothing(self):
        self.assertEqual(parse_variants("## 版本 A 更好\n\n因为开头更早给出结果。"), [])

    def test_carousel_json_without_language_tag_and_table_only_carousels(self):
        copy = {"content_type": "photo_carousel", "slides": [{"number": n, "title_lines": [f"Slide {n}"], "body": ""} for n in (1, 2, 3)],
                "caption_en": "Kit", "caption_zh": "套装"}
        untagged = "## 版本 A\n\n**English**\n\n> One kit.\n\n```\n" + json.dumps(copy) + "\n```\n"
        self.assertEqual([slide["title_lines"] for slide in parse_carousels(untagged)[0]["slides"]], [["Slide 1"], ["Slide 2"], ["Slide 3"]])
        table = ("## 版本 B\n\n| 张 | 作用 | 叠字 | 正文 | 中文 | 素材来源 | 禁写 |\n|---|---|---|---|---|---|---|\n"
                 "| 1 | 开场 | One kit / no jig | Open the case. | 一套 | 实拍 | 无 |\n| 2 | 证明 | Mirror edge | | 镜面 | 实拍 | 无 |\n"
                 "| 3 | 收口 | Below | | 下方 | 实拍 | 无 |\n")
        found = parse_carousels(table)
        self.assertEqual(found[0]["slides"][0]["title_lines"], ["One kit", "no jig"])
        self.assertEqual(found[0]["slides"][0]["body"], "Open the case.")
        self.assertIn("没有找到图文 JSON", found[0]["problems"][0])


# Shape of a real round (AIT-230fa117a95e, 2026-10-15): strict blocks, but variants named 版本 A/B with no 变体 number.
NAMED = """**核对范围：** 看了 8 帧。

---

## 版本 A｜与原稿一致

**202H-S2 · 版本 A（原稿原样）· 镜面经历 → 下方看 202H**

**开头画面：** 手里转刀。

**口播（配音用）**
```
I sharpened every EDC knife.
Check out the 202H below.
```

**对应中文**
```
我把每把刀都磨了。
点下面看看。
```

```
🔴Mirror Edge#SHARPAL202H #EDC
```

---

## 版本 B｜口袋刀是不是过头

**202H-S2 · 版本 B · 镜面经历 → 过头吗 → 下方看 202H**

执行变化：换观看问题。

**口播（配音用）**
```
Overkill for a pocket knife?
Maybe.
```

```
🔴Overkill#SHARPAL202H
```
"""


class NamedVariantTests(unittest.TestCase):
    def test_variants_named_by_version_letter_split_at_their_own_heading(self):
        found = parse_variants(NAMED)
        self.assertEqual([item["label"] for item in found], ["版本 A｜与原稿一致", "版本 B｜口袋刀是不是过头"])
        first, second = variant_script(NAMED, 0), variant_script(NAMED, 1)
        self.assertTrue(first.startswith("**开头画面：**"), first[:60])
        self.assertIn("I sharpened every EDC knife.", first)
        self.assertIn("🔴Mirror Edge", first)
        self.assertNotIn("版本 B", first)
        self.assertNotIn("核对范围", first)
        self.assertTrue(second.startswith("执行变化"), second[:60])
        self.assertNotIn("I sharpened", second)
        self.assertIn("🔴Overkill", second)


class ReformatCheckTests(unittest.TestCase):
    def test_delivery_ready_and_script_detection(self):
        self.assertFalse(delivery_ready(LOOSE))
        self.assertTrue(delivery_ready(FORMATTED))
        self.assertTrue(delivery_ready(FORMATTED, carousel=True))
        table = "\n| 张 | 作用 | 叠字 | 正文 |\n|---|---|---|---|\n| 1 | 开场 | One kit | |\n"
        self.assertFalse(delivery_ready(FORMATTED + table, carousel=True))
        self.assertTrue(looks_like_script(LOOSE))
        self.assertFalse(looks_like_script("只回答问题：版本 A 更适合冷启动。"))

    def test_faithful_relayout_passes(self):
        self.assertEqual(reformat_problems(LOOSE, FORMATTED), [])

    def test_changed_dropped_or_unformatted_relayouts_are_refused(self):
        changed = FORMATTED.replace("Now it's like this.", "Now it's razor sharp.")
        self.assertIn("口播和原稿逐字对不上", " ".join(reformat_problems(LOOSE, changed)))
        dropped = FORMATTED.split("**版本 B")[0]
        self.assertIn("只有 1 条", " ".join(reformat_problems(LOOSE, dropped)))
        chinese = FORMATTED.replace("现在是这样。", "现在锋利了。")
        self.assertIn("中文和原稿对不上", " ".join(reformat_problems(LOOSE, chinese)))
        self.assertTrue(reformat_problems(LOOSE, LOOSE))


class FormatPassTests(unittest.TestCase):
    tearDown = base.TaskTests.tearDown
    new_manager = base.TaskTests.new_manager
    body = base.TaskTests.body
    wait = base.TaskTests.wait
    path = base.TaskTests.path

    def setUp(self):
        base.TaskTests.setUp(self)
        base.install_review_fixture(self.skill)

    def scripted(self, *outputs):
        calls = []

        def run(prompt, **kwargs):
            calls.append({"prompt": prompt, **kwargs})
            return outputs[min(len(calls), len(outputs)) - 1]
        self.bridges[0].run = run
        return calls

    def test_prompts_carry_the_delivery_format_after_the_user_request(self):
        calls = self.scripted("初稿。", FORMATTED)
        done = self.wait(self.manager.create(self.identifier, self.body("variants", "做两个版本"))["task_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(calls), 2)
        for call in calls:
            self.assertLess(call["prompt"].index("当前用户要求：\n做两个版本"), call["prompt"].index("交付格式（工作台按此拆分"))
        self.assertIn("初稿没按此格式交付时，复核稿按此格式改正", calls[1]["prompt"])
        self.assertNotIn("或固定模板", calls[0]["prompt"])
        self.assertEqual(done["rounds"][-1]["format_check"], {"status": "ok"})

    def test_unformatted_review_is_relaid_out_and_verified(self):
        calls = self.scripted("初稿。", LOOSE, FORMATTED)
        done = self.wait(self.manager.create(self.identifier, self.body("variants", "做两个版本"))["task_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(calls), 3)
        self.assertIn("<untrusted_candidate>\n" + LOOSE, calls[2]["prompt"])
        self.assertEqual(calls[2]["skills"], [])
        self.assertNotIn("video_path", calls[2])
        turn = done["rounds"][-1]
        self.assertEqual(turn["output"], FORMATTED.strip())
        self.assertEqual(turn["format_check"]["status"], "repaired")
        raw = json.loads(self.path(done["task_id"]).read_text(encoding="utf-8"))["rounds"][-1]
        self.assertEqual(raw["stages"]["review"]["output"], LOOSE)
        self.assertEqual(raw["stages"]["format"]["status"], "completed")

    def test_relayout_that_changes_words_is_not_used(self):
        calls = self.scripted("初稿。", LOOSE, FORMATTED.replace("Now it's like this.", "Now it's razor sharp."))
        done = self.wait(self.manager.create(self.identifier, self.body("variants"))["task_id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(len(calls), 3)
        turn = done["rounds"][-1]
        self.assertEqual(turn["output"], LOOSE)
        self.assertEqual(turn["format_check"]["status"], "failed")
        self.assertIn("逐字对不上", turn["format_check"]["reason"])

    def test_answers_that_are_not_scripts_skip_the_format_pass(self):
        calls = self.scripted("初稿。", "版本 A 更适合冷启动，因为结果出现得更早。")
        done = self.wait(self.manager.create(self.identifier, self.body("variants"))["task_id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(done["rounds"][-1]["format_check"]["status"], "skipped")

    def test_format_round_relays_out_an_earlier_candidate_with_its_model_and_no_skills(self):
        calls = self.scripted("初稿。", LOOSE, "NO_SCRIPT", FORMATTED)
        done = self.wait(self.manager.create(self.identifier, self.body("variants"))["task_id"])
        self.assertEqual(done["rounds"][-1]["output"], LOOSE)
        source = done["rounds"][-1]["round_id"]
        queued = self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex, "task_revision": done["revision"],
                                                              "format_round_id": source})
        self.assertEqual(queued["rounds"][-1]["kind"], "format")
        self.assertIn("整理第 1 轮候选的格式", queued["rounds"][-1]["instruction"])
        finished = self.wait(done["task_id"])
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(len(calls), 4)
        self.assertEqual(finished["rounds"][-1]["output"], FORMATTED.strip())
        self.assertEqual(finished["rounds"][-1]["format_source"], source)
        self.assertEqual(calls[3]["skills"], [])

    def test_failed_format_round_keeps_nothing_and_reports_why(self):
        calls = self.scripted("初稿。", LOOSE, "NO_SCRIPT", FORMATTED.replace("七块磨板", "八块磨板"))
        done = self.wait(self.manager.create(self.identifier, self.body("variants"))["task_id"])
        self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex, "task_revision": done["revision"],
                                                     "format_round_id": done["rounds"][-1]["round_id"]})
        finished = self.wait(done["task_id"])
        self.assertEqual(finished["rounds"][-1]["status"], "failed")
        self.assertIn("中文和原稿对不上", finished["rounds"][-1]["error"])
        self.assertEqual(finished["rounds"][-1]["output"], "")

    def test_format_round_needs_a_completed_variant_round(self):
        done = self.wait(self.manager.create(self.identifier, self.body("review"))["task_id"])
        with self.assertRaises(base.AITaskError):
            self.manager.continue_task(done["task_id"], {"request_id": uuid4().hex, "task_revision": done["revision"],
                                                         "format_round_id": done["rounds"][-1]["round_id"]})


if __name__ == "__main__":
    unittest.main()
