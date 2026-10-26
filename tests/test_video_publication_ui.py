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
          window.responseStatus = 503;
          window.saved = false;
          window.publicationDetail = {revision:'revision-one',source_signature:'source-one',can_register:true,history:[],
            source:{script:{script_version:1},take:{id:'TAK-111111111111'},timeline:{name:'Reviewed'}}};
          window.salesVideos = [];
          new Function(setup)();
          const api = async (url,options) => {
            if (!options && url.startsWith('/api/performance/videos')) return {videos:structuredClone(window.salesVideos)};
            if (!options) return structuredClone(window.publicationDetail);
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

    def mount_carousel_runs(self, runs):
        self.page.evaluate("""async runs => {
          const api = async url => {
            if (url.endsWith('/video-carousel')) return {runs,source:{available:false,timelines:[]},running:false};
            if (url === '/api/ai/options') return {models:[]};
            return structuredClone(window.publicationDetail);
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:{id:'PRD-111111111111'},api});
        }""", runs)
        self.page.locator('.video-carousel h3').wait_for()

    def test_video_saved_original_remains_accessible_after_its_order_is_missing(self):
        self.mount_carousel_runs([{
            "id": "TBL-0123456789ab", "script_id": "SCR-0123456789ab", "product": "202H",
            "status": "needs_attention", "has_result": True, "slide_count": 6,
            "carousel_id": "", "carousel_missing": True, "finished_at": "2026-10-21T21:07:32+08:00"}])
        box = self.page.locator('.video-carousel')
        self.assertIn('关联的图文制作单已不存在', box.inner_text())
        self.assertIn('6 张', box.inner_text())
        link = box.get_by_role('link', name='查看原逐张表', exact=True)
        self.assertEqual(link.get_attribute('href'), '#content/SCR-0123456789ab?table=TBL-0123456789ab')
        self.assertEqual(box.locator('a[href*="#production/"]').count(), 0)

    def test_empty_latest_failure_does_not_hide_an_older_saved_original(self):
        self.mount_carousel_runs([
            {"id": "TBL-abcdef012345", "status": "failed", "has_result": False, "carousel_id": "",
             "error": "没有找到来源时间线。", "finished_at": "2026-10-22T16:13:44+08:00"},
            {"id": "TBL-0123456789ab", "script_id": "SCR-0123456789ab", "product": "202H",
             "status": "needs_attention", "has_result": True, "slide_count": 6,
             "carousel_id": "", "carousel_missing": True, "finished_at": "2026-10-21T21:07:32+08:00"}])
        links = self.page.locator('.video-carousel a')
        self.assertEqual(links.count(), 1)
        self.assertIn('原逐张表 · 6 张', links.inner_text())
        self.assertIn('TBL-0123456789ab', links.get_attribute('href'))
        self.assertNotIn('TBL-abcdef012345', links.get_attribute('href'))

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

    def test_calendar_choice_defaults_to_not_uploaded_and_is_sent_as_a_boolean(self):
        self.assertEqual(self.page.locator('[name="calendar_uploaded"]').input_value(), '0')
        self.fill()
        self.page.evaluate('window.responseStatus = 0')
        self.page.locator('[type="submit"]').click()
        self.page.wait_for_function('window.saved')
        self.assertIs(self.page.evaluate('window.calls[0].calendar_uploaded'), False)

        # The real page remounts after a save; do the same for a fresh form.
        self.page.reload()
        self.mount()
        self.page.evaluate('window.responseStatus = 0')
        self.fill()
        self.page.locator('[name="calendar_uploaded"]').evaluate("node => { node.value = '1'; }")
        self.page.locator('[type="submit"]').click()
        self.page.wait_for_function('window.saved')
        self.assertIs(self.page.evaluate('window.calls[0].calendar_uploaded'), True)

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
