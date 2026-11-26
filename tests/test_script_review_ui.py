"""口播审查逐句采纳：候选稿审完在抽屉里勾选改动，存成新一轮。只用临时运行目录和假的 AI。"""
from __future__ import annotations

import os
import time
import unittest

import test_phase4_writing_ui as writing_fixture
import test_server as fixture
from test_script_review import NEW_REPORT, SAMPLE_DRAFT


class ScriptBridge:
    def __init__(self):
        self.report = NEW_REPORT

    def status(self):
        return {"available": True, "label": "临时测试助手"}

    def run(self, prompt, *, cwd, on_progress, cancel_event, **options):
        on_progress("测试任务正在处理")
        return self.report if "sharpal-202h-script-review" in prompt else SAMPLE_DRAFT


class ScriptReviewAdoptBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    create_task = writing_fixture.WritingLifecycleBrowserTests.create_task
    open_page = writing_fixture.WritingLifecycleBrowserTests.open_page

    def setUp(self):
        writing_fixture.WritingLifecycleBrowserTests.setUp(self)
        self.server.app.ai.bridge = ScriptBridge()

    def review(self, body, query):
        status, _, data = self.request("POST", "/api/script-reviews/start", body)
        self.assertEqual(status, 200, data)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            status, _, data = self.request("GET", f"/api/script-reviews?{query}")
            if data.get("latest", {}).get("status") == "done":
                return data
            time.sleep(0.05)
        self.fail("review did not finish")

    def test_ticked_review_lines_become_a_new_candidate_round(self):
        task = self.create_task("写三句", action="variants")
        self.assertIn("202H", str(task.get("product") or task.get("context", {}).get("product")))
        round_id = task["rounds"][0]["round_id"]
        self.review({"kind": "candidate", "task_id": task["task_id"], "round_id": round_id, "variant_index": 0},
                    f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        page = self.open_page(f"#/202H/ai/{task['task_id']}", width=1400)
        card = page.locator(".ai-variant-card").first
        card.locator("[data-desk-review]").click()
        drawer = page.locator("#script-review-drawer")
        adopt = drawer.locator(".sr-adopt")
        adopt.wait_for()
        items = adopt.locator(".sr-adopt-item")
        self.assertEqual(items.count(), 2)
        self.assertIn("第 3 句", items.nth(1).inner_text())
        self.assertIn("It comes with a 4-year warranty. Check out the 202H below.", items.nth(1).locator(".sr-add").inner_text())
        self.assertIn("第 2 句审查没改", adopt.inner_text())
        self.assertIn("采纳 1 句口播、2 条画面", adopt.locator("[data-sr-adopt-summary]").inner_text())
        # Unticking the picture of line 1 leaves only the line-3 change.
        items.nth(0).locator("[data-sr-pick-proof]").uncheck()
        self.assertIn("采纳 1 句口播、1 条画面", adopt.locator("[data-sr-adopt-summary]").inner_text())
        # The Black Friday line is offered beside the safe default.
        items.nth(1).locator('[data-sr-alt][value="1"]').check()
        if os.environ.get("SR_SHOT"):
            items.nth(1).locator('[data-sr-alt][value="0"]').check()
            items.nth(0).locator("[data-sr-pick-proof]").check()
            page.screenshot(path=os.environ["SR_SHOT"])
            items.nth(0).locator("[data-sr-pick-proof]").uncheck()
            items.nth(1).locator('[data-sr-alt][value="1"]').check()
        adopt.locator('[data-sr-adopt]:not([data-sr-adopt="pool"])').click()
        page.wait_for_function("() => !document.querySelector('#script-review-drawer')?.open")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = self.server.app.ai.get(task["task_id"])
            if len(current["rounds"]) == 2:
                break
            time.sleep(0.05)
        newest = current["rounds"][-1]
        self.assertIn("采纳口播审查", newest["instruction"])
        self.assertIn("It's in the Black Friday deal, so check out the 202H below.", newest["output"])
        self.assertIn("| 整箱定格，手势指向购物车 |", newest["output"])
        self.assertNotIn("箱盖掀开只露磨板边缘微距", newest["output"])
        page.locator(f'.ai-round[data-round-id="{newest["round_id"]}"]').wait_for()

    def test_finished_review_opens_from_the_task_panel(self):
        task = self.create_task("写三句", action="variants")
        round_id = task["rounds"][0]["round_id"]
        done = self.review({"kind": "candidate", "task_id": task["task_id"], "round_id": round_id, "variant_index": 0},
                           f"kind=candidate&task_id={task['task_id']}&round_id={round_id}&variant_index=0")
        run_id = "review:" + done["latest"]["id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
                row["run_id"] == run_id and row["status"] == "needs_attention" for row in self.server.app.activity.feed(limit=50)["items"]):
            time.sleep(0.05)
        # Start somewhere else: the panel takes the reader to the draft and opens its review.
        page = self.open_page("#/202H/content", width=1400)
        page.locator("#task-activity-open").click()
        row = page.locator(f"[data-run-id='{run_id}']")
        row.wait_for()
        self.assertIn("口播审查", row.inner_text())
        self.assertEqual(row.locator("[data-activity-result]").get_attribute("title"), "打开审查")
        if os.environ.get("SR_PANEL_SHOT"):
            page.screenshot(path=os.environ["SR_PANEL_SHOT"])
        row.locator("[data-activity-result]").click()
        page.wait_for_function("() => document.querySelector('#script-review-drawer')?.open")
        page.locator("#script-review-drawer .sr-adopt").wait_for()
        self.assertIn(f"/ai/{task['task_id']}", page.evaluate("location.hash"))
        self.assertNotIn("review=", page.evaluate("location.hash"))


if __name__ == "__main__":
    unittest.main()
