"""Saved view persistence through isolated browsers and the real workbench API."""
from __future__ import annotations

import json
import unittest
from uuid import uuid4

from playwright.sync_api import expect
import test_content_filters_ui as filter_fixture


LEGACY_KEY = "case-content-views-v1:202H"


class ContentSavedViewsBrowserTests(unittest.TestCase):
    # Reuse fixture helpers without inheriting or rediscovering their test cases.
    setUpClass = classmethod(filter_fixture.ContentFiltersBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(filter_fixture.ContentFiltersBrowserTests.tearDownClass.__func__)
    setUp = filter_fixture.ContentFiltersBrowserTests.setUp
    stop_server = filter_fixture.ContentFiltersBrowserTests.stop_server
    request = filter_fixture.ContentFiltersBrowserTests.request
    open_page = filter_fixture.ContentFiltersBrowserTests.open_page
    script = filter_fixture.ContentFiltersBrowserTests.script
    finished_job = filter_fixture.ContentFiltersBrowserTests.finished_job
    flow = filter_fixture.ContentFiltersBrowserTests.flow
    manage = filter_fixture.ContentFiltersBrowserTests.manage
    content_page = filter_fixture.ContentFiltersBrowserTests.content_page
    view = filter_fixture.ContentFiltersBrowserTests.view
    records = filter_fixture.ContentFiltersBrowserTests.records
    assert_rows = filter_fixture.ContentFiltersBrowserTests.assert_rows
    editor = filter_fixture.ContentFiltersBrowserTests.editor
    open_values = filter_fixture.ContentFiltersBrowserTests.open_values
    set_filters = filter_fixture.ContentFiltersBrowserTests.set_filters
    dialog = filter_fixture.ContentFiltersBrowserTests.dialog
    params = filter_fixture.ContentFiltersBrowserTests.params

    def stored_views(self, product="202H"):
        status, _, result = self.request("GET", "/api/content/views?product=" + product)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["product"], product)
        return result

    def refresh(self, page):
        with page.expect_response(lambda response: "/api/content/views?" in response.url
                                  and response.request.method == "GET"):
            page.locator("#content-flow-refresh").click()

    def save(self, page, name, update=False):
        dialog = self.dialog(page)
        dialog.locator('[name="name"]').fill(name)
        dialog.locator("[data-view-update]" if update else "[data-view-save]").click()
        expect(dialog).not_to_be_visible()
        button = page.locator("[data-flow-saved-view]").filter(has_text=name)
        expect(button).to_have_count(1)
        return button

    def legacy_view(self):
        return {"id": str(uuid4()), "name": "旧浏览器里的视图", "q": "旧稿",
                "filter": {"match": "any", "conditions": [
                    {"field": "kind", "values": ["script", "video"]},
                    {"field": "status", "values": ["awaiting_review", "needs_attention"]}]}}

    def seed_legacy(self, page, record):
        page.evaluate("([key, record]) => localStorage.setItem(key, JSON.stringify([record]))",
                      [LEGACY_KEY, record])

    def test_other_browser_uses_updated_and_deleted_views_and_products_stay_separate(self):
        rows = self.records()
        first, second = self.content_page(), self.content_page()
        self.assertIsNot(first.context, second.context)
        first.evaluate("localStorage.setItem('test-browser-only', 'first')")
        self.assertIsNone(second.evaluate("localStorage.getItem('test-browser-only')"))

        self.view(first, "all")
        self.set_filters(first, [("kind", ["video"]), ("status", ["approved"])])
        self.save(first, "共享的待发布视频")
        identifier = self.stored_views()["views"][0]["id"]
        self.assertIsNone(first.evaluate("key => localStorage.getItem(key)", LEGACY_KEY))

        self.refresh(second)
        remote = second.locator(f'[data-flow-saved-view="{identifier}"]')
        expect(remote).to_have_text("共享的待发布视频")
        remote.click()
        self.assert_rows(second, [rows["approved"]])

        self.set_filters(first, [("kind", ["video"])])
        self.save(first, "共享的全部视频", update=True)
        self.refresh(second)
        expect(remote).to_have_attribute("title", "共享的全部视频")
        remote.click()
        self.assert_rows(second, [rows["approved"], rows["draft"]])

        second.evaluate("location.hash = '#/129N/content'")
        expect(second.locator("#content-flow-list")).to_be_visible()
        expect(second.locator("[data-flow-saved-view]")).to_have_count(0)
        self.save(second, "另一个产品的视图")
        self.assertEqual([row["name"] for row in self.stored_views("129N")["views"]], ["另一个产品的视图"])
        self.assertEqual([row["name"] for row in self.stored_views()["views"]], ["共享的全部视频"])

        second.evaluate("location.hash = '#/202H/content'")
        expect(remote).to_have_attribute("title", "共享的全部视频")
        dialog = self.dialog(first)
        dialog.locator("[data-view-delete]").click()
        expect(dialog).not_to_be_visible()
        self.refresh(second)
        expect(second.locator("[data-flow-saved-view]")).to_have_count(0)
        self.assertEqual(self.stored_views()["views"], [])
        self.assertEqual(len(self.stored_views("129N")["views"]), 1)

    def test_legacy_views_migrate_once_and_are_visible_in_an_independent_browser(self):
        record = self.legacy_view()
        first = self.open_page("#/202H/library")
        self.seed_legacy(first, record)
        first.evaluate("location.hash = '#/202H/content'")
        expect(first.locator("[data-flow-saved-view]")).to_have_text(record["name"])
        first.wait_for_function("key => localStorage.getItem(key) === null", arg=LEGACY_KEY)
        stored = self.stored_views()["views"]
        self.assertEqual(len(stored), 1)
        for key, value in record.items():
            self.assertEqual(stored[0][key], value)
        first.reload()
        expect(first.locator("[data-flow-saved-view]")).to_have_count(1)

        # A second browser carrying the same old record cannot duplicate the migrated view.
        second = self.open_page("#/202H/library")
        self.seed_legacy(second, record)
        second.evaluate("location.hash = '#/202H/content'")
        expect(second.locator("[data-flow-saved-view]")).to_have_text(record["name"])
        second.wait_for_function("key => localStorage.getItem(key) === null", arg=LEGACY_KEY)
        self.assertEqual(len(self.stored_views()["views"]), 1)
        second.locator("[data-flow-saved-view]").click()
        expect(second.locator("#content-flow-query")).to_have_value(record["q"])
        second.wait_for_function("id => new URLSearchParams(location.hash.split('?')[1]).get('saved') === id",
                                 arg=record["id"])
        self.assertEqual(json.loads(self.params(second)["filter"][0]), record["filter"])

    def test_failed_legacy_import_keeps_local_copy_and_explicit_refresh_retries(self):
        page = self.open_page("#/202H/library")
        record = self.legacy_view()
        self.seed_legacy(page, record)
        failures = []

        def reject_import(route):
            if route.request.method == "POST" and route.request.post_data_json.get("action") == "import":
                failures.append(route.request.post_data_json)
                return route.fulfill(status=503, json={"error": "隔离测试：旧视图暂未同步"})
            return route.continue_()

        page.route("**/api/content/views*", reject_import)
        with page.expect_response(lambda response: response.status == 503 and "/api/content/views" in response.url):
            page.evaluate("location.hash = '#/202H/content'")
        self.assertTrue(failures)
        expect(page.locator("#content-flow-views-error")).to_contain_text("旧视图暂未同步")
        self.assertEqual(json.loads(page.evaluate("key => localStorage.getItem(key)", LEGACY_KEY)), [record])
        self.assertEqual(self.stored_views()["views"], [])
        page.unroute("**/api/content/views*", reject_import)
        self.refresh(page)
        page.wait_for_function("key => localStorage.getItem(key) === null", arg=LEGACY_KEY)
        expect(page.locator("[data-flow-saved-view]")).to_have_text(record["name"])
        expect(page.locator("#content-flow-views-error")).to_be_hidden()
        self.assertEqual(len(self.stored_views()["views"]), 1)

    def test_failed_save_keeps_name_and_conditions_for_retry(self):
        page = self.content_page()
        self.view(page, "all")
        self.set_filters(page, [("kind", ["video", "script"])], match="any")
        calls = []

        def fail_first_save(route):
            if route.request.method != "POST" or route.request.post_data_json.get("action") != "save":
                return route.continue_()
            calls.append(route.request.post_data_json)
            if len(calls) == 1:
                return route.fulfill(status=503, json={"error": "隔离测试：保存暂时失败，请重试"})
            return route.continue_()

        page.route("**/api/content/views*", fail_first_save)
        dialog = self.dialog(page)
        dialog.locator('[name="name"]').fill("失败后保留我的输入")
        dialog.locator("[data-view-save]").click()
        expect(dialog.locator("[data-view-error]")).to_contain_text("保存暂时失败")
        expect(dialog).to_be_visible()
        expect(dialog.locator('[name="name"]')).to_have_value("失败后保留我的输入")
        expect(dialog.locator("[data-view-save]")).to_be_enabled()
        self.assertEqual(self.stored_views()["views"], [])
        dialog.locator("[data-view-save]").click()
        expect(dialog).not_to_be_visible()
        self.assertEqual(len(calls), 2)
        result = self.stored_views()["views"]
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "失败后保留我的输入")
        saved_filter = result[0]["filter"]
        self.assertEqual(saved_filter["match"], "any")
        self.assertEqual(len(saved_filter["conditions"]), 1)
        self.assertEqual(saved_filter["conditions"][0]["field"], "kind")
        self.assertEqual(set(saved_filter["conditions"][0]["values"]), {"video", "script"})

    def test_stale_dialog_cannot_overwrite_a_change_from_another_browser(self):
        first = self.content_page()
        self.view(first, "all")
        self.save(first, "两个设备共用的视图")
        identifier = self.stored_views()["views"][0]["id"]
        second = self.content_page()
        second.locator(f'[data-flow-saved-view="{identifier}"]').click()
        stale_dialog = self.dialog(second)
        stale_dialog.locator('[name="name"]').fill("设备二尚未提交的名称")

        self.save(first, "设备一已保存的名称", update=True)
        with second.expect_response(lambda response: "/api/content/views" in response.url
                                    and response.request.method == "POST") as response:
            stale_dialog.locator("[data-view-update]").click()
        self.assertEqual(response.value.status, 409)
        expect(stale_dialog.locator("[data-view-error]")).to_be_visible()
        expect(stale_dialog.locator('[name="name"]')).to_have_value("设备二尚未提交的名称")
        self.assertEqual([row["name"] for row in self.stored_views()["views"]], ["设备一已保存的名称"])
        stale_dialog.locator("[data-close]").last.click()
        self.refresh(second)
        expect(second.locator(f'[data-flow-saved-view="{identifier}"]')).to_have_text("设备一已保存的名称")


if __name__ == "__main__":
    unittest.main()
