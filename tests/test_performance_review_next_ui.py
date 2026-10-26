"""A confirmed new review continues in the same drawer using the full ready queue."""
import unittest
from urllib.parse import parse_qs, urlparse

import test_feedback_chain_ui as fixture
import test_manual_performance_status_ui as list_fixture
import test_performance as performance
from review_legacy_fixture import seed_legacy_followup


SECOND = '7600000000000000002'
THIRD = '7600000000000000003'
OTHER = '7600000000000000019'


class PerformanceReviewNextTests(unittest.TestCase):
    setUpClass = classmethod(fixture.FeedbackChainBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.FeedbackChainBrowserTests.tearDownClass.__func__)
    stop_server = fixture.FeedbackChainBrowserTests.stop_server
    request = fixture.FeedbackChainBrowserTests.request
    open_page = fixture.FeedbackChainBrowserTests.open_page
    ok = fixture.FeedbackChainBrowserTests.ok
    review = fixture.FeedbackChainBrowserTests.review
    chain = fixture.FeedbackChainBrowserTests.chain
    open_list = list_fixture.ManualPerformanceStatusTests.open_list

    def setUp(self):
        fixture.FeedbackChainBrowserTests.setUp(self)
        self.exports = self.root / 'exports'
        performance.export(self.exports / '9月' / '第三周视频.xlsx', '2026-09-14', '2026-09-20',
                           [performance.row(SECOND,1500),performance.row(THIRD,2000)])
        performance.export(self.exports / '9月' / '第四周视频.xlsx', '2026-09-21', '2026-09-27',
                           [performance.row(SECOND,100),performance.row(THIRD,1200)])
        self.server.app.performance.sync()
        self.window = self.server.app.performance.video(self.video_id)['window_end']

    def expected_next(self,page,video_id=None,product='202H'):
        video_id = video_id or self.video_id
        video = self.ok('GET',f'/api/performance/videos/{video_id}')['video']
        return page.evaluate('args => ReviewFollowups.nextReady(args)',{'product':product,'exclude':video})['video']

    def save(self,drawer,note='保存这次复盘的明确结论。',reference='weak'):
        drawer.locator('[data-feedback-note]').fill(note)
        drawer.locator('[data-feedback-reference]').select_option(reference)
        drawer.locator('[data-feedback-save]').click()

    def wait_video(self,drawer,video):
        source = drawer.locator(f'.perf-video-source [data-copy-id="{video["video_id"]}"]')
        source.wait_for()
        drawer.locator('[data-feedback-save]').wait_for()
        self.assertEqual(drawer.locator('[data-feedback-note]').input_value(),'')
        self.assertEqual(drawer.locator('[data-feedback-reference]').input_value(),'')
        self.assertIn(video['headline'],drawer.locator('#perf-video-title').inner_text())
        self.assertTrue(drawer.locator('.perf-video-source a').get_attribute('href').endswith('/video/'+video['video_id']))
        self.assertEqual(drawer.locator('.perf-video-status .perf-status').inner_text(),'待复盘')

    def test_list_saves_continuously_without_using_search_sort_or_closing_drawer(self):
        page = self.open_list(query='tab=list&q=Mirror&sort=gmv&dir=asc',width=390)
        self.assertEqual(page.locator('[data-video-row]').count(),1)
        expected = self.expected_next(page)
        self.assertIn(expected['video_id'],(SECOND,THIRD))
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        drawer.evaluate("node => {node.dataset.identityTest='original';window.drawerCloses=0;node.addEventListener('close',()=>drawerCloses++);}")
        self.save(drawer)
        self.wait_video(drawer,expected)
        self.assertEqual(drawer.get_attribute('data-identity-test'),'original')
        self.assertEqual(page.evaluate('drawerCloses'),0)
        self.assertEqual(drawer.evaluate('node => node.scrollTop'),0)
        params = parse_qs(urlparse(page.url).fragment.partition('?')[2])
        self.assertEqual((params['q'],params['sort'],params['dir']),(['Mirror'],['gmv'],['asc']))
        self.assertEqual((params['video'],params['feedback_window']),([expected['video_id']],[expected['window_end']]))
        self.assertEqual(page.locator('[data-video-row]').count(),1)
        self.assertEqual(page.locator(f'[data-video-row="{self.video_id}"] .perf-status').inner_text(),'弱参考价值')
        next_video = self.expected_next(page,expected['video_id'])
        self.assertIsNotNone(next_video)
        self.save(drawer,note='下一条独立的复盘结论。',reference='high')
        self.wait_video(drawer,next_video)
        saved = self.server.app.review_followups.public()['entries']
        self.assertEqual(len(saved),2)
        self.assertEqual({row['video_id']:row['reference_value'] for row in saved},{self.video_id:'weak',expected['video_id']:'high'})
        self.assertEqual(self.server.app.creation.public()['plans'],[])

    def test_floating_continues_without_navigating_the_page_below(self):
        page = self.open_page(route='#/202H/directions',ready='#content')
        expected = self.expected_next(page)
        before = page.url
        page.evaluate('id => CasePerformance.openVideoInPlace(id)',self.video_id)
        drawer = page.locator('#perf-video-drawer-floating[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        self.save(drawer)
        self.wait_video(drawer,expected)
        self.assertEqual(page.url,before)
        self.assertEqual(page.locator('#perf-results').count(),0)
        self.assertEqual(page.locator('dialog.perf-video-drawer[open]').count(),1)

    def test_editing_a_review_keeps_the_same_video_and_preserves_stop(self):
        self.review(note='已完成的旧结论。',decision='stop',reference_value='weak')
        page = self.open_list(query=f'tab=list&video={self.video_id}')
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-redo]').click()
        self.save(drawer,note='补充失败教训，继续保留停测。',reference='medium')
        drawer.locator('[data-feedback-reference-label]',has_text='中等参考价值').wait_for()
        self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'),self.video_id)
        self.assertEqual(self.chain()['review']['decision'],'stop')
        self.assertEqual(len(self.server.app.review_followups.public()['entries']),1)

    def test_closed_or_replaced_drawer_does_not_apply_a_late_full_video_response(self):
        page = self.open_list()
        expected = self.expected_next(page)
        target = '/api/performance/videos/'+expected['video_id']
        response = self.ok('GET',target)
        held = []
        page.route('**'+target,lambda route: held.append(route))
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        with page.expect_request('**'+target):
            self.save(drawer)
        drawer.locator('[data-drawer-close]').click()
        # Reopening the same video must not revive the earlier continuation either.
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer.locator('[data-feedback-redo]').wait_for()
        held[0].fulfill(json=response)
        page.wait_for_timeout(150)
        self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'),self.video_id)
        self.assertEqual(page.locator('[data-feedback-redo]').count(),1)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']),1)

    def test_change_page_during_next_review_read_does_not_reopen_drawer(self):
        page = self.open_list()
        expected = self.expected_next(page)
        chain_response = self.ok('GET',f'/api/feedback-chain?product=202H&video_id={expected["video_id"]}&window_end={expected["window_end"]}')
        held = []
        def intercept(route):
            if parse_qs(urlparse(route.request.url).query).get('video_id') == [expected['video_id']]:
                held.append(route)
            else:
                route.continue_()
        page.route('**/api/feedback-chain?*',intercept)
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        with page.expect_request(lambda request: '/api/feedback-chain?' in request.url and parse_qs(urlparse(request.url).query).get('video_id') == [expected['video_id']]):
            self.save(drawer)
        page.evaluate("location.hash='#/202H/directions'")
        page.wait_for_url('**/#/202H/directions')
        held[0].fulfill(json=chain_response)
        page.wait_for_timeout(150)
        self.assertEqual(page.locator('dialog.perf-video-drawer[open]').count(),0)
        self.assertTrue(page.url.endswith('#/202H/directions'))
        self.assertEqual(len(self.server.app.review_followups.public()['entries']),1)

    def test_all_products_scope_can_continue_to_another_product(self):
        for filename,start,end,existing in (
            ('第一周视频.xlsx','2026-08-31','2026-09-06',[performance.row(self.video_id,8000,3,200,450),performance.row(SECOND,500)]),
            ('第二周视频.xlsx','2026-09-07','2026-09-13',[performance.row(self.video_id,4000,1,100,150),performance.row(SECOND,300),performance.row(THIRD,900)])):
            values = performance.row(SECOND,4000,3,100,200)
            values[performance.HEADER.index('Video ID')] = OTHER
            values[performance.HEADER.index('Video Info')] = 'Other product ready review'
            values[performance.HEADER.index('Products')] = 'SHARPAL 129N'
            values[performance.HEADER.index('Time')] = '2026/08/31 06:00:00'
            performance.export(self.exports / '9月' / filename,start,end,[*existing,values])
        self.server.app.performance.sync()
        page = self.open_list(query='tab=list&q=Mirror')
        page.goto(f'{self.base}/#/all/videos?tab=list&q=Mirror')
        page.locator('.perf-table').wait_for()
        self.assertEqual(page.locator('#global-product').input_value(),'')
        expected = self.expected_next(page,product='')
        self.assertEqual(expected['video_id'],OTHER)
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        self.save(drawer)
        self.wait_video(drawer,expected)
        self.assertIn('#/all/videos?',page.url)
        self.assertEqual(page.locator('#global-product').input_value(),'')
        self.assertIn('129N',drawer.locator('.perf-video-meta').inner_text())

    def test_full_video_failure_keeps_saved_review_and_retry_only_reads(self):
        page = self.open_list()
        expected = self.expected_next(page)
        target = '/api/performance/videos/'+expected['video_id']
        reads, saves = [], []
        def flaky(route):
            reads.append(route.request.url)
            if len(reads) == 1:
                route.fulfill(status=503,json={'error':'测试下一条读取失败'})
            else:
                route.continue_()
        page.route('**'+target,flaky)
        page.on('request',lambda request: saves.append(request.post_data_json) if request.method == 'POST' and request.url.endswith('/api/review-followups') else None)
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        self.save(drawer)
        drawer.locator('[data-feedback-next-retry]').wait_for()
        self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'),self.video_id)
        self.assertEqual(drawer.locator('[data-feedback-reference-label]').inner_text(),'弱参考价值')
        drawer.locator('[data-feedback-next-retry]').click()
        self.wait_video(drawer,expected)
        self.assertEqual(len(saves),1)
        self.assertEqual(len(reads),2)

    def test_candidate_deferred_during_prefetch_is_skipped_on_read_only_retry(self):
        page = self.open_list()
        expected = self.expected_next(page)
        def defer_next(route):
            if parse_qs(urlparse(route.request.url).query).get('video_id') == [expected['video_id']]:
                seed_legacy_followup(self.server.app.review_followups,expected,state='deferred',defer_until='2099-01-01')
            route.continue_()
        page.route('**/api/feedback-chain?*',defer_next)
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        self.save(drawer)
        drawer.locator('[data-feedback-next-retry]').wait_for()
        self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'),self.video_id)
        alternate = self.expected_next(page)
        self.assertNotEqual(alternate['video_id'],expected['video_id'])
        drawer.locator('[data-feedback-next-retry]').click()
        self.wait_video(drawer,alternate)
        self.assertEqual(len(self.server.app.review_followups.public()['entries']),2)

    def test_editing_saved_conclusion_cancels_a_late_continuation(self):
        page = self.open_list()
        expected = self.expected_next(page)
        target = '/api/performance/videos/'+expected['video_id']
        response = self.ok('GET',target)
        held = []
        page.route('**'+target,lambda route: held.append(route))
        page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-save]').wait_for()
        with page.expect_request('**'+target):
            self.save(drawer)
        drawer.locator('[data-feedback-redo]').click()
        drawer.locator('[data-feedback-note]').fill('用户开始编辑的新内容不能被下一条覆盖。')
        held[0].fulfill(json=response)
        page.wait_for_timeout(150)
        self.assertEqual(drawer.locator('.perf-video-source [data-copy-label="视频 ID"]').get_attribute('data-copy-id'),self.video_id)
        self.assertEqual(drawer.locator('[data-feedback-note]').input_value(),'用户开始编辑的新内容不能被下一条覆盖。')
        self.assertEqual(len(self.server.app.review_followups.public()['entries']),1)


if __name__ == '__main__':
    unittest.main()
