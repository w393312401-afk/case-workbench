"""写稿按对标 in the browser: the 开始写稿 dialog's 按对标 block, the hook-bank entry, and the check card on drafts."""
from __future__ import annotations

import json
import os
import unittest

from playwright.sync_api import expect

import test_ai_production_entry_ui as entry
import test_ai_selection_ui as browser
import test_server as fixture
from test_benchmark import CONTRAST, DRAFT

TASK_ID, ROUND_ID = entry.TASK_ID, entry.ROUND_ID
BENCH = {"rules": [{"n": 1, "text": "第一秒放钝刀切番茄的结果", "basis": "出圈 4/4 · 对照 1/7", "dim": "第 1 秒画面"},
                   {"n": 2, "text": "证明出现：第 5 秒", "basis": "出圈 3/4", "dim": "证明出现"}]}
HOOK = {"id": "7550000000000000009", "author": "bladeguy", "title": "t", "stats": {"views": 500000, "followers": 100000},
        "hook": {"type": "质疑回应", "lines": [{"time": "00:00", "en": "Will this ruin my knives?", "zh": "会毁刀吗"}]}}


class BenchmarkBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def page(self, hash_route):
        context = self.browser.new_context(viewport={"width": 1180, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(8000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.route("**/api/ai/status", lambda route: route.fulfill(json={"available": True, "label": "隔离测试助手"}))
        page.route("**/api/ai/options*", lambda route: route.fulfill(json={"models": [], "skills": [], "default_settings": {}}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{hash_route}")
        return page

    def test_dialog_from_hook_bank_sends_rules_and_hook(self):
        self.server.app.hook_bank.save("202H", [HOOK])
        self.server.app.benchmark.save_contrast("202H", CONTRAST, "磨刀器对照")
        page = self.page("#/202H/hooks")
        sent = []

        def capture(route):
            if route.request.method == "POST":
                sent.append(route.request.post_data_json)
                route.fulfill(status=400, json={"error": "测试只看请求"})
            else:
                route.continue_()
        page.route("**/api/ai/tasks", capture)
        page.locator("[data-hook-write='7550000000000000009']").click()
        dialog = page.locator("#ai-create-dialog")
        expect(dialog).to_be_visible()
        bench = dialog.locator(".ai-benchmark")
        expect(bench).to_have_class("ai-benchmark is-on")
        expect(bench.locator(".ai-benchmark-hook")).to_contain_text("Will this ruin my knives?")
        bench.locator("#ai-benchmark-source").select_option(label=bench.locator("#ai-benchmark-source option").nth(1).inner_text())
        expect(bench.locator("[data-benchmark-rule]")).to_have_count(2)
        bench.locator("[data-benchmark-rule='2']").uncheck()
        if os.environ.get("BENCH_PREVIEW_DIALOG"):
            dialog.screenshot(path=os.environ["BENCH_PREVIEW_DIALOG"])
        expect(page.locator("#submit-ai-create")).to_be_enabled()
        page.locator("#submit-ai-create").click()
        expect(dialog.locator("#ai-create-error")).to_contain_text("测试只看请求")
        body = sent[-1]
        contrast_id = self.server.app.benchmark.contrasts("202H")[0]["id"]
        self.assertEqual(body["benchmark"], {"contrast_id": contrast_id, "rules": [1], "hook_id": "7550000000000000009"})
        self.assertEqual(body["instruction"], "按勾选的对标约束写稿。")

    def test_check_card_shows_gaps_and_fills_the_follow_up(self):
        task = {"task_id": TASK_ID, "revision": "r", "status": "completed", "busy": False, "product": "202H", "action": "variants",
                "context": {"title": "按对标写稿", "benchmark": BENCH},
                "rounds": [{"round_id": ROUND_ID, "status": "completed", "instruction": "按对标", "output": DRAFT, "messages": [], "skill_selection": "none"}]}
        check = {"status": "done", "rules": BENCH["rules"], "results": [{"variant": 1, "items": [
            {"rule": 1, "ok": True, "where": "第 1 句（约 0 秒）", "fix": ""},
            {"rule": 2, "ok": False, "where": "第 3 句（约 5 秒）", "fix": "把切纸挪到第 2 句"}]}]}
        context = self.browser.new_context(viewport={"width": 1180, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(8000)

        def ai_api(route):
            path = route.request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [task]})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": task})
            else:
                route.fulfill(status=404, json={"error": "unexpected"})
        page.route("**/api/ai/**", ai_api)
        page.route("**/api/benchmark/check?*", lambda route: route.fulfill(json=check))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{TASK_ID}")
        card = page.locator(".ai-variant-card").first
        expect(card.locator(".ai-bench-badge")).to_have_text("对标 1/2 · 1 个缺口")
        expect(card.locator(".ai-bench-gap")).to_contain_text("缺口：证明出现：第 5 秒")
        expect(card.locator(".ai-bench-done summary")).to_have_text("已做到 1 条")
        if os.environ.get("BENCH_PREVIEW_CHECK"):
            card.screenshot(path=os.environ["BENCH_PREVIEW_CHECK"])
        card.locator("[data-bench-fix]").click()
        value = page.locator("#ai-continue-instruction").input_value()
        self.assertIn("按对标核对改这条稿", value)
        self.assertIn("- 证明出现：第 5 秒：第 3 句（约 5 秒）。把切纸挪到第 2 句", value)


if __name__ == "__main__":
    unittest.main()
