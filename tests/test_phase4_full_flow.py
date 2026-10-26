"""The complete direction-to-case workflow, with a fake AI and disposable records."""
from __future__ import annotations

import json
import re

import unittest
from uuid import uuid4

import test_server as fixture
import test_ai_selection_ui as ai_fixture



def skip_skills(page):
    """The skill picker is gone from the UI; the isolated test store has no skill files, so submit with skills off."""
    def handle(route):
        request = route.request
        if request.method != "POST":
            return route.continue_()
        body = json.loads(request.post_data or "{}")
        body["skill_selection"] = "none"
        route.continue_(post_data=json.dumps(body))
    page.route(re.compile(r"/api/ai/tasks$"), handle)

class PhaseFourFullFlowTests(unittest.TestCase):
    setUpClass = classmethod(ai_fixture.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ai_fixture.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.app.ai.bridge = ai_fixture.FakeBridge()
        self.server.daemon_threads = False

    def test_selling_point_to_direction_to_candidate_to_published_case_without_leaving_workbench(self):
        original = self.store.read_case(self.identifier)
        catalog = self.request("GET", "/api/direction-catalog?product=202H")[2]
        status, _, created = self.request("POST", "/api/selling-points", {
            "revision": catalog["revision"], "request_id": uuid4().hex,
            "point": {"product": "202H", "code": "S1", "tier": "S", "title": "定角不用猜",
                      "rationale": "临时演示资料", "evidence": "仅用于测试"},
        })
        self.assertEqual(status, 200, created)
        context = self.browser.new_context(viewport={"width": 1280, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(12000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        base = f"http://127.0.0.1:{self.server.server_port}/"
        page.goto(base + "#/202H/directions?tab=points")
        page.locator("[data-use-point]").first.click()
        wizard = page.locator("#direction-create-dialog")
        wizard.get_by_role("button", name="下一步", exact=True).click()
        wizard.locator('[name="concern"]').fill("新手如何控制角度 → 夹持与导杆")
        wizard.get_by_role("button", name="下一步", exact=True).click()
        wizard.locator('[name="closing"]').fill("按自己的刀具需要选择")
        wizard.get_by_role("button", name="保存方向", exact=True).click()
        page.wait_for_url("**/directions/202H-S1")
        page.locator("#direction-write").click()
        page.locator("#ai-create-instruction").fill("为临时方向生成一条测试稿。")
        skip_skills(page)
        page.locator("#submit-ai-create").click()
        page.locator(".ai-candidate-actions .ai-menu summary").first.click()
        page.get_by_role("menuitem", name="保存采用稿", exact=True).click()
        dialog = page.locator("#script-editor-dialog")
        dialog.locator('[name="title"]').fill("独立方向的第一条脚本")
        dialog.locator('[name="text"]').fill("Set the angle. Follow the guide.")
        dialog.locator('[name="translation_zh"]').fill("先设角度，再跟着导杆磨。")
        dialog.get_by_role("button", name="保存采用稿", exact=True).click()
        page.locator("#script-release").click()
        dialog.locator('[name="status"]').select_option("published")
        dialog.locator('[name="video_id"]').fill("7664986913497009442")
        dialog.locator('[name="published_on"]').fill("2026-10-08")
        dialog.get_by_role("button", name="保存进度", exact=True).click()
        page.locator(".script-facts").get_by_text("7664986913497009442", exact=True).wait_for()
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-case").click()
        dialog.locator('[name="conclusion"]').fill("方向、脚本与发布已登记，销售数据等待导入后观察。")
        dialog.get_by_role("button", name="创建案例", exact=True).click()
        page.wait_for_url("**/case/**")
        self.assertEqual(self.store.read_case(self.identifier), original)
        creation = self.request("GET", "/api/creation?product=202H")[2]
        self.assertEqual(len(creation["scripts"]), 1)
        script = creation["scripts"][0]
        self.assertEqual(script["direction_id"], "202H-S1")
        case = self.store.read_case(script["case_id"])
        self.assertEqual(case["current"]["transcript"]["text"], script["text"])
        self.assertEqual(case["current"]["test_direction"]["script_version"], script["script_id"])
        self.assertEqual(case["current"]["metrics"]["video_id"], script["video_id"])
        self.assertNotIn("items_sold", case["current"].get("metrics", {}))
        page.goto(base + "#/202H/directions/202H-S1")
        page.locator("#direction-scripts").click()
        self.assertEqual(page.locator("#direction-scripts").get_attribute("aria-selected"), "true")
        page.locator(f'#direction-scripts-panel a[href*="{script["script_id"]}"]').wait_for()
        page.locator("#direction-cases").click()
        self.assertEqual(page.locator("#direction-cases").get_attribute("aria-selected"), "true")
        page.locator(f'#direction-cases-panel a[href*="{script["case_id"]}"]').wait_for()
        page.locator('[data-direction-plan="execution"]').click()
        plan = page.locator("#direction-plan-dialog")
        plan.locator("textarea").fill("下轮只更换开头，保持方向。")
        plan.get_by_role("button", name="保存安排", exact=True).click()
        page.locator("#home-nav").click()
        page.get_by_text("下轮只更换开头，保持方向。", exact=True).wait_for()
        self.assertEqual(len(self.request("GET", "/api/creation?product=202H")[2]["plans"]), 1)
        self.assertEqual(page.url.split("#")[0], base)


if __name__ == "__main__":
    unittest.main()
