"""Antigravity settings and writing controls with isolated browser fixtures."""
from __future__ import annotations

import copy
import unittest

import test_ai_defaults_ui as writing_fixture
import test_settings_ui as settings_fixture


MODEL = "antigravity:claude-sonnet-5-5-medium"
API_MODEL = {"id": MODEL, "name": "Antigravity Sonnet", "provider": "antigravity",
             "available": True, "reasoning_efforts": []}
CONFIG = {"settings": {"enabled": True, "base_url": "http://127.0.0.1:8046/v1",
                       "model": "claude-sonnet-5-5-medium", "vision": False},
          "status": {"available": True, "label": "Antigravity API 已配置"},
          "key_set": True, "key_tail": "abcd"}


def key_catalog(*, configured=True, tail="abcd"):
    return {"keys": [
        {"name": "ANTIGRAVITY_API_KEY", "label": "Antigravity", "used_for": "AI 写稿",
         "where": "Antigravity Tools", "url": "http://127.0.0.1:8046", "prefix": "sk-", "storage": "本机私有配置",
         "set": configured, "tail": tail if configured else "", "looks_right": configured},
        {"name": "APIFY_TOKEN", "label": "Apify", "used_for": "评论抓取",
         "where": "Apify", "url": "https://console.apify.com", "prefix": "apify_api_",
         "set": False, "tail": "", "looks_right": False},
    ]}


class AntigravitySettingsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(settings_fixture.SettingsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(settings_fixture.SettingsBrowserTests.tearDownClass.__func__)
    open_page = settings_fixture.SettingsBrowserTests.open_page

    def open_settings(self, responses=None, *, origin="http://127.0.0.1:8765"):
        responses = {"/api/ai/antigravity": copy.deepcopy(CONFIG),
                     "/api/keys": key_catalog(),
                     "/api/ai/status": {"available": True, "providers": {
                         "codex": {"available": False}, "claude": {"available": False},
                         "antigravity": CONFIG["status"]}},
                     "/api/ai/options": {"models": [API_MODEL],
                                         "default_settings": {"model": MODEL, "reasoning_effort": None},
                                         "defaults_configured": True},
                     **(responses or {})}
        return self.open_page(responses, origin=origin)

    def test_configuration_is_local_and_only_displays_key_tail(self):
        page = self.open_settings()
        page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-antigravity-key").input_value(), "")
        self.assertEqual(page.locator("#settings-antigravity-key").get_attribute("type"), "password")
        self.assertEqual(page.locator("#settings-antigravity-key").get_attribute("autocomplete"), "off")
        self.assertIn("····abcd", page.locator("#settings-antigravity-key-state").inner_text())
        self.assertFalse(page.locator("#settings-antigravity-vision").is_checked())
        self.assertEqual(page.locator('[data-settings-model-provider="antigravity"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator(f'#settings-default-model option[value="{MODEL}"]').count(), 1)
        self.assertEqual(page.locator("#settings-default-model").input_value(), MODEL)
        self.assertTrue(page.locator("#settings-default-effort").is_disabled())
        self.assertIn("可改用 Antigravity", page.locator("#settings-codex-status").inner_text())
        self.assertEqual(page.evaluate("window.storageWrites"), [])

    def test_save_is_single_request_and_clears_secret_before_response(self):
        page = self.open_settings({"/api/ai/antigravity#POST": {"deferred_fixture": True},
                                   "/api/ai/antigravity/check": {"ok": True, "detail": "模型已返回回复"}})
        page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
        page.locator("#settings-antigravity-key").fill("sk-fixture-secret-never-display")
        self.assertTrue(page.locator("#settings-antigravity-check").is_disabled())
        page.evaluate("""() => {
          const form = document.querySelector('#settings-antigravity-form');
          form.dispatchEvent(new Event('submit', {cancelable:true}));
          form.dispatchEvent(new Event('submit', {cancelable:true}));
        }""")
        page.wait_for_function("window.deferredRequests.length === 1")
        self.assertEqual(page.locator("#settings-antigravity-key").input_value(), "")
        self.assertTrue(page.locator("#settings-antigravity-save").is_disabled())
        posts = page.evaluate("window.requests.filter(row => row.path === '/api/ai/antigravity' && row.method === 'POST')")
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["body"], {**CONFIG["settings"], "api_key": "sk-fixture-secret-never-display"})
        page.evaluate("data => window.deferredRequests[0].resolve(data)", CONFIG)
        page.get_by_text("API 配置已保存，可以检查连接并选择全局默认模型。", exact=True).wait_for()
        page.locator("#settings-antigravity-check").click()
        page.get_by_text("连接成功：模型已返回回复", exact=True).wait_for()
        checks = page.evaluate("window.requests.filter(row => row.path === '/api/ai/antigravity/check')")
        self.assertEqual(checks[0]["body"], {})
        self.assertNotIn("sk-fixture", page.locator("#settings-antigravity-card").inner_text())
        self.assertEqual(page.evaluate("window.storageWrites"), [])

    def test_save_failure_keeps_draft_but_clears_key_and_never_checks_unsaved_data(self):
        page = self.open_settings({"/api/ai/antigravity#POST": {"error_fixture": "测试：配置写入失败"}})
        page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
        page.locator("#settings-antigravity-model").fill("another-model")
        page.locator("#settings-antigravity-key").fill("sk-fixture-private-key")
        page.locator("#settings-antigravity-save").click()
        page.get_by_text("测试：配置写入失败", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-antigravity-key").input_value(), "")
        self.assertEqual(page.locator("#settings-antigravity-model").input_value(), "another-model")
        self.assertTrue(page.locator("#settings-antigravity-check").is_disabled())
        self.assertNotIn("/api/ai/antigravity/check", page.evaluate("window.calls"))
        self.assertEqual(page.evaluate("window.storageWrites"), [])

    def test_blank_key_preserves_existing_secret(self):
        page = self.open_settings({"/api/ai/antigravity#POST": CONFIG})
        page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
        page.locator("#settings-antigravity-save").click()
        page.get_by_text("API 配置已保存，可以检查连接并选择全局默认模型。", exact=True).wait_for()
        posts = page.evaluate("window.requests.filter(row => row.path === '/api/ai/antigravity' && row.method === 'POST')")
        self.assertNotIn("api_key", posts[0]["body"])

    def test_api_model_can_be_saved_as_global_default(self):
        page = self.open_settings({"/api/ai/options": {"models": [API_MODEL],
                                                       "default_settings": {"model": None, "reasoning_effort": None},
                                                       "defaults_configured": True},
                                   "/api/ai/preferences": {"preferences": {"model": MODEL, "reasoning_effort": None}}})
        page.locator('[data-settings-model-provider="antigravity"]').click()
        page.locator(f'#settings-default-model option[value="{MODEL}"]').wait_for(state="attached")
        page.locator("#settings-default-model").select_option(MODEL)
        self.assertTrue(page.locator("#settings-default-effort").is_disabled())
        page.locator("#settings-default-save").click()
        page.get_by_text("全局默认已保存，下一次任务和继续对话将使用此设置。", exact=True).wait_for()
        posts = page.evaluate("window.requests.filter(row => row.path === '/api/ai/preferences' && row.method === 'POST')")
        self.assertEqual(posts[0]["body"], {"model": MODEL, "reasoning_effort": None})

    def test_common_key_save_and_delete_refresh_api_state_without_replacing_its_draft(self):
        for remove in (False, True):
            with self.subTest(remove=remove):
                page = self.open_settings({"/api/keys#POST": {}, "/api/keys/remove": {}})
                page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
                page.locator("#settings-antigravity-url").fill("http://127.0.0.1:9000/v1")
                page.locator("#settings-antigravity-model").fill("unsaved-model")
                page.locator("#settings-antigravity-vision").check()
                page.locator("#settings-antigravity-key").fill("sk-unsaved-api-draft")
                updated = {**CONFIG, "key_set": not remove, "key_tail": "" if remove else "WXYZ",
                           "status": {"available": not remove, "label": "尚未设置 API 密钥" if remove else "API 密钥已更新"}}
                page.evaluate("""data => {
                  window.responses['/api/ai/antigravity'] = data.config;
                  window.responses['/api/keys'] = data.keys;
                  window.responses['/api/ai/options?refresh=1'] = {models:[{...data.model, available:!data.remove}],
                    default_settings:{model:data.model.id,reasoning_effort:null},defaults_configured:true};
                  window.responses['/api/ai/status'] = {available:!data.remove,providers:{
                    codex:{available:false},claude:{available:false},antigravity:data.config.status}};
                }""", {"config": updated, "keys": key_catalog(configured=not remove, tail="WXYZ"),
                       "model": API_MODEL, "remove": remove})
                common = page.locator('[data-key="ANTIGRAVITY_API_KEY"]')
                if remove:
                    page.once("dialog", lambda dialog: dialog.accept())
                    common.locator("[data-key-remove]").click()
                else:
                    common.locator("input").fill("sk-new-common-key")
                    common.get_by_role("button", name="保存", exact=True).click()
                page.get_by_text(updated["status"]["label"], exact=True).wait_for()
                self.assertEqual(page.locator("#settings-antigravity-url").input_value(), "http://127.0.0.1:9000/v1")
                self.assertEqual(page.locator("#settings-antigravity-model").input_value(), "unsaved-model")
                self.assertTrue(page.locator("#settings-antigravity-vision").is_checked())
                self.assertEqual(page.locator("#settings-antigravity-key").input_value(), "sk-unsaved-api-draft")
                self.assertIn("尚未设置密钥" if remove else "····WXYZ", page.locator("#settings-antigravity-key-state").inner_text())
                page.wait_for_function("window.calls.includes('/api/ai/options?refresh=1')")
                page.wait_for_function(f"expected => document.querySelector('#settings-default-model option[value=\"{MODEL}\"]')?.disabled === expected", arg=remove)
                option = page.locator(f'#settings-default-model option[value="{MODEL}"]')
                self.assertEqual(option.evaluate("element => element.disabled"), remove)
                self.assertEqual(page.locator('[data-settings-model-provider="antigravity"]').get_attribute("aria-pressed"), "true")
                if remove:
                    self.assertTrue(page.locator("#settings-default-save").is_disabled())
                    self.assertIn("未就绪", option.inner_text())
                self.assertEqual(page.evaluate("window.storageWrites"), [])
                page.close()

    def test_api_save_updates_common_key_state_without_clearing_other_key_drafts(self):
        updated = {**CONFIG, "key_tail": "WXYZ"}
        page = self.open_settings({"/api/ai/antigravity#POST": updated})
        page.get_by_text("Antigravity API 已配置", exact=True).wait_for()
        page.locator('[data-key="APIFY_TOKEN"] input').fill("apify_api_unsaved-draft")
        page.locator('[data-key="ANTIGRAVITY_API_KEY"] input').fill("sk-unsaved-common-draft")
        page.evaluate("data => window.responses['/api/keys'] = data", key_catalog(tail="WXYZ"))
        page.locator("#settings-antigravity-key").fill("sk-new-api-key")
        page.locator("#settings-antigravity-save").click()
        page.get_by_text("已设置 ····WXYZ", exact=True).wait_for()
        self.assertEqual(page.locator('[data-key="APIFY_TOKEN"] input').input_value(), "apify_api_unsaved-draft")
        self.assertEqual(page.locator('[data-key="ANTIGRAVITY_API_KEY"] input').input_value(), "sk-unsaved-common-draft")
        self.assertEqual(page.locator("#settings-antigravity-key").input_value(), "")
        self.assertIn("····WXYZ", page.locator("#settings-antigravity-key-state").inner_text())

    def test_remote_page_never_requests_local_service_configuration(self):
        page = self.open_settings(origin="https://workbench.example.com")
        page.get_by_text("Antigravity 服务在运行工作台的电脑上设置。", exact=True).wait_for()
        self.assertEqual(page.locator("#settings-antigravity-form").count(), 0)
        self.assertFalse(any(path.startswith("/api/ai/antigravity") for path in page.evaluate("window.calls")))


class AntigravityWritingBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing_fixture.AIDefaultsBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing_fixture.AIDefaultsBrowserTests.tearDownClass.__func__)
    setUp = writing_fixture.AIDefaultsBrowserTests.setUp
    stop_server = writing_fixture.AIDefaultsBrowserTests.stop_server
    request = writing_fixture.AIDefaultsBrowserTests.request
    open_page = writing_fixture.AIDefaultsBrowserTests.open_page

    def test_writing_selects_api_model_without_inventing_reasoning_effort(self):
        page = self.open_page()
        self.options["models"].append(copy.deepcopy(API_MODEL))
        page.evaluate("CaseAI.refreshAvailability({available:true,providers:{antigravity:{available:true}}})")
        page.evaluate("async () => { await CaseAI.startBlank('202H'); const fold = document.querySelector('.ai-create-model-fold'); if (fold) fold.open = true; }")
        page.locator('[data-create-provider="antigravity"]').click()
        page.locator('#ai-create-model option[value="' + MODEL + '"]').wait_for(state="attached")
        page.locator("#ai-create-model").select_option(MODEL)
        self.assertTrue(page.locator("#ai-create-effort").is_disabled())
        self.assertEqual(page.locator("#ai-create-effort").input_value(), "")
        page.locator("#ai-create-instruction").fill("用这个 API 写稿。")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-rounds").wait_for()
        self.assertEqual(self.posts[0]["model"], MODEL)
        self.assertIsNone(self.posts[0]["reasoning_effort"])

    def test_saved_api_usage_and_model_are_visible_in_round(self):
        page = self.open_page(continuation=True)
        self.options["models"].append(copy.deepcopy(API_MODEL))
        self.task["rounds"][0].update(model=MODEL, requested_model=MODEL,
                                       usage={"input_tokens": 1200, "output_tokens": 90, "total_tokens": 1290})
        page.reload()
        page.locator("#ai-rounds").wait_for()
        page.get_by_text("Token 用量：输入 1,200 / 输出 90 / 合计 1,290", exact=False).wait_for()
        self.assertIn("Antigravity", page.locator("#ai-rounds").inner_text())


if __name__ == "__main__":
    unittest.main()
