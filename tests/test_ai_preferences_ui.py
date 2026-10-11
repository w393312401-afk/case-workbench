"""Global model controls use isolated browser responses, never live model calls."""
import unittest
from playwright.sync_api import expect

import test_settings_ui as fixture


MODELS = [
    {"id": "claude-default", "name": "Claude 客户端默认", "provider": "claude", "reasoning_efforts": ["high", "max"], "available": True},
    {"id": "test-codex", "name": "测试 Codex", "provider": "codex", "reasoning_efforts": ["low", "high"], "default_reasoning_effort": "low"},
    {"id": "offline-model", "name": "未连接模型", "available": False},
]


class GlobalModelSettingsTests(unittest.TestCase):
    setUpClass = classmethod(fixture.SettingsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.SettingsBrowserTests.tearDownClass.__func__)
    open_page = fixture.SettingsBrowserTests.open_page

    def open_settings(self, preferences=None, **responses):
        page = self.open_page({
            "/api/ai/status": {"available": True, "providers": {"codex": {"available": True}, "claude": {"available": True}}},
            "/api/performance/summary": {"sources": []},
            "/api/ai/options": {"models": MODELS, "default_settings": preferences or {"model": "claude-default", "reasoning_effort": "max"}, "defaults_configured": True},
            "/api/ai/preferences": {"preferences": preferences or {"model": "claude-default", "reasoning_effort": "max"}},
            **responses,
        })
        page.wait_for_function("!document.querySelector('#settings-default-model').disabled")
        return page

    def test_save_resets_effort_and_reload_preserves_saved_defaults(self):
        page = self.open_settings()
        self.assertEqual(page.locator("#settings-default-model").input_value(), "claude-default")
        self.assertEqual(page.locator("#settings-default-effort").input_value(), "max")
        self.assertTrue(page.locator("#settings-default-save").is_disabled())
        page.locator("#settings-default-model").select_option("test-codex")
        self.assertEqual(page.locator("#settings-default-effort").input_value(), "")
        page.locator("#settings-default-effort").select_option("high")
        saved = {"model": "test-codex", "reasoning_effort": "high"}
        page.evaluate("preferences => { window.responses['/api/ai/preferences'] = {preferences}; window.responses['/api/ai/options'].default_settings = preferences; window.defaultUpdates = []; window.CaseAI.refreshDefaults = value => window.defaultUpdates.push(value); }", saved)
        page.locator("#settings-default-save").click()
        page.locator("#settings-default-status").get_by_text("全局默认已保存", exact=False).wait_for()
        self.assertEqual(page.evaluate("window.requests.filter(item => item.path === '/api/ai/preferences' && item.method === 'POST').map(item => item.body)"), [saved])
        self.assertEqual(page.evaluate("window.defaultUpdates"), [saved])
        page.evaluate("WorkbenchSettings.render()")
        page.wait_for_function("!document.querySelector('#settings-default-model').disabled")
        self.assertEqual(page.locator("#settings-default-model").input_value(), saved["model"])
        self.assertEqual(page.locator("#settings-default-effort").input_value(), saved["reasoning_effort"])

    def test_missing_saved_model_is_visible_and_can_be_replaced_with_client_default(self):
        page = self.open_settings({"model": "retired-model", "reasoning_effort": "ultra"})
        self.assertEqual(page.locator("#settings-default-model").input_value(), "retired-model")
        self.assertIn("当前不在可用列表", page.locator("#settings-default-error").inner_text())
        self.assertTrue(page.locator("#settings-default-save").is_disabled())
        page.locator("#settings-default-model").select_option("")
        self.assertEqual(page.locator("#settings-default-effort").input_value(), "")
        self.assertTrue(page.locator("#settings-default-effort").is_disabled())
        self.assertTrue(page.locator("#settings-default-save").is_enabled())
        expect(page.locator('#settings-default-model option[value="offline-model"]')).to_have_attribute("disabled", "")

    def test_failed_save_keeps_draft_and_allows_retry(self):
        page = self.open_settings(**{"/api/ai/preferences#POST": {"error_fixture": "保存暂时失败"}})
        page.locator("#settings-default-model").select_option("test-codex")
        page.locator("#settings-default-save").click()
        page.get_by_text("保存暂时失败", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-default-model").input_value(), "test-codex")
        self.assertTrue(page.locator("#settings-default-save").is_enabled())
        page.evaluate("window.responses['/api/ai/preferences#POST'] = {preferences:{model:'test-codex',reasoning_effort:null}}")
        page.locator("#settings-default-save").click()
        page.locator("#settings-default-status").get_by_text("全局默认已保存", exact=False).wait_for()

    def test_refresh_preserves_unsaved_selection(self):
        page = self.open_settings(**{"/api/ai/options?refresh=1": {"models": MODELS, "default_settings": {"model": "claude-default", "reasoning_effort": "max"}}})
        page.locator("#settings-default-model").select_option("test-codex")
        page.locator("#settings-default-effort").select_option("high")
        page.locator("#settings-default-refresh").click()
        page.wait_for_function("!document.querySelector('#settings-default-model').disabled")
        self.assertEqual(page.locator("#settings-default-model").input_value(), "test-codex")
        self.assertEqual(page.locator("#settings-default-effort").input_value(), "high")
        self.assertTrue(page.locator("#settings-default-save").is_enabled())

    def test_failed_loading_does_not_allow_overwriting_unknown_preferences(self):
        page = self.open_page({
            "/api/ai/options": {"error_fixture": "读取失败"},
            "/api/ai/status": {"available": True},
            "/api/performance/summary": {"sources": []},
            "/api/ai/options?refresh=1": {"models": MODELS, "default_settings": {"model": None, "reasoning_effort": None}},
        })
        page.get_by_text("读取失败", exact=True).wait_for()
        self.assertTrue(page.locator("#settings-default-save").is_disabled())
        self.assertTrue(page.locator("#settings-default-model").is_disabled())
        page.evaluate("window.responses['/api/ai/preferences'] = {preferences:{model:null,reasoning_effort:null}}")
        page.locator("#settings-default-refresh").click()
        page.wait_for_function("!document.querySelector('#settings-default-model').disabled")
        self.assertEqual(page.locator("#settings-default-model").input_value(), "")

    def test_narrow_screen_and_labels_remain_usable(self):
        page = self.open_settings()
        page.set_viewport_size({"width": 390, "height": 844})
        expect(page.get_by_role("combobox", name="默认模型", exact=True)).to_be_visible()
        expect(page.get_by_role("combobox", name="思考强度", exact=True)).to_be_visible()
        self.assertTrue(page.evaluate("document.querySelector('.settings-model-card').scrollWidth <= document.querySelector('.settings-model-card').clientWidth"))

    def test_initial_default_can_be_saved_without_changing_it(self):
        page = self.open_settings(**{"/api/ai/options": {"models": MODELS, "default_settings": {"model": "claude-default", "reasoning_effort": None}, "defaults_configured": False}})
        expect(page.locator("#settings-default-save")).to_be_enabled()
        page.evaluate("window.responses['/api/ai/preferences#POST'] = {preferences:{model:'claude-default',reasoning_effort:null}}")
        page.locator("#settings-default-save").click()
        page.locator("#settings-default-status").get_by_text("全局默认已保存", exact=False).wait_for()
        expect(page.locator("#settings-default-save")).to_be_disabled()

    def test_corrupt_preferences_can_be_repaired_without_losing_model_catalog(self):
        page = self.open_settings(**{"/api/ai/options": {"models": MODELS, "default_settings": {"model": None, "reasoning_effort": None}, "defaults_configured": True, "defaults_error": "全局配置无法读取，请重新保存。"}})
        page.get_by_text("全局配置无法读取，请重新保存。", exact=True).wait_for()
        page.locator("#settings-default-model").select_option("test-codex")
        page.evaluate("window.responses['/api/ai/preferences#POST'] = {preferences:{model:'test-codex',reasoning_effort:null}}")
        page.locator("#settings-default-save").click()
        page.locator("#settings-default-status").get_by_text("全局默认已保存", exact=False).wait_for()


if __name__ == "__main__":
    unittest.main()
