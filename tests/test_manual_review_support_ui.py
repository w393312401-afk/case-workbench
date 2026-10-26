"""Manual reference labels stay consistent in supporting review and case views."""
import unittest

import test_review_observation_panel_ui as observation
import test_sales_data_ui as sales_ui


class ManualFeedbackStatusTests(unittest.TestCase):
    setUpClass = classmethod(observation.ObservationPanelTests.setUpClass.__func__)
    tearDownClass = classmethod(observation.ObservationPanelTests.tearDownClass.__func__)
    panel = observation.ObservationPanelTests.panel

    def test_exact_window_label_and_reload_callback_ignore_automatic_judgment(self):
        page = self.panel(mature=True, review={"state": "reviewed", "decision": "stop",
                                              "reference_value": "weak", "note": "保留失败教训。"})
        page.evaluate("""() => {
          chain.current_window = false;
          chain.review_status = {key:'weak',label:'弱参考价值'};
          chain.summary.status = 'focus';
          chain.summary.status_label = '重点复制';
          chain.summary.status_detail = '流量高，建议重点复制';
          window.loaded = [];
          FeedbackChain.mount(document.querySelector('#panel'), {
            video:{product:chain.product,video_id:chain.video_id,window_end:chain.window_end,
              review_status:{key:'high',label:'高参考价值'}},
            onLoaded:data => loaded.push({window_end:data.window_end,...data.review_status})
          });
        }""")
        page.wait_for_function("loaded.length === 1")
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '弱参考价值')
        page.locator('.feedback-data-details > summary').click()
        self.assertIn('历史观察窗', page.locator('#panel').inner_text())
        for retired in ('重点复制', '跟进状态', '已判定视频'):
            self.assertNotIn(retired, page.locator('#panel').inner_text())
        self.assertEqual(page.evaluate('loaded[0]'),
                         {'window_end': '2026-11-01', 'key': 'weak', 'label': '弱参考价值'})

        page.evaluate("chain.review_status = {key:'high',label:'高参考价值'}")
        page.locator('[data-feedback-refresh]').click()
        page.wait_for_function("loaded.length === 2")
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '高参考价值')
        self.assertEqual(page.evaluate('loaded[1].key'), 'high')
        self.assertEqual(page.evaluate('requests'), [])

    def test_legacy_review_without_classification_shows_pending_classification(self):
        page = self.panel(mature=True, review={'state': 'reviewed', 'decision': 'stop',
                                              'reference_value': None, 'note': '已人工复盘。'})
        self.assertEqual(page.locator('[data-feedback-reference-label]').inner_text(), '待分类')
        self.assertEqual(page.evaluate('requests'), [])

    def test_embedded_review_keeps_source_visible_and_copyable_when_metrics_are_not_external(self):
        page = self.panel(mature=True, width=390, scheme='dark')
        page.add_script_tag(path=str(observation.ROOT / 'static' / 'copy-id.js'))
        page.evaluate("""() => {
          window.copied = [];
          window.copyText = async value => copied.push(value);
          chain.account = '@blade.test';
          FeedbackChain.mount(document.querySelector('#panel'), {video:{product:chain.product,video_id:chain.video_id,
            window_end:chain.window_end,headline:'嵌入复盘视频'}});
        }""")
        dialog = page.locator('#panel')
        source = dialog.locator('.feedback-source')
        source.wait_for()
        self.assertFalse(dialog.locator('.feedback-data-details').evaluate('node => node.open'))
        self.assertTrue(source.get_by_text('7691330683482017055', exact=True).is_visible())
        link = source.get_by_role('link', name='TikTok 原视频 ↗', exact=True)
        original_url = 'https://www.tiktok.com/@blade.test/video/7691330683482017055'
        self.assertEqual(link.get_attribute('href'), original_url)
        self.assertEqual(link.get_attribute('target'), '_blank')
        source.locator('[data-copy-label="视频 ID"]').click()
        source.locator('[data-copy-label="原视频链接"]').click()
        self.assertEqual(page.evaluate('copied'), ['7691330683482017055', original_url])
        self.assertFalse(dialog.evaluate('node => node.scrollWidth > node.clientWidth'))
        page.evaluate("""() => FeedbackChain.mount(document.querySelector('#panel'), {
          video:{product:chain.product,video_id:chain.video_id,window_end:chain.window_end},
          externalMetrics:{window_end:chain.window_end}
        })""")
        page.locator('#panel .feedback-evidence').wait_for(state='attached')
        self.assertEqual(page.locator('#panel .feedback-source').count(), 0)


class ManualCaseStatusTests(unittest.TestCase):
    setUp = sales_ui.SalesDataBrowserTests.setUp
    setUpClass = classmethod(sales_ui.SalesDataBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(sales_ui.SalesDataBrowserTests.tearDownClass.__func__)
    stop_server = sales_ui.SalesDataBrowserTests.stop_server
    request = sales_ui.SalesDataBrowserTests.request
    make_playable_video = sales_ui.SalesDataBrowserTests.make_playable_video
    open_sales_page = sales_ui.SalesDataBrowserTests.open_sales_page

    def test_case_sales_data_uses_only_manual_classification(self):
        snapshot = sales_ui.sales(self.store.read_case(self.identifier))
        snapshot['video'].update(status='focus', status_label='重点复制')
        snapshots = {self.identifier: snapshot}
        for key, label in ((None, '待复盘'), ('pending', '待复盘'), ('unclassified', '待分类'),
                           ('none', '无参考价值'), ('weak', '弱参考价值'),
                           ('medium', '中等参考价值'), ('high', '高参考价值')):
            with self.subTest(key=key):
                if key is not None:
                    snapshot['video']['review_status'] = {'key': key, 'label': label}
                page = self.open_sales_page(f'#case/{self.identifier}', sales_snapshots=snapshots)
                page.locator('.detail-tabs [data-tab="data"]').click()
                section = page.locator('#detail-section-performance')
                section.get_by_text('复盘分类：' + label, exact=True).wait_for()
                self.assertNotIn('重点复制', section.inner_text())
                self.assertNotIn('跟进状态', section.inner_text())
                page.context.close()


if __name__ == '__main__':
    unittest.main()
