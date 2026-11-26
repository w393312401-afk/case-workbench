"""Editing pre-fills global defaults, allows a per-edit choice, and reminds above medium, with fake HTTP clients only."""
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
        page.locator('.production-steps [data-step~="edit"]').click()
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
        # Nothing is ticked by default any more; the last edit's material is.
        self.payload["product_settings"]["material_timelines"] = ["蓝点折刀成品素材"]
        self.catalog = {"default_settings": {"model": "claude-opus", "reasoning_effort": "high"}, "models": [
            {"id": "claude-default", "name": "Claude 客户端默认", "available": True, "reasoning_efforts": ["high", "max"]},
            {"id": "claude-opus", "name": "Claude Opus", "available": True, "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "claude-sonnet", "name": "Claude Sonnet", "available": True, "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
            {"id": "gpt-6.1-sol", "name": "Codex 测试模型", "provider": "codex", "available": True,
             "reasoning_efforts": ["low", "medium", "high", "xhigh", "max", "ultra"]}], "skills": []}
        self.clients = {"providers": {"claude": {"available": True}, "codex": {"available": True}}, "busy": False}
        self.clients_error = ""
        self.options_error = ""
        self.hold_options, self.pending_options = False, []

    def add_api_model(self):
        model = "antigravity:claude-sonnet-5-5-medium"
        self.catalog["models"].append({"id": model, "name": "Antigravity Sonnet", "provider": "antigravity",
                                        "available": True, "reasoning_efforts": []})
        self.clients["providers"]["antigravity"] = {"available": True, "label": "Antigravity API 已就绪",
                                                       "usage_source": "antigravity_api"}
        return model

    def start_and_keep(self, form, effort):
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        bar = form.locator("[data-effort-confirm]")
        expect(bar).to_contain_text(f"思考强度 {effort} 高于 medium")
        self.assertEqual(self.calls, [])
        bar.locator("[data-effort-keep]").click()

    def test_compatible_defaults_prefill_controls_and_submit_without_overrides(self):
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Opus · high")
        expect(form.locator("[data-edit-settings]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_have_value("claude-opus")
        expect(form.locator('[name="effort"]')).to_have_value("high")
        form.locator('[name="instruction"]').fill("开头使用切纸结果")
        self.assertEqual(form.locator('select[name="mode"]').count(), 0)
        self.start_and_keep(form, "high")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual(len(self.calls), 1)
        body = self.calls[0]["body"]
        self.assertNotIn("model", body)
        self.assertNotIn("effort", body)
        self.assertEqual((body["instruction"], body["mode"], body["materials"]),
                         ("开头使用切纸结果", "explain", ["蓝点折刀成品素材"]))

    def test_api_global_default_is_offered_and_used_for_editing(self):
        model = self.add_api_model()
        self.catalog["default_settings"] = {"model": model, "reasoning_effort": None}
        self.clients["providers"]["claude"]["available"] = False
        self.clients["providers"]["codex"]["available"] = False
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Antigravity Sonnet · 模型默认思考强度")
        expect(form.locator('[name="model"]')).to_have_value(model)
        expect(form.locator('[name="effort"]')).to_have_value("")
        expect(form.locator('[name="effort"]')).to_be_disabled()
        expect(form.locator('[name="effort"]')).to_have_text("模型默认")
        self.assertEqual(form.locator(f'option[value="{model}"]').count(), 1)
        expect(form.locator("[data-edit-provider-status]")).to_have_text("Antigravity API可用于 AI 剪辑。")
        expect(form.locator("[data-edit-usage]")).to_contain_text("使用 Antigravity 服务额度/计费，由本机 Claude Code 执行剪辑")
        expect(form.locator("[data-edit-usage]")).not_to_contain_text("订阅额度")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertNotIn("model", self.calls[0]["body"])
        self.assertNotIn("effort", self.calls[0]["body"])

    def test_api_override_submits_model_without_native_reasoning_effort(self):
        model = self.add_api_model()
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator('[name="effort"]')).to_have_value("high")
        form.locator('[data-edit-provider="antigravity"]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        form.locator('[data-edit-default-refresh]').click()
        expect(form.locator('[data-edit-provider="antigravity"]')).to_have_attribute('aria-pressed', 'true')
        expect(form.locator('[name="model"]')).to_have_value('')
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        form.locator('[name="model"]').select_option(model)
        expect(form.locator('[name="effort"]')).to_have_value("")
        expect(form.locator('[name="effort"]')).to_be_disabled()
        expect(form.locator("[data-edit-usage]")).to_contain_text("使用 Antigravity 服务额度/计费")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), (model, None))

    def test_api_default_waits_for_its_edit_executor_without_falling_back(self):
        model = self.add_api_model()
        self.catalog["default_settings"] = {"model": model, "reasoning_effort": None}
        self.clients["providers"]["antigravity"] = {"available": False, "error": "未找到 Claude Code 执行程序"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator('[name="model"]')).to_have_value(model)
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("未找到 Claude Code 执行程序")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        self.clients["providers"]["antigravity"]["available"] = True
        form.locator('[data-edit-default-refresh]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        expect(form.locator('[name="model"]')).to_have_value(model)

    def test_low_effort_default_is_used_without_an_override(self):
        self.catalog["default_settings"] = {"model": "claude-sonnet", "reasoning_effort": "medium"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Sonnet · medium")
        expect(form.locator("[data-edit-override-note]")).to_be_hidden()
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertNotIn("effort", self.calls[0]["body"])

    def test_incompatible_default_requires_a_choice_and_offers_every_model_effort(self):
        self.catalog["default_settings"] = {"model": "claude-opus", "reasoning_effort": "ultra"}
        self.catalog["models"][2]["available"] = False
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-override]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_have_value("")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        form.locator('[data-edit-provider="claude"]').click()
        expect(form.locator('option[value="claude-sonnet"]')).to_have_attribute("disabled", "")
        form.locator('[name="model"]').select_option("claude-opus")
        self.assertEqual(form.locator('[name="effort"] option').evaluate_all("rows => rows.map(row => row.value)"), ["", "low", "medium", "high", "xhigh", "max"])
        form.locator('[name="effort"]').select_option("max")
        self.start_and_keep(form, "max")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("claude-opus", "max"))

    def test_default_changes_clear_stale_overrides_and_preserve_edit_work(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "unsupported"}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.locator('[data-edit-provider="claude"]').click()
        form.locator('[name="model"]').select_option("claude-opus")
        form.locator('[name="effort"]').select_option("max")
        form.locator('[name="instruction"]').fill("保留这段额外要求")
        self.catalog["default_settings"] = {"model": "claude-sonnet", "reasoning_effort": "high"}
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Sonnet · high")
        expect(form.locator('[name="model"]')).to_have_value("claude-sonnet")
        expect(form.locator('[name="effort"]')).to_have_value("high")
        self.catalog["default_settings"] = {"model": "claude-opus", "reasoning_effort": "ultra"}
        form.locator('[data-edit-default-refresh]').click()
        expect(form.locator("[data-edit-override]")).to_be_visible()
        expect(form.locator('[name="model"]')).to_have_value("")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[name="instruction"]')).to_have_value("保留这段额外要求")
        self.assertEqual(self.calls, [])

    def test_codex_native_default_uses_its_own_client_and_inherits_effort(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "ultra"}
        self.clients["providers"]["claude"] = {"available": False, "error": "Claude 未连接"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Codex 测试模型 · ultra")
        expect(form.locator("[data-edit-provider-status]")).to_have_text("Codex 客户端可用于 AI 剪辑。")
        expect(form.locator("[data-edit-usage]")).to_contain_text("使用 Codex 客户端当前登录账户的额度")
        self.start_and_keep(form, "ultra")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
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
                expect(form.locator('[name="model"]')).to_have_value("codex-default")
                expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
                page.close()
        self.assertEqual(self.calls, [])

    def test_codex_override_offers_native_reasoning_and_submits_selected_model(self):
        self.catalog["default_settings"] = {"model": "codex-default", "reasoning_effort": "high"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-override]")).to_be_visible()
        form.locator('[data-edit-provider="codex"]').click()
        form.locator('[name="model"]').select_option("gpt-6.1-sol")
        self.assertEqual(form.locator('[name="effort"] option').evaluate_all("rows => rows.map(row => row.value)"),
                         ["", "low", "medium", "high", "xhigh", "max", "ultra"])
        form.locator('[name="effort"]').select_option("ultra")
        self.start_and_keep(form, "ultra")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("gpt-6.1-sol", "ultra"))

    def test_haiku_default_is_used_for_editing(self):
        self.catalog["models"].append({"id": "claude-haiku", "name": "Claude Haiku", "provider": "claude",
                                       "available": True, "reasoning_efforts": ["low", "medium", "high"]})
        self.catalog["default_settings"] = {"model": "claude-haiku", "reasoning_effort": "medium"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Haiku · medium")
        expect(form.locator("[data-edit-default]")).not_to_contain_text("不兼容")
        expect(form.locator('[name="model"]')).to_have_value("claude-haiku")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        self.assertEqual(form.locator('[name="model"] option[value="claude-haiku"]').count(), 1)

    def test_codex_unavailable_blocks_start_even_when_claude_is_connected(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "high"}
        self.clients["providers"]["codex"] = {"available": False, "error": "Codex 未登录"}
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("Codex 未登录")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_disabled()
        expect(form.locator('[data-copy-handoff]')).to_be_enabled()
        self.clients["providers"]["codex"] = {"available": True}
        form.locator('[data-edit-default-refresh]').click()
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
        self.assertEqual(self.calls, [])

    def test_busy_allows_start_but_failed_client_check_blocks_until_refresh(self):
        self.clients["busy"] = True
        page = self.open_page()
        form = page.locator("#edit-form")
        expect(form.locator("[data-edit-provider-status]")).to_contain_text("需要操作达芬奇时会自动等待")
        expect(form.get_by_role('button', name='开始 AI 剪辑', exact=True)).to_be_enabled()
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
        expect(form.locator('[name="model"]')).to_have_value("gpt-6.1-sol")
        expect(form.locator('[name="effort"]')).to_have_value("high")
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

    def test_api_edit_receipt_uses_service_billing_label(self):
        model = self.add_api_model()
        completed = fixture.ProductionValidationBrowserTests.edit(self, "awaiting_review")
        completed.update(provider="antigravity", client_label="Antigravity API", model=model, requested_model=model,
                         effective_model="claude-sonnet-5-5-medium", effective_effort=None, effort=None,
                         output="已按标记剪辑。", ai_usage={"editor": {"runs": 1, "turns": 3, "input_tokens": 100, "output_tokens": 20},
                                                              "total": {"cost_usd": 1.234}})
        self.payload["job"]["edits"] = [completed]
        page = self.open_page()
        card = page.locator('[data-edit-id="EDT-0123456789ab"]')
        expect(card).to_contain_text("Antigravity API")
        expect(card).to_contain_text("使用 Antigravity 服务额度/计费")
        expect(card).not_to_contain_text("Claude 订阅额度")
        expect(card.locator(".edit-usage")).to_contain_text("客户端估算约 $1.23；实际费用以 Antigravity 为准")
        expect(card.locator(".edit-usage")).not_to_contain_text("按 API 价约")

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

    def test_one_edit_can_pick_another_model_without_a_reminder_at_medium(self):
        self.catalog["default_settings"] = {"model": "claude-opus", "reasoning_effort": "max"}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.locator('[name="model"]').select_option("claude-sonnet")
        expect(form.locator('[name="effort"]')).to_have_value("max")
        form.locator('[name="effort"]').select_option("medium")
        expect(form.locator("[data-edit-default]")).to_contain_text("全局默认：Claude Opus · max（本次已单独选择）")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("claude-sonnet", "medium"))

    def test_picking_the_default_again_sends_no_override(self):
        page = self.open_page()
        form = page.locator("#edit-form")
        form.locator('[name="effort"]').select_option("medium")
        form.locator('[name="effort"]').select_option("high")
        expect(form.locator("[data-edit-default]")).to_contain_text("使用全局默认：Claude Opus · high")
        self.start_and_keep(form, "high")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertNotIn("model", self.calls[0]["body"])

    def test_reminder_can_lower_to_medium_for_this_edit_only(self):
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "max"}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        bar = form.locator("[data-effort-confirm]")
        expect(bar).to_be_visible()
        expect(form.locator(".take-actions")).to_be_hidden()
        expect(bar.locator("[data-effort-keep]")).to_have_text("仍用 max 开始")
        bar.locator("[data-effort-lower]").click()
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((self.calls[0]["body"]["model"], self.calls[0]["body"]["effort"]), ("gpt-6.1-sol", "medium"))

    def test_reminder_cancel_and_form_changes_do_not_start(self):
        page = self.open_page()
        form = page.locator("#edit-form")
        bar = form.locator("[data-effort-confirm]")
        start = form.get_by_role('button', name='开始 AI 剪辑', exact=True)
        start.click()
        bar.locator("[data-effort-cancel]").click()
        expect(bar).to_be_hidden()
        expect(start).to_be_visible()
        start.click()
        expect(bar).to_be_visible()
        form.locator('[name="effort"]').select_option("xhigh")
        expect(bar).to_be_hidden()
        self.assertEqual(self.calls, [])
        self.start_and_keep(form, "xhigh")
        page.wait_for_function("() => document.querySelector('#toast')?.textContent.includes('已加入剪辑任务')")
        self.assertEqual(self.calls[0]["body"]["effort"], "xhigh")

    def test_reminder_hides_lowering_when_the_model_has_no_medium(self):
        self.catalog["models"][3]["reasoning_efforts"] = ["high", "max"]
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": "max"}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        bar = form.locator("[data-effort-confirm]")
        expect(bar.locator("[data-effort-keep]")).to_be_visible()
        expect(bar.locator("[data-effort-lower]")).to_be_hidden()

    def test_client_default_effort_is_judged_by_the_models_own_default(self):
        self.catalog["models"][3]["default_reasoning_effort"] = "ultra"
        self.catalog["default_settings"] = {"model": "gpt-6.1-sol", "reasoning_effort": None}
        page = self.open_page()
        form = page.locator("#edit-form")
        form.get_by_role('button', name='开始 AI 剪辑', exact=True).click()
        expect(form.locator("[data-effort-confirm]")).to_contain_text("思考强度 ultra 高于 medium")


if __name__ == "__main__":
    unittest.main()
