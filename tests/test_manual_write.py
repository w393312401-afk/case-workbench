"""手写新稿: a script typed from nothing becomes a finished round with no AI call."""
from pathlib import Path
import sys
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))
import ai_tasks
import manual_write
import test_ai_tasks as base
import test_manual_edit as edit
from variant_parse import parse_variants

LINES = [{"spoken": "My knives were always dull.", "zh": "我的刀总是钝的。"},
         {"spoken": "Then I found this sharpener.", "zh": "后来我找到了这个磨刀器。"}]


class BuildTests(unittest.TestCase):
    def test_the_text_parses_like_a_delivered_draft(self):
        title, hook, lines = manual_write.clean({"title": " 角度顾虑 ", "hook": "Stop guessing", "lines": LINES})
        parsed = parse_variants(manual_write.build(title, hook, lines), loose=False)
        self.assertEqual(len(parsed), 1)
        self.assertEqual([line["spoken"] for line in parsed[0]["lines"]], [line["spoken"] for line in LINES])
        self.assertEqual([line["zh"] for line in parsed[0]["lines"]], [line["zh"] for line in LINES])

    def test_missing_or_oversized_input_is_refused(self):
        for bad in ({"lines": []}, {"lines": [{"spoken": "a", "zh": ""}]}, {"lines": [{"spoken": "", "zh": "中"}]},
                    {"lines": [{"spoken": "```", "zh": "中"}]}, {"lines": [{"spoken": "x" * 601, "zh": "中"}]}, "text"):
            with self.assertRaises(manual_write.WriteError):
                manual_write.clean(bad)


SPOKEN = ["I get it—not every working knife needs a mirror finish.", "So now I clamp the knife in the 202H.",
          "You get free returns for 30 days, so check out the full 202H set below."]
CHINESE = ["我懂，不是每把干活的刀都要磨成镜面。", "所以现在我把刀夹进 202H。", "30 天免费退货，下方看看整套 202H。"]
# The way a chat reply arrives through its copy button: fenced 口播, numbered 中文, a real table, fenced 🔴 lines.
MARKDOWN = "\n".join([
    "开头逻辑：A", "", "**开头画面**（待核）：对镜，把刀转向镜头。", "",
    "开头屏显：`Skipping the mirror finish? / Here's the part I wouldn't skip 🔪`（不追镜面？这一步我不会省）",
    "屏显搭配：点破。前 3 秒配音念到 \"mirror finish\"", "",
    "屏显备选：同写｜`Mirror finish on a work knife? / Not for me ✋`（工作刀磨镜面？我不用）", "",
    "**口播（配音用）**", "", "```", *SPOKEN, "```", "", "**对应中文**", "",
    *[f"{number}. {line}" for number, line in enumerate(CHINESE, 1)], "",
    "**意群表**", "", "| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |", "|---|---|---|---|---|",
    "| 1 | I get it… | 非镜面的工作刀 | 新拍：对镜持刀 | 展示 |", "| 2 | So now I clamp… | 夹刀 | 组装 | 动作演示 |",
    "| 3 | You get free returns… | 结尾 CTA | 闭合工具箱 | 展示 |", "",
    "**补拍清单**", "", "- 同一把刀磨完当场切纸。", "",
    "可换句：第 2 行", "", "可换句备选：平时版｜`My kitchen knives are next.`（下一批就磨我的厨刀。）", "",
    "**发布文案**", "", "```", "🔴A Working Edge#SHARPAL202H #knifesharpening", "```", "",
    "```", "🔴Skip the Mirror#SHARPAL202H #knifesharpener", "```", "", "中文对照：干活的刃口／镜面可以省。"])
# The same reply selected on screen and copied: no fences, no table bars, no bold.
RENDERED = "\n".join([
    "开头屏显：Skipping the mirror finish? / Here's the part I wouldn't skip 🔪（不追镜面？这一步我不会省）", "",
    "口播（配音用）", "", *SPOKEN, "", "对应中文", "", *[f"{number}. {line}" for number, line in enumerate(CHINESE, 1)], "",
    "意群表", "", "#\t口播\t画面要证明什么\t素材标签\t证据", "1\tI get it…\t非镜面的工作刀\t新拍：对镜持刀\t展示",
    "2\tSo now I clamp…\t夹刀\t组装\t动作演示", "3\tYou get free returns…\t结尾 CTA\t闭合工具箱\t展示", "",
    "发布文案", "", "🔴A Working Edge#SHARPAL202H #knifesharpening"])


