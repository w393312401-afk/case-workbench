"""A conclusion and manual reference value complete a review without choosing a future action."""
import unittest

import test_feedback_chain_ui as fixture
import test_manual_performance_status_ui as performance_ui
import test_review_observation_panel_ui as observation
from review_legacy_fixture import seed_legacy_followup


class SaveReviewPrimaryTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FeedbackChainBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FeedbackChainBrowserTests.tearDownClass.__func__)
    setUp = fixture.FeedbackChainBrowserTests.setUp
    stop_server = fixture.FeedbackChainBrowserTests.stop_server
    request = fixture.FeedbackChainBrowserTests.request
    open_page = fixture.FeedbackChainBrowserTests.open_page
    ok = fixture.FeedbackChainBrowserTests.ok
    chain = fixture.FeedbackChainBrowserTests.chain
    review = fixture.FeedbackChainBrowserTests.review
    direction = fixture.FeedbackChainBrowserTests.direction
    video_page = fixture.FeedbackChainBrowserTests.video_page
    open_list = performance_ui.ManualPerformanceStatusTests.open_list

    def fill_review(self, page, note='结构混乱的教训值得保留，下一条先明确主题。', reference='weak'):
        page.locator('[data-feedback-note]').fill(note)
        page.locator('[data-feedback-reference]').select_option(reference)
        return note

    def make_current_the_last_pending_review(self):
        # These tests inspect the saved conclusion; continuous navigation has its
        # own multi-video tests. Keep this explicitly as the end-of-queue case.
        app = self.server.app
        for video in app.performance.videos():
            if video['video_id'] != self.video_id and video.get('window_end'):
                seed_legacy_followup(app.review_followups, video, reference_value='weak')

    def test_save_is_only_primary_action_and_requires_conclusion_and_classification(self):
        page = self.video_page(width=390)
        self.assertEqual(page.locator('.feedback-decide button.primary').all_text_contents(), ['保存复盘'])
        self.assertFalse(page.locator('.feedback-more-actions').evaluate('node => node.open'))
        self.assertFalse(page.locator('[data-feedback-decide=stop]').is_visible())
        self.assertTrue(page.locator('.perf-video-source').is_visible())
        self.assertEqual(page.locator('[data-feedback-reference] option:checked').inner_text(), '请选择参考价值')
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            self.assertTrue(page.locator('[data-feedback-save]').is_visible())
            self.assertFalse(page.evaluate('() => document.documentElement.scrollWidth > innerWidth'))
        page.locator('[data-feedback-save]').click()
        self.assertIn('先写一句复盘结论', page.locator('[data-feedback-error]').inner_text())
        page.locator('[data-feedback-note]').fill('先确定主题再剪辑。')
        page.locator('[data-feedback-save]').click()
        self.assertIn('请选择参考价值', page.locator('[data-feedback-error]').inner_text())
        self.assertEqual(self.server.app.review_followups.public()['entries'], [])
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_primary_save_updates_open_drawer_and_list_without_decision_or_plan(self):
        self.make_current_the_last_pending_review()
        page = self.open_list()
        row = page.locator(f'[data-video-row="{self.video_id}"]')
        self.assertEqual(row.locator('.perf-status').inner_text(), '待复盘')
        row.locator('.perf-title').click()
        page.locator('[data-feedback-save]').wait_for()
        requests = []
        page.on('request', lambda request: requests.append(request.post_data_json)
                if request.method == 'POST' and request.url.endswith('/api/review-followups') else None)
        note = self.fill_review(page)
        page.locator('[data-feedback-save]').click()
        page.locator('[data-feedback-reference-label]', has_text='弱参考价值').wait_for()
        page.locator('.perf-video-status .perf-status', has_text='弱参考价值').wait_for()
        row.locator('.perf-status', has_text='弱参考价值').wait_for()
        self.assertTrue(page.locator('#perf-video-drawer').evaluate('node => node.open'))
        self.assertEqual(set(requests[0]), {'request_id', 'revision', 'product', 'video_id', 'window_end',
                                           'state', 'note', 'reference_value'})
        saved = self.chain()['review']
        self.assertEqual((saved['state'], saved['note'], saved['reference_value'], saved['decision']),
                         ('reviewed', note, 'weak', ''))
        self.assertEqual(len(saved['history']), 1)
        self.assertEqual(self.server.app.creation.public()['plans'], [])
        self.assertEqual(self.server.app.ai.list_tasks(), [])

    def test_last_home_review_updates_queue_and_keeps_saved_conclusion_open(self):
        self.make_current_the_last_pending_review()
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        row = page.locator(f'.followup-row[data-followup-video="{self.video_id}"]')
        row.wait_for()
        row.locator('[data-followup-chain]').click()
        dialog = page.locator('#perf-video-drawer-floating[open]')
        self.fill_review(dialog, reference='medium')
        dialog.locator('[data-feedback-save]').click()
        dialog.locator('[data-feedback-reference-label]', has_text='中等参考价值').wait_for()
        row.wait_for(state='detached')
        self.assertTrue(dialog.is_visible())
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_edit_primary_save_preserves_existing_stop_and_failure_assessment(self):
        self.review(legacy=True, note='原失败教训。', decision='stop', reference_value='weak',
                    outcome='negative', evidence='preliminary')
        page = self.video_page(ready='[data-feedback-redo]')
        self.assertEqual(page.locator('[data-feedback-redo]').inner_text(), '编辑复盘')
        page.locator('[data-feedback-redo]').click()
        note = self.fill_review(page, '保留失败教训，并补充先明确主题的要求。')
        page.locator('[data-feedback-save]').click()
        page.locator('[data-feedback-redo]').wait_for()
        saved = self.chain()['review']
        self.assertEqual((saved['decision'], saved['outcome'], saved['evidence'], saved['reference_value']),
                         ('stop', 'negative', 'preliminary', 'weak'))
        self.assertEqual(saved['note'], note)
        self.assertEqual(self.server.app.creation.public()['plans'], [])
        self.assertIn('停测', page.locator('.feedback-conclusion-head').inner_text())

    def test_unknown_save_reopens_frozen_inputs_and_replays_one_request(self):
        self.make_current_the_last_pending_review()
        page = self.video_page()
        note = self.fill_review(page)
        requests = []

        def lost_response(route):
            if route.request.method != 'POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response = route.fetch()
            if len(requests) == 1:
                route.fulfill(status=503, json={'error': '保存响应中断'})
            else:
                route.fulfill(response=response)

        page.route('**/api/review-followups', lost_response)
        page.locator('[data-feedback-save]').click()
        page.locator('[data-feedback-error]', has_text='结果尚未确认').wait_for()
        for selector in ('[data-feedback-note]', '[data-feedback-reference]', '[data-feedback-draft]',
                         '[data-feedback-refresh]', '[data-feedback-decide=stop]'):
            self.assertTrue(page.locator(selector).is_disabled())
        page.locator('#perf-video-drawer [data-drawer-close]').click()
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        page.locator('[data-feedback-error]', has_text='结果尚未确认').wait_for()
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), note)
        self.assertEqual(page.locator('[data-feedback-reference]').input_value(), 'weak')
        self.assertTrue(page.locator('[data-feedback-note]').is_disabled())
        self.assertTrue(page.locator('[data-feedback-save]').is_enabled())
        page.locator('[data-feedback-save]').click()
        page.locator('[data-feedback-reference-label]', has_text='弱参考价值').wait_for()
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(self.chain()['review']['history']), 1)
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_explicit_action_unknown_result_keeps_direction_when_reopened(self):
        direction = self.direction()
        page = self.video_page()
        self.fill_review(page)
        page.locator('.feedback-more-actions > summary').click()
        selector = page.locator('[data-feedback-direction]')
        page.wait_for_function('id => [...document.querySelectorAll("[data-feedback-direction] option")].some(o => o.value === id)',
                               arg=direction['direction_id'])
        selector.select_option(direction['direction_id'])
        requests = []

        def lost_response(route):
            if route.request.method != 'POST':
                return route.continue_()
            requests.append(route.request.post_data_json)
            response = route.fetch()
            route.fulfill(status=503, json={'error': '保存响应中断'}) if len(requests) == 1 else route.fulfill(response=response)

        page.route('**/api/review-followups', lost_response)
        page.locator('[data-feedback-decide=execution]').click()
        page.locator('[data-feedback-error]', has_text='结果尚未确认').wait_for()
        page.locator('#perf-video-drawer [data-drawer-close]').click()
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        page.locator('[data-feedback-error]', has_text='结果尚未确认').wait_for()
        page.wait_for_function('id => document.querySelector("[data-feedback-direction]")?.value === id', arg=direction['direction_id'])
        self.assertTrue(page.locator('[data-feedback-direction]').is_disabled())
        page.locator('[data-feedback-decide=execution]').click()
        page.locator('[data-feedback-redo]').wait_for()
        self.assertEqual(requests[0], requests[1])
        plans = self.server.app.creation.public()['plans']
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]['direction_id'], direction['direction_id'])

    def test_read_failure_keeps_inputs_and_confirmed_save_cannot_repeat(self):
        self.make_current_the_last_pending_review()
        page = self.video_page()
        note = self.fill_review(page)
        failure = lambda route: route.fulfill(status=503, json={'error': '暂时无法读取复盘'})
        page.route('**/api/feedback-chain?*', failure)
        page.locator('[data-feedback-refresh]').click()
        page.locator('.feedback-load-error', has_text='输入已保留').wait_for()
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), note)
        self.assertEqual(page.locator('[data-feedback-reference]').input_value(), 'weak')
        page.unroute('**/api/feedback-chain?*', failure)
        page.locator('[data-feedback-load-retry]').click()
        page.locator('.feedback-load-error').wait_for(state='detached')
        page.route('**/api/feedback-chain?*', failure)
        page.locator('[data-feedback-save]').click()
        page.locator('.feedback-load-error', has_text='复盘已保存').wait_for()
        self.assertEqual(page.locator('[data-feedback-note]').input_value(), note)
        self.assertTrue(page.locator('[data-feedback-save]').is_disabled())
        self.assertEqual(page.locator('[data-feedback-save]').inner_text(), '已保存')
        self.assertEqual(len(self.chain()['review']['history']), 1)
        page.unroute('**/api/feedback-chain?*', failure)
        page.locator('[data-feedback-load-retry]').click()
        page.locator('[data-feedback-reference-label]', has_text='弱参考价值').wait_for()
        self.assertEqual(len(self.chain()['review']['history']), 1)


class SaveReviewObservationGateTests(unittest.TestCase):
    setUpClass = classmethod(observation.ObservationPanelTests.setUpClass.__func__)
    tearDownClass = classmethod(observation.ObservationPanelTests.tearDownClass.__func__)
    panel = observation.ObservationPanelTests.panel

    def test_primary_save_stays_open_with_a_reminder_before_data_is_complete(self):
        for args in ({}, {'context': False}):
            with self.subTest(args=args):
                page = self.panel(**args)
                self.assertTrue(page.locator('[data-feedback-save]').is_enabled())
                self.assertTrue(page.locator('[data-feedback-reference]').is_enabled())
                self.assertTrue(page.locator('.feedback-early-warning').is_visible())
                self.assertEqual(page.evaluate('requests'), [])


if __name__ == '__main__':
    unittest.main()
