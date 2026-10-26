"""Manual review classification is separate from closing and the saved conclusion."""
import unittest

import test_feedback_chain_ui as fixture
import test_review_observation_panel_ui as panel_fixture


class ReviewReferenceValueBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FeedbackChainBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FeedbackChainBrowserTests.tearDownClass.__func__)
    setUp = fixture.FeedbackChainBrowserTests.setUp
    stop_server = fixture.FeedbackChainBrowserTests.stop_server
    request = fixture.FeedbackChainBrowserTests.request
    open_page = fixture.FeedbackChainBrowserTests.open_page
    ok = fixture.FeedbackChainBrowserTests.ok
    chain = fixture.FeedbackChainBrowserTests.chain
    review = fixture.FeedbackChainBrowserTests.review
    video_page = fixture.FeedbackChainBrowserTests.video_page

    def test_stop_can_save_weak_reference_without_direction_or_changing_judgment(self):
        self.review(legacy=True, decision='stop', outcome='negative', evidence='preliminary')
        page = self.video_page(width=390, ready='[data-feedback-redo]')
        self.assertEqual(page.locator('[data-feedback-redo]').inner_text(),'编辑复盘')
        page.locator('[data-feedback-redo]').click()
        selector = page.locator('[data-feedback-reference]')
        self.assertTrue(selector.is_visible())
        self.assertEqual(selector.input_value(), '')
        self.assertEqual(selector.locator('option').all_text_contents(),
                         ['请选择参考价值', '无参考价值', '弱参考价值', '中等参考价值', '高参考价值'])
        self.assertEqual(page.locator('.feedback-reference-guide').count(), 0)
        page.locator('[data-feedback-note]').fill('有少量成交，但剪辑与文案主题不一致，结束本条。')
        selector.select_option('weak')
        page.locator('.feedback-more-actions > summary').click()
        page.locator('[data-feedback-decide=stop]').click()
        page.locator('[data-feedback-reference-label]').wait_for()
        saved = self.chain()['review']
        self.assertEqual((saved['reference_value'], saved['decision'], saved['outcome'], saved['evidence']),
                         ('weak', 'stop', 'negative', 'preliminary'))
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '弱参考价值')
        self.assertEqual(self.server.app.creation.public()['plans'], [])
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        page.reload()
        page.locator('[data-feedback-reference-label]').wait_for()
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '弱参考价值')

    def test_reviewed_video_can_be_classified_afterwards_without_rewriting_source(self):
        self.review(legacy=True, note='人工复盘已结束，保留真实成交和内容判断。', decision='stop', outcome='negative', evidence='preliminary')
        before = self.chain()
        page = self.video_page(ready='[data-feedback-classify]')
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '待分类')
        requests = []
        page.on('request', lambda request: requests.append(request.post_data_json)
                if request.method == 'POST' and '/api/review-followups' in request.url else None)
        for value, label in [('none', '无参考价值'), ('weak', '弱参考价值'),
                             ('medium', '中等参考价值'), ('high', '高参考价值'), ('', '待分类')]:
            page.locator('[data-feedback-classify]').click()
            dialog = page.locator('.feedback-form-dialog')
            self.assertEqual(dialog.locator('.feedback-reference-guide').count(), 0)
            dialog.locator('[name=reference_value]').select_option(value)
            dialog.get_by_role('button', name='保存分类', exact=True).click()
            dialog.wait_for(state='detached')
            page.wait_for_function('label => document.querySelector("[data-feedback-reference-label]")?.textContent === label', arg=label)
            after = self.chain()
            self.assertEqual(after['source_review'], before['source_review'])
            self.assertEqual(after['review']['reference_value'], value)
            for key in ('state', 'decision', 'note', 'outcome', 'evidence', 'at'):
                self.assertEqual(after['review'][key], before['review'][key])
        self.assertEqual(len(requests), 5)
        for body in requests:
            self.assertEqual(set(body), {'request_id', 'revision', 'product', 'video_id', 'window_end', 'classification_only', 'reference_value'})
            self.assertIs(body['classification_only'], True)
        self.assertEqual(self.server.app.creation.public()['plans'], [])

    def test_classification_unknown_result_replays_the_same_request(self):
        self.review(decision='stop')
        page = self.video_page(ready='[data-feedback-classify]')
        page.locator('[data-feedback-classify]').click()
        dialog = page.locator('.feedback-form-dialog')
        dialog.locator('[name=reference_value]').select_option('medium')
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
        dialog.get_by_role('button', name='保存分类', exact=True).click()
        dialog.get_by_text('结果尚未确认', exact=False).wait_for()
        self.assertTrue(dialog.locator('[name=reference_value]').is_disabled())
        dialog.get_by_role('button', name='保存分类', exact=True).click()
        dialog.wait_for(state='detached')
        page.wait_for_function('() => document.querySelector("[data-feedback-reference-label]")?.textContent === "中等参考价值"')
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(self.chain()['review']['history']), 2)

    def test_unconfirmed_classification_is_not_saved_by_selecting_it(self):
        page = self.video_page()
        page.locator('[data-feedback-reference]').select_option('weak')
        self.assertEqual(page.locator('[data-feedback-decide=observe], [data-feedback-decide=none]').count(), 0)
        self.assertIsNone(self.chain()['review'])
        page.reload()
        page.locator('[data-feedback-reference]').wait_for()
        self.assertEqual(page.locator('[data-feedback-reference]').input_value(), '')
        self.assertIsNone(self.chain()['review'])


class ReviewReferenceValueObservationTests(unittest.TestCase):
    setUpClass = classmethod(panel_fixture.ObservationPanelTests.setUpClass.__func__)
    tearDownClass = classmethod(panel_fixture.ObservationPanelTests.tearDownClass.__func__)
    panel = panel_fixture.ObservationPanelTests.panel

    def test_young_video_can_be_classified_with_a_reminder(self):
        page = self.panel()
        self.assertTrue(page.locator('[data-feedback-reference]').is_enabled())
        self.assertTrue(page.locator('.feedback-early-warning').is_visible())
        self.assertEqual(page.evaluate('requests'), [])

    def test_historical_review_keeps_classification_action_available(self):
        page = self.panel(mature=True, review={'state':'reviewed', 'note':'已完成的历史复盘。',
                                              'decision':'stop', 'reference_value':'weak'})
        page.evaluate('chain.current_window = false')
        page.locator('[data-feedback-refresh]').click()
        page.locator('[data-feedback-classify]').wait_for()
        self.assertEqual(page.locator('[data-feedback-redo]').count(), 0)
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '弱参考价值')
        page.locator('[data-feedback-classify]').click()
        self.assertTrue(page.locator('.feedback-form-dialog [name=reference_value]').is_enabled())
