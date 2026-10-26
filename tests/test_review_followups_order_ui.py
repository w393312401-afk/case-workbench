"""Browser check that priority reasons precede ownership, with stable fallback ordering."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "static" / "review-followups.js"


class ReviewQueueOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_anomalies_lead_own_posts_and_then_recent_backlog(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="queue"></main>'))
        page.goto("http://127.0.0.1:43220/review-queue-test")
        page.evaluate("""() => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }""")
        page.add_script_tag(path=str(SCRIPT))
        page.evaluate("""() => {
          // 25 older backlog windows, then one post registered from a script, due last.
          const videos = Array.from({length:25}, (_, i) => ({video_id:String(1000 + i), product:'202H',posted_on:'2026-03-01',last_data:'2026-10-23', window_end:`2026-04-${String(i % 28 + 1).padStart(2,'0')}`,
            status:'drop', status_label:'放弃', headline:`历史视频 ${i}`, flags:i === 24 ? [{key:'late', label:'后起'}] : []}));
          videos.push({video_id:'7697632860966030623', product:'202H',posted_on:'2026-10-01',last_data:'2026-10-23', window_end:'2026-10-18', status:'follow', status_label:'跟进', headline:'工作台成片'});
          ReviewFollowups.mount(document.querySelector('#queue'), {videos, entries:[], revision:'r1', week:{today:'2026-10-23', start:'2026-10-19', end:'2026-10-25'},
            scripts:[{id:'SCR-1', video_id:'7697632860966030623', published_on:'2026-10-17', status:'published'}], refresh:async () => {}, url:hash => hash});
        }""")
        rows = page.locator('.followup-row')
        self.assertEqual(rows.count(), 5)
        self.assertIn('今天先看', page.locator('.followup-heading').inner_text())
        self.assertEqual(rows.first.locator('.followup-first').inner_text(), '先看这条')
        self.assertFalse(page.locator('[data-followup-search]').is_visible())
        self.assertEqual(page.locator('.followup-controls').get_attribute('open'), None)
        self.assertFalse(rows.first.locator('[data-followup-edit]').is_visible())
        self.assertEqual(rows.first.locator('button:visible').count(), 1)
        self.assertTrue(rows.first.locator('[data-followup-chain]').is_visible())
        self.assertIn('历史视频 24', rows.first.inner_text())
        self.assertIn('有后起信号', rows.first.inner_text())
        self.assertIn('工作台成片', rows.nth(1).inner_text())
        self.assertEqual(rows.nth(1).locator('.home-chip', has_text='工作台发布').count(), 1)
        self.assertEqual(page.locator('.home-chip', has_text='工作台发布').count(), 1)
        # After the own post, the most recently due backlog comes first.
        self.assertIn('历史视频 23', rows.nth(2).inner_text())
        first_page = rows.evaluate_all('nodes=>nodes.map(node=>node.dataset.followupVideo)')
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '1–5 / 共 26 条')
        page.locator('[data-followup-next]').click()
        self.assertEqual(page.locator('.followup-pager > span').inner_text(), '6–10 / 共 26 条')
        self.assertEqual(rows.count(), 5)
        self.assertFalse(set(first_page) & set(rows.evaluate_all('nodes=>nodes.map(node=>node.dataset.followupVideo)')))
        page.locator('[data-followup-prev]').click()
        self.assertEqual(first_page, rows.evaluate_all('nodes=>nodes.map(node=>node.dataset.followupVideo)'))
        # Flags ride on the meta line; the single extra action is a text button, not a ⋯ menu.
        self.assertEqual(rows.first.locator('small .followup-flags .home-chip', has_text='后起').count(), 1)
        self.assertEqual(page.locator('.followup-menu').count(), 0)
        rows.nth(1).locator('.followup-details > summary').click()
        self.assertEqual(rows.nth(1).locator('[data-followup-edit]').inner_text(), '暂缓')
        # The default list leaves counting to the tab; a search shows how many it kept.
        self.assertEqual(page.locator('.followup-tools > span').count(), 0)
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-search]').fill('历史视频 2')
        self.assertEqual(page.locator('.followup-tools > span').inner_text(), '6 条')
        page.locator('.followup-controls > summary').click()
        self.assertTrue(page.locator('.followup-active-filter').is_visible())
        self.assertIn('历史视频 2', page.locator('.followup-active-filter').inner_text())
        # 「暂缓」 opens the dialog already on 暂缓, with its date field ready.
        rows.first.locator('.followup-details > summary').click()
        page.locator('.followup-row [data-followup-edit]').first.click()
        dialog = page.locator('.followup-dialog')
        self.assertEqual(dialog.locator('[name=state]').input_value(), 'deferred')
        self.assertTrue(dialog.locator('[data-defer]').is_visible())
        self.assertTrue(dialog.locator('[name=defer_until]').evaluate('node => node === document.activeElement'))
        self.assertEqual(errors, [])

    def test_deferred_and_reviewed_rows_name_what_the_button_changes(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="queue"></main>'))
        page.goto("http://127.0.0.1:43220/review-queue-states")
        page.evaluate("""() => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
        }""")
        page.add_script_tag(path=str(SCRIPT))
        page.evaluate("""() => {
          const video = (id, end) => ({video_id:id, product:'202H', window_end:end, status:'drop', status_label:'放弃', headline:`视频 ${id}`});
          ReviewFollowups.mount(document.querySelector('#queue'), {videos:[video('1','2026-10-11'), video('2','2026-10-04')],
            entries:[{video_id:'1', product:'202H', window_end:'2026-10-11', state:'deferred', defer_until:'2026-11-30'},
                     {video_id:'2', product:'202H', window_end:'2026-10-04', state:'reviewed', decision:'none', note:'无结论'}],
            revision:'r1', week:{today:'2026-10-23', start:'2026-10-19', end:'2026-10-25'}, scripts:[], refresh:async () => {}, url:hash => hash});
        }""")
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-filter]').select_option('all')
        page.locator('.followup-details > summary').nth(0).click()
        page.locator('.followup-details > summary').nth(1).click()
        buttons = page.locator('[data-followup-edit]')
        self.assertEqual(buttons.all_inner_texts(), ['改日期', '改状态'])
        buttons.first.click()
        self.assertEqual(page.locator('.followup-dialog [name=state]').input_value(), 'deferred')

    def test_primary_action_matches_each_queue_and_opens_the_same_video(self):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.route('**/*', lambda route: route.fulfill(content_type='text/html', body='<main id="queue"></main>'))
        page.goto('http://127.0.0.1:43220/review-queue-actions')
        page.evaluate("""() => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          window.opened = [];
          window.CasePerformance = {openVideoInPlace: (id,options) => opened.push({id,window:options.feedbackWindow,changed:typeof options.onChanged,closed:typeof options.onClose})};
        }""")
        page.add_script_tag(path=str(SCRIPT))
        page.evaluate("""() => {
          const base = {product:'202H',posted_on:'2026-10-01',window_end:'2026-10-18',last_data:'2026-10-23',status:'follow',window_views:1500,orders:1};
          ReviewFollowups.mount(document.querySelector('#queue'), {
            videos:[{...base,video_id:'ai-ready',headline:'核对草稿'}, {...base,video_id:'ready',headline:'开始复盘'},
                    {...base,video_id:'observing',headline:'继续观察',posted_on:'2026-10-20'},
                    {...base,video_id:'missing',headline:'补数据',status:'incomplete'}],
            entries:[],revision:'r1',week:{today:'2026-10-23',start:'2026-10-19',end:'2026-10-25'},scripts:[],
            tasks:[{task_id:'AIT-fixture',action:'retrospective',status:'completed',created_at:'2026-10-22',
                    context:{review_video:{video_id:'ai-ready',window_end:'2026-10-18'}}}],
            refresh:async()=>{},url:hash=>hash});
        }""")
        page.locator('.followup-controls > summary').click()
        page.locator('[data-followup-filter]').select_option('all')
        for video_id, label in [('ai-ready', '核对结论'), ('ready', '开始复盘'),
                                ('observing', '查看观察'), ('missing', '核对数据')]:
            row = page.locator(f'[data-followup-video="{video_id}"]')
            self.assertEqual(row.locator('button:visible').count(), 1)
            self.assertEqual(row.locator('[data-followup-chain]').inner_text(), label)
            row.locator('[data-followup-chain]').click()
        self.assertEqual(page.evaluate('opened'), [dict(id=video_id, window='2026-10-18', changed='function', closed='function')
                                                    for video_id in ['ai-ready', 'ready', 'observing', 'missing']])


if __name__ == "__main__":
    unittest.main()
