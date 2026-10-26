"""Task product filtering shares the sidebar picker against a disposable library."""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from playwright.sync_api import expect

import test_product_combo_ui as products
import test_task_activity_ui as activity


class ActivityProductPickerBrowserTests(unittest.TestCase):
    setUpClass = classmethod(products.ProductComboBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(products.ProductComboBrowserTests.tearDownClass.__func__)
    stop_server = products.ProductComboBrowserTests.stop_server
    request = products.ProductComboBrowserTests.request
    make_playable_video = products.ProductComboBrowserTests.make_playable_video
    assert_no_overflow = products.ProductComboBrowserTests.assert_no_overflow
    wait_peek = products.ProductComboBrowserTests.wait_peek
    catalog_change = products.ProductComboBrowserTests.catalog_change
    assert_in_viewport = products.ProductComboBrowserTests.assert_in_viewport
    open_sidebar_picker = products.ProductComboBrowserTests.open_picker
    add_run = activity.TaskActivityBrowserTests.add_run
    counts = activity.TaskActivityBrowserTests.counts
    respond = activity.TaskActivityBrowserTests.respond

    def setUp(self):
        products.ProductComboBrowserTests.setUp(self)
        self.requests, self.runs, self.events, self.changes, self.claims, self.read = [], {}, [], [], set(), set()
        self.epoch, self.version, self.available = "picker-fixture-epoch", 0, True
        self.feed_failures = 0
        self.catalog_change("add", "129N")
        for product in ("202H", "129N"):
            run = self.add_run("ai:" + product, product)
            run["owner_id"] = "AIT-" + product

    def open_page(self, *, width=1280, height=900, color_scheme="light", touch=False):
        if touch:
            context = self.browser.new_context(viewport={"width": width, "height": height}, has_touch=True)
            self.addCleanup(context.close)
            context.add_init_script("localStorage.setItem('workbench-sidebar-v1','expanded')")
            page = context.new_page()
            page.set_default_timeout(10000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
            page.route("**/api/activity**", self.respond)
            page.goto(self.base + "/#/202H/library")
            page.locator("#case-search").wait_for()
        else:
            page = products.ProductComboBrowserTests.open_page(
                self, width=width, height=height, preference="expanded",
                before_navigation=lambda page: page.route("**/api/activity**", self.respond),
            )
        page.emulate_media(color_scheme=color_scheme)
        page.wait_for_function("() => localStorage.getItem('workbench-activity-cursor-v1') !== null")
        return page

    def open_tasks(self, page):
        page.locator("#task-activity-open").click()
        dialog = page.locator("#task-activity-dialog")
        expect(dialog).to_be_visible()
        return dialog

    def open_picker(self, page):
        page.locator(".activity-product-trigger").click()
        panel = page.locator(".activity-product-panel")
        expect(panel).to_be_visible()
        expect(page.locator(".activity-product-search")).to_be_focused()
        return panel

    def open_manager(self, page):
        self.open_sidebar_picker(page).locator("[data-combo-manage]").click()
        manager = page.locator("#product-manager-dialog[open]")
        expect(manager).to_be_visible()
        return manager

    def picker_values(self, panel):
        return panel.locator("[data-product]").evaluate_all("rows => rows.map(row => row.dataset.product)")

    def choose_product(self, page, product):
        panel = self.open_picker(page)
        panel.locator('[data-product=' + json.dumps(product) + ']').click()
        expect(panel).to_be_hidden()
        expect(page.locator("#activity-product")).to_have_value(product)

    def test_sidebar_and_tasks_share_catalogue_without_historical_or_hidden_products(self):
        self.catalog_change("add", "205H")
        self.catalog_change("add", "Electric portable kitchen sharpener")
        self.catalog_change("add", "Historical hidden product")
        self.catalog_change("remove", "Historical hidden product")
        self.catalog_change("add", "Historical deleted product")
        self.catalog_change("delete", "Historical deleted product")
        hidden = self.add_run("ai:hidden", "Historical hidden product")
        hidden["owner_id"] = "AIT-hidden"
        self.add_run("ai:unknown", "Historical unknown product")["owner_id"] = "AIT-unknown"
        self.add_run("ai:deleted", "Historical deleted product")["owner_id"] = "AIT-deleted"
        page = self.open_page()
        sidebar = self.open_sidebar_picker(page)
        sidebar_values = self.picker_values(sidebar)
        page.keyboard.press("Escape")
        dialog = self.open_tasks(page)
        panel = self.open_picker(page)
        self.assertEqual(self.picker_values(panel), sidebar_values)
        self.assertNotIn("Historical hidden product", sidebar_values)
        self.assertNotIn("Historical unknown product", sidebar_values)
        self.assertNotIn("Historical deleted product", sidebar_values)
        self.assertEqual(sidebar_values.count(""), 1)
        self.assertIn("当前产品", panel.inner_text())
        self.assertIn("产品型号", panel.inner_text())
        self.assertIn("其他产品", panel.inner_text())
        expect(panel.locator('[data-product="202H"]')).to_have_attribute("aria-selected", "true")
        page.keyboard.press("Escape")
        self.choose_product(page, "")
        # A task can still be found through all products without repopulating the catalogue.
        expect(dialog.locator("#activity-list")).to_contain_text("Historical hidden product")
        panel = self.open_picker(page)
        self.assertNotIn("Historical hidden product", self.picker_values(panel))

    def test_search_filters_only_tasks_and_global_selection_synchronizes_all_status_tabs(self):
        self.catalog_change("add", "Electric portable kitchen sharpener")
        page = self.open_page()
        original_hash = page.evaluate("location.hash")
        dialog = self.open_tasks(page)
        expect(dialog.locator("#activity-list")).to_contain_text("202H")
        panel = self.open_picker(page)
        search = panel.locator(".activity-product-search")
        search.fill("portable ELECTRIC")
        self.assertEqual(self.picker_values(panel), ["Electric portable kitchen sharpener"])
        search.fill("sharpal 129")
        panel.locator('[data-product="129N"]').click()
        expect(dialog.locator("#activity-list")).to_contain_text("129N")
        expect(dialog.locator("#activity-list")).not_to_contain_text("202H")
        expect(page.locator(".activity-product-trigger")).to_contain_text("SHARPAL 129N")
        self.assertEqual(page.evaluate("location.hash"), original_hash)
        expect(page.locator("#global-product")).to_have_value("202H")
        # Product choices are remembered per task tab, just like type and title filters.
        dialog.locator('[data-activity-filter="all"]').click()
        expect(page.locator("#activity-product")).to_have_value("202H")
        self.choose_product(page, "")
        dialog.locator('[data-activity-filter="current"]').click()
        expect(page.locator("#activity-product")).to_have_value("129N")
        dialog.locator("[data-activity-close]").click()
        sidebar = self.open_sidebar_picker(page)
        sidebar.locator('[data-product="129N"]').click()
        page.wait_for_function("() => WorkbenchShell.product() === '129N'")
        dialog = self.open_tasks(page)
        expect(page.locator("#activity-product")).to_have_value("129N")
        dialog.locator('[data-activity-filter="all"]').click()
        expect(page.locator("#activity-product")).to_have_value("129N")

    def test_keyboard_and_escape_work_inside_docked_and_modal_task_panels(self):
        for width in (1280, 390):
            with self.subTest(width=width):
                page = self.open_page(width=width)
                dialog = self.open_tasks(page)
                self.assertEqual(dialog.evaluate("node => node.matches(':modal')"), width < 1200)
                panel = self.open_picker(page)
                self.assertTrue(panel.evaluate("node => node.matches(':popover-open')"))
                search = panel.locator(".activity-product-search")
                search.fill("sharpal 129")
                active = search.get_attribute("aria-activedescendant")
                self.assertTrue(active)
                self.assertEqual(page.locator("[id=" + json.dumps(active) + "]").get_attribute("data-product"), "129N")
                self.assertEqual(page.locator("[id=" + json.dumps(active) + "]").count(), 1)
                search.evaluate("""node => node.dispatchEvent(new KeyboardEvent('keydown', {
                    key: 'Enter', isComposing: true, bubbles: true, cancelable: true,
                }))""")
                expect(panel).to_be_visible()
                expect(page.locator("#activity-product")).to_have_value("202H")
                page.keyboard.press("Enter")
                expect(panel).to_be_hidden()
                expect(page.locator("#activity-product")).to_have_value("129N")
                expect(page.locator(".activity-product-trigger")).to_be_focused()
                panel = self.open_picker(page)
                page.keyboard.press("End")
                last = panel.locator("[data-product]").last.get_attribute("id")
                self.assertEqual(search.get_attribute("aria-activedescendant"), last)
                page.keyboard.press("Home")
                first = panel.locator("[data-product]").first.get_attribute("id")
                self.assertEqual(search.get_attribute("aria-activedescendant"), first)
                page.keyboard.press("ArrowDown")
                self.assertNotEqual(search.get_attribute("aria-activedescendant"), first)
                page.keyboard.press("Escape")
                expect(panel).to_be_hidden()
                expect(dialog).to_be_visible()
                expect(page.locator(".activity-product-trigger")).to_be_focused()
                page.keyboard.press("Escape")
                expect(dialog).to_be_hidden()
                expect(page.locator("#task-activity-open")).to_be_focused()
                self.assertEqual(page.locator(".product-picker-panel:popover-open").count(), 0)
                page.close()

        touch_page = self.open_page(width=390, touch=True)
        self.open_tasks(touch_page)
        panel = self.open_picker(touch_page)
        panel.locator('[data-product="129N"]').tap()
        expect(panel).to_be_hidden()
        expect(touch_page.locator("#activity-product")).to_have_value("129N")
        expect(touch_page.locator("#task-activity-dialog")).to_be_visible()

    def test_catalogue_management_updates_an_existing_task_picker(self):
        page = self.open_page()
        dialog = self.open_tasks(page)
        self.open_picker(page)
        dialog.locator("[data-activity-close]").click()
        expect(page.locator(".activity-product-panel")).to_be_hidden()
        manager = self.open_manager(page)
        manager.locator("[data-product-add-input]").fill("205H")
        manager.locator("[data-product-add]").click()
        manager.locator('[data-product-row="205H"]').wait_for()
        manager.locator("[data-product-manager-close]").first.click()
        self.open_tasks(page)
        panel = self.open_picker(page)
        expect(panel.locator('[data-product="205H"]')).to_be_visible()
        panel.locator('[data-product="205H"]').click()
        dialog.locator("[data-activity-close]").click()
        manager = self.open_manager(page)
        manager.locator('[data-remove="205H"]').click()
        manager.locator('[data-product-row="205H"]').wait_for(state="detached")
        manager.locator("[data-product-manager-close]").first.click()
        self.open_tasks(page)
        panel = self.open_picker(page)
        self.assertNotIn("205H", self.picker_values(panel))
        # Historical saved filter stays legible; it does not become a catalogue entry.
        expect(page.locator(".activity-product-trigger")).to_contain_text("SHARPAL 205H")
        panel.locator('[data-product=""]').click()
        expect(page.locator(".activity-product-trigger")).to_contain_text("全部产品")

    def test_task_picker_and_long_product_names_fit_narrow_screens_in_both_themes(self):
        long_name = "便携式电动磨刀器（品牌型号待核实），厨房用刀、户外工具及多种刀具适用，详细商品名称用于测试完整显示和自动换行"
        self.catalog_change("add", long_name)
        output = os.environ.get("ACTIVITY_PICKER_SCREENSHOT_DIR")
        for width, height in ((320, 900), (390, 900), (1280, 900), (667, 375)):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page = self.open_page(width=width, height=height, color_scheme=theme)
                    dialog = self.open_tasks(page)
                    if width <= 560:
                        self.assertAlmostEqual(dialog.bounding_box()["width"], width, delta=1)
                    self.assertGreaterEqual(dialog.locator("#activity-list").bounding_box()["height"], 42)
                    panel = self.open_picker(page)
                    self.assert_in_viewport(page, dialog)
                    self.assert_in_viewport(page, panel)
                    self.assertTrue(panel.locator(".activity-product-list").evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    self.assertGreaterEqual(panel.locator(".activity-product-list").bounding_box()["height"], 42)
                    self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    row = panel.locator('[data-product=' + json.dumps(long_name, ensure_ascii=False) + ']')
                    row.scroll_into_view_if_needed()
                    self.assertTrue(row.evaluate("""node => {
                        const box = node.getBoundingClientRect();
                        return node.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
                    }"""), "Product option must be reachable above the modal and within the viewport")
                    if output:
                        Path(output).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(output) / f"activity-product-{width}-{theme}.png"))
                    row.click()
                    expect(page.locator(".activity-product-trigger")).to_contain_text(long_name)
                    self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
                    self.assert_no_overflow(page)
                    page.close()


if __name__ == "__main__":
    unittest.main()
