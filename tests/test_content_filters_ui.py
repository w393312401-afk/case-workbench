"""Custom content filters and saved views against disposable stores and the real API."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from playwright.sync_api import expect
import test_content_flow_ui as flow_fixture


class ContentFiltersBrowserTests(unittest.TestCase):
    setUpClass = classmethod(flow_fixture.ContentFlowBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(flow_fixture.ContentFlowBrowserTests.tearDownClass.__func__)
    setUp = flow_fixture.ContentFlowBrowserTests.setUp
    stop_server = flow_fixture.ContentFlowBrowserTests.stop_server
    request = flow_fixture.ContentFlowBrowserTests.request
    open_page = flow_fixture.ContentFlowBrowserTests.open_page
    script = flow_fixture.ContentFlowBrowserTests.script
    finished_job = flow_fixture.ContentFlowBrowserTests.finished_job
    flow = flow_fixture.ContentFlowBrowserTests.flow
    manage = flow_fixture.ContentFlowBrowserTests.manage
    content_page = flow_fixture.ContentFlowBrowserTests.content_page
    view = flow_fixture.ContentFlowBrowserTests.view
    screenshot = flow_fixture.ContentFlowBrowserTests.screenshot

    def records(self):
        script = self.script("仍待制作的采用稿", text="A waiting script.")
        archived = self.script("归档采用稿", text="An archived script.")
        self.manage("script:" + archived["family_id"], {"archived": True})
        approved = self.finished_job("已经完成的视频")
        draft_script = self.script("待制作的视频", text="An unfinished video.")
        draft = self.server.app.create_production_job({
            "request_id": uuid4().hex, "revision": self.server.app.production.public()["revision"],
            "job": {"script_id": draft_script["id"]}})["job"]
        return {"script": "script:" + script["family_id"], "archived": "script:" + archived["family_id"],
                "approved": "video:" + approved["id"], "draft": "video:" + draft["id"]}

    def assert_rows(self, page, identifiers):
        identifiers = list(identifiers)
        rows = page.locator("#content-flow-list .flow-row")
        expect(rows).to_have_count(len(identifiers))
        for identifier in identifiers:
            expect(page.locator(f'#content-flow-list [data-content-id="{identifier}"]')).to_have_count(1)
        self.assertEqual(set(rows.evaluate_all("rows => rows.map(row => row.dataset.contentId)")), set(identifiers))

    def editor(self, page):
        editor = page.locator("#content-flow-filter-editor")
        if not editor.is_visible():
            page.locator("#content-flow-filter-toggle").click()
        expect(editor).to_be_visible()
        return editor

    def open_values(self, row):
        if not row.locator("details").evaluate("node => node.open"):
            row.locator("[data-filter-values]").click()

    def set_filters(self, page, conditions, match="all"):
        editor = self.editor(page)
        while editor.locator("[data-filter-remove]").count():
            editor.locator("[data-filter-remove]").last.click()
        editor.locator("#content-filter-match").select_option(match)
        for field, values in conditions:
            editor.locator("[data-filter-add]").click()
            row = editor.locator("[data-filter-row]").last
            row.locator("[data-filter-field]").select_option(field)
            self.open_values(row)
            for value in values:
                row.locator(f'[data-filter-value="{value}"]').check()
            row.locator("[data-filter-values]").click()
        editor.locator("[data-filter-apply]").click()
        expect(editor).to_be_hidden()

    def dialog(self, page):
        page.locator("#content-flow-save-view").click()
        dialog = page.locator(".flow-view-dialog")
        expect(dialog).to_be_visible()
        return dialog

    def params(self, page):
        return parse_qs(page.evaluate("location.hash").partition("?")[2])

    def test_multiselect_and_all_any_match_real_server_results(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.assert_rows(page, rows.values())
        self.set_filters(page, [("kind", ["video", "script"]), ("status", ["approved", "archived"])])
        self.assert_rows(page, [rows["approved"], rows["archived"]])
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("视频")
        expect(page.locator("#content-flow-filter-summary")).to_contain_text("已归档")
        expect(page.locator("#content-flow-result-count")).to_contain_text("2")
        params = self.params(page)
        self.assertIn("filter", params)
        spec = json.loads(params["filter"][0])
        self.assertEqual(spec["match"], "all")
        api = self.flow(urlencode({"product": "202H", "view": "all", "filter": params["filter"][0]}))
        self.assertEqual({row["id"] for row in api["items"]}, {rows["approved"], rows["archived"]})

        editor = self.editor(page)
        editor.locator("#content-filter-match").select_option("any")
        editor.locator("[data-filter-apply]").click()
        self.assert_rows(page, rows.values())
        self.assertEqual(json.loads(self.params(page)["filter"][0])["match"], "any")
        page.reload()
        self.assert_rows(page, rows.values())
        editor = self.editor(page)
        expect(editor.locator("#content-filter-match")).to_have_value("any")
        expect(editor.locator('[data-filter-value="video"]')).to_be_checked()
        expect(editor.locator('[data-filter-value="script"]')).to_be_checked()
        expect(editor.locator('[data-filter-value="approved"]')).to_be_checked()
        expect(editor.locator('[data-filter-value="archived"]')).to_be_checked()

    def test_saved_views_survive_reload_switch_update_delete_and_stay_in_their_product(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        self.set_filters(page, [("kind", ["video"]), ("status", ["approved"])])
        dialog = self.dialog(page)
        dialog.locator('[name="name"]').fill("已完成的视频")
        dialog.locator("[data-view-save]").click()
        expect(dialog).not_to_be_visible()
        saved = page.locator("[data-flow-saved-view]").filter(has_text="已完成的视频")
        expect(saved).to_have_count(1)
        self.assert_rows(page, [rows["approved"]])
        page.reload()
        expect(saved).to_have_attribute("aria-pressed", "true")
        self.assert_rows(page, [rows["approved"]])

        self.view(page, "all")
        self.assert_rows(page, rows.values())
        saved.click()
        self.assert_rows(page, [rows["approved"]])
        self.set_filters(page, [("kind", ["video"])])
        self.assert_rows(page, [rows["approved"], rows["draft"]])
        dialog = self.dialog(page)
        dialog.locator('[name="name"]').fill("全部视频")
        dialog.locator("[data-view-update]").click()
        expect(dialog).not_to_be_visible()
        saved = page.locator("[data-flow-saved-view]").filter(has_text="全部视频")
        expect(page.locator("[data-flow-saved-view]")).to_have_count(1)
        page.reload()
        expect(saved).to_have_attribute("aria-pressed", "true")
        self.assert_rows(page, [rows["approved"], rows["draft"]])

        page.evaluate("location.hash = '#/129N/content'")
        expect(page.locator("#content-flow-list")).to_be_visible()
        expect(page.locator("[data-flow-saved-view]")).to_have_count(0)
        page.evaluate("location.hash = '#/202H/content'")
        expect(saved).to_have_count(1)
        saved.click()
        self.assert_rows(page, [rows["approved"], rows["draft"]])
        dialog = self.dialog(page)
        dialog.locator("[data-view-delete]").click()
        expect(dialog).not_to_be_visible()
        expect(page.locator("[data-flow-saved-view]")).to_have_count(0)
        page.reload()
        expect(page.locator("#content-flow-list")).to_be_visible()
        expect(page.locator("[data-flow-saved-view]")).to_have_count(0)

    def test_drafts_need_values_cancel_safely_and_presets_clear_custom_conditions_and_search(self):
        rows = self.records()
        page = self.content_page()
        self.view(page, "all")
        original_hash = page.evaluate("location.hash")
        editor = self.editor(page)
        editor.locator("[data-filter-add]").click()
        expect(editor.locator("[data-filter-apply]")).to_be_disabled()
        row = editor.locator("[data-filter-row]")
        self.open_values(row)
        row.locator('[data-filter-value="video"]').check()
        expect(editor.locator("[data-filter-apply]")).to_be_enabled()
        row.locator('[data-filter-value="video"]').uncheck()
        expect(editor.locator("[data-filter-apply]")).to_be_disabled()
        editor.locator("[data-filter-cancel]").click()
        self.assertEqual(page.evaluate("location.hash"), original_hash)
        self.assert_rows(page, rows.values())

        self.set_filters(page, [("kind", ["script"]), ("status", ["archived"])])
        page.locator("#content-flow-query").fill("归档采用稿")
        self.assert_rows(page, [rows["archived"]])
        self.view(page, "publish")
        expect(page.locator("#content-flow-query")).to_have_value("")
        self.assert_rows(page, [rows["approved"]])
        self.assertNotIn("filter", self.params(page))
        self.assertNotIn("q", self.params(page))
        editor = self.editor(page)
        # A preset may explain its own rule, but must not retain the old kind or archive rule.
        expect(editor.locator('[data-filter-value="script"]:checked')).to_have_count(0)
        expect(editor.locator('[data-filter-value="archived"]:checked')).to_have_count(0)
        editor.locator("[data-filter-cancel]").click()
        self.view(page, "all")
        self.assert_rows(page, rows.values())

    def test_leaving_content_closes_unsaved_view_dialog_without_creating_a_view(self):
        self.script("检查返回时清理视图对话框")
        page = self.open_page("#/202H/library")
        page.evaluate("location.hash = '#/202H/content'")
        expect(page.locator("#content-flow-list .flow-row")).to_have_count(1)
        dialog = self.dialog(page)
        dialog.locator('[name="name"]').fill("尚未保存的视图")

        page.go_back()
        page.wait_for_url("**/#/202H/library")
        expect(page.locator("#content-flow-list")).to_have_count(0)
        expect(page.locator(".flow-view-dialog")).to_have_count(0)

        page.go_forward()
        expect(page.locator("#content-flow-list .flow-row")).to_have_count(1)
        expect(page.locator("[data-flow-saved-view]")).to_have_count(0)
        dialog = self.dialog(page)
        expect(dialog.locator('[name="name"]')).to_have_value("")
        dialog.locator('[name="name"]').fill("正常保存的视图")
        dialog.locator("[data-view-save]").click()
        expect(page.locator("[data-flow-saved-view]")).to_have_text("正常保存的视图")

    def test_detail_back_keeps_filter_and_query_and_editor_fits_light_dark_mobile(self):
        item = self.script("筛选后打开详情")
        self.script("另一个采用稿", text="Another copy.")
        page = self.content_page()
        self.view(page, "all")
        self.set_filters(page, [("kind", ["script"])])
        page.locator("#content-flow-query").fill("筛选后")
        self.assert_rows(page, ["script:" + item["family_id"]])
        before = page.evaluate("location.hash")
        page.locator(".flow-row .flow-title").click()
        page.locator("#script-production-toggle").wait_for()
        expect(page.locator("#navigate-up")).to_have_attribute("href", before)
        page.locator("#navigate-up").click()
        self.assert_rows(page, ["script:" + item["family_id"]])
        self.assertEqual(page.evaluate("location.hash"), before)
        expect(page.locator("#content-flow-query")).to_have_value("筛选后")

        editor = self.editor(page)
        editor.locator("[data-filter-add]").click()
        row = editor.locator("[data-filter-row]").last
        row.locator("[data-filter-field]").select_option("status")
        self.open_values(row)
        for scheme, width in (("light", 1280), ("dark", 1280), ("light", 390), ("dark", 390)):
            page.set_viewport_size({"width": width, "height": 900})
            page.emulate_media(color_scheme=scheme)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
            self.screenshot(page, f"content-custom-filters-{scheme}-{width}.png")
        row.locator('[data-filter-value="archived"]').check()
        editor.locator("[data-filter-apply]").click()
        expect(page.locator("#content-flow-list .empty")).to_be_visible()
        expect(page.locator("#content-flow-result-count")).to_contain_text("0")


if __name__ == "__main__":
    unittest.main()
