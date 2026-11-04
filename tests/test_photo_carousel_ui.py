"""The photo window imports the selected timeline, reports progress and permits a retry after failure."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class PhotoCarouselBrowserTests(unittest.TestCase):
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
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main id="publication"></main>'))
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/photo-test")
        for name in ("style.css", "buttons.css", "production.css", "photo-carousel.css"):
            self.page.add_style_tag(path=str(STATIC / name))
        self.page.add_script_tag(path=str(STATIC / "photo-carousel.js"))
        self.page.evaluate("""async () => {
          window.calls = [];window.detail = {exists:false};window.toast = () => {};
          window.job = {id:'PRD-111111111111',timelines:[{unique_id:'one',name:'First'},{unique_id:'two',name:'Second'}]};
          window.api = async (path,options) => {
            if (options) {
              window.calls.push({path,body:structuredClone(options.body)});
              window.detail.resolve_import = {status:'running',message:'正在导出无字版…',percent:12};
            }
            if (path.endsWith('/frames')) return {items:[]};
            return structuredClone(window.detail);
          };
          const root = document.querySelector('#publication');
          root.innerHTML = PhotoCarousel.markup('two');
          await PhotoCarousel.bind(root,{job:window.job,api:window.api,uid:'two'});
        }""")

    def open(self):
        self.page.locator('[data-photo-open]').click()
        return self.page.locator('dialog.photo-carousel-dialog')

    def ready(self):
        self.page.evaluate("""() => {window.detail = {exists:true,id:'PHS-111111111111',video:{name:'Second-无字版.mp4',duration:3},
          frames_job:{status:'done',count:15},works:[],versions:[],ai:{status:'idle'},
          resolve_import:{status:'done',message:'无字版已导入，字幕和屏显轨已恢复。'}};}""")

    def test_import_uses_selected_timeline_and_polls_through_automatic_frames(self):
        dialog = self.open()
        button = dialog.get_by_role('button', name='从达芬奇导入无字版', exact=True)
        button.click()
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_disabled())
        self.assertEqual(dialog.locator('[data-photo-resolve]').get_attribute('aria-busy'), 'true')
        self.assertIn('12%', dialog.locator('[data-photo-resolve-progress]').inner_text())
        self.assertEqual(dialog.locator('[data-photo-video]').count(), 0)
        self.assertEqual(self.page.evaluate('window.calls'), [{
            'path':'/api/production/jobs/PRD-111111111111/photo-set/from-resolve', 'body':{'timeline_uid':'two'}}])
        self.ready()
        dialog.locator('[data-photo-resolve-progress]', has_text='字幕和屏显轨已恢复').wait_for()
        self.assertIn('Second-无字版.mp4', dialog.inner_text())
        self.assertTrue(dialog.locator('[data-photo-plan]').is_visible())
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_enabled())
        self.assertEqual(len(self.page.evaluate('window.calls')), 1)

    def test_closing_and_reopening_preserves_running_import(self):
        dialog = self.open()
        dialog.locator('[data-photo-resolve]').click()
        dialog.locator('[data-photo-resolve-progress]').wait_for()
        dialog.locator('[data-close]').click()
        self.assertEqual(self.page.locator('dialog.photo-carousel-dialog').count(), 0)
        dialog = self.open()
        dialog.locator('[data-photo-resolve-progress]').wait_for()
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_disabled())
        self.assertEqual(len(self.page.evaluate('window.calls')), 1)
        self.ready()
        dialog.locator('[data-photo-resolve-progress]', has_text='字幕和屏显轨已恢复').wait_for()

    def test_failure_keeps_previous_video_and_retry_available(self):
        self.ready()
        dialog = self.open()
        dialog.locator('[data-photo-resolve]').click()
        self.page.evaluate("window.detail.resolve_import = {status:'failed',error:'素材离线，请重新链接后重试。'}")
        dialog.locator('[data-photo-resolve-error]', has_text='素材离线').wait_for()
        self.assertIn('Second-无字版.mp4', dialog.inner_text())
        self.assertTrue(dialog.locator('[data-photo-resolve]').is_enabled())
        dialog.locator('[data-photo-resolve]').click()
        self.assertEqual(len(self.page.evaluate('window.calls')), 2)

    def test_button_and_progress_fit_light_dark_and_narrow_windows(self):
        dialog = self.open()
        for scheme, width in (('light', 1280), ('dark', 1280), ('dark', 390)):
            self.page.emulate_media(color_scheme=scheme)
            self.page.set_viewport_size({'width':width,'height':850})
            button = dialog.locator('[data-photo-resolve]')
            self.assertTrue(button.is_visible())
            self.assertTrue(button.evaluate('(el) => el.getBoundingClientRect().right <= innerWidth'))
            self.assertTrue(dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth'))


if __name__ == '__main__':
    unittest.main()
