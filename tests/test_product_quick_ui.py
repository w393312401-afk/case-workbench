"""常用两款并排切换（198H｜202H｜⋯）和切回产品时回到上次停的那一页。"""
from __future__ import annotations

import json
import unittest

import test_product_combo_ui as combo


class ProductQuickBrowserTests(unittest.TestCase):
    setUpClass = classmethod(combo.ProductComboBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(combo.ProductComboBrowserTests.tearDownClass.__func__)
    setUp = combo.ProductComboBrowserTests.setUp
    stop_server = combo.ProductComboBrowserTests.stop_server
    request = combo.ProductComboBrowserTests.request
    make_playable_video = combo.ProductComboBrowserTests.make_playable_video
    assert_no_overflow = combo.ProductComboBrowserTests.assert_no_overflow
    assert_route = combo.ProductComboBrowserTests.assert_route
    open_page = combo.ProductComboBrowserTests.open_page
    open_picker = combo.ProductComboBrowserTests.open_picker
    catalog_change = combo.ProductComboBrowserTests.catalog_change

    def add_products(self, *codes):
        for code in codes:
            self.catalog_change("add", code)

    def quick_codes(self, page, scope=".sidebar"):
        return page.locator(f"{scope} [data-product-quick]").evaluate_all("nodes => nodes.map(node => node.dataset.productQuick)")

    def wait_quick(self, page, codes, scope=".sidebar"):
        page.wait_for_function(
            "([scope, codes]) => JSON.stringify([...document.querySelectorAll(scope + ' [data-product-quick]')].map(node => node.dataset.productQuick)) === JSON.stringify(codes)",
            arg=[scope, codes],
        )

    def test_two_recent_products_switch_in_one_click_and_keep_their_places(self):
        self.add_products("198H", "129N")
        page = self.open_page()
        # Nothing used yet: the current product and the first other one in the list.
        self.wait_quick(page, ["129N", "202H"])
        page.locator(".product-combo-trigger").click()
        page.locator('.product-combo-panel [data-picker-value="198H"]').click()
        self.assert_route(page, "library", product="198H")
        # The two most recent, ordered by model number so they do not swap on every switch.
        self.wait_quick(page, ["198H", "202H"])
        current = page.locator(".sidebar [data-product-quick].is-current")
        self.assertEqual(current.get_attribute("data-product-quick"), "198H")
        self.assertEqual(current.get_attribute("aria-pressed"), "true")
        page.locator('.sidebar [data-product-quick="202H"]').click()
        self.assert_route(page, "library", product="202H")
        self.wait_quick(page, ["198H", "202H"])
        page.locator('.sidebar [data-product-quick="198H"]').click()
        self.assert_route(page, "library", product="198H")
        self.wait_quick(page, ["198H", "202H"])
        self.assertEqual(json.loads(page.evaluate("localStorage.getItem('workbench-product-recent-v1')"))[:2], ["198H", "202H"])
        # ⋯ is the original list; the trigger shows no name while the current product is one of the two.
        trigger = page.locator(".product-combo-trigger")
        self.assertLess(trigger.bounding_box()["width"], 40)
        self.assertFalse(page.locator(".product-combo-trigger > span").is_visible())
        self.open_picker(page)
        page.keyboard.press("Escape")
        self.assert_no_overflow(page)

    def test_opening_another_products_page_keeps_the_chosen_two(self):
        self.add_products("198H", "129N", "101N")
        page = self.open_page()
        page.locator(".product-combo-trigger").click()
        page.locator('.product-combo-panel [data-picker-value="198H"]').click()
        self.assert_route(page, "library", product="198H")
        self.wait_quick(page, ["198H", "202H"])
        # A link or a detail page of another product switches the product, but the buttons stay as chosen,
        # also after a refresh; the trigger then shows the other product's name.
        page.goto(self.base + "/#/129N/library")
        self.assert_route(page, "library", product="129N")
        page.reload()
        page.locator(".product-combo-trigger").wait_for(state="attached")
        self.wait_quick(page, ["198H", "202H"])
        self.assertEqual(page.locator(".sidebar [data-product-quick].is-current").count(), 0)
        self.assertTrue(page.locator(".product-combo-trigger > span").is_visible())
        page.locator('.sidebar [data-product-quick="202H"]').click()
        self.assert_route(page, "library", product="202H")
        self.wait_quick(page, ["198H", "202H"])

    def test_switching_back_returns_to_the_detail_that_was_open(self):
        self.add_products("198H")
        page = self.open_page()
        self.wait_quick(page, ["198H", "202H"])
        page.goto(self.base + f"/#/202H/case/{self.identifier}")
        page.locator("#edit-case").wait_for()
        page.locator('.sidebar [data-product-quick="198H"]').click()
        # The case belongs to 202H, so 198H opens its library list, as before.
        self.assert_route(page, "library", product="198H")
        page.locator('.sidebar [data-product-quick="202H"]').click()
        page.wait_for_function("id => location.hash.startsWith('#/202H/case/' + id)", arg=self.identifier)
        page.locator("#edit-case").wait_for()
        # A part of the workbench that 202H was never left in still opens its list.
        page.goto(self.base + "/#/202H/reviews")
        page.locator('.sidebar [data-product-quick="198H"]').click()
        self.assert_route(page, "reviews", product="198H")

    def test_folded_sidebar_shows_the_two_buttons_in_the_top_bar(self):
        self.add_products("198H")
        page = self.open_page(preference="collapsed")
        page.wait_for_function("() => document.documentElement.classList.contains('sidebar-collapsed')")
        self.wait_quick(page, ["198H", "202H"], ".topbar")
        topbar = page.locator(".topbar .product-quick")
        self.assertTrue(topbar.is_visible())
        page.locator('.topbar [data-product-quick="198H"]').click()
        self.assert_route(page, "library", product="198H")
        # Expanded, the buttons live in the sidebar only.
        expanded = self.open_page(preference="expanded")
        self.wait_quick(expanded, ["198H", "202H"])
        self.assertFalse(expanded.locator(".topbar .product-quick").is_visible())
        self.assertTrue(expanded.locator(".sidebar .product-quick").is_visible())

    def test_phone_top_bar_fits_the_two_buttons_and_falls_back_when_narrow(self):
        self.add_products("198H")
        for width, quick in ((390, True), (375, True), (260, False)):
            with self.subTest(width=width):
                page = self.open_page(width=width, height=800)
                page.wait_for_function("() => document.querySelector('.sidebar .global-product').classList.contains('has-quick')")
                page.wait_for_timeout(150)
                self.assertEqual(page.locator(".sidebar .product-quick").is_visible(), quick)
                boxes = page.evaluate("""() => {
                  const picker = document.querySelector('.sidebar .global-product').getBoundingClientRect();
                  const others = [document.querySelector('.campaign-runway'), ...document.querySelector('.topbar-right').children]
                    .map(node => node && node.getBoundingClientRect()).filter(box => box && box.width);
                  return { right: picker.right, left: Math.min(...others.map(box => box.left)) };
                }""")
                self.assertLessEqual(boxes["right"], boxes["left"])
                if quick:
                    self.assertEqual(page.locator('.sidebar [data-product-quick="198H"] span').evaluate("node => getComputedStyle(node, '::before').content"), '"198"')
                    # No ⋯ on phones: tapping the product already chosen opens the full list.
                    page.locator('.sidebar [data-product-quick="202H"]').click()
                    page.locator(".product-combo-panel").wait_for()
                    self.assertLessEqual(page.locator(".product-combo-panel").bounding_box()["x"] + page.locator(".product-combo-panel").bounding_box()["width"], width)
                    page.keyboard.press("Escape")
                    page.locator(".product-combo-panel").wait_for(state="hidden")
                    page.locator('.sidebar [data-product-quick="198H"]').click()
                    self.assert_route(page, "library", product="198H")
                else:
                    self.assertTrue(page.locator(".product-combo-trigger > span").is_visible())
                self.assert_no_overflow(page)


if __name__ == "__main__":
    unittest.main()
