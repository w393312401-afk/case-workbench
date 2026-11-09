"""Candidate production entry points; isolated workbench and mocked AI records."""
from __future__ import annotations

import unittest

import test_ai_selection_ui as browser
import test_server as fixture


TASK_ID = "AIT-0123456789ab"
ROUND_ID = "AIR-0123456789ab"
VOICE_OUTPUT = """## 变体 1：固定角度

**口播（配音用）**
```
Set the angle once. Keep it steady through every pass.
```

**中文对照**
先固定角度，每一遍都保持稳定。

**意群表**
| 口播意群 | 画面方向 |
| --- | --- |
| Set the angle once. | 调整角度 |
| Keep it steady through every pass. | 稳定推进 |
"""


def titled_variant(number, structure, first):
    """One variant as the writing skill lays it out, with a JSON carousel block under 图文版."""
    return f"""---

**202H-S2 · 变体 #{number} · {structure}**

开头画面：刃面倒影 {number}。

开头屏显：`Screen {number}`（屏显 {number}）

**口播（配音用）**

```
{first}
Second line {number}.
```

**对应中文**

```
第一句 {number}。
第二句 {number}。
```

**意群表**

| # | 口播 |
| --- | --- |
| 1 | {first} |

补拍清单：同一刃面 {number}。

**图文版**

```
{{"content_type": "photo_carousel", "variant": "#{number}"}}
```
"""


TWO_VARIANTS_FOR_ORDERS = chr(10).join(
    line for number in (1, 2) for line in (f"## 变体 #{number}", "**口播（配音用）**", "```", f"Line {number}.", "```", ""))


class AIProductionEntryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_candidate(self, output, *, action="variants", scripts=None):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        round_record = {
            "round_id": ROUND_ID, "status": "completed", "instruction": "临时写稿要求",
            "output": output, "messages": [], "skill_selection": "none",
        }
        task = {
            "task_id": TASK_ID, "revision": "fixture-revision", "status": "completed",
            "busy": False, "product": "202H", "action": action,
            "context": {"title": "临时候选口播"}, "rounds": [round_record],
        }
        posts, choices = [], []
        self.choices = choices

        def ai_api(route):
            request = route.request
            path = request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if request.method == "POST" and path == f"/tasks/{TASK_ID}/variant-choice":
                body = request.post_data_json
                picks = task.setdefault("variant_choices", {}).setdefault(body["round_id"], {"total": 5, "picks": {}})["picks"]
                if body["choice"]:
                    picks[str(body["variant_index"])] = body["choice"]
                else:
                    picks.pop(str(body["variant_index"]), None)
                choices.append(body)
                route.fulfill(json={"task": task})
            elif request.method != "GET":
                posts.append(request.url)
                route.fulfill(status=400, json={"error": "Unexpected fixture mutation"})
            elif path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [task]})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": task})
            else:
                route.fulfill(status=404, json={"error": "Unexpected mocked AI endpoint"})

        page.route("**/api/ai/**", ai_api)
        if scripts is not None:
            page.route("**/api/creation?*", lambda route: route.fulfill(json={"revision": "fixture", "scripts": scripts}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{TASK_ID}")
        page.locator(".ai-round-output").wait_for()
        page.evaluate("""() => {
          window.productionEntryCalls = [];
          const record = (kind, task, round) => {
            window.productionEntryCalls.push(JSON.parse(JSON.stringify({kind, task, round})));
          };
          window.CaseProduction.fromCandidate = (task, round, pick) => {
            record('video', task, round);
            window.productionEntryCalls.at(-1).pick = pick;
          };
          window.CaseScripts.adoptCandidate = (task, round, writeBack, pick) => {
            record('adopt', task, round);
            Object.assign(window.productionEntryCalls.at(-1), {writeBack, pick});
          };
        }""")
        return page, task, round_record, posts

    def test_order_made_from_a_draft_can_be_deleted_from_the_desk(self):
        job = "PRD-0123456789ab"
        scripts = [{"id": "SCR-0123456789ab", "script_id": "SCR-0123456789ab", "family_id": "SCR-0123456789ab", "version": 1, "product": "202H",
                    "title": "变体 #2", "status": "adopted", "archived": False, "source": {"task_id": TASK_ID, "round_id": ROUND_ID, "variant_index": 1},
                    "production_usage": [{"id": job, "kind": "video", "status": "voiced", "archived": False, "publication": None}]}]
        page, *_ = self.open_candidate(TWO_VARIANTS_FOR_ORDERS, scripts=scripts)
        calls = []

        def delete(route):
            body = route.request.post_data_json
            calls.append(body)
            if body.get("preview"):
                route.fulfill(json={"title": "临时制作单", "takes": 1, "edits": 0, "files": 2, "bytes": 2097152, "timelines": 0,
                                    "draft_returned": True, "draft_kept": ""})
            else:
                scripts[0].update(archived=True, production_usage=[])
                route.fulfill(json={"deleted_job": job, "warnings": [], "draft_returned": True})

        page.route(f"**/api/production/jobs/{job}/candidate-delete", delete)
        card = page.locator('.ai-variant-card[data-variant-card="2"]')
        page.locator('[data-variant-pick="2"]').click()
        order = card.locator(".ai-variant-order")
        order.wait_for()
        self.assertIn("已配音", order.inner_text())
        self.assertEqual(page.locator('.ai-variant-card[data-variant-card="1"] .ai-variant-order').count(), 0)
        self.assertFalse(card.locator('[data-variant-choice="skip"]').is_visible())
        order.locator("[data-delete-order]").click()
        dialog = page.locator("dialog[open], .modal:visible").last
        dialog.get_by_text("转入归档").wait_for()
        dialog.locator('input[name="sure"]').check()
        dialog.get_by_role("button", name="永久删除").click()
        page.wait_for_function("() => !document.querySelector('.ai-variant-card[data-variant-card=\"2\"] .ai-variant-order')")
        self.assertTrue(card.locator('[data-variant-choice="skip"]').is_visible())
        self.assertEqual(page.url.split("#")[1], f"/202H/ai/{TASK_ID}")
        self.assertEqual([bool(call.get("preview")) for call in calls], [True, False])
        self.assertTrue(calls[1]["request_id"])

    def visible_cards(self, page):
        return [card.get_attribute("data-variant-card") for card in page.locator(".ai-variant-card").all() if card.is_visible()]

    def tabs(self, card):
        return card.locator("[data-variant-tab]").all_inner_texts()

    def test_variants_are_picked_from_the_rail_and_act_on_the_one_shown(self):
        structures = ["回应质疑", "其他：原理讲解", "送礼", "身份认同", "结果倒叙"]
        output = "本轮说明在最前。\n\n" + "".join(titled_variant(index + 1, name, f"First line {index + 1}.")
                                             for index, name in enumerate(structures))
        page, task, round_record, posts = self.open_candidate(output)
        rail = page.locator(".ai-desk-rail")
        rail.wait_for()
        rows = rail.locator("[data-variant-pick]")
        self.assertEqual(rows.count(), 5)
        self.assertEqual(rows.nth(1).locator(".ai-desk-variant-name").inner_text(), "#2 原理讲解")
        self.assertEqual(rows.nth(1).locator("[data-variant-status]").inner_text(), "待定")
        self.assertEqual(page.locator('.ai-variant-card[data-variant-card="2"] .ai-variant-screen').text_content(), "开头屏显Screen 2")
        # Notes wait behind their own rail entry and the rail shows there is something in them.
        self.assertTrue(page.locator(".ai-desk-notes").is_hidden())
        rail.locator('[data-desk-section="notes"]').click()
        self.assertIn("本轮说明在最前", page.locator(".ai-desk-notes").inner_text())
        self.assertEqual(self.visible_cards(page), [])
        rows.nth(0).click()
        self.assertEqual(self.visible_cards(page), ["1"])
        rows.nth(2).click()
        self.assertEqual(self.visible_cards(page), ["3"])
        visible = page.locator('.ai-variant-card[data-variant-card="3"]')
        visible.locator("[data-desk-produce]").click()
        visible.locator("[data-desk-adopt]").click()
        visible.locator("[data-desk-write-case]").click()
        calls = page.evaluate("window.productionEntryCalls")
        self.assertEqual([(call["kind"], call["pick"]["variantIndex"], call["pick"]["variantCount"], call.get("writeBack")) for call in calls],
                         [("video", 2, 5, None), ("adopt", 2, 5, False), ("adopt", 2, 5, True)])
        self.assertEqual(rows.nth(2).get_attribute("aria-selected"), "true")
        rows.nth(2).press("ArrowUp")
        page.keyboard.press("Enter")
        self.assertEqual(self.visible_cards(page), ["2"])
        # Marking a draft shows on its rail row; pressing the same mark again clears it.
        card_two = page.locator('.ai-variant-card[data-variant-card="2"]')
        card_two.locator('[data-variant-choice="skip"]').click()
        page.wait_for_function("() => document.querySelector('.ai-desk-variant[data-variant-pick=\"2\"] .is-skip')")
        self.assertEqual(rows.nth(1).locator("[data-variant-status]").inner_text(), "不做")
        self.assertEqual(card_two.locator('[data-variant-choice="skip"]').get_attribute("aria-pressed"), "true")
        card_two.locator('[data-variant-choice="skip"]').click()
        page.wait_for_function("() => !document.querySelector('.ai-desk-variant[data-variant-pick=\"2\"] .is-skip')")
        self.assertEqual([item["choice"] for item in self.choices], ["skip", ""])
        self.assertEqual(self.choices[0]["variant_index"], 1)
        # A tab stays chosen from one variant to the next.
        card = page.locator('.ai-variant-card[data-variant-card="2"]')
        card.locator('[data-variant-tab="意群表"]').click()
        rows.nth(0).click()
        self.assertTrue(page.locator('.ai-variant-card[data-variant-card="1"] [data-variant-panel="意群表"]').is_visible())
        rows.nth(1).click()
        # The follow-up box says which variant it will change, and can be widened to all of them.
        target = page.locator("#ai-continue-target")
        self.assertEqual(target.inner_text(), "只改 #2")
        target.click()
        self.assertEqual(target.inner_text(), "改全部变体")
        target.click()
        # The rail replaces the right-edge outline, and the raw answer stays complete.
        self.assertTrue(page.locator("#ai-variant-nav").is_hidden())
        self.assertEqual(page.locator(".ai-raw-output pre").text_content(), output)
        # Saved scripts mark their variant by index, or by "变体 #N" in older titles; other rounds never count.
        page.evaluate("""([task, round]) => window.CaseAI.taskScriptsLoaded(task, [
          {status: 'published', title: '任意标题', source: {task_id: task, round_id: round, variant_index: 1}},
          {status: 'adopted', title: '202H-S2 变体 #3 · 送礼', source: {task_id: task, round_id: round}},
          {status: 'published', title: '202H-S2 变体 #1', source: {task_id: task, round_id: 'AIR-another-round'}}])""",
                      [TASK_ID, ROUND_ID])
        self.assertEqual(rows.locator("[data-variant-status]").all_inner_texts(), ["待定", "已发布", "已采纳", "待定", "待定"])
        self.assertEqual(page.locator('.ai-variant-card[data-variant-card="2"] [data-variant-status]').inner_text(), "已发布")
        page.set_viewport_size({"width": 390, "height": 850})
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        self.assertEqual(posts, [])

    def test_a_single_variant_is_one_card_without_a_picker(self):
        page, task, round_record, posts = self.open_candidate(VOICE_OUTPUT)
        card = page.locator(".ai-variant-card")
        card.wait_for()
        self.assertEqual(page.locator(".ai-round.is-single-draft").count(), 1)
        self.assertTrue(page.locator("#ai-continue-target").is_hidden())
        self.assertEqual(card.locator(".ai-variant-card-head h4").inner_text(), "#1 固定角度")
        self.assertEqual(card.locator(".ai-variant-pairs p").all_inner_texts(),
                         ["Set the angle once. Keep it steady through every pass.", "先固定角度，每一遍都保持稳定。"])
        self.assertEqual(self.tabs(card), ["口播", "意群表"])
        self.assertEqual(page.locator(".ai-raw-output pre").text_content(), VOICE_OUTPUT)

    def test_suggested_variants_named_with_v_each_get_their_own_row(self):
        output = "".join(titled_variant(index + 1, "x", f"Line {index + 1}.") for index in range(1))
        output = output.replace("**202H-S2 · 变体 #1 · x**", "### 变体 #1（已登记：价位对比）")
        for number, name in ((2, "先问价再看值不值"), (3, "换新刀 vs 修旧刀")):
            output += titled_variant(number, "x", f"Line {number}.").replace(f"**202H-S2 · 变体 #{number} · x**", f"### 建议新增变体 V{number}：{name}")
        page, task, round_record, posts = self.open_candidate(output)
        rows = page.locator(".ai-desk-rail [data-variant-pick]")
        rows.first.wait_for()
        self.assertEqual(rows.locator(".ai-desk-variant-name").all_inner_texts(), ["#1 价位对比", "#2 先问价再看值不值", "#3 换新刀 vs 修旧刀"])
        self.assertEqual(page.locator(".ai-variant-card").count(), 3)
        # The second card must not swallow the third.
        self.assertNotIn("Line 3.", page.locator('.ai-variant-card[data-variant-card="2"]').text_content())
        self.assertEqual(posts, [])

    def test_untitled_variants_split_at_rules_and_share_the_closing_check(self):
        def untitled(number):
            return f"""开头逻辑：处境开场 {number}。

开头画面（待核，新拍）：手在磨石上 {number}。

**开头屏显**：Guided {number}

**口播（配音用）**

```
Spoken {number}.
```

**对应中文**

```
口播 {number}。
```

补拍清单：以下镜头

- 镜头 {number}

**发布文案**

测试方向：202H-S1 · {number}
"""
        output = f"按美区英语口播写，三版共用的说明。\n\n{untitled(1)}\n---\n\n{untitled(2)}\n**发布前核对**：确认读法与购买动机。\n\n经验范围：弱参考。\n"
        page, task, round_record, posts = self.open_candidate(output)
        chips = page.locator(".ai-desk-rail [data-variant-pick]")
        chips.first.wait_for()
        self.assertEqual(chips.count(), 2)
        self.assertEqual(page.locator(".ai-variant-card-head h4").all_inner_texts(), ["第 1 条", "第 2 条"])
        output_text = page.locator(".ai-round-output")
        self.assertEqual(output_text.locator(".ai-desk-notes > p").first.text_content(), "按美区英语口播写，三版共用的说明。")
        card = page.locator('.ai-variant-card[data-variant-card="1"]')
        self.assertEqual([line.split("：")[0] for line in card.locator(".ai-variant-opening p").all_inner_texts()],
                         ["开头逻辑", "开头画面（待核，新拍）", "开头屏显"])
        self.assertEqual(self.tabs(card), ["口播", "开头", "补拍清单", "发布文案"])
        card.locator('[data-variant-tab="补拍清单"]').click()
        self.assertIn("镜头 1", card.locator('[data-variant-panel="补拍清单"]').inner_text())
        shared = page.locator(".ai-variant-shared")
        self.assertTrue(shared.is_hidden())
        page.locator('.ai-desk-rail [data-desk-section="check"]').click()
        self.assertTrue(shared.is_visible())
        self.assertEqual(shared.locator("h4").inner_text(), "发布前核对")
        self.assertIn("确认读法与购买动机", shared.inner_text())
        self.assertIn("经验范围", shared.inner_text())
        self.assertNotIn("确认读法", page.locator('.ai-variant-card[data-variant-card="2"]').text_content())
        self.assertEqual(posts, [])

    def test_a_repeated_title_line_stays_with_its_variant(self):
        output = ("**变体 #7 · 逐件对照**\n\n**202H-S2 · 变体 #7 逐件对照 ｜ 估读未计数**\n\n" + titled_variant(7, "", "One kit.").split("\n", 4)[4]
                  + titled_variant(9, "其他:送礼", "For the knife person."))
        page, task, round_record, posts = self.open_candidate(output)
        chips = page.locator(".ai-desk-rail [data-variant-pick]")
        chips.first.wait_for()
        self.assertEqual(chips.count(), 2)
        self.assertEqual(page.locator(".ai-variant-card-head h4").first.inner_text(), "#7 逐件对照")
        self.assertIn("估读未计数", page.locator(".ai-variant-subtitle").inner_text())
        self.assertEqual(chips.nth(1).locator(".ai-desk-variant-name").inner_text(), "#9 送礼")

    def test_other_completed_task_types_offer_results_without_writing_production(self):
        for action in ("review", "retrospective"):
            with self.subTest(action=action):
                page, task, round_record, posts = self.open_candidate(VOICE_OUTPUT, action=action)
                self.assertTrue(page.locator(".ai-candidate-actions").is_hidden())
                self.assertEqual(page.locator(".ai-output-title").inner_text(), "审稿结论" if action == "review" else "复盘结论")
                self.assertNotIn("待选稿", page.locator("#ai-management-state").inner_text())
                self.assertEqual(page.evaluate("window.productionEntryCalls"), [])
                self.assertEqual(posts, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
