"""改稿 on a candidate draft: fake bridges and disposable case folders only."""
import json
from pathlib import Path
import sys
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))
import ai_tasks
import manual_edit
import test_ai_tasks as base
from test_ai_tasks import FakeBridge
from variant_parse import parse_variants, swap_notes


def draft(number, rows):
    table = "\n".join(f"| {index} | {spoken} | 画面 {number}-{index} | 标签 | 证据 |" for index, (spoken, _) in enumerate(rows, 1))
    return (f"**变体 #{number} · 结构 {number}**\n\n开头屏显：`Hook {number}`（钩子）\n\n**口播（配音用）**\n\n```\n"
            + "\n".join(spoken for spoken, _ in rows) + "\n```\n\n**对应中文**\n\n```\n" + "\n".join(zh for _, zh in rows)
            + "\n```\n\n**意群表**\n\n| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |\n|---|---|---|---|---|\n" + table
            + "\n\n可换句：第 3 行\n\n```\n🔴 Title " + str(number) + " #tag\n```\n")


FIRST = [("My knives were always dull.", "我的刀总是钝的。"), ("Then I found this sharpener.", "后来我找到了这个磨刀器。"),
         ("It comes with a four year warranty.", "它有四年质保。")]
SECOND = [("Stop guessing the angle.", "别再猜角度了。"), ("This holds it for you.", "它帮你固定住。")]
CANDIDATE = "写作说明\n\n" + draft(1, FIRST) + "\n---\n\n" + draft(2, SECOND) + "\n**发布前核对**\n\n- 价格待核\n"


def rows(*items):
    return [{"source": source, "spoken": spoken, "zh": zh, "translate": translate} for source, spoken, zh, translate in items]


class RebuildTests(unittest.TestCase):
    def test_a_reworded_line_replaces_its_blocks_and_leaves_everything_else(self):
        record = manual_edit.prepare(CANDIDATE, 0, rows((2, FIRST[1][0], FIRST[1][1], False), (1, "My knives got dull fast.", "我的刀钝得很快。", False),
                                                        (None, "No more guessing.", "不用再猜了。", False), (3, FIRST[2][0], FIRST[2][1], False)))
        self.assertEqual(manual_edit.pending(record), [])
        result = manual_edit.build(CANDIDATE, record)
        first, second = parse_variants(result, loose=False)
        self.assertEqual([line["spoken"] for line in first["lines"]],
                         [FIRST[1][0], "My knives got dull fast.", "No more guessing.", FIRST[2][0]])
        self.assertEqual([line["zh"] for line in first["lines"]], [FIRST[1][1], "我的刀钝得很快。", "不用再猜了。", FIRST[2][1]])
        self.assertEqual([line["proof"] for line in first["lines"]], ["画面 1-2", "待更新：画面 1-1", "待补", "画面 1-3"])
        self.assertEqual(first["problems"], [])
        self.assertEqual(swap_notes(result, 0)["line"], 4)
        self.assertEqual(swap_notes(result, 1)["line"], 3)
        self.assertEqual([line["spoken"] for line in second["lines"]], [spoken for spoken, _ in SECOND])
        self.assertEqual(result.split("变体 #2")[1], CANDIDATE.split("变体 #2")[1])
        self.assertEqual(record["lines"][1]["before"], {"spoken": FIRST[0][0], "zh": FIRST[0][1]})
        self.assertNotIn("before", record["lines"][0])

    def test_removed_lines_are_recorded_and_no_change_is_refused(self):
        record = manual_edit.prepare(CANDIDATE, 1, rows((1, SECOND[0][0], SECOND[0][1], False)))
        self.assertEqual(record["removed"], [{"number": 2, "spoken": SECOND[1][0], "zh": SECOND[1][1]}])
        with self.assertRaises(manual_edit.EditError):
            manual_edit.prepare(CANDIDATE, 1, rows(*[(number, spoken, zh, False) for number, (spoken, zh) in enumerate(SECOND, 1)]))
        with self.assertRaises(manual_edit.EditError):
            manual_edit.prepare(CANDIDATE, 1, rows((1, SECOND[0][0], "", False)))
        with self.assertRaises(manual_edit.EditError):
            manual_edit.prepare(CANDIDATE, 1, rows((1, "", "只有中文", False)))
        with self.assertRaises(manual_edit.EditError):
            manual_edit.prepare(CANDIDATE, 5, rows((1, "a", "b", False)))
        with self.assertRaises(manual_edit.EditError):
            manual_edit.prepare("**English**\n\nHello there.", 0, rows((1, "a", "b", False)))

    def test_only_the_marked_lines_are_asked_for_and_taken(self):
        record = manual_edit.prepare(CANDIDATE, 0, rows((1, FIRST[0][0], FIRST[0][1], False), (2, FIRST[1][0], "直到我换了这个带角度的磨刀器。", True),
                                                        (3, FIRST[2][0], FIRST[2][1], False)))
        self.assertEqual(manual_edit.pending(record), [2])
        prompt = manual_edit.prompt(record, "de")
        self.assertIn("2. 【重写】中文：直到我换了这个带角度的磨刀器。", prompt)
        self.assertIn("德语", prompt)
        self.assertIn("只写第 2 句", prompt)
        with self.assertRaises(manual_edit.EditError):
            manual_edit.build(CANDIDATE, record)
        done = manual_edit.answer(record, '```json\n{"lines":[{"number":2,"spoken":"Until I switched to this angle guide."},{"number":1,"spoken":"ignored"}]}\n```')
        self.assertEqual([line["spoken"] for line in done["lines"]], [FIRST[0][0], "Until I switched to this angle guide.", FIRST[2][0]])
        self.assertTrue(done["lines"][1]["translated"])
        self.assertEqual(done["lines"][1]["before"]["spoken"], FIRST[1][0])
        for bad in ("不是 JSON", '{"lines":[]}', '{"lines":[{"number":2,"spoken":"混了中文 here"}]}'):
            with self.assertRaises(manual_edit.EditError):
                manual_edit.answer(record, bad)


