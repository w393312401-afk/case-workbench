"""Bundled startup preserves CSP, script boundaries and immutable version bytes."""
from __future__ import annotations

import gzip
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from program_assets import ProgramAssets, build_snapshot, script_hash
import test_server as fixture


class ProgramAssetBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-assets-")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.write("a.js", '"use strict";window.order=[typeof later];const shared=11;Promise.resolve().then(()=>order.push("micro"));')
        self.write("b.js", 'function later() {} order.push("second");window.value=shared;window.sloppy=(function(){return this===window})();')
        self.write("a.css", '@charset "UTF-8";\n.fixture{color:red}')
        self.write("b.css", '.fixture{color:blue}')
        self.write("index.html", '<!doctype html><html><head><link rel="stylesheet" href="/a.css">'
                   '<script src="/a.js" defer></script><link rel="stylesheet" href="/b.css">'
                   '<script src="/b.js" defer></script></head><body><p class="fixture">Ready</p></body></html>')

    def write(self, name, source):
        (self.folder / name).write_text(source, encoding="utf-8")

    def test_cascade_order_and_exact_hashes_without_changing_source_files(self):
        snapshot = build_snapshot(self.folder)
        self.assertEqual(len(snapshot.resources), 2)
        styles = snapshot.resources["workbench-styles-0.css"]
        self.assertLess(styles.raw.index(b"color:red"), styles.raw.index(b"color:blue"))
        self.assertNotIn(b"@charset", styles.raw)
        self.assertIn(script_hash((self.folder / "a.js").read_text() + "\n//# sourceURL=/a.js"), snapshot.script_hashes)
        self.assertIn(b"src=\"/workbench-program-0.js?v=", snapshot.index.raw)
        self.assertTrue((self.folder / "a.js").is_file())
        self.assertFalse((self.folder / "workbench-program-0.js").exists())

    def test_script_and_stylesheet_boundaries_are_preserved(self):
        for boundary in ('<script>window.inline=true;</script>', '<script src="/a.js" type="module"></script>'):
            self.write("index.html", '<head><script src="/a.js" defer></script>' + boundary + '<script src="/b.js" defer></script></head>')
            self.assertEqual(build_snapshot(self.folder).resources, {})
        self.write("a.js", 'window.source=document.currentScript.src;')
        self.write("index.html", '<head><script src="/a.js" defer></script><script src="/b.js" defer></script></head>')
        self.assertEqual(build_snapshot(self.folder).resources, {})
        self.write("index.html", '<head><link rel="stylesheet" href="/a.css"><link rel="stylesheet" href="/b.css" media="print"></head>')
        self.assertEqual(build_snapshot(self.folder).resources, {})

    def test_previous_versions_survive_an_edit_and_old_snapshots_are_bounded(self):
        cache = ProgramAssets(keep=2)
        first = cache.load(self.folder, 1)
        original = first.resources["workbench-program-0.js"]
        self.write("b.js", 'window.version=2;')
        second = cache.load(self.folder, 2).resources[original.name]
        self.assertNotEqual(original.version, second.version)
        self.assertEqual(cache.find(original.name, original.version).raw, original.raw)
        self.assertIs(cache.load(self.folder, 2).resources[original.name], second)
        self.write("b.js", 'window.version=3;')
        cache.load(self.folder, 3)
        self.assertIsNone(cache.find(original.name, original.version))
        self.assertIsNone(cache.find(original.name, "000000000000"))

    def test_real_browser_preserves_scopes_microtasks_cascade_and_blocks_unlisted_inline_code(self):
        from playwright.sync_api import sync_playwright
        snapshot = build_snapshot(self.folder)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.add_init_script("document.addEventListener('DOMContentLoaded',()=>window.order.push('dcl'))")

                def serve(route):
                    path = route.request.url.split("assets.test", 1)[1].split("?", 1)[0]
                    if path == "/":
                        route.fulfill(body=snapshot.index.raw, content_type="text/html", headers={"Content-Security-Policy":
                            "default-src 'none'; script-src 'self' " + " ".join(snapshot.script_hashes) + "; style-src 'self'"})
                    else:
                        resource = snapshot.resources[path[1:]]
                        route.fulfill(body=resource.raw, content_type="text/css" if path.endswith(".css") else "text/javascript")

                page.route("https://assets.test/**", serve)
                page.goto("https://assets.test/", wait_until="domcontentloaded")
                self.assertEqual(page.evaluate("({order,value,sloppy})"),
                                 {"order": ["undefined", "micro", "second", "dcl"], "value": 11, "sloppy": True})
                self.assertEqual(page.locator(".fixture").evaluate("node=>getComputedStyle(node).color"), "rgb(0, 0, 255)")
                page.evaluate("const injected=document.createElement('script');injected.textContent='window.unlistedExecuted=true';document.head.appendChild(injected)")
                self.assertFalse(page.evaluate("window.unlistedExecuted===true"))
                self.assertEqual(errors, [])
            finally:
                browser.close()


class ProgramAssetHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_same_length_asset_edit_rebuilds_index_and_keeps_the_old_version_exact(self):
        temporary = self.root / "program-fixture"
        shutil.copytree(fixture.APP_ROOT / "static", temporary / "static")
        with patch.object(fixture.SERVER, "APP_ROOT", temporary):
            _, _, before = self.request("GET", "/")
            url = re.search(rb'href="(/workbench-styles-0\.css\?v=[0-9a-f]{12})"', before)[1].decode()
            _, _, original_bundle = self.request("GET", url)
            file = temporary / "static/style.css"
            original = file.read_bytes()
            marker = file.stat()
            changed = original.replace(b"14px", b"13px", 1)
            self.assertNotEqual(original, changed)
            self.assertEqual(len(original), len(changed))
            file.write_bytes(changed)
            os.utime(file, ns=(marker.st_atime_ns, marker.st_mtime_ns))
            _, _, after = self.request("GET", "/")
            next_url = re.search(rb'href="(/workbench-styles-0\.css\?v=[0-9a-f]{12})"', after)[1].decode()
            self.assertNotEqual(url, next_url)
            self.assertEqual(self.request("GET", url)[2], original_bundle)
            self.assertNotEqual(self.request("GET", next_url)[2], original_bundle)

    def test_index_uses_two_program_resources_with_private_immutable_caching(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        page = body.decode()
        references = re.findall(r'(?:src|href)="(/workbench-[^"]+)"', page)
        self.assertEqual(len(references), 2)
        self.assertIn("'sha256-", headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-inline'", headers["Content-Security-Policy"])
        self.assertNotIn("'unsafe-eval'", headers["Content-Security-Policy"])
        for path in references:
            status, asset, content = self.request("GET", path, headers={"Accept-Encoding": "gzip"})
            self.assertEqual(status, 200)
            self.assertEqual(asset["Cache-Control"], "private, max-age=31536000, immutable")
            self.assertEqual(asset["Content-Encoding"], "gzip")
            self.assertTrue(gzip.decompress(content))
            checked, _, empty = self.request("GET", path, headers={"Accept-Encoding": "gzip", "If-None-Match": asset["ETag"]})
            self.assertEqual((checked, empty), (304, b""))
            status, head, empty = self.request("HEAD", path, headers={"Accept-Encoding": "gzip"})
            self.assertEqual((status, head["Content-Length"], empty), (200, str(len(content)), b""))
        self.assertEqual(self.request("GET", "/notion-upload.js")[0], 200)
        for suffix in ("v=000000000000", "v=bad", "v=000000000000&v=111111111111"):
            status, response, _ = self.request("GET", "/workbench-program-0.js?" + suffix)
            self.assertIn(status, (400, 404))
            self.assertEqual(response["Cache-Control"], "no-store")
