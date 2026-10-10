"""Reading saved writing records must not wait for external client discovery."""
from __future__ import annotations

import json
import unittest

import test_ui_regressions as browser
import test_server as fixture


TASK = "AIT-0123456789ab"


class AILoadingBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def open_writing(self, tail=""):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(5000)
        pending, errors = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.route("**/api/ai/status", lambda route: pending.append(route))
        page.route("**/api/ai/options", lambda route: pending.append(route))
        task = {"task_id": TASK, "status": "completed", "product": "202H",
                "action": "variants", "context": {"title": "可立即阅读的写稿记录"},
                "rounds": [{"round_id": "round-one", "status": "completed",
                            "instruction": "测试要求", "output": "保存的完整脚本", "messages": []}]}
        page.route("**/api/ai/tasks", lambda route: route.fulfill(json={"tasks": [task]}))
        page.route(f"**/api/ai/tasks/{TASK}", lambda route: route.fulfill(json={"task": task}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#ai{tail}", wait_until="domcontentloaded")
        return page, pending

    def finish_discovery(self, pending):
        for route in pending:
            body = ({"available": True, "label": "测试服务"} if route.request.url.endswith("/status") else
                    {"models": [], "skills": [], "default_model": None})
            route.fulfill(body=json.dumps(body), content_type="application/json")

    def test_list_is_readable_while_client_status_is_pending(self):
        page, pending = self.open_writing()
        page.get_by_text("可立即阅读的写稿记录", exact=False).first.wait_for()
        self.assertTrue(any(route.request.url.endswith("/status") for route in pending))
        self.finish_discovery(pending)
        page.locator("#ai-service-label").get_by_text("可以开始协作", exact=False).wait_for()

    def test_saved_reply_is_readable_before_models_and_status_arrive(self):
        page, pending = self.open_writing("/" + TASK)
        page.locator("#ai-rounds").get_by_text("保存的完整脚本", exact=False).wait_for()
        self.assertTrue(any(route.request.url.endswith("/options") for route in pending))
        self.assertTrue(page.locator("#ai-continue-model").is_disabled())
        self.finish_discovery(pending)
        page.wait_for_function("() => !document.querySelector('#ai-continue-model').disabled")
        self.assertEqual(page.locator("#ai-rounds").get_by_text("保存的完整脚本", exact=False).count(), 1)


if __name__ == "__main__":
    unittest.main()
