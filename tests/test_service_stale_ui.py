"""The page tells the user when the running service predates an update."""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

from production_browser import ProductionBrowserTests
import server


class ServiceStaleTests(unittest.TestCase):
    setUpClass = classmethod(ProductionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ProductionBrowserTests.tearDownClass.__func__)
    setUp = ProductionBrowserTests.setUp
    stop_server = ProductionBrowserTests.stop_server
    request = ProductionBrowserTests.request
    open_page = ProductionBrowserTests.open_page
    make_playable_video = ProductionBrowserTests.make_playable_video

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

    def test_gateway_update_explains_that_running_tasks_continue_and_keeps_its_button_after_refusal(self):
        page = self.open_page("#production")
        page.route("**/api/service/status", lambda route: route.fulfill(json={
            "app": "video-case-workbench", "pid": 101, "task_host_pid": 500,
            "code_stale": True, "restart_keeps_tasks": True, "task_host_code_stale": True}))
        page.route("**/api/service/restart", lambda route: route.fulfill(status=409, json={"error": "文件正在保存，请稍后重试。"}))
        page.evaluate("() => checkServiceFresh()")
        banner = page.locator("#code-stale-banner")
        expect(banner).to_contain_text("后台任务会继续运行")
        expect(banner).to_contain_text("当前任务按原版本继续")
        restart = banner.locator("[data-stale-restart]")
        expect(restart).to_have_text("重启网页服务")
        restart.click()
        expect(banner).to_contain_text("文件正在保存")
        expect(restart).to_be_enabled()
        expect(restart).to_have_text("重启网页服务")

    def test_task_engine_update_waits_for_idle_without_offering_another_web_restart(self):
        page = self.open_page("#production")
        status = {"app": "video-case-workbench", "pid": 101, "task_host_pid": 500,
                  "code_stale": False, "restart_keeps_tasks": True, "task_host_code_stale": True}
        page.route("**/api/service/status", lambda route: route.fulfill(json=status))
        page.evaluate("() => checkServiceFresh()")
        banner = page.locator("#code-stale-banner")
        expect(banner).to_contain_text("任务引擎将在空闲后更新，当前任务按原版本继续")
        expect(banner.locator("[data-stale-restart]")).to_have_count(0)
        expect(banner).to_have_attribute("role", "status")
        page.evaluate("() => checkServiceFresh()")
        expect(banner.locator("[data-stale-restart]")).to_have_count(0)
        status["task_host_code_stale"] = False
        page.evaluate("() => checkServiceFresh()")
        expect(banner).to_have_count(0)

    def test_ignoring_the_engine_notice_survives_web_restart_and_does_not_hide_a_web_update(self):
        page = self.open_page("#production")
        status = {"app": "video-case-workbench", "pid": 101, "task_host_pid": 500,
                  "code_stale": False, "restart_keeps_tasks": True, "task_host_code_stale": True}
        page.route("**/api/service/status", lambda route: route.fulfill(json=status))
        page.evaluate("() => checkServiceFresh()")
        page.locator("#code-stale-banner [data-stale-later]").click()
        status["pid"] = 102
        page.evaluate("() => checkServiceFresh()")
        expect(page.locator("#code-stale-banner")).to_have_count(0)
        status["code_stale"] = True
        page.evaluate("() => checkServiceFresh()")
        expect(page.locator("#code-stale-banner [data-stale-restart]")).to_have_text("重启网页服务")

    def test_gateway_restart_reloads_for_a_new_gateway_pid_while_the_same_task_host_waits_for_update(self):
        page = self.open_page("#production")
        page.clock.install()
        status = {"app": "video-case-workbench", "pid": 101, "task_host_pid": 500,
                  "code_stale": True, "restart_keeps_tasks": True, "task_host_code_stale": True}
        restarts = []

        def restart(route):
            restarts.append(route.request.post_data_json)
            status.update(pid=102, code_stale=False)
            route.fulfill(json={"ok": True, "pid": 101, "tasks_continue": True})

        page.route("**/api/service/status", lambda route: route.fulfill(json=status))
        page.route("**/api/service/restart", restart)
        page.evaluate("() => checkServiceFresh()")
        page.locator("#code-stale-banner [data-stale-restart]").click()
        expect(page.locator("#code-stale-banner")).to_contain_text("正在重启网页服务，后台任务继续运行")
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.clock.run_for(1501)
        page.evaluate("() => checkServiceFresh()")
        expect(page.locator("#code-stale-banner")).to_contain_text("任务引擎将在空闲后更新")
        expect(page.locator("#code-stale-banner [data-stale-restart]")).to_have_count(0)
        self.assertEqual(restarts, [{}])
        self.assertEqual(status["task_host_pid"], 500)

    def test_restart_endpoint_never_interrupts_running_work(self):
        from unittest import mock
        app, launched = self.server.app, []
        with mock.patch.object(server.subprocess, "Popen", side_effect=lambda *a, **k: launched.append(a[0])):
            with mock.patch.object(app, "edit_running", return_value=True):
                status, _, body = self.request("POST", "/api/service/restart", {})
            self.assertEqual(status, 409, body)
            self.assertEqual(launched, [])
            status, _, body = self.request("POST", "/api/service/restart", {})
            self.assertEqual(status, 200, body)
            self.assertTrue(launched[0][-2].endswith("launcher.pyw") and launched[0][-1] == "--no-browser")


if __name__ == "__main__":
    unittest.main()
