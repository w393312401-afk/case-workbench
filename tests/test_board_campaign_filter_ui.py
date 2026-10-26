"""Dashboard campaign periods select real weekly, monthly, and daily export data."""
from copy import deepcopy
from pathlib import Path
import re
import unittest

import test_performance as performance
import test_performance_creators as creators
import test_performance_shops as shops
import test_review_ui as fixture


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".test-artifacts" / "board-campaign-filter"
CAMPAIGN = "TKC-manual-september"
SECOND = "TKC-manual-first-week"
PERIOD = "2026-09-08~2026-09-10"


class BoardCampaignFilterTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request

    def setUp(self):
        fixture.ReviewPageTests.setUp(self)
        folder = self.root / "exports"
        for day, gmv in (("2026-09-08", 20), ("2026-09-09", 30), ("2026-09-10", 40), ("2026-09-11", 50)):
            shops.totals(folder / "本土店" / "销售" / f"{day}.xlsx", day, gmv)
        photo = deepcopy(shops.ITEM_ROWS[:1])
        photo[0][5] = "SHARPAL 202H Precision Kit(1732157836537861037)"
        shops.item_export(folder / "本土店" / "图文" / "九月.xlsx", "2026-09-01", "2026-09-30", photo)
        photo[0][4], photo[0][6] = "2026/08/14 06:27:38", "30"
        shops.item_export(folder / "本土店" / "图文" / "八月.xlsx", "2026-08-01", "2026-08-31", photo)
        for month, end, gmv in (("08", "31", 40), ("09", "30", 100)):
            performance.export(folder / "本土店" / "达人" / f"{month}.xlsx", f"2026-{month}-01", f"2026-{month}-{end}",
                [creators.creator_row("8100000000000000001", "alice", gmv, 2, 1000, 40,
                    posted=f"2026/{month}/10 10:00:00")], creators.CREATOR_HEADER)
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        self.calendar = {"campaigns": [
            {"id": CAMPAIGN, "name": "九月三天活动", "start": "2026-09-08", "end": "2026-09-10", "confidence": "manual", "verified": True},
            {"id": SECOND, "name": "九月第一周活动", "start": "2026-09-01", "end": "2026-09-02", "confidence": "manual", "verified": True},
        ], "revision": "campaign-r1"}
        self.campaign_requests = 0

    def open_board(self, *, query="tab=board", failures=0, width=1200, scheme="light"):
        context = self.browser.new_context(viewport={"width": width, "height": 900}, color_scheme=scheme,
                                           timezone_id="Asia/Shanghai")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def campaigns(route):
            if route.request.method == "POST":
                changed = route.request.post_data_json["campaign"]
                self.calendar["campaigns"] = [{**row, **changed} if row["id"] == changed["id"] else row
                                               for row in self.calendar["campaigns"]]
                self.calendar["revision"] = "campaign-r2"
                route.fulfill(json=deepcopy(self.calendar))
                return
            self.campaign_requests += 1
            route.fulfill(status=503, json={"error": "测试活动读取失败"}) if self.campaign_requests <= failures else route.fulfill(json=deepcopy(self.calendar))

        page.route("**/api/campaigns", campaigns)
        page.goto(f"{self.base}/#/202H/videos?{query}")
        page.locator("#perf-campaign").wait_for()
        return page

    def choose(self, page, selected=CAMPAIGN):
        page.locator(f'#perf-campaign option[value="{selected}"]').wait_for(state="attached")
        page.locator("#perf-campaign").select_option(selected)

    def wait_gmv(self, page, value):
        page.wait_for_function("value => document.querySelector('#perf-board [data-perf-metric=gmv] strong')?.textContent === value", arg=value)

    def filters(self, page):
        return page.evaluate("CasePerformance.captureListState().filters")

    def fold(self, page, text):
        return page.locator("details.perf-fold").filter(has=page.locator("summary > span", has_text=re.compile("^" + re.escape(text) + "$")))

    def test_campaign_restricts_weeks_days_and_monthly_sources_with_honest_scope(self):
        page = self.open_board()
        self.wait_gmv(page, "$600")
        self.choose(page)
        self.wait_gmv(page, "$150")
        self.assertEqual(self.filters(page)["period"], PERIOD)
        self.assertEqual(self.filters(page)["board_campaign"], CAMPAIGN)
        trigger = page.locator("#perf-date-trigger").inner_text()
        self.assertIn("2026-09-08", trigger)
        self.assertIn("2026-09-10", trigger)
        hint = page.locator("#perf-campaign-hint").inner_text()
        self.assertIn("周", hint)
        self.assertIn("月", hint)
        page.wait_for_function("() => [...document.querySelectorAll('.perf-source')].every(card => !card.textContent.includes('正在读取'))")
        source_values = page.locator(".perf-sources .perf-source > strong").all_inner_texts()
        self.assertEqual(source_values, ["$150", "$121", "$100"])
        weeks = self.fold(page, "每周数据表")
        weeks.locator("summary").click()
        self.assertEqual(weeks.locator("tbody tr td:first-child").all_inner_texts(), ["2026-09-07 周"])
        shop = self.fold(page, "店铺每日汇总")
        shop.locator("summary").click()
        self.assertIn("$90", shop.inner_text())
        self.assertIn("09-08 至 09-10", shop.inner_text())
        self.assertNotIn("店铺汇总约为", shop.inner_text())
        self.assertNotIn("09-07 至", shop.inner_text())
        comparison = self.fold(page, "按月对比")
        comparison.locator("summary").click()
        self.assertEqual(comparison.locator("tbody tr td:first-child").all_inner_texts(), ["9月"])
        monthly = self.fold(page, "月度汇总")
        monthly.locator("summary").click()
        self.assertEqual(monthly.locator("tbody tr td:first-child").all_inner_texts(), ["2026-09"])

    def test_manual_date_and_clear_restore_unbound_periods(self):
        page = self.open_board()
        self.choose(page)
        self.wait_gmv(page, "$150")
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-mode="day"]').click()
        page.locator('#perf-date [data-date="2026-09-02"]').click()
        self.wait_gmv(page, "$450")
        self.assertEqual(self.filters(page)["board_campaign"], "")
        self.assertEqual(self.filters(page)["period"], "2026-09-02~2026-09-02")
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")
        self.choose(page)
        page.locator("#perf-campaign").select_option("")
        self.wait_gmv(page, "$600")
        self.assertEqual(self.filters(page)["period"], "all")
        self.assertTrue(page.locator("#perf-reset").is_hidden())
        self.choose(page)
        page.locator("#perf-reset").click()
        self.wait_gmv(page, "$600")
        self.assertEqual(self.filters(page)["board_campaign"], "")

    def test_activity_id_persists_through_url_reload_and_sidebar_return(self):
        page = self.open_board(query="tab=board&board_campaign=" + CAMPAIGN)
        self.wait_gmv(page, "$150")
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        state = page.evaluate("CasePerformance.captureListState()")
        self.assertIn("board_campaign=" + CAMPAIGN, state["hash"])
        page.reload()
        self.wait_gmv(page, "$150")
        self.assertEqual(self.filters(page)["period"], PERIOD)
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        page.locator('[data-page="videos"]').click()
        self.wait_gmv(page, "$150")
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)

    def test_board_and_list_share_the_selected_campaign_and_clearing(self):
        page = self.open_board(query="tab=board&campaign=" + SECOND)
        self.wait_gmv(page, "$450")
        self.assertEqual(page.locator("#perf-campaign").input_value(), SECOND)
        self.choose(page)
        self.assertEqual(self.filters(page)["campaign"], CAMPAIGN)
        self.assertEqual(self.filters(page)["board_campaign"], CAMPAIGN)
        page.locator("#perf-tab-list").click()
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        page.locator("#perf-campaign").select_option("")
        page.locator("#perf-tab-board").click()
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")
        self.assertEqual(self.filters(page)["campaign"], "")
        self.wait_gmv(page, "$600")

    def test_selected_campaign_failure_retry_edit_and_deleted_calendar(self):
        page = self.open_board(query="tab=board&board_campaign=" + CAMPAIGN, failures=1)
        page.locator("[data-perf-campaign-retry]").wait_for()
        self.assertEqual(self.filters(page)["board_campaign"], CAMPAIGN)
        self.assertEqual(page.locator("#perf-board .perf-kpi").count(), 0)
        self.assertTrue(page.locator("#perf-reset").is_visible())
        page.locator("[data-perf-campaign-retry]").click()
        self.wait_gmv(page, "$150")
        page.locator("#perf-campaigns .campaign-past > summary").click()
        page.locator(f'[data-campaign-edit="{CAMPAIGN}"]').click()
        dialog = page.locator("dialog.campaign-dialog")
        dialog.locator('[name="start"]').fill("2026-09-01")
        dialog.locator('[name="end"]').fill("2026-09-02")
        dialog.get_by_role("button", name="保存", exact=True).click()
        dialog.wait_for(state="detached")
        self.wait_gmv(page, "$450")
        self.assertEqual(self.filters(page)["period"], "2026-09-01~2026-09-02")
        self.assertIn("2026-09-01", page.locator("#perf-date-trigger").inner_text())
        self.calendar["campaigns"] = []
        page.reload()
        self.wait_gmv(page, "$600")
        self.assertEqual(self.filters(page)["board_campaign"], "")
        self.assertEqual(self.filters(page)["period"], "all")

    def test_light_dark_and_mobile_layout(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for scheme, width in (("light", 1200), ("dark", 1200), ("light", 390), ("dark", 390)):
            with self.subTest(scheme=scheme, width=width):
                page = self.open_board(width=width, scheme=scheme)
                self.choose(page)
                self.wait_gmv(page, "$150")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                date_box = page.locator("#perf-date").bounding_box()
                trigger_box = page.locator("#perf-date-trigger").bounding_box()
                self.assertLessEqual(trigger_box["x"] + trigger_box["width"], date_box["x"] + date_box["width"] + 1,
                                     "The selected date range must not overlap the adjacent account picker")
                page.screenshot(path=str(ARTIFACTS / f"board-{scheme}-{width}.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
