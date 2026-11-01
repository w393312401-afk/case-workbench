"""Browser checks for durable publication retries, independent of real user files."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


SCRIPT = Path(__file__).resolve().parents[1] / "static" / "video-publication.js"
COPY_ID = SCRIPT.with_name("copy-id.js")
JOB = "PRD-111111111111"


class VideoPublicationBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main><section id="publication"></section></main>'))
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/publication-test")
        self.mount()

    def mount(self, setup=""):
        # `setup` runs after the default fixture and before mounting, so a test can add a saved registration or sales rows.
        self.page.add_script_tag(path=str(SCRIPT))
        self.page.evaluate("""async (setup) => {
          window.calls = [];
          window.calendarCalls = [];
          window.responseStatus = 503;
          window.saved = false;
          window.publicationDetail = {revision:'revision-one',source_signature:'source-one',can_register:true,history:[],
            source:{script:{script_version:1},take:{id:'TAK-111111111111'},timeline:{name:'Reviewed'}}};
          window.salesVideos = [];
          new Function(setup)();
          const api = async (url,options) => {
            if (!options && url.startsWith('/api/performance/videos')) return {videos:structuredClone(window.salesVideos)};
            if (!options) return structuredClone(window.publicationDetail);
            if (url === '/api/content/calendar') {window.calendarCalls.push(structuredClone(options.body));return {};}
            window.calls.push(structuredClone(options.body));
            if (window.responseStatus) throw Object.assign(new Error('测试响应'),{status:window.responseStatus});
            return {};
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:{id:'PRD-111111111111'},api,
            onSaved:async () => {window.saved = true;}});
        }""", setup)

    def fill(self):
        # Only the video ID is up front; the file and account sit in the optional fold.
        self.page.locator('.video-publication-more').evaluate_all('nodes => nodes.forEach(node => { node.open = true; })')
        self.page.locator('[name="file_path"]').fill('C:\\Videos\\actual.mp4')
        self.page.locator('[name="account"]').fill('fixture')
        self.page.locator('[name="post_id"]').fill('7999999999999999901')
        self.page.locator('[name="file_confirmed"]').check()

    def test_screen_copy_and_full_text(self):
        self.assertEqual(self.page.locator('[data-copy-screen]').count(), 0)
        self.mount("""window.publicationDetail.copy = {version: 1, titles: ['🔴Title #tag'], titles_zh: ['中文标题'],
          zh: '中文口播', en: 'English speech', screen_en: 'ONE KIT\\nALL STAGES',
          screen_zh: '一套工具\\n覆盖全阶段'};
          window.copyText = text => {window.copiedText = text;};""")
        row = self.page.locator('.video-publish-copy-row')
        # Top to bottom is the order they are pasted at publish time.
        self.assertEqual(row.locator('.video-publish-copy-label').all_inner_texts(), ['钩子屏显', '标题', '文案'])
        # Chinese sits beside the foreign words but stays out of the clipboard.
        self.assertIn('一套工具', row.locator('[data-copy-screen]').inner_text())
        row.locator('[data-copy-screen]').click()
        self.assertEqual(self.page.evaluate('window.copiedText'), 'ONE KIT\nALL STAGES')
        self.assertIn('中文标题', row.locator('[data-copy-title]').inner_text())
        row.locator('[data-copy-title]').click()
        self.assertEqual(self.page.evaluate('window.copiedText'), '🔴Title #tag')
        row.locator('[data-copy-publish]').click()
        self.assertEqual(self.page.evaluate('window.copiedText'), '中文口播\n\nEnglish speech')
        row.locator('summary').click()
        full = row.locator('.video-publish-copy').text_content()
        self.assertIn('中文口播', full)
        self.assertIn('English speech', full)
        self.assertEqual(self.page.evaluate('window.calls'), [])

    def test_missing_title_chinese_fills_in_and_failure_leaves_copy_working(self):
        self.mount("""window.publicationDetail.copy = {titles: ['🔴Title #tag'], titles_zh: ['']};
          window.copyText = text => {window.copiedText = text;};""")
        title = self.page.locator('[data-copy-title]')
        title.locator('small', has_text='中文对照暂时没生成').wait_for()
        title.click()
        self.assertEqual(self.page.evaluate('window.copiedText'), '🔴Title #tag')
        self.page.evaluate("""window.responseStatus = 0""")
        self.page.evaluate("""async () => {
          await VideoPublication.mount(document.querySelector('#publication'),{job:{id:'PRD-111111111111'},
            api:async (url,options) => options ? {titles_zh:['后来的中文']} : structuredClone(window.publicationDetail)});
        }""")
        title.locator('small', has_text='后来的中文').wait_for()

    def test_screen_chinese_alone_is_visible_but_not_copyable_as_foreign_text(self):
        self.mount("window.publicationDetail.copy = {screen_en:'', screen_zh:'中文屏显'};")
        self.assertEqual(self.page.locator('[data-copy-screen]').count(), 0)
        self.assertIn('中文屏显', self.page.locator('.video-publish-copy-row').inner_text())

    def test_post_id_fills_its_posting_day_and_a_hand_picked_date_stays(self):
        post, date = self.page.locator('[name="post_id"]'), self.page.locator('[name="published_on"]')
        post.fill('7697632860966030623')
        self.assertEqual(date.input_value(), '2026-10-17')
        post.fill('https://www.tiktok.com/@fixture/video/7698015486365273375')
        post.blur()
        self.page.wait_for_function("document.querySelector('[name=account]').value === 'fixture'")
        self.assertEqual((post.input_value(), date.input_value()), ('7698015486365273375', '2026-10-18'))
        self.assertIn('日期按视频 ID 补上', self.page.locator('[data-account-hint]').inner_text())
        date.evaluate("(node, value) => { node.value = value; node.dispatchEvent(new Event('input')); }", '2026-10-19')
        post.fill('7697632860966030623')
        self.assertEqual(date.input_value(), '2026-10-19')
        for value in ('1234567890123456789012', '9999999999999999999999999'):
            post.fill(value)
            post.blur()
            self.assertEqual(date.input_value(), '2026-10-19')

    SALES = "window.salesVideos = [{video_id:'7697632860966030623', account:'sharpaledgelab', posted_on:'2026-10-17'}]"

    def test_sales_data_fills_account_and_date_but_not_a_hand_picked_date(self):
        self.mount(self.SALES)
        post, date = self.page.locator('[name="post_id"]'), self.page.locator('[name="published_on"]')
        post.fill('7697632860966030623')
        post.blur()
        self.page.wait_for_function("document.querySelector('[name=account]').value === 'sharpaledgelab'")
        self.assertEqual(date.input_value(), '2026-10-17')
        self.assertEqual(self.page.locator('[data-account-hint]').inner_text(), '已按销售数据里的这条视频补上账号和日期')
        self.mount(self.SALES)
        date.evaluate("(node, value) => { node.value = value; node.dispatchEvent(new Event('input')); }", '2026-10-19')
        post.fill('7697632860966030623')
        post.blur()
        self.page.wait_for_function("document.querySelector('[name=account]').value === 'sharpaledgelab'")
        self.assertEqual(date.input_value(), '2026-10-19')
        self.assertEqual(self.page.locator('[data-account-hint]').inner_text(), '已按销售数据里的这条视频补上账号')

    def test_a_saved_date_that_differs_from_the_id_is_kept(self):
        # An earlier registration typed on the day it was entered; pasting the same post again must not move it.
        self.mount("""window.publicationDetail.publication = {id:'VPU-111111111111', post_id:'7697632860966030623', published_on:'2026-10-19',
          account:'sharpaledgelab', script_version:1, registered_at:'2026-10-19T05:27:48Z', source:{}}""")
        self.page.locator('.video-publication-editor > summary').click()
        post, date = self.page.locator('[name="post_id"]'), self.page.locator('[name="published_on"]')
        self.assertEqual(date.input_value(), '2026-10-19')
        post.fill('https://www.tiktok.com/@sharpaledgelab/video/7697632860966030623')
        post.blur()
        self.assertEqual(date.input_value(), '2026-10-19')

    def test_unknown_result_locks_form_and_replays_same_request_after_reload(self):
        self.fill()
        self.page.locator('[type="submit"]').click()
        self.page.locator('[data-publication-error]:not([hidden])').wait_for()
        self.assertTrue(self.page.locator('[name="account"]').is_disabled())
        self.assertTrue(self.page.locator('[data-refresh-publication]').is_disabled())
        first = self.page.evaluate('window.calls[0]')
        self.page.reload()
        self.mount()
        self.assertEqual(self.page.locator('[name="account"]').input_value(), 'fixture')
        self.assertTrue(self.page.locator('[name="account"]').is_disabled())
        self.page.evaluate('window.responseStatus = 0')
        self.page.get_by_role('button', name='重试同一登记', exact=True).click()
        self.page.wait_for_function('window.saved')
        self.assertEqual(first, self.page.evaluate('window.calls[0]'))
        self.assertIsNone(self.page.evaluate(f'sessionStorage.getItem("video-publication:{JOB}")'))

    def test_calendar_choice_defaults_to_not_uploaded_until_a_post_id_is_given(self):
        self.assertEqual(self.page.locator('[name="calendar_uploaded"]').input_value(), '0')
        self.page.locator('[name="post_id"]').fill('12345')
        self.assertEqual(self.page.locator('[name="calendar_uploaded"]').input_value(), '0')
        self.page.locator('[name="post_id"]').fill('7999999999999999901')
        self.assertEqual(self.page.locator('[name="calendar_uploaded"]').input_value(), '1')
        self.fill()
        self.page.evaluate('window.responseStatus = 0')
        self.page.locator('[type="submit"]').click()
        self.page.wait_for_function('window.saved')
        self.assertIs(self.page.evaluate('window.calls[0].calendar_uploaded'), True)

    def test_pasting_the_post_link_marks_the_order_calendar_as_uploaded(self):
        line = self.page.locator('[data-calendar-plan]')
        line.get_by_role('button', name='标记为已上传').wait_for()
        self.page.locator('[name="post_id"]').fill('https://www.tiktok.com/@fixture/video/7698015486365273375')
        line.get_by_role('button', name='改回未上传').wait_for()
        self.assertIn('已上传', line.inner_text())
        calls = self.page.evaluate('window.calendarCalls')
        self.assertEqual(len(calls), 1)
        self.assertIs(calls[0]['calendar_uploaded'], True)
        self.assertEqual(self.page.locator('[name="published_on"]').input_value(), '2026-10-18')
        self.page.locator('[name="post_id"]').fill('7698015486365273376')
        self.assertEqual(len(self.page.evaluate('window.calendarCalls')), 1)

    def test_each_timeline_saves_its_planned_day_on_its_own(self):
        # The newest timeline keeps the order's entry; an older one has its own, so changing one day leaves the other.
        lines = "[{unique_id:'tl-new',name:'New'},{unique_id:'tl:old',name:'Old'}]"
        for uid, expected in (('tl-new', f'video:{JOB}'), ('tl:old', f'video:{JOB}_tl_tlold')):
            self.mount(f"window.publicationDetail = {{...window.publicationDetail,timelines:{lines},timeline_uid:'{uid}'}}")
            date = self.page.locator('[data-calendar-plan] input[type="date"]')
            date.fill('2026-11-12')
            self.page.wait_for_function('window.calendarCalls.length === 1')
            self.assertEqual(self.page.evaluate('window.calendarCalls[0]'), {'id': expected, 'calendar_date': '2026-11-12'})

    def test_older_versions_of_the_same_post_fold_to_one_line_and_other_videos_keep_a_receipt(self):
        self.mount("""const record = (id, post, day, at) => ({id, post_id:post, published_on:day, account:'fixture', script_version:1,
            registered_at:at, source:{take:{id:'TAK-1'},timeline:{name:'Reviewed'}}});
          window.publicationDetail.publication = {...record('VPU-333333333333','7698015486365273375','2026-10-18','2026-10-29T06:33:10'),
            calendar_uploaded:true, source_current:true};
          window.publicationDetail.history = [record('VPU-000000000000','7698015486365273999','2026-10-10','2026-10-10T01:00:00'),
            record('VPU-111111111111','7698015486365273375','2026-10-19','2026-10-19T05:27:48'),
            {...record('VPU-222222222222','7698015486365273375','2026-10-18','2026-10-23T01:05:51'), calendar_uploaded:true}];""")
        history = self.page.locator('.video-publication-history')
        self.assertEqual(history.locator('> summary').inner_text(), '其他视频 1 个 · 旧版登记 2 条')
        history.locator('> summary').click()
        lines = history.locator('.video-publication-superseded > summary').all_inner_texts()
        self.assertEqual(lines, ['2026-10-23 登记 · 发布日期 2026-10-18 · 已被 2026-10-29 的登记取代',
                                 '2026-10-19 登记 · 发布日期 2026-10-19 · 已被 2026-10-23 的登记取代'])
        self.assertEqual(history.locator(':scope > .video-publication-receipt').count(), 1)
        self.assertIn('7698015486365273999', history.locator(':scope > .video-publication-receipt').inner_text())
        self.assertIn('未记录上传', history.locator('.video-publication-superseded').nth(1).text_content())

    def test_receipt_shows_the_calendar_mark_and_toggles_it_in_place(self):
        self.page.add_script_tag(path=str(COPY_ID))
        self.page.evaluate("""async () => {
          window.copyText = (text, message) => { window.copiedId = [text, message]; };
          window.calls = [];window.saved = false;
          window.publicationDetail = {...window.publicationDetail,can_register:true,
            publication:{id:'VPU-111111111111',account:'fixture',published_on:'2026-10-22',post_id:'7999999999999999901',script_version:1,
              calendar_uploaded:false,source_current:true,source:{take:{id:'TAK-1'},timeline:{name:'Reviewed'}},registered_at:'2026-10-22'}};
          const api = async (url,options) => {
            if (!options) return structuredClone(window.publicationDetail);
            window.calls.push({url,body:structuredClone(options.body)});
            return {};
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:{id:'PRD-111111111111'},api,onSaved:async () => {window.saved = true;}});
        }""")
        self.assertEqual(self.page.locator('.video-publication-receipt [data-copy-id]').get_attribute('data-copy-id'), '7999999999999999901')
        self.page.locator('.video-publication-receipt [data-copy-id]').click()
        self.assertEqual(self.page.evaluate('window.copiedId'), ['7999999999999999901', '视频 ID已复制'])
        line = self.page.locator('.video-publication-receipt .video-publication-calendar')
        self.assertIn('未上传', line.inner_text())
        line.get_by_role('button', name='标记为已上传').click()
        self.page.wait_for_function('window.saved')
        call = self.page.evaluate('window.calls[0]')
        self.assertTrue(call['url'].endswith('/publication/calendar'))
        self.assertEqual(call['body'], {'publication_id': 'VPU-111111111111', 'calendar_uploaded': True})

    def test_a_detected_post_registers_in_one_click_and_another_can_be_refused(self):
        self.mount("""window.responseStatus = 0;window.publicationDetail.timeline_uid = 'timeline-1';
          window.publicationDetail.candidates = [
            {post_id:'7700962284323065118',account:'sharpaledgelab',title:'🔴Give Your Wrist a Guide#SHARPAL202H',published_on:'2026-10-26',
             source:'tk_lens',source_label:'TK 看板',account_known:true,also:['PRD-222222222222 · Lab261025-202-1']},
            {post_id:'7701324000684805406',account:'zackontheedge',title:'🔴Give Your Wrist a Guide#x',published_on:'2026-10-27',
             source:'shop_export',source_label:'Shop 导出',account_known:false,also:[]}];""")
        rows = self.page.locator('.video-publication-candidate')
        self.assertEqual(rows.count(), 2)
        self.assertIn('Give Your Wrist a Guide', rows.nth(0).inner_text())
        self.assertIn('也对得上另 1 条', rows.nth(0).inner_text())
        self.assertIn('账号待核对', rows.nth(1).inner_text())
        rows.nth(1).get_by_role('button', name='不是').click()
        self.page.wait_for_function('window.calls.length === 1')
        self.assertEqual(self.page.evaluate('window.calls[0]'), {'post_id': '7701324000684805406', 'timeline_uid': 'timeline-1'})
        self.page.locator('.video-publication-candidate').first.get_by_role('button', name='登记').click()
        self.page.wait_for_function('window.saved')
        body = self.page.evaluate('window.calls[1]')
        self.assertEqual((body['post_id'], body['account'], body['published_on'], body['calendar_uploaded']),
                         ('7700962284323065118', 'sharpaledgelab', '2026-10-26', True))
        self.assertEqual(body['post_url'], 'https://www.tiktok.com/@sharpaledgelab/video/7700962284323065118')

    def test_an_automatic_registration_is_marked_and_can_be_undone(self):
        self.page.evaluate("""async () => {
          window.toast = message => { window.toasted = message; };
          window.calls = [];window.saved = false;
          window.publicationDetail = {...window.publicationDetail,can_register:true,
            publication:{id:'VPU-111111111111',account:'sharpaledgelab',published_on:'2026-10-26',post_id:'7700962284323065118',script_version:1,
              calendar_uploaded:true,source_current:true,source:{take:{id:'TAK-1'},timeline:{name:'Reviewed'}},registered_at:'2026-10-26',
              auto:{source:'tk_lens',title:'🔴Give Your Wrist a Guide#SHARPAL202H'}}};
          const api = async (url,options) => {
            if (!options) return structuredClone(window.publicationDetail);
            window.calls.push({url,body:structuredClone(options.body)});
            return {warning:''};
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:{id:'PRD-111111111111'},api,onSaved:async () => {window.saved = true;}});
        }""")
        receipt = self.page.locator('.video-publication-receipt')
        self.assertIn('自动登记', receipt.inner_text())
        receipt.get_by_role('button', name='撤销').click()
        self.page.wait_for_function('window.saved')
        call = self.page.evaluate('window.calls[0]')
        self.assertTrue(call['url'].endswith('/publication/undo-auto'))
        self.assertEqual(call['body'], {'publication_id': 'VPU-111111111111'})
        self.assertIn('不会再自动登记', self.page.evaluate('window.toasted'))

    def test_definitive_conflict_can_refresh_version_before_new_submission(self):
        self.fill()
        self.page.evaluate('window.responseStatus = 409')
        self.page.locator('[type="submit"]').click()
        self.page.locator('[data-publication-error]:not([hidden])').wait_for()
        self.assertTrue(self.page.locator('[name="account"]').is_enabled())
        first = self.page.evaluate('window.calls[0]')
        self.page.evaluate("window.publicationDetail.revision = 'revision-two'")
        self.page.locator('[data-refresh-publication]').click()
        self.page.locator('[name="account"]').wait_for(state='attached')
        self.fill()
        self.page.evaluate('window.responseStatus = 0')
        self.page.locator('[type="submit"]').click()
        self.page.wait_for_function('window.saved')
        second = self.page.evaluate('window.calls[1]')
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertEqual(second['revision'], 'revision-two')


if __name__ == '__main__':
    unittest.main()
