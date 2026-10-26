"""AI history selection and local deletion in an isolated browser workbench.

Run with: python -m unittest discover -s tests -p test_ai_selection_ui.py -v
The server uses a temporary case library and an ephemeral port.
"""

from __future__ import annotations

import time
import unittest
from uuid import uuid4

from playwright.sync_api import sync_playwright

import test_server as fixture


class FakeBridge:
    def status(self):
        return {"available": True, "label": "临时测试助手"}

    def run(self, prompt, *, cwd, on_progress, cancel_event, **options):
        on_progress("测试任务正在处理")
        return "## 临时候选结果\n\n这条内容只属于临时测试案例。"


class AISelectionBrowserTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        except Exception:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.app.ai.bridge = FakeBridge()
        self.server.daemon_threads = False

    def create_task(self, instruction):
        body = {
            "request_id": uuid4().hex,
            "case_id": self.identifier,
            "revision": self.detail()["revision"],
            "action": "review",
            "instruction": instruction,
            "skill_selection": "none",
        }
        status, _, response = self.request("POST", "/api/ai/tasks", body)
        self.assertEqual(status, 200, response)
        task_id = response["task"]["task_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            task = self.server.app.ai.get(task_id)
            if task["status"] not in {"queued", "running"} and not task["busy"]:
                self.assertEqual(task["status"], "completed", task)
                return task
            time.sleep(0.02)
        self.fail(f"Temporary AI task did not finish: {task_id}")

    def open_ai_list(self):
        context = self.browser.new_context(viewport={"width": 1080, "height": 720})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#ai")
        page.locator("#connection-text").get_by_text("案例库同步").wait_for()
        return page

    def test_select_all_cancel_and_delete_local_ai_records(self):
        before_case = self.store.read_case(self.identifier)
        tasks = [self.create_task(f"临时历史记录 {index}") for index in range(3)]
        ids = [task["task_id"] for task in tasks]
        paths = {
            task_id: self.store.folder(self.identifier) / "collaboration" / task_id / "task.json"
            for task_id in ids
        }
        self.assertTrue(all(path.is_file() for path in paths.values()))

        page = self.open_ai_list()
        entries = page.locator(".ai-task-entry")
        page.wait_for_function("() => document.querySelectorAll('.ai-task-entry').length === 3")
        self.assertEqual(entries.count(), 3)
        self.assertTrue(page.locator("#ai-delete-selected").is_disabled())
        page.locator("#ai-select-toggle").click()  # checkboxes and delete appear in selection mode

        page.locator(f'[data-ai-task-id="{ids[0]}"] input[type="checkbox"]').check()
        self.assertEqual(page.locator("#ai-selected-count").inner_text(), "已选 1 条")
        self.assertTrue(page.locator("#ai-select-all").evaluate("input => input.indeterminate"))
        self.assertTrue(page.locator("#ai-delete-selected").is_enabled())

        page.locator("#ai-select-all").check()
        self.assertEqual(page.locator("#ai-selected-count").inner_text(), "已选 3 条")
        self.assertTrue(page.locator("#ai-select-all").is_checked())
        page.locator(f'[data-ai-task-id="{ids[2]}"] input[type="checkbox"]').uncheck()
        self.assertEqual(page.locator("#ai-selected-count").inner_text(), "已选 2 条")

        page.locator("#ai-delete-selected").click()
        dialog = page.locator("#ai-delete-dialog")
        self.assertTrue(dialog.evaluate("element => element.open"))
        self.assertIn("2 条", page.locator("#ai-delete-count").inner_text())
        page.locator("#cancel-ai-delete").click()
        self.assertFalse(dialog.evaluate("element => element.open"))
        self.assertTrue(all(path.is_file() for path in paths.values()))
        self.assertEqual(entries.count(), 3)

        page.locator("#ai-delete-selected").click()
        page.locator("#confirm-ai-delete").click()
        page.wait_for_function("() => document.querySelectorAll('.ai-task-entry').length === 1")
        page.wait_for_function("() => !document.querySelector('#ai-delete-dialog').open")
        self.assertEqual(entries.first.get_attribute("data-ai-task-id"), ids[2])
        self.assertEqual(page.locator("#ai-selected-count").inner_text(), "已选 0 条")
        self.assertTrue(page.locator("#ai-delete-selected").is_disabled())
        self.assertFalse(paths[ids[0]].exists())
        self.assertFalse(paths[ids[1]].exists())
        self.assertTrue(paths[ids[2]].is_file())

        page.locator("#ai-select-all").check()
        self.assertEqual(page.locator("#ai-selected-count").inner_text(), "已选 1 条")
        page.locator("#ai-delete-selected").click()
        page.locator("#confirm-ai-delete").click()
        page.locator("#ai-task-list .ai-start").get_by_role("heading", name="开始写稿", exact=True).wait_for()
        page.wait_for_function("() => !document.querySelector('#ai-delete-dialog').open")
        self.assertEqual(entries.count(), 0)
        self.assertTrue(page.locator("#ai-select-all").is_disabled())
        self.assertFalse(paths[ids[2]].exists())
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        self.assertEqual(self.store.read_case(self.identifier), before_case)

    def test_management_state_archive_history_and_restore_keep_answers(self):
        before_case = self.store.read_case(self.identifier)
        task = self.create_task("长期管理临时任务")
        path = self.store.folder(self.identifier) / "collaboration" / task["task_id"] / "task.json"
        page = self.open_ai_list()
        entry = page.locator(f'[data-ai-task-id="{task["task_id"]}"]')
        entry.wait_for()
        self.assertEqual(page.locator("#ai-list-view").input_value(), "active")
        entry.locator("a").click()
        page.locator("#ai-management-state").wait_for()
        page.locator("#ai-management-state").select_option("abandoned")
        # Refreshes must preserve an unsaved management choice.
        page.wait_for_timeout(2700)
        self.assertEqual(page.locator("#ai-management-state").input_value(), "abandoned")
        page.locator("#ai-save-management").click()
        page.wait_for_function("() => document.querySelector('#ai-management-state').dataset.persistedState === 'abandoned'")
        current = self.server.app.ai.get(task["task_id"])
        self.assertEqual(current["management_state"], "abandoned")
        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["rounds"], task["rounds"])
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#ai")
        entry.wait_for()
        page.locator("#ai-select-toggle").click()
        page.locator("#ai-select-all").check()
        page.locator("#ai-archive-selected").click()
        page.wait_for_function("() => document.querySelectorAll('.ai-task-entry').length === 0")
        self.assertTrue(path.is_file())
        self.assertTrue(self.server.app.ai.get(task["task_id"])["archived"])
        page.locator("#ai-list-view").select_option("history")
        entry.wait_for()
        self.assertIn("已归档", entry.inner_text())
        page.locator("#ai-select-all").check()
        page.locator("#ai-restore-selected").click()
        page.wait_for_function("() => document.querySelectorAll('.ai-task-entry').length === 0")
        page.locator("#ai-list-view").select_option("active")
        entry.wait_for()
        current = self.server.app.ai.get(task["task_id"])
        self.assertFalse(current["archived"])
        self.assertEqual(current["management_state"], "abandoned")
        self.assertEqual(current["rounds"], task["rounds"])
        self.assertEqual(self.store.read_case(self.identifier), before_case)

    def test_lost_archive_response_can_retry_same_request_after_task_leaves_active_list(self):
        task = self.create_task("临时重试归档任务")
        page = self.open_ai_list()
        page.locator(".ai-task-entry").wait_for()
        requests = []

        def manage_route(route):
            requests.append(route.request.post_data_json)
            if len(requests) == 1:
                response = route.fetch(headers={**route.request.all_headers(), "connection": "close"})
                self.assertEqual(response.status, 200)
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/api/ai/tasks/manage", manage_route)
        page.locator("#ai-select-toggle").click()
        page.locator("#ai-select-all").check()
        page.locator("#ai-archive-selected").click()
        page.get_by_role("button", name="重试归档", exact=True).wait_for()
        page.wait_for_function("() => document.querySelectorAll('.ai-task-entry').length === 0")
        archived = self.server.app.ai.get(task["task_id"])
        self.assertTrue(archived["archived"])
        self.assertTrue(page.locator("#ai-archive-selected").is_enabled())
        page.locator("#ai-archive-selected").click()
        page.get_by_role("button", name="归档所选", exact=True).wait_for()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(self.server.app.ai.get(task["task_id"])["revision"], archived["revision"])


if __name__ == "__main__":
    unittest.main()
