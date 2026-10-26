"""Disposable browser fixture: result reading, original adoption, action-specific UI."""
import copy
from pathlib import Path
import unittest

from playwright.sync_api import expect
import test_ai_selection_ui as browser
import test_server as fixture


class NoiseContentBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_task(self, action="variants", *, color="light"):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850}, color_scheme=color)
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors, posts = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        output = '## 变体 1\nmain_difference: 固定角度\nconcern: 装夹麻烦\n\n**口播（配音用）**\nKeep the angle steady.\n\n**中文对照**\n保持角度稳定。\n\n```\n🔴Hold It Steady#sharpening\n```'
        task = {"task_id": "AIT-0123456789ab", "revision": 1, "product": "202H", "action": action,
                "status": "completed", "management_state": "pending_selection", "created_at": "2026-10-23T02:30:00Z",
                "display_title": "稳定推进｜" + {"variants": "候选稿", "review": "审稿结果", "retrospective": "复盘结果"}[action],
                "context": {"product": "202H", "review_video": {"video_id": "1234567890123456789", "product": "202H", "window_end": "2026-10-18"}} if action == "retrospective" else {"title": "稳定推进"},
                "progress": "生成完毕，等待查看候选",
                "rounds": [{"round_id": "AIR-0123456789ab", "status": "completed", "instruction": "完整保留的要求。" * 40,
                            "output": output, "model": "gpt-6.1-sol", "reasoning_effort": "medium",
                            "messages": [{"item_id": "one", "phase": "commentary", "complete": True, "text": "正在处理"},
                                         {"item_id": "two", "phase": "final_answer", "complete": True, "text": output}]}]}
        original = copy.deepcopy(task)

        def mock_ai(route):
            path = route.request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if route.request.method != "GET":
                posts.append(route.request.url)
                route.fulfill(status=400, json={"error": "Unexpected fixture mutation"})
            elif path == "/status":
                route.fulfill(json={"available": True, "label": "测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [task]})
            elif path == "/tasks/" + task["task_id"]:
                route.fulfill(json={"task": task})
            else:
                route.fulfill(status=404, json={"error": "Fixture endpoint"})
        page.route("**/api/ai/**", mock_ai)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{task['task_id']}")
        expect(page.locator(".ai-round-output")).to_be_visible()
        return page, task, original, output, posts

    def test_final_answer_is_single_reading_copy_but_adoption_keeps_original(self):
        for color in ("light", "dark"):
            with self.subTest(color=color):
                page, task, original, output, posts = self.open_task(color=color)
                body = page.locator(".ai-round-output").inner_text()
                self.assertIn("主打差异", body)
                self.assertIn("观众顾虑", body)
                self.assertIn("Keep the angle steady.", body)
                self.assertIn("保持角度稳定。", body)
                self.assertNotIn("main_difference", body)
                self.assertNotIn("🔴", body)
                self.assertEqual(page.locator(".ai-stream-message:visible").count(), 0)
                self.assertFalse(page.locator(".ai-user-request").evaluate("el => el.open"))
                self.assertFalse(page.locator(".ai-title-lines-detail").evaluate("el => el.open"))
                self.assertFalse(page.locator(".ai-raw-output").evaluate("el => el.open"))
                self.assertEqual(page.locator(".ai-raw-output pre").text_content(), output)
                self.assertIn("思考强度：中", page.locator(".ai-run-line").inner_text())
                self.assertTrue(page.locator(".ai-task-progress").is_hidden())
                self.assertFalse(page.locator("#workflow-links details").first.evaluate("el => el.open"))
                report_dir = Path(__file__).resolve().parents[1] / "reports" / "information-noise-implementation-2026-10-23"
                report_dir.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(report_dir / f"content-fixture-{color}.png"), full_page=True)
                page.evaluate("() => {window.savedRound=null;window.CaseProduction.fromCandidate=(task,round)=>{window.savedRound=JSON.parse(JSON.stringify(round));};}")
                page.locator("[data-start-production]").click()
                self.assertEqual(page.evaluate("window.savedRound.output"), output)
                self.assertEqual(task, original)
                self.assertEqual(posts, [])

    def test_review_and_retrospective_have_confirmation_and_correct_result_actions(self):
        for action, label in (("review", "审稿"), ("retrospective", "复盘")):
            with self.subTest(action=action):
                page, task, original, output, posts = self.open_task(action)
                expect(page.locator(".ai-output-title")).to_have_text(label + "结论")
                self.assertTrue(page.locator(".ai-candidate-actions").is_hidden())
                self.assertEqual(page.locator("#ai-task-adopted").inner_text(), "")
                self.assertNotIn("待选稿", page.locator("#ai-management-state").inner_text())
                self.assertNotIn("空白写稿", page.locator(".ai-task-context").inner_text())
                self.assertNotIn("采用稿", page.locator("#workflow-links").inner_text())
                expect(page.locator("#ai-continue-submit")).to_have_text("继续" + label)
                if action == "retrospective":
                    self.assertIn("feedback_window=2026-10-18", page.locator(".ai-task-context>a").get_attribute("href"))
                self.assertEqual(task, original)
                self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
