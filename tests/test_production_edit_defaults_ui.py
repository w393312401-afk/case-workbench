"""Editing inherits global defaults and requires an explicit compatible override, with fake HTTP clients only."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_production_validation_ui as fixture


class ProductionEditDefaultsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ProductionValidationBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ProductionValidationBrowserTests.tearDownClass.__func__)
    stop_server = fixture.ProductionValidationBrowserTests.stop_server
    request = fixture.ProductionValidationBrowserTests.request
    make_playable_video = fixture.ProductionValidationBrowserTests.make_playable_video

    def open_page(self, **kwargs):
        page = fixture.ProductionValidationBrowserTests.open_page(self, **kwargs)
        page.locator('.production-steps [data-step="edit"]').click()
        return page

    def ai_options_route(self, route):
        if self.hold_options:
            self.pending_options.append(route)
        else:
            route.fulfill(status=503 if self.options_error else 200, content_type="application/json",
                          body=json.dumps({"error": self.options_error} if self.options_error else self.catalog))

    def edit_clients_route(self, route):
        route.fulfill(status=503 if self.clients_error else 200, content_type="application/json",
                      body=json.dumps({"error": self.clients_error} if self.clients_error else self.clients))

    def setUp(self):
        fixture.ProductionValidationBrowserTests.setUp(self)
        self.payload["job"]["timelines"] = [{"unique_id": "uid-own", "name": "Lab261011-202-1",
            "creator": "Lab", "markers": 1, "frames": 60, "fps": "29.97", "take_id": "", "built_at": "2026-10-11"}]
        self.timelines = ["蓝点折刀成品素材", "Lab261011-202-1"]
        self.catalog = {"default_settings": {"model": "claude-opus", "reasoning_effort": "high"}, "models": [
            {"id": "claude-default", "name": "Claude 客户端默认", "available": True, "reasoning_efforts": ["high", "max"]},
            {"id": "claude-opus", "name": "Claude Opus", "available": True, "reasoning_efforts": ["high", "max"]},
            {"id": "claude-sonnet", "name": "Claude Sonnet", "available": True, "reasoning_efforts": ["high"]},
            {"id": "gpt-6.1-sol", "name": "Codex 测试模型", "provider": "codex", "available": True,
             "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]}], "skills": []}
        self.clients = {"providers": {"claude": {"available": True}, "codex": {"available": True}}, "busy": False}
        self.clients_error = ""
        self.options_error = ""
        self.hold_options, self.pending_options = False, []

    def test_compatible_defaults_hide_local_controls_and_submit_without_overrides(self):
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Opus · high")
        expect(form.locator("[data-edit-settings]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_be_hidden()
        expect(form.locator('[name="effort"]')).to_be_hidden()
        form.locator('[name="instruction"]').fill("开头使用切纸结果")
        form.locator('[name="mode"]').select_option("sync")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('AI 剪辑已开始')")
        self.assertEqual(len(self.calls), 1)
        body = self.calls[0]["body"]
        self.assertNotIn("model", body)
        self.assertNotIn("effort", body)
        self.assertEqual((body["instruction"], body["mode"], body["materials"]),
                         ("开头使用切纸结果", "sync", ["蓝点折刀成品素材"]))

    def test_incompatible_default_requires_a_choice_and_only_offers_supported_efforts(self):
        self.catalog["default_settings"] = {"model": "claude-opus", "reasoning_effort": "low"}
        self.catalog["models"][2]["available"] = False
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-override]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_have_value("")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        expect(form.locator('option[value="claude-sonnet"]')).to_have_attribute("disabled", "")
        form.locator('[name="model"]').select_option("claude-opus")
        self.assertEqual(form.locator('[name="effort"] option').evaluate_all("rows => rows.map(row => row.value)"), ["", "high", "max"])
        form.locator('[name="effort"]').select_option("max")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('AI 剪辑已开始')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("claude-opus", "max"))

    def test_default_changes_clear_stale_overrides_and_preserve_edit_work(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "unsupported"}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.locator('[name="model"]').select_option("claude-opus")
        form.locator('[name="effort"]').select_option("max")
        form.locator('[name="instruction"]').fill("保留这段额外要求")
        form.locator('[name="mode"]').select_option("montage")
        self.catalog["default_settings"] = {"model": "claude-sonnet", "reasoning_effort": "high"}
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Sonnet · high")
        expect(form.locator('[name="model"]')).to_have_value("")
        expect(form.locator('[name="effort"]')).to_have_value("")
        expect(form.locator('[name="model"]')).to_be_hidden()
        self.catalog["default_settings"] = {"model": "claude-opus", "reasoning_effort": "low"}
        form.locator('[data-edit-default-refresh]').click()
        expect(form.locator("[data-edit-override]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_have_value("")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[name="instruction"]')).to_have_value("保留这段额外要求")
        expect(form.locator('[name="mode"]')).to_have_value("montage")
        self.assertEqual(self.calls, [])

    def test_codex_native_default_uses_its_own_client_and_inherits_effort(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "ultra"}
        self.clients["providers"]["claude"] = {"available": False, "error": "Claude 未连接"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Codex 测试模型 · ultra")
        expect(form.locator("[data-edit-provider-status]")).to_have_text("Codex 客户端可用于 AI 剪辑。")
        expect(form.locator("[data-edit-usage]")).to_contain_text("使用 Codex 客户端当前登录账户的额度")
        expect(form.locator("[data-edit-override]")).to_be_hidden()
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('AI 剪辑已开始')")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("model", self.calls[0]["body"])
        self.assertNotIn("effort", self.calls[0]["body"])

    def test_codex_client_default_and_null_default_work_without_native_catalog(self):
        self.catalog["models"] = []
        self.catalog["models_error"] = "隔离测试：原生模型目录暂不可用"
        self.clients["providers"]["claude"]["available"] = False
        for model in (None, "codex-default"):
            with self.subTest(model=model):
                self.catalog["default_settings"] = {"model": model, "reasoning_effort": None}
                page = self.open_page()
                form = page.locator("#edit-form")
                expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Codex 客户端默认")
                expect(form.locator("[data-edit-override]")).to_be_hidden()
                expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
                page.close()
        self.assertEqual(self.calls, [])

    def test_codex_override_offers_native_reasoning_and_submits_selected_model(self):
        self.catalog["default_settings"] = {"model": "codex-default", "reasoning_effort": "high"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-override]")).to_be_visible()
        form.locator('[name="model"]').select_option("gpt-6.1-sol")
        self.assertEqual(form.locator('[name="effort"] option').evaluate_all("rows => rows.map(row => row.value)"),
                         ["", "low", "medium", "high", "xhigh", "max", "ultra"])
        form.locator('[name="effort"]').select_option("ultra")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('AI 剪辑已开始')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("gpt-6.1-sol", "ultra"))

    def test_unsupported_claude_model_is_not_offered_as_a_compatible_override(self):
        self.catalog["models"].append({"id": "claude-haiku", "name": "Claude Haiku", "provider": "claude",
                                       "available": True, "reasoning_efforts": ["high"]})
        self.catalog["default_settings"] = {"model": "claude-haiku", "reasoning_effort": None}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("Claude Haiku")
        expect(form.locator("[data-edit-override]")).to_be_visible()
        expect(form.locator('[name="model"] option[value="claude-haiku"]')).to_have_count(0)
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        form.locator('[name="model"]').select_option("codex-default")
        self.assertEqual(form.locator('[name="effort"] option').evaluate_all("rows => rows.map(row => row.value)"), [""])
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_codex_unavailable_blocks_start_even_when_claude_is_connected(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        self.clients["providers"]["codex"] = {"available": False, "error": "Codex 未登录"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("Codex 未登录")
        expect(form.locator("[data-edit-override]")).to_be_hidden()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.clients["providers"]["codex"] = {"available": True}
        form.locator('[data-edit-default-refresh]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_busy_or_failed_client_check_blocks_start_and_refresh_recovers(self):
        self.clients["busy"] = True
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("已有达芬奇 AI 任务")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        self.clients["busy"] = False
        self.clients_error = "客户端状态读取失败"
        form.locator('[data-edit-default-refresh]').click()
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("客户端状态读取失败")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        self.clients_error = ""
        form.locator('[data-edit-default-refresh]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_missing_native_catalog_does_not_claim_saved_model_is_incompatible(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        self.catalog["models"] = self.catalog["models"][:3]
        self.catalog["models_error"] = "Codex 模型目录读取失败"
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-override]")).to_be_hidden()
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("Codex 模型目录读取失败")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        self.assertEqual(self.calls, [])

    def test_edit_receipts_distinguish_requested_and_actual_models_and_client(self):
        completed = fixture.ProductionValidationBrowserTests.edit(self, "awaiting_review")
        completed.update(provider="codex", model="codex-default", requested_model="codex-default",
                         effective_model="gpt-6.1-sol", effective_effort="ultra", output="已按标记剪辑。")
        unknown = fixture.ProductionValidationBrowserTests.edit(self, "failed", "EDT-111111111111")
        unknown.update(provider="codex", model="gpt-6.1-sol", effort="high", output="未能启动。")
        self.payload["job"]["edits"] = [completed, unknown]
        page = self.open_page()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        expect(card).to_contain_text("Codex 客户端")
        expect(card).to_contain_text("实际模型：gpt-6.1-sol（申请 Codex 客户端默认）")
        expect(card).to_contain_text("实际思考强度：ultra")
        expect(card).to_contain_text("Codex 客户端的剪辑说明")
        expect(card).not_to_contain_text("Claude")
        unknown_card = page.locator('[data-edit-id="EDT-111111111111"]')
        expect(unknown_card).to_contain_text("申请模型：gpt-6.1-sol · 实际型号尚未回报")
        expect(unknown_card).not_to_contain_text("实际模型：")

    def test_loading_or_failed_defaults_block_start_but_not_handoff(self):
        self.hold_options = True

        def handoff(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"prompt": "isolated handoff"}))

        page = self.open_page(post=handoff)
        page.context.grant_permissions(["clipboard-write", "clipboard-read"])
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("正在读取")
        expect(form.locator('[name="model"]')).to_be_hidden()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        form.locator('[data-copy-handoff]').click()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0]["url"].endswith("/handoff"))
        self.options_error = "隔离测试：全局配置读取失败"
        self.hold_options = False
        for route in self.pending_options:
            self.ai_options_route(route)
        expect(form.locator("[data-edit-default]")).to_contain_text("全局配置读取失败")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[name="model"]')).to_be_hidden()
        self.options_error = ""
        form.locator('[data-edit-default-refresh]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()

    def test_unavailable_compatible_model_blocks_start_without_an_override(self):
        self.catalog["models"][1]["available"] = False
        self.catalog["models"][1]["reasoning_efforts"] = []
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("暂不可用，请在设置中检查连接")
        expect(form.locator('[name="model"]')).to_be_hidden()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_corrupt_preferences_do_not_enable_a_fallback_override(self):
        self.catalog["default_settings"] = {"model": None, "reasoning_effort": None}
        self.catalog["defaults_error"] = "全局默认模型配置无法读取，请在设置重新保存。"
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("配置无法读取")
        expect(form.locator("[data-edit-override]")).to_be_hidden()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
