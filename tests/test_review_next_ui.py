"""Continuous review uses today's priority queue and never repeats a confirmed write."""
import unittest
from datetime import date

import test_feedback_chain_ui as fixture
import test_performance as performance


class ReviewNextBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FeedbackChainBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FeedbackChainBrowserTests.tearDownClass.__func__)
    stop_server = fixture.FeedbackChainBrowserTests.stop_server
    request = fixture.FeedbackChainBrowserTests.request
    open_page = fixture.FeedbackChainBrowserTests.open_page
    ok = fixture.FeedbackChainBrowserTests.ok
    review = fixture.FeedbackChainBrowserTests.review
    chain = fixture.FeedbackChainBrowserTests.chain

    def setUp(self):
        fixture.FeedbackChainBrowserTests.setUp(self)
        self.second = '7600000000000000002'
        self.third = '7600000000000000003'
        performance.export(self.root / 'exports' / 'next-week.xlsx', '2026-09-14', '2026-09-20',
                           [performance.row(self.second, 80), performance.row(self.third, 600)])
        performance.export(self.root / 'exports' / 'fourth-week.xlsx', '2026-09-21', '2026-09-27',
                           [performance.row(self.third, 100)])
        self.ok('POST', '/api/performance/sync', {})

    def home_review(self):
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        row = page.locator(f'[data-followup-video="{self.video_id}"]')
        row.wait_for()
        row.locator('[data-followup-chain]').click()
        page.locator('#perf-video-drawer-floating [data-feedback-save]').wait_for()
        page.evaluate('window.originalReviewDrawer = document.querySelector("#perf-video-drawer-floating")')
        self.assertEqual(page.locator('.feedback-chain-dialog').count(), 0)
        self.assertEqual(page.evaluate('location.hash'), '#/202H/home')
        return page

    def save(self, page, video_id, label='weak'):
        dialog = page.locator('#perf-video-drawer-floating[open]')
        dialog.locator('[data-feedback-note]').fill('已人工核对 ' + video_id + '，保留本条教训。')
        dialog.locator('[data-feedback-reference]').select_option(label)
        dialog.locator('[data-feedback-save]').click()

    def wait_source(self, page, video_id):
        page.locator(f'#perf-video-drawer-floating .perf-video-source [data-copy-id="{video_id}"]').wait_for()
        page.locator('#perf-video-drawer-floating [data-feedback-save]').wait_for()

    def delay_next(self, page):
        page.evaluate("""() => {
          const original = ReviewFollowups.nextReady;
          ReviewFollowups.nextReady = async options => {
            const next = await original(options);
            window.nextLookupHeld = true;
            return await new Promise(resolve => {window.releaseNextLookup = () => resolve(next);});
          };
        }""")

    def test_home_saves_in_priority_order_in_same_drawer_then_shows_current_scope_done(self):
        page = self.home_review()
        expected = [self.video_id, self.third, self.second]
        self.assertEqual(page.locator('.followup-row').evaluate_all('nodes => nodes.map(node => node.dataset.followupVideo)'), expected)
        self.assertTrue(page.locator('.feedback-next-hint').is_visible())
        for index, video_id in enumerate(expected):
            self.wait_source(page, video_id)
            page.locator('#perf-video-drawer-floating').evaluate('node => node.scrollTop = node.scrollHeight')
            self.save(page, video_id)
            if index + 1 < len(expected):
                self.wait_source(page, expected[index + 1])
                self.assertTrue(page.evaluate('document.querySelector("#perf-video-drawer-floating") === originalReviewDrawer'))
                self.assertEqual(page.locator('#perf-video-drawer-floating').evaluate('node => node.scrollTop'), 0)
                self.assertEqual(page.locator('#perf-video-drawer-floating .rev-drawer-body').evaluate('node => node.scrollTop'), 0)
                self.assertEqual(page.evaluate('location.hash'), '#/202H/home')
            else:
                page.locator('.feedback-next-status', has_text='当前范围内，今天可复盘的视频已全部完成').wait_for()
                self.assertIn(video_id, page.locator('.feedback-conclusion').inner_text())
                self.assertTrue(page.locator('#perf-video-drawer-floating').is_visible())
        entries = self.server.app.review_followups.public()['entries']
        self.assertEqual({entry['video_id'] for entry in entries}, set(expected))
        self.assertTrue(all(len(entry['history']) == 1 for entry in entries))
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_registration_without_sales_data_opens_the_same_drawer_from_title_and_action(self):
        video_id = '7690000000000000001'
        app = self.server.app
        catalog = app.creation.read()
        catalog['scripts'].append({'id':'SCR-111111111111', 'product':'202H', 'title':'新登记暂无销量',
                                   'video_id':video_id, 'published_on':date.today().isoformat(),
                                   'version':1, 'status':'published', 'history':[]})
        app.creation.write(catalog)
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-filter]').select_option('all')
        row = page.locator(f'.followup-row[data-followup-video="{video_id}"]')
        for selector in ('[data-followup-registration]', '[data-followup-chain]'):
            row.locator(selector).click()
            drawer = page.locator('#perf-video-drawer-floating[open]')
            drawer.locator('[data-feedback-save]').wait_for()
            self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'), video_id)
            self.assertEqual(drawer.locator('#perf-video-title').inner_text(), '新登记暂无销量')
            self.assertTrue(drawer.locator('.feedback-early-warning').is_visible())
            self.assertEqual(page.locator('.feedback-chain-dialog').count(), 0)
            self.assertEqual(page.evaluate('location.hash'), '#/202H/home')
            drawer.locator('[data-drawer-close]').click()
        self.assertEqual(app.review_followups.public()['entries'], [])

    def test_closing_home_drawer_refreshes_the_queue_after_a_review_is_updated_elsewhere(self):
        page = self.home_review()
        self.review(note='其他入口已完成这条复盘。', reference_value='weak')
        row = page.locator(f'.followup-row[data-followup-video="{self.video_id}"]')
        self.assertEqual(row.count(), 1)
        page.locator('#perf-video-drawer-floating [data-drawer-close]').click()
        row.wait_for(state='detached')
        self.assertEqual(page.locator('#perf-video-drawer-floating[open]').count(), 0)
        self.assertEqual(page.evaluate('location.hash'), '#/202H/home')
        self.assertEqual(len(self.server.app.review_followups.public()['entries']), 1)

    def test_queue_failure_keeps_saved_review_and_retry_only_reads(self):
        page = self.home_review()
        page.evaluate("""() => {
          window.originalNextReady = ReviewFollowups.nextReady;
          ReviewFollowups.nextReady = async () => {throw new Error('test lookup failure');};
        }""")
        posts = []
        page.on('request', lambda request: posts.append(request.url) if request.method == 'POST' else None)
        self.save(page, self.video_id)
        page.locator('[data-feedback-next-retry]').wait_for()
        self.assertIn(self.video_id, page.locator('.feedback-conclusion').inner_text())
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '弱参考价值')
        self.assertNotIn('已全部完成', page.locator('.feedback-next-status').inner_text())
        page.evaluate('() => {ReviewFollowups.nextReady = originalNextReady;}')
        page.locator('[data-feedback-next-retry]').click()
        self.wait_source(page, self.third)
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']), 1)

    def test_next_detail_failure_does_not_replace_saved_source(self):
        page = self.home_review()

        def fail_next_detail(route):
            if self.third in route.request.url:
                route.fulfill(status=503, json={'error': '下一条详情读取失败'})
            else:
                route.continue_()

        page.route('**/api/feedback-chain?*', fail_next_detail)
        self.save(page, self.video_id)
        page.locator('[data-feedback-next-retry]').wait_for()
        self.assertTrue(page.locator(f'.perf-video-source [data-copy-id="{self.video_id}"]').is_visible())
        self.assertIn(self.video_id, page.locator('.feedback-conclusion').inner_text())
        page.unroute('**/api/feedback-chain?*', fail_next_detail)
        page.locator('[data-feedback-next-retry]').click()
        self.wait_source(page, self.third)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']), 1)

    def test_closing_during_next_lookup_does_not_reopen_drawer(self):
        page = self.home_review()
        self.delay_next(page)
        self.save(page, self.video_id)
        page.wait_for_function('() => window.nextLookupHeld === true')
        page.locator('#perf-video-drawer-floating [data-drawer-close]').click()
        page.evaluate('releaseNextLookup()')
        page.wait_for_function('() => !document.querySelector("#perf-video-drawer-floating").open')
        self.assertEqual(page.locator('.feedback-chain-dialog, #perf-video-drawer-floating[open]').count(), 0)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']), 1)

    def test_changing_product_during_next_lookup_keeps_late_response_from_navigating(self):
        page = self.home_review()
        self.delay_next(page)
        self.save(page, self.video_id)
        page.wait_for_function('() => window.nextLookupHeld === true')
        page.evaluate("history.replaceState(null,'','#/129N/home');releaseNextLookup()")
        page.wait_for_function('() => location.hash === "#/129N/home"')
        self.assertTrue(page.locator(f'.perf-video-source [data-copy-id="{self.video_id}"]').is_visible())
        self.assertEqual(page.locator(f'.perf-video-source [data-copy-id="{self.third}"]').count(), 0)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']), 1)

    def test_editing_saved_review_cancels_pending_next_lookup(self):
        page = self.home_review()
        self.delay_next(page)
        self.save(page, self.video_id)
        page.wait_for_function('() => window.nextLookupHeld === true')
        page.locator('[data-feedback-redo]').click()
        page.locator('[data-feedback-note]').fill('正在补充当前结论，请保留输入。')
        page.evaluate('releaseNextLookup()')
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), '正在补充当前结论，请保留输入。')
        self.assertTrue(page.locator(f'.perf-video-source [data-copy-id="{self.video_id}"]').is_visible())
        self.assertEqual(page.locator('.feedback-next-hint').count(), 0)

    def test_failed_save_does_not_query_next_until_receipt_is_confirmed(self):
        page = self.home_review()
        page.evaluate("""() => {
          window.nextQueries = 0;
          const original = ReviewFollowups.nextReady;
          ReviewFollowups.nextReady = async options => {nextQueries++;return await original(options);};
        }""")
        requests = []

        def lost_response(route):
            if route.request.method != 'POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response = route.fetch()
            route.fulfill(status=503, json={'error': '保存响应中断'}) if len(requests) == 1 else route.fulfill(response=response)

        page.route('**/api/review-followups', lost_response)
        self.save(page, self.video_id)
        page.locator('[data-feedback-error]', has_text='结果尚未确认').wait_for()
        self.assertEqual(page.evaluate('nextQueries'), 0)
        self.assertTrue(page.locator(f'.perf-video-source [data-copy-id="{self.video_id}"]').is_visible())
        page.locator('[data-feedback-save]').click()
        self.wait_source(page, self.third)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(page.evaluate('nextQueries'), 1)
        self.assertEqual(len(self.server.app.review_followups.public()['entries'][0]['history']), 1)


if __name__ == '__main__':
    unittest.main()
