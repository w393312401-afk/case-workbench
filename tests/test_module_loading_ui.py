"""Loading regressions use isolated records and deliberately held optional reads."""
import json
import unittest

import test_review_ui as review


class ModuleLoadingTests(unittest.TestCase):
    setUpClass = classmethod(review.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review.ReviewPageTests.tearDownClass.__func__)
    setUp = review.ReviewPageTests.setUp
    stop_server = review.ReviewPageTests.stop_server
    request = review.ReviewPageTests.request

    def page(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        return page

    def test_home_work_opens_before_charts_and_keeps_selected_tab(self):
        page, held = self.page(), []
        page.route("**/api/performance/analytics", lambda route: held.append(route))
        page.goto(f"{self.base}/#/202H/home")
        page.locator(".home-tabs").wait_for()
        self.assertEqual(len(held), 1)
        page.locator("#home-weekly-section").get_by_text("正在读取周图…").wait_for()
        page.locator('[data-home-tab="cases"]').click()
        tab = page.locator('[data-home-tab="cases"]')
        page.evaluate("window.chosenHomeTabNode = document.querySelector('[data-home-tab=cases]')")
        held.pop().continue_()
        page.locator(".home-weekly-charts").wait_for()
        self.assertEqual(tab.get_attribute("aria-selected"), "true")
        self.assertTrue(page.evaluate("chosenHomeTabNode === document.querySelector('[data-home-tab=cases]')"))

    def test_video_table_opens_before_charts_and_direction_catalog(self):
        page, held = self.page(), []
        page.route("**/api/performance/analytics", lambda route: held.append(route))
        page.route("**/api/direction-catalog", lambda route: held.append(route))
        page.goto(f"{self.base}/#/202H/videos?video={review.performance.LINKED}")
        page.locator(".perf-table").wait_for()
        self.assertGreaterEqual(len(held), 2)
        self.assertIn("Is A Mirror Edge Overrated", page.locator(".perf-table").inner_text())
        page.locator("#perf-q").fill("keep this query")
        for route in held:
            route.continue_()
        page.wait_for_function("() => !document.querySelector('.perf-direction button')?.textContent.includes('读取方向')")
        self.assertEqual(page.locator("#perf-q").input_value(), "keep this query")

    def test_review_results_start_while_summary_is_pending(self):
        page, held = self.page(), []
        page.add_init_script("""(() => {
          window.reviewListReads = [];
          const original = window.fetch;
          window.fetch = function(input, options) {
            if (String(input).startsWith('/api/reviews?')) window.reviewListReads.push(String(input));
            return original.call(this, input, options);
          };
        })();""")
        def summary(route):
            held.append(route)
            route.request.frame.evaluate("window.reviewSummaryHeld = true")

        page.route("**/api/reviews/summary", summary)
        page.goto(f"{self.base}/#/202H/reviews?topic=stability")
        page.wait_for_function("() => reviewListReads.length > 0 && window.reviewSummaryHeld")
        self.assertEqual(len(held), 1)
        self.assertIn("topic=stability", page.evaluate("reviewListReads[0]"))
        held.pop().continue_()
        page.locator("#rev-count").get_by_text("1 条", exact=False).wait_for()
        self.assertEqual(page.evaluate("reviewListReads.length"), 1)

    def test_video_reentry_waits_for_fresh_charts_without_waiting_for_catalog(self):
        page = self.page()
        page.goto(f"{self.base}/#/202H/videos")
        page.locator(".perf-trend-card").first.wait_for()
        page.locator('[data-page="settings"]').click()
        page.locator(".settings-page").wait_for()
        charts, catalog = [], []
        page.route("**/api/performance/analytics", lambda route: charts.append(route))
        page.route("**/api/direction-catalog", lambda route: catalog.append(route))
        page.locator('[data-page="videos"]').click()
        page.locator("#perf-board").get_by_text("正在读取看板数据…").wait_for()
        self.assertEqual(page.locator("#perf-board .perf-trend-card").count(), 0)
        self.assertEqual(len(charts), 1)
        self.assertEqual(len(catalog), 1)
        charts.pop().continue_()
        page.locator("#perf-board .perf-trend-card").first.wait_for()
        self.assertEqual(len(catalog), 1)
        catalog.pop().continue_()

    def test_unknown_review_labels_are_corrected_after_speculative_error(self):
        page = self.page()
        page.goto(f"{self.base}/#/202H/reviews?platform=unknown&topic=unknown&sort=bad&rating=9")
        page.locator(".rev-card").first.wait_for()
        self.assertEqual(page.locator("#rev-count").inner_text(), "10 条")
        self.assertEqual(page.evaluate("location.hash"), "#/202H/reviews")

    def test_production_connection_checks_share_pending_read_and_allow_force(self):
        page, held = self.page(), []
        calls = []

        def connection(route):
            calls.append(route.request.url)
            if len(calls) == 1:
                held.append(route)
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

        page.route("**/api/production/eleven", connection)
        page.goto(f"{self.base}/#/202H/production")
        page.locator("#production-eleven").get_by_text("ElevenLabs：正在检查连接…").wait_for()
        page.evaluate("CaseProduction.route(true)")
        self.assertEqual(len(calls), 1)
        held.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        page.evaluate("CaseProduction.route(true)")
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        self.assertEqual(len(calls), 1)
        page.locator("#production-eleven [data-recheck]").click()
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        self.assertEqual(len(calls), 2)
        page.evaluate("CaseProduction.route(true, true)")
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
