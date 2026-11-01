"""Video-list campaign filters against the real app, with disposable data and browser requests."""
from copy import deepcopy
from pathlib import Path
import unittest

import test_performance as performance
import test_review_ui as fixture


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / ".test-artifacts" / "video-campaign-filter"
CAMPAIGN = "TKC-manual-september"
IDS = {name: str(7800000000000000000 + index) for index, name in enumerate(
    ("start", "inside", "end", "after", "unknown", "other-product"), 1)}
IDS["before"] = performance.LINKED


class VideoCampaignFilterTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request

    def setUp(self):
        fixture.ReviewPageTests.setUp(self)
        self.calendar = {"campaigns": [{"id": CAMPAIGN, "name": "人工登记九月活动",
            "start": "2026-09-17", "end": "2026-09-27", "confidence": "manual", "verified": True}],
            "revision": "campaign-r1"}
        self.campaign_requests = 0
        self.payload = self.request("GET", "/api/performance/videos")[2]
        base = self.payload["videos"][0]

        def video(name, posted, end, *, status="follow", account="tester", product="SHARPAL 202H"):
            return {**base, "video_id": IDS[name], "posted_on": posted, "posted_at": posted or "",
                "window_end": end, "headline": name, "title": name, "status": status,
                "status_label": {"follow": "跟进", "watch": "观察"}[status], "account": account,
                "review_status": {"key": "weak" if status == "watch" else "high"},
                "product": product, "case_id": "", "flags": [], "window_views": 5000,
                "items": 999, "gmv": 9999, "ctor_pct": 50.0}

        self.payload["videos"] = [
            video("before", "2026-09-01", "2026-09-16"),
            video("start", "2026-09-10", "2026-09-17"),
            video("inside", "2026-09-18", "2026-10-04", status="watch"),
            video("end", "2026-09-27", "2026-10-11", account="second"),
            video("after", "2026-09-28", "2026-10-11"),
            video("unknown", None, "2026-09-27"),
            video("other-product", "2026-09-18", "2026-10-04", product="SHARPAL 129N"),
        ]
        # [video index, week, views, impressions, clicks, orders, items, gmv]; the activity touches the weeks of 09-14 and 09-21.
        self.analytics = {"videos": [[IDS["start"], "tester", "202H", "2026-09-10"], [IDS["inside"], "tester", "202H", "2026-09-18"]],
            "weekly": [[0, "2026-09-07", 10, 10, 10, 50, 50, 500], [0, "2026-09-14", 10, 10, 100, 5, 5, 50],
                [1, "2026-09-14", 10, 10, 100, 4, 3, 30.25], [1, "2026-09-21", 10, 10, 100, 6, 7, 70.25], [1, "2026-09-28", 10, 10, 10, 100, 100, 1000]],
            "monthly": [], "months": {}, "shop_daily": []}

    def open_list(self, *, width=1100, scheme="light", failures=0, query="tab=list", analytics="ok"):
        context = self.browser.new_context(viewport={"width": width, "height": 900},
                                           color_scheme=scheme, timezone_id="Asia/Shanghai")
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.route("**/api/performance/videos?*", lambda route: route.fulfill(json=self.payload))
        self.held_analytics = []
        page.route("**/api/performance/analytics?*", {
            "ok": lambda route: route.fulfill(json=self.analytics),
            "fail": lambda route: route.fulfill(status=503, json={"error": "测试周数据读取失败"}),
            "hold": lambda route: self.held_analytics.append(route),
        }[analytics])

        def campaigns(route):
            if route.request.method == "POST":
                changed = route.request.post_data_json["campaign"]
                self.calendar["campaigns"] = [
                    {**row, **changed} if row["id"] == changed["id"] else row
                    for row in self.calendar["campaigns"]]
                self.calendar["revision"] = "campaign-r2"
                route.fulfill(json=deepcopy(self.calendar))
                return
            self.campaign_requests += 1
            if self.campaign_requests <= failures:
                route.fulfill(status=503, json={"error": "测试活动读取失败"})
            else:
                route.fulfill(json=deepcopy(self.calendar))

        page.route("**/api/campaigns", campaigns)
        page.goto(f"{self.base}/#/202H/videos?{query}")
        page.locator("#perf-campaign").wait_for()
        return page

    def choose(self, page):
        page.locator(f'#perf-campaign option[value="{CAMPAIGN}"]').wait_for(state="attached")
        page.locator("#perf-campaign").select_option(CAMPAIGN)

    def ids(self, page):
        return set(page.locator("[data-video-row]").evaluate_all("rows => rows.map(row => row.dataset.videoRow)"))

    def wait_ids(self, page, names):
        expected = {IDS[name] for name in names}
        page.wait_for_function("expected => JSON.stringify([...document.querySelectorAll('[data-video-row]')].map(row => row.dataset.videoRow).sort()) === JSON.stringify(expected.sort())", arg=list(expected))
        self.assertEqual(self.ids(page), expected)

    def test_direct_list_overlap_boundaries_and_combined_filters(self):
        page = self.open_list()
        self.choose(page)
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertIn("3 条", page.locator(".perf-toolbar .result-count").inner_text())
        self.assertIn("观察窗", page.locator("#perf-campaign-hint").inner_text())
        self.assertEqual(page.locator('[data-status-filter="high"] strong').inner_text(), "2")
        page.locator("#perf-account").select_option("tester")
        self.wait_ids(page, ["start", "inside"])
        page.locator('.perf-legend-item[data-status-filter="weak"]').click()
        self.wait_ids(page, ["inside"])
        page.locator("#perf-q").fill("no match")
        self.wait_ids(page, [])
        page.locator("#perf-reset").click()
        self.wait_ids(page, ["before", "start", "inside", "end", "after", "unknown"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")
        self.assertTrue(page.locator("#perf-reset").is_hidden())
        # A date without an activity selects publication days instead of observation-window overlap.
        page.goto(f"{self.base}/#/202H/videos?tab=list&period=2026-09-17~2026-09-27")
        self.wait_ids(page, ["inside", "end"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), "")

    def sales(self, page, name):
        return page.locator(f'[data-video-row="{IDS[name]}"] td.num').evaluate_all(
            "cells => cells.slice(2, 5).map(cell => cell.textContent.trim())")

    def headers(self, page):
        return [page.locator(f'[data-sort="{key}"]').text_content()[:-1] for key in ("items", "gmv", "ctor")]

    def test_campaign_sales_columns_cover_its_weeks_and_sort_by_them(self):
        page = self.open_list()
        self.choose(page)
        self.wait_ids(page, ["start", "inside", "end"])
        page.wait_for_function("document.querySelector('[data-sort=items]').textContent.startsWith('活动周')")
        self.assertEqual(self.headers(page), ["活动周售出", "活动周 GMV", "活动周 CTOR"])
        self.assertIn("2026-09-14 至 2026-09-27", page.locator("#perf-campaign-hint").inner_text())
        # Weeks outside the activity are left out, and a whole week still counts when only part of it overlaps.
        self.assertEqual(self.sales(page, "start"), ["5", "$50", "5.00%"])
        self.assertEqual(self.sales(page, "inside"), ["10", "$101", "5.00%"])
        self.assertEqual(self.sales(page, "end"), ["—", "—", "—"])
        cell = page.locator(f'[data-video-row="{IDS["inside"]}"] td.num').nth(2)
        self.assertEqual(cell.get_attribute("title"), "累计售出 999")
        page.locator('[data-sort="items"]').click()
        order = "rows => rows.map(row => row.dataset.videoRow)"
        self.assertEqual(page.locator("[data-video-row]").evaluate_all(order), [IDS["inside"], IDS["start"], IDS["end"]])
        # Without the activity the same columns return to the video's own totals.
        page.locator("#perf-campaign").select_option("")
        self.wait_ids(page, ["before", "start", "inside", "end", "after", "unknown"])
        self.assertEqual(self.headers(page), ["售出", "GMV", "CTOR"])
        self.assertEqual(self.sales(page, "inside"), ["999", "$9,999", "50.00%"])
        self.assertIsNone(page.locator(f'[data-video-row="{IDS["inside"]}"] td.num').nth(2).get_attribute("title"))

    def test_picked_date_sales_columns_cover_its_weeks_while_videos_stay_by_publication_date(self):
        page = self.open_list(query="tab=list&period=2026-09-17~2026-09-20")
        self.wait_ids(page, ["inside"])
        page.wait_for_function("document.querySelector('[data-sort=items]').textContent.startsWith('所选周')")
        self.assertEqual(self.headers(page), ["所选周售出", "所选周 GMV", "所选周 CTOR"])
        self.assertIn("2026-09-14 至 2026-09-20", page.locator("#perf-date-note").inner_text())
        # Only the week of 09-14 is touched, so the following weeks' sales are left out.
        self.assertEqual(self.sales(page, "inside"), ["3", "$30", "4.00%"])
        cell = page.locator(f'[data-video-row="{IDS["inside"]}"] td.num').nth(2)
        self.assertEqual(cell.get_attribute("title"), "累计售出 999")
        # A wider date keeps the same columns; clearing it returns to totals with no note.
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-preset="all"]').click()
        self.wait_ids(page, ["before", "start", "inside", "end", "after", "unknown"])
        self.assertEqual(self.headers(page), ["售出", "GMV", "CTOR"])
        self.assertEqual(self.sales(page, "inside"), ["999", "$9,999", "50.00%"])
        self.assertEqual(page.locator("#perf-date-note").count(), 0)
        # Other tabs have no sales columns of this kind and no note.
        page.goto(f"{self.base}/#/202H/videos?tab=board&period=2026-09-17~2026-09-20")
        page.locator("#perf-board").wait_for()
        self.assertEqual(page.locator("#perf-date-note").count(), 0)

    def test_picked_date_sales_say_they_are_totals_until_weeks_load(self):
        page = self.open_list(query="tab=list&period=2026-09-17~2026-09-20", analytics="hold")
        self.wait_ids(page, ["inside"])
        self.assertEqual(self.headers(page), ["售出", "GMV", "CTOR"])
        self.assertIn("暂为累计值", page.locator("#perf-date-note").inner_text())
        while not self.held_analytics:
            page.wait_for_timeout(50)
        self.held_analytics[0].fulfill(json=self.analytics)
        page.wait_for_function("document.querySelector('[data-sort=items]').textContent.startsWith('所选周')")
        self.assertIn("鼠标悬停", page.locator("#perf-date-note").inner_text())
        self.assertEqual(self.sales(page, "inside"), ["3", "$30", "4.00%"])

    def test_campaign_sales_say_they_are_totals_until_weeks_load_and_when_they_fail(self):
        page = self.open_list(query="tab=list&campaign=" + CAMPAIGN, analytics="hold")
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(self.headers(page), ["售出", "GMV", "CTOR"])
        self.assertEqual(self.sales(page, "inside"), ["999", "$9,999", "50.00%"])
        self.assertIn("暂为累计值", page.locator("#perf-campaign-hint").inner_text())
        while not self.held_analytics:
            page.wait_for_timeout(50)
        self.held_analytics[0].fulfill(json=self.analytics)
        page.wait_for_function("document.querySelector('[data-sort=items]').textContent.startsWith('活动周')")
        self.assertEqual(self.sales(page, "inside"), ["10", "$101", "5.00%"])
        self.assertIn("鼠标悬停", page.locator("#perf-campaign-hint").inner_text())
        failed = self.open_list(query="tab=list&campaign=" + CAMPAIGN, analytics="fail")
        self.wait_ids(failed, ["start", "inside", "end"])
        failed.wait_for_function("document.querySelector('#perf-campaign-hint')?.textContent.includes('暂时读不到')")
        self.assertEqual(self.headers(failed), ["售出", "GMV", "CTOR"])
        self.assertEqual(self.sales(failed, "inside"), ["999", "$9,999", "50.00%"])

    def test_campaign_persists_on_reload_sidebar_return_and_video_deep_link(self):
        page = self.open_list()
        self.choose(page)
        self.wait_ids(page, ["start", "inside", "end"])
        page.reload()
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        captured = page.evaluate("CasePerformance.captureListState()")
        self.assertEqual(captured["filters"]["campaign"], CAMPAIGN)
        self.assertIn("campaign=" + CAMPAIGN, captured["hash"])
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        page.locator('[data-page="videos"]').click()
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        page.goto(f"{self.base}/#/202H/videos?tab=list&video={IDS['before']}")
        page.locator("#perf-video-drawer[open]").wait_for()
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        self.wait_ids(page, ["start", "inside", "end"])
        # The drawer's reads hold the temporary database open; let them finish before the fixture removes it.
        page.wait_for_load_state("networkidle")

    def test_failure_retry_and_deleted_saved_campaign(self):
        page = self.open_list(failures=1)
        page.locator("[data-perf-campaign-retry]").wait_for()
        self.wait_ids(page, ["before", "start", "inside", "end", "after", "unknown"])
        page.locator("[data-perf-campaign-retry]").click()
        self.choose(page)
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(self.campaign_requests, 2)
        self.calendar["campaigns"] = []
        page.reload()
        page.wait_for_function("document.querySelector('#perf-campaign')?.value === ''")
        self.wait_ids(page, ["before", "start", "inside", "end", "after", "unknown"])
        self.assertEqual(page.evaluate("JSON.parse(localStorage.getItem('workbench-video-data-filters')).campaign"), "")

    def test_saved_campaign_failure_retains_selection_disables_export_and_can_retry_or_clear(self):
        page = self.open_list(failures=1, query="tab=list&campaign=" + CAMPAIGN)
        page.locator("[data-perf-campaign-retry]").wait_for()
        self.assertEqual(page.evaluate("CasePerformance.captureListState().filters.campaign"), CAMPAIGN)
        self.assertTrue(page.locator("#perf-reset").is_visible())
        self.assertTrue(page.locator("#perf-export").is_disabled())
        self.assertEqual(self.ids(page), set())
        self.assertIn("无法读取", page.locator("#perf-results").inner_text())
        page.locator("[data-perf-campaign-retry]").click()
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        self.assertTrue(page.locator("#perf-export").is_enabled())
        # Clearing is also available while the saved selection still cannot be read.
        another = self.open_list(failures=self.campaign_requests + 1, query="tab=list&campaign=" + CAMPAIGN)
        another.locator("[data-perf-campaign-retry]").wait_for()
        another.locator("#perf-reset").click()
        self.wait_ids(another, ["before", "start", "inside", "end", "after", "unknown"])
        self.assertEqual(another.evaluate("CasePerformance.captureListState().filters.campaign"), "")
        self.assertTrue(another.locator("#perf-export").is_enabled())

    def test_board_retry_and_date_edit_refresh_the_same_list_campaign(self):
        page = self.open_list(failures=1, query="tab=list&campaign=" + CAMPAIGN)
        page.locator("[data-perf-campaign-retry]").wait_for()
        page.locator("#perf-tab-board").click()
        page.locator("[data-perf-campaign-retry]").click()
        page.locator(f'[data-campaign-id="{CAMPAIGN}"]').wait_for(state="attached")
        page.locator("#perf-tab-list").click()
        self.wait_ids(page, ["start", "inside", "end"])
        self.assertEqual(page.locator("[data-perf-campaign-retry]").count(), 0)
        page.locator("#perf-tab-board").click()
        page.locator("#perf-campaigns .campaign-past > summary").click()
        page.locator(f'[data-campaign-edit="{CAMPAIGN}"]').click()
        dialog = page.locator("dialog.campaign-dialog")
        dialog.locator('[name="start"]').fill("2026-09-28")
        dialog.locator('[name="end"]').fill("2026-10-11")
        dialog.get_by_role("button", name="保存", exact=True).click()
        dialog.wait_for(state="detached")
        page.locator("#perf-tab-list").click()
        self.wait_ids(page, ["inside", "end", "after"])
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        self.assertIn("2026-09-28", page.locator("#perf-campaign-hint").inner_text())
        self.assertIn("2026-10-11", page.locator("#perf-campaign-hint").inner_text())

    def test_export_uses_the_same_filtered_ids_and_other_tabs_and_shops_share_campaign(self):
        page = self.open_list()
        self.choose(page)
        self.wait_ids(page, ["start", "inside", "end"])
        # Capture the selected IDs without creating or downloading an export file.
        page.route("**/api/performance/public-export", lambda route: route.fulfill(status=400, json={"error": "测试只检查导出范围"}))
        with page.expect_request("**/api/performance/public-export") as exported:
            page.locator("#perf-export").click()
        self.assertEqual(set(exported.value.post_data_json["video_ids"]), self.ids(page))
        page.locator("#perf-tab-board").click()
        self.assertTrue(page.locator("#perf-campaign").is_visible())
        self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
        self.assertTrue(page.locator("#perf-reset").is_visible())
        for tab in ("photos", "creators"):
            page.locator(f"#perf-tab-{tab}").click()
            self.assertTrue(page.locator("#perf-campaign").is_visible())
            self.assertEqual(page.locator("#perf-campaign").input_value(), CAMPAIGN)
            self.assertTrue(page.locator("#perf-reset").is_visible())
        page.locator("#perf-tab-list").click()
        self.wait_ids(page, ["start", "inside", "end"])
        page.locator('[data-perf-shop="cross"]').click()
        page.wait_for_function("() => JSON.parse(localStorage.getItem('workbench-video-data-filters')).shop === 'cross'")
        self.assertEqual(page.evaluate("Campaigns.selection()"), CAMPAIGN)

    def test_light_dark_and_mobile_layout(self):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        for scheme, width in (("light", 1100), ("dark", 1100), ("light", 390), ("dark", 390)):
            with self.subTest(scheme=scheme, width=width):
                page = self.open_list(width=width, scheme=scheme)
                self.choose(page)
                self.wait_ids(page, ["start", "inside", "end"])
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                bounds = page.locator(".perf-campaign-filter").bounding_box()
                self.assertGreaterEqual(bounds["x"], 0)
                self.assertLessEqual(bounds["x"] + bounds["width"], width)
                page.screenshot(path=str(ARTIFACTS / f"list-{scheme}-{width}.png"), full_page=True)


if __name__ == "__main__":
    unittest.main()
