"""Reading saved writing records must not wait for external client discovery."""
from __future__ import annotations

import json
import unittest

import test_ui_regressions as browser
import test_server as fixture


TASK = "AIT-0123456789ab"


class AILoadingBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def open_writing(self, tail="", *, task=None, counts_route=None, nav_reads=None):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(5000)
        pending, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        if nav_reads is not None:
            page.on("request", lambda request: nav_reads.append(request.url.split("/api/ai/tasks", 1)[1])
                    if request.url.split("?")[0].endswith("/api/ai/tasks") else None)
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.route("**/api/ai/status", lambda route: pending.append(route))
        page.route("**/api/ai/options*", lambda route: pending.append(route))
        task = task or {"task_id": TASK, "status": "completed", "product": "202H",
                "action": "variants", "context": {"title": "可立即阅读的写稿记录"},
                "rounds": [{"round_id": "round-one", "status": "completed",
                            "instruction": "测试要求", "output": "保存的完整脚本", "messages": []}]}
        page.route("**/api/ai/tasks", lambda route: route.fulfill(json={"tasks": [task]}))
        if counts_route:
            page.route("**/api/ai/tasks?projection=counts", counts_route)
        page.route(f"**/api/ai/tasks/{TASK}", lambda route: route.fulfill(json={"task": task}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#ai{tail}", wait_until="domcontentloaded")
        return page, pending

    def history(self, *, action="variants", count=20):
        return {"task_id": TASK, "status": "completed", "product": "202H", "action": action,
                "context": {"title": "多轮记录"},
                "rounds": [{"round_id": f"round-{index}", "status": "completed", "instruction": "测试要求",
                            "output": f"第 {index + 1} 轮的完整脚本",
                            "messages": [{"item_id": "answer", "phase": "final_answer", "complete": True,
                                          "text": "历史流式回复不应再重复渲染"}]}
                           for index in range(count)]}

    def finish_discovery(self, pending):
        for route in pending:
            body = ({"available": True, "label": "测试服务"} if route.request.url.endswith("/status") else
                    {"models": [], "skills": [], "default_model": None})
            route.fulfill(body=json.dumps(body), content_type="application/json")

    def test_list_is_readable_while_client_status_is_pending(self):
        page, pending = self.open_writing()
        page.get_by_text("可立即阅读的写稿记录", exact=False).first.wait_for()
        self.assertTrue(any(route.request.url.endswith("/status") for route in pending))
        self.finish_discovery(pending)
        page.locator("#ai-service-label").get_by_text("可以开始协作", exact=False).wait_for()

    def test_saved_reply_is_readable_before_models_and_status_arrive(self):
        page, pending = self.open_writing("/" + TASK)
        page.locator("#ai-rounds p").get_by_text("保存的完整脚本", exact=False).wait_for()
        self.assertTrue(any("/api/ai/options" in route.request.url for route in pending))
        self.assertEqual(page.locator("#ai-continue-model,#ai-continue-effort").count(), 0)
        self.assertIn("正在读取全局默认配置", page.locator("#ai-continue-defaults-summary").inner_text())
        self.finish_discovery(pending)
        page.locator("#ai-continue-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
        self.assertEqual(page.locator("#ai-rounds p").get_by_text("保存的完整脚本", exact=False).count(), 1)

    def test_detail_navigation_reads_only_counts_and_product_switch_reuses_them(self):
        reads = []
        page, pending = self.open_writing("/" + TASK, nav_reads=reads, counts_route=lambda route:
                                         route.fulfill(json={"counts": {"active": 3, "by_product": {"202H": 1, "129N": 2}}}))
        page.wait_for_function("document.querySelector('#ai-active-count').textContent === '1'")
        self.assertEqual(reads, ["?projection=counts"])
        self.assertEqual(self.request("POST", "/api/products/catalog", {"action": "add", "product": "129N"})[0], 200)
        page.evaluate("() => WorkbenchShell.refreshProducts()")
        page.evaluate("location.hash = '#/129N/library'")
        page.wait_for_function("document.querySelector('#ai-active-count').textContent === '2'")
        self.assertEqual(reads, ["?projection=counts"])
        self.finish_discovery(pending)

    def test_late_counts_cannot_replace_a_newer_list_count(self):
        counts, reads = [], []
        page, pending = self.open_writing("/" + TASK, counts_route=lambda route: counts.append(route), nav_reads=reads)
        page.locator("#ai-rounds p").first.wait_for()
        page.evaluate("location.hash = '#/202H/ai'")
        page.locator("#ai-task-list").wait_for()
        page.wait_for_function("() => document.querySelector('#ai-active-count').textContent === '0'")
        self.assertTrue(counts)
        counts[0].fulfill(json={"counts": {"active": 9, "by_product": {"202H": 9}}})
        page.evaluate("() => new Promise(resolve => setTimeout(resolve,50))")
        self.assertEqual(page.locator("#ai-active-count").inner_text(), "0")
        self.assertEqual(reads, ["?projection=counts", ""])
        self.finish_discovery(pending)

    def test_navigation_accepts_an_old_engine_that_ignores_projection(self):
        reads = []
        older = {"task_id": "AIT-111111111111", "status": "running", "product": "202H"}
        page, pending = self.open_writing("/" + TASK, nav_reads=reads,
                                         counts_route=lambda route: route.fulfill(json={"tasks": [older]}))
        page.wait_for_function("document.querySelector('#ai-active-count').textContent === '1'")
        self.assertEqual(reads, ["?projection=counts"])
        self.finish_discovery(pending)

    def test_navigation_falls_back_if_an_old_engine_rejects_projection(self):
        reads = []
        page, pending = self.open_writing("/" + TASK, nav_reads=reads,
                                         counts_route=lambda route: route.fulfill(status=400, json={"error": "旧服务"}))
        page.wait_for_function("document.querySelector('#ai-active-count').textContent === '0'")
        self.assertEqual(reads, ["?projection=counts", ""])
        self.finish_discovery(pending)

    def test_long_writing_history_builds_only_the_selected_round(self):
        task = self.history()
        page, pending = self.open_writing("/" + TASK, task=task)
        latest = page.locator('[data-round-id="round-19"]')
        latest.locator(".ai-round-output p").wait_for()
        self.assertEqual(page.locator("#ai-rounds .ai-round").count(), 20)
        self.assertEqual(page.locator("#ai-round-picker").count(), 0)
        # The left rail lists every round, newest first; only the chosen one is on screen.
        self.assertEqual(page.locator(".ai-round-row").count(), 20)
        self.assertEqual(page.locator(".ai-round-row").first.get_attribute("data-round-pick"), "round-19")
        self.assertEqual(page.locator('.ai-round-row[aria-current="true"]').get_attribute("data-round-pick"), "round-19")
        self.assertEqual(page.locator("#ai-rounds .ai-round:visible").count(), 1)
        self.assertEqual(page.locator(".ai-round-output p").count(), 1)
        self.assertEqual(page.locator(".ai-stream-message").count(), 0)
        first = page.locator('[data-round-id="round-0"]')
        page.locator('[data-round-pick="round-0"]').click()
        self.assertEqual(first.locator(".ai-round-output").inner_text(), "第 1 轮的完整脚本")
        self.assertTrue(first.locator(".ai-round-output").is_visible())
        self.assertFalse(latest.locator(".ai-round-body").is_visible())
        self.assertEqual(page.locator(".ai-round-output p").count(), 2)
        # A poll may update a hidden round; opening it uses that latest snapshot.
        page.locator('[data-round-pick="round-19"]').click()
        task["rounds"][0]["output"] = "历史轮次更新后的正文"
        page.evaluate("() => window.CaseAI.route(true)")
        page.locator('[data-round-pick="round-0"]').click()
        self.assertEqual(first.locator(".ai-round-output").inner_text(), "历史轮次更新后的正文")
        self.finish_discovery(pending)

    def test_writing_round_choice_survives_polls_and_works_by_keyboard(self):
        page, pending = self.open_writing("/" + TASK, task=self.history(count=3))
        latest = page.locator('[data-round-id="round-2"]')
        latest.locator(".ai-round-output p").wait_for()
        older = page.locator('[data-round-pick="round-0"]')
        older.focus()
        older.press("Enter")
        self.assertEqual(older.get_attribute("aria-current"), "true")
        self.assertFalse(latest.locator(".ai-round-body").is_visible())
        page.evaluate("() => window.CaseAI.route(true)")
        self.assertEqual(older.get_attribute("aria-current"), "true")
        self.assertTrue(page.locator('[data-round-id="round-0"] .ai-round-output').is_visible())
        self.assertEqual(page.locator('.ai-round-toggle[aria-expanded="true"]').count(), 1)
        self.finish_discovery(pending)

    def test_hand_writing_form_saves_a_new_round_through_continue(self):
        task = self.history(count=2)
        page, pending = self.open_writing("/" + TASK, task=task)
        page.locator('[data-round-id="round-1"] .ai-round-output p').wait_for()
        posts = []

        def save(route):
            body = json.loads(route.request.post_data)
            posts.append(body)
            task["rounds"].append({"round_id": "round-hand", "status": "completed", "kind": "manual_write",
                                   "instruction": "手写新稿 · 我的稿（1 句）", "output": "手写完整脚本", "messages": []})
            task["revision"] = 2
            route.fulfill(json={"task": task})

        page.route(f"**/api/ai/tasks/{TASK}/continue", save)
        page.locator("#ai-round-write").click()
        page.locator('[data-mw-mode="type"]').click()
        self.assertTrue(page.locator("#ai-manual-write").is_visible())
        self.assertEqual(page.locator("#ai-rounds .ai-round:visible").count(), 0)
        page.locator("[data-mw-save]").click()
        self.assertIn("至少写一句", page.locator("[data-mw-error]").inner_text())
        page.locator("[data-mw-title]").fill("我的稿")
        page.locator('[data-mw-field="spoken"]').first.fill("My knives were dull.")
        page.locator("[data-mw-save]").click()
        self.assertIn("还没有中文", page.locator("[data-mw-error]").inner_text())
        page.locator('[data-mw-field="zh"]').first.fill("我的刀很钝。")
        page.locator("[data-mw-save]").click()
        page.locator('[data-round-id="round-hand"] .ai-round-output p').wait_for()
        self.assertEqual(posts[0]["manual_write"], {"title": "我的稿", "hook": "", "lines": [{"spoken": "My knives were dull.", "zh": "我的刀很钝。"}]})
        self.assertFalse(page.locator("#ai-manual-write").is_visible())
        self.assertEqual(page.locator('.ai-round-row[aria-current="true"]').get_attribute("data-round-pick"), "round-hand")
        self.finish_discovery(pending)

    def test_pasting_a_whole_draft_previews_then_saves_through_continue(self):
        task = self.history(count=2)
        page, pending = self.open_writing("/" + TASK, task=task)
        page.locator('[data-round-id="round-1"] .ai-round-output p').wait_for()
        posts, previews = [], []

        def preview(route):
            body = json.loads(route.request.post_data)
            previews.append(body)
            if "口播" not in body["text"]:
                return route.fulfill(json={"recognized": False, "hook": "", "rows": [{"spoken": "My knives were dull.", "zh": "我的刀很钝。"}]})
            route.fulfill(json={"recognized": True, "variants": [{"label": "变体 #1 · 导入稿", "lines": 3, "chinese": 3, "table": 3, "first": "I get it.",
                                                                  "screens": 3, "pickups": 0, "swap": False, "titles": 3, "problems": []}]})

        def save(route):
            posts.append(json.loads(route.request.post_data))
            task["rounds"].append({"round_id": "round-paste", "status": "completed", "kind": "manual_write",
                                   "instruction": "导入整稿 · 变体 #1 · 导入稿（3 句）", "output": "导入的完整脚本", "messages": []})
            task["revision"] = 2
            route.fulfill(json={"task": task})

        page.route("**/api/ai/manual-import/preview", preview)
        page.route(f"**/api/ai/tasks/{TASK}/continue", save)
        paste = "(text) => {const data = new DataTransfer();data.setData('text/plain',text);document.querySelector('[data-mi-zone]').dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));}"
        page.locator("#ai-round-write").click()
        self.assertEqual(page.locator('[data-mw-mode="paste"]').get_attribute("aria-pressed"), "true")
        self.assertTrue(page.locator("[data-mw-save]").is_disabled())
        page.evaluate(paste, "My knives were dull.\n我的刀很钝。")
        self.assertIn("已贴入", page.locator(".ai-mi-summary").inner_text())
        page.locator("[data-mi-fill]").click()
        self.assertEqual(page.locator('[data-mw-field="zh"]').first.input_value(), "我的刀很钝。")
        page.locator('[data-mw-mode="paste"]').click()
        page.locator("[data-mi-clear]").click()
        self.assertFalse(page.locator("[data-mi-text]").is_visible())
        page.evaluate(paste, "口播（配音用）\nI get it.")
        page.locator(".ai-mi-variant").wait_for()
        self.assertEqual(page.locator(".ai-mi-chip.is-ok").all_inner_texts(), ["口播 3 句", "中文 3 句", "意群表 3 行", "屏显 3 条", "发布文案 3 行"])
        self.assertEqual(page.locator(".ai-mi-chip.is-none").all_inner_texts(), ["补拍清单 无", "可换句 无"])
        page.locator("[data-mi-raw-toggle]").click()
        self.assertEqual(page.locator("[data-mi-text]").input_value(), "口播（配音用）\nI get it.")
        page.locator("[data-mw-save]").click()
        page.locator('[data-round-id="round-paste"] .ai-round-output p').wait_for()
        self.assertEqual(posts[0]["manual_import"], {"text": "口播（配音用）\nI get it.", "title": ""})
        self.assertNotIn("manual_write", posts[0])
        self.assertFalse(page.locator("#ai-manual-write").is_visible())
        self.finish_discovery(pending)

    def test_new_writing_round_opens_and_collapses_the_previous_round(self):
        task = self.history(count=2)
        page, pending = self.open_writing("/" + TASK, task=task)
        previous = page.locator('[data-round-id="round-1"]')
        previous.locator(".ai-round-output p").wait_for()
        task["rounds"].append({"round_id": "round-new", "status": "completed", "instruction": "新一轮要求",
                               "output": "新一轮的完整脚本", "messages": []})
        page.evaluate("() => window.CaseAI.route(true)")
        latest = page.locator('[data-round-id="round-new"]')
        latest.locator(".ai-round-output p").wait_for()
        self.assertEqual(latest.locator(".ai-round-toggle").get_attribute("aria-expanded"), "true")
        self.assertTrue(latest.locator(".ai-round-output").is_visible())
        self.assertEqual(previous.locator(".ai-round-toggle").get_attribute("aria-expanded"), "false")
        self.assertFalse(previous.locator(".ai-round-body").is_visible())
        self.assertEqual(page.locator('.ai-round-toggle[aria-expanded="true"]').count(), 1)
        self.finish_discovery(pending)

    def test_round_deep_link_builds_the_requested_history_first(self):
        page, pending = self.open_writing("/" + TASK + "?round=round-2", task=self.history())
        target = page.locator('[data-round-id="round-2"] .ai-round-output p')
        target.wait_for()
        self.assertTrue(target.is_visible())
        self.assertEqual(page.locator('[data-round-id="round-2"] .ai-round-toggle').get_attribute("aria-expanded"), "true")
        self.assertEqual(page.locator('[data-round-id="round-19"] .ai-round-toggle').get_attribute("aria-expanded"), "false")
        self.assertEqual(page.locator(".ai-round-output p").count(), 1)
        self.finish_discovery(pending)

    def test_nonwriting_history_renders_a_collapsed_reply_when_opened(self):
        page, pending = self.open_writing("/" + TASK, task=self.history(action="review", count=3))
        page.locator('[data-round-id="round-2"] .ai-round-output p').wait_for()
        self.assertEqual(page.locator(".ai-round-output p").count(), 1)
        first = page.locator('[data-round-id="round-0"]')
        first.locator(".ai-round-toggle").click()
        self.assertTrue(first.locator(".ai-round-output p").is_visible())
        self.assertEqual(first.locator(".ai-round-output").inner_text(), "第 1 轮的完整脚本")
        self.finish_discovery(pending)

    def test_completed_reply_removes_previously_rendered_stream_messages(self):
        task = self.history(count=1)
        task["status"] = task["rounds"][0]["status"] = "interrupted"
        task["rounds"][0]["output"] = ""
        page, pending = self.open_writing("/" + TASK, task=task)
        page.locator(".ai-stream-message .ai-stream-body p").wait_for()
        task["status"] = task["rounds"][0]["status"] = "completed"
        task["rounds"][0]["output"] = "保存后的完整回答"
        page.evaluate("() => window.CaseAI.route(true)")
        self.assertEqual(page.locator(".ai-round-output").inner_text(), "保存后的完整回答")
        self.assertEqual(page.locator(".ai-stream-message").count(), 0)
        self.finish_discovery(pending)

    def test_staged_round_keeps_progress_and_settings_in_a_side_column(self):
        task = {"task_id": TASK, "status": "interrupted", "product": "202H", "action": "variants",
                "context": {"title": "复核被打断的写稿记录"},
                "rounds": [{"round_id": "round-run", "status": "interrupted", "instruction": "只写两条测试稿", "output": "", "messages": [],
                            "requested_model": "claude-sonnet", "skills": [{"name": "writing", "label": "带货脚本创作"}],
                            "skills_status": "submitted", "review_context": {"status": "attached", "reviews": 802},
                            "stages": {"draft": {"status": "completed", "model": "claude-sonnet", "started_at": "2026-11-15T03:40:00+00:00",
                                                 "completed_at": "2026-11-15T03:41:05+00:00",
                                                 "usage": {"input_tokens": 4, "output_tokens": 900, "cache_read_input_tokens": 85000,
                                                           "cache_creation_input_tokens": 29000}},
                                       "review": {"status": "interrupted"}}}]}
        page, pending = self.open_writing("/" + TASK, task=task)
        node = page.locator('[data-round-id="round-run"]')
        node.locator(".ai-run-step").first.wait_for()
        self.assertEqual([step.get_attribute("class").split()[-1] for step in node.locator(".ai-run-step").all()],
                         ["is-done", "is-problem", "is-todo"])
        self.assertEqual(node.locator(".ai-run-step.is-done small").inner_text(), "1 分 5 秒")
        self.assertIn("读缓存 85,000 / 写缓存 29,000", node.locator(".ai-run-step.is-done").get_attribute("title"))
        self.assertIn("评论摘要 802 条", node.locator(".ai-run-meta").inner_text())
        self.assertEqual(node.locator(".ai-user-request").inner_text().count("只写两条测试稿"), 1)
        # The per-stage lines stay out of the way until asked for; nothing is dropped.
        self.assertFalse(node.locator(".ai-run-line").is_visible())
        node.locator("[data-run-settings]").click()
        self.assertTrue(node.locator(".ai-run-line").is_visible())
        self.assertIn("独立内容复核", node.locator(".ai-stage-status").inner_text())
        self.assertEqual(node.locator("[data-run-settings]").inner_text(), "收起设置与用量")
        node.locator("[data-run-settings]").click()
        self.assertFalse(node.locator(".ai-run-line").is_visible())
        self.finish_discovery(pending)


if __name__ == "__main__":
    unittest.main()
