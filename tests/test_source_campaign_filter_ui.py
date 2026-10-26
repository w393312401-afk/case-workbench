"""Photo and creator campaign controls share dates while keeping report coverage explicit."""
from pathlib import Path
import unittest

import test_board_campaign_filter_ui as board_fixture
import test_performance as performance
import test_performance_creators as creators


CAMPAIGN, SECOND, PERIOD = board_fixture.CAMPAIGN, board_fixture.SECOND, board_fixture.PERIOD
ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".test-artifacts" / "source-campaign-filter"


class SourceCampaignFilterTests(unittest.TestCase):
    setUpClass = classmethod(board_fixture.BoardCampaignFilterTests.setUpClass.__func__)
    tearDownClass = classmethod(board_fixture.BoardCampaignFilterTests.tearDownClass.__func__)
    setUp = board_fixture.BoardCampaignFilterTests.setUp
    stop_server = board_fixture.BoardCampaignFilterTests.stop_server
    request = board_fixture.BoardCampaignFilterTests.request
    choose = board_fixture.BoardCampaignFilterTests.choose
    filters = board_fixture.BoardCampaignFilterTests.filters

    def open_source(self, tab, **options):
        options.setdefault("query", "tab=" + tab)
        return board_fixture.BoardCampaignFilterTests.open_board(self, **options)

    def wait_amount(self, page, tab, amount):
        if tab == "photos":
            page.wait_for_function("amount => document.querySelector('#perf-photos .perf-card-head')?.textContent.includes('GMV ' + amount)", arg=amount)
        else:
            page.wait_for_function("amount => document.querySelector('#perf-creators .perf-kpi strong')?.textContent === amount", arg=amount)

    def assert_report_span(self, page, tab):
        text = page.locator("#perf-" + tab).inner_text()
        self.assertIn("09-01", text)
        self.assertIn("09-30", text)
        self.assertIn("导出", text)
        hint = page.locator("#perf-campaign-hint").inner_text()
        self.assertIn("2026-09-08", hint)
        self.assertIn("2026-09-10", hint)
        self.assertIn("导出", hint)

    def test_photo_and_creator_totals_select_the_touching_report_and_explain_coverage(self):
        for tab, all_amount, selected_amount in (("photos", "$150.50", "$120.50"), ("creators", "$140", "$100")):
            with self.subTest(tab=tab):
                page = self.open_source(tab)
                self.wait_amount(page, tab, all_amount)
                self.choose(page)
                self.wait_amount(page, tab, selected_amount)
                self.assertEqual(self.filters(page)["period"], PERIOD)
                self.assertEqual(self.filters(page)["board_campaign"], CAMPAIGN)
                self.assert_report_span(page, tab)
                if tab == "creators":
                    self.assertEqual(page.locator("#perf-creators .perf-week-data").first.locator("tbody tr").count(), 1)
                page.locator("#perf-campaign").select_option("")
                self.wait_amount(page, tab, all_amount)
                self.assertEqual(self.filters(page)["period"], "all")

    def test_creator_search_sales_scope_pagination_and_drawer_follow_the_activity(self):
        rows = [creators.creator_row("8100000000000000001", "alice", 100, 2, 1000, 40, posted="2026/09/10 10:00:00")]
        rows += [creators.creator_row(str(8200000000000000000 + index), f"bob_{index:03d}", 0, 0, 10, 1,
                                     posted="2026/09/10 10:00:00") for index in range(105)]
        performance.export(self.root / "exports" / "本土店" / "达人" / "09.xlsx", "2026-09-01", "2026-09-30", rows, creators.CREATOR_HEADER)
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        page = self.open_source("creators")
        self.wait_amount(page, "creators", "$140")
        self.assertEqual(page.locator("[data-creator-row]").count(), 1)
        page.locator("#perf-cshow").select_option("all")
        page.wait_for_function("() => document.querySelectorAll('[data-creator-row]').length === 100")
        page.locator("#perf-creator-more").click()
        self.assertEqual(page.locator("[data-creator-row]").count(), 106)
        self.choose(page)
        self.wait_amount(page, "creators", "$100")
        self.assertEqual(page.locator("[data-creator-row]").count(), 100)
        page.locator("#perf-cq").fill("@bob_021")
        page.wait_for_function("() => document.querySelectorAll('[data-creator-row]').length === 1")
        self.assertEqual(page.locator("[data-creator-row]").get_attribute("data-creator-row"), "bob_021")
        page.locator("#perf-cshow").select_option("sold")
        self.assertEqual(page.locator("[data-creator-row]").count(), 0)
        page.locator("#perf-cq").fill("alice")
        page.locator('[data-creator-row="alice"]').wait_for()
        page.locator('[data-creator-row="alice"] td.perf-title').click()
        drawer = page.locator("#perf-video-drawer[open]")
        self.assertEqual(drawer.locator(".perf-video-facts dd").first.inner_text(), "$100.00")
        self.assertIn("09-01", drawer.inner_text())
        self.assertIn("09-30", drawer.inner_text())
        monthly = drawer.locator(".perf-video-section").filter(has=page.get_by_role("heading", name="每月", exact=True))
        self.assertEqual(monthly.locator("tbody tr").count(), 1)
        drawer.get_by_role("button", name="关闭达人详情").click()
        page.locator("#perf-reset").click()
        self.wait_amount(page, "creators", "$140")
        self.assertEqual(page.locator("#perf-cq").input_value(), "")
        self.assertEqual(page.locator("#perf-cshow").input_value(), "sold")
        self.assertEqual(self.filters(page)["board_campaign"], "")

    def test_all_data_pages_share_the_activity_and_clear_together(self):
        page = self.open_source("photos", query="tab=photos&campaign=" + SECOND)
        self.choose(page)
        self.wait_amount(page, "photos", "$120.50")
        for tab in ("creators", "board", "photos"):
            page.locator(f"#perf-tab-{tab}").click()
            self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
            self.assertEqual(self.filters(page)["period"], PERIOD)
        page.locator("#perf-tab-list").click()
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        page.locator("#perf-campaign").select_option("")
        page.locator("#perf-tab-creators").click()
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")
        self.wait_amount(page, "creators", "$140")
        self.assertEqual(self.filters(page)["campaign"], "")
        self.choose(page)
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-mode="day"]').click()
        page.locator('#perf-date [data-date="2026-09-02"]').click()
        self.assertEqual(self.filters(page)["board_campaign"], "")
        self.assertEqual(self.filters(page)["period"], "2026-09-02~2026-09-02")
        page.locator("#perf-tab-photos").click()
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")

    def test_direct_url_reload_and_sidebar_return_restore_activity_on_both_sources(self):
        for tab, amount in (("photos", "$120.50"), ("creators", "$100")):
            with self.subTest(tab=tab):
                page = self.open_source(tab, query=f"tab={tab}&board_campaign={CAMPAIGN}")
                self.wait_amount(page, tab, amount)
                self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
                self.assertIn("board_campaign=" + CAMPAIGN, page.evaluate("CasePerformance.captureListState().hash"))
                page.reload()
                self.wait_amount(page, tab, amount)
                page.locator('[data-page="settings"]').click()
                page.locator(".settings-page").wait_for()
                page.locator('[data-page="videos"]').click()
                self.wait_amount(page, tab, amount)
                self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
                self.assertEqual(self.filters(page)["period"], PERIOD)

    def test_saved_activity_failure_retry_and_deleted_activity_do_not_show_false_totals(self):
        for tab, amount, all_amount in (("photos", "$120.50", "$150.50"), ("creators", "$100", "$140")):
            with self.subTest(tab=tab):
                calendar = self.calendar["campaigns"][:]
                page = self.open_source(tab, query=f"tab={tab}&board_campaign={CAMPAIGN}", failures=self.campaign_requests + 1)
                page.locator("[data-perf-campaign-retry]").wait_for()
                self.assertEqual(self.filters(page)["board_campaign"], CAMPAIGN)
                self.assertEqual(page.locator(f"#perf-{tab} .perf-kpi, #perf-{tab} table").count(), 0)
                self.assertTrue(page.locator("#perf-reset").is_visible())
                page.locator("[data-perf-campaign-retry]").click()
                self.wait_amount(page, tab, amount)
                self.calendar["campaigns"] = []
                page.reload()
                self.wait_amount(page, tab, all_amount)
                self.assertEqual(self.filters(page)["board_campaign"], "")
                self.assertEqual(self.filters(page)["period"], "all")
                self.calendar["campaigns"] = calendar

    def test_light_dark_desktop_and_mobile_layout(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for tab, amount in (("photos", "$120.50"), ("creators", "$100")):
            for scheme, width in (("light", 1200), ("dark", 1200), ("light", 390), ("dark", 390)):
                with self.subTest(tab=tab, scheme=scheme, width=width):
                    page = self.open_source(tab, width=width, scheme=scheme)
                    self.choose(page)
                    self.wait_amount(page, tab, amount)
                    self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                    box = page.locator("#perf-campaign-filter").bounding_box()
                    self.assertGreaterEqual(box["x"], 0)
                    self.assertLessEqual(box["x"] + box["width"], width)
                    date_box, button_box = page.locator("#perf-date").bounding_box(), page.locator("#perf-date-trigger").bounding_box()
                    self.assertLessEqual(button_box["x"] + button_box["width"], date_box["x"] + date_box["width"] + 1)
                    page.screenshot(path=str(ARTIFACTS / f"{tab}-{scheme}-{width}.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
