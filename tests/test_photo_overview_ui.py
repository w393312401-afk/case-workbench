"""The 图文 tab lists every uploaded video by picture-carousel state and takes 做 / 不做 in place."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class PhotoOverviewBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 850})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="content" class="is-carousel"><div id="list" class="flow-list"></div></main>',
                                                            headers={"Content-Security-Policy": "style-src-elem 'self' 'unsafe-inline'; style-src-attr 'none'"}))
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/overview-test")
        for name in ("style.css", "buttons.css", "content-flow.css"):
            self.page.add_style_tag(path=str(STATIC / name))
        self.page.evaluate("""() => {
          window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
          window.calls = [];window.opened = [];window.toasts = [];window.reloads = 0;
          window.toast = text => window.toasts.push(text);
          window.api = async (path,options) => {window.calls.push({path,body:options?.body});return {};};
          window.PhotoCarousel = {open:args => window.opened.push(args.uid),askDelete:args => {window.deleting = args;}};
        }""")
        self.page.add_script_tag(path=str(STATIC / "photo-overview.js"))
        self.page.evaluate("""() => {
          const row = (uid,state,extra = {}) => ({job_id:'PRD-111111111111',job_title:'202H 开箱',timeline_uid:uid,timeline_name:'时间线 ' + uid,
            uploaded:true,registered:false,since:'2026-11-05T10:00',state,reason:'',...extra});
          window.data = {items:[row('a','pending'),row('b','pending'),row('c','doing'),row('d','exported'),row('e','skip',{reason:'没有实拍'}),row('f','uploaded',{notion_works:['A']})],
            counts:{pending:2,want:0,doing:1,exported:1,uploaded:1,skip:1}};
          window.draw = () => PhotoOverview.paint(document.querySelector('#list'),window.data,{reload:() => window.reloads++});
          window.draw();
        }""")

    def rows(self):
        return self.page.locator('[data-po-row]')

    def test_default_lists_open_work_and_ended_rows_have_their_own_tab(self):
        # Exported but not yet on Notion is still open work; uploaded, finished and skipped sit under 已结束.
        self.assertEqual(self.rows().count(), 4)
        # Open states with nothing in them (要做) take no tab.
        self.assertEqual(self.page.locator('.po-tab').all_text_contents(), ['要处理4', '待决定2', '做中1', '待上传1', '已结束2'])
        # One row per timeline: the script name rides beside it instead of heading a group of one.
        self.assertEqual(self.page.locator('.po-group-title, #list .flow-group').count(), 0)
        self.assertIn('202H 开箱', self.page.locator('[data-po-row$="|a"]').inner_text())
        self.page.locator('[data-po-only="done"]').click()
        self.assertEqual(self.rows().count(), 2)
        self.assertIn('原因：没有实拍', self.page.locator('[data-po-row$="|e"]').inner_text())
        self.assertIn('图文 A 版已上传', self.page.locator('[data-po-row$="|f"]').inner_text())
        self.assertIn('已上传 1 · 已完结 0 · 不做 1', self.page.locator('.po-strip').inner_text())
        self.page.locator('[data-po-only=""]').click()
        self.assertEqual(self.rows().count(), 4)

    def test_state_tab_shows_only_that_state(self):
        self.page.locator('[data-po-only="exported"]').click()
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.page.locator('[data-po-row] [data-po-open]').inner_text(), '上传 Notion')
        # No row button is the page's primary action.
        self.assertEqual(self.page.locator('#list button.primary').count(), 0)

    def test_week_old_exports_can_be_picked_and_finished_together(self):
        self.page.evaluate("() => {window.data.items.push({...window.data.items[3],timeline_uid:'g',timeline_name:'时间线 g'});window.data.counts.exported = 2;window.draw();}")
        self.page.locator('[data-po-only="exported"]').click()
        self.assertIn('超过 7 天', self.page.locator('[data-po-row$="|d"]').inner_text())
        self.page.locator('[data-po-select-stale]').click()
        self.assertIn('已选 2 条', self.page.locator('.po-batch').inner_text())
        self.page.once('dialog', lambda dialog: dialog.accept())
        self.page.locator('[data-po-batch-finish]').click()
        self.page.wait_for_function('window.reloads === 1')
        self.assertEqual([call['body'] for call in self.page.evaluate('window.calls')],
                         [{'timeline': 'd', 'decision': 'finish'}, {'timeline': 'g', 'decision': 'finish'}])

    def test_open_work_can_be_finished_and_a_finished_row_undone(self):
        self.page.locator('[data-po-row$="|c"] [data-po-finish]').click()
        self.page.wait_for_function('window.reloads === 1')
        self.assertEqual(self.page.evaluate('window.calls'), [{
            'path': '/api/production/jobs/PRD-111111111111/photo-decision', 'body': {'timeline': 'c', 'decision': 'finish'}}])
        self.page.evaluate("""() => {
          const row = window.data.items.find(item => item.timeline_uid === 'c');
          row.state = 'finished';row.finished_at = '2026-11-08T12:00:00+00:00';window.draw();
        }""")
        finished = self.page.locator('[data-po-row$="|c"]')
        self.assertIn('手动完结', finished.inner_text())
        self.assertEqual(finished.locator('[data-po-open]').inner_text(), '查看')
        finished.locator('[data-po-reopen]').click()
        self.page.wait_for_function('window.reloads === 2')
        self.assertEqual(self.page.evaluate('window.calls.at(-1).body.decision'), 'reopen')

    def test_delete_asks_first_and_only_on_rows_with_work(self):
        self.assertEqual(self.page.locator('[data-po-row$="|a"] [data-po-delete]').count(), 0)
        # Permanent delete waits in the row's ··· menu.
        self.assertFalse(self.page.locator('[data-po-row$="|c"] [data-po-delete]').is_visible())
        self.page.locator('[data-po-row$="|c"] .po-menu > summary').click()
        self.page.locator('[data-po-row$="|c"] [data-po-delete]').click()
        self.assertEqual(self.page.evaluate('[window.deleting.jobId,window.deleting.uid,window.calls.length]'), ['PRD-111111111111', 'c', 0])
        self.page.evaluate('window.deleting.onDone()')
        self.assertEqual(self.page.evaluate('window.reloads'), 1)

    def test_exported_row_opens_the_window_to_upload(self):
        button = self.page.locator('[data-po-row$="|d"] [data-po-open]')
        self.assertEqual(button.inner_text(), '上传 Notion')
        button.click()
        self.assertEqual(self.page.evaluate('window.opened'), ['d'])

    def test_want_records_the_decision_then_opens_the_window(self):
        self.page.locator('[data-po-row$="|a"] [data-po-want]').click()
        self.page.wait_for_function('window.opened.length === 1')
        self.assertEqual(self.page.evaluate('window.calls'), [{
            'path': '/api/production/jobs/PRD-111111111111/photo-decision', 'body': {'timeline': 'a', 'decision': 'want'}}])
        self.assertEqual(self.page.evaluate('window.opened'), ['a'])
        self.assertEqual(self.page.evaluate('window.reloads'), 1)

    def test_skip_is_one_click_and_stays_listed_with_undo_after_reload(self):
        self.page.locator('[data-po-row$="|a"] [data-po-skip]').click()
        self.page.wait_for_function('window.reloads === 1')
        self.assertEqual(self.page.evaluate('window.calls[0].body'), {'timeline': 'a', 'decision': 'skip'})
        self.page.evaluate("() => {window.data.items[0].state = 'skip';window.data.counts = {pending:1,want:0,doing:1,exported:1,uploaded:1,skip:2};window.draw();}")
        self.assertEqual(self.page.locator('[data-po-row$="|a"] [data-po-clear]').inner_text(), '撤销')
        self.assertEqual(self.rows().count(), 4)

    def test_batch_skip_marks_every_selected_pending_row(self):
        self.page.locator('[data-po-select-all]').click()
        self.assertIn('已选 2 条', self.page.locator('.po-batch').inner_text())
        self.page.locator('[data-po-batch-skip]').click()
        self.page.wait_for_function('window.reloads === 1')
        self.assertEqual([call['body'] for call in self.page.evaluate('window.calls')],
                         [{'timeline': 'a', 'decision': 'skip'}, {'timeline': 'b', 'decision': 'skip'}])

    def test_failure_is_reported_and_the_list_reloads(self):
        self.page.evaluate("() => {window.api = async () => {throw new Error('这条已经开始做图文了，不能再改成不做。');};}")
        self.page.locator('[data-po-row$="|b"] [data-po-skip]').click()
        self.page.wait_for_function('window.toasts.length === 1')
        self.assertIn('已经开始做', self.page.evaluate('window.toasts[0]'))
        self.assertEqual(self.page.evaluate('window.reloads'), 1)

    def test_empty_states_explain_what_belongs_here(self):
        self.page.evaluate("() => {window.data = {items:[],counts:{pending:0,want:0,doing:0,exported:0,uploaded:0,skip:0}};window.draw();}")
        self.assertIn('登记发布后', self.page.locator('#list .empty').inner_text())

    def test_title_opens_the_drawer_without_leaving_the_page(self):
        title = self.page.locator('[data-po-row$="|c"] a.flow-title')
        self.assertIn('photo=c', title.get_attribute('href'))  # Ctrl / middle click can still open the order in a new tab
        title.click()
        self.assertEqual(self.page.evaluate('window.opened'), ['c'])
        self.assertEqual(self.page.evaluate('location.hash'), '')


    def test_one_day_of_undecided_videos_exports_their_textless_cuts_together(self):
        self.page.evaluate("""() => {
          const base = window.data.items[0];
          window.data.items.push({...base,timeline_uid:'h',timeline_name:'时间线 h',since:'2026-11-14T15:30:00Z',state:'want'},
            {...base,timeline_uid:'i',timeline_name:'时间线 i',since:'2026-11-14',state:'pending'},
            {...base,timeline_uid:'j',timeline_name:'时间线 j',since:'2026-11-14T03:00:00Z',state:'pending',importing:true});
          window.api = async (path,options) => {window.calls.push({path,body:options?.body});
            return {started:[{job_id:'PRD-111111111111',timeline_uid:'h'}],skipped:[{job_id:'PRD-111111111111',timeline_uid:'i',error:'这条成片没有达芬奇工程关联'}]};};
          window.confirm = () => true;window.draw();
        }""")
        day = self.page.evaluate("() => {const d = new Date('2026-11-14T15:30:00Z');return `${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;}")
        options = self.page.locator('[data-po-day] option').all_text_contents()
        self.assertTrue(options[0].startswith('11-'), options)
        self.assertTrue(any(text.endswith('· 2 条') for text in options) or day != '11-14', options)
        # A row whose export is already running is not offered again and says what it is doing.
        running = self.page.locator('[data-po-row$="|j"]')
        self.assertIn('导出无字版', running.inner_text())
        self.assertEqual(running.locator('[data-po-select]').count(), 0)
        self.assertEqual(running.locator('[data-po-open]').inner_text(), '查看进度')
        self.page.locator('[data-po-day]').select_option('2026-11-14')
        self.page.locator('[data-po-day-export]').click()
        self.page.wait_for_function('window.reloads > 0')
        call = self.page.evaluate('window.calls[0]')
        self.assertEqual(call['path'], '/api/photo-sets/import-day')
        uids = [item['timeline_uid'] for item in call['body']['items']]
        self.assertIn('i', uids)
        self.assertNotIn('j', uids)
        self.assertEqual(self.page.evaluate('window.toasts.at(-1)'), '已排队导出 1 条无字版；1 条没导：时间线 i（这条成片没有达芬奇工程关联）')

    def test_failed_export_shows_on_the_row_and_the_day_bar_hides_on_closed_tabs(self):
        self.page.evaluate("() => {window.data.items[0].import_error = '素材离线';window.draw();}")
        self.assertIn('无字版导出失败：素材离线', self.page.locator('[data-po-row$="|a"]').inner_text())
        self.assertEqual(self.page.locator('[data-po-day] option').all_text_contents(), ['11-05 周四 · 2 条'])
        self.page.locator('[data-po-only="done"]').click()
        self.assertEqual(self.page.locator('[data-po-day]').count(), 0)


    def test_finalized_rows_can_be_sent_out_in_german_without_stickers(self):
        # 外语版（2026-11-24）：有已定稿版本的行（含已上传）能勾，批量出德语版；行上写着外语版的进度和结果。
        self.page.evaluate("""() => {
          const items = window.data.items;
          items[3].translatable = true;
          items[5].translatable = true;items[5].foreign = [{market:'de',label:'DE',keys:['A-DE'],done:1}];
          items[2].translatable = true;items[2].translating = {status:'running',market:'de',progress:'AI 正在把 A 版改成德语…'};
          window.api = async (path,options) => {window.calls.push({path,body:options?.body});return {started:[{job_id:'PRD-111111111111',timeline_uid:'d'}],skipped:[{job_id:'PRD-111111111111',timeline_uid:'f',error:'已经有德语版'}]};};
          window.draw();
        }""")
        self.assertIn('AI 正在把 A 版改成德语…', self.page.locator('[data-po-row$="|c"]').inner_text())
        self.page.locator('[data-po-select$="|d"]').check()
        self.assertEqual(self.page.locator('[data-po-translate]').inner_text(), '出德语版')
        self.page.locator('[data-po-only="done"]').click()
        self.assertIn('德语版 A-DE 已导出', self.page.locator('[data-po-row$="|f"]').inner_text())
        self.page.locator('[data-po-select$="|f"]').check()
        self.page.locator('[data-po-auto-export]').uncheck()
        asked = []
        self.page.on("dialog", lambda dialog: (asked.append(dialog.message), dialog.accept()))
        self.page.locator('[data-po-translate]').click()
        self.page.wait_for_function('window.reloads > 0')
        self.assertIn('评论贴纸不带', asked[0])
        call = self.page.evaluate('window.calls.at(-1)')
        self.assertEqual(call['path'], '/api/photo-sets/translate')
        self.assertEqual((call['body']['market'], call['body']['export']), ('de', False))
        self.assertEqual([item['timeline_uid'] for item in call['body']['items']], ['f'])
        self.assertEqual(self.page.evaluate('window.toasts.at(-1)'), '已排队出德语版 1 条；1 条没排：时间线 f（已经有德语版）')

    def test_language_pick_changes_the_button_and_rows_being_translated_are_left_out(self):
        self.page.evaluate("""() => {
          window.data.items[3].translatable = true;
          window.data.items[1].translatable = true;window.data.items[1].translating = {status:'queued',market:'de'};
          window.draw();
        }""")
        self.assertIn('排队出德语版', self.page.locator('[data-po-row$="|b"]').inner_text())
        self.page.locator('[data-po-select$="|b"]').check()
        self.assertEqual(self.page.locator('[data-po-translate]').count(), 0)
        self.page.locator('[data-po-select$="|d"]').check()
        self.page.locator('[data-po-market]').select_option('uk')
        self.assertEqual(self.page.locator('[data-po-translate]').inner_text(), '出英式英语版（1 条已定稿的）')


if __name__ == "__main__":
    unittest.main()
