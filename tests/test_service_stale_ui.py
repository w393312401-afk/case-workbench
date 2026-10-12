"""The page tells the user when the running service predates an update."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

import test_production_carousel_ui as carousel_fixture
import server


class ServiceStaleTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def test_status_reports_whether_the_code_changed_since_start(self):
        status, _, body = self.request("GET", "/api/service/status")
        self.assertEqual(status, 200)
        payload = body if isinstance(body, dict) else json.loads(body)
        self.assertIs(payload["code_stale"], False)
        self.assertGreater(server.code_signature(), 0)

    def test_banner_appears_for_a_stale_service_and_can_be_dismissed(self):
        page = self.open_page("#production")
        page.route("**/api/service/status", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"app": "video-case-workbench", "code_stale": True})))
        page.evaluate("() => checkServiceFresh()")
        banner = page.locator("#code-stale-banner")
        expect(banner).to_be_visible()
        expect(banner).to_contain_text("重启")
        banner.locator("[data-stale-later]").click()
        expect(banner).to_have_count(0)

    def test_restart_button_asks_the_service_and_reports_a_refusal(self):
        page = self.open_page("#production")
        page.route("**/api/service/status", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({"app": "video-case-workbench", "pid": 1, "code_stale": True})))
        page.route("**/api/service/restart", lambda route: route.fulfill(
            status=409, content_type="application/json", body=json.dumps({"error": "AI 剪辑仍在运行，请等它结束或先停止，再重新启动。"})))
        page.evaluate("() => checkServiceFresh()")
        banner = page.locator("#code-stale-banner")
        banner.locator("[data-stale-restart]").click()
        expect(banner).to_contain_text("AI 剪辑仍在运行")
        expect(banner.locator("[data-stale-restart]")).to_be_enabled()

    def test_restart_endpoint_never_interrupts_running_work(self):
        from unittest import mock
        app, launched = self.server.app, []
        with mock.patch.object(server.subprocess, "Popen", side_effect=lambda *a, **k: launched.append(a[0])):
            app._table_state()["TBL-000000000000"] = {"progress": ""}
            try:
                status, _, body = self.request("POST", "/api/service/restart", {})
                self.assertEqual(status, 409, body)
                self.assertEqual(launched, [])
            finally:
                app._table_state().clear()
            status, _, body = self.request("POST", "/api/service/restart", {})
            self.assertEqual(status, 200, body)
            self.assertTrue(launched[0][-2].endswith("launcher.pyw") and launched[0][-1] == "--no-browser")


if __name__ == "__main__":
    unittest.main()
