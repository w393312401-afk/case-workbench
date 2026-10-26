"""Cross-page dropdown interactions against a disposable workbench library."""
from __future__ import annotations

import copy
import json
import re
import unittest

from playwright.sync_api import expect

import test_performance as performance
import test_server as fixture
import test_ui_regressions as browser
from test_review_store import build_sources


class UnifiedDropdownPagesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["content_origin"] = "overseas_creator"
        self.store.append(self.identifier, {"kind": "editorial", "text": "隔离筛选样本", "current": current})
        current.pop("source_event_ids", None)
        current.update(title="自己的第二条样本", content_origin="self_produced")
        current["metrics"]["video_id"] = "7000000000000000001"
        self.own = self.store.add({"title": current["title"], "product": "SHARPAL 202H",
                                   "original": {"text": "Disposable dropdown fixture"},
                                   "feedback": {"kind": "editorial", "text": "隔离样本"},
                                   "current": current})["case_id"]
        build_sources(self.runtime / "reviews" / "sources")
        performance.build_exports(self.root / "exports")
        self.server.app.performance.sources = [self.root / "exports"]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        self.settings_posts = []
        self.defaults = {"model": "fixture-model", "reasoning_effort": "low"}

    def open_page(self, route="#/202H/library", *, width=1280, theme="light"):
        context = self.browser.new_context(viewport={"width": width, "height": 900}, color_scheme=theme)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def options(route):
            route.fulfill(json={"models": [{"id": "fixture-model", "name": "隔离测试模型",
                                            "available": True, "reasoning_efforts": ["low", "high"]}],
                                "default_settings": self.defaults, "defaults_configured": True, "skills": []})

        def preferences(route):
            if route.request.method == "POST":
                self.settings_posts.append(route.request.post_data_json)
                self.defaults = {key: route.request.post_data_json.get(key) for key in ("model", "reasoning_effort")}
            route.fulfill(json={"preferences": self.defaults, "saved": route.request.method == "POST"})

        page.route("**/api/ai/options*", options)
        page.route("**/api/ai/preferences", preferences)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{route}")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        return page

    def control(self, page, selector):
        native = page.locator(selector)
        root = native.locator("xpath=following-sibling::*[1]")
        expect(root).to_have_class(re.compile(r"\bworkbench-select\b"))
        trigger = root.locator("button.product-picker-trigger[data-select-trigger]")
        expect(trigger).to_be_visible()
        return native, root, trigger

    def open_picker(self, page, selector):
        native, root, trigger = self.control(page, selector)
        trigger.click()
        panel = page.locator("[id=" + json.dumps(trigger.get_attribute("aria-controls")) + "]").locator("xpath=..")
        expect(panel).to_have_class(re.compile(r"\bworkbench-select-panel\b"))
        expect(panel).to_be_visible()
        expect(panel.locator("input[type=search]")).to_be_focused()
        self.assert_in_viewport(page, panel)
        return native, root, trigger, panel

    def choose(self, page, selector, value, *, query=None):
        _, _, _, panel = self.open_picker(page, selector)
        if query is not None:
            panel.locator("input[type=search]").fill(query)
        panel.locator("[data-picker-value=" + json.dumps(value, ensure_ascii=False) + "]").click()
        expect(page.locator(selector)).to_have_value(value)
        self.assertEqual(page.locator(".workbench-select-panel:visible").count(), 0)

    def assert_enhanced(self, page, scope="#content"):
        page.wait_for_function("""scope => [...document.querySelectorAll(scope + ' select:not([multiple])')]
            .every(select => select.nextElementSibling?.matches('.workbench-select, .product-picker'))""", arg=scope)
        self.assertEqual(page.locator(scope + ' select:not([multiple]):not([aria-hidden="true"])').count(), 0)

    def assert_in_viewport(self, page, node):
        bounds = node.bounding_box()
        self.assertIsNotNone(bounds)
        viewport = page.viewport_size
        self.assertGreaterEqual(bounds["x"], -1)
        self.assertGreaterEqual(bounds["y"], -1)
        self.assertLessEqual(bounds["x"] + bounds["width"], viewport["width"] + 1)
        self.assertLessEqual(bounds["y"] + bounds["height"], viewport["height"] + 1)

    def assert_no_overflow(self, page):
        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"))

    def test_library_filters_sort_and_saved_view_use_dropdowns_and_keep_their_effect(self):
        page = self.open_page()
        page.locator("#case-search").wait_for()
        browser.open_library_filters(page)
        self.assert_enhanced(page)
        self.choose(page, "#content-origin-filter", "overseas_creator", query="海外")
        expect(page.locator("[data-selection-card]")).to_have_count(1)
        expect(page.locator("[data-selection-card]")).to_have_attribute("data-selection-card", self.identifier)
        self.choose(page, "#sort-filter", "sold", query="售出")
        self.choose(page, "#sort-direction", "asc")
        self.assertEqual(page.evaluate("[state.contentOrigin, state.sort, state.sortDir]"), ["overseas_creator", "sold", "asc"])
        browser.close_library_filters(page)
        page.locator("#library-save-view-button").click()
        page.locator('#library-save-view [name="view_name"]').fill("隔离的达人筛选")
        page.locator("#library-save-view").get_by_role("button", name="保存视图", exact=True).click()
        page.wait_for_function("document.querySelector('#library-saved-view').value.startsWith('custom-')")
        saved_view = page.locator("#library-saved-view").input_value()
        page.locator("#library-clear-filters").click()
        expect(page.locator("[data-selection-card]")).to_have_count(2)
        self.choose(page, "#library-saved-view", saved_view, query="隔离")
        expect(page.locator("[data-selection-card]")).to_have_count(1)
        page.reload()
        page.locator("#case-search").wait_for()
        expect(page.locator("[data-selection-card]")).to_have_count(1)
        browser.open_library_filters(page)
        expect(page.locator("#sort-filter")).to_have_value("sold")
        expect(page.locator("#sort-direction")).to_have_value("asc")
        self.assert_enhanced(page)

    def test_review_drawer_and_video_account_filter_work_after_page_replacement(self):
        page = self.open_page("#/202H/reviews")
        page.locator(".rev-card").first.wait_for()
        self.choose(page, "#rev-sort", "newest", query="最新")
        page.locator("#rev-filter-open").click()
        expect(page.locator("#rev-drawer")).to_be_visible()
        self.assert_enhanced(page)
        self.choose(page, "#rev-replied", "yes", query="回复")
        page.locator("#rev-drawer-done").click()
        expect(page.locator("#rev-count")).to_contain_text("1 条")
        self.assertIn("replied=yes", page.evaluate("location.hash"))
        page.evaluate("location.hash = '#/202H/videos'")
        page.locator("#perf-account").wait_for(state="attached")
        page.locator("#perf-tab-list").click()
        self.assert_enhanced(page)
        previous_count = page.locator("[data-video-row]").count()
        self.choose(page, "#perf-account", "bushcraftedge", query="bush")
        expect(page.locator("#perf-reset")).to_be_visible()
        self.assertGreater(previous_count, page.locator("[data-video-row]").count())
        self.assertTrue(all("bushcraftedge" in text for text in page.locator("[data-video-row]").all_text_contents()))
        page.locator("#perf-reset").click()
        expect(page.locator("#perf-account")).to_have_value("")

    def test_editor_form_and_settings_dependent_effort_save_selected_values(self):
        page = self.open_page(f"#case/{self.identifier}")
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        self.assert_enhanced(page, "#edit-dialog")
        self.choose(page, '#edit-dialog [name="content_origin"]', "self_produced", query="自制")
        self.choose(page, '#edit-dialog [name="category"]', "待验证与整理", query="待验证")
        page.locator('#edit-dialog [name="note"]').fill("通过统一下拉修改隔离样本")
        page.locator("#save-button").click()
        expect(page.locator("#edit-dialog")).to_be_hidden()
        saved = self.store.read_case(self.identifier)["current"]
        self.assertEqual((saved["content_origin"], saved["category"]), ("self_produced", "待验证与整理"))
        page.evaluate("location.hash = '#/202H/settings'")
        expect(page.locator("#settings-default-model")).to_be_enabled()
        self.assert_enhanced(page)
        self.choose(page, "#settings-default-effort", "high", query="高")
        page.locator("#settings-default-save").click()
        expect(page.locator("#settings-default-status")).to_contain_text("全局默认已保存")
        self.assertEqual(self.settings_posts[-1], {"model": "fixture-model", "reasoning_effort": "high"})

    def test_search_panels_and_controls_fit_mobile_and_desktop_in_both_themes(self):
        for width in (320, 390, 1280):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page = self.open_page(width=width, theme=theme)
                    page.locator("#case-search").wait_for()
                    browser.open_library_filters(page)
                    self.assert_enhanced(page)
                    native, _, trigger, panel = self.open_picker(page, "#content-origin-filter")
                    expect(panel.locator('[data-picker-value="all"]')).to_have_attribute("aria-selected", "true")
                    self.assert_no_overflow(page)
                    page.keyboard.press("Escape")
                    expect(trigger).to_be_focused()
                    browser.close_library_filters(page)
                    page.evaluate("location.hash = '#/202H/reviews'")
                    page.locator(".rev-card").first.wait_for()
                    page.locator("#rev-filter-open").click()
                    self.assert_enhanced(page)
                    self.open_picker(page, "#rev-language")
                    self.assert_no_overflow(page)
                    page.keyboard.press("Escape")
                    page.locator("#rev-drawer-done").click()
                    page.evaluate("location.hash = '#/202H/settings'")
                    expect(page.locator("#settings-default-model")).to_be_enabled()
                    self.assert_enhanced(page)
                    self.open_picker(page, "#settings-default-model")
                    self.assert_no_overflow(page)
                    page.close()


if __name__ == "__main__":
    unittest.main()
