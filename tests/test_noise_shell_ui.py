"""Exercise the real update notice against an isolated status response, never the live service."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / 'static/app.js').read_text(encoding='utf-8')
NOTICE = APP[APP.index('let staleNoticeDismissed = false;'):APP.index('setTimeout(checkServiceFresh, 3000);')]


class NoiseShellBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel='chrome', headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def open(self, width=1280):
        context = self.browser.new_context(viewport={'width': width, 'height': 720})
        self.addCleanup(context.close)
        page = context.new_page()
        page.route('http://noise.test/**', lambda r: r.fulfill(status=200, content_type='text/html', body='<main><header class="topbar"></header><section id="content">当前结果</section></main>'))
        page.goto('http://noise.test/')
        page.add_style_tag(path=str(ROOT / 'static/style.css'))
        self.install(page)
        return page

    def install(self, page, pid=100):
        page.add_script_tag(content=f"window.fixtureStatus={{pid:{pid},code_stale:true}}; window.fetch=async()=>({{ok:true,json:async()=>window.fixtureStatus}});window.api=async()=>{{throw Object.assign(new Error('任务仍在运行'),{{status:409}})}};")
        page.add_script_tag(content=NOTICE)
        page.evaluate('checkServiceFresh()')

    def test_mobile_notice_fits_and_does_not_cover_content(self):
        page = self.open(390)
        bounds = page.locator('#code-stale-banner').bounding_box()
        self.assertLessEqual(bounds['x'] + bounds['width'], 390)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertLessEqual(bounds['y'] + bounds['height'], page.locator('#content').bounding_box()['y'])
        page.get_by_role('button', name='重启工作台').click()
        page.get_by_text('暂时不能重启：任务仍在运行').wait_for()
        self.assertTrue(page.get_by_role('button', name='重启工作台').is_enabled())

    def test_later_survives_reload_for_same_process_but_not_a_new_process(self):
        page = self.open()
        page.get_by_role('button', name='稍后').click()
        self.assertEqual(page.locator('#code-stale-banner').count(), 0)
        page.evaluate('window.fixtureStatus.pid=102;checkServiceFresh()')
        self.assertEqual(page.locator('#code-stale-banner').count(), 1)
        page.get_by_role('button', name='稍后').click()
        page.reload()
        self.install(page)
        self.assertEqual(page.locator('#code-stale-banner').count(), 0)
        page.evaluate('window.fixtureStatus.pid=101;checkServiceFresh()')
        self.assertEqual(page.locator('#code-stale-banner').count(), 1)
