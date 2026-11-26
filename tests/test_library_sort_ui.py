"""Case-library sort keys: order, missing figures last, and the extra figure shown while sorting by it."""
from __future__ import annotations

import unittest

from test_noise_library_voice_ui import NoiseCaseReadingTests


class LibrarySortTests(unittest.TestCase):
    setUpClass = classmethod(NoiseCaseReadingTests.setUpClass.__func__)
    tearDownClass = classmethod(NoiseCaseReadingTests.tearDownClass.__func__)

    def setUp(self):
        NoiseCaseReadingTests.setUp(self)
        self.page.evaluate(r'''() => {
          window.numeric = value => parseFloat(String(value).replace(/,/g, ""));
          window.librarySortNames = {updated:"最近更新",posted:"发布日期",views:"播放量",impressions:"商品曝光",clicks:"商品点击",ctor:"CTOR",sold:"售出件数",sold_per_k:"每千次播放售出",gmv:"成交金额"};
          Object.assign(state, {query:"", archiveCategory:"all", salesStatus:"all", sourceType:"all", variantType:"all", direction:"", contentOrigin:"all", textForm:"all"});
          const make = (id, updated, metrics, video) => ({case_id:id, product:"202H", updated_at:updated,
            current:{title:id, category:"待验证与整理", metrics:{}},
            sales_data: metrics ? {status:"matched", metrics, video} : {status:"not_found", metrics:{}, video:null}});
          window.items = [
            make("a", "2026-10-01", {views_display:"5000", product_impressions_display:"900", product_clicks_display:"80", ctor_display:"2.5%", items_sold:4},
                 {views:5000, gmv:199.6, posted_on:"2026-09-01"}),
            // Public-page views replace the display figure; the sales table's own views stay in views_shop.
            make("b", "2026-10-03", {views_display:"90000", product_impressions_display:"300", product_clicks_display:"120", ctor_display:"10%", items_sold:6},
                 {views:90000, views_source:"public", views_shop:1000, gmv:320, posted_on:"2026-09-20"}),
            make("c", "2026-10-02", null, null),
          ];
          window.scopedCases = () => window.items;
        }''')

    def order(self, sort, direction="desc"):
        return self.page.evaluate("([sort, dir]) => { state.sort = sort; state.sortDir = dir; return filteredCases().map(item => item.case_id); }",
                                  [sort, direction])

    def test_each_key_orders_cases_and_keeps_missing_figures_last(self):
        self.assertEqual(self.order("updated"), ["b", "c", "a"])
        for sort, first in (("views", "b"), ("impressions", "a"), ("clicks", "b"), ("ctor", "b"), ("sold", "b"), ("gmv", "b"), ("posted", "b")):
            with self.subTest(sort=sort):
                other = "a" if first == "b" else "b"
                self.assertEqual(self.order(sort), [first, other, "c"])
                self.assertEqual(self.order(sort, "asc"), [first, other, "c"] if sort == "ctor" else [other, first, "c"])

    def test_sold_per_thousand_views_uses_sales_table_views(self):
        # a: 4 / 5000 = 0.8 per thousand; b: 6 / 1000 (not the public 90000) = 6 per thousand.
        self.assertEqual(self.order("sold_per_k"), ["b", "a", "c"])
        self.assertEqual(self.page.evaluate("window.items.map(item => librarySortDisplay(item, 'sold_per_k'))"), ["0.8", "6", None])

    def test_extra_figure_shows_only_while_sorting_by_it(self):
        self.order("gmv")
        self.page.evaluate("document.querySelector('#content').innerHTML = listView(filteredCases()) + card(window.items[0])")
        self.assertEqual(self.page.locator("th [data-sort-header='gmv']").count(), 1)
        self.assertEqual(self.page.locator("tbody tr").first.locator("td.num").all_inner_texts(), ["90000", "10%", "6", "$320"])
        self.assertIn("$200", self.page.locator(".card-metrics.has-sort-metric").inner_text())
        self.order("views")
        self.page.evaluate("document.querySelector('#content').innerHTML = listView(filteredCases()) + card(window.items[0])")
        self.assertEqual(self.page.locator("tbody tr").first.locator("td.num").count(), 3)
        self.assertEqual(self.page.locator(".card-metrics.has-sort-metric").count(), 0)

    def test_recent_sort_prioritizes_complete_data_and_filter_uses_status(self):
        self.page.evaluate('''() => {
          const recent=(value,complete)=>({start:'2026-10-01',end:'2026-10-28',required_days:28,
            items:{value,complete,covered_days:complete?28:7},gmv:{value:value*20,complete,covered_days:complete?28:7},
            orders:{value,complete,covered_days:complete?28:7}});
          items[0].library_performance={age_days:168,status:'selling',recent:recent(3,true),campaign:{percent:65,dates_unverified:true}};
          items[1].library_performance={age_days:55,status:'unknown',recent:recent(20,false),campaign:{percent:null,reason:'周表跨活动边界'}};
        }''')
        self.assertEqual(self.order("recent_sold"), ["a", "b", "c"])
        self.assertEqual(self.order("recent_sold", "asc"), ["a", "b", "c"])
        self.assertEqual(self.order("recent_gmv"), ["a", "b", "c"])
        self.page.evaluate("state.salesStatus='selling';document.querySelector('#content').innerHTML=card(filteredCases()[0])")
        self.assertEqual(self.page.evaluate("filteredCases().map(item=>item.case_id)"), ["a"])
        text = self.page.locator(".case-card").inner_text()
        for part in ("发布 168 天", "还在卖", "近 28 天售出 3 件", "已录活动期出单占 65%", "日期待核对"):
            self.assertIn(part, text)

    def test_small_ratio_samples_are_last_even_when_the_rate_is_higher(self):
        self.page.evaluate("items[0].sales_data.metrics.ctor_display='11%'; items[1].sales_data.metrics.ctor_display='2%'")
        self.assertEqual(self.order("ctor"), ["b", "a", "c"])
        self.assertEqual(self.order("ctor", "asc"), ["b", "a", "c"])
        self.page.evaluate("document.querySelector('#content').innerHTML=card(items[0])")
        self.assertIn("CTOR 样本小", self.page.locator(".case-card").inner_text())
        self.assertIn("100", self.page.locator(".library-sample").get_attribute("title"))


if __name__ == "__main__":
    unittest.main()
