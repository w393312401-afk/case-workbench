"""The sales data page: shop switch on top, each shop read on its own, photo tab."""
from __future__ import annotations

import unittest

import test_performance as exports
import test_performance_creators as creators
import test_performance_shops as shops
import test_server as fixture
import test_ui_regressions as browser

LINKED, CROSS_VIDEO = exports.LINKED, shops.CROSS_VIDEO


class ShopPageTests(unittest.TestCase):
    setUp_ = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    cross = True
    product_table = True

    def setUp(self):
        self.setUp_()
        self.folder = self.root / "exports"
        local, cross = self.folder / "本土店", self.folder / "跨境店"
        exports.export(local / "视频" / "第一周视频.xlsx", "2026-08-31", "2026-09-06", [exports.row(LINKED, 8000, 3, 200, 450.0)])
        exports.export(local / "视频" / "第二周视频.xlsx", "2026-09-07", "2026-09-13", [exports.row(LINKED, 4000, 1, 100, 150.0)])
        if self.product_table:
            shops.photo_export(local / "图文" / "第二周图文.xlsx", "2026-10-12", "2026-10-18")
        shops.item_export(local / "图文" / "9月" / "图文明细.xlsx", "2026-09-01", "2026-09-30")
        # An all-creator export for September: alice sells (100), the brand handle is ours and never counts as a creator.
        exports.export(local / "达人" / "9月达人视频.xlsx", "2026-09-01", "2026-09-30", [
            creators.creator_row("8100000000000000001", "alice", 100.0, 2, 1000, 40, posted="2026/09/10 10:00:00"),
            creators.creator_row("8100000000000000002", "sharpal.usa", 50.0, 1, posted="2026/09/10 10:00:00")], creators.CREATOR_HEADER)
        # One of the two posts was registered on a photo production.
        self.server.app.carousel_post_links = lambda: {"7685397373245312269": "CAR-0001"}
        if self.cross:
            exports.export(cross / "视频" / "第一周视频.xlsx", "2026-09-07", "2026-09-13", [exports.row(CROSS_VIDEO, 700, 2, 40, 80.0)])
            shops.photo_export(cross / "图文" / "第一周图文.xlsx", "2026-10-12", "2026-10-18",
                               [["SHARPAL 202H Precision Kitchen Knife Sharpening System", "1732157836537861037", "40", "1", "900", "0.02", "0.05", "44"]],
                               shop_name="SHARPAL GLOBAL")
        self.server.app.performance.sources = [self.folder]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[2]["failed"], [])

    def open_data(self, product="all"):
        page = self.open_page(f"#/{product}/videos")
        page.locator(".perf-shops").wait_for()
        return page

    def test_shop_bar_leads_the_page_and_each_shop_is_read_on_its_own(self):
        page = self.open_data()
        self.assertEqual(page.locator(".perf-heading h1").inner_text(), "带货数据")
        self.assertEqual(page.locator("#breadcrumb").inner_text(), "带货数据")
        self.assertEqual(page.locator("[data-perf-shop]").all_inner_texts(), ["本土店\n导出至 10-18", "跨境店\n导出至 10-18"])
        self.assertEqual(page.locator('[data-perf-shop="local"]').get_attribute("aria-selected"), "true")
        page.locator(".perf-kpis").wait_for()
        page.locator(".perf-tabs [data-perf-tab=list]").click()
        page.locator(".perf-table").wait_for()
        self.assertIn("Is A Mirror Edge Overrated", page.locator(".perf-table").inner_text())
        page.locator('[data-perf-shop="cross"]').click()
        page.locator('[data-perf-shop="cross"][aria-selected="true"]').wait_for()
        page.locator(".perf-table").wait_for()
        text = page.locator(".perf-table").inner_text()
        self.assertIn("Angle guide demo", text)
        self.assertNotIn("Mirror Edge", text)
        self.assertEqual(page.locator("#perf-account option").all_inner_texts(), ["全部账号", "sharpal_us"])
        self.assertIn("跨境店：", page.locator(".perf-heading p").inner_text())
        # The choice sticks while the page is open, and the first shop comes back whole.
        page.locator('[data-perf-shop="local"]').click()
        page.locator('[data-perf-shop="local"][aria-selected="true"]').wait_for()
        page.locator(".perf-table").wait_for()
        self.assertIn("Mirror Edge", page.locator(".perf-table").inner_text())
        self.assertEqual(page.locator("#perf-account option").all_inner_texts(), ["全部账号", "bushcraftedge"])

    def test_photo_tab_shows_the_shops_photo_sales_apart_from_video(self):
        page = self.open_data()
        self.assertEqual(page.locator(".perf-tabs [role=tab]").all_inner_texts(), ["数据看板", "视频明细", "图文", "海外达人"])
        page.locator(".perf-tabs [data-perf-tab=photos]").click()
        page.locator("#perf-photos .perf-kpis").wait_for()
        kpis = page.locator("#perf-photos .perf-kpi").all_inner_texts()
        self.assertEqual(len(kpis), 4)
        self.assertIn("$1,422", kpis[0])  # 1,094.32 + 300.2 + 27.53
        self.assertIn("16", kpis[1])  # 8 + 6 + 2 orders
        text = page.locator("#perf-photos").inner_text()
        self.assertIn("每千次曝光", text)
        self.assertIn("未归属商品", text)
        self.assertIn("202H", text)
        self.assertGreaterEqual(page.locator("#perf-photos svg.perf-chart").count(), 4)
        self.assertEqual(page.locator("#perf-account").count(), 0)  # accounts mean nothing for photo exports
        page.locator('[data-perf-shop="cross"]').click()
        page.locator('[data-perf-shop="cross"][aria-selected="true"]').wait_for()
        page.locator("#perf-photos .perf-kpis").wait_for()
        self.assertIn("$40", page.locator("#perf-photos .perf-kpi").first.inner_text())
        self.assertNotIn("未归属商品", page.locator("#perf-photos section", has_text="商品表现").locator("table").first.inner_text())

    def test_per_post_table_lists_each_photo_and_links_its_production(self):
        page = self.open_data()
        page.locator(".perf-tabs [data-perf-tab=photos]").click()
        page.locator("#perf-photos .perf-kpis").wait_for()
        section = page.locator("#perf-photos section", has_text="逐条图文")
        section.wait_for()
        text = section.inner_text()
        self.assertIn("2 条", text)
        self.assertIn("Kitchen knife tip", text)
        self.assertIn("@bladesharptrevor", text)
        self.assertIn("（没有文案）", text)  # a post without text is shown as such
        # Every post opens on TikTok from its text; only the registered one also links its production.
        posts = section.locator('a[href^="https://www.tiktok.com/"]')
        self.assertEqual(posts.count(), 2)
        self.assertEqual(posts.first.get_attribute("href"), "https://www.tiktok.com/@sharpal_us/photo/7685397373245312269")
        self.assertEqual(posts.first.get_attribute("target"), "_blank")
        link = section.locator("a.perf-case-link", has_text="CAR-")
        self.assertEqual((link.count(), link.inner_text()), (1, "CAR-0001"))
        self.assertTrue(link.get_attribute("href").endswith("/production/CAR-0001"))
        self.assertIn("120.50", text)  # GMV; the second "items sold" column (9) is never read
        # The product filter keeps the posts that link the product.
        page.locator("#global-product").select_option("202H")
        page.locator("#perf-photos section", has_text="1 条").wait_for()
        self.assertIn("@bladesharptrevor", page.locator("#perf-photos section", has_text="逐条图文").inner_text())

    def test_board_opens_with_three_sources_side_by_side_and_a_monthly_comparison(self):
        page = self.open_data()
        cards = page.locator(".perf-sources .perf-source")
        cards.first.wait_for()
        self.assertEqual(cards.count(), 3)
        # Photo and creator figures arrive after the charts; the strip fills in by itself.
        page.wait_for_function("() => [...document.querySelectorAll('.perf-source')].every(card => !card.textContent.includes('正在读取'))")
        texts = cards.all_inner_texts()
        self.assertIn("自制视频", texts[0])
        self.assertIn("$600", texts[0])      # 450 + 150
        self.assertIn("$121", texts[1])      # 120.5
        self.assertIn("售出 3", texts[1])     # first "items sold" column only
        self.assertIn("$100", texts[2])      # alice only; the brand handle is not a creator
        self.assertIn("不含自有账号", texts[2])
        self.assertEqual(page.locator("#perf-board .perf-kpi").count(), 6)  # the six video tiles are unchanged
        fold = page.locator("details.perf-fold", has_text="按月对比")
        fold.locator("summary").click()
        row = fold.locator("tbody tr").first
        self.assertEqual(row.locator("td").all_inner_texts()[:4], ["9月", "$600", "$121", "$100"])
        self.assertIn("视频为周报表合计", row.inner_text())
        self.assertGreaterEqual(fold.locator("svg.perf-chart path.cat-2").count(), 1)
        # A card opens the tab behind it.
        cards.nth(1).click()
        page.locator("#perf-tab-photos[aria-selected=true]").wait_for()

    def test_account_filter_does_not_reach_the_creators(self):
        page = self.open_data()
        page.wait_for_function("() => [...document.querySelectorAll('.perf-source')].every(card => !card.textContent.includes('正在读取'))")
        page.locator("#perf-account").select_option("bushcraftedge")
        card = page.locator(".perf-sources .perf-source")
        card.nth(2).get_by_text("不受账号筛选影响").wait_for()
        self.assertIn("$100", card.nth(2).inner_text())  # creators unchanged
        self.assertIn("$0", card.nth(1).inner_text())    # none of the photo posts is bushcraftedge's

    def test_product_filter_leaves_out_unattributed_photo_sales(self):
        page = self.open_data()
        page.locator(".perf-tabs [data-perf-tab=photos]").click()
        page.locator("#perf-photos .perf-kpis").wait_for()
        self.assertIn("未归属商品", page.locator("#perf-photos section", has_text="商品表现").locator("table").first.inner_text())
        page.locator("#global-product").select_option("202H")
        page.locator("#perf-photos .perf-kpi").first.wait_for()
        self.assertIn("$1,094", page.locator("#perf-photos .perf-kpi").first.inner_text())
        self.assertNotIn("未归属商品", page.locator("#perf-photos section", has_text="商品表现").locator("table").first.inner_text())


