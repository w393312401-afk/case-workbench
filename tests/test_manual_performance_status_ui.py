"""Human classification drives performance surfaces, with disposable reviews and exports."""
import copy
from pathlib import Path
import unittest

import test_review_ui as fixture
import test_feedback_chain as feedback
import test_performance as performance
from review_legacy_fixture import seed_legacy_followup


STATUS_NAMES = {'high':'高参考价值','medium':'中等参考价值','weak':'弱参考价值','none':'无参考价值','unclassified':'待分类','pending':'待复盘'}
ARTIFACTS = Path(__file__).resolve().parents[1] / '.test-artifacts' / 'manual-review-status'


class ManualPerformanceStatusTests(unittest.TestCase):
    setUpClass = classmethod(fixture.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.ReviewPageTests.tearDownClass.__func__)
    stop_server = fixture.ReviewPageTests.stop_server
    request = fixture.ReviewPageTests.request
    ok = feedback.FeedbackChainHTTPTests.ok
    review = feedback.FeedbackChainHTTPTests.review
    chain = feedback.FeedbackChainHTTPTests.chain

    def setUp(self):
        fixture.ReviewPageTests.setUp(self)
        self.video_id = performance.LINKED
        self.window = self.server.app.performance.video(self.video_id)['window_end']

    def open_list(self, *, payload=None, query='tab=list', stored=None, width=1100, scheme='light'):
        context = self.browser.new_context(viewport={'width':width,'height':900}, color_scheme=scheme)
        self.addCleanup(context.close)
        if stored is not None:
            import json
            context.add_init_script('localStorage.setItem("workbench-video-data-filters", ' + json.dumps(json.dumps(stored)) + ')')
        context.grant_permissions(['clipboard-read','clipboard-write'],origin=self.base)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        if payload is not None:
            page.route('**/api/performance/videos?*', lambda route: route.fulfill(json=payload))
        page.goto(f'{self.base}/#/202H/videos?{query}')
        page.locator('.perf-table').wait_for()
        return page

    def sample_payload(self):
        payload = self.ok('GET','/api/performance/videos')
        base = next(v for v in payload['videos'] if v['video_id'] == self.video_id)
        payload['videos'] = []
        for index, key in enumerate([*STATUS_NAMES, 'legacy']):
            video = {**copy.deepcopy(base), 'video_id':str(7800000000000000000 + index), 'title':key,
                     'headline':key, 'case_id':'', 'status':'focus', 'status_label':'重点复制',
                     'reference':{'key':'orders','label':'成交可参考'}, 'flags':[{'key':'late','label':'后起量'},{'key':'no_orders','label':'高播未转化'}], 'this_week_gmv':10,
                     'review_status':{'key':key,'label':'不信任外部标签'}}
            if key == 'legacy':
                video.pop('review_status')
            payload['videos'].append(video)
        return payload

    def test_all_six_classifications_filter_counts_sort_and_copy_ignore_auto_signals(self):
        page = self.open_list(payload=self.sample_payload())
        for key, label in STATUS_NAMES.items():
            self.assertEqual(page.locator(f'.perf-legend-item[data-status-filter="{key}"] strong').inner_text(), '2' if key == 'pending' else '1')
            page.locator(f'.perf-legend-item[data-status-filter="{key}"]').click()
            self.assertEqual(page.locator('.perf-table .perf-status').all_text_contents(), [label] * (2 if key == 'pending' else 1))
            self.assertIn('review_status=' + key, page.url)
            page.locator(f'.perf-legend-item[data-status-filter="{key}"]').click()
        page.locator('[data-sort="status"]').click()
        self.assertEqual(page.locator('.perf-table .perf-status').all_text_contents(), list(STATUS_NAMES.values()) + ['待复盘'])
        page.locator('[data-sort="status"]').click()
        self.assertEqual(page.locator('.perf-table .perf-status').all_text_contents(), ['待复盘'] + list(reversed(STATUS_NAMES.values())))
        text = page.locator('#content').inner_text() + page.evaluate('CasePerformance.reviewMaterial()')
        for retired in ('重点复制','成交可参考','后起量','高播未转化','98%','75%','跟进规则'):
            self.assertNotIn(retired,text)
        for label in STATUS_NAMES.values():
            self.assertIn(label,text)
        # Newly written filters survive reload and use the separate manual-status URL key.
        page.locator('.perf-legend-item[data-status-filter="weak"]').click()
        page.reload()
        page.locator('.perf-table').wait_for()
        self.assertEqual(page.locator('.perf-table .perf-status').all_text_contents(),['弱参考价值'])

    def test_old_saved_filters_and_urls_do_not_reinterpret_automatic_pending_or_focus(self):
        for old in ('focus','pending'):
            with self.subTest(old=old):
                page = self.open_list(payload=self.sample_payload(),stored={'status':old,'tab':'list'},query='tab=list&status='+old)
                self.assertEqual(page.locator('[data-video-row]').count(),7)
                self.assertEqual(page.evaluate('CasePerformance.captureListState().filters.status'),'')
                self.assertEqual(page.evaluate('JSON.parse(localStorage.getItem("workbench-video-data-filters")).status'),'')
                self.assertNotIn('status=',page.url)

    def test_weak_stopped_failure_reclassification_updates_open_drawer_list_and_counts(self):
        lesson = '虽有成交，但剪辑与文案逻辑混乱、主题飘忽；本条流量期已结束。'
        self.review(legacy=True,note=lesson,decision='stop',outcome='negative',evidence='preliminary',reference_value='weak')
        page = self.open_list()
        row = page.locator(f'[data-video-row="{self.video_id}"]')
        self.assertEqual(row.locator('.perf-status').inner_text(),'弱参考价值')
        row.locator('.perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-classify]').wait_for()
        self.assertEqual(drawer.locator('.perf-video-status .perf-status').inner_text(),'弱参考价值')
        self.assertIn(lesson,drawer.inner_text())
        self.assertNotIn('观察窗到',drawer.locator('.perf-video-status').inner_text())
        self.assertIn('停测',drawer.inner_text())
        drawer.locator('[data-feedback-classify]').click()
        form = page.locator('.feedback-form-dialog')
        form.locator('[name=reference_value]').select_option('medium')
        form.get_by_role('button',name='保存分类',exact=True).click()
        form.wait_for(state='detached')
        drawer.locator('.perf-video-status .perf-status',has_text='中等参考价值').wait_for()
        self.assertEqual(row.locator('.perf-status').inner_text(),'中等参考价值')
        self.assertEqual(page.locator('.perf-legend-item[data-status-filter="weak"] strong').inner_text(),'0')
        self.assertEqual(page.locator('.perf-legend-item[data-status-filter="medium"] strong').inner_text(),'1')
        saved = self.chain()['review']
        self.assertEqual((saved['reference_value'],saved['decision'],saved['outcome'],saved['note']),('medium','stop','negative',lesson))
        self.assertEqual(self.server.app.creation.public()['plans'],[])
        drawer.locator('[data-drawer-close]').click()
        row.locator('.perf-title').click()
        drawer.locator('[data-feedback-classify]').wait_for()
        self.assertEqual(drawer.locator('.perf-video-status .perf-status').inner_text(),'中等参考价值')

    def test_saving_first_review_changes_pending_to_unclassified_without_closing_drawer(self):
        page = self.open_list()
        row = page.locator(f'[data-video-row="{self.video_id}"]')
        row.locator('.perf-title').click()
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-note]').fill('结束本条，稍后分类。')
        drawer.locator('.feedback-more-actions > summary').click()
        drawer.locator('[data-feedback-decide=stop]').click()
        drawer.locator('[data-feedback-classify]').wait_for()
        drawer.locator('.perf-video-status .perf-status',has_text='待分类').wait_for()
        self.assertEqual(row.locator('.perf-status').inner_text(),'待分类')

    def test_historical_window_label_does_not_borrow_or_overwrite_current_classification(self):
        self.review(decision='stop',reference_value='high')
        historical = '2026-09-06'
        seed_legacy_followup(self.server.app.review_followups, {'product':'202H','video_id':self.video_id,'window_end':historical}, decision='stop',reference_value='weak')
        page = self.open_list(query=f'tab=list&video={self.video_id}&feedback_window={historical}')
        drawer = page.locator('#perf-video-drawer[open]')
        drawer.locator('[data-feedback-classify]').wait_for()
        self.assertEqual(drawer.locator('.perf-video-status .perf-status').inner_text(),'弱参考价值')
        self.assertIn('历史复盘',drawer.inner_text())
        self.assertEqual(page.locator(f'[data-video-row="{self.video_id}"] .perf-status').inner_text(),'高参考价值')
        drawer.locator('[data-feedback-classify]').click()
        form = page.locator('.feedback-form-dialog')
        form.locator('[name=reference_value]').select_option('none')
        form.get_by_role('button',name='保存分类',exact=True).click()
        form.wait_for(state='detached')
        drawer.locator('.perf-video-status .perf-status',has_text='无参考价值').wait_for()
        self.assertEqual(page.locator(f'[data-video-row="{self.video_id}"] .perf-status').inner_text(),'高参考价值')

    def test_video_source_id_and_direct_link_remain_visible_in_both_drawers(self):
        page = self.open_list(width=390)
        url = f'https://www.tiktok.com/@bushcraftedge/video/{self.video_id}'
        for floating in (False,True):
            with self.subTest(floating=floating):
                if floating:
                    page.goto(f'{self.base}/#/202H/directions')
                    page.locator('#content').wait_for()
                    page.evaluate('id => CasePerformance.openVideoInPlace(id)',self.video_id)
                    drawer = page.locator('#perf-video-drawer-floating[open]')
                else:
                    page.locator(f'[data-video-row="{self.video_id}"] .perf-title').click()
                    drawer = page.locator('#perf-video-drawer[open]')
                source = drawer.locator('.perf-video-source')
                source.wait_for()
                self.assertIn(self.video_id,source.inner_text())
                source.locator('[data-copy-id]').click()
                self.assertEqual(page.evaluate('navigator.clipboard.readText()'),self.video_id)
                link = source.get_by_role('link',name='原视频')
                self.assertEqual(link.get_attribute('href'),url)
                self.assertEqual(link.get_attribute('target'),'_blank')
                self.assertIn('noopener',link.get_attribute('rel'))
                source.locator('[data-copy-video-link]').click()
                self.assertEqual(page.evaluate('navigator.clipboard.readText()'),url)
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                ARTIFACTS.mkdir(parents=True,exist_ok=True)
                for scheme in ('light','dark'):
                    page.emulate_media(color_scheme=scheme)
                    page.screenshot(path=str(ARTIFACTS/f'drawer-{scheme}-{"floating" if floating else "list"}-390.png'))
                drawer.locator('[data-drawer-close]').click()

    def test_manual_classification_layout_light_dark_and_mobile(self):
        ARTIFACTS.mkdir(parents=True,exist_ok=True)
        for scheme,width in (('light',1100),('dark',1100),('light',390),('dark',390)):
            with self.subTest(scheme=scheme,width=width):
                page = self.open_list(payload=self.sample_payload(),scheme=scheme,width=width)
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                self.assertEqual(page.locator('.perf-legend-item').count(),6)
                page.screenshot(path=str(ARTIFACTS/f'list-{scheme}-{width}.png'),full_page=True)


if __name__ == '__main__':
    unittest.main()
