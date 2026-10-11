"""Global defaults in writing forms; isolated library and mocked AI endpoints only."""
from __future__ import annotations

import copy
import unittest

import test_server as fixture
import test_ai_selection_ui as browser


TASK_ID = "AIT-0123456789ab"
MODELS = [
    {"id": "gpt-6.1-sol", "name": "GPT-6.1-Sol", "available": True, "reasoning_efforts": ["high"]},
    {"id": "claude-sonnet", "name": "Claude Sonnet", "provider": "claude", "available": True, "reasoning_efforts": ["medium"]},
]


class AIDefaultsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def open_page(self, *, continuation=False, broken_options=False, corrupt_defaults=False):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        context.add_init_script("localStorage.setItem('case-workbench.ai.last-settings', JSON.stringify({model:'old-local-model',reasoning_effort:'low'}))")
        page = context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        self.posts = []
        self.options = {"models": copy.deepcopy(MODELS), "skills": [],
                        "default_settings": {"model": "gpt-6.1-sol", "reasoning_effort": "high"}}
        if corrupt_defaults:
            self.options.update(default_settings={"model": None, "reasoning_effort": None},
                                defaults_error="全局默认配置已损坏，请在设置中重新保存。", defaults_configured=False)
        self.broken_options = broken_options
        self.task = {"task_id": TASK_ID, "revision": "fixture-revision", "status": "completed", "busy": False,
                     "product": "202H", "action": "variants", "context": {"title": "已保存任务"},
                     "rounds": [{"round_id": "one", "status": "completed", "instruction": "先前要求",
                                 "output": "保存的脚本", "messages": [], "requested_model": "old-history-model",
                                 "requested_reasoning_effort": "low", "model": "actual-history-model", "skill_selection": "none"}]}

        def ai_api(route):
            request = route.request
            path = request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                if self.broken_options:
                    route.fulfill(status=503, json={"error": "fixture: defaults unavailable"})
                else:
                    route.fulfill(json=self.options)
            elif request.method == "POST":
                self.posts.append(request.post_data_json)
                route.fulfill(json={"task": self.task})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [self.task] if continuation else []})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": self.task})
            else:
                route.fulfill(status=404, json={"error": "Unexpected mocked AI endpoint"})

        page.route("**/api/ai/**", ai_api)
        tail = f"/{TASK_ID}" if continuation else ""
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai{tail}")
        page.wait_for_function("() => !!window.CaseAI")
        return page

    def assert_global_request(self, payload):
        self.assertNotIn("model", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertIn("skill_selection", payload)

    def test_new_writing_review_and_retrospective_share_defaults_without_local_overrides(self):
        for action in ("variants", "review", "retrospective"):
            with self.subTest(action=action):
                page = self.open_page()
                if action == "variants":
                    page.evaluate("CaseAI.startBlank('202H')")
                else:
                    page.evaluate("""async action => {
                      document.querySelector('#content').insertAdjacentHTML('beforeend','<div id="detail-ai-panel"></div>');
                      await CaseAI.mountCaseActions({case_id:'fixture-case',product:'202H',revision:'fixture-revision',title:'临时案例'});
                      document.querySelector(`[data-ai-action="${action}"]`).click();
                    }""", action)
                summary = page.locator("#ai-create-defaults-summary")
                summary.get_by_text("使用全局默认 · GPT-6.1-Sol · 思考高", exact=True).wait_for()
                self.assertEqual(page.locator("#ai-create-model,#ai-create-effort").count(), 0)
                self.assertIsNone(page.evaluate("localStorage.getItem('case-workbench.ai.last-settings')"))
                self.assertEqual(page.locator("#ai-create-defaults a").get_attribute("target"), "_blank")
                page.locator("#ai-create-instruction").fill("测试全局默认配置。")
                page.locator("#submit-ai-create").click()
                page.locator("#ai-rounds").wait_for()
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(self.posts[0]["action"], action)
                self.assert_global_request(self.posts[0])
                page.close()

    def test_continuation_uses_changed_defaults_preserves_draft_and_keeps_historical_model(self):
        page = self.open_page(continuation=True)
        summary = page.locator("#ai-continue-defaults-summary")
        summary.get_by_text("使用全局默认 · GPT-6.1-Sol · 思考高", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-continue-model,#ai-continue-effort").count(), 0)
        self.assertIn("actual-history-model", page.locator("#ai-rounds").inner_text())
        page.locator("#ai-continue-instruction").fill("保留这份续写要求。")
        page.evaluate("CaseAI.refreshDefaults({model:'claude-sonnet',reasoning_effort:'medium'})")
        summary.get_by_text("使用全局默认 · Claude Sonnet · 思考中", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-continue-instruction").input_value(), "保留这份续写要求。")
        page.locator("#ai-continue-submit").click()
        page.wait_for_function("() => document.querySelector('#ai-continue-instruction').value === ''")
        self.assertEqual(len(self.posts), 1)
        self.assert_global_request(self.posts[0])
        self.assertEqual(self.posts[0]["skill_selection"], "none")

    def test_failed_defaults_load_blocks_until_rechecked(self):
        page = self.open_page(broken_options=True)
        page.evaluate("CaseAI.startBlank('202H')")
        page.locator("#ai-create-instruction").fill("不会在配置未知时提交。")
        self.assertEqual(page.locator("#ai-create-defaults-summary").inner_text(), "全局默认配置暂不可用")
        self.assertIn("请重新检查", page.locator("#ai-create-settings .ai-settings-issue").inner_text())
        self.assertTrue(page.locator("#submit-ai-create").is_disabled())
        self.broken_options = False
        page.locator("#ai-create-check").click()
        page.locator("#ai-create-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
        self.assertTrue(page.locator("#submit-ai-create").is_enabled())
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "不会在配置未知时提交。")
        self.assertEqual(self.posts, [])

    def test_inflight_old_options_do_not_replace_a_newly_saved_default(self):
        page = self.open_page()
        page.evaluate("CaseAI.startBlank('202H')")
        page.locator("#ai-create-instruction").fill("保留写稿要求。")
        page.locator("#ai-create-advanced").evaluate("details => { details.open = true; }")
        pending = []
        page.route("**/api/ai/options?refresh=1", lambda route: pending.append(route))
        page.locator("#ai-create-check").click()
        page.wait_for_timeout(100)
        self.assertEqual(len(pending), 1)
        page.evaluate("CaseAI.refreshDefaults({model:'claude-sonnet',reasoning_effort:'medium'})")
        pending[0].fulfill(json={**self.options, "default_settings": {"model": None, "reasoning_effort": None},
                                "defaults_error": "旧响应：全局配置已损坏。", "defaults_configured": False})
        page.locator("#ai-create-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考中", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "保留写稿要求。")
        self.assertTrue(page.locator("#submit-ai-create").is_enabled())

    def test_corrupt_defaults_block_new_and_continued_tasks_until_saved(self):
        for continuation in (False, True):
            with self.subTest(continuation=continuation):
                page = self.open_page(continuation=continuation, corrupt_defaults=True)
                if not continuation:
                    page.evaluate("CaseAI.startBlank('202H')")
                prefix = "ai-continue" if continuation else "ai-create"
                submit = "#ai-continue-submit" if continuation else "#submit-ai-create"
                page.locator(f"#{prefix}-defaults-summary").get_by_text("全局默认配置暂不可用", exact=True).wait_for()
                page.locator(f"#{prefix}-instruction").fill("保留输入，不使用错误的默认值。")
                self.assertIn("已损坏", page.locator(f"#{prefix}-settings .ai-settings-issue").inner_text())
                self.assertTrue(page.locator(submit).is_disabled())
                page.locator(f"#{prefix}-form").dispatch_event("submit")
                self.assertEqual(self.posts, [])
                page.evaluate("CaseAI.refreshDefaults({model:'claude-sonnet',reasoning_effort:'medium'})")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考中", exact=True).wait_for()
                self.assertTrue(page.locator(submit).is_enabled())
                self.assertTrue(page.locator(f"#{prefix}-settings .ai-settings-issue").is_hidden())
                self.assertEqual(page.locator(f"#{prefix}-instruction").input_value(), "保留输入，不使用错误的默认值。")
                page.close()

    def test_reopening_create_or_task_reads_defaults_changed_in_another_tab(self):
        for continuation in (False, True):
            with self.subTest(continuation=continuation):
                page = self.open_page(continuation=continuation)
                prefix = "ai-continue" if continuation else "ai-create"
                if not continuation:
                    page.evaluate("CaseAI.startBlank('202H')")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · GPT-6.1-Sol · 思考高", exact=True).wait_for()
                if continuation:
                    page.evaluate("location.hash='#/202H/ai'")
                    page.locator("#ai-task-list").wait_for()
                else:
                    page.locator("#cancel-ai-create").click()
                # Another tab has saved this; this page has received no refreshDefaults call.
                self.options["default_settings"] = {"model": "claude-sonnet", "reasoning_effort": "medium"}
                if continuation:
                    page.locator(".ai-task-card").click()
                else:
                    page.evaluate("CaseAI.startBlank('202H')")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考中", exact=True).wait_for()
                self.assertEqual(self.posts, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
