"""Candidate production entry points; isolated workbench and mocked AI records."""
from __future__ import annotations

import unittest

import test_ai_selection_ui as browser
import test_server as fixture


TASK_ID = "AIT-0123456789ab"
ROUND_ID = "AIR-0123456789ab"
VOICE_OUTPUT = """## 变体 1：固定角度

**口播（配音用）**
```
Set the angle once. Keep it steady through every pass.
```

**中文对照**
先固定角度，每一遍都保持稳定。

**意群表**
| 口播意群 | 画面方向 |
| --- | --- |
| Set the angle once. | 调整角度 |
| Keep it steady through every pass. | 稳定推进 |
"""


class AIProductionEntryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_candidate(self, output, *, action="variants"):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        round_record = {
            "round_id": ROUND_ID, "status": "completed", "instruction": "临时写稿要求",
            "output": output, "messages": [], "skill_selection": "none",
        }
        task = {
            "task_id": TASK_ID, "revision": "fixture-revision", "status": "completed",
            "busy": False, "product": "202H", "action": action,
            "context": {"title": "临时候选口播"}, "rounds": [round_record],
        }
        posts = []

        def ai_api(route):
            request = route.request
            path = request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if request.method != "GET":
                posts.append(request.url)
                route.fulfill(status=400, json={"error": "Unexpected fixture mutation"})
            elif path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks":
                route.fulfill(json={"tasks": [task]})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": task})
            else:
                route.fulfill(status=404, json={"error": "Unexpected mocked AI endpoint"})

        page.route("**/api/ai/**", ai_api)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai/{TASK_ID}")
        page.locator(".ai-round-output").wait_for()
        page.evaluate("""() => {
          window.productionEntryCalls = [];
          const record = (kind, task, round) => {
            window.productionEntryCalls.push(JSON.parse(JSON.stringify({kind, task, round})));
          };
          window.CaseProduction.fromCandidateCarousel = (task, round) => record('carousel', task, round);
          window.CaseProduction.fromCandidate = (task, round) => record('video', task, round);
          window.CaseScripts.adoptCandidate = (task, round) => record('adopt', task, round);
        }""")
        return page, task, round_record, posts

    def test_candidate_starts_video_production_and_keeps_adoption_in_more_actions(self):
        self.assertNotIn("photo_carousel", VOICE_OUTPUT)
        self.assertNotIn("叠字", VOICE_OUTPUT)
        outputs = {
            "voice_without_carousel": VOICE_OUTPUT,
            "legacy_overlay": VOICE_OUTPUT + "\n旧版叠字：Hold the angle steady.\n",
            "legacy_carousel": VOICE_OUTPUT + '\n旧格式：{"kind":"photo_carousel"}\n',
        }
        for name, output in outputs.items():
            with self.subTest(output=name):
                page, task, round_record, posts = self.open_candidate(output)
                primary = page.locator(".ai-candidate-actions > [data-start-production]")
                self.assertEqual(primary.inner_text(), "开始制作")
                primary.click()
                self.assertEqual(page.evaluate("window.productionEntryCalls"), [
                    {"kind": "video", "task": task, "round": round_record},
                ])
                menu = page.locator(".ai-candidate-actions .ai-menu")
                menu.locator("summary").click()
                self.assertEqual(menu.locator("[data-produce-carousel]").count(), 0)
                self.assertEqual(menu.locator("[data-produce-video]").count(), 0)
                self.assertEqual(menu.locator("[data-adopt-script]").inner_text(), "保存采用稿")
                menu.locator("[data-adopt-script]").click()
                self.assertEqual(page.evaluate("window.productionEntryCalls"), [
                    {"kind": "video", "task": task, "round": round_record},
                    {"kind": "adopt", "task": task, "round": round_record},
                ])
                self.assertEqual(page.locator("#toast .toast-item").count(), 0)
                self.assertEqual(posts, [])
                page.close()

    def test_other_completed_task_types_offer_results_without_writing_production(self):
        for action in ("review", "retrospective"):
            with self.subTest(action=action):
                page, task, round_record, posts = self.open_candidate(VOICE_OUTPUT, action=action)
                self.assertTrue(page.locator(".ai-candidate-actions").is_hidden())
                self.assertEqual(page.locator(".ai-output-title").inner_text(), "审稿结论" if action == "review" else "复盘结论")
                self.assertNotIn("待选稿", page.locator("#ai-management-state").inner_text())
                self.assertEqual(page.evaluate("window.productionEntryCalls"), [])
                self.assertEqual(posts, [])
                page.close()


if __name__ == "__main__":
    unittest.main()
