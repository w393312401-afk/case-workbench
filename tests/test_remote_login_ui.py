"""Account login browser checks; all requests use a disposable mocked origin."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote, urlsplit

from playwright.sync_api import sync_playwright
import test_server as fixture


ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://workbench.test"


class RemoteLoginUITests(unittest.TestCase):
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
        self.login_status = 200
        self.session_status = 200
        self.username = "reader"
        self.exported = False
        self.actual_shell = False
        self.mode = None
        self.page.route("**/*", self.route)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(self.errors, []))

    def route(self, route):
        request = route.request
        self.requests.append((request.method, request.url, request.post_data))
        path = urlsplit(request.url).path
        if path == "/auth/login" and request.method == "GET":
            route.fulfill(status=200, content_type="text/html", body=(ROOT / "deployment/auth-assets/login.html").read_text(encoding="utf-8"))
        elif path in ("/auth/login.js", "/auth/login.css"):
            route.fulfill(status=200, content_type="text/javascript" if path.endswith(".js") else "text/css",
                          body=(ROOT / "deployment/auth-assets" / Path(path).name).read_text(encoding="utf-8"))
        elif path == "/auth/login":
            route.fulfill(status=self.login_status, content_type="application/json", headers={"Retry-After": "30"},
                          body=json.dumps({"ok": self.login_status == 200, "username": self.username}))
        elif path == "/auth/mode":
            route.fulfill(status=404 if self.mode is None else 200, content_type="application/json", body=json.dumps(self.mode or {"error": "Not found"}))
        elif path == "/auth/session":
            route.fulfill(status=self.session_status, content_type="application/json",
                          body=json.dumps({"authenticated": self.session_status == 200, "username": self.username, "login_url": "/auth/login"}))
        elif path == "/auth/logout":
            route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
        elif path == "/remote-session.js":
            route.fulfill(status=200, content_type="text/javascript", body=(ROOT / "static/remote-session.js").read_text(encoding="utf-8"))
        elif path.startswith("/api/"):
            route.fulfill(status=401, content_type="application/json", headers={"X-Workbench-Auth": "required"} if path != "/api/plain" else {}, body='{"error":"auth"}')
        elif path == "/external":
            route.fulfill(status=401, content_type="application/json", headers={"X-Workbench-Auth": "required", "Access-Control-Allow-Origin": "*"}, body='{}')
        elif self.actual_shell and path.endswith(".css"):
            route.fulfill(status=200, content_type="text/css", body=(ROOT / "static" / Path(path).name).read_text(encoding="utf-8"))
        elif self.actual_shell and path.endswith(".js"):
            route.fulfill(status=200, content_type="text/javascript", body="")
        elif self.actual_shell and path == "/":
            route.fulfill(status=200, content_type="text/html", body=(ROOT / "static/index.html").read_text(encoding="utf-8"))
        else:
            exported = "<script>window.PublicTextMode={};</script>" if self.exported else ""
            route.fulfill(status=200, content_type="text/html", body=f"<!doctype html><html><head>{exported}<script src='/remote-session.js' defer></script></head><body><header><div class='topbar-right'></div></header><main>Fixture</main></body></html>")

    def fill(self):
        self.page.locator("#username").fill("reader")
        self.page.locator("#password").fill("temporary-test-passphrase")

    def test_login_uses_json_and_returns_to_hash_without_storing_password(self):
        next_path = "/?view=all#/202H/library"
        self.page.goto(ORIGIN + "/auth/login?next=" + quote(next_path, safe=""))
        self.fill()
        self.page.locator("#show-password").click()
        self.assertEqual(self.page.locator("#password").get_attribute("type"), "text")
        self.assertEqual(self.page.locator("#show-password").get_attribute("aria-pressed"), "true")
        self.page.locator("#login-submit").click()
        self.page.wait_for_url(ORIGIN + next_path)
        self.assertEqual(self.page.evaluate("[localStorage.length,sessionStorage.length]"), [0, 0])
        posts = [row for row in self.requests if row[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], ORIGIN + "/auth/login")
        self.assertEqual(json.loads(posts[0][2]), {"username": "reader", "password": "temporary-test-passphrase"})
        self.assertFalse(any("temporary-test-passphrase" in url for _, url, _ in self.requests))

    def test_unsafe_next_values_fall_back_to_home(self):
        for unsafe in ("//evil.test", "/\\evil.test", "/%2fevil.test", "https://evil.test", "/%250a//evil.test", "/auth/login", "/\x00evil", "/%25255cevil.test"):
            with self.subTest(next=unsafe):
                self.page.goto(ORIGIN + "/auth/login?next=" + quote(unsafe, safe=""))
                self.fill()
                self.page.locator("#login-submit").click()
                self.page.wait_for_url(ORIGIN + "/")

    def test_server_redirect_inherited_hash_returns_to_original_page(self):
        self.page.goto(ORIGIN + "/auth/login?next=%2F#/202H/library")
        self.fill()
        self.page.locator("#login-submit").click()
        self.page.wait_for_url(ORIGIN + "/#/202H/library")
        self.page.goto(ORIGIN + "/auth/login?next=" + quote("/#/202H/home", safe="") + "#/202H/library")
        self.fill()
        self.page.locator("#login-submit").click()
        self.page.wait_for_url(ORIGIN + "/#/202H/home")

    def test_guest_entry_requires_explicit_capability_and_unconfigured_account_is_disabled(self):
        self.page.goto(ORIGIN + "/auth/login")
        self.page.wait_for_function("!document.querySelector('#login-submit').disabled")
        self.assertTrue(self.page.locator("#guest-browse").is_hidden())
        self.mode = {"guest_enabled": True, "configured": False, "username": "must-not-be-shown"}
        self.page.reload()
        self.page.locator("#guest-browse").wait_for()
        self.assertEqual(self.page.locator("#guest-browse").get_attribute("href"), "/")
        self.assertTrue(self.page.locator("#login-submit").is_disabled())
        self.assertTrue(self.page.locator("#username").is_disabled())
        self.assertIn("尚未设置账号", self.page.locator("#login-availability").inner_text())
        self.assertNotIn("must-not-be-shown", self.page.locator("body").inner_text())
        self.assertEqual(self.page.locator("#username").input_value(), "")
        self.page.locator("#login-form").evaluate("f => f.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))")
        self.assertFalse(any(method == "POST" for method, _, _ in self.requests))
        self.mode = {"guest_enabled": True, "configured": True}
        self.page.reload()
        self.page.wait_for_function("!document.querySelector('#login-submit').disabled")
        self.assertTrue(self.page.locator("#guest-browse").is_visible())
        self.assertTrue(self.page.locator("#login-availability").is_hidden())

    def test_error_feedback_and_mobile_layout(self):
        self.page.goto(ORIGIN + "/auth/login")
        for response_status, expected in ((401, "账号或密码错误"), (429, "30 秒后重试"), (503, "暂时不可用")):
            self.login_status = response_status
            self.fill()
            self.page.locator("#login-submit").click()
            self.page.wait_for_function("expected => document.querySelector('#login-error').textContent.includes(expected)", arg=expected)
            self.assertEqual(self.page.locator("#login-error").get_attribute("role"), "alert")
            self.assertTrue(self.page.locator("#login-submit").is_enabled())
            if response_status == 401:
                self.assertEqual(self.page.locator("#password").input_value(), "")
        self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
        self.page.locator("#password").fill("")
        self.page.screenshot(path=str(Path(tempfile.gettempdir()) / "case-workbench-login-mobile.png"))

    def test_username_is_plain_text_and_logout_preserves_destination(self):
        self.username = '<img src=x onerror="alert(1)">'
        self.page.goto(ORIGIN + "/#/202H/home")
        self.page.locator("#remote-account summary").click()
        self.assertEqual(self.page.locator(".remote-account-name").inner_text(), self.username)
        self.assertEqual(self.page.locator("#remote-account img").count(), 0)
        self.page.locator(".remote-account-logout").click()
        self.page.wait_for_url(ORIGIN + "/auth/login?next=" + quote("/#/202H/home", safe=""))
        posts = [row for row in self.requests if row[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1], ORIGIN + "/auth/logout")
        self.assertEqual(posts[0][2], "{}")

    def test_expired_api_redirects_once_without_replaying_mutation(self):
        self.page.goto(ORIGIN + "/?demo=1#/202H/library")
        self.page.locator("#remote-account").wait_for()
        self.page.evaluate("void fetch('/api/save', {method:'POST', body:'save once'})")
        self.page.wait_for_url(ORIGIN + "/auth/login?next=" + quote("/?demo=1#/202H/library", safe=""))
        self.assertEqual(len([row for row in self.requests if "/api/save" in row[1]]), 1)

    def test_plain_401_and_cross_origin_401_are_not_auth_redirects(self):
        self.page.goto(ORIGIN + "/")
        self.page.locator("#remote-account").wait_for()
        self.assertEqual(self.page.evaluate("fetch('/api/plain').then(r=>r.status)"), 401)
        self.assertEqual(self.page.evaluate("fetch('https://unrelated.test/external').then(r=>r.status)"), 401)
        self.assertEqual(self.page.url, ORIGIN + "/")

    def test_legacy_404_is_silent_and_expired_session_redirects(self):
        self.session_status = 404
        self.page.goto(ORIGIN + "/")
        self.page.wait_for_load_state("networkidle")
        self.assertEqual(self.page.locator("#remote-account").count(), 0)
        self.assertEqual(self.page.url, ORIGIN + "/")
        self.session_status = 401
        self.page.goto(ORIGIN + "/?expired=1#/202H/home")
        self.page.wait_for_url(ORIGIN + "/auth/login?next=" + quote("/?expired=1#/202H/home", safe=""))

    def test_local_and_text_export_pages_do_not_request_session(self):
        for origin in ("http://127.0.0.1:1234", "http://localhost:1234", "http://[::1]:1234"):
            with self.subTest(origin=origin):
                self.requests.clear()
                self.page.goto(origin + "/")
                self.page.wait_for_load_state("networkidle")
                self.assertFalse(any("/auth/" in url for _, url, _ in self.requests))
        self.exported = True
        self.requests.clear()
        self.page.goto(ORIGIN + "/")
        self.page.wait_for_load_state("networkidle")
        self.assertFalse(any("/auth/" in url for _, url, _ in self.requests))

    def test_account_menu_fits_existing_desktop_and_mobile_header(self):
        self.actual_shell = True
        self.username = "long-workbench-account-name"
        self.page.goto(ORIGIN + "/")
        self.page.locator("#remote-account").wait_for()
        for width in (320, 390, 768, 1280):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 844})
                self.page.locator("#remote-account").evaluate("element => element.open = true")
                self.assertFalse(self.page.evaluate("document.documentElement.scrollWidth > innerWidth"))
                menu = self.page.locator(".remote-account-menu").bounding_box()
                self.assertGreaterEqual(menu["x"], 0)
                self.assertLessEqual(menu["x"] + menu["width"], width)


class RemoteSessionServerIntegrationTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    setUpClass = classmethod(RemoteLoginUITests.setUpClass.__func__)
    tearDownClass = classmethod(RemoteLoginUITests.tearDownClass.__func__)

    def test_remote_assets_are_served_with_real_csp_and_styles_apply(self):
        for asset, mime in (("remote-session.js", "javascript"), ("remote-session.css", "text/css")):
            status, headers, content = self.request("GET", "/" + asset)
            self.assertEqual(status, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertEqual(content, (ROOT / "static" / asset).read_bytes())
        context = self.browser.new_context(viewport={"width": 390, "height": 844})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(5000)
        page.add_init_script("window.__cspViolations=[]; document.addEventListener('securitypolicyviolation',e=>window.__cspViolations.push(e.violatedDirective));")

        def route_request(route):
            parsed = urlsplit(route.request.url)
            if parsed.path == "/auth/session":
                route.fulfill(status=200, content_type="application/json", body='{"authenticated":true,"username":"fixture-reader"}')
                return
            status, headers, content = self.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
            # Only the account feature executes; its HTML, stylesheet, script and
            # CSP still come from the actual server/static-route allowlist.
            if parsed.path.endswith(".js") and parsed.path != "/remote-session.js":
                content = b""
                headers.pop("Content-Length", None)
            if isinstance(content, dict):
                content = json.dumps(content)
            route.fulfill(status=status, headers=headers, body=content)

        page.route("**/*", route_request)
        page.goto(ORIGIN + "/")
        page.locator("#remote-account summary").click()
        self.assertEqual(page.locator(".remote-account-menu").evaluate("e=>getComputedStyle(e).position"), "absolute")
        self.assertEqual(page.locator("#remote-account summary").evaluate("e=>getComputedStyle(e).borderRadius"), "8px")
        self.assertEqual(page.evaluate("window.__cspViolations"), [])


if __name__ == "__main__":
    unittest.main()
