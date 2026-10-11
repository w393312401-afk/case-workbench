"""Claude/Codex still-search controls and receipts, using only temporary fake clients."""
from __future__ import annotations

import unittest

from playwright.sync_api import expect

import test_production_carousel_ui as carousel_fixture


class CarouselStillClientBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def test_failed_search_gaps_are_unverified_rather_than_missing_footage(self):
        self.app.ai.save_preferences({"model": None, "reasoning_effort": None})
        output = ('工具启动失败（code-mode host is disabled），画面尚未核验。\n\n```json\n'
                  '{"slides":{"1":0},"gaps":[{"slide":1,"need":"待核验完整开箱画面"}]}\n```')
        self.codex.run = lambda *args, **options: output
        page = self.open_page(f"#production/{self.carousel['id']}")
        form = page.locator("#still-form")
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('button[type="submit"]').click()
        card = page.locator("#search-runs .edit-card.failed")
        card.wait_for(state="attached", timeout=15000)
        expect(card.get_by_role("heading", name="尚未完成核验", exact=True)).to_be_visible()
        expect(card.get_by_role("heading", name="没找到合格画面", exact=True)).to_have_count(0)
        expect(card.locator(".edit-gaps")).to_contain_text("第 1 张：待核验完整开箱画面")
        expect(card.locator(".ai-error")).to_contain_text("未返回有效的候选清单")
        card.locator("summary").click()
        expect(card.locator(".edit-output")).to_contain_text("code-mode host is disabled")
        search = self.app.production.carousel(self.carousel["id"])["searches"][-1]
        self.assertEqual((search["status"], search["added"], search["output"]), ("failed", 0, output))

    def test_codex_model_uses_its_own_availability_and_shows_session_receipt(self):
        self.searcher.status = lambda: {"available": False, "error": "隔离测试：Claude 未连接"}
        self.codex.wait_after_export = True
        self.codex.actual_model, self.codex.actual_effort = "gpt-6.1-sol", "high"
        self.app.ai.save_preferences({"model": "gpt-6.1-sol", "reasoning_effort": "high"})
        self.addCleanup(self.codex.release.set)
        source_bytes = self.source.read_bytes()
        script = self.app.creation.get_script(self.carousel["script_id"])
        page = self.open_page(f"#production/{self.carousel['id']}")
        form, status = page.locator("#still-form"), page.locator("[data-still-provider-status]")
        expect(status).to_contain_text("Codex 客户端可用于找底图")
        expect(form.locator('select[name="model"]')).to_be_hidden()
        expect(form.locator('[data-still-default]')).to_contain_text("使用全局默认：Codex 当前模型 · high")
        expect(form.locator("[data-still-usage]")).to_contain_text("当前登录账户的额度")
        self.assertNotIn("Claude 账户", form.locator("[data-still-usage]").inner_text())
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/search")) as submitted:
            form.locator('button[type="submit"]').click()
        self.assertNotIn("model", submitted.value.post_data_json)
        self.assertNotIn("effort", submitted.value.post_data_json)
        self.assertTrue(self.codex.entered.wait(3))
        running = page.locator("#search-runs .edit-card.running small")
        expect(running).to_contain_text("Codex 客户端")
        expect(running).to_contain_text("实际模型：Codex 当前模型")
        expect(running).to_contain_text("实际思考强度：high")
        self.assertNotIn("实际型号尚未回报", running.inner_text())
        self.assertNotIn("Claude 订阅", running.inner_text())
        self.codex.release.set()
        page.locator("#search-runs .script-status").get_by_text("已完成", exact=True).wait_for(state="attached", timeout=15000)
        search = self.app.production.carousel(self.carousel["id"])["searches"][-1]
        self.assertEqual((search["provider"], search["requested_model"], search["effective_model"], search["effective_effort"]),
                         ("codex", "gpt-6.1-sol", "gpt-6.1-sol", "high"))
        self.assertEqual((self.codex.calls[0]["model"], self.codex.calls[0]["reasoning_effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.source.read_bytes(), source_bytes)
        self.assertEqual(self.app.creation.get_script(script["id"]), script)

    def test_codex_default_survives_missing_catalog_without_claiming_actual_model(self):
        self.app.ai.save_preferences({"model": None, "reasoning_effort": None})
        self.catalog.error = "隔离测试：模型列表暂时不可用"

        def run_without_receipt(prompt, **options):
            options.pop("on_event", None)
            return carousel_fixture.Searcher.run(self.codex, prompt, **options)

        self.codex.run = run_without_receipt
        page = self.open_page(f"#production/{self.carousel['id']}")
        form = page.locator("#still-form")
        expect(form.locator('select[name="model"]')).to_be_hidden()
        expect(form.locator("[data-still-provider-status]")).to_contain_text("模型列表暂时不可用")
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('button[type="submit"]').click()
        page.locator("#search-runs .script-status").get_by_text("已完成", exact=True).wait_for(state="attached", timeout=15000)
        page.locator('[data-step="pick"]').click()
        record = page.locator("#search-runs .edit-card small")
        expect(record).to_contain_text("申请模型：Codex 客户端默认 · 实际型号尚未回报")
        self.assertNotIn("实际模型：", record.inner_text())
        search = self.app.production.carousel(self.carousel["id"])["searches"][-1]
        self.assertEqual(search["requested_model"], "codex-default")
        self.assertIsNone(search["effective_model"])
        self.assertIsNone(self.codex.calls[0]["model"])
        self.assertEqual(self.searcher.calls, [])

    def test_unavailable_codex_does_not_block_connected_claude(self):
        self.codex.available = False
        before = self.app.production.public()["revision"]
        page = self.open_page(f"#production/{self.carousel['id']}")
        form, status = page.locator("#still-form"), page.locator("[data-still-provider-status]")
        expect(status).to_contain_text("Claude Code 客户端可用于找底图")
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        expect(form.locator('select[name="model"]')).to_be_hidden()
        self.app.ai.save_preferences({"model": None, "reasoning_effort": None})
        page.reload()
        expect(status).to_contain_text("Codex 客户端暂不可用")
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        expect(form.locator('select[name="model"]')).to_be_hidden()
        self.app.ai.save_preferences({"model": "claude-opus", "reasoning_effort": None})
        page.reload()
        expect(status).to_contain_text("Claude Code 客户端可用于找底图")
        expect(form.locator("[data-still-usage]")).to_contain_text("Claude 账户的订阅额度")
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        self.assertEqual(self.app.production.public()["revision"], before)
        self.assertEqual(self.app.production.carousel(self.carousel["id"])["searches"], [])
        self.assertEqual((self.searcher.calls, self.codex.calls), ([], []))

    def test_incompatible_default_requires_explicit_specialized_choice(self):
        self.app.ai.save_preferences({"model": "claude-haiku", "reasoning_effort": None})
        page = self.open_page(f"#production/{self.carousel['id']}")
        form = page.locator("#still-form")
        expect(form.locator('[data-still-override]')).to_be_visible()
        expect(form.locator('select[name="model"]')).to_have_value("")
        expect(form.locator('option[value="claude-haiku"]')).to_have_count(0)
        expect(form.locator('button[type="submit"]')).to_be_disabled()
        self.assertEqual((self.searcher.calls, self.codex.calls), ([], []))
        form.locator('select[name="model"]').select_option("codex-default")
        expect(form.locator('button[type="submit"]')).to_be_enabled()
        form.locator('button[type="submit"]').click()
        page.locator("#search-runs .script-status").get_by_text("已完成", exact=True).wait_for(state="attached", timeout=15000)
        search = self.app.production.carousel(self.carousel["id"])["searches"][-1]
        self.assertEqual((search["model"], search["effort"]), ("codex-default", None))
        self.assertEqual(self.app.ai.preferences()["model"], "claude-haiku")


if __name__ == "__main__":
    unittest.main()
