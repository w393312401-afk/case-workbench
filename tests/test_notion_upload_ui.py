"""Exercise the real publishing/Notion UI with a simulated local API."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class NotionUploadBrowserTests(unittest.TestCase):
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
        self.page.goto("http://127.0.0.1:43219/notion-test")
        self.page.add_script_tag(path=str(STATIC / "notion-upload.js"))
        self.page.add_script_tag(path=str(STATIC / "video-publication.js"))
        self.page.evaluate("""async () => {
          window.toast = () => {};
          window.calls = [];window.uploadFailure = false;window.holdUpload = false;
          window.uploads = {};
          window.job = {id:'PRD-111111111111',edits:[],timelines:[{unique_id:'one',name:'First'},{unique_id:'two',name:'Second'}]};
          window.api = async (path,options) => {
            if (path === '/api/notion/connection') return {status:window.connectorState || 'connected',authorization_url:window.connectorState === 'waiting' ? 'https://mcp.notion.com/authorize-fixture' : ''};
            if (path === '/api/notion/connect') {window.connectorState = 'waiting';return {status:'waiting'};}
            if (path.startsWith('/api/content/calendar')) return {calendar_date:'2026-11-03'};
            if (path.startsWith('/api/performance/videos')) return {videos:[]};
            if (path.includes('/notion')) {
              if (options) {
                window.calls.push(structuredClone(options.body));
                const body = options.body;
                window.uploads[body.timeline_uid] = {id:'NUP-111111111111',timeline_uid:body.timeline_uid,
                  status:window.holdUpload ? 'running' : window.uploadFailure ? 'failed' : 'completed',
                  progress:window.holdUpload ? '正在上传视频到 Notion。' : '',error:window.uploadFailure ? '测试上传失败' : '',
                  page_id:'40d4062a50ec81b8b4dceadc1c7acc8a',title:body.title};
                return {...window.uploads[body.timeline_uid],status:'running'};
              }
              const uid = new URL('http://local'+path).searchParams.get('timeline');
              return {timeline_uid:uid,name:uid==='one' ? 'First' : 'Second',configured:true,destination:'TK排期计划',
                content_id:'video:PRD-111111111111',file_path:'C:/Videos/finished.mp4',published_on:'2026-11-03',account:'fixture',
                copy:{en:'English speech.',zh:'中文文案。',titles:['🔴First title#tag','🔴Second title#tag'],screen_en:'LOCK THE ANGLE',screen_zh:'锁住角度'},
                receipt:window.uploads[uid] || null};
            }
            return {timeline_uid:path.includes('timeline=one') ? 'one' : 'two',timelines:[{unique_id:'two',name:'Second'},{unique_id:'one',name:'First'}],
              revision:'rev',source_signature:'sig',can_register:true,source:{},history:[]};
          };
          await VideoPublication.mount(document.querySelector('#publication'),{job:window.job,api:window.api});
        }""")
        self.page.locator('[data-notion-open]').wait_for()

    def open(self):
        self.page.locator('[data-notion-open]').click()
        return self.page.locator('dialog.notion-upload-dialog')

    def test_upload_is_before_registration_and_uses_selected_timeline_and_real_date(self):
        self.assertTrue(self.page.evaluate("document.querySelector('[data-notion-upload]').compareDocumentPosition(document.querySelector('[name=post_id]')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        self.page.locator('[name="post_id"]').fill('7999999999999999901')
        dialog = self.open()
        self.assertEqual('2026-11-03', dialog.locator('[name="published_on"]').input_value())
        self.assertIn('中文文案。', dialog.text_content())
        dialog.locator('[data-notion-title-choice]').select_option('1')
        dialog.locator('[data-notion-submit]').click()
        dialog.locator('[data-notion-progress]', has_text='已上传到 Notion。').wait_for()
        call = self.page.evaluate('window.calls[0]')
        self.assertEqual('two', call['timeline_uid'])
        self.assertEqual('🔴Second title#tag', call['title'])
        self.assertEqual('2026-11-03', call['published_on'])
        self.assertTrue(call['request_id'])
        self.assertEqual('7999999999999999901', self.page.locator('#publication [name="post_id"]').input_value())
        self.assertTrue(dialog.locator('[data-notion-result]').is_visible())

    def test_failed_upload_preserves_inputs_and_retries_the_same_request(self):
        self.page.evaluate('window.uploadFailure = true')
        dialog = self.open()
        dialog.locator('[data-notion-submit]').click()
        dialog.locator('[data-notion-error]', has_text='测试上传失败').wait_for()
        self.assertEqual('C:/Videos/finished.mp4', dialog.locator('[name="file_path"]').input_value())
        self.assertFalse(self.page.locator('[data-notion-status]').inner_text() == '已上传')
        self.page.evaluate('window.uploadFailure = false')
        dialog.locator('[data-notion-submit]').click()
        dialog.locator('[data-notion-progress]', has_text='已上传到 Notion。').wait_for()
        calls = self.page.evaluate('window.calls')
        self.assertEqual(calls[0]['request_id'], calls[1]['request_id'])

    def test_switching_timeline_has_its_own_upload_and_existing_link(self):
        self.page.evaluate("window.uploads.two = {status:'completed',page_id:'40d4062a50ec81b8b4dceadc1c7acc8a'}")
        self.page.locator('[data-publication-timeline="one"]').click()
        self.page.locator('[data-notion-upload="one"]').wait_for()
        dialog = self.open()
        self.assertIn('First →', dialog.text_content())
        dialog.locator('[data-notion-submit]').click()
        dialog.locator('[data-notion-progress]', has_text='已上传到 Notion。').wait_for()
        self.assertEqual('one', self.page.evaluate('window.calls[0].timeline_uid'))

    def test_closing_window_does_not_lose_running_upload(self):
        self.page.evaluate('window.holdUpload = true')
        dialog = self.open()
        dialog.locator('[data-notion-submit]').click()
        dialog.locator('[data-notion-progress]', has_text='正在上传视频到 Notion。').wait_for()
        dialog.locator('.dialog-footer [data-close]').click()
        self.page.evaluate("window.uploads.two.status = 'completed';window.uploads.two.progress = ''")
        self.page.locator('[data-notion-open]', has_text='更新到 Notion').wait_for()

    def test_first_connection_offers_authorization_before_enabling_upload(self):
        self.page.evaluate("window.connectorState = 'disconnected'")
        dialog = self.open()
        dialog.locator('[data-notion-connect]').wait_for(state='visible')
        self.assertTrue(dialog.locator('[data-notion-submit]').is_disabled())
        dialog.locator('[data-notion-connect]').click()
        dialog.locator('[data-notion-authorize]').wait_for(state='visible')
        self.assertEqual('https://mcp.notion.com/authorize-fixture', dialog.locator('[data-notion-authorize]').get_attribute('href'))
        self.page.evaluate("window.connectorState = 'connected'")
        dialog.locator('[data-connection-status]', has_text='Notion 已连接').wait_for()
        self.assertTrue(dialog.locator('[data-notion-submit]').is_enabled())


if __name__ == "__main__":
    unittest.main()
