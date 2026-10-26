"""Guest bundles preserve script units, initialization order and CSS cascade."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from guest_assets import bundle_guest_assets


class GuestAssetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="guest-assets-test-")
        self.addCleanup(self.temporary.cleanup)
        self.site = Path(self.temporary.name)

    def write(self, name, text):
        (self.site / name).write_text(text, encoding="utf-8")

    def test_bundles_keep_sync_guards_separate_and_styles_in_cascade_order(self):
        for name in ("guard.js", "a.js", "b.js"):
            self.write(name, f"window.order.push('{name}');")
        self.write("a.css", '@charset "UTF-8";\n.fixture {color:red}')
        self.write("b.css", ".fixture {color:blue}")
        page = ('<script src="/guard.js"></script>'
                '<link rel="stylesheet" href="/a.css">'
                '<script src="/a.js" defer></script>'
                '<link rel="stylesheet" href="/b.css">'
                '<script defer src="/b.js"></script>')
        result = bundle_guest_assets(self.site, page)
        self.assertIn('<script src="/guard.js"></script>', result)
        self.assertEqual(result.count('src="/guest-program-0.js"'), 1)
        self.assertEqual(result.count('href="/guest-styles-0.css"'), 1)
        style = (self.site / "guest-styles-0.css").read_text(encoding="utf-8")
        self.assertLess(style.index("color:red"), style.index("color:blue"))
        self.assertNotIn("@charset", style)
        self.assertTrue((self.site / "a.js").is_file(), "old resource URLs remain usable")

    def test_single_assets_and_script_boundaries_stay_unchanged(self):
        for name in ("a.js", "b.js", "c.js"):
            self.write(name, "window.fixture = true;")
        self.write("a.css", ".fixture {color:red}")
        for boundary in ('<script src="/b.js" type="module"></script>',
                         '<script src="/b.js" defer integrity="fixture"></script>',
                         '<script>window.inline = true;</script>'):
            page = ('<script src="/a.js" defer></script>' + boundary +
                    '<script src="/c.js" defer></script><link rel="stylesheet" href="/a.css">')
            self.assertEqual(bundle_guest_assets(self.site, page), page)
        self.write("b.js", "window.source = document.currentScript.src;")
        page = '<script src="/a.js" defer></script><script src="/b.js" defer></script>'
        self.assertEqual(bundle_guest_assets(self.site, page), page)

    def test_css_import_media_and_inline_style_are_cascade_boundaries(self):
        for name in ("a.css", "b.css", "c.css"):
            self.write(name, f".fixture {{content:'{name}'}}")
        for boundary in ('<link rel="stylesheet" href="/b.css" media="print">',
                         '<style>.fixture {color:black}</style>'):
            page = '<link rel="stylesheet" href="/a.css">' + boundary + '<link rel="stylesheet" href="/c.css">'
            self.assertEqual(bundle_guest_assets(self.site, page), page)
        self.write("b.css", '@import "a.css";\n.fixture {color:blue}')
        page = ''.join(f'<link rel="stylesheet" href="/{name}.css">' for name in ("a", "b", "c"))
        self.assertEqual(bundle_guest_assets(self.site, page), page)

    def test_missing_local_assets_fail_and_external_urls_are_untouched(self):
        page = '<script src="/a.js" defer></script><script src="/b.js" defer></script>'
        with self.assertRaisesRegex(ValueError, "guest_bundle_asset_missing"):
            bundle_guest_assets(self.site, page)
        remote = '<script src="https://example.com/a.js" defer></script>'
        self.assertEqual(bundle_guest_assets(self.site, remote), remote)

    def test_only_plain_known_bootstrap_scripts_are_inlined_and_html_is_escaped(self):
        sample = '</ScRiPt><p id="injected">unsafe</p><script>'
        self.write("guest-mode.js", 'window.sample=' + json.dumps(sample) + ';')
        self.write("public-mode.js", 'window.publicReady=true;')
        self.write("other.js", 'window.otherReady=true;')
        page = ('<script src="/guest-assets/guest-mode.js?v=before"></script>'
                '<script src="/public-mode.js"></script><script src="/other.js"></script>'
                '<script src="/guest-mode.js" nonce="preserve"></script>')
        result = bundle_guest_assets(self.site, page)
        self.assertIn('data-guest-bootstrap="guest-mode.js"', result)
        self.assertIn('data-guest-bootstrap="public-mode.js"', result)
        self.assertNotIn('src="/public-mode.js"', result)
        self.assertNotIn('</ScRiPt>', result)
        self.assertIn('\\u003c/ScRiPt>', result)
        self.assertIn('<script src="/other.js"></script>', result)
        self.assertIn('<script src="/guest-mode.js" nonce="preserve"></script>', result)

    @unittest.skipUnless(shutil.which("node"), "Node is required to verify classic script behavior")
    def test_classic_scopes_strictness_errors_and_microtasks_are_preserved(self):
        self.write("a.js", '"use strict";window.order=[typeof later];const sharedLexical=11;'
                   'Promise.resolve().then(()=>order.push("micro"));')
        self.write("b.js", 'function later() {} order.push("second");window.shared=sharedLexical;'
                   'window.sloppy=(function(){return this===window})();')
        self.write("c.js", 'throw new Error("unit failure");')
        self.write("d.js", 'order.push("after-error");')
        page = ''.join(f'<script src="/{name}.js" defer></script>' for name in "abcd")
        bundle_guest_assets(self.site, page)
        bundle = self.site / "guest-program-0.js"
        checked = subprocess.run(["node", "--check", str(bundle)], capture_output=True, timeout=15)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode())
        runner = """
