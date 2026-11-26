"""改稿 on the candidate desk; isolated workbench and mocked AI records."""
from __future__ import annotations

import copy
import unittest

import test_ai_production_entry_ui as entry
from test_ai_production_entry_ui import ROUND_ID, TASK_ID, titled_variant

NEW_ROUND = "AIR-ba9876543210"
OUTPUT = titled_variant(1, "先说烦恼", "First line 1.") + titled_variant(2, "先给结果", "First line 2.")


class ManualEditBrowserTests(unittest.TestCase):
    setUpClass = entry.AIProductionEntryBrowserTests.__dict__["setUpClass"]
    tearDownClass = entry.AIProductionEntryBrowserTests.__dict__["tearDownClass"]
    stop_server = entry.AIProductionEntryBrowserTests.stop_server
    request = entry.AIProductionEntryBrowserTests.request
    setUp = entry.AIProductionEntryBrowserTests.setUp
    open_candidate = entry.AIProductionEntryBrowserTests.open_candidate

    def test_rewriting_chinese_marks_lines_for_the_writer_and_saves_a_new_round(self):
        scripts = [{"id": "SCR-0123456789ab", "script_id": "SCR-0123456789ab", "family_id": "SCR-0123456789ab", "version": 1, "product": "202H",
                    "title": "变体 #2", "status": "adopted", "archived": False,
                    "source": {"task_id": TASK_ID, "round_id": ROUND_ID, "variant_index": 1}, "production_usage": []}]
        page, task, _, _ = self.open_candidate(OUTPUT, scripts=scripts)
        sent = []

        def save(route):
            body = route.request.post_data_json
            sent.append(body)
            lines = body["manual_edit"]["lines"]
            written = [{**line, "spoken": f"Rewritten {number}." if line["translate"] else line["spoken"], "translate": False,
                        **({"translated": True} if line["translate"] else {}),
                        **({"before": {"spoken": "First line 1.", "zh": "第一句 1。"}} if line["source"] == 1 else {})}
                       for number, line in enumerate(lines, 1)]
            first = titled_variant(1, "先说烦恼", "First line 1.").replace(
                "First line 1.\nSecond line 1.", "\n".join(line["spoken"] for line in written)).replace(
                "第一句 1。\n第二句 1。", "\n".join(line["zh"] for line in written))
            task["rounds"].append({"round_id": NEW_ROUND, "status": "completed", "instruction": "人工改稿", "kind": "manual_edit",
                                   "output": first + titled_variant(2, "先给结果", "First line 2."), "messages": [], "skill_selection": "none",
                                   "manual_edit": {"source_round_id": ROUND_ID, "variant_index": 0, "lines": written, "removed": []}})
            route.fulfill(json={"task": copy.deepcopy(task)})

        page.route(f"**/api/ai/tasks/{TASK_ID}/continue", save)
        card = page.locator(f'.ai-round[data-round-id="{ROUND_ID}"] .ai-variant-card[data-variant-card="1"]')
        card.locator("[data-desk-edit]").click()
        rows = card.locator("[data-edit-row]")
        self.assertEqual(rows.count(), 2)
        button = card.locator("[data-edit-save]")
        self.assertTrue(button.is_disabled())
        self.assertFalse(card.locator(".ai-variant-actions").is_visible())
        # Rewriting the Chinese of a line sends it to the writer; typing its spoken words keeps them as typed.
        rows.nth(0).locator("[data-edit-field=zh]").fill("我的刀怎么磨都不快。")
        self.assertEqual(rows.nth(0).locator(".ai-edit-state").inner_text(), "待重译")
        self.assertEqual(button.inner_text(), "重译 1 句并存为新一轮")
        rows.nth(1).locator("[data-edit-field=spoken]").fill("Second line, my way.")
        self.assertEqual(rows.nth(1).locator(".ai-edit-state").inner_text(), "手改，照你写的存")
        self.assertEqual(button.inner_text(), "重译 1 句并存为新一轮")
        rows.nth(1).locator("[data-edit-act=revert]").click()
        self.assertEqual(rows.nth(1).locator("[data-edit-field=spoken]").input_value(), "Second line 1.")
        # A new line needs only its Chinese; moving it up keeps what was typed.
        rows.nth(1).locator("[data-edit-act=add]").click()
        self.assertEqual(rows.count(), 3)
        rows.nth(2).locator("[data-edit-field=zh]").fill("所以我换了这个。")
        rows.nth(2).locator("[data-edit-act=up]").click()
        self.assertEqual(rows.nth(1).locator("[data-edit-field=zh]").input_value(), "所以我换了这个。")
        self.assertEqual(button.inner_text(), "重译 2 句并存为新一轮")
        button.click()
        made = page.locator(f'.ai-round[data-round-id="{NEW_ROUND}"] .ai-variant-card[data-variant-card="1"]')
        made.wait_for()
        self.assertEqual(sent[0]["manual_edit"], {"round_id": ROUND_ID, "variant_index": 0, "lines": [
            {"source": 1, "spoken": "First line 1.", "zh": "我的刀怎么磨都不快。", "translate": True},
            {"source": None, "spoken": "", "zh": "所以我换了这个。", "translate": True},
            {"source": 2, "spoken": "Second line 1.", "zh": "第二句 1。", "translate": False}]})
        self.assertEqual(sent[0]["task_revision"], "fixture-revision")
        # The new round opens on the rewritten draft, shows what changed, and the old round is untouched.
        self.assertTrue(made.is_visible())
        self.assertIn("重译 1 句", made.locator(".ai-edit-summary").inner_text())
        self.assertIn("新增 1 句", made.locator(".ai-edit-summary").inner_text())
        self.assertEqual(made.locator(".ai-variant-pairs .ai-edit-tag").all_inner_texts(), ["重译", "新增"])
        self.assertEqual(made.locator(".ai-variant-pairs .ai-variant-was").all_inner_texts(), ["First line 1.", "第一句 1。"])
        self.assertFalse(made.locator(".ai-edit").is_visible())
        # The draft that was not touched keeps the script made from it in the round before.
        other = page.locator(f'.ai-round[data-round-id="{NEW_ROUND}"] .ai-variant-card[data-variant-card="2"]')
        page.locator(f'.ai-round[data-round-id="{NEW_ROUND}"] [data-variant-pick="2"]').click()
        self.assertIn("has-script", other.get_attribute("class"))
        self.assertNotIn("has-script", made.get_attribute("class"))

    def test_cancel_leaves_the_draft_as_it_was(self):
        page, *_ = self.open_candidate(OUTPUT)
        card = page.locator('.ai-variant-card[data-variant-card="1"]')
        card.locator("[data-desk-edit]").click()
        card.locator("[data-edit-row]").nth(0).locator("[data-edit-act=remove]").click()
        self.assertEqual(card.locator("[data-edit-row]").count(), 1)
        page.once("dialog", lambda dialog: dialog.accept())
        card.locator("[data-edit-cancel]").click()
        self.assertFalse(card.locator(".ai-edit").is_visible())
        self.assertEqual(card.locator(".ai-variant-pairs p").count(), 4)
        self.assertTrue(card.locator(".ai-variant-actions").is_visible())
        card.locator("[data-desk-edit]").click()
        self.assertEqual(card.locator("[data-edit-row]").count(), 2)


if __name__ == "__main__":
    unittest.main()
