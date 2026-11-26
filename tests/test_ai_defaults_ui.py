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

    def open_page(self, *, continuation=False, broken_options=False, corrupt_defaults=False, task_status="completed"):
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
        self.task = {"task_id": TASK_ID, "revision": "fixture-revision", "status": task_status, "busy": False,
                     "product": "202H", "action": "variants", "context": {"title": "已保存任务"},
                     "rounds": [{"round_id": "one", "status": task_status, "instruction": "先前要求",
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
                    page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
                else:
                    page.evaluate("""async action => {
                      document.querySelector('#content').insertAdjacentHTML('beforeend','<div id="detail-ai-panel"></div>');
                      await CaseAI.mountCaseActions({case_id:'fixture-case',product:'202H',revision:'fixture-revision',title:'临时案例'});
                      document.querySelector(`[data-ai-action="${action}"]`).click();
                    }""", action)
                summary = page.locator("#ai-create-defaults-summary")
                summary.get_by_text("使用全局默认 · GPT-6.1-Sol · 思考强度：高", exact=True).wait_for()
                # Only writing offers a one-off model choice; it starts on the global default.
                self.assertEqual(page.locator("#ai-create-model,#ai-create-effort").count(), 2 if action == "variants" else 0)
                if action == "variants":
                    self.assertEqual(page.locator("#ai-create-model").input_value(), "__default__")
                    self.assertTrue(page.locator("#ai-create-model").is_disabled())
                    self.assertEqual(page.locator('[data-create-provider="__default__"]').get_attribute("aria-pressed"), "true")
                    self.assertTrue(page.locator("#ai-create-effort").is_disabled())
                self.assertIsNone(page.evaluate("localStorage.getItem('case-workbench.ai.last-settings')"))
                self.assertEqual(page.locator("#ai-create-defaults a").get_attribute("target"), "_blank")
                page.locator("#ai-create-instruction").fill("测试全局默认配置。")
                page.locator("#submit-ai-create").click()
                page.locator("#ai-rounds").wait_for()
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(self.posts[0]["action"], action)
                self.assert_global_request(self.posts[0])
                page.close()

    def test_writing_can_pick_a_model_for_this_task_only(self):
        page = self.open_page()
        page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
        page.locator("#ai-create-defaults-summary").get_by_text("使用全局默认 · GPT-6.1-Sol · 思考强度：高", exact=True).wait_for()
        page.locator('[data-create-provider="claude"]').click()
        self.assertEqual(page.locator('[data-create-provider="claude"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator("#ai-create-model").input_value(), "")
        self.assertEqual(page.locator('#ai-create-model option[value="gpt-6.1-sol"]').count(), 0)
        self.assertTrue(page.locator("#submit-ai-create").is_disabled())
        page.locator("#ai-create-model").select_option("claude-sonnet")
        self.assertEqual(page.locator("#ai-create-effort").input_value(), "")
        page.locator("#ai-create-effort").select_option("medium")
        self.assertIn("本次单独选择", page.locator("#ai-create-defaults-summary").inner_text())
        page.locator("#ai-create-instruction").fill("用单独模型写稿。")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-rounds").wait_for()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["model"], "claude-sonnet")
        self.assertEqual(self.posts[0]["reasoning_effort"], "medium")
        page.close()

    def test_service_switch_clears_previous_selection_and_can_return_to_global_default(self):
        page = self.open_page()
        page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
        page.locator("#ai-create-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
        page.locator('[data-create-provider="claude"]').click()
        page.locator("#ai-create-model").select_option("claude-sonnet")
        page.locator("#ai-create-effort").select_option("medium")
        page.locator('[data-create-provider="codex"]').click()
        self.assertEqual(page.locator("#ai-create-model").input_value(), "")
        self.assertEqual(page.locator('#ai-create-model option[value="claude-sonnet"]').count(), 0)
        self.assertEqual(page.evaluate("document.activeElement.dataset.createProvider"), "codex")
        page.locator('[data-create-provider="__default__"]').click()
        self.assertTrue(page.locator("#ai-create-model").is_disabled())
        self.assertTrue(page.locator("#ai-create-effort").is_disabled())
        self.assertIn("使用全局默认", page.locator("#ai-create-defaults-summary").inner_text())
        page.locator("#ai-create-instruction").fill("使用全局默认。")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-rounds").wait_for()
        self.assert_global_request(self.posts[0])

    def test_continuation_uses_changed_defaults_preserves_draft_and_keeps_historical_model(self):
        page = self.open_page(continuation=True)
        summary = page.locator("#ai-continue-defaults-summary")
        summary.get_by_text("使用全局默认 · GPT-6.1-Sol · 思考强度：高", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-continue-model,#ai-continue-effort").count(), 0)
        self.assertIn("actual-history-model", page.locator("#ai-rounds").inner_text())
        page.locator("#ai-continue-instruction").fill("保留这份续写要求。")
        page.evaluate("CaseAI.refreshDefaults({model:'claude-sonnet',reasoning_effort:'medium'})")
        summary.get_by_text("使用全局默认 · Claude Sonnet · 思考强度：中", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-continue-instruction").input_value(), "保留这份续写要求。")
        page.locator("#ai-continue-submit").click()
        page.wait_for_function("() => document.querySelector('#ai-continue-instruction').value === ''")
        self.assertEqual(len(self.posts), 1)
        self.assert_global_request(self.posts[0])
        self.assertEqual(self.posts[0]["skill_selection"], "none")

    def test_stopped_task_retries_without_typing_and_accepts_extra_requirements(self):
        for status in ("failed", "interrupted", "cancelled"):
            with self.subTest(status=status):
                page = self.open_page(continuation=True, task_status=status)
                page.locator("#ai-continue-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
                instruction = page.locator("#ai-continue-instruction")
                submit = page.locator("#ai-continue-submit")
                self.assertEqual(instruction.input_value(), "")
                self.assertFalse(instruction.evaluate("node => node.required"))
                self.assertTrue(submit.is_enabled())
                self.assertEqual(submit.inner_text(), "重试")
                self.assertIn("无需输入", page.locator("#ai-continue-note").inner_text())
                # A blank retry is explicit in the payload; the server restores the failed round's request.
                with page.expect_response(lambda response: response.url.endswith("/continue") and response.request.method == "POST"):
                    if status == "interrupted":
                        instruction.press("Control+Enter")
                    else:
                        submit.click()
                page.locator("#ai-continue-submit").get_by_text("重试", exact=True).wait_for()
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(self.posts[0]["instruction"], "")
                self.assertEqual(self.posts[0]["task_revision"], "fixture-revision")
                self.assert_global_request(self.posts[0])
                instruction.fill("   ")
                self.assertEqual(submit.inner_text(), "重试")
                instruction.fill("保留原稿，再压短一点。")
                self.assertEqual(submit.inner_text(), "继续修改并重试")
                with page.expect_response(lambda response: response.url.endswith("/continue") and response.request.method == "POST"):
                    submit.click()
                page.wait_for_function("() => document.querySelector('#ai-continue-instruction').value === ''")
                self.assertEqual(len(self.posts), 2)
                self.assertEqual(self.posts[1]["instruction"], "保留原稿，再压短一点。")
                page.close()

    def test_blank_retry_keeps_one_request_while_busy_and_after_an_uncertain_response(self):
        page = self.open_page(continuation=True, task_status="failed")
        page.locator("#ai-continue-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
        attempts = []
        page.route(f"**/api/ai/tasks/{TASK_ID}/continue", lambda route: attempts.append(route))
        instruction = page.locator("#ai-continue-instruction")
        with page.expect_request(lambda request: request.url.endswith("/continue") and request.method == "POST"):
            instruction.press("Control+Enter")
        page.wait_for_function("() => document.querySelector('#ai-continue-submit').disabled")
        page.locator("#ai-continue-form").dispatch_event("submit")
        instruction.press("Control+Enter")
        self.assertEqual(len(attempts), 1)
        first_payload = attempts[0].request.post_data_json
        self.assertEqual(first_payload["instruction"], "")
        attempts[0].fulfill(status=503, json={"error": "测试：响应尚未确认"})
        page.locator("#ai-continue-submit").get_by_text("重试同一请求", exact=True).wait_for()
        self.assertEqual(instruction.input_value(), "")
        self.assertTrue(instruction.evaluate("node => node.readOnly"))
        self.assertFalse(instruction.evaluate("node => node.required"))
        with page.expect_request(lambda request: request.url.endswith("/continue") and request.method == "POST"):
            page.locator("#ai-continue-submit").click()
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[1].request.post_data_json, first_payload)
        attempts[1].fulfill(json={"task": self.task})
        page.locator("#ai-continue-submit").get_by_text("重试", exact=True).wait_for()

    def test_completed_task_still_requires_a_new_request(self):
        page = self.open_page(continuation=True)
        page.locator("#ai-continue-defaults-summary").get_by_text("使用全局默认", exact=False).wait_for()
        instruction = page.locator("#ai-continue-instruction")
        self.assertTrue(instruction.evaluate("node => node.required"))
        self.assertTrue(page.locator("#ai-continue-submit").is_disabled())
        instruction.press("Control+Enter")
        page.locator("#ai-continue-form").dispatch_event("submit")
        self.assertEqual(self.posts, [])

    def test_failed_defaults_load_blocks_until_rechecked(self):
        page = self.open_page(broken_options=True)
        page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
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
        page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
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
        page.locator("#ai-create-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考强度：中", exact=True).wait_for()
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "保留写稿要求。")
        self.assertTrue(page.locator("#submit-ai-create").is_enabled())

    def test_corrupt_defaults_block_new_and_continued_tasks_until_saved(self):
        for continuation in (False, True):
            with self.subTest(continuation=continuation):
                page = self.open_page(continuation=continuation, corrupt_defaults=True)
                if not continuation:
                    page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
                prefix = "ai-continue" if continuation else "ai-create"
                submit = "#ai-continue-submit" if continuation else "#submit-ai-create"
                page.locator(f"#{prefix}-defaults-summary").get_by_text("全局默认配置暂不可用", exact=True).wait_for()
                page.locator(f"#{prefix}-instruction").fill("保留输入，不使用错误的默认值。")
                self.assertIn("已损坏", page.locator(f"#{prefix}-settings .ai-settings-issue").inner_text())
                self.assertTrue(page.locator(submit).is_disabled())
                page.locator(f"#{prefix}-form").dispatch_event("submit")
                self.assertEqual(self.posts, [])
                page.evaluate("CaseAI.refreshDefaults({model:'claude-sonnet',reasoning_effort:'medium'})")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考强度：中", exact=True).wait_for()
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
                    page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · GPT-6.1-Sol · 思考强度：高", exact=True).wait_for()
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
                    page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
                page.locator(f"#{prefix}-defaults-summary").get_by_text("使用全局默认 · Claude Sonnet · 思考强度：中", exact=True).wait_for()
                self.assertEqual(self.posts, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
