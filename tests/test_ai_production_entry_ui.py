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


class AIProductionEntryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_candidate(self, output, *, action="variants"):
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
        posts = []

        def ai_api(route):
            request = route.request
            path = request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if request.method != "GET":
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
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{TASK_ID}")
        page.locator(".ai-round-output").wait_for()
        page.evaluate("""() => {
          window.productionEntryCalls = [];
          const record = (kind, task, round) => {
            window.productionEntryCalls.push(JSON.parse(JSON.stringify({kind, task, round})));
          };
          window.CaseProduction.fromCandidate = (task, round) => record('video', task, round);
          window.CaseScripts.adoptCandidate = (task, round) => record('adopt', task, round);
        }""")
        return page, task, round_record, posts

    def visible_cards(self, page):
        return [card.get_attribute("data-variant-card") for card in page.locator(".ai-variant-card").all() if card.is_visible()]

    def test_five_or_more_variants_use_an_overview_table_and_one_card_at_a_time(self):
        structures = ["回应质疑", "其他：原理讲解", "送礼", "身份认同", "结果倒叙"]
        output = "本轮说明在最前。\n\n" + "".join(titled_variant(index + 1, name, f"First line {index + 1}.")
                                             for index, name in enumerate(structures))
        page, task, round_record, posts = self.open_candidate(output)
        board = page.locator(".ai-variant-board")
        board.wait_for()
        self.assertIn("本轮说明在最前", page.locator(".ai-round-output").inner_text())
        rows = board.locator(".ai-variant-overview [data-variant-pick]")
        self.assertEqual(rows.count(), 5)
        self.assertEqual(rows.nth(1).locator("span").nth(0).inner_text(), "原理讲解")
        self.assertEqual(rows.nth(1).locator("span").nth(1).inner_text(), "Screen 2")
        self.assertEqual(rows.nth(1).locator(".ai-variant-first").inner_text(), "First line 2.")
        self.assertEqual(rows.nth(1).locator("[data-variant-status]").inner_text(), "待定")
        self.assertEqual(self.visible_cards(page), ["1"])
        rows.nth(2).click()
        self.assertEqual(self.visible_cards(page), ["3"])
        self.assertEqual(rows.nth(2).get_attribute("aria-selected"), "true")
        rows.nth(2).press("ArrowUp")
        page.keyboard.press("Enter")
        self.assertEqual(self.visible_cards(page), ["2"])
        board.locator("[data-variant-show-all]").click()
        self.assertEqual(self.visible_cards(page), ["1", "2", "3", "4", "5"])
        board.locator("[data-variant-show-all]").click()
        self.assertEqual(self.visible_cards(page), ["2"])
        # The overview replaces the right-edge outline, and the raw answer stays complete.
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
        self.assertEqual(page.locator(".ai-variant-board").count(), 0)
        self.assertEqual(card.locator(".ai-variant-card-head h4").inner_text(), "变体 1：固定角度")
        self.assertEqual(card.locator(".ai-variant-pairs p").all_inner_texts(),
                         ["Set the angle once. Keep it steady through every pass.", "先固定角度，每一遍都保持稳定。"])
        self.assertEqual(card.locator(".ai-variant-fold > summary").all_inner_texts(), ["意群表"])
        self.assertEqual(page.locator(".ai-raw-output pre").text_content(), VOICE_OUTPUT)

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
        chips = page.locator(".ai-variant-chips [data-variant-pick]")
        chips.first.wait_for()
        self.assertEqual(chips.count(), 2)
        self.assertEqual(page.locator(".ai-variant-card-head h4").all_inner_texts(), ["第 1 条", "第 2 条"])
        output_text = page.locator(".ai-round-output")
        self.assertEqual(output_text.locator("> p").first.inner_text(), "按美区英语口播写，三版共用的说明。")
        card = page.locator('.ai-variant-card[data-variant-card="1"]')
        self.assertEqual([line.split("：")[0] for line in card.locator(".ai-variant-opening p").all_inner_texts()],
                         ["开头逻辑", "开头画面（待核，新拍）", "开头屏显"])
        self.assertEqual(card.locator(".ai-variant-fold > summary").all_inner_texts(), ["补拍清单", "发布文案"])
        card.locator(".ai-variant-fold > summary").first.click()
        self.assertIn("镜头 1", card.locator(".ai-variant-fold").first.inner_text())
        shared = page.locator(".ai-variant-shared")
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
        chips = page.locator(".ai-variant-chips [data-variant-pick]")
        chips.first.wait_for()
        self.assertEqual(chips.count(), 2)
        self.assertEqual(page.locator(".ai-variant-card-head h4").first.inner_text(), "变体 #7 · 逐件对照")
        self.assertIn("估读未计数", page.locator(".ai-variant-subtitle").inner_text())
        self.assertEqual(chips.nth(1).locator("span").first.inner_text(), "送礼")

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
