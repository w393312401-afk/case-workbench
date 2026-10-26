"""Product picker and catalogue management against a disposable real library."""
from __future__ import annotations

import json
import unittest

import test_sidebar_ui as sidebar
import test_server as fixture


class ProductComboBrowserTests(unittest.TestCase):
    # Share only fixture/helpers, without rerunning unrelated sidebar tests.
    setUpClass = classmethod(sidebar.SidebarBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(sidebar.SidebarBrowserTests.tearDownClass.__func__)
    setUp = sidebar.SidebarBrowserTests.setUp
    stop_server = sidebar.SidebarBrowserTests.stop_server
    request = sidebar.SidebarBrowserTests.request
    make_playable_video = sidebar.SidebarBrowserTests.make_playable_video
    assert_no_overflow = sidebar.SidebarBrowserTests.assert_no_overflow
    wait_peek = sidebar.SidebarBrowserTests.wait_peek

    def assert_route(self, page, name, *, product="202H"):
        # Hash assignment precedes the shell's hashchange handler and select update.
        page.wait_for_function(
            "product => document.querySelector('#global-product').value === product", arg=product,
        )
        sidebar.SidebarBrowserTests.assert_route(self, page, name, product=product)

    def open_page(self, *, width=1280, height=900, preference=None, read_only=None,
                  before_navigation=None):
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        if preference is not None:
            context.add_init_script(
                "localStorage.setItem('workbench-sidebar-v1', " + json.dumps(preference) + ");"
            )
        if read_only:
            # Exercise shell gating separately from the guest network guards.
            context.add_init_script(f"window.{read_only} = true;")
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        if before_navigation:
            before_navigation(page)
        page.goto(self.base + "/#/202H/library")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        page.locator("#case-search").wait_for()
        page.locator(".product-combo-trigger").wait_for(state="attached")
        return page

    def catalog_change(self, action, product):
        status, _, data = self.request("POST", "/api/products/catalog", {
            "action": action, "product": product,
        })
        self.assertEqual(status, 200, data)
        return data

    def open_picker(self, page):
        page.locator(".product-combo-trigger").click()
        panel = page.locator(".product-combo-panel")
        panel.wait_for()
        page.wait_for_function(
            "() => document.activeElement === document.querySelector('.product-combo-search')"
        )
        return panel

    def open_manager(self, page):
        self.open_picker(page).locator("[data-combo-manage]").click()
        dialog = page.locator("#product-manager-dialog[open]")
        dialog.wait_for()
        self.assertEqual(page.get_by_role("dialog").get_attribute("id"), "product-manager-dialog")
        self.assertTrue(page.locator(".product-combo-panel").is_hidden())
        return dialog

    def assert_in_viewport(self, page, locator):
        box = locator.bounding_box()
        self.assertIsNotNone(box)
        viewport = page.viewport_size
        self.assertGreaterEqual(box["x"], 0)
        self.assertGreaterEqual(box["y"], 0)
        self.assertLessEqual(box["x"] + box["width"], viewport["width"] + 1)
        self.assertLessEqual(box["y"] + box["height"], viewport["height"] + 1)
        self.assert_no_overflow(page)

    def case_library_bytes(self):
        return {str(path.relative_to(self.skill)): path.read_bytes()
                for path in self.skill.rglob("*") if path.is_file()}

    def assert_deleted_products_stay_absent(self, page, names):
        catalog = self.request("GET", "/api/products")[2]
        for name in names:
            self.assertNotIn(name, catalog["products"])
            self.assertNotIn(name, catalog["removed"])
        page.reload()
        page.locator("#case-search").wait_for()
        page.evaluate("names => WorkbenchShell.registerProducts(names)", names)
        panel = self.open_picker(page)
        for name in names:
            self.assertEqual(panel.locator('[data-product=' + json.dumps(name, ensure_ascii=False) + ']').count(), 0,
                             "Reloading and source discovery must not resurrect a deleted product")

    def test_picker_groups_models_searches_words_and_restores_focus_after_selection(self):
        self.catalog_change("add", "205H")
        self.catalog_change("add", "Electric portable kitchen sharpener")
        self.catalog_change("add", "Hidden fixture product")
        self.catalog_change("remove", "Hidden fixture product")
        page = self.open_page()
        panel = self.open_picker(page)
        panel.locator('[data-product="205H"]').wait_for()
        self.assertTrue(panel.evaluate("node => node.parentElement === document.body"))
        self.assertIn("型号", panel.inner_text())
        self.assertIn("其他产品", panel.inner_text())
        current = panel.locator('[data-product="202H"]')
        self.assertEqual(current.get_attribute("role"), "option")
        self.assertEqual(current.get_attribute("aria-selected"), "true")
        self.assertEqual(panel.locator('[data-product="Hidden fixture product"]').count(), 0)
        self.assertEqual(panel.locator('[data-product=""]').count(), 1)
        search = page.locator(".product-combo-search")
        self.assertEqual(search.get_attribute("role"), "combobox")
        self.assertEqual(search.get_attribute("aria-controls"), "product-combo-options")
        search.fill("portable electric")
        matched = panel.locator('.product-combo-list [data-product]')
        self.assertEqual(matched.count(), 1)
        self.assertEqual(matched.get_attribute("data-product"), "Electric portable kitchen sharpener")
        search.fill("sharpal 205")
        panel.locator('[data-product="205H"]').click()
        self.assert_route(page, "library", product="205H")
        self.assertTrue(panel.is_hidden())
        self.assertTrue(page.locator(".product-combo-trigger").evaluate("node => node === document.activeElement"))
        panel = self.open_picker(page)
        self.assertEqual(panel.locator('[data-product="205H"]').get_attribute("aria-selected"), "true")
        self.assertEqual(panel.locator('[aria-selected="true"]').count(), 1)

    def test_keyboard_navigation_escape_and_ime_confirmation(self):
        self.catalog_change("add", "205H")
        self.catalog_change("add", "207H")
        page = self.open_page()
        panel = self.open_picker(page)
        panel.locator('[data-product="207H"]').wait_for()
        search = page.locator(".product-combo-search")
        search.fill("20")
        first = search.get_attribute("aria-activedescendant")
        self.assertTrue(first)
        page.keyboard.press("ArrowDown")
        second = search.get_attribute("aria-activedescendant")
        self.assertNotEqual(first, second)
        page.keyboard.press("ArrowUp")
        self.assertEqual(search.get_attribute("aria-activedescendant"), first)
        page.keyboard.press("ArrowDown")
        selected = page.locator("[id=" + json.dumps(second) + "]").get_attribute("data-product")
        search.evaluate("""node => node.dispatchEvent(new KeyboardEvent('keydown', {
          key: 'Enter', code: 'Enter', isComposing: true, bubbles: true, cancelable: true,
        }))""")
        self.assertTrue(panel.is_visible())
        self.assert_route(page, "library")
        page.keyboard.press("Enter")
        self.assert_route(page, "library", product=selected)
        self.assertTrue(panel.is_hidden())
        self.open_picker(page)
        page.keyboard.press("Escape")
        self.assertTrue(panel.is_hidden())
        self.assertTrue(page.locator(".product-combo-trigger").evaluate("node => node === document.activeElement"))
        self.assert_route(page, "library", product=selected)

    def test_manager_add_hide_restore_persists_without_changing_case_records(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page()
        dialog = self.open_manager(page)
        add = dialog.locator("[data-product-add-input]")
        add.fill("205H")
        dialog.locator("[data-product-add]").click()
        row = dialog.locator('[data-product-row="205H"]')
        row.wait_for()
        self.assertEqual(add.input_value(), "")
        self.assertIn("205H", self.request("GET", "/api/products")[2]["products"])
        self.assert_route(page, "library")
        manager_search = dialog.locator("[data-product-manager-search]")
        manager_search.fill("205")
        self.assertEqual(dialog.locator("[data-product-manager-list] [data-product-row]").count(), 1)
        row.locator('[data-remove="205H"]').click()
        row.wait_for(state="detached")
        catalog = self.request("GET", "/api/products")[2]
        self.assertNotIn("205H", catalog["products"])
        self.assertIn("205H", catalog["removed"])
        dialog.locator('[data-product-tab="hidden"]').click()
        dialog.locator('[data-product-row="205H"] [data-restore="205H"]').click()
        dialog.locator('[data-product-row="205H"]').wait_for(state="detached")
        catalog = self.request("GET", "/api/products")[2]
        self.assertIn("205H", catalog["products"])
        self.assertNotIn("205H", catalog["removed"])
        dialog.locator('[data-product-tab="active"]').click()
        dialog.locator('[data-product-row="205H"]').wait_for()
        dialog.locator("[data-product-manager-close]").first.click()
        self.assertTrue(page.locator("#product-manager-dialog").is_hidden())
        page.wait_for_function("() => document.querySelector('.product-combo-trigger') === document.activeElement")
        page.reload()
        page.locator("#case-search").wait_for()
        self.open_picker(page).locator('[data-product="205H"]').wait_for()
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_manager_keeps_failed_input_and_prevents_duplicate_writes(self):
        page = self.open_page()
        dialog = self.open_manager(page)
        requests = []

        def change(route):
            requests.append(route.request.post_data_json)
            if len(requests) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({
                    "error": "暂时无法保存，请重试。",
                }, ensure_ascii=False))
            else:
                route.continue_()

        page.route("**/api/products/catalog", change)
        add = dialog.locator("[data-product-add-input]")
        add.fill("205H")
        dialog.locator("[data-product-add]").click()
        error = dialog.locator('#product-manager-error[role="alert"]')
        error.get_by_text("暂时无法保存，请重试。", exact=False).wait_for()
        self.assertEqual(add.input_value(), "205H")
        self.assertNotIn("205H", self.request("GET", "/api/products")[2]["products"])
        dialog.locator("[data-product-add]").click()
        dialog.locator('[data-product-row="205H"]').wait_for()
        self.assertEqual(add.input_value(), "")
        self.assertEqual(len(requests), 2)
        status = dialog.locator('#product-manager-status[role="status"]')
        self.assertIn("205H", status.inner_text())
        add.fill("sharpal 205h")
        dialog.locator("[data-product-add]").click()
        page.wait_for_function("""() => {
          const error = document.querySelector('#product-manager-error');
          const status = document.querySelector('#product-manager-status');
          return /已存在|已有|已经/.test([error?.textContent, status?.textContent].join(' '));
        }""")
        self.assertEqual(len(requests), 2, "Duplicates should be handled without another catalogue write")
        self.assertEqual(add.input_value(), "sharpal 205h")

    def test_manager_deletes_active_product_only_after_confirmation_and_preserves_cases(self):
        page = self.open_page()
        before = self.case_library_bytes()
        dialog = self.open_manager(page)
        row = dialog.locator('[data-product-row="202H"]')
        row.locator('[data-delete="202H"]').click()
        confirmation = dialog.locator("[data-product-delete-confirmation]")
        confirmation.wait_for()
        self.assertIn("202H", confirmation.inner_text())
        self.assertIn("案例", confirmation.inner_text())
        self.assertIn("202H", self.request("GET", "/api/products")[2]["products"])
        confirmation.locator("[data-product-delete-cancel]").click()
        confirmation.wait_for(state="hidden")
        self.assertTrue(row.is_visible())
        self.assertIn("202H", self.request("GET", "/api/products")[2]["products"])

        row.locator('[data-delete="202H"]').click()
        confirmation.locator("[data-product-delete-confirm]").click()
        row.wait_for(state="detached")
        self.assertEqual(self.case_library_bytes(), before)
        self.assert_deleted_products_stay_absent(page, ["202H"])
        self.assertEqual(self.case_library_bytes(), before)

    def test_late_initial_catalog_response_cannot_resurrect_a_deleted_product(self):
        held = {}

        def hold_initial_catalog(route):
            status, _, catalog = self.request("GET", "/api/products")
            self.assertEqual(status, 200, catalog)
            held.update(route=route, catalog=catalog)
            # Retain the actual pre-deletion response until the mutation finishes.

        page = self.open_page(before_navigation=lambda page: page.route(
            "**/api/products", hold_initial_catalog,
        ))
        self.assertIn("202H", held["catalog"]["products"])
        before = self.case_library_bytes()
        dialog = self.open_manager(page)
        row = dialog.locator('[data-product-row="202H"]')
        row.locator('[data-delete="202H"]').click()
        dialog.locator("[data-product-delete-confirm]").click()
        row.wait_for(state="detached")
        self.assertNotIn("202H", self.request("GET", "/api/products")[2]["products"])

        with page.expect_response(lambda response: response.url.endswith("/api/products")):
            held["route"].fulfill(status=200, content_type="application/json",
                                  body=json.dumps(held["catalog"], ensure_ascii=False))
        # Let the released response's JSON promise and UI updates finish before
        # asserting absence; an immediate negative locator check could pass early.
        page.evaluate("""() => new Promise(resolve =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
        self.assertEqual(row.count(), 0, "A stale initial load must not overwrite a successful deletion")
        dialog.locator('[data-product-tab="hidden"]').click()
        self.assertEqual(row.count(), 0)
        dialog.locator("[data-product-manager-close]").first.click()
        panel = self.open_picker(page)
        self.assertEqual(panel.locator('[data-product="202H"]').count(), 0)
        self.assertEqual(self.case_library_bytes(), before)

    def test_manager_deletes_every_hidden_product_even_when_search_filters_the_list(self):
        for name in ("205H", "207H"):
            self.catalog_change("add", name)
        for name in ("202H", "205H", "207H"):
            self.catalog_change("remove", name)
        page = self.open_page()
        before = self.case_library_bytes()
        dialog = self.open_manager(page)
        dialog.locator('[data-product-tab="hidden"]').click()
        dialog.locator("[data-product-manager-search]").fill("205")
        self.assertEqual(dialog.locator("[data-product-row]").count(), 1)
        dialog.locator("[data-product-delete-hidden]").click()
        confirmation = dialog.locator("[data-product-delete-confirmation]")
        confirmation.wait_for()
        self.assertIn("3", confirmation.inner_text(), "Confirmation must count all hidden products")
        self.assertIn("案例", confirmation.inner_text())
        confirmation.locator("[data-product-delete-confirm]").click()
        dialog.locator('[data-product-row="205H"]').wait_for(state="detached")
        self.assertEqual(self.request("GET", "/api/products")[2]["removed"], [])
        dialog.locator("[data-product-manager-search]").fill("")
        self.assertEqual(dialog.locator("[data-product-row]").count(), 0)
        self.assertEqual(self.case_library_bytes(), before)
        self.assert_deleted_products_stay_absent(page, ["202H", "205H", "207H"])
        self.assertEqual(self.case_library_bytes(), before)

    def test_manager_hides_and_deletes_existing_names_over_120_characters(self):
        # Imported case names can predate the limit on manually added products.
        long_name = "历史导入商品名称：" + "便携电动磨刀器品牌型号待核实" * 12
        self.assertGreater(len(long_name), 120)
        current = fixture.new_case_snapshot(self.video)
        current["title"] = "保留长产品名的历史案例"
        self.store.add({
            "title": current["title"], "product": long_name,
            "original": {"text": "Long product fixture", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "Historical product name fixture"},
            "current": current,
        })
        page = self.open_page(width=390)
        before = self.case_library_bytes()
        dialog = self.open_manager(page)
        self.assertEqual(dialog.locator("[data-product-add-input]").get_attribute("maxlength"), "120")
        row = dialog.locator('[data-product-row=' + json.dumps(long_name, ensure_ascii=False) + ']')
        row.wait_for()
        row.locator("[data-remove]").click()
        row.wait_for(state="detached")
        self.assertIn(long_name, self.request("GET", "/api/products")[2]["removed"])
        self.assertTrue(dialog.locator("#product-manager-error").is_hidden())
        dialog.locator('[data-product-tab="hidden"]').click()
        row.wait_for()
        row.locator("[data-delete]").click()
        confirmation = dialog.locator("[data-product-delete-confirmation]")
        confirmation.wait_for()
        confirmation.locator("[data-product-delete-confirm]").scroll_into_view_if_needed()
        self.assert_in_viewport(page, dialog)
        self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
        confirmation.locator("[data-product-delete-confirm]").click()
        row.wait_for(state="detached")
        self.assertEqual(self.case_library_bytes(), before)
        self.assert_deleted_products_stay_absent(page, [long_name])
        self.assertEqual(self.case_library_bytes(), before)

    def test_mobile_delete_confirmation_scrolls_to_cancel_confirm_and_footer(self):
        long_name = "历史长产品名称：" + "厨房便携电动磨刀器品牌型号待核实" * 12
        current = fixture.new_case_snapshot(self.video)
        current["title"] = "小屏删除确认的历史案例"
        self.store.add({
            "title": current["title"], "product": long_name,
            "original": {"text": "Small-screen confirmation fixture", "source": "isolated UI test"},
            "feedback": {"kind": "editorial", "text": "Historical product name fixture"},
            "current": current,
        })
        page = self.open_page(width=390, height=600)
        before = self.case_library_bytes()
        dialog = self.open_manager(page)
        row = dialog.locator('[data-product-row=' + json.dumps(long_name, ensure_ascii=False) + ']')
        row.locator("[data-remove]").click()
        row.wait_for(state="detached")
        dialog.locator('[data-product-tab="hidden"]').click()
        row.wait_for()
        confirmation = dialog.locator("[data-product-delete-confirmation]")
        cancel = confirmation.locator("[data-product-delete-cancel]")
        confirm = confirmation.locator("[data-product-delete-confirm]")
        done = dialog.locator(".product-manager-foot [data-product-manager-close]")

        def wheel_to_controls():
            self.assertTrue(dialog.evaluate("node => node.scrollHeight > node.clientHeight"))
            heading = dialog.locator(".product-manager-heading")
            heading.scroll_into_view_if_needed()
            initial_scroll = dialog.evaluate("node => node.scrollTop")
            heading.hover()
            page.mouse.wheel(0, 1200)
            page.wait_for_function(
                "before => document.querySelector('#product-manager-dialog').scrollTop > before",
                arg=initial_scroll,
            )
            self.assert_in_viewport(page, dialog)
            for control in (cancel, confirm, done):
                self.assert_in_viewport(page, control)
                frame, bounds = dialog.bounding_box(), control.bounding_box()
                self.assertGreaterEqual(bounds["y"], frame["y"])
                self.assertLessEqual(bounds["y"] + bounds["height"], frame["y"] + frame["height"])
                self.assertTrue(control.evaluate("""node => {
                    const box = node.getBoundingClientRect();
                    return node.contains(document.elementFromPoint(box.x + box.width / 2,
                                                                    box.y + box.height / 2));
                }"""), "The visible control must not be clipped or covered")

        row.locator("[data-delete]").click()
        confirmation.wait_for()
        wheel_to_controls()
        cancel.click()
        confirmation.wait_for(state="hidden")
        self.assertIn(long_name, self.request("GET", "/api/products")[2]["removed"])

        row.locator("[data-delete]").click()
        confirmation.wait_for()
        wheel_to_controls()
        confirm.click()
        row.wait_for(state="detached")
        done.scroll_into_view_if_needed()
        self.assert_in_viewport(page, done)
        done.click()
        self.assertTrue(dialog.is_hidden())
        self.assertEqual(self.case_library_bytes(), before)
        self.assertNotIn(long_name, self.request("GET", "/api/products")[2]["removed"])

    def test_floating_picker_survives_sidebar_hover_and_long_names_fit_mobile(self):
        long_name = "便携式电动磨刀器（品牌型号待核实），厨房用刀、户外工具及多种刀具适用，详细商品名称用于测试完整显示和自动换行"
        self.catalog_change("add", long_name)
        page = self.open_page(preference="collapsed")
        page.locator('#sidebar-rail [data-sidebar-target="library-nav"]').hover()
        self.wait_peek(page)
        panel = self.open_picker(page)
        self.assertAlmostEqual(panel.bounding_box()["width"], 360, delta=1)
        self.assert_in_viewport(page, panel)
        panel.locator('[data-product="' + long_name + '"]').hover()
        page.wait_for_timeout(350)
        self.assertTrue(panel.is_visible())
        self.wait_peek(page)
        self.assertTrue(panel.locator(".product-combo-list").evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
        long_row = panel.locator('[data-product="' + long_name + '"]')
        short_row = panel.locator('[data-product="202H"]')
        self.assertGreater(long_row.bounding_box()["height"], short_row.bounding_box()["height"] + 8)
        self.assertTrue(long_row.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
        page.locator(".product-combo-trigger").click()
        page.mouse.move(1200, 800)
        self.wait_peek(page, False)

        mobile = self.open_page(width=390)
        panel = self.open_picker(mobile)
        self.assert_in_viewport(mobile, panel)
        self.assertTrue(panel.locator(".product-combo-list").evaluate("node => node.scrollWidth <= node.clientWidth + 1"))
        panel.locator("[data-combo-manage]").click()
        dialog = mobile.locator("#product-manager-dialog[open]")
        dialog.wait_for()
        self.assert_in_viewport(mobile, dialog)
        self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))

    def test_short_mobile_manager_scrolls_all_controls_into_view(self):
        for product in ("205H", "207H", "209H"):
            self.catalog_change("add", product)
        page = self.open_page(width=390, height=320)
        dialog = self.open_manager(page)
        self.assert_in_viewport(page, dialog)
        self.assertTrue(dialog.evaluate("node => node.scrollHeight > node.clientHeight"))

        def reveal(control):
            control.scroll_into_view_if_needed()
            self.assertTrue(control.is_visible())
            self.assert_in_viewport(page, control)
            frame = dialog.bounding_box()
            bounds = control.bounding_box()
            self.assertGreaterEqual(bounds["y"], frame["y"])
            self.assertLessEqual(bounds["y"] + bounds["height"], frame["y"] + frame["height"])
            self.assertTrue(dialog.evaluate("node => node.scrollWidth <= node.clientWidth + 1"))

        add = dialog.locator("[data-product-add-input]")
        reveal(add)
        # A mouse wheel must move the outer dialog, including its footer.
        initial_scroll = dialog.evaluate("node => node.scrollTop")
        add.hover()
        page.mouse.wheel(0, 800)
        page.wait_for_function(
            "before => document.querySelector('#product-manager-dialog').scrollTop > before",
            arg=initial_scroll,
        )
        reveal(add)
        add.fill("211H")
        submit = dialog.locator("[data-product-add]")
        reveal(submit)
        submit.click()
        remove = dialog.locator('[data-product-row="211H"] [data-remove="211H"]')
        remove.wait_for()
        reveal(remove)
        remove.click()
        remove.wait_for(state="detached")
        hidden_tab = dialog.locator('[data-product-tab="hidden"]')
        reveal(hidden_tab)
        hidden_tab.click()
        restore = dialog.locator('[data-product-row="211H"] [data-restore="211H"]')
        reveal(restore)
        restore.click()
        restore.wait_for(state="detached")
        self.assertIn("211H", self.request("GET", "/api/products")[2]["products"])
        footer = dialog.locator(".product-manager-foot")
        reveal(footer)
        done = footer.locator("[data-product-manager-close]")
        reveal(done)
        done.click()
        self.assertTrue(page.locator("#product-manager-dialog").is_hidden())
        page.wait_for_function("() => document.querySelector('.product-combo-trigger') === document.activeElement")
        self.assert_no_overflow(page)

    def test_read_only_modes_keep_selection_and_hide_management(self):
        self.catalog_change("add", "205H")
        for flag in ("WorkbenchGuest", "PublicTextMode"):
            with self.subTest(read_only=flag):
                page = self.open_page(read_only=flag)
                panel = self.open_picker(page)
                self.assertEqual(panel.locator("[data-combo-manage]:visible").count(), 0)
                self.assertEqual(page.locator("#product-manager-dialog[open]").count(), 0)
                panel.locator('[data-product="205H"]').click()
                self.assert_route(page, "library", product="205H")


if __name__ == "__main__":
    unittest.main()
