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

    def test_default_lists_open_work_and_folds_finished_rows(self):
        # Exported but not yet on Notion is still open work; uploaded and skipped fold away.
        self.assertEqual(self.rows().count(), 4)
        self.assertEqual(self.page.locator('.po-chip').all_inner_texts(), ['2\n待决定', '0\n要做', '1\n做中', '1\n待上传', '1\n已上传', '0\n已完结', '1\n不做'])
        self.page.locator('[data-po-fold]').click()
        self.assertEqual(self.rows().count(), 6)
        self.assertIn('原因：没有实拍', self.page.locator('[data-po-row$="|e"]').inner_text())
        self.assertIn('图文 A 版已上传', self.page.locator('[data-po-row$="|f"]').inner_text())
        self.page.locator('[data-po-fold]').click()
        self.assertEqual(self.rows().count(), 4)

    def test_chip_shows_only_that_state_including_folded_ones(self):
        self.page.locator('[data-po-only="uploaded"]').click()
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.page.locator('[data-po-row] [data-po-open]').inner_text(), '查看')
        self.page.locator('[data-po-only="uploaded"]').click()
        self.assertEqual(self.rows().count(), 4)

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


if __name__ == "__main__":
    unittest.main()
