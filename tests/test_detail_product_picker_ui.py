"""Case product assignment uses the shared catalogue in a disposable library.

Every HTTP write targets a temporary fixture on a random loopback port. Case
records and index bytes are checked independently of the editable label map.
"""
from __future__ import annotations

import json
import unittest

import test_product_combo_ui as combo
import test_server as fixture


class DetailProductPickerBrowserTests(unittest.TestCase):
    setUpClass = classmethod(combo.ProductComboBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(combo.ProductComboBrowserTests.tearDownClass.__func__)
    setUp = combo.ProductComboBrowserTests.setUp
    stop_server = combo.ProductComboBrowserTests.stop_server
    request = combo.ProductComboBrowserTests.request
    make_playable_video = combo.ProductComboBrowserTests.make_playable_video
    catalog_change = combo.ProductComboBrowserTests.catalog_change
    assert_no_overflow = combo.ProductComboBrowserTests.assert_no_overflow
    case_library_bytes = combo.ProductComboBrowserTests.case_library_bytes

    def open_detail(self, *, width=1280, height=900, read_only=None):
        context = self.browser.new_context(viewport={"width": width, "height": height})
        self.addCleanup(context.close)
        if read_only:
            context.add_init_script(f"window.{read_only} = true;")
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"{self.base}/#/202H/case/{self.identifier}")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for(state="attached")
        page.locator("#detail-product > summary").wait_for()
        return page

    def open_picker(self, page):
        page.locator("#detail-product > summary").click()
        dialog = page.locator("#case-product-dialog[open]")
        dialog.wait_for()
        dialog.locator("[data-product-search]").wait_for()
        return dialog

    def current_product(self):
        status, _, data = self.request("GET", f"/api/cases/{self.identifier}")
        self.assertEqual(status, 200, data)
        return data["product"]

    def option_values(self, dialog):
        return dialog.locator("[data-product-option]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.productOption)"
        )

    def add_source_product(self, product, *, video_id="7664986913497009458"):
        current = fixture.snapshot(self.video)
        current["metrics"]["video_id"] = video_id
        return self.store.add({
            "title": "历史产品名称 fixture", "product": product,
            "original": {"text": "Disposable source product", "source": "isolated picker test"},
            "feedback": {"kind": "editorial", "text": "Fixture only"}, "current": current,
        })

    def test_choices_match_sidebar_and_exclude_hidden_deleted_case_names(self):
        legacy = "Deleted source product fixture"
        self.add_source_product(legacy)
        self.catalog_change("add", "198H")
        self.catalog_change("remove", "202H")
        self.catalog_change("delete", legacy)
        before = self.case_library_bytes()
        page = self.open_detail()
        dialog = self.open_picker(page)
        dialog.locator('[data-product-option="198H"]').wait_for()
        values = self.option_values(dialog)
        self.assertNotIn("202H", values)
        self.assertNotIn(legacy, values)
        self.assertIn("202H", dialog.locator("[data-product-current]").inner_text())
        self.assertEqual(dialog.locator("select, [data-product-input]").count(), 0)
        shared = page.evaluate("WorkbenchShell.getProductChoices().map(item => item.value)")
        self.assertEqual(values, shared)
        self.assertEqual(dialog.locator('[data-product-option="198H"]').inner_text().strip(), "SHARPAL 198H")
        dialog.locator("[data-product-cancel]").click()
        dialog.wait_for(state="detached")
        page.locator(".product-combo-trigger").click()
        sidebar = page.locator(".product-combo-panel")
        sidebar.wait_for()
        sidebar_values = sidebar.locator('[data-product]:not([data-product=""])').evaluate_all(
            "nodes => nodes.map(node => node.dataset.product)"
        )
        self.assertEqual(set(sidebar_values), set(values))
        self.assertEqual(self.case_library_bytes(), before)

    def test_search_select_save_changes_assignment_without_rewriting_case_records(self):
        self.catalog_change("add", "198H")
        before = self.case_library_bytes()
        page = self.open_detail()
        dialog = self.open_picker(page)
        dialog.locator('[data-product-option="198H"]').wait_for()
        self.assertTrue(dialog.locator("[data-product-save]").is_disabled())
        dialog.locator("[data-product-search]").fill("sharpal 198")
        self.assertEqual(self.option_values(dialog), ["198H"])
        dialog.locator('[data-product-option="198H"]').click()
        self.assertEqual(self.current_product(), "SHARPAL 202H", "Selecting is a draft until Save")
        dialog.locator("[data-product-save]").click()
        dialog.wait_for(state="detached")
        page.locator("#detail-product > summary").get_by_text("SHARPAL 198H", exact=False).wait_for()
        self.assertEqual(self.current_product(), "SHARPAL 198H")
        self.assertEqual(self.case_library_bytes(), before)
        labels = json.loads((self.runtime / "evidence" / "case-products.json").read_text(encoding="utf-8"))
        self.assertEqual(labels["cases"][self.identifier]["product"], "SHARPAL 198H")
        page.reload()
        page.locator("#detail-product > summary").get_by_text("SHARPAL 198H", exact=False).wait_for()

    def test_new_source_products_are_verified_once_without_resurrecting_stale_names(self):
        deleted = "Deleted historical source fixture"
        new_product = "Newly imported fixture product"
        bogus = "Unverified stale browser product"
        self.add_source_product(deleted)
        self.catalog_change("delete", deleted)
        page = self.open_detail()
        page.evaluate("() => WorkbenchShell.refreshProducts()")
        self.add_source_product(new_product, video_id="7664986913497009459")
        status, _, fresh_catalog = self.request("GET", "/api/products")
        self.assertEqual(status, 200, fresh_catalog)
        self.assertIn(new_product, fresh_catalog["products"])
        requests, held = [], {}

        def catalog(route):
            requests.append(route.request.url)
            if len(requests) == 1:
                held["route"] = route
            else:
                route.continue_()

        page.route("**/api/products", catalog)
        with page.expect_request("**/api/products"):
            page.evaluate("""names => {
              state.polling = true;
              state.cases.push(...names.map((product, index) => ({case_id: 'discovery-' + index, product})));
              for (let index = 0; index < 40; index++) WorkbenchShell.update();
            }""", [new_product, bogus, deleted])
        # A second explicit consumer must await the same fresh response, not
        # resolve early with the inventory that existed before the import.
        page.evaluate("""() => {
          window.__catalogRefreshDone = false;
          void WorkbenchShell.refreshProducts().then(choices => {
            window.__catalogRefreshChoices = choices;
            window.__catalogRefreshDone = true;
          });
        }""")
        self.assertFalse(page.evaluate("window.__catalogRefreshDone"))
        self.assertEqual(len(requests), 1)
        self.assertNotIn(new_product, page.evaluate("WorkbenchShell.getProductChoices().map(row => row.value)"))
        with page.expect_response(lambda response: response.url.endswith("/api/products")):
            held["route"].fulfill(status=200, content_type="application/json", body=json.dumps(fresh_catalog, ensure_ascii=False))
        page.wait_for_function("window.__catalogRefreshDone === true")
        choices = page.evaluate("window.__catalogRefreshChoices.map(row => row.value)")
        self.assertIn(new_product, choices)
        self.assertNotIn(bogus, choices)
        self.assertNotIn(deleted, choices)
        with page.expect_response(lambda response: response.url.endswith("/api/products")):
            page.evaluate("() => { for (let index = 0; index < 40; index++) WorkbenchShell.update(); }")
        # Leave enough time for accidental promise/update recursion to fire.
        page.wait_for_timeout(350)
        self.assertEqual(len(requests), 2, "Repeated renders should coalesce and never recurse into catalogue requests")
        page.locator(".product-combo-trigger").click()
        sidebar = page.locator(".product-combo-panel")
        sidebar.locator('[data-product="' + new_product + '"]').wait_for()
        self.assertEqual(sidebar.locator('[data-product="' + bogus + '"]').count(), 0)
        self.assertEqual(sidebar.locator('[data-product="' + deleted + '"]').count(), 0)

    def test_save_updates_detail_and_sidebar_immediately_during_background_poll(self):
        self.catalog_change("add", "198H")
        before = self.case_library_bytes()
        page = self.open_detail()
        dialog = self.open_picker(page)
        dialog.locator('[data-product-option="198H"]').click()
        page.evaluate("state.polling = true")
        dialog.locator("[data-product-save]").click()
        dialog.wait_for(state="detached")
        page.locator("#detail-product > summary").get_by_text("SHARPAL 198H", exact=False).wait_for()
        self.assertTrue(page.evaluate("state.polling"), "The product UI must not depend on starting another full refresh")
        self.assertEqual(page.evaluate("WorkbenchShell.product()"), "198H")
        self.assertEqual(page.locator("#global-product").input_value(), "198H")
        self.assertEqual(page.evaluate("state.detail.product"), "SHARPAL 198H")
        self.assertEqual(page.evaluate("id => state.cases.find(row => row.case_id === id).product", self.identifier), "SHARPAL 198H")
        self.assertEqual(self.current_product(), "SHARPAL 198H")
        self.assertEqual(self.case_library_bytes(), before)

    def test_cancel_and_escape_discard_selection_without_writes(self):
        self.catalog_change("add", "198H")
        before = self.case_library_bytes()
        page = self.open_detail()
        writes = []
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
        for close in ("cancel", "escape"):
            dialog = self.open_picker(page)
            dialog.locator('[data-product-option="198H"]').click()
            if close == "cancel":
                dialog.locator("[data-product-cancel]").click()
            else:
                page.keyboard.press("Escape")
            dialog.wait_for(state="detached")
            self.assertTrue(page.locator("#detail-product > summary").evaluate("node => node === document.activeElement"))
        self.assertEqual(writes, [])
        self.assertEqual(self.current_product(), "SHARPAL 202H")
        self.assertEqual(self.case_library_bytes(), before)
        self.assertFalse((self.runtime / "evidence" / "case-products.json").exists())

    def test_catalog_failure_requires_retry_and_does_not_offer_stale_case_names(self):
        self.catalog_change("add", "198H")
        page = self.open_detail()
        attempts = []

        def catalog(route):
            attempts.append(route.request.url)
            if len(attempts) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "目录暂时不可用，请重试。"}, ensure_ascii=False))
            else:
                route.continue_()

        page.route("**/api/products", catalog)
        dialog = self.open_picker(page)
        dialog.locator("[data-product-retry]").wait_for()
        self.assertEqual(self.option_values(dialog), [])
        self.assertTrue(dialog.locator("[data-product-save]").is_disabled())
        self.assertIn("加载失败", dialog.inner_text())
        dialog.locator("[data-product-retry]").click()
        dialog.locator('[data-product-option="198H"]').wait_for()
        self.assertEqual(len(attempts), 2)
        self.assertTrue(dialog.locator("[data-product-save]").is_disabled())

    def test_save_failure_keeps_choice_for_explicit_retry(self):
        self.catalog_change("add", "198H")
        page = self.open_detail()
        dialog = self.open_picker(page)
        dialog.locator('[data-product-option="198H"]').click()
        writes = []

        def save(route):
            writes.append(route.request.post_data_json)
            if len(writes) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "保存暂时失败，请重试。"}, ensure_ascii=False))
            else:
                route.continue_()

        page.route(f"**/api/cases/{self.identifier}/product", save)
        dialog.locator("[data-product-save]").click()
        dialog.get_by_text("保存暂时失败，请重试。", exact=False).wait_for()
        self.assertEqual(dialog.locator('[data-product-option="198H"]').get_attribute("aria-selected"), "true")
        self.assertFalse(dialog.locator("[data-product-save]").is_disabled())
        self.assertEqual(self.current_product(), "SHARPAL 202H")
        dialog.locator("[data-product-save]").click()
        dialog.wait_for(state="detached")
        self.assertEqual(writes, [{"product": "SHARPAL 198H"}, {"product": "SHARPAL 198H"}])
        self.assertEqual(self.current_product(), "SHARPAL 198H")

    def test_narrow_dialog_wraps_long_names_and_keeps_actions_reachable(self):
        name = "未核实产品名称" * 45
        self.add_source_product(name)
        before = self.case_library_bytes()
        page = self.open_detail(width=390, height=600)
        dialog = self.open_picker(page)
        dialog.locator("[data-product-search]").fill("未核实")
        option = dialog.locator("[data-product-option]")
        option.wait_for()
        self.assertEqual(option.inner_text().strip(), name)
        box = dialog.bounding_box()
        self.assertGreaterEqual(box["x"], 0)
        self.assertGreaterEqual(box["y"], 0)
        self.assertLessEqual(box["x"] + box["width"], 391)
        self.assertLessEqual(box["y"] + box["height"], 601)
        self.assertFalse(dialog.evaluate("node => node.scrollWidth > node.clientWidth"))
        self.assertFalse(option.evaluate("node => node.scrollWidth > node.clientWidth"))
        self.assert_no_overflow(page)
        option.click()
        dialog.locator("[data-product-save]").click()
        dialog.wait_for(state="detached")
        self.assertEqual(self.current_product(), name, "An existing catalogue name may exceed the new-name limit")
        self.assertEqual(self.case_library_bytes(), before)

    def test_keyboard_selects_draft_and_read_only_modes_do_not_open_picker(self):
        self.catalog_change("add", "198H")
        page = self.open_detail()
        dialog = self.open_picker(page)
        search = dialog.locator("[data-product-search]")
        search.fill("198")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        self.assertEqual(dialog.locator('[data-product-option="198H"]').get_attribute("aria-selected"), "true")
        self.assertEqual(self.current_product(), "SHARPAL 202H")
        self.assertTrue(dialog.is_visible())
        self.assertFalse(dialog.locator("[data-product-save]").is_disabled())
        dialog.locator("[data-product-cancel]").click()
        for flag in ("WorkbenchGuest", "PublicTextMode"):
            with self.subTest(read_only=flag):
                read_only = self.open_detail(read_only=flag)
                summary = read_only.locator("#detail-product > summary")
                self.assertEqual(summary.get_attribute("aria-disabled"), "true")
                summary.evaluate("node => node.click()")
                self.assertEqual(read_only.locator("#case-product-dialog[open]").count(), 0)
                self.assertFalse(read_only.locator("#detail-product").evaluate("node => node.open"))


if __name__ == "__main__":
    unittest.main()
