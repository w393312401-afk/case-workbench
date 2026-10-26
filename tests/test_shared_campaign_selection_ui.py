"""One selected campaign follows review, video, dashboard, photo, and creator pages."""
import unittest

import test_board_campaign_filter_ui as fixture
import test_performance as performance


CAMPAIGN, SECOND, PERIOD = fixture.CAMPAIGN, fixture.SECOND, fixture.PERIOD
LINKED = performance.LINKED
ANGLE, NEWER, OLDER = "7600000000000000002", "7600000000000000003", "7600000000000000004"


class SharedCampaignSelectionTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BoardCampaignFilterTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BoardCampaignFilterTests.tearDownClass.__func__)
    setUp = fixture.BoardCampaignFilterTests.setUp
    stop_server = fixture.BoardCampaignFilterTests.stop_server
    request = fixture.BoardCampaignFilterTests.request
    open_board = fixture.BoardCampaignFilterTests.open_board
    choose = fixture.BoardCampaignFilterTests.choose
    filters = fixture.BoardCampaignFilterTests.filters
    wait_gmv = fixture.BoardCampaignFilterTests.wait_gmv

    def home(self, page):
        page.locator('[data-page="home"]').click()
        page.locator('[data-home-tab="due"]').click()
        controls = page.locator('.followup-controls')
        controls.wait_for()
        if controls.get_attribute('open') is None:
            controls.locator(':scope > summary').click()
        page.locator('[data-followup-campaign]').wait_for()
        page.locator('[data-followup-filter]').select_option("all")

    def tab(self, page, tab):
        if page.locator("#perf-tab-" + tab).count() == 0:
            page.locator('[data-page="videos"]').click()
        page.locator("#perf-tab-" + tab).click()
        page.locator("#perf-campaign").wait_for()

    def selected(self, page, value, home=False):
        selector = "[data-followup-campaign]" if home else "#perf-campaign"
        page.wait_for_function("({selector,value}) => document.querySelector(selector)?.value === value", arg={"selector": selector, "value": value})
        self.assertEqual(page.evaluate("Campaigns.selection()"), value)

    def ids(self, page, home=False):
        selector = "[data-followup-chain]" if home else "[data-video-row]"
        expression = "rows => rows.map(row => row.dataset.followupChain.split(':')[1])" if home else "rows => rows.map(row => row.dataset.videoRow)"
        return set(page.locator(selector).evaluate_all(expression))

    def wait_ids(self, page, expected, home=False):
        selector = "[data-followup-chain]" if home else "[data-video-row]"
        page.wait_for_function("({selector,expected,home}) => JSON.stringify([...document.querySelectorAll(selector)].map(row => home ? row.dataset.followupChain.split(':')[1] : row.dataset.videoRow).sort()) === JSON.stringify(expected.sort())", arg={"selector": selector, "expected": list(expected), "home": home})
        self.assertEqual(self.ids(page, home), set(expected))

    def test_dashboard_selection_reaches_video_and_review_without_publication_date_loss(self):
        page = self.open_board()
        self.choose(page)
        self.wait_gmv(page, "$150")
        self.tab(page, "list")
        self.selected(page, CAMPAIGN)
        self.assertEqual(self.filters(page)["period"], "all")
        self.wait_ids(page, [LINKED, ANGLE, NEWER])
        # The first two posts predate the activity, but their observation windows still overlap it.
        self.assertIn(LINKED, self.ids(page))
        self.home(page)
        self.selected(page, CAMPAIGN, home=True)
        self.wait_ids(page, [LINKED, ANGLE, NEWER], home=True)

    def test_review_and_each_data_page_update_the_same_selection_in_both_directions(self):
        page = self.open_board()
        for index, tab in enumerate(("list", "board", "photos", "creators")):
            with self.subTest(tab=tab):
                expected, replacement = (CAMPAIGN, SECOND) if index % 2 == 0 else (SECOND, CAMPAIGN)
                self.home(page)
                page.locator(f'[data-followup-campaign] option[value="{expected}"]').wait_for(state="attached")
                page.locator('[data-followup-campaign]').select_option(expected)
                self.tab(page, tab)
                self.selected(page, expected)
                page.locator("#perf-campaign").select_option(replacement)
                self.home(page)
                self.selected(page, replacement, home=True)

    def test_old_sidebar_memory_cannot_override_new_review_selection_or_clear(self):
        page = self.open_board()
        self.choose(page)
        self.home(page)
        page.locator('[data-followup-campaign]').select_option(SECOND)
        # The remembered video URL still described the older activity when this module was left.
        page.reload()
        page.locator('[data-home-tab="due"]').click()
        self.selected(page, SECOND, home=True)
        self.tab(page, "board")
        self.selected(page, SECOND)
        self.wait_gmv(page, "$450")
        page.reload()
        self.selected(page, SECOND)
        self.wait_gmv(page, "$450")
        self.home(page)
        page.locator('[data-followup-campaign]').select_option("")
        self.tab(page, "board")
        self.selected(page, "")
        self.wait_gmv(page, "$600")
        page.reload()
        self.selected(page, "")
        self.home(page)
        self.selected(page, "", home=True)

    def test_video_publication_date_and_review_due_date_remain_independent_of_activity_dates(self):
        page = self.open_board()
        self.choose(page)
        self.tab(page, "list")
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-mode="day"]').click()
        page.locator('#perf-date [data-date="2026-09-09"]').click()
        self.selected(page, CAMPAIGN)
        self.wait_ids(page, [NEWER])
        self.assertEqual(self.filters(page)["list_period"], "2026-09-09~2026-09-09")
        self.tab(page, "photos")
        self.assertEqual(self.filters(page)["period"], PERIOD)
        self.tab(page, "list")
        self.assertEqual(self.filters(page)["period"], "2026-09-09~2026-09-09")
        self.wait_ids(page, [NEWER])
        self.home(page)
        page.locator("#followup-date-trigger").click()
        page.locator('#followup-date [data-mode="day"]').click()
        page.locator('#followup-date [data-date="2026-09-13"]').click()
        self.wait_ids(page, [LINKED, ANGLE], home=True)
        self.tab(page, "list")
        page.locator("#perf-campaign").select_option(SECOND)
        self.home(page)
        self.selected(page, SECOND, home=True)
        self.assertIn("2026-09-13", page.locator("#followup-date-trigger").inner_text())
        self.wait_ids(page, [LINKED, ANGLE], home=True)

    def test_explicit_new_activity_link_updates_global_but_video_drawer_keeps_selection(self):
        page = self.open_board()
        self.choose(page)
        page.goto(f"{self.base}/#/202H/videos?tab=board&board_campaign={SECOND}")
        self.selected(page, SECOND)
        self.wait_gmv(page, "$450")
        self.home(page)
        self.selected(page, SECOND, home=True)
        page.goto(f"{self.base}/#/202H/videos?tab=list&video={OLDER}")
        page.locator("#perf-video-drawer[open]").wait_for()
        self.selected(page, SECOND)
        self.wait_ids(page, [LINKED, ANGLE])
        self.assertIn("Older video", page.locator("#perf-video-drawer").inner_text())
        page.locator('#perf-video-drawer [data-drawer-close]').click()
        self.home(page)
        self.selected(page, SECOND, home=True)

    def test_manual_statistics_and_publication_dates_remain_separate_without_activity(self):
        page = self.open_board()
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-mode="day"]').click()
        page.locator('#perf-date [data-date="2026-09-02"]').click()
        self.wait_gmv(page, "$450")
        self.selected(page, "")
        self.tab(page, "list")
        self.assertEqual(self.filters(page)["period"], "all")
        self.wait_ids(page, [LINKED, ANGLE, NEWER, OLDER])
        page.locator("#perf-date-trigger").click()
        page.locator('#perf-date [data-mode="day"]').click()
        page.locator('#perf-date [data-date="2026-09-09"]').click()
        self.wait_ids(page, [NEWER])
        for refresh in (False, True):
            with self.subTest(refresh=refresh):
                if refresh:
                    page.reload()
                    self.wait_ids(page, [NEWER])
                self.tab(page, "board")
                self.wait_gmv(page, "$450")
                self.assertEqual(self.filters(page)["period"], "2026-09-02~2026-09-02")
                self.tab(page, "list")
                self.wait_ids(page, [NEWER])
                self.assertEqual(self.filters(page)["period"], "2026-09-09~2026-09-09")

    def test_failed_calendar_retains_global_choice_and_deletion_clears_every_page(self):
        page = self.open_board(query="tab=board&board_campaign=" + CAMPAIGN, failures=1)
        page.locator("[data-perf-campaign-retry]").wait_for()
        self.assertEqual(page.evaluate("Campaigns.selection()"), CAMPAIGN)
        self.assertEqual(page.locator("#perf-board .perf-kpi").count(), 0)
        page.locator("[data-perf-campaign-retry]").click()
        self.wait_gmv(page, "$150")
        self.home(page)
        self.selected(page, CAMPAIGN, home=True)
        self.calendar["campaigns"] = []
        page.reload()
        page.locator('[data-home-tab="due"]').click()
        self.selected(page, "", home=True)
        for tab in ("list", "board", "photos", "creators"):
            self.tab(page, tab)
            self.selected(page, "")

    def test_latest_search_account_and_status_survive_refresh_with_selected_activity(self):
        page = self.open_board(query="tab=list")
        page.locator("#perf-q").fill("Mirror")
        self.wait_ids(page, [LINKED])
        self.choose(page)
        self.wait_ids(page, [LINKED])
        page.locator("#perf-q").fill("Full Kit")
        self.wait_ids(page, [NEWER])
        page.reload()
        self.wait_ids(page, [NEWER])
        self.selected(page, CAMPAIGN)
        self.assertEqual(page.locator("#perf-q").input_value(), "Full Kit")
        page.locator("#perf-account").select_option("bushcraftedge")
        page.locator('.perf-legend-item[data-status-filter="pending"]').click()
        page.reload()
        self.wait_ids(page, [NEWER])
        self.selected(page, CAMPAIGN)
        self.assertEqual(page.locator("#perf-q").input_value(), "Full Kit")
        self.assertEqual(page.locator("#perf-account").input_value(), "bushcraftedge")
        self.assertEqual(self.filters(page)["status"], "pending")


if __name__ == "__main__":
    unittest.main()
