"""Guest-only browser behavior with real guards and disposable fixture data."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://workbench.test"
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'none'; media-src 'none'; object-src 'none'; base-uri 'none'; form-action 'self'"
HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<script src="/guest-mode.js"></script><script src="/public-mode.js"></script>
<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/shell.css"><link rel="stylesheet" href="/public-mode.css"><link rel="stylesheet" href="/guest-mode.css">
<script src="/fixture.js" defer></script></head><body><main><header class="topbar"><div class="topbar-right">
<button id="add-top">收录案例</button><button id="task-activity-open">任务</button><button id="files-nav">设置</button>
<button id="global-search-open">全局搜索</button></div></header><section id="content">
<h1 id="case-title"></h1><form id="case-search-form" role="search"><input id="case-search" type="search" aria-label="筛选案例"><button id="filter-reset" type="button">重置筛选</button></form>
<button data-detail-tab="script" id="detail-tab">查看文案</button><button data-status-filter="all" id="status-filter">全部进度</button>
<a id="read-case" class="case-card" href="#/202H/case/PVR-000000000001">查看案例正文</a>
<a id="read-script" class="script-card" href="#/202H/content/SCR-000000000001">查看采用稿</a>
<a id="production-link" href="#/202H/production/CAR-000000000001">制作单</a>
<button id="script-copy">复制正文</button><button data-native-copy="x" id="native-copy">原句</button><button data-export="x" id="export">导出</button>
<button aria-label="下载资料" id="icon-download">↓</button><a download="data.txt" href="/download.txt" id="download-link">资料</a>
<button data-generate="x" id="generate">生成</button><button data-run="x" id="run">继续执行</button>
<form id="edit-form"><input id="edit-value"><button type="submit" id="save">保存</button></form>
<button id="ordinary-title">查看进度记录</button><p id="case-body"></p><div id="dynamic"></div>
</section></main></body></html>"""
FIXTURE_JS = """
window.__actions = 0; window.__reads = 0;
document.querySelectorAll('#script-copy,#native-copy,#export,#icon-download,#generate,#run').forEach(b=>b.addEventListener('click',()=>window.__actions++));
document.querySelector('#edit-form').addEventListener('submit',e=>{e.preventDefault();window.__actions++;});
document.querySelector('#detail-tab').addEventListener('click',()=>window.__reads++);
document.querySelector('#filter-reset').addEventListener('click',()=>{document.querySelector('#case-search').value='';window.__reads++;});
fetch('/api/state').then(r=>r.json()).then(data=>document.querySelector('#case-title').textContent=data.title);
function readRoute(){if(location.hash.includes('/case/'))fetch('/api/cases/PVR-000000000001').then(r=>r.json()).then(data=>document.querySelector('#case-body').textContent=data.text);}
window.addEventListener('hashchange',readRoute);readRoute();
"""


class GuestModeUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.requests = []
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.page.add_init_script("window.__violations=[];document.addEventListener('securitypolicyviolation',e=>window.__violations.push(e.violatedDirective));")
        self.page.route("**/*", self.route)

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        self.requests.append((request.method, path, request.headers))
        headers = {"Content-Security-Policy": CSP}
        if path == "/":
            route.fulfill(status=200, content_type="text/html", headers=headers, body=HTML)
        elif path == "/fixture.js":
            route.fulfill(status=200, content_type="text/javascript", body=FIXTURE_JS)
        elif path in ("/public-mode.js", "/public-mode.css"):
            route.fulfill(status=200, content_type="text/javascript" if path.endswith(".js") else "text/css", body=(ROOT / "deployment/text-only" / path[1:]).read_text(encoding="utf-8"))
        elif path.endswith((".js", ".css")):
            route.fulfill(status=200, content_type="text/javascript" if path.endswith(".js") else "text/css", body=(ROOT / "static" / path[1:]).read_text(encoding="utf-8"))
        elif path == "/guest/api/public-version":
            route.fulfill(status=200, json={"version": "fixture-1"})
        elif path.startswith("/guest/api/"):
            route.fulfill(status=200, json={"title": "已发布案例资料", "text": "原始案例的只读正文"})
        elif path.startswith("/api/"):
            route.fulfill(status=500, json={"error": "Private API must never be requested"})
        else:
            route.fulfill(status=404, json={"error": "not found"})

    def open(self, suffix=""):
        self.page.goto(ORIGIN + "/" + suffix)
        self.page.locator("#case-title").get_by_text("已发布案例资料").wait_for()

    def test_read_fetches_are_namespaced_and_request_options_survive(self):
        self.open()
        self.assertTrue(self.page.evaluate("WorkbenchGuest && Boolean(PublicTextMode)"))
        self.assertEqual(self.page.evaluate("fetch(new Request('/api/cases/PVR-000000000001',{headers:{'X-Fixture':'kept'}})).then(r=>r.status)"), 200)
        self.assertEqual(self.page.evaluate("fetch('/guest/api/state').then(r=>r.status)"), 200)
        self.assertFalse(any(path.startswith("/api/") for _, path, _ in self.requests))
        self.assertFalse(any(path.startswith("/guest/guest/") for _, path, _ in self.requests))
        requested = [row for row in self.requests if row[1] == "/guest/api/cases/PVR-000000000001"]
        self.assertEqual(requested[-1][2].get("x-fixture"), "kept")

    def test_writes_never_reach_network_and_action_controls_are_inert(self):
        self.open()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.page.evaluate("method=>fetch('/api/upload',{method,body:'x'}).then(r=>r.status)", method), 403)
        self.assertEqual(self.page.evaluate("fetch(new Request('/api/creation',{method:'POST',body:'x'})).then(r=>r.status)"), 403)
        for selector in ("#add-top", "#task-activity-open", "#files-nav", "#script-copy", "#native-copy", "#export", "#icon-download", "#download-link", "#generate", "#run", "#production-link"):
            self.assertTrue(self.page.locator(selector).is_hidden(), selector)
        self.page.evaluate("document.querySelectorAll('#script-copy,#native-copy,#export,#icon-download,#generate,#run').forEach(b=>b.click());document.querySelector('#edit-form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));")
        self.assertEqual(self.page.evaluate("window.__actions"), 0)
        self.assertFalse(any(method not in ("GET", "HEAD") for method, _, _ in self.requests))

    def test_filters_detail_navigation_and_existing_text_remain_available(self):
        self.open()
        self.page.locator("#case-search").fill("案例")
        self.page.locator("#filter-reset").click()
        self.assertEqual(self.page.locator("#case-search").input_value(), "")
        self.page.locator("#detail-tab").click()
        self.assertEqual(self.page.evaluate("window.__reads"), 2)
        self.assertTrue(self.page.locator("#status-filter").is_visible())
        self.assertTrue(self.page.locator("#ordinary-title").is_visible())
        self.page.locator("#read-case").click()
        self.page.wait_for_url(ORIGIN + "/#/202H/case/PVR-000000000001")
        self.page.locator("#case-body").get_by_text("原始案例的只读正文").wait_for()
        self.assertTrue(self.page.locator("#read-script").is_visible())
        self.page.locator("#read-script").evaluate("e=>e.textContent='新增广告中的购买理由'")
        self.page.wait_for_function("!document.querySelector('#read-script').hasAttribute('data-public-hidden') && !document.querySelector('#read-script').hasAttribute('data-guest-hidden')")
        self.page.locator("#read-script").click()
        self.page.wait_for_url(ORIGIN + "/#/202H/content/SCR-000000000001")

    def test_production_settings_and_content_routes_are_readonly_destinations(self):
        for supplied, expected in (("#/202H/production/CAR-000000000001", "#/202H/ai"),
                                   ("#production?product=202H", "#ai?product=202H"),
                                   ("#/202H/settings", "#/202H/library"), ("#content", "#ai"),
                                   ("#/202H/scripts", "#/202H/ai"),
                                   ("#/202H/content/SCR-000000000001", "#/202H/content/SCR-000000000001"),
                                   ("#content/SCR-000000000001?product=202H", "#content/SCR-000000000001?product=202H")):
            with self.subTest(hash=supplied):
                self.open(supplied)
                self.assertEqual(urlsplit(self.page.url).fragment, expected[1:])

    def test_new_action_controls_are_hidden_and_guest_banner_works_under_csp(self):
        self.open()
        self.page.evaluate("document.querySelector('#dynamic').innerHTML='<button data-copy-summary>复制小结</button><button data-generate-preview>生成预览</button><button data-detail-tab=history>历史</button>'")
        self.page.wait_for_function("document.querySelector('[data-copy-summary]').dataset.guestHidden==='true'")
        self.assertTrue(self.page.locator("[data-copy-summary]").is_hidden())
        self.assertTrue(self.page.locator("[data-generate-preview]").is_hidden())
        self.assertTrue(self.page.locator("[data-detail-tab=history]").is_visible())
        self.assertIn("访客浏览 · 仅供查看", self.page.locator("#guest-banner").inner_text())
        self.assertEqual(self.page.title(), "TK工作台 · 访客浏览")
        self.assertEqual(self.page.locator("#guest-banner").evaluate("e=>getComputedStyle(e).display"), "flex")
        self.assertTrue(self.page.locator("#guest-login-link").get_attribute("href").startswith("/auth/login"))
        self.assertEqual(self.page.evaluate("window.__violations"), [])
        self.page.screenshot(path=str(Path(tempfile.gettempdir()) / "case-workbench-guest-mobile.png"))

    def test_adopted_copy_and_edit_controls_are_hidden_while_history_expands(self):
        self.open()
        self.page.evaluate("""document.querySelector('#dynamic').innerHTML = `<section class="adopted-script">
          <button data-adopted-copy>复制正文</button><button data-adopted-edit>编辑采用稿</button>
          <details data-script-versions><summary>版本历史（1）</summary><pre>Saved adopted words</pre></details></section>`;""")
        self.page.wait_for_function("document.querySelector('[data-adopted-copy]').dataset.guestHidden === 'true'")
        self.assertTrue(self.page.locator("[data-adopted-copy]").is_hidden())
        self.assertTrue(self.page.locator("[data-adopted-edit]").is_hidden())
        self.page.locator("[data-script-versions] > summary").click()
        self.assertTrue(self.page.get_by_text("Saved adopted words", exact=True).is_visible())

    def test_login_link_leaves_forced_guest_view(self):
        self.open("?guest=1#/202H/library")
        self.assertEqual(self.page.locator("#guest-login-link").get_attribute("href"), "/auth/login?next=%2F%23%2F202H%2Flibrary")

    def test_guest_media_is_allowed_but_private_media_and_nonmedia_attachments_stay_blocked(self):
        self.open()
        markup = self.page.evaluate("""PublicTextMode.textOnlyMarkup('<img src="/guest-media/PVR-000000000001/screenshot"><video controls src="/guest-media/PVR-000000000001/video"></video><audio controls></audio><iframe src="/private"></iframe><object data="/private"></object>')""")
        for tag in ("<img", "<video", "<audio"):
            self.assertIn(tag, markup)
        self.assertNotIn("<iframe", markup)
        self.assertNotIn("<object", markup)
        self.assertEqual(self.page.evaluate("fetch('/media/PVR-000000000001/video').then(r=>r.status)"), 403)
        self.assertEqual(self.page.evaluate("fetch('https://outside.test/video').then(r=>r.status)"), 403)
        self.page.evaluate("""document.querySelector('#dynamic').innerHTML = `
          <a class="history-asset" id="history-video" href="/guest-media/PVR-000000000001/video?event=fixture">当时的视频</a>
          <a class="history-asset" id="history-document" href="/guest-media/PVR-000000000001/transcript?event=fixture">当时的文案来源文件</a>
          <button class="cue-time">0:02</button><button data-native-seek="2">播放此段</button>
          <button class="player-loop">循环播放</button>`;""")
        self.page.locator("#history-video[data-guest-media-link]").wait_for()
        self.assertTrue(self.page.locator("#history-document").is_hidden())
        for selector in (".cue-time", "[data-native-seek]", ".player-loop"):
            self.assertTrue(self.page.locator(selector).is_visible(), selector)
            self.assertTrue(self.page.locator(selector).is_enabled(), selector)

    def test_overview_write_actions_are_hidden_but_reading_and_playback_are_kept(self):
        self.open()
        self.page.evaluate("""document.querySelector('#dynamic').innerHTML = `
          <div id="home-page"><div class="data-warning">待处理内容暂时无法读取。<button data-home-retry>重新读取</button></div></div>
          <button data-home-review>复盘</button><button data-home-batch>一起复盘</button>
          <button data-followup-chain>复盘</button><button data-followup-show-waiting>一起看</button>
          <select data-followup-filter><option>全部</option></select><input data-followup-search type="search">
          <button data-followup-next>下一页</button><a id="video-detail" href="#videos?tab=list">视频明细</a>
          <details class="detail-menu"><summary aria-label="更多操作">更多操作</summary><button>归档案例</button></details>
          <button aria-label="更多操作">⋯</button><button class="player-loop">循环播放</button>
          <section class="ai-case-actions"><div class="ai-case-heading">AI 协作</div><details class="ai-case-history" hidden></details></section>
          <details class="detail-product"><summary>202H</summary><div class="detail-product-panel"><select data-product-select></select></div></details>`;""")
        self.page.wait_for_function("document.querySelector('[data-followup-chain]').dataset.guestHidden==='true'")
        for selector in ("#home-page > .data-warning", "[data-home-review]", "[data-home-batch]", "[data-followup-chain]", "[data-followup-show-waiting]", ".detail-menu", "button[aria-label='更多操作']", ".ai-case-actions", ".detail-product-panel"):
            self.assertTrue(self.page.locator(selector).is_hidden(), selector)
        for selector in ("[data-followup-filter]", "[data-followup-search]", "[data-followup-next]", "#video-detail", ".detail-product > summary", ".player-loop"):
            self.assertTrue(self.page.locator(selector).is_visible(), selector)
        self.page.locator(".detail-product > summary").evaluate("node => node.click()")
        self.assertFalse(self.page.locator(".detail-product").evaluate("node=>node.open"))
        self.assertEqual(self.page.locator(".detail-product > summary").get_attribute("aria-disabled"), "true")


if __name__ == "__main__":
    unittest.main()
