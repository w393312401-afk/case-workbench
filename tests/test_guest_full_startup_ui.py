"""Full exported-page startup: actual index, every deployed script, real DOM.

The fixture server reads only its temporary case library. Mutation callbacks are
bounded in the regression harness so replaying the old bug cannot hang Chrome.
"""
from __future__ import annotations

import importlib.util
import base64
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
import test_server as fixture


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://workbench.test"
spec = importlib.util.spec_from_file_location("guest_startup_snapshot", ROOT / "deployment/guest_snapshot.py")
guest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guest)


class GuestFullStartupTests(unittest.TestCase):
    bundled_assets = False
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        # Generate a tiny disposable clip locally. Playback uses Chrome's real
        # decoder and the namespaced fixture endpoint, without external media.
        generator = cls.browser.new_page()
        recorded = generator.evaluate("""() => {
          const canvas = document.createElement('canvas');canvas.width=64;canvas.height=64;
          const context = canvas.getContext('2d');context.fillStyle='#3475e9';context.fillRect(0,0,64,64);
          return canvas.toDataURL('image/png').split(',')[1];
        }""")
        generator.close()
        cls.image_bytes = base64.b64decode(recorded)
        cls.media_bytes = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
            "-i", "color=c=blue:s=64x64:d=1", "-c:v", "libvpx", "-f", "webm", "pipe:1"],
            check=True, capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.legacy_hide = False
        self.bundle_site = None
        if self.bundled_assets:
            directory = tempfile.TemporaryDirectory(prefix="guest-full-bundle-")
            self.addCleanup(directory.cleanup)
            self.bundle_site = Path(directory.name)
            for source in (ROOT / "static").iterdir():
                if source.suffix in {".js", ".css"}:
                    shutil.copy2(source, self.bundle_site / source.name)
            for name in ("public-mode.js", "public-mode.css"):
                shutil.copy2(ROOT / "deployment/text-only" / name, self.bundle_site / name)
        self.context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self.scripts = [
            {"id": "SCR-000000000001", "family_id": "SCR-000000000001", "version": 1,
             "title": "新增广告中的购买理由", "product": "202H", "text": "Original fixture words",
             "status": "adopted", "archived": True, "created_at": "2026-10-15T08:00:00Z"},
            {"id": "SCR-000000000002", "family_id": "SCR-000000000001", "parent_id": "SCR-000000000001", "version": 2,
             "title": "新增广告中的购买理由", "product": "202H", "text": "Published fixture script",
             "status": "adopted", "created_at": "2026-10-16T08:00:00Z"},
        ]
        self.requests = []
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(self.errors, []))
        self.page.add_init_script("""
          const OriginalObserver = window.MutationObserver;
          window.__mutationCallbacks = 0;
          window.__mutationFlood = false;
          window.MutationObserver = class extends OriginalObserver {
            constructor(callback) { super((records, observer) => {
              if (++window.__mutationCallbacks > 500) {
                window.__mutationFlood = true; observer.disconnect(); return;
              }
              callback(records, observer);
            }); }
          };
        """)
        self.page.route("**/*", self.route)

    def route(self, route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path
        self.requests.append((request.method, path))
        if path == "/":
            page = (ROOT / "static/index.html").read_text(encoding="utf-8")
            # Apply the same text-export transforms before the actual guest-page
            # function namespaces its assets. No workbench module is mocked.
            page = page.replace('<link rel="stylesheet" href="/activity.css">', "").replace('<script src="/activity.js" defer></script>', "")
            page = page.replace('<link rel="stylesheet" href="/content-flow.css"><script src="/content-flow.js" defer></script>', "")
            page = page.replace('id="ai-nav" data-page="content"', 'id="ai-nav" data-page="ai"')
            page = page.replace('<link rel="icon" type="image/svg+xml" href="/favicon.svg">', "")
            page = page.replace("<head>", '<head><script src="/public-mode.js"></script>', 1)
            page = page.replace("</head>", '<link rel="stylesheet" href="/public-mode.css"></head>', 1)
            page = guest.guest_page(page)
            if self.bundle_site is not None:
                if self.legacy_hide:
                    source = ROOT / "deployment/text-only/public-mode.js"
                    self.bundle_site.joinpath(source.name).write_text(source.read_text(encoding="utf-8").replace(
                        'if (node.dataset.publicHidden !== "true") node.dataset.publicHidden = "true";',
                        'node.dataset.publicHidden = "true";'), encoding="utf-8")
                page = guest.guest_asset_references(guest.ASSETS.bundle_guest_assets(self.bundle_site, page))
            route.fulfill(status=200, content_type="text/html", body=page,
                          headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self'; img-src 'self'; media-src 'self'; object-src 'none'"})
        elif path.startswith("/guest-assets/"):
            name = path.rsplit("/", 1)[-1]
            source = self.bundle_site / name if self.bundle_site is not None else ROOT / ("deployment/text-only" if name in {"public-mode.js", "public-mode.css"} else "static") / name
            content = source.read_text(encoding="utf-8")
            if name == "public-mode.js" and self.legacy_hide:
                content = content.replace('if (node.dataset.publicHidden !== "true") node.dataset.publicHidden = "true";', 'node.dataset.publicHidden = "true";')
            route.fulfill(status=200, content_type="text/javascript" if name.endswith(".js") else "text/css", body=content)
        elif path == "/guest/api/public-version":
            route.fulfill(status=200, json={"version": "fixture-1"})
        elif path == "/guest/api/content":
            route.fulfill(status=404, json={"error": "Unavailable in guest view"})
        elif path == "/guest/api/creation":
            route.fulfill(status=200, json={"scripts": self.scripts, "revision": "fixture", "materials": [], "plans": [], "theme_links": []})
        elif path.startswith("/guest/api/scripts/"):
            script = next((item for item in self.scripts if item["id"] == path.rsplit("/", 1)[-1]), None)
            route.fulfill(status=200 if script else 404, json={"script": script, "revision": "fixture"})
        elif path.startswith("/guest/api/"):
            status, _, data = self.request("GET", path[len("/guest"):] + ("?" + parsed.query if parsed.query else ""))
            if path == "/guest/api/state" and status == 200:
                data.update(token="", paths={}, public_mode=True, guest_mode=True)
            if path == "/guest/api/cases/" + self.identifier and status == 200:
                data["assets"]["screenshot"] = {"exists": True, "name": "fixture.png"}
                data["assets"]["contact_sheet"] = {"exists": True, "name": "fixture.png"}
            route.fulfill(status=status, json=data)
        elif path.startswith("/guest-media/" + self.identifier + "/"):
            if path.endswith("/video"):
                start = int(request.headers.get("range", "bytes=0-").split("=")[-1].split("-")[0] or 0)
                body = self.media_bytes[start:]
                route.fulfill(status=206, content_type="video/webm", body=body,
                              headers={"Accept-Ranges": "bytes", "Content-Range": f"bytes {start}-{len(self.media_bytes)-1}/{len(self.media_bytes)}"})
            elif path.rsplit("/", 1)[-1] in {"thumbnail", "screenshot", "contact_sheet"}:
                route.fulfill(status=200, content_type="image/png", body=self.image_bytes)
            else:
                route.fulfill(status=404, json={"error": "Media kind unavailable"})
        else:
            route.fulfill(status=404, json={"error": "not found"})

    def test_product_name_starting_with_action_stays_selectable_with_both_read_only_guards(self):
        name = "新增款便携磨刀器"
        status, _, data = self.request("POST", "/api/products/catalog", {"action": "add", "product": name})
        self.assertEqual(status, 200, data)
        self.page.goto(ORIGIN + "/#/202H/library", wait_until="load")
        self.page.locator(".case-card").first.wait_for()
        self.assertTrue(self.page.evaluate("window.WorkbenchGuest === true && !!window.PublicTextMode"))
        trigger = self.page.locator(".product-combo-trigger")
        trigger.click()
        panel = self.page.locator(".product-combo-panel")
        panel.locator('[data-product="' + name + '"]').click()
        self.page.wait_for_function("name => document.querySelector('.product-combo-trigger span').textContent === name", arg=name)
        # The real public guard also runs inside guest mode. Its action-label
        # heuristic must not hide a browse control carrying the product name.
        self.page.evaluate("PublicTextMode.scan()")
        self.assertTrue(trigger.is_visible())
        self.assertTrue(trigger.is_enabled())
        self.assertIsNone(trigger.get_attribute("data-public-hidden"))
        self.assertIsNone(trigger.get_attribute("data-guest-hidden"))
        trigger.click()
        self.assertEqual(panel.locator("[data-combo-manage]:visible").count(), 0)
        panel.locator('[data-product="202H"]').click()
        self.page.wait_for_function("() => WorkbenchShell.product() === '202H'")
        self.assertTrue(trigger.is_visible())
        self.assertTrue(trigger.is_enabled())
        self.assertEqual(self.page.locator("#product-manager-dialog").count(), 0)

    def test_full_guest_page_reaches_load_renders_and_navigates_without_observer_flood(self):
        self.page.goto(ORIGIN + "/#/202H/library", wait_until="load")
        self.page.locator(".case-card").first.wait_for()
        thumbnail = self.page.locator("[data-card-thumbnail]").first
        self.assertTrue(thumbnail.is_visible())
        self.assertTrue(thumbnail.evaluate("async img=>{await img.decode();return img.naturalWidth>0;}"))
        self.assertIn("/guest-media/", thumbnail.get_attribute("src"))
        self.assertEqual(self.page.title(), "TK工作台 · 访客浏览")
        self.assertFalse(self.page.evaluate("window.__mutationFlood"))
        self.page.locator(".case-card").first.click()
        self.page.wait_for_url("**/case/" + self.identifier)
        self.page.locator(".detail-heading").wait_for()
        self.assertTrue(self.page.locator(".detail-menu").is_hidden())
        self.assertTrue(self.page.locator(".detail-product > summary").is_visible())
        self.page.locator(".detail-product > summary").evaluate("node=>node.click()")
        self.assertFalse(self.page.locator(".detail-product").evaluate("node=>node.open"))
        self.assertTrue(self.page.locator(".ai-case-actions").is_hidden())
        for tab in ("script", "data", "history"):
            self.assertTrue(self.page.locator("#detail-tab-" + tab).is_visible())
        self.page.locator("#detail-tab-script").click()
        self.page.locator(".player-loop").first.wait_for(state="visible")
        for button in self.page.locator(".player-loop").all():
            self.assertTrue(button.is_visible())
        player = self.page.locator("#case-player")
        self.assertTrue(player.is_visible())
        self.assertIn("/guest-media/", player.get_attribute("src"))
        self.assertTrue(player.evaluate("""async video=>{
          video.muted=true;video.loop=true;await video.play();
          await new Promise(resolve=>setTimeout(resolve,180));
          const played=video.currentTime>0 && !video.paused && video.videoWidth===64;
          video.pause();return played;
        }"""))
        self.assertTrue(player.evaluate("video=>video.controlsList.contains('nodownload')"))
        self.page.locator(".guest-media-gallery summary").click()
        for image in self.page.locator(".guest-media-gallery img").all():
            self.assertTrue(image.evaluate("async img=>{await img.decode();return img.naturalWidth>0;}"))
        self.assertEqual(self.page.locator(".guest-media-gallery img").count(), 2)
        self.page.evaluate("location.hash='#/202H/home'")
        self.page.locator("[data-home-tab='due'][aria-selected='true']").wait_for()
        self.page.locator("#home-page > .data-warning[data-guest-hidden='true']").wait_for(state="attached")
        self.assertTrue(self.page.locator("[data-home-tab='tasks']").is_hidden())
        self.assertTrue(self.page.locator("#home-tasks-title-panel").is_hidden())
        self.assertTrue(self.page.locator("#home-page > .data-warning").is_hidden())
        self.assertTrue(self.page.locator("#home-due-title-panel").get_by_text("视频明细", exact=True).is_visible())
        self.assertEqual(self.page.locator("#connection-text").inner_text(), "访客浏览 · 仅供查看")
        self.assertEqual(self.page.locator("#sync-detail").text_content(), "仅供查看；登录后操作。")
        content_reads = sum(path == "/guest/api/content" for _, path in self.requests)
        self.page.evaluate("location.hash='#/202H/scripts'")
        self.page.wait_for_url("**/#/202H/ai")
        self.page.locator("#ai-task-list").wait_for()
        self.page.locator("#ai-task-list .skeleton-page").wait_for(state="detached")
        self.assertEqual(sum(path == "/guest/api/content" for _, path in self.requests), content_reads)
        self.assertFalse(self.page.evaluate("window.__mutationFlood"))
        self.assertEqual(self.page.evaluate("document.readyState"), "complete")
        self.assertEqual(self.page.evaluate("""async () => {
          await new Promise(resolve => setTimeout(resolve, 50));
          const before = window.__mutationCallbacks;
          PublicTextMode.scan(); PublicTextMode.scan(); PublicTextMode.scan();
          await new Promise(resolve => setTimeout(resolve, 50));
          return window.__mutationCallbacks - before;
        }"""), 0, "Scanning an unchanged page must not wake the sidebar observer")
        self.assertFalse(any(path.startswith("/api/") for _, path in self.requests))
        self.assertFalse(any(path.startswith("/media/") for _, path in self.requests))
        self.assertFalse(any(method != "GET" for method, _ in self.requests))

    def test_adopted_content_and_old_script_links_keep_automatic_history_readable(self):
        for module in ("content", "scripts"):
            with self.subTest(module=module):
                self.page.goto(ORIGIN + f"/#/202H/{module}/SCR-000000000002", wait_until="load")
                self.page.wait_for_url("**/#/202H/content/SCR-000000000002")
                body = self.page.locator("#content-adopted-script")
                body.locator(".adopted-copy").get_by_text("Published fixture script", exact=True).wait_for()
                self.page.wait_for_function("document.querySelector('[data-adopted-copy]').dataset.guestHidden === 'true'")
                self.assertTrue(body.locator("[data-adopted-copy]").is_hidden())
                self.assertTrue(self.page.locator("#script-derive").is_hidden())
                history = body.locator("[data-script-versions]")
                history.locator(":scope > summary").click()
                old = history.locator('[data-script-version="SCR-000000000001"]')
                old.locator(":scope > summary").click()
                self.assertTrue(old.get_by_text("Original fixture words", exact=True).is_visible())
                old.get_by_text("查看这版内容", exact=True).click()
                self.page.wait_for_url("**/#/202H/content/SCR-000000000001")
                self.page.locator(".adopted-copy").get_by_text("Original fixture words", exact=True).wait_for()
                self.assertFalse(self.page.evaluate("window.__mutationFlood"))
        self.assertFalse(any(path == "/guest/api/content" for _, path in self.requests))
        self.assertFalse(any(method != "GET" for method, _ in self.requests))

    def test_replaying_unconditional_hidden_writes_reproduces_sidebar_feedback_loop(self):
        self.legacy_hide = True
        self.page.goto(ORIGIN + "/#/202H/library", wait_until="load")
        self.assertTrue(self.page.evaluate("window.__mutationFlood"), "The actual shell must reproduce the old observer feedback loop")


class BundledGuestFullStartupTests(GuestFullStartupTests):
    bundled_assets = True

    def test_bundle_loads_all_modules_and_prefetches_state_before_dom_ready(self):
        self.page.goto(ORIGIN + "/#/202H/library", wait_until="load")
        self.page.locator(".case-card").first.wait_for()
        self.assertEqual(sum(path.startswith("/guest-assets/") for _, path in self.requests), 2)
        self.assertEqual(sum(path == "/guest/api/state" for _, path in self.requests), 1)
        metrics = self.page.evaluate("""() => ({
          state: performance.getEntriesByType('resource').find(entry => new URL(entry.name).pathname === '/guest/api/state').startTime,
          ready: performance.getEntriesByType('navigation')[0].domContentLoadedEventStart,
          loaded: !!window.CaseAI && !!window.CaseProduction && !!window.PublicTextMode && window.WorkbenchGuest,
        })""")
        self.assertLess(metrics["state"], metrics["ready"])
        self.assertTrue(metrics["loaded"])


if __name__ == "__main__":
    unittest.main()
