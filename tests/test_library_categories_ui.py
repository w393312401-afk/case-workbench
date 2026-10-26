"""Custom library categories and their independent product scope.

All browser checks use the disposable cross-product LibraryViewsTests fixture.
"""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs

from playwright.sync_api import expect

import test_library_views_ui as library


class LibraryCategoriesBrowserTests(unittest.TestCase):
    setUpClass = classmethod(library.LibraryViewsTests.setUpClass.__func__)
    tearDownClass = classmethod(library.LibraryViewsTests.tearDownClass.__func__)
    setUp = library.LibraryViewsTests.setUp
    stop_server = library.LibraryViewsTests.stop_server
    request = library.LibraryViewsTests.request
    make_playable_video = library.LibraryViewsTests.make_playable_video
    ids = library.LibraryViewsTests.ids

    def open_page(self, *, width=1280, theme="light"):
        context = self.browser.new_context(
            viewport={"width": width, "height": 900},
            color_scheme=theme,
            has_touch=width < 600,
        )
        self.addCleanup(context.close)
        context.add_init_script(
            "if(localStorage.getItem('case-library-navigation-v1')===null)"
            "localStorage.setItem('case-library-navigation-v1'," + json.dumps(self.legacy) + ");"
        )
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/library")
        page.locator("#case-search").wait_for()
        page.locator("#library-product-filter + .workbench-select").wait_for()
        return page

    def stored_categories(self, page):
        return json.loads(page.evaluate("localStorage.getItem('case-library-navigation-v1')"))

    def category(self, page, identifier):
        return page.locator(f'[data-library-category="{identifier}"]')

    def choose_category(self, page, identifier):
        self.category(page, identifier).click()
        # Category navigation updates the hash before the route render settles.
        page.wait_for_function("id => state.view === id", arg=identifier)
        expect(self.category(page, identifier)).to_have_attribute("aria-pressed", "true")

    def create_category(self, page, name="跨产品精选", members=None):
        library.open_library_management(page)
        page.locator("#library-category-add").click()
        editor = page.locator("#library-category-editor[open]")
        editor.locator('[name="category_name"]').fill(name)
        description = editor.locator('[name="description"]')
        if description.count():
            description.fill("跨产品整理的实拍参考")
        for identifier in members if members is not None else [self.own, self.other]:
            editor.locator(f'input[type="checkbox"][data-case-id="{identifier}"]').check()
        editor.get_by_role("button", name="保存分类", exact=True).click()
        expect(editor).not_to_be_visible()
        entry = next(item for item in self.stored_categories(page) if item["label"] == name)
        self.assertTrue(entry["id"].startswith("custom-"))
        return entry["id"]

    def assert_no_overflow(self, page):
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        ))

    def assert_inside_viewport(self, locator, page):
        bounds = locator.bounding_box()
        self.assertIsNotNone(bounds)
        self.assertGreaterEqual(bounds["x"], -1)
        self.assertGreaterEqual(bounds["y"], -1)
        self.assertLessEqual(bounds["x"] + bounds["width"], page.viewport_size["width"] + 1)
        self.assertLessEqual(bounds["y"] + bounds["height"], page.viewport_size["height"] + 1)

    def test_default_all_products_and_existing_collection_cross_product_members_remain(self):
        assignment = (self.runtime / "navigation-assignments.json").read_bytes()
        page = self.open_page()
        expect(page.locator("#global-product")).to_have_value("202H")
        expect(page.locator("#library-product-filter")).to_have_value("")
        expect(self.category(page, "reference-all")).to_contain_text("全部产品")
        expect(self.category(page, "reference-all")).to_have_attribute("aria-pressed", "true")
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own, self.other})
        self.assertEqual(page.locator("[data-library-preset]").count(), 0)

        self.choose_category(page, self.legacy_id)
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own})
        page.reload()
        page.locator("#case-search").wait_for()
        expect(self.category(page, self.legacy_id)).to_have_attribute("aria-pressed", "true")
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own})
        self.choose_category(page, "reference-all")
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own, self.other})
        self.assertEqual((self.runtime / "navigation-assignments.json").read_bytes(), assignment)

    def test_local_product_picker_and_global_product_changes_are_independent(self):
        page = self.open_page()
        picker = page.locator("#library-product-filter + .workbench-select [data-select-trigger]")
        picker.click()
        panel = page.locator(".workbench-select-panel:popover-open")
        expect(panel.locator(".product-picker-search")).to_be_focused()
        panel.locator(".product-picker-search").fill("303X")
        panel.locator('[data-picker-value="303X"]').click()
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        expect(page.locator("#global-product")).to_have_value("202H")
        self.assertEqual(self.ids(page), [self.other])
        self.assertEqual(parse_qs(page.url.split("?", 1)[1])["caseProduct"], ["303X"])

        library.open_library_filters(page)
        page.locator("#direction-filter").select_option("303X-S2")
        page.locator("#variant-filter").select_option("direction")
        library.close_library_filters(page)
        self.assertEqual(self.ids(page), [self.other])
        list_hash = page.evaluate("location.hash")
        page.locator(f'.case-card[href="#case/{self.other}"]').click()
        page.locator(".case-dock-title").wait_for()
        page.locator("#navigate-up").click()
        page.locator("#case-search").wait_for()
        page.wait_for_function("expected => location.hash === expected", arg=list_hash)
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        expect(page.locator("#global-product")).to_have_value("202H")
        expect(page.locator("#direction-filter")).to_have_value("303X-S2")
        expect(page.locator("#variant-filter")).to_have_value("direction")
        self.assertEqual(self.ids(page), [self.other])
        selected_category = page.evaluate("state.view")
        page.locator("#global-product").select_option("")
        expect(page.locator("#global-product")).to_have_value("")
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        expect(page.locator("#direction-filter")).to_have_value("303X-S2")
        expect(page.locator("#variant-filter")).to_have_value("direction")
        self.assertEqual(page.evaluate("state.view"), selected_category)
        self.assertEqual(self.ids(page), [self.other])
        page.reload()
        page.locator("#case-search").wait_for()
        expect(page.locator("#global-product")).to_have_value("")
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        expect(page.locator("#direction-filter")).to_have_value("303X-S2")
        expect(page.locator("#variant-filter")).to_have_value("direction")
        self.assertEqual(self.ids(page), [self.other])
        page.locator("#global-product").select_option("202H")
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        self.assertEqual(self.ids(page), [self.other])

    def test_opening_all_or_cross_product_manual_category_clears_direction_and_variant(self):
        page = self.open_page()

        def apply_scope():
            page.locator("#library-product-filter").select_option("303X")
            library.open_library_filters(page)
            page.locator("#direction-filter").select_option("303X-S2")
            page.locator("#variant-filter").select_option("direction")
            library.close_library_filters(page)
            self.assertEqual(self.ids(page), [self.other])

        def assert_cleared(expected_ids):
            expect(page.locator("#library-product-filter")).to_have_value("")
            expect(page.locator("#direction-filter")).to_have_value("")
            expect(page.locator("#variant-filter")).to_have_value("all")
            expect(page.locator("#global-product")).to_have_value("202H")
            self.assertEqual(set(self.ids(page)), set(expected_ids))

        apply_scope()
        self.choose_category(page, "reference-all")
        assert_cleared([self.identifier, self.own, self.other])
        apply_scope()
        identifier = self.create_category(page)
        self.choose_category(page, identifier)
        assert_cleared([self.own, self.other])
        self.choose_category(page, "reference-all")
        apply_scope()
        self.choose_category(page, identifier)
        assert_cleared([self.own, self.other])

    def test_editing_legacy_collection_uses_explicit_members_and_discards_old_product_query(self):
        legacy = json.loads(self.legacy)
        # Older navigation preferences stored the display product group.
        legacy[0].update(product="SHARPAL 202H", query="Alpha")
        self.legacy = json.dumps(legacy, ensure_ascii=False)
        assignment = (self.runtime / "navigation-assignments.json").read_bytes()
        before = {identifier: self.store.read_case(identifier) for identifier in [
            self.identifier, self.own, self.archived, self.other,
        ]}
        page = self.open_page()
        self.choose_category(page, self.legacy_id)
        self.assertEqual(self.ids(page), [self.identifier])
        library.open_library_management(page)
        page.locator("#library-category-manage").click()
        page.locator(
            f'#library-category-manager [data-category-id="{self.legacy_id}"] [data-edit-category]'
        ).click()
        editor = page.locator("#library-category-editor[open]")
        expect(editor.locator(f'[data-case-id="{self.identifier}"]')).to_be_checked()
        expect(editor.locator(f'[data-case-id="{self.own}"]')).to_be_checked()
        editor.locator(f'[data-case-id="{self.other}"]').check()
        editor.get_by_role("button", name="保存分类", exact=True).click()
        expect(editor).not_to_be_visible()
        self.choose_category(page, self.legacy_id)
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own, self.other})
        saved = next(item for item in self.stored_categories(page) if item["id"] == self.legacy_id)
        self.assertTrue(saved["libraryCategory"])
        self.assertEqual((saved["product"], saved["query"]), ("", ""))
        self.assertEqual(set(saved["caseIds"]), {self.identifier, self.own, self.other})
        page.reload()
        page.locator("#case-search").wait_for()
        expect(page.locator("#global-product")).to_have_value("202H")
        expect(page.locator("#library-product-filter")).to_have_value("")
        self.assertEqual(set(self.ids(page)), {self.identifier, self.own, self.other})
        self.assertEqual((self.runtime / "navigation-assignments.json").read_bytes(), assignment)
        self.assertEqual({identifier: self.store.read_case(identifier) for identifier in before}, before)

    def test_create_edit_delete_restore_categories_persist_without_changing_cases(self):
        before = {identifier: self.store.read_case(identifier) for identifier in [
            self.identifier, self.own, self.archived, self.other,
        ]}
        assignment = (self.runtime / "navigation-assignments.json").read_bytes()
        page = self.open_page(width=390)
        identifier = self.create_category(page)
        self.choose_category(page, identifier)
        self.assertEqual(set(self.ids(page)), {self.own, self.other})
        page.reload()
        page.locator("#case-search").wait_for()
        self.assertEqual(set(self.ids(page)), {self.own, self.other})

        library.open_library_management(page)
        page.locator("#library-category-manage").click()
        manager = page.locator("#library-category-manager[open]")
        row = manager.locator(f'[data-category-id="{identifier}"]')
        row.locator("[data-edit-category]").click()
        editor = page.locator("#library-category-editor[open]")
        expect(editor.locator('[name="category_name"]')).to_have_value("跨产品精选")
        expect(editor.locator(f'[data-case-id="{self.own}"]')).to_be_checked()
        expect(editor.locator(f'[data-case-id="{self.other}"]')).to_be_checked()
        editor.locator('[name="category_name"]').fill("磨刀实拍精选")
        editor.locator(f'[data-case-id="{self.own}"]').uncheck()
        editor.locator(f'[data-case-id="{self.identifier}"]').check()
        editor.get_by_role("button", name="保存分类", exact=True).click()
        expect(editor).not_to_be_visible()
        if not manager.is_visible():
            library.open_library_management(page)
            page.locator("#library-category-manage").click()
        row.locator("[data-delete-category]").click()
        expect(row.locator("[data-restore-category]")).to_be_visible()
        manager.get_by_role("button", name="完成", exact=True).click()
        expect(self.category(page, identifier)).to_have_count(0)
        self.assertEqual(page.evaluate("state.view"), "reference-all")

        page.reload()
        page.locator("#case-search").wait_for()
        library.open_library_management(page)
        page.locator("#library-category-manage").click()
        row.locator("[data-restore-category]").click()
        manager.get_by_role("button", name="完成", exact=True).click()
        expect(self.category(page, identifier)).to_contain_text("磨刀实拍精选")
        self.choose_category(page, identifier)
        self.assertEqual(set(self.ids(page)), {self.identifier, self.other})
        page.reload()
        page.locator("#case-search").wait_for()
        self.assertEqual(set(self.ids(page)), {self.identifier, self.other})
        stored = self.stored_categories(page)
        legacy = next(item for item in stored if item["id"] == self.legacy_id)
        self.assertEqual((legacy["label"], legacy["caseIds"]), ("原有达人收藏", [self.own]))
        self.assertEqual((self.runtime / "navigation-assignments.json").read_bytes(), assignment)
        self.assertEqual({identifier: self.store.read_case(identifier) for identifier in before}, before)

    def test_saved_view_restores_local_product_without_switching_sidebar(self):
        page = self.open_page()
        page.locator("#library-product-filter").select_option("303X")
        library.open_library_filters(page)
        page.locator("#content-origin-filter").select_option("overseas_creator")
        page.locator("#direction-filter").select_option("303X-S2")
        library.close_library_filters(page)
        library.open_library_management(page)
        page.locator("#library-save-view-button").click()
        page.locator('#library-save-view [name="view_name"]').fill("303X 达人参考")
        page.locator("#library-save-view").get_by_role("button", name="保存视图", exact=True).click()
        page.wait_for_function(
            "() => document.querySelector('#library-saved-view').value.startsWith('custom-')"
        )
        saved_id = page.locator("#library-saved-view").input_value()
        expect(page.locator("#global-product")).to_have_value("202H")
        self.assertEqual(self.ids(page), [self.other])
        expect(self.category(page, saved_id)).to_have_attribute("aria-pressed", "true")
        page.locator("#case-search").fill("no matching saved case")
        expect(page.locator("#library-saved-view")).to_have_value("")
        expect(self.category(page, saved_id)).to_have_attribute("aria-pressed", "false")
        expect(self.category(page, "reference-all")).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        library.open_library_management(page)
        page.locator("#library-saved-view").select_option(saved_id)
        expect(page.locator("#case-search")).to_have_value("")
        expect(self.category(page, saved_id)).to_have_attribute("aria-pressed", "true")
        self.assertEqual(self.ids(page), [self.other])
        page.locator("#library-product-filter").select_option("")
        expect(page.locator("#library-saved-view")).to_have_value("")
        library.open_library_management(page)
        page.locator("#library-saved-view").select_option(saved_id)
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        expect(page.locator("#global-product")).to_have_value("202H")
        expect(page.locator("#direction-filter")).to_have_value("303X-S2")
        self.assertEqual(self.ids(page), [self.other])
        page.reload()
        page.locator("#case-search").wait_for()
        expect(page.locator("#global-product")).to_have_value("202H")
        expect(page.locator("#library-product-filter")).to_have_value("303X")
        self.assertEqual(self.ids(page), [self.other])

    def test_category_controls_and_product_layer_fit_mobile_desktop_and_both_themes(self):
        for width in (390, 1280):
            for theme in ("light", "dark"):
                with self.subTest(width=width, theme=theme):
                    page = self.open_page(width=width, theme=theme)
                    self.assert_no_overflow(page)
                    trigger = page.locator("#library-product-filter + .workbench-select [data-select-trigger]")
                    trigger.click()
                    panel = page.locator(".workbench-select-panel:popover-open")
                    self.assert_inside_viewport(panel, page)
                    panel.locator(".product-picker-search").fill("303X")
                    panel.locator('[data-picker-value="303X"]').click()
                    self.assertEqual(self.ids(page), [self.other])
                    identifier = self.create_category(page, name="响应式分类", members=[self.other])
                    self.choose_category(page, identifier)
                    self.assertEqual(self.ids(page), [self.other])
                    self.assert_no_overflow(page)
                    library.open_library_management(page)
                    page.locator("#library-category-manage").click()
                    manager = page.locator("#library-category-manager[open]")
                    self.assert_inside_viewport(manager, page)
                    manager.locator(f'[data-category-id="{identifier}"] [data-edit-category]').click()
                    editor = page.locator("#library-category-editor[open]")
                    self.assert_inside_viewport(editor, page)
                    self.assert_no_overflow(page)
                    editor.get_by_role("button", name="取消", exact=True).click()
                    if manager.is_visible():
                        manager.get_by_role("button", name="完成", exact=True).click()
                    self.assert_no_overflow(page)


if __name__ == "__main__":
    unittest.main()
