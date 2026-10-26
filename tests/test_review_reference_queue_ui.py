"""Manual reference classifications stay visible and filter only completed reviews."""
from pathlib import Path
import unittest

import test_review_followup_filters_ui as filters


class ReviewReferenceQueueTests(unittest.TestCase):
    setUpClass = classmethod(filters.ReviewFollowupFilterTests.setUpClass.__func__)
    tearDownClass = classmethod(filters.ReviewFollowupFilterTests.tearDownClass.__func__)
    open_queue = filters.ReviewFollowupFilterTests.open_queue
    open_filters = filters.ReviewFollowupFilterTests.open_filters
    ids = filters.ReviewFollowupFilterTests.ids

    def classified_queue(self, *, width=1100, scheme="light"):
        page = self.open_queue(width=width, scheme=scheme)
        page.evaluate("""() => {
          const base={product:'202H',posted_on:'2026-09-01',window_end:'2026-09-13',
            last_data:'2026-10-23',status:'follow',window_views:1000,orders:2};
          const refs=['','none','weak','medium','high','high','high'];
          queueFixture.videos=refs.map((value,index)=>({...base,video_id:'review-'+index,
            headline:'人工复盘 '+index,orders:index===0 ? 200 : 0}));
          queueFixture.entries=queueFixture.videos.map((video,index)=>({...video,
            state:'reviewed',decision:'stop',note:'人工确认的结论',outcome:'negative',
            reference_value:refs[index],at:'2026-10-20T12:00:00+08:00'}));
          delete queueFixture.entries[0].reference_value;
          queueFixture.videos.push({...base,video_id:'ready',headline:'今日待复盘'});
          mountQueue();
        }""")
        self.open_filters(page)
        page.locator('[data-followup-filter]').select_option('reviewed')
        return page

    def test_completed_rows_show_manual_classification_without_expanding(self):
        page = self.classified_queue()
        labels = page.locator('[data-followup-reference-value]').all_text_contents()
        self.assertEqual(labels, ['待分类', '无参考价值', '弱参考价值', '中等参考价值', '高参考价值'])
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '1–5 / 共 7 条')
        for badge in page.locator('[data-followup-reference-value]').all():
            self.assertTrue(badge.is_visible())
        self.assertTrue(all(not details.evaluate('(el)=>el.open')
                            for details in page.locator('.followup-details').all()))
        # A high-selling legacy video is still unclassified; no metric inference.
        page.locator('[data-followup-reference-filter]').select_option('unclassified')
        self.assertEqual(self.ids(page), ['review-0'])
        self.assertEqual(page.locator('[data-followup-reference-value]').inner_text(), '待分类')

    def test_reference_filter_counts_and_summary_survive_remount_and_clear_on_other_queue(self):
        page = self.classified_queue()
        page.locator('[data-followup-next]').click()
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '6–7 / 共 7 条')
        page.locator('[data-followup-reference-filter]').select_option('high')
        self.assertEqual(self.ids(page), ['review-4', 'review-5', 'review-6'])
        self.assertEqual(page.locator('.followup-tools > span').inner_text(), '3 条')
        self.assertFalse(page.locator('.followup-pager').is_visible())
        page.evaluate('mountQueue()')
        self.assertIn('高参考价值', page.locator('.followup-active-filter').inner_text())
        self.assertTrue(page.locator('.followup-active-filter').is_visible())
        self.open_filters(page)
        self.assertEqual(page.locator('[data-followup-reference-filter]').input_value(), 'high')
        page.locator('[data-followup-queue=ready]').click()
        self.assertEqual(self.ids(page), ['ready'])
        self.assertEqual(page.locator('[data-followup-reference-filter]').count(), 0)
        self.assertEqual(page.locator('.followup-active-filter').count(), 0)
        page.locator('[data-followup-filter]').select_option('reviewed')
        self.assertEqual(page.locator('[data-followup-reference-filter]').input_value(), '')
        self.assertEqual(page.locator('.followup-tools > span').inner_text(), '7 条')
        page.locator('[data-followup-reference-filter]').select_option('weak')
        self.assertEqual(self.ids(page), ['review-2'])
        page.locator('[data-followup-reset]').click()
        self.assertEqual(self.ids(page), ['ready'])

    def test_all_classifications_combine_with_search_and_fit_light_dark_mobile(self):
        artifact = Path(__file__).resolve().parents[1] / '.test-artifacts' / 'review-reference-value'
        artifact.mkdir(parents=True, exist_ok=True)
        for scheme, width in (('light', 1100), ('dark', 390)):
            with self.subTest(scheme=scheme, width=width):
                page = self.classified_queue(width=width, scheme=scheme)
                for value, expected in (('none', ['review-1']), ('weak', ['review-2']),
                                        ('medium', ['review-3']), ('high', ['review-4','review-5','review-6'])):
                    page.locator('[data-followup-reference-filter]').select_option(value)
                    self.assertEqual(self.ids(page), expected)
                page.locator('[data-followup-search]').fill('人工复盘 5')
                self.assertEqual(self.ids(page), ['review-5'])
                page.locator('[data-followup-search]').fill('')
                page.locator('.followup-controls > summary').click()
                self.assertTrue(page.locator('.followup-active-filter').is_visible())
                self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
                page.screenshot(path=str(artifact / f'queue-{scheme}-{width}.png'), full_page=True)


if __name__ == '__main__':
    unittest.main()
