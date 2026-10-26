"""The campaign calendar on the data board, and the campaign note inside a video's review."""
import json
import unittest

import test_feedback_chain as feedback_fixture
import test_plan_variant_binding as plan_fixture
import test_performance as performance
import test_review_ui as review_fixture
from test_ai_tasks import FakeBridge


class CampaignBrowserTests(unittest.TestCase):
    setUpClass = classmethod(review_fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review_fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = review_fixture.ReviewPageTests.stop_server
    request = review_fixture.ReviewPageTests.request
    open_page = review_fixture.ReviewPageTests.open_page
    direction = plan_fixture.PlanVariantHTTPTests.direction
    ok = feedback_fixture.FeedbackChainHTTPTests.ok

    def setUp(self):
        review_fixture.ReviewPageTests.setUp(self)
        self.video_id = performance.LINKED
        self.video = self.server.app.performance.video(self.video_id)
        self.server.app.ai.bridge = FakeBridge(output="好的。")

    def campaigns(self):
        return self.ok("GET", "/api/campaigns")

    def board(self, width=1200):
        return self.open_page(width=width, route="#/202H/videos?tab=board", ready="#perf-campaigns .campaign-row")

    def test_board_lists_every_node_with_how_far_its_date_can_be_trusted(self):
        page = self.board()
        rows = page.locator("#perf-campaigns .campaign-row")
        self.assertEqual(rows.count(), len(self.campaigns()["campaigns"]))
        # Today is past the autumn sale, so it sits in the folded list of finished campaigns.
        page.locator("#perf-campaigns .campaign-past > summary").click()
        bfcm = page.locator('[data-campaign-id="TKC-2026-bfcm"]')
        self.assertIn("官方原文", bfcm.inner_text())
        self.assertIn("11/11 – 11/30", bfcm.inner_text())
        self.assertEqual(bfcm.locator("a").get_attribute("href").split("/")[2], "seller-us.tiktok.com")
        fall = page.locator('[data-campaign-id="TKC-2026-fall-deals"]')
        self.assertIn("说法不一", fall.inner_text())
        self.assertIn("10/2–10/11", fall.inner_text())
        self.assertIn("日期只来自第三方日历或说法不一", page.locator("#perf-campaigns .campaign-warning").inner_text())
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))

    def test_checking_a_date_in_seller_center_is_saved_for_every_device(self):
        page = self.board()
        page.locator("#perf-campaigns .campaign-past > summary").click()
        page.locator('[data-campaign-edit="TKC-2026-fall-deals"]').click()
        dialog = page.locator("dialog.campaign-dialog")
        dialog.locator('[name="start"]').fill("2026-10-06")
        dialog.locator('[name="end"]').fill("2026-10-15")
        dialog.locator('[name="confidence"]').select_option("official")
        dialog.locator('[name="verified"]').check()
        dialog.get_by_role("button", name="保存", exact=True).click()
        row = page.locator('[data-campaign-id="TKC-2026-fall-deals"]')
        row.get_by_text("已核对").wait_for()
        self.assertIn("10/6 – 10/15", row.inner_text())
        saved = next(item for item in self.campaigns()["campaigns"] if item["id"] == "TKC-2026-fall-deals")
        self.assertEqual((saved["start"], saved["end"], saved["confidence"], saved["verified"], saved["edited"]),
                         ("2026-10-06", "2026-10-15", "official", True, True))
        # Defaults come back with one click, and a bad range is refused with the typed text kept.
        page.locator('[data-campaign-edit="TKC-2026-fall-deals"]').click()
        dialog = page.locator("dialog.campaign-dialog")
        dialog.get_by_role("button", name="恢复默认").click()
        page.locator('[data-campaign-id="TKC-2026-fall-deals"] .campaign-trust.is-disputed').wait_for()
        page.locator("[data-campaign-add]").click()
        dialog = page.locator("dialog.campaign-dialog")
        dialog.locator('[name="name"]').fill("手滑的活动")
        dialog.locator('[name="start"]').fill("2026-12-10")
        dialog.locator('[name="end"]').fill("2026-12-01")
        dialog.get_by_role("button", name="保存", exact=True).click()
        dialog.locator("[data-error]").wait_for()
        self.assertEqual(dialog.locator('[name="name"]').input_value(), "手滑的活动")
        self.assertNotIn("手滑的活动", json.dumps(self.campaigns(), ensure_ascii=False))

    def test_checklist_ranks_by_reviews_it_affects_and_settles_each_item_in_place(self):
        page = self.board()
        page.locator("[data-campaign-checklist]").click()
        dialog = page.locator("dialog.campaign-check-dialog")
        dialog.locator(".campaign-check").first.wait_for()
        total = self.server.app.campaigns.public()["campaigns"]
        self.assertEqual(dialog.locator(".campaign-check").count(), len([row for row in total if row["check"] and not row["verified"]]))
        self.assertIn("北京时间时：美国夏令时（3/8 至 11/1）减 15 小时", dialog.inner_text())
        # Each item says what to look at, and where the dates it holds are already used.
        bfcm = dialog.locator('[data-check-id="TKC-2026-bfcm"]')
        self.assertIn("11/30 还是创作者日历写的 12/1", bfcm.inner_text())
        self.assertEqual(dialog.locator('[data-check-id="TKC-2026-fall-deals"] a').first.get_attribute("href").split("/")[2], "www.tiktok.com")
        order = dialog.locator(".campaign-check").evaluate_all("nodes => nodes.map(node => node.dataset.checkId)")
        self.assertEqual(sorted(order), sorted(row["id"] for row in total if row["check"]))
        # Confirming a date keeps it and removes it from the list; the card behind it follows.
        count = len(order)
        bfcm.locator("[data-check-ok]").click()
        page.wait_for_function("count => document.querySelectorAll('dialog.campaign-check-dialog .campaign-check').length === count - 1", arg=count)
        saved = next(row for row in self.campaigns()["campaigns"] if row["id"] == "TKC-2026-bfcm")
        self.assertEqual((saved["verified"], saved["start"], saved["end"]), (True, "2026-11-11", "2026-11-30"))
        # An activity that never ran is deleted from the same list.
        page.once("dialog", lambda confirmation: confirmation.accept())
        dialog.locator('[data-check-id="TKC-2026-jumpstart"] [data-check-drop]').click()
        page.wait_for_function("count => document.querySelectorAll('dialog.campaign-check-dialog .campaign-check').length === count - 2", arg=count)
        self.assertNotIn("TKC-2026-jumpstart", [row["id"] for row in self.campaigns()["campaigns"]])
        # The whole list can be taken to Seller Center as text.
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        dialog.locator("[data-copy]").click()
        page.get_by_text("待核对清单已复制").wait_for()
        copied = page.evaluate("navigator.clipboard.readText()")
        self.assertIn(f"共 {count - 2} 项", copied)
        self.assertIn("要核对：", copied)
        self.assertNotIn("Jumpstart", copied)
        # Editing a date goes through the normal editor.
        dialog.locator('[data-check-id="TKC-2026-fall-deals"] [data-check-edit]').click()
        page.locator("dialog.campaign-dialog [name=name]").wait_for()
        self.assertEqual(page.locator("dialog.campaign-dialog [name=start]").input_value(), "2026-10-04")

    def test_campaign_effect_compares_the_days_inside_with_the_fortnight_before(self):
        page = self.board()
        result = page.evaluate("""() => {
          const days = (from, count, gmv, orders) => Array.from({length: count}, (_, index) => {
            const day = new Date(Date.UTC(2026, 8, 1 + from + index)).toISOString().slice(0, 10);
            return {date: day, gmv, orders, views: 1, clicks: 1};
          });
          const row = {id: 'TKC-test', start: '2026-09-15', end: '2026-09-20'};
          // 14 quiet days at 100/day (Sep 1-14), then 6 campaign days at 250/day.
          const full = [...days(0, 14, 100, 4), ...days(14, 6, 250, 10)];
          return {
            full: Campaigns.impact(row, full),
            thin: Campaigns.impact(row, [...days(0, 5, 100, 4), ...days(14, 6, 250, 10)]),
            nothing: Campaigns.impact(row, []),
          };
        }""")
        self.assertEqual(result["full"]["gmv"], [250, 100])
        self.assertEqual(result["full"]["orders"], [10, 4])
        self.assertTrue(result["thin"]["missing"])
        self.assertIsNone(result["nothing"])

    def test_product_effect_uses_that_products_whole_weeks_and_skips_gaps_and_other_campaigns(self):
        page = self.board()
        for name, start, end in (("2030 大促", "2030-03-04", "2030-03-17"), ("2030 小促", "2030-02-18", "2030-02-19")):
            self.ok("POST", "/api/campaigns", {"action": "save", "revision": self.campaigns()["revision"], "campaign": {
                "name": name, "start": start, "end": end, "confidence": "manual"}})
        result = page.evaluate("""async () => {
          await Campaigns.load(true);
          const find = name => Campaigns.overlapping('2030-01-01', '2030-12-31').find(row => row.name === name);
          const mondays = ['2030-01-14','2030-01-21','2030-01-28','2030-02-04','2030-02-11','2030-02-18','2030-02-25','2030-03-04','2030-03-11'];
          const gmv = {'2030-02-18': 7777, '2030-02-25': 9999, '2030-03-04': 300, '2030-03-11': 300};
          const ctx = (product, only = mondays) => ({
            product,
            matches: value => value === '202H',
            videos: [{video_id: 'A', product: '202H'}, {video_id: 'B', product: '103N'}],
            // 02-25 has a gap in the exports; 02-18 sat inside a small campaign
            weeks: mondays.map(week => ({week, complete: week !== '2030-02-25'})),
            analytics: {videos: [['A'], ['B']], weekly: only.flatMap(week => [
              [0, week, 0, 0, 0, 2, 2, gmv[week] ?? 100], [1, week, 0, 0, 0, 50, 50, 1000]])},
          });
          const row = find('2030 大促');
          return {
            single: Campaigns.weeklyImpact(row, ctx('202H')),
            everyone: Campaigns.weeklyImpact(row, ctx('')),
            short: Campaigns.weeklyImpact(row, {...ctx('202H'), weeks: ctx('202H').weeks.filter(week => week.week >= '2030-02-18')}),
            tiny: Campaigns.weeklyImpact(find('2030 小促'), ctx('202H')),
            none: Campaigns.weeklyImpact(row, null),
          };
        }""")
        self.assertEqual(result["single"]["gmv"], [300, 100], "Only this product's weeks; gap week and campaign week stay out of the baseline")
        self.assertEqual((result["single"]["weeks"], result["single"]["baseline"]), (2, 4))
        self.assertEqual(result["single"]["orders"], [2, 2])
        self.assertEqual(result["everyone"]["gmv"], [1300, 1100], "Without a product every video counts")
        self.assertEqual(result["short"]["missing"], "baseline")
        self.assertEqual(result["tiny"]["missing"], "weeks", "A two-day campaign never fills four days of a week")
        self.assertIsNone(result["none"])

    def test_without_the_calendar_loaded_no_week_is_called_quiet(self):
        page = self.open_page(route="#/202H/directions", ready="#content")
        result = page.evaluate("""() => Campaigns.weeklyImpact({id: 'x', start: '2030-03-04', end: '2030-03-17'}, {
          product: '', matches: () => true, videos: [{video_id: 'A', product: 'p'}], weeks: [{week: '2030-03-04', complete: true}],
          analytics: {videos: [['A']], weekly: [[0, '2030-03-04', 0, 0, 0, 1, 1, 10]]}})""")
        self.assertIsNone(result)

    def test_video_drawer_lists_window_campaigns_on_top_without_reading_advice(self):
        page = self.open_page(route=f"#/202H/videos?video={self.video_id}", ready="[data-feedback-save]")
        self.assertEqual(page.locator("[data-video-campaigns] .feedback-campaigns").count(),
                         1 if self.server.app.campaigns.overlapping(self.video["posted_on"], self.video["window_end"]) else 0)
        revision = self.campaigns()["revision"]
        self.ok("POST", "/api/campaigns", {"action": "save", "revision": revision, "campaign": {
            "name": "窗口里的大促", "start": self.video["window_end"], "end": self.video["window_end"], "confidence": "planning"}})
        page.reload()
        page.locator("[data-feedback-save]").wait_for()
        note = page.locator("[data-video-campaigns] .feedback-campaigns")
        note.wait_for()
        text = note.inner_text()
        self.assertIn("观察窗内有平台活动", text)
        self.assertIn("窗口里的大促", text)
        self.assertIn("待核对", text)
        self.assertNotIn("高客单", text)
        # Above the figures, and not repeated inside the review.
        self.assertTrue(page.evaluate("""() => !!(document.querySelector('[data-video-campaigns]').compareDocumentPosition(
            document.querySelector('[data-video-facts]')) & Node.DOCUMENT_POSITION_FOLLOWING)"""))
        self.assertEqual(page.locator("#video-feedback-chain .feedback-campaigns").count(), 0)

if __name__ == "__main__":
    unittest.main()
