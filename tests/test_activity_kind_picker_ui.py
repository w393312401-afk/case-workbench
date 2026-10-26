"""Task type picker shares the product filter interaction and small-screen layout."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

import test_activity_product_picker_ui as product_activity
import test_task_activity_ui as activity


class ActivityKindPickerBrowserTests(unittest.TestCase):
    # Reuse only disposable fixtures and helpers, without repeating product tests.
    setUpClass = classmethod(product_activity.ActivityProductPickerBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(product_activity.ActivityProductPickerBrowserTests.tearDownClass.__func__)
    stop_server = product_activity.ActivityProductPickerBrowserTests.stop_server
    request = product_activity.ActivityProductPickerBrowserTests.request
    make_playable_video = product_activity.ActivityProductPickerBrowserTests.make_playable_video
    assert_no_overflow = product_activity.ActivityProductPickerBrowserTests.assert_no_overflow
    assert_in_viewport = product_activity.ActivityProductPickerBrowserTests.assert_in_viewport
    wait_peek = product_activity.ActivityProductPickerBrowserTests.wait_peek
    catalog_change = product_activity.ActivityProductPickerBrowserTests.catalog_change
    add_run = activity.TaskActivityBrowserTests.add_run
    counts = activity.TaskActivityBrowserTests.counts
    open_page = product_activity.ActivityProductPickerBrowserTests.open_page
    open_tasks = product_activity.ActivityProductPickerBrowserTests.open_tasks
    open_product_picker = product_activity.ActivityProductPickerBrowserTests.open_picker

    def setUp(self):
        product_activity.ActivityProductPickerBrowserTests.setUp(self)
        for kind, title in (("images", "出图 · 图片任务"), ("voice", "配音 · 配音任务")):
            row = self.add_run(kind + ":fixture", "202H", notify=True)
            row.update(kind=kind, title=title, owner_id="CAR-" + kind,
                       url="#/202H/production/CAR-" + kind)

    def respond(self, route):
        params = parse_qs(urlparse(route.request.url).query)
        kind = params.get("kind", [""])[0]
        if not kind or "after" in params or urlparse(route.request.url).path != "/api/activity":
            return activity.TaskActivityBrowserTests.respond(self, route)

        class KindFilteredRoute:
            request = route.request

            def fulfill(self, **options):
                payload = options.get("json")
                if payload and "items" in payload:
                    payload["items"] = [row for row in payload["items"] if row["kind"] == kind]
                route.fulfill(**options)

        return activity.TaskActivityBrowserTests.respond(self, KindFilteredRoute())

    def open_kind_picker(self, page):
        page.locator(".activity-kind-trigger").click()
        panel = page.locator(".activity-kind-panel")
        expect(panel).to_be_visible()
        expect(panel.locator(".activity-kind-search")).to_be_focused()
        return panel

    def choose_kind(self, page, kind):
        panel = self.open_kind_picker(page)
        panel.locator("[data-picker-value=" + json.dumps(kind) + "]").click()
        expect(panel).to_be_hidden()
        expect(page.locator("#activity-kind")).to_have_value(kind)

    def assert_list_request(self, kind, status):
        requests = [row["params"] for row in self.requests
                    if row["path"] == "/api/activity" and row["params"].get("filter") == status]
        self.assertTrue(requests)
        self.assertEqual(requests[-1].get("kind", ""), kind)

    def test_chinese_search_selection_and_all_types_reset_filter_only_tasks_and_remember_tabs(self):
        page = self.open_page()
        original_hash = page.evaluate("location.hash")
        dialog = self.open_tasks(page)
        panel = self.open_kind_picker(page)
        search = panel.locator(".activity-kind-search")
        search.fill("出图")
        expect(panel.locator("[data-picker-value]")).to_have_count(1)
        expect(panel.locator("[data-picker-value]")).to_have_attribute("data-picker-value", "images")
        panel.locator('[data-picker-value="images"]').click()
        expect(dialog.locator("#activity-list")).to_contain_text("图片任务")
        expect(dialog.locator("#activity-list")).not_to_contain_text("配音任务")
        self.assert_list_request("images", "current")
        expect(page.locator(".activity-kind-trigger")).to_contain_text("出图")
        self.assertEqual(page.evaluate("location.hash"), original_hash)
        expect(page.locator("#global-product")).to_have_value("202H")

        dialog.locator('[data-activity-filter="unread"]').click()
        expect(page.locator("#activity-kind")).to_have_value("")
        self.choose_kind(page, "voice")
        expect(dialog.locator("#activity-list")).to_contain_text("配音任务")
        expect(dialog.locator("#activity-list")).not_to_contain_text("图片任务")
        self.assert_list_request("voice", "unread")
        dialog.locator('[data-activity-filter="all"]').click()
        expect(page.locator("#activity-kind")).to_have_value("")
        self.choose_kind(page, "ai")
        expect(dialog.locator("#activity-list")).to_contain_text("写稿第 1 轮")
        self.assert_list_request("ai", "all")
        self.choose_kind(page, "")
        expect(page.locator(".activity-kind-trigger")).to_contain_text("全部类型")
        expect(dialog.locator("#activity-list")).to_contain_text("图片任务")
        expect(dialog.locator("#activity-list")).to_contain_text("配音任务")
        self.assert_list_request("", "all")
        for status, expected in (("current", "images"), ("unread", "voice"), ("all", "")):
            dialog.locator('[data-activity-filter="' + status + '"]').click()
            expect(page.locator("#activity-kind")).to_have_value(expected)
        dialog.locator("[data-activity-close]").click()
        page.reload()
        page.locator("#case-search").wait_for()
        dialog = self.open_tasks(page)
        dialog.locator('[data-activity-filter="current"]').click()
        expect(page.locator("#activity-kind")).to_have_value("images")
        expect(page.locator(".activity-kind-trigger")).to_contain_text("出图")

    def test_product_and_kind_popovers_are_mutually_exclusive_and_aria_targets_are_unique(self):
        page = self.open_page()
        self.open_tasks(page)
        product_panel = self.open_product_picker(page)
        kind_panel = self.open_kind_picker(page)
        expect(product_panel).to_be_hidden()
        self.assertEqual(page.locator(".product-picker-panel:popover-open").count(), 1)
        search = kind_panel.locator(".activity-kind-search")
        expect(search).to_have_attribute("role", "combobox")
        target = search.get_attribute("aria-controls")
        self.assertEqual(page.locator("[id=" + json.dumps(target) + "]").count(), 1)
        expect(kind_panel.locator(".activity-kind-list")).to_have_attribute("id", target)
        active = search.get_attribute("aria-activedescendant")
        self.assertTrue(active)
        self.assertEqual(page.locator("[id=" + json.dumps(active) + "]").count(), 1)
        expect(page.locator("[id=" + json.dumps(active) + "]")).to_have_attribute("role", "option")
        product_panel = self.open_product_picker(page)
        expect(kind_panel).to_be_hidden()
        self.assertEqual(page.locator(".product-picker-panel:popover-open").count(), 1)
        duplicate_ids = page.evaluate("""() => {
            const ids = [...document.querySelectorAll('.product-picker-panel [id], .product-picker-panel[id]')]
                .map(node => node.id);
            return ids.filter((id, index) => ids.indexOf(id) !== index);
        }""")
        self.assertEqual(duplicate_ids, [])

    def test_keyboard_ime_escape_and_touch_work_in_docked_and_modal_panels(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self.open_page(width=width)
                dialog = self.open_tasks(page)
                self.assertEqual(dialog.evaluate("node => node.matches(':modal')"), width < 1200)
                panel = self.open_kind_picker(page)
                self.assertTrue(panel.evaluate("node => node.matches(':popover-open')"))
                search = panel.locator(".activity-kind-search")
                search.fill("配音")
                search.evaluate("""node => node.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Enter', isComposing: true, bubbles: true, cancelable: true,
                }))""")
                expect(panel).to_be_visible()
                expect(page.locator("#activity-kind")).to_have_value("")
                page.keyboard.press("Enter")
                expect(panel).to_be_hidden()
                expect(page.locator("#activity-kind")).to_have_value("voice")
                expect(page.locator(".activity-kind-trigger")).to_be_focused()
                panel = self.open_kind_picker(page)
                page.keyboard.press("End")
                self.assertEqual(search.get_attribute("aria-activedescendant"),
                                 panel.locator("[data-picker-value]").last.get_attribute("id"))
                page.keyboard.press("Home")
                self.assertEqual(search.get_attribute("aria-activedescendant"),
                                 panel.locator("[data-picker-value]").first.get_attribute("id"))
                page.keyboard.press("Escape")
                expect(panel).to_be_hidden()
                expect(dialog).to_be_visible()
                expect(page.locator(".activity-kind-trigger")).to_be_focused()
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                expect(page.locator("#task-activity-open")).to_be_focused()
                page.close()

        page = self.open_page(width=390, touch=True)
        dialog = self.open_tasks(page)
        panel = self.open_kind_picker(page)
        panel.locator('[data-picker-value="images"]').tap()
        expect(panel).to_be_hidden()
        expect(page.locator("#activity-kind")).to_have_value("images")
        expect(dialog).to_be_visible()

    def test_both_filter_styles_match_and_fit_small_portrait_and_landscape_in_both_themes(self):
        for width, height in ((320, 900), (667, 375)):
            for theme in ("light", "dark"):
                with self.subTest(width=width, height=height, theme=theme):
                    page = self.open_page(width=width, height=height, color_scheme=theme)
                    dialog = self.open_tasks(page)
                    styles = page.evaluate("""() => {
                        const properties = ['height', 'fontSize', 'borderRadius', 'backgroundColor', 'borderColor'];
                        return ['.activity-product-trigger', '.activity-kind-trigger'].map(selector => {
                            const style = getComputedStyle(document.querySelector(selector));
                            return properties.map(property => style[property]);
                        });
                    }""")
                    self.assertEqual(styles[0], styles[1])
                    panel = self.open_kind_picker(page)
                    self.assert_in_viewport(page, dialog)
                    self.assert_in_viewport(page, panel)
                    listing = panel.locator(".activity-kind-list")
                    self.assertGreaterEqual(listing.bounding_box()["height"], 42)
                    self.assertTrue(listing.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    last = panel.locator('[data-picker-value="case"]')
                    last.scroll_into_view_if_needed()
                    self.assertTrue(last.evaluate("""node => {
                        const box = node.getBoundingClientRect();
                        return node.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
                    }"""), "The last task type remains reachable within the viewport")
                    last.click()
                    expect(page.locator(".activity-kind-trigger")).to_contain_text("自动拆解")
                    self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    self.assert_no_overflow(page)
                    page.close()


if __name__ == "__main__":
    unittest.main()