const fs=require('node:fs'),vm=require('node:vm');
const context=vm.createContext({Promise});context.window=context;
context.document={createElement:()=>({dataset:{},remove(){}}),head:{appendChild(script){
  try { vm.runInContext(script.textContent,context); } catch(error) { context.unitError=error.message; }
}}};
(async()=>{await vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
process.stdout.write(JSON.stringify({order:context.order,sloppy:context.sloppy,shared:context.shared,error:context.unitError}));})();
"""
        result = subprocess.run(["node", "-e", runner, str(bundle)], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertEqual(json.loads(result.stdout), {"order": ["undefined", "micro", "second", "after-error"],
                         "sloppy": True, "shared": 11, "error": "unit failure"})

    def test_real_browser_finishes_all_script_microtasks_before_dom_ready(self):
        from playwright.sync_api import sync_playwright
        self.write("guard.js", 'window.order=["guard"];')
        self.write("a.js", 'order.push("a");Promise.resolve().then(()=>order.push("micro"));')
        self.write("b.js", 'order.push("b");')
        self.write("a.css", '.fixture {color:red}')
        self.write("b.css", '.fixture {color:blue}')
        page = ('<!doctype html><html><head><script src="/guard.js"></script>'
                '<link rel="stylesheet" href="/a.css"><script src="/a.js" defer></script>'
                '<link rel="stylesheet" href="/b.css"><script src="/b.js" defer></script>'
                '</head><body><p class="fixture">Ready</p><script>'
                'document.addEventListener("DOMContentLoaded",()=>order.push("dcl"));</script></body></html>')
        page = bundle_guest_assets(self.site, page)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            tab = browser.new_page()
            errors = []
            tab.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                path = route.request.url.split("guest.test", 1)[1]
                if path == "/":
                    route.fulfill(body=page, content_type="text/html", headers={"Content-Security-Policy":
                        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self'"})
                else:
                    file = self.site / path.lstrip("/")
                    route.fulfill(body=file.read_bytes(), content_type="text/css" if file.suffix == ".css" else "text/javascript")
            tab.route("**/*", serve)
            tab.goto("https://guest.test/", wait_until="domcontentloaded")
            self.assertEqual(tab.evaluate("order"), ["guard", "a", "micro", "b", "dcl"])
            self.assertEqual(tab.locator(".fixture").evaluate("node => getComputedStyle(node).color"), "rgb(0, 0, 255)")
            self.assertEqual(errors, [])
            tab.close()
            browser.close()

    def test_real_browser_inline_guards_start_prefetch_before_deferred_program(self):
        from playwright.sync_api import sync_playwright
        sample = '</ScRiPt><p id="injected">unsafe</p><script>'
        self.write("guest-mode.js", '"use strict";const guardLexical=23;window.order=["guest","prefetch-start"];'
                   'window.prefetchStarted=performance.now();window.prefetch=fetch("/guest/api/state");'
                   'window.escapeSample=' + json.dumps(sample) + ';')
        self.write("public-mode.js", 'window.bootstrapShared=guardLexical;order.push("public");')
        self.write("a.js", 'window.programStarted=performance.now();order.push("a");')
        self.write("b.js", 'order.push("b");')
        page = ('<!doctype html><html><head><script src="/guest-mode.js"></script>'
                '<script src="/public-mode.js"></script><script src="/a.js" defer></script>'
                '<script src="/b.js" defer></script></head><body><script>'
                'document.addEventListener("DOMContentLoaded",()=>order.push("dcl"));</script></body></html>')
        page = bundle_guest_assets(self.site, page)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            tab = browser.new_page()
            errors, requests = [], []
            tab.on("pageerror", lambda error: errors.append(str(error)))
            def serve(route):
                path = route.request.url.split("guest.test", 1)[1]
                requests.append(path)
                if path == "/":
                    route.fulfill(body=page, content_type="text/html", headers={"Content-Security-Policy":
                        "default-src 'none'; script-src 'self' 'unsafe-inline'; connect-src 'self'"})
                elif path == "/guest/api/state":
                    route.fulfill(body='{"ready":true}', content_type="application/json")
                else:
                    file = self.site / path.lstrip("/")
                    route.fulfill(body=file.read_bytes(), content_type="text/javascript")
            tab.route("**/*", serve)
            tab.goto("https://guest.test/", wait_until="domcontentloaded")
            self.assertEqual(tab.evaluate("order"), ["guest", "prefetch-start", "public", "a", "b", "dcl"])
            self.assertTrue(tab.evaluate("prefetchStarted <= programStarted"))
            self.assertEqual(tab.evaluate("bootstrapShared"), 23)
            self.assertEqual(tab.evaluate("escapeSample"), sample)
            self.assertEqual(tab.locator("#injected").count(), 0)
            self.assertNotIn("/guest-mode.js", requests)
            self.assertNotIn("/public-mode.js", requests)
            self.assertIn("/guest/api/state", requests)
            self.assertEqual(errors, [])
            tab.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