class EmptyShopPageTests(ShopPageTests):
    """The cross-border folder has nothing yet: its tab says where files go, and nothing leaks over from the local shop."""
    cross = False

    def test_shop_bar_leads_the_page_and_each_shop_is_read_on_its_own(self):
        page = self.open_data()
        self.assertEqual(page.locator('[data-perf-shop="cross"]').inner_text(), "跨境店\n暂无数据")
        page.locator('[data-perf-shop="cross"]').click()
        page.locator(".perf-board, .empty").first.wait_for()
        page.get_by_text("跨境店还没有数据").wait_for()
        self.assertIn("「跨境店」子文件夹", page.locator("#content .empty").inner_text())
        self.assertEqual(page.locator(".perf-table").count(), 0)
        page.locator('[data-perf-shop="local"]').click()
        page.locator(".perf-kpis").wait_for()

    test_photo_tab_shows_the_shops_photo_sales_apart_from_video = None
    test_product_filter_leaves_out_unattributed_photo_sales = None
    test_per_post_table_lists_each_photo_and_links_its_production = None
    test_board_opens_with_three_sources_side_by_side_and_a_monthly_comparison = None
    test_account_filter_does_not_reach_the_creators = None


class PostsOnlyPageTests(ShopPageTests):
    """Only the per-post export is on disk (no per-product weekly table): the tab shows the posts and nothing breaks."""
    product_table = False
    cross = False

    def test_photo_tab_shows_only_the_per_post_table(self):
        page = self.open_data()
        page.locator(".perf-tabs [data-perf-tab=photos]").click()
        page.locator("#perf-photos section", has_text="逐条图文").wait_for()
        self.assertEqual(page.locator("#perf-photos section h2").all_inner_texts(), ["逐条图文"])
        self.assertEqual(page.locator("#perf-photos .perf-kpis").count(), 0)
        self.assertIn("2 条", page.locator("#perf-photos section").inner_text())

    test_shop_bar_leads_the_page_and_each_shop_is_read_on_its_own = None
    test_photo_tab_shows_the_shops_photo_sales_apart_from_video = None
    test_product_filter_leaves_out_unattributed_photo_sales = None
    test_per_post_table_lists_each_photo_and_links_its_production = None
    test_board_opens_with_three_sources_side_by_side_and_a_monthly_comparison = None
    test_account_filter_does_not_reach_the_creators = None


if __name__ == "__main__":
    unittest.main()
