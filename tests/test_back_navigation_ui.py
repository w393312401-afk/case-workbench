"""Parent-page navigation against a disposable direction catalog and browser profile."""
from __future__ import annotations

import unittest
from urllib.parse import parse_qs

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class BackNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    filtered_list = "#/202H/directions?q=%E5%AE%9A%E8%A7%92&tier=S&status=testing&archive=all"
    detail_route = "#/202H/directions/202H-S1"

    def assert_hash(self, page, expected):
        page.wait_for_function(
            "expected => location.hash.split('?')[0] === expected.split('?')[0]", arg=expected)
        actual_path, _, actual_query = page.evaluate("location.hash").partition("?")
        expected_path, _, expected_query = expected.partition("?")
        self.assertEqual(actual_path, expected_path)
        self.assertEqual(parse_qs(actual_query), parse_qs(expected_query))

    def open_direction(self, page):
        page.locator('.dir-row[data-direction-id="202H-S1"] .direction-card-link').click()
        page.locator("#direction-definition").wait_for()

    def parent_link(self, page):
        return page.locator("#breadcrumb").get_by_role("link", name="卖点与方向", exact=True)

    def scroll_list_to_direction(self, page):
        # Keep the real list and card link; only increase spacing so restoration
        # can be observed even with this deliberately small temporary catalog.
        page.evaluate("""() => {
            const sheet = document.styleSheets[0];
            sheet.insertRule('.dir-list { padding-top: 850px !important; padding-bottom: 1500px !important; }', sheet.cssRules.length);
        }""")
        link = page.locator('.dir-row[data-direction-id="202H-S1"] .direction-card-link')
        link.scroll_into_view_if_needed()
        page.wait_for_function("() => window.scrollY > 300")
        return page.evaluate("window.scrollY")

    def test_breadcrumb_returns_to_filter_scope_and_scroll_then_workspace_opens_home(self):
        page = self.open_page(self.filtered_list)
        scroll_before = self.scroll_list_to_direction(page)
        list_hash = page.evaluate("location.hash")
        self.open_direction(page)
        self.assertEqual(self.parent_link(page).get_attribute("href"), list_hash)
        page.locator("#breadcrumb [aria-current=page]").get_by_text("202H-S1", exact=True).wait_for()
        self.parent_link(page).click()
        page.locator("#direction-filters").wait_for()
        self.assert_hash(page, list_hash)
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) <= 2", arg=scroll_before)
        self.assertEqual(page.locator('[name="q"]').input_value(), "定角")
        self.assertEqual(page.locator('[data-tier-filter="S"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator('#direction-filters [name="status"]').input_value(), "testing")
        self.assertEqual(page.locator('[name="archive"]').input_value(), "all")
        # The breadcrumb no longer repeats the sidebar's way home.
        self.assertEqual(page.locator("#breadcrumb").get_by_role("link", name="工作空间", exact=True).count(), 0)
        page.locator("#home-nav").click()
        self.assert_hash(page, "#/202H/home")

    def test_top_page_and_escape_actions_share_the_remembered_parent(self):
        page = self.open_page(self.filtered_list)
        list_hash = page.evaluate("location.hash")
        for action in ("top", "breadcrumb", "escape"):
            with self.subTest(action=action):
                self.open_direction(page)
                self.assertEqual(page.locator("#navigate-up").get_attribute("href"), list_hash)
                back = page.locator("#content").get_by_role("link", name="返回卖点与方向", exact=True, include_hidden=True)
                self.assertEqual(back.get_attribute("href"), list_hash)
                # On desktop the pinned top-bar arrow replaces the in-page link, which stays for phones.
                self.assertFalse(back.is_visible())
                if action == "top":
                    page.locator("#navigate-up").click()
                elif action == "breadcrumb":
                    self.parent_link(page).click()
                else:
                    page.keyboard.press("Escape")
                page.locator("#direction-filters").wait_for()
                self.assert_hash(page, list_hash)

    def test_deep_link_has_safe_parent_and_reload_keeps_the_visited_filter_scope(self):
        direct = self.open_page(self.detail_route)
        self.assertEqual(self.parent_link(direct).get_attribute("href"), "#/202H/directions")
        direct.locator("#navigate-up").click()
        direct.locator("#direction-filters").wait_for()
        self.assert_hash(direct, "#/202H/directions")

        visited = self.open_page(self.filtered_list)
        list_hash = visited.evaluate("location.hash")
        self.open_direction(visited)
        visited.reload()
        visited.locator("#direction-definition").wait_for()
        self.assertEqual(self.parent_link(visited).get_attribute("href"), list_hash)
        visited.locator("#navigate-up").click()
        visited.locator("#direction-filters").wait_for()
        self.assert_hash(visited, list_hash)

    def test_all_products_list_returns_from_concrete_detail_with_scope_scroll_and_picker_preserved(self):
        page = self.open_page(self.filtered_list.replace("/202H/", "/all/"))
        self.assertEqual(page.locator("#global-product").input_value(), "")
        list_hash = page.evaluate("location.hash")
        scroll_before = self.scroll_list_to_direction(page)
        self.open_direction(page)
        self.assertEqual(page.evaluate("location.hash.split('?')[0]"), self.detail_route)
        self.assertEqual(page.locator("#global-product").input_value(), "202H")
        for link in (self.parent_link(page), page.locator("#navigate-up"), page.locator(".back-link")):
            self.assertEqual(link.get_attribute("href"), list_hash)
        page.locator("#navigate-up").click()
        page.locator("#direction-filters").wait_for()
        self.assert_hash(page, list_hash)
        page.wait_for_function("expected => Math.abs(window.scrollY - expected) <= 2", arg=scroll_before)
        self.assertEqual(page.locator("#global-product").input_value(), "")
        self.assertEqual(page.locator('[name="q"]').input_value(), "定角")
        self.assertEqual(page.locator('[data-tier-filter="S"]').get_attribute("aria-pressed"), "true")
        self.assertEqual(page.locator('[name="archive"]').input_value(), "all")

        # A fresh profile receiving an all-products deep link should offer the
        # same parent from every visible return control and from the keyboard.
        direct = self.open_page("#/all/directions/202H-S1")
        for link in (self.parent_link(direct), direct.locator("#navigate-up"), direct.locator(".back-link")):
            self.assertEqual(link.get_attribute("href"), "#/all/directions")
        direct.keyboard.press("Escape")
        direct.locator("#direction-filters").wait_for()
        self.assert_hash(direct, "#/all/directions")
        self.assertEqual(direct.locator("#global-product").input_value(), "")

    def test_invalid_saved_parents_cannot_send_a_direction_to_another_product_or_external_url(self):
        for remembered in ("#/999H/directions?q=wrong-product", "https://example.invalid/directions"):
            with self.subTest(remembered=remembered):
                page = self.open_page(self.detail_route)
                page.evaluate("""hash => {
                    sessionStorage.setItem('workbench-navigation-v1', JSON.stringify([
                        ['202H', {hash, scroll: 600}]
                    ]));
                }""", remembered)
                page.reload()
                page.locator("#direction-definition").wait_for()
                for link in (self.parent_link(page), page.locator("#navigate-up"), page.locator(".back-link")):
                    self.assertEqual(link.get_attribute("href"), "#/202H/directions")
                page.locator("#navigate-up").click()
                page.locator("#direction-filters").wait_for()
                self.assert_hash(page, "#/202H/directions")
                self.assertEqual(page.locator("#global-product").input_value(), "202H")

    def test_parent_navigation_respects_unsaved_direction_guard_without_losing_return_scope(self):
        page = self.open_page(self.filtered_list)
        list_hash = page.evaluate("location.hash")
        self.open_direction(page)
        detail_hash = page.evaluate("location.hash")
        page.locator("#direction-edit").click()
        notes = page.locator('#direction-edit-dialog [name="notes"]')
        notes.fill("离开前应保留的未保存修改")
        # A modal makes the underlying breadcrumb inert. Follow its actual href
        # through the same hash navigation path used by all visible back links.
        href = self.parent_link(page).get_attribute("href")
        page.evaluate("href => { location.hash = href; }", href)
        page.locator("#direction-discard-dialog[open]").wait_for()
        page.get_by_role("button", name="继续编辑", exact=True).click()
        self.assert_hash(page, detail_hash)
        self.assertEqual(notes.input_value(), "离开前应保留的未保存修改")
        self.assertEqual(self.parent_link(page).get_attribute("href"), list_hash)
        page.evaluate("href => { location.hash = href; }", href)
        page.locator("#direction-discard-dialog[open]").wait_for()
        page.get_by_role("button", name="放弃修改", exact=True).click()
        page.locator("#direction-filters").wait_for()
        self.assert_hash(page, list_hash)
        saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")
        self.assertEqual(saved["notes"], "")

    def test_mobile_keeps_the_page_return_action_readable_without_overflow(self):
        page = self.open_page(self.filtered_list, width=390)
        list_hash = page.evaluate("location.hash")
        self.open_direction(page)
        self.assertFalse(page.locator("#navigate-up").is_visible())
        back = page.get_by_role("link", name="返回卖点与方向", exact=True)
        self.assertTrue(back.is_visible())
        bounds = back.bounding_box()
        self.assertIsNotNone(bounds)
        self.assertGreaterEqual(bounds["x"], 0)
        self.assertLessEqual(bounds["x"] + bounds["width"], 390)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        back.click()
        page.locator("#direction-filters").wait_for()
        self.assert_hash(page, list_hash)
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
