"""Independent directions UI, using only a temporary product library."""
import copy
import json
import unittest
from uuid import uuid4
from unittest.mock import patch

import test_server as fixture
import test_ui_regressions as browser


class DirectionCatalogBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.point_s = self.create_point("S1", "定角结构让整段推磨保持角度")
        self.point_a = self.create_point("A8", "一次准备，两面继续")
        self.point_b = self.create_point("B9", "检查接触后再决定")
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["test_direction"] = {"direction_id": "202H-S1", "main_difference": "S1 定角不用猜",
                                     "concern": "原顾虑 → 实际接触", "closing": "原收口", "variant_type": "direction"}
        current["production"] = {"script_version": "V2"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "临时方向记录", "current": current})
        self.catalog()

    def catalog(self):
        status, _, data = self.request("GET", "/api/direction-catalog?product=202H")
        self.assertEqual(status, 200, data)
        return data

    def create_point(self, code, title):
        status, _, data = self.request("POST", "/api/selling-points", {
            "revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "point": {"product": "202H", "code": code, "tier": code[0], "title": title,
                      "rationale": "隔离测试分级依据", "evidence": "隔离测试证据，不涉及真实产品判断"},
        })
        self.assertEqual(status, 200, data)
        return next(point for point in self.catalog()["selling_points"] if point["code"] == code)

    def open_page(self, route="#/202H/directions", width=1280):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{route}")
        page.locator("#direction-filters" if "/directions/" not in route else "#direction-definition").wait_for()
        return page

    def test_create_zero_case_direction_by_clicking_chain_and_edit_default_without_changing_cases(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page("#/202H/directions?tab=points", width=390)
        page.locator("#new-selling-point").click()
        page.locator('#selling-point-dialog [name="code"]').fill("B10")
        page.locator('#selling-point-dialog [name="title"]').fill("保留明确使用边界")
        page.locator('#selling-point-dialog [name="evidence"]').fill("隔离测试出处")
        page.get_by_role("button", name="保存卖点", exact=True).click()
        page.locator("#selling-point-dialog").wait_for(state="hidden")
        page.locator(".dir-list").get_by_text("保留明确使用边界", exact=True).wait_for()
        page.locator('[data-tier-filter="A"]').click()
        page.wait_for_function("()=>document.querySelectorAll('[data-point-id]').length===1")
        self.assertIn("A8", page.locator(".dir-list").inner_text())
        page.locator("[data-use-point]").click()
        page.locator("#direction-create-dialog[open]").wait_for()
        self.assertEqual(page.locator("#direction-generated-id").inner_text(), "202H-A8")
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator("#direction-create-dialog [role=alert]").get_by_text("不是 S 级卖点", exact=False).wait_for()
        page.locator('#direction-create-dialog [name="lead_reason"]').fill("隔离测试：只测准备环节")
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator('#direction-create-dialog [name="concern"]').fill("准备麻烦吗")
        page.locator('#direction-create-dialog [data-chain-point]').filter(has_text="B9").click()
        self.assertEqual(page.locator('[name="concern"]').input_value(), "准备麻烦吗 → B9 检查接触后再决定")
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator('#direction-create-dialog [data-chain-point]').filter(has_text="B9").click()
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        page.get_by_role("button", name="保存方向", exact=True).click()
        page.locator("#direction-definition").wait_for()
        self.assertIn("/202H/directions/202H-A8", page.url)
        page.locator("#direction-cases").click()  # related records are tabs; cases is not the first one
        page.locator("#direction-cases-panel").get_by_text("方向已经独立保存", exact=False).wait_for()
        page.locator("#direction-edit").click()
        page.locator('#direction-edit-dialog [name="concern"]').fill("更新后的默认顾虑")
        page.locator('#direction-edit-dialog [name="status"]').select_option("paused")
        page.get_by_role("button", name="保存定义", exact=True).click()
        page.locator("#direction-edit-dialog").wait_for(state="hidden")
        page.locator('#direction-definition > .direction-chain').get_by_text("更新后的默认顾虑", exact=False).wait_for()
        saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-A8")
        self.assertEqual(saved["status"], "paused")
        self.assertTrue(any(item["concern"].startswith("准备麻烦吗") for item in saved["variants"]))
        with patch.object(self.server.app.performance, "video_brief", return_value={"7664986913497009499": {"product": "202H"}}):
            status, _, linked = self.request("POST", "/api/video-directions", {
                "revision": self.catalog()["revision"], "request_id": uuid4().hex,
                "product": "202H", "video_id": "7664986913497009499", "direction_id": "202H-A8",
            })
        self.assertEqual(status, 200, linked)
        page.locator(".back-link").click()
        card = page.locator('[data-direction-id="202H-A8"]')
        card.wait_for()
        self.assertIn("1 条视频", card.locator(".dir-sub").inner_text())
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_existing_direction_keeps_short_main_and_old_execution_while_draft_guard_preserves_input(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page("#/202H/directions/202H-S1")
        page.locator("#direction-add-variant").click()
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator('[name="concern"]').fill("新的执行顾虑")
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator("#direction-create-dialog [role=alert]").get_by_text("请选择推进结构", exact=False).wait_for()
        page.locator('[name="progression"]').select_option("结果倒叙")
        page.locator('[name="execution_change"]').fill("先给结果再倒回原因")
        page.evaluate("location.hash='#/202H/library'")
        page.locator("#direction-discard-dialog[open]").wait_for()
        page.get_by_role("button", name="继续编辑", exact=True).click()
        self.assertEqual(page.locator('[name="concern"]').input_value(), "新的执行顾虑")
        self.assertIn("/202H/directions/202H-S1", page.url)
        page.get_by_role("button", name="下一步", exact=True).click()
        page.locator('[name="closing"]').fill("新的执行收口")
        page.get_by_role("button", name="保存方向", exact=True).click()
        page.locator("#direction-create-dialog").wait_for(state="hidden")
        page.locator(".direction-variant-list").get_by_text("新的执行顾虑", exact=False).wait_for()
        saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")
        self.assertEqual(saved["main_difference"], "S1 定角不用猜")
        self.assertEqual(saved["concern"], "原顾虑 → 实际接触")
        self.assertTrue(any(item["concern"] == "新的执行顾虑" for item in saved["variants"]))
        self.assertEqual(self.store.read_case(self.identifier), before)
        page.locator("#direction-edit").click()
        page.locator('#direction-edit-dialog [name="notes"]').fill("不保存的草稿")
        page.keyboard.press("Escape")
        page.get_by_role("button", name="放弃修改", exact=True).click()
        self.assertEqual(next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")["notes"], "")


if __name__ == "__main__":
    unittest.main()
