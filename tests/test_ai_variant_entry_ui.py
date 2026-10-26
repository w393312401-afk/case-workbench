"""Direction writing entry scopes; isolated library and mocked AI submissions only."""
from __future__ import annotations

import copy
import unittest

import test_ai_selection_ui as browser
import test_server as fixture


TASK_ID = "AIT-0123456789ab"
DIRECTION = {
    "direction_id": "202H-S1", "product": "202H", "main_difference": "固定角度", "status": "draft",
    "variants": [
        {"progression": None, "concern": "基准顾虑", "closing": "基准收口", "baseline": True},
        {"progression": "冲突结构", "concern": "冲突顾虑", "conflict": True},
        {"progression": "逐件对照", "concern": "逐件顾虑", "closing": "逐件收口"},
    ],
}


class AIVariantEntryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.daemon_threads = False

    def open_page(self):
        context = self.browser.new_context(viewport={"width": 1080, "height": 850})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(7000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))
        self.posts = []
        self.fail_submission = False
        self.current_direction = copy.deepcopy(DIRECTION)
        self.task = {
            "task_id": TASK_ID, "revision": "fixture-revision", "status": "completed", "busy": False,
            "product": "202H", "action": "variants", "context": {"title": "模拟单变体写稿"}, "rounds": [],
        }

        def ai_api(route):
            request = route.request
            path = request.url.split("/api/ai", 1)[1].split("?", 1)[0]
            if path == "/status":
                route.fulfill(json={"available": True, "label": "隔离测试助手"})
            elif path == "/options":
                route.fulfill(json={"models": [], "skills": [], "default_settings": {}})
            elif path == "/tasks" and request.method == "POST":
                self.posts.append(request.post_data_json)
                if self.fail_submission:
                    route.fulfill(status=500, json={"error": "模拟返回结果丢失"})
                else:
                    route.fulfill(json={"task": self.task})
            elif path == "/tasks":
                route.fulfill(json={"tasks": []})
            elif path == f"/tasks/{TASK_ID}":
                route.fulfill(json={"task": self.task})
            else:
                route.fulfill(status=404, json={"error": "Unexpected mocked AI endpoint"})

        page.route("**/api/ai/**", ai_api)
        page.route("**/api/direction-catalog?**", lambda route: route.fulfill(json={
            "directions": [self.current_direction] if self.current_direction else []}))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/ai")
        page.wait_for_function("() => !!window.CaseAI && !!state.token")
        return page

    def open_entry(self, page, *, index=None, plan=None):
        direction = copy.deepcopy(DIRECTION)
        if plan:
            direction.update(plan_id=plan, plan_note=f"按测试卡 {plan} 写稿")
        if index is None:
            return page.evaluate("direction => CaseAI.startFromDirection(direction)", direction)
        return page.evaluate("({direction,index}) => CaseAI.startFromDirection(direction,{variantIndex:index})",
                             {"direction": direction, "index": index})

    def selected(self, page):
        return page.locator("[data-variant-index]:checked").evaluate_all(
            "boxes => boxes.map(box => Number(box.dataset.variantIndex))")

    def submit_failed(self, page):
        self.fail_submission = True
        page.locator("#submit-ai-create").click()
        page.locator("#submit-ai-create").get_by_text("重试同一请求", exact=True).wait_for()

    def test_manual_entry_is_available_in_empty_list_and_blank_writing(self):
        page = self.open_page()
        page.evaluate("() => {window.manualEntryCalls = 0; window.CaseScripts.openManual = () => {window.manualEntryCalls += 1;};}")
        page.locator('[data-ai-start="manual"]').click()
        self.assertEqual(page.evaluate("window.manualEntryCalls"), 1)
        page.locator("#ai-start-blank").click()
        page.locator("#ai-create-dialog").wait_for(state="visible")
        page.locator("#ai-create-manual").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(page.evaluate("window.manualEntryCalls"), 2)
        self.assertEqual(self.posts, [])

    def test_single_variant_entry_submits_only_the_requested_original_index(self):
        page = self.open_page()
        self.assertTrue(self.open_entry(page, index=2))
        self.assertEqual(self.selected(page), [2])
        self.assertIn("变体 3", page.locator("#ai-create-case").inner_text())
        self.assertIn("将按 1 条变体各写一稿", page.locator("#ai-create-hint").inner_text())
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["variants"], [{"index": 2, "progression": "逐件对照"}])
        self.assertEqual(self.posts[0]["new_variant_count"], 0)
        self.assertEqual(self.posts[0]["direction_id"], DIRECTION["direction_id"])

    def test_direction_entry_keeps_all_available_variants_selected(self):
        page = self.open_page()
        self.assertTrue(self.open_entry(page))
        self.assertEqual(self.selected(page), [0, 2])
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[0]["variants"], [{"index": 0, "progression": None}, {"index": 2, "progression": "逐件对照"}])

    def test_plan_entry_keeps_its_request_and_no_default_variant_selection(self):
        page = self.open_page()
        self.assertTrue(self.open_entry(page, plan="PLAN-first"))
        self.assertEqual(self.selected(page), [])
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "按测试卡 PLAN-first 写稿")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[0]["plan_id"], "PLAN-first")
        self.assertNotIn("variants", self.posts[0])

    def test_case_entry_offers_diverge_mode_with_an_optional_request(self):
        page = self.open_page()
        self.assertTrue(page.evaluate("id => CaseAI.startWriting(id)", self.identifier))
        page.locator(".ai-source-mode").wait_for()
        self.assertTrue(page.locator('[name="ai-source-mode"][value="adapt"]').is_checked())
        self.assertTrue(page.locator("#submit-ai-create").is_disabled())
        page.locator('[name="ai-source-mode"][value="diverge"]').check()
        page.locator("#ai-diverge-count").fill("4")
        self.assertIn("发散 4 条", page.locator("#ai-create-hint").inner_text())
        self.assertEqual(page.locator("#ai-create-instruction-label").inner_text(), "补充要求（选填）")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[0]["writing_mode"], {"mode": "diverge", "count": 4})
        self.assertEqual(self.posts[0]["case_id"], self.identifier)
        self.assertNotIn("variants", self.posts[0])

    def test_case_entry_default_mode_sends_no_writing_mode(self):
        page = self.open_page()
        self.assertTrue(page.evaluate("id => CaseAI.startWriting(id)", self.identifier))
        page.locator("#ai-create-instruction").fill("写 3 条")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertNotIn("writing_mode", self.posts[0])

    def test_unknown_results_remain_scoped_and_retry_the_exact_original_payload(self):
        page = self.open_page()
        self.open_entry(page, index=2)
        page.locator("#ai-create-instruction").fill("只给第三条写一个版本")
        self.submit_failed(page)
        original = copy.deepcopy(self.posts[0])
        page.locator("#cancel-ai-create").click()

        self.open_entry(page, index=0)
        self.assertEqual(self.selected(page), [0])
        self.assertEqual(page.locator("#submit-ai-create").inner_text(), "开始协作")
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "")
        self.submit_failed(page)
        self.assertNotEqual(self.posts[1]["request_id"], original["request_id"])
        page.locator("#cancel-ai-create").click()

        self.open_entry(page)
        self.assertEqual(self.selected(page), [0, 2])
        self.assertEqual(page.locator("#submit-ai-create").inner_text(), "开始协作")
        page.locator("#cancel-ai-create").click()

        self.open_entry(page, plan="PLAN-first")
        self.assertEqual(self.selected(page), [])
        self.assertEqual(page.locator("#submit-ai-create").inner_text(), "开始协作")
        page.locator("#cancel-ai-create").click()

        self.open_entry(page, index=2)
        self.assertEqual(self.selected(page), [2])
        self.assertTrue(page.locator('[data-variant-index="2"]').is_disabled())
        self.assertTrue(page.locator("#ai-create-instruction").get_attribute("readonly") is not None)
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), original["instruction"])
        self.assertIn("此入口上次提交", page.locator("#ai-create-error").inner_text())
        self.fail_submission = False
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[2], original)

    def test_different_plans_keep_independent_unknown_submissions(self):
        page = self.open_page()
        self.open_entry(page, plan="PLAN-first")
        self.submit_failed(page)
        original = copy.deepcopy(self.posts[0])
        page.locator("#cancel-ai-create").click()
        self.open_entry(page, plan="PLAN-second")
        self.assertEqual(page.locator("#submit-ai-create").inner_text(), "开始协作")
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), "按测试卡 PLAN-second 写稿")
        page.locator("#cancel-ai-create").click()
        self.open_entry(page, plan="PLAN-first")
        self.fail_submission = False
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[1], original)

    def test_invalid_or_conflicting_variant_does_not_open_or_submit(self):
        page = self.open_page()
        for index in (-1, 1, 3, "2"):
            with self.subTest(index=index):
                self.assertFalse(self.open_entry(page, index=index))
                self.assertFalse(page.locator("#ai-create-dialog").evaluate("dialog => dialog.open"))
        self.assertEqual(self.posts, [])

    def test_changed_variant_after_catalog_refresh_requires_reselection(self):
        page = self.open_page()
        self.current_direction["variants"][2]["concern"] = "另一条最新顾虑"
        self.open_entry(page, index=2)
        self.assertTrue(page.locator("#submit-ai-create").is_disabled())
        self.assertIn("这条变体已更新", page.locator("#ai-create-error").inner_text())
        self.assertEqual(self.posts, [])

    def test_shifted_catalog_position_does_not_silently_select_a_different_variant(self):
        page = self.open_page()
        variants = self.current_direction["variants"]
        variants[0], variants[2] = variants[2], variants[0]
        self.open_entry(page, index=2)
        self.assertTrue(page.locator("#submit-ai-create").is_disabled())
        self.assertIn("这条变体已更新", page.locator("#ai-create-error").inner_text())
        self.assertEqual(self.posts, [])

    def test_retry_survives_removed_variant_and_unavailable_live_basket(self):
        page = self.open_page()
        self.open_entry(page, index=2)
        self.submit_failed(page)
        original = copy.deepcopy(self.posts[0])
        self.assertEqual(original["instruction"], "")
        page.locator("#cancel-ai-create").click()
        updated = copy.deepcopy(DIRECTION)
        updated["variants"] = updated["variants"][:2]
        self.current_direction = None
        page.route("**/api/materials?**", lambda route: route.fulfill(status=500, json={"error": "模拟篮暂不可读"}))
        self.assertTrue(page.evaluate("direction => CaseAI.startFromDirection(direction,{variantIndex:2})", updated))
        self.assertEqual(page.locator("#submit-ai-create").inner_text(), "重试同一请求")
        self.assertTrue(page.locator("#submit-ai-create").is_enabled())
        self.fail_submission = False
        page.locator("#submit-ai-create").click()
        page.locator("#ai-create-dialog").wait_for(state="hidden")
        self.assertEqual(self.posts[1], original)


if __name__ == "__main__":
    unittest.main()