class RoundTests(unittest.TestCase):
    setUp, tearDown = base.TaskTests.setUp, base.TaskTests.tearDown
    new_manager, body, wait, path = base.TaskTests.new_manager, base.TaskTests.body, base.TaskTests.wait, base.TaskTests.path

    def candidate_task(self, manager=None):
        manager = manager or self.manager
        task = self.wait(manager.create(self.identifier, self.body("variants", "写两条。"))["task_id"], manager)
        path = self.path(task["task_id"])
        raw = json.loads(path.read_text(encoding="utf-8"))
        # The temporary folder has no writing skill, so the first round is settled by hand with the candidate under test.
        raw.update(status="completed")
        raw["rounds"][-1].update(status="completed", output=CANDIDATE, error=None)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        manager.clear_cache()
        return manager.get(task["task_id"])

    def edit(self, task, lines, index=0, manager=None, round_id=None):
        return (manager or self.manager).continue_task(task["task_id"], {
            "request_id": uuid4().hex, "task_revision": task["revision"],
            "manual_edit": {"round_id": round_id or task["rounds"][-1]["round_id"], "variant_index": index, "lines": lines}})

    def test_a_hand_edit_without_translation_is_a_finished_round_and_carries_marks(self):
        task = self.candidate_task()
        calls = len(self.bridges[0].calls)
        first = task["rounds"][-1]["round_id"]
        for index in (0, 1):
            self.manager.set_variant_choice(task["task_id"], {"round_id": first, "variant_index": index, "choice": "skip"})
        self.assertEqual(self.manager.settle_variant_choices(task["task_id"], {})["management_state"], "finished")
        body = {"request_id": uuid4().hex, "task_revision": task["revision"], "manual_edit": {
            "round_id": first, "variant_index": 0, "lines": rows((1, "My knives got dull fast.", FIRST[0][1], False),
                                                                 (2, FIRST[1][0], FIRST[1][1], False), (3, FIRST[2][0], FIRST[2][1], False))}}
        saved = self.manager.continue_task(task["task_id"], body)
        self.assertEqual(self.manager.continue_task(task["task_id"], body)["revision"], saved["revision"])
        turn = saved["rounds"][-1]
        self.assertEqual((saved["status"], turn["status"], turn["kind"], len(saved["rounds"])), ("completed", "completed", "manual_edit", 2))
        self.assertEqual(len(self.bridges[0].calls), calls)
        self.assertEqual(turn["manual_edit"]["source_round_id"], first)
        self.assertEqual(parse_variants(turn["output"])[0]["lines"][0]["spoken"], "My knives got dull fast.")
        self.assertEqual(saved["rounds"][0]["output"], CANDIDATE)
        self.assertEqual(saved["management_state"], "pending_selection")
        self.assertEqual(saved["variant_choices"][turn["round_id"]], {"total": 2, "picks": {"1": "skip"}})
        self.assertTrue(turn["read_estimates"])
        # The untouched draft keeps the script made from it in the round before; the rewritten one does not.
        made = {(task["task_id"], first): {0, 1}}
        self.assertEqual(ai_tasks.made_here(made, task["task_id"], turn["round_id"], ai_tasks.edit_chain(saved["rounds"], turn["round_id"])), {1})
        listed = next(item for item in self.manager.list_tasks() if item["task_id"] == task["task_id"])
        self.assertEqual(listed["latest_round_chain"], [[first, [0]]])

    def test_changed_chinese_goes_to_the_writer_and_comes_back_in_place(self):
        bridge = FakeBridge(output=json.dumps({"lines": [{"number": 2, "spoken": "Until I switched to this one."}]}))
        manager = self.new_manager(bridge)
        task = self.candidate_task(manager)
        calls = len(bridge.calls)
        queued = self.edit(task, rows((1, FIRST[0][0], FIRST[0][1], False), (2, FIRST[1][0], "直到我换了这个。", True),
                                      (3, FIRST[2][0], FIRST[2][1], False)), manager=manager)
        self.assertEqual(queued["rounds"][-1]["kind"], "manual_edit")
        done = self.wait(task["task_id"], manager)
        turn = done["rounds"][-1]
        self.assertEqual((done["status"], turn["status"]), ("completed", "completed"))
        self.assertEqual(len(bridge.calls), calls + 1)
        self.assertIn("【重写】中文：直到我换了这个。", bridge.calls[-1]["prompt"])
        first = parse_variants(turn["output"])[0]
        self.assertEqual([line["spoken"] for line in first["lines"]], [FIRST[0][0], "Until I switched to this one.", FIRST[2][0]])
        self.assertEqual(first["lines"][1]["zh"], "直到我换了这个。")
        self.assertEqual(first["lines"][1]["proof"], "待更新：画面 1-2")
        self.assertTrue(turn["manual_edit"]["lines"][1]["translated"])
        self.assertEqual(turn["manual_edit"]["lines"][1]["before"]["spoken"], FIRST[1][0])

    def test_an_unusable_answer_fails_the_round_and_keeps_what_was_typed(self):
        manager = self.new_manager(FakeBridge(output="抱歉，我不能。"))
        task = self.candidate_task(manager)
        self.edit(task, rows((1, FIRST[0][0], "我的刀怎么磨都不快。", True), (2, FIRST[1][0], FIRST[1][1], False),
                             (3, FIRST[2][0], FIRST[2][1], False)), manager=manager)
        failed = self.wait(task["task_id"], manager)
        turn = failed["rounds"][-1]
        self.assertEqual((failed["status"], turn["status"]), ("failed", "failed"))
        self.assertIn("没按约定格式", turn["error"])
        self.assertEqual(turn["manual_edit"]["lines"][0]["zh"], "我的刀怎么磨都不快。")
        self.assertEqual(failed["rounds"][0]["output"], CANDIDATE)

    def test_blank_retry_retranslates_the_saved_edit_and_keeps_untouched_drafts(self):
        bridge = FakeBridge(error=RuntimeError("临时连接失败"))
        manager = self.new_manager(bridge)
        task = self.candidate_task(manager)
        source = task["rounds"][-1]["round_id"]
        manager.set_variant_choice(task["task_id"], {"round_id": source, "variant_index": 1, "choice": "skip"})
        self.edit(task, rows((1, FIRST[0][0], "我的刀怎么磨都不快。", True), (2, FIRST[1][0], FIRST[1][1], False),
                             (3, FIRST[2][0], FIRST[2][1], False)), manager=manager)
        failed = self.wait(task["task_id"], manager)
        self.assertEqual(failed["status"], "failed")
        bridge.error = None
        bridge.output = json.dumps({"lines": [{"number": 1, "spoken": "My knives just would not get sharp."}]})
        request = {"request_id": uuid4().hex, "task_revision": failed["revision"], "instruction": ""}
        queued = manager.continue_task(task["task_id"], request)
        turn = queued["rounds"][-1]
        self.assertEqual((turn["kind"], turn["instruction"], turn["manual_edit"]),
                         ("manual_edit", failed["rounds"][-1]["instruction"], failed["rounds"][-1]["manual_edit"]))
        self.assertEqual(queued["rounds"][:-1], failed["rounds"])
        self.assertEqual(queued["variant_choices"][turn["round_id"]], {"total": 2, "picks": {"1": "skip"}})
        finished = self.wait(task["task_id"], manager)
        self.assertEqual(finished["status"], "completed")
        self.assertIn("【重写】中文：我的刀怎么磨都不快。", bridge.calls[-1]["prompt"])
        variants = parse_variants(finished["rounds"][-1]["output"])
        self.assertEqual([line["spoken"] for line in variants[0]["lines"]],
                         ["My knives just would not get sharp.", FIRST[1][0], FIRST[2][0]])
        self.assertEqual([line["spoken"] for line in variants[1]["lines"]], [spoken for spoken, _ in SECOND])
        self.assertEqual(finished["rounds"][0]["output"], CANDIDATE)
        self.assertEqual(ai_tasks.edit_chain(finished["rounds"], turn["round_id"]), [[source, [0]]])
        count = len(bridge.calls)
        manager.continue_task(task["task_id"], request)
        self.assertEqual(len(bridge.calls), count)

    def test_refusals(self):
        task = self.candidate_task()
        with self.assertRaises(ai_tasks.AITaskError):
            self.edit(task, rows(*[(number, spoken, zh, False) for number, (spoken, zh) in enumerate(FIRST, 1)]))
        with self.assertRaises(ai_tasks.AITaskError):
            self.edit(task, rows((1, "a", "b", False)), round_id="AIR-000000000000")
        with self.assertRaises(ai_tasks.AITaskError):
            self.edit({**task, "revision": task["revision"] - 1}, rows((1, "a", "b", False)))
        self.assertEqual(len(self.manager.get(task["task_id"])["rounds"]), 1)


if __name__ == "__main__":
    unittest.main()
