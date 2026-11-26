"""Unused writing conversations: grouping, safe cleanup and undo in an isolated browser."""
from __future__ import annotations

from copy import deepcopy
import unittest

import test_ai_production_entry_ui as browser
from test_ai_production_entry_ui import TASK_ID, titled_variant


class AIRoundCleanupBrowserTests(unittest.TestCase):
    setUpClass = browser.AIProductionEntryBrowserTests.__dict__["setUpClass"]
    tearDownClass = browser.AIProductionEntryBrowserTests.__dict__["tearDownClass"]
    setUp = browser.AIProductionEntryBrowserTests.setUp
    stop_server = browser.AIProductionEntryBrowserTests.stop_server
    request = browser.AIProductionEntryBrowserTests.request

    def task(self, *, all_unused=False):
        rounds = []
        for number in range(1, 7):
            protected = not all_unused and number in {1, 3, 5}
            rounds.append({
                "round_id": f"round-{number}", "number": number,
                "status": "failed" if number == 2 else "completed",
                "instruction": f"第 {number} 轮修改要求",
                "output": "" if number == 2 else titled_variant(1, "固定角度", f"Round {number}."),
                "error": "临时重译失败" if number == 2 else None,
                "messages": [], "skill_selection": "none",
                "cleanup": {"protected": protected,
                            "reason": ("采用稿来源" if number == 3 else "已采用") if protected else ""},
            })
        return {
            "task_id": TASK_ID, "revision": 1, "status": "completed", "busy": False,
            "stopping": False, "product": "202H", "action": "variants",
            "context": {"title": "临时清理轮次"}, "rounds": rounds,
            "round_cleanup": {"eligible_round_ids": [row["round_id"] for row in rounds if not row["cleanup"]["protected"]],
                              "deleted_count": 0, "undo_batch": None, "available": True},
        }

    def open_task(self, task=None):
        task = task or self.task()
        original = deepcopy(task["rounds"])
        context = self.browser.new_context(viewport={"width": 1100, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors, posts = [], []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def ai_api(route):
            path = route.request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if route.request.method == "POST" and path == f"/tasks/{TASK_ID}/round-cleanup":
                body = route.request.post_data_json
                posts.append(body)
                self.assertEqual(body["task_revision"], task["revision"])
                self.assertTrue(body.get("request_id"))
                if body["action"] == "delete_unused":
                    ids = list(task["round_cleanup"]["eligible_round_ids"])
                    task["rounds"] = [row for row in task["rounds"] if row["round_id"] not in ids]
                    task["round_cleanup"] = {
                        "eligible_round_ids": [], "deleted_count": len(ids), "available": True,
                        "undo_batch": {"id": "cleanup-fixture", "round_ids": ids, "count": len(ids)},
                    }
                    response = {"deleted_count": len(ids)}
                elif body["action"] == "undo":
                    self.assertEqual(body.get("batch_id"), "cleanup-fixture")
                    task["rounds"] = deepcopy(original)
                    count = task["round_cleanup"]["deleted_count"]
                    task["round_cleanup"] = {
                        "eligible_round_ids": [row["round_id"] for row in original if not row["cleanup"]["protected"]],
                        "deleted_count": 0, "undo_batch": None, "available": True,
                    }
                    response = {"restored_count": count}
                else:
                    self.fail(f"Unexpected cleanup action: {body}")
                task["revision"] += 1
                route.fulfill(json={"task": deepcopy(task), **response})
            elif route.request.method != "GET":
                self.fail(f"Unexpected mutation against fixture: {route.request.url}")
            elif path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [task]})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": deepcopy(task)})
            else:
                route.fulfill(status=404, json={"error": "Unexpected fixture endpoint"})

        page.route("**/api/ai/**", ai_api)
        page.route("**/api/creation?*", lambda route: route.fulfill(json={"revision": "fixture", "scripts": []}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{TASK_ID}")
        page.locator('[data-round-id="round-6"] .ai-variant-card').wait_for()
        page.locator("#ai-round-cleanup").wait_for()
        return page, task, posts

    def test_cleanup_keeps_adopted_and_source_rounds_and_undo_restores_original_numbers(self):
        page, task, posts = self.open_task()
        self.assertEqual(page.locator("#ai-unused-rounds .ai-round").count(), 3)
        self.assertEqual(page.locator("#ai-kept-rounds .ai-round").count(), 3)
        self.assertIn("采用稿来源", page.locator("#ai-round-rows").inner_text())
        page.locator("#ai-round-cleanup").click()
        page.wait_for_function("() => document.querySelectorAll('#ai-rounds .ai-round').length === 3")
        self.assertEqual([row["round_id"] for row in task["rounds"]], ["round-1", "round-3", "round-5"])
        self.assertEqual(page.locator("#ai-kept-rounds .ai-round-toggle h2").all_inner_texts(), ["第 1 轮", "第 3 轮", "第 5 轮"])
        self.assertTrue(page.locator("#ai-round-cleanup").is_disabled())
        page.evaluate("() => window.CaseAI.route(true)")
        self.assertEqual(page.locator("#ai-rounds .ai-round").count(), 3)
        page.locator("#ai-round-cleanup-undo").click()
        page.wait_for_function("() => document.querySelectorAll('#ai-rounds .ai-round').length === 6")
        self.assertEqual(page.locator("#ai-unused-rounds .ai-round-toggle h2").all_inner_texts(), ["第 2 轮", "第 4 轮", "第 6 轮"])
        self.assertEqual([body["action"] for body in posts], ["delete_unused", "undo"])
        self.assertEqual(posts[1]["task_revision"], 2)

    def test_deleting_every_unused_round_keeps_the_followup_draft_and_cursor(self):
        page, _, posts = self.open_task(self.task(all_unused=True))
        field = page.locator("#ai-continue-instruction")
        field.fill("保留我正在写的下一轮要求")
        field.evaluate("el => { el.focus(); el.setSelectionRange(3, 7); }")
        page.evaluate("() => window.CaseAI.route(true)")
        self.assertEqual(field.input_value(), "保留我正在写的下一轮要求")
        self.assertTrue(field.evaluate("el => document.activeElement === el"))
        # Invoking the action without moving focus exercises moving the shared composer out of the deleted desk.
        page.locator("#ai-round-cleanup").evaluate("button => button.click()")
        page.wait_for_function("() => document.querySelectorAll('#ai-rounds .ai-round').length === 0")
        self.assertTrue(field.is_visible())
        self.assertEqual(field.input_value(), "保留我正在写的下一轮要求")
        self.assertTrue(field.evaluate("el => document.activeElement === el"))
        self.assertEqual(field.evaluate("el => [el.selectionStart, el.selectionEnd]"), [3, 7])
        page.evaluate("() => window.CaseAI.route(true)")
        self.assertEqual(page.locator("#ai-rounds .ai-round").count(), 0)
        self.assertEqual(field.input_value(), "保留我正在写的下一轮要求")
        self.assertEqual(len(posts), 1)
        page.locator("#ai-round-cleanup-undo").click()
        page.wait_for_function("() => document.querySelectorAll('#ai-rounds .ai-round').length === 6")
        self.assertEqual(field.input_value(), "保留我正在写的下一轮要求")

    def test_cleanup_is_disabled_while_the_task_is_stopping(self):
        task = self.task()
        task.update(status="cancelled", busy=True, stopping=True)
        task["round_cleanup"]["available"] = False
        page, _, posts = self.open_task(task)
        self.assertTrue(page.locator("#ai-round-cleanup").is_disabled())
        self.assertEqual(posts, [])

    def test_legacy_task_without_verified_cleanup_metadata_stays_readable(self):
        task = self.task(all_unused=True)
        task.pop("round_cleanup")
        for row in task["rounds"]:
            row.pop("cleanup")
        page, _, posts = self.open_task(task)
        self.assertEqual(page.locator("#ai-unused-rounds .ai-round").count(), 6)
        self.assertTrue(page.locator('[data-round-id="round-6"] .ai-variant-card').is_visible())
        self.assertTrue(page.locator("#ai-round-cleanup").is_disabled())
        self.assertEqual(posts, [])


if __name__ == "__main__":
    unittest.main()
