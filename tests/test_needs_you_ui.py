"""The 需要你出手 strip hides when nothing is stuck, folds its rows, and links each to the order's publish step."""
from pathlib import Path
import unittest

from playwright.sync_api import sync_playwright

STATIC = Path(__file__).resolve().parents[1] / "static"


class NeedsYouBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 700})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.route("**/*", lambda route: route.fulfill(content_type="text/html", body='<main class="content"><section id="host" class="flow-needs"></section></main>',
                                                            headers={"Content-Security-Policy": "style-src-elem 'self' 'unsafe-inline'; style-src-attr 'none'"}))
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual([], self.errors))
        self.page.goto("http://127.0.0.1:43219/needs-test")
        for name in ("style.css", "buttons.css", "content-flow.css"):
            self.page.add_style_tag(path=str(STATIC / name))
        self.page.evaluate("() => {window.esc = v => String(v ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));}")
        self.page.add_script_tag(path=str(STATIC / "needs-you.js"))
        self.page.evaluate("""() => {
          window.data = {items:[
            {kind:'preview',kind_label:'预览导出失败',job_id:'PRD-1',job_title:'202H 开箱',timeline_uid:'d',timeline_name:'时间线 d',product:'202H',message:'达芬奇没开',since:'2026-11-06T10:00:00+00:00',hash:'#production/PRD-1?step=publish'},
            {kind:'notion',kind_label:'Notion 上传失败',job_id:'PRD-1',job_title:'202H 开箱',timeline_uid:'a',timeline_name:'时间线 a',product:'202H',message:'连接中断',since:'2026-11-06T09:00:00+00:00',hash:'#production/PRD-1?step=publish'}],
            counts:{preview:1,notion:1}};
          window.draw = () => NeedsYou.paint(document.querySelector('#host'),window.data,{url:(hash,product) => '#/' + product + '/' + hash.slice(1)});
          window.draw();
        }""")

    def test_summary_bar_counts_by_kind_and_rows_fold_open(self):
        bar = self.page.locator("[data-needs-toggle]")
        self.assertIn("需要你出手 2", bar.inner_text())
        self.assertIn("预览导出 1 · Notion 上传 1", bar.inner_text())
        self.assertEqual(self.page.locator(".needs-row").count(), 0)
        bar.click()
        self.assertEqual(self.page.locator(".needs-row").count(), 2)
        self.assertEqual(self.page.locator(".needs-row a").first.get_attribute("href"), "#/202H/production/PRD-1?step=publish")
        self.assertIn("达芬奇没开", self.page.locator(".needs-row").first.inner_text())
        bar.click()
        self.assertEqual(self.page.locator(".needs-row").count(), 0)

    def test_hides_itself_when_nothing_is_stuck(self):
        self.page.evaluate("() => {window.data = {items:[],counts:{}};window.draw();}")
        self.assertFalse(self.page.locator("#host").is_visible())

    def test_a_carousel_failure_opens_its_drawer_in_place(self):
        self.page.evaluate("""() => {
          window.opened = [];window.api = async () => ({});
          window.PhotoCarousel = {open:args => window.opened.push([args.job.id,args.uid,args.timelineName])};
          window.data.items.push({kind:'carousel',kind_label:'图文抽帧失败',job_id:'PRD-2',job_title:'202H 对比',timeline_uid:'p',timeline_name:'时间线 p',product:'202H',
            message:'视频读不出来',since:'2026-11-06T08:00:00+00:00',hash:'#production/PRD-2?step=cut&photo=p'});
          window.data.counts.carousel = 1;window.draw();
        }""")
        self.page.locator("[data-needs-toggle]").click()
        self.page.locator("[data-needs-photo]").click()
        self.assertEqual(self.page.evaluate("window.opened"), [["PRD-2", "p", "时间线 p"]])
        self.assertEqual(self.page.evaluate("location.hash"), "")
        # Other failures still go to their order page.
        self.assertIsNone(self.page.locator(".needs-row a").first.get_attribute("data-needs-photo"))


if __name__ == "__main__":
    unittest.main()