class ImportTests(unittest.TestCase):
    def test_a_copied_reply_is_kept_whole(self):
        text, label, count = manual_write.import_text(MARKDOWN, "不追镜面")
        self.assertEqual((label, count), ("变体 #1 · 不追镜面", 3))
        parsed = parse_variants(text, loose=False)[0]
        self.assertEqual([(line["spoken"], line["zh"]) for line in parsed["lines"]], list(zip(SPOKEN, CHINESE)))
        self.assertEqual((parsed["lines"][1]["tags"], parsed["problems"]), ("组装", []))
        found = manual_write.preview(MARKDOWN, "不追镜面")
        self.assertTrue(found["recognized"])
        self.assertEqual({key: found["variants"][0][key] for key in ("lines", "chinese", "table", "screens", "pickups", "swap", "titles")},
                         {"lines": 3, "chinese": 3, "table": 3, "screens": 2, "pickups": 1, "swap": True, "titles": 2})

    def test_a_reply_copied_from_the_screen_is_rebuilt_without_changing_a_word(self):
        text, _, count = manual_write.import_text(RENDERED)
        parsed = parse_variants(text, loose=False)[0]
        self.assertEqual(count, 3)
        self.assertEqual([(line["spoken"], line["zh"], line["evidence"]) for line in parsed["lines"]],
                         list(zip(SPOKEN, CHINESE, ["展示", "动作演示", "展示"])))
        self.assertEqual((parsed["label"], parsed["problems"]), ("变体 #1 · 导入稿", []))

    def test_mismatched_chinese_is_reported_not_guessed(self):
        found = manual_write.preview(MARKDOWN.replace("3. " + CHINESE[2], ""))
        self.assertEqual(found["variants"][0]["chinese"], 0)
        self.assertTrue(any("中文 2 行" in problem for problem in found["variants"][0]["problems"]))

    def test_text_without_sections_falls_back_to_rows(self):
        found = manual_write.preview("\n".join(SPOKEN + [""] + CHINESE))
        self.assertFalse(found["recognized"])
        self.assertEqual(found["rows"], [{"spoken": spoken, "zh": zh} for spoken, zh in zip(SPOKEN, CHINESE)])
        for bad in ("", "   ", "x" * 60001, None):
            with self.assertRaises(manual_write.WriteError):
                manual_write.preview(bad)
        with self.assertRaises(manual_write.WriteError):
            manual_write.import_text("\n".join(SPOKEN))


class RoundTests(unittest.TestCase):
    setUp, tearDown = base.TaskTests.setUp, base.TaskTests.tearDown
    new_manager, body, wait, path = base.TaskTests.new_manager, base.TaskTests.body, base.TaskTests.wait, base.TaskTests.path
    candidate_task = edit.RoundTests.candidate_task

    def test_a_hand_written_round_is_saved_at_once_without_the_assistant(self):
        task = self.candidate_task()
        calls = len(self.bridges[0].calls)
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "manual_write": {"title": "手写", "hook": "", "lines": LINES}}
        saved = self.manager.continue_task(task["task_id"], body)
        self.assertEqual(self.manager.continue_task(task["task_id"], body)["revision"], saved["revision"])
        turn = saved["rounds"][-1]
        self.assertEqual((saved["status"], turn["status"], turn["kind"], len(saved["rounds"])), ("completed", "completed", "manual_write", 2))
        self.assertEqual(len(self.bridges[0].calls), calls)
        self.assertEqual(parse_variants(turn["output"])[0]["lines"][1]["zh"], LINES[1]["zh"])

    def test_a_pasted_delivery_becomes_a_round_without_the_assistant(self):
        task = self.candidate_task()
        calls = len(self.bridges[0].calls)
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "manual_import": {"text": MARKDOWN, "title": "不追镜面"}}
        saved = self.manager.continue_task(task["task_id"], body)
        turn = saved["rounds"][-1]
        self.assertEqual((saved["status"], turn["status"], turn["kind"], turn["instruction"]),
                         ("completed", "completed", "manual_write", "导入整稿 · 变体 #1 · 不追镜面（3 句）"))
        self.assertEqual(len(self.bridges[0].calls), calls)
        self.assertIn("🔴Skip the Mirror#SHARPAL202H", turn["output"])
        with self.assertRaises(ai_tasks.AITaskError):
            self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, "task_revision": saved["revision"],
                                                         "manual_import": {"text": "\n".join(SPOKEN)}})

    def test_a_stale_revision_or_bad_lines_are_refused(self):
        task = self.candidate_task()
        for body in ({"task_revision": task["revision"] + 5, "manual_write": {"lines": LINES}},
                     {"task_revision": task["revision"], "manual_write": {"lines": [{"spoken": "a", "zh": ""}]}}):
            with self.assertRaises(ai_tasks.AITaskError):
                self.manager.continue_task(task["task_id"], {"request_id": uuid4().hex, **body})
        self.assertEqual(len(self.manager.get(task["task_id"])["rounds"]), 1)


if __name__ == "__main__":
    unittest.main()
