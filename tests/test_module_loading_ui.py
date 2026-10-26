"""Loading regressions use isolated records and deliberately held optional reads."""
import json
import re
import unittest

import test_review_ui as review

# The data page asks for one shop at a time ("?shop=local"); these tests care which endpoint is read, not for which shop.
SUMMARY, ANALYTICS = re.compile(r"/api/performance/summary(\?|$)"), re.compile(r"/api/performance/analytics(\?|$)")
VIDEOS = re.compile(r"/api/performance/videos(\?shop=[a-z]+)?$")


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

    def hold_read(self, page, pattern, held, name="optional"):
        def pause(route):
            held.append(route)
            route.request.frame.evaluate("""name => {
              window.heldReads ||= {};
              window.heldReads[name] = (window.heldReads[name] || 0) + 1;
            }""", name)
        page.route(pattern, pause)

    def track_sales_reads(self, page):
        page.add_init_script("""(() => {
          window.salesReads = [];
          window.salesFinished = [];
          const original = window.fetch;
          window.fetch = function(input, options) {
            const path = String(input).replace(/[?&]shop=[a-z]+/, '');
            const salesRead = path.startsWith('/api/performance/') || path.startsWith('/api/direction-catalog');
            if (salesRead) window.salesReads.push({path,
              homeReady: Boolean(document.querySelector('.home-tabs')),
              videosReady: Boolean(document.querySelector('#perf-results, #perf-board'))});
            return original.call(this, input, options).then(response => {
              if (salesRead) window.salesFinished.push(path);
              return response;
            });
          };
        })();""")

    def assert_no_optional_sales_reads(self, page):
        self.assertEqual(page.evaluate("""salesReads.filter(read =>
          read.path === '/api/performance/analytics' || read.path.startsWith('/api/direction-catalog'))"""), [])

    def test_home_work_remains_available_without_chart_services(self):
        page, chart_calls = self.page(), []

        def unavailable(route):
            chart_calls.append(route.request.url)
            route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "charts unavailable"}))

        page.route(SUMMARY, unavailable)
        page.route(ANALYTICS, unavailable)
        page.goto(f"{self.base}/#/202H/home")
        page.locator(".home-tabs").wait_for()
        page.locator('[data-home-tab="cases"]').click()
        self.assertTrue(page.locator("#home-cases-title-panel").is_visible())
        self.assertEqual(page.locator('[data-home-tab="cases"]').get_attribute("aria-selected"), "true")
        self.assertEqual(page.locator("#home-page .data-warning").count(), 0)
        self.assertEqual(chart_calls, [])

    def test_video_table_opens_before_charts_and_direction_catalog(self):
        page, held = self.page(), []
        self.hold_read(page, ANALYTICS, held)
        self.hold_read(page, "**/api/direction-catalog", held)
        page.goto(f"{self.base}/#/202H/videos?video={review.performance.LINKED}")
        page.locator(".perf-table").wait_for()
        page.wait_for_function("() => window.heldReads?.optional === 2")
        self.assertGreaterEqual(len(held), 2)
        self.assertIn("Is A Mirror Edge Overrated", page.locator(".perf-table").inner_text())
        page.locator("#perf-video-drawer [data-drawer-close]").click()
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
        self.hold_read(page, ANALYTICS, charts)
        self.hold_read(page, "**/api/direction-catalog", catalog)
        page.locator('[data-page="videos"]').click()
        page.locator("#perf-board").get_by_text("正在读取看板数据…").wait_for()
        page.wait_for_function("() => window.heldReads?.optional === 2")
        self.assertEqual(page.locator("#perf-board .perf-trend-card").count(), 0)
        self.assertEqual(len(charts), 1)
        self.assertEqual(len(catalog), 1)
        charts.pop().continue_()
        page.locator("#perf-board .perf-trend-card").first.wait_for()
        self.assertEqual(len(catalog), 1)
        catalog.pop().continue_()

    def test_home_only_reads_work_data_before_and_after_render(self):
        page, videos = self.page(), []
        self.track_sales_reads(page)
        home_videos = "/api/performance/videos?projection=home&product=202H"
        self.hold_read(page, "**" + home_videos, videos, "videos")
        page.goto(f"{self.base}/#/202H/home")
        page.wait_for_function("() => window.heldReads?.videos")
        self.assertEqual(page.evaluate("salesReads.map(read => read.path)"), [home_videos])
        videos.pop().continue_()
        page.locator(".home-tabs").wait_for()
        self.assertEqual(page.evaluate("salesReads.map(read => read.path)"), [home_videos])
        page.unroute("**" + home_videos)
        page.locator('#home-page a[href$="/videos?tab=board"]').click()
        page.locator("#perf-board .perf-trend-card").first.wait_for()
        self.assertTrue(page.evaluate("salesReads.some(read => read.path === '/api/performance/analytics')"))

    def test_video_defers_charts_and_catalog_until_table_has_rendered(self):
        page, summary, videos = self.page(), [], []
        self.track_sales_reads(page)
        self.hold_read(page, SUMMARY, summary, "summary")
        self.hold_read(page, VIDEOS, videos, "videos")
        page.goto(f"{self.base}/#/202H/videos?video={review.performance.LINKED}")
        page.wait_for_function("() => window.heldReads?.summary && window.heldReads?.videos")
        self.assert_no_optional_sales_reads(page)
        videos.pop().continue_()
        page.wait_for_function("() => salesFinished.includes('/api/performance/videos')")
        self.assert_no_optional_sales_reads(page)
        summary.pop().continue_()
        page.locator(".perf-table").wait_for()
        page.wait_for_function("() => salesReads.some(read => read.path === '/api/performance/analytics') && salesReads.some(read => read.path.startsWith('/api/direction-catalog'))")
        self.assertTrue(page.evaluate("salesReads.filter(read => read.path === '/api/performance/analytics' || read.path.startsWith('/api/direction-catalog')).every(read => read.videosReady)"))

    def test_leaving_before_foreground_reads_finish_does_not_start_optional_work(self):
        for module in ("home", "videos"):
            with self.subTest(module=module):
                page, summary, videos = self.page(), [], []
                self.track_sales_reads(page)
                if module == "videos":
                    self.hold_read(page, SUMMARY, summary, "summary")
                video_path = "/api/performance/videos" + ("?projection=home&product=202H" if module == "home" else "")
                self.hold_read(page, "**" + video_path if module == "home" else VIDEOS, videos, "videos")
                page.goto(f"{self.base}/#/202H/{module}")
                page.wait_for_function("() => window.heldReads?.videos")
                if module == "videos":
                    page.wait_for_function("() => window.heldReads?.summary")
                page.locator('[data-page="settings"]').click()
                page.locator(".settings-page").wait_for()
                if summary:
                    summary.pop().continue_()
                videos.pop().continue_()
                page.wait_for_function("path => salesFinished.includes(path)", arg=video_path)
                if module == "videos":
                    page.wait_for_function("() => salesFinished.includes('/api/performance/summary')")
                page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                self.assert_no_optional_sales_reads(page)
                self.assertEqual(page.locator(".settings-page").count(), 1)

    def test_failed_video_foreground_read_skips_extras_and_reload_can_retry(self):
        page, calls = self.page(), []
        self.track_sales_reads(page)

        def summary(route):
            calls.append(route.request.url)
            if len(calls) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "temporary foreground failure"}))
            else:
                route.continue_()

        page.route(SUMMARY, summary)
        page.goto(f"{self.base}/#/202H/videos?video={review.performance.LINKED}")
        page.get_by_role("heading", name="带货数据暂不可用").wait_for()
        self.assert_no_optional_sales_reads(page)
        page.reload()
        page.locator(".perf-table").wait_for()
        self.assertEqual(len(calls), 2)

    def test_home_work_retry_keeps_selected_tab(self):
        page, calls = self.page(), []

        def videos(route):
            calls.append(route.request.url)
            if len(calls) == 1:
                route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "temporary video-list failure"}))
            else:
                route.continue_()

        page.route("**/api/performance/videos?projection=home&product=202H", videos)
        page.goto(f"{self.base}/#/202H/home")
        page.locator("#home-page .data-warning [data-home-retry]").wait_for()
        page.locator('[data-home-tab="cases"]').click()
        page.locator("#home-page .data-warning [data-home-retry]").click()
        page.locator("#home-page .data-warning").wait_for(state="detached")
        self.assertEqual(len(calls), 2)
        self.assertEqual(page.locator('[data-home-tab="cases"]').get_attribute("aria-selected"), "true")

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
        page.locator("#content-flow-list").wait_for()
        self.assertEqual(page.evaluate("location.hash"), "#/202H/content")
        self.assertEqual(page.locator("#production-queue, #production-settings-open, .scripts-tabs").count(), 0)
        self.assertEqual(len(calls), 0)
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-production-settings").click()
        page.locator("#production-eleven").get_by_text("ElevenLabs：正在检查连接…").wait_for(state="attached")
        self.assertTrue(page.locator("#production-eleven").is_visible())
        self.assertTrue(page.locator("#content-flow-list").is_visible())
        page.evaluate("CaseProduction.openSettings()")
        self.assertEqual(len(calls), 1)
        held.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for(state="attached")
        self.assertTrue(page.locator("#production-eleven").is_visible())
        self.assertEqual(page.locator("#production-service-warning").count(), 0)
        page.evaluate("ContentFlow.route(true)")
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for(state="attached")
        self.assertEqual(len(calls), 1)
        page.locator("#production-settings[open] #production-eleven").wait_for()
        page.locator("#production-eleven [data-recheck]").click()
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        self.assertEqual(len(calls), 2)
        page.locator("#production-settings [data-drawer-close]").click()
        page.evaluate("ContentFlow.route(true)")
        self.assertEqual(len(calls), 2)
        page.locator(".flow-main-menu > summary").click()
        page.locator("#content-production-settings").click()
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for(state="attached")
        self.assertEqual(len(calls), 2)
        page.locator("#production-eleven [data-recheck]").click()
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接").wait_for()
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
