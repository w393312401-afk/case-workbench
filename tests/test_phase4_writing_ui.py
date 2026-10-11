"""Writing lifecycle browser checks; all state and AI outputs are temporary fixtures."""
from __future__ import annotations

import copy
import time
import unittest
from uuid import uuid4

import test_server as fixture
import test_ai_selection_ui as ai_fixture


class WritingLifecycleBrowserTests(unittest.TestCase):
    setUpClass = classmethod(ai_fixture.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ai_fixture.AISelectionBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    create_task = ai_fixture.AISelectionBrowserTests.create_task

    def setUp(self):
        fixture.WorkbenchHTTPTests.setUp(self)
        self.server.app.ai.bridge = ai_fixture.FakeBridge()
        self.server.daemon_threads = False

    def open_page(self, route="#/202H/scripts", width=1280):
        context = self.browser.new_context(viewport={"width": width, "height": 900})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(12000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{route}")
        page.locator("#connection-text").get_by_text("已连接本地案例库").wait_for(state="attached")
        return page

    def catalog(self):
        status, _, data = self.request("GET", "/api/creation?product=202H")
        self.assertEqual(status, 200, data)
        return data

    def create_script(self, text="Independent adopted script."):
        status, _, data = self.request("POST", "/api/scripts", {
            "revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "script": {"title": "临时脚本", "product": "202H", "text": text,
                       "translation_zh": "独立采用的正文。", "confirmed": True},
        })
        self.assertEqual(status, 200, data)
        return data["script"]

    def test_manual_script_derivation_release_and_case_conclusion_preserve_old_version(self):
        page = self.open_page(width=390)
        page.locator("#add-top").click()  # the top bar "保存脚本" opens the manual editor
        dialog = page.locator("#script-editor-dialog")
        dialog.locator('[name="title"]').fill("一个明确角度")
        dialog.locator('[name="text"]').fill("Keep the same angle across each pass.")
        dialog.locator('[name="translation_zh"]').fill("每一遍都保持同一角度。")
        dialog.get_by_role("button", name="保存脚本版本", exact=True).click()
        page.locator("#script-production-toggle").wait_for()
        first = self.catalog()["scripts"][0]
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-derive").click()
        dialog.locator('[name="text"]').fill("Set your angle once. Keep it steady.")
        dialog.get_by_role("button", name="保存脚本版本", exact=True).click()
        page.locator(".script-body").get_by_text("Set your angle once. Keep it steady.", exact=True).wait_for()
        self.assertIn("Set your angle once", page.locator(".script-body").inner_text())
        versions = self.catalog()["scripts"]
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[0]["text"], first["text"])
        self.assertEqual(versions[1]["parent_id"], first["script_id"])
        page.locator("#script-release").click()
        dialog.locator('[name="status"]').select_option("published")
        dialog.locator('[name="video_id"]').fill("7664986913497009441")
        dialog.locator('[name="published_on"]').fill("2026-10-08")
        dialog.get_by_role("button", name="保存进度", exact=True).click()
        page.locator(".script-facts").get_by_text("7664986913497009441", exact=True).wait_for()
        self.assertIn("7664986913497009441", page.locator(".script-facts").inner_text())
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-case").click()
        dialog.locator('[name="conclusion"]').fill("固定角度的演示易于理解，表现数据待观察。")
        dialog.get_by_role("button", name="创建案例", exact=True).click()
        page.wait_for_url("**/case/**")
        linked = self.catalog()["scripts"][1]
        saved = self.request("GET", f"/api/cases/{linked['case_id']}")[2]
        self.assertEqual(saved["current"]["transcript"]["text"], "Set your angle once. Keep it steady.")
        self.assertIn("固定角度的演示", saved["current"]["conclusion"])
        self.assertEqual(saved["current"]["production"]["script_version"], linked["script_id"])

    def test_completed_candidate_requires_single_text_and_writeback_conflict_preserves_input(self):
        task = self.create_task("请只处理临时案例。")
        before = self.store.read_case(self.identifier)
        page = self.open_page(f"#/202H/ai/{task['task_id']}")
        page.locator(".ai-candidate-actions .ai-menu summary").first.click()
        page.get_by_role("menuitem", name="写回案例", exact=True).click()
        dialog = page.locator("#script-editor-dialog")
        self.assertEqual(dialog.locator('[name="text"]').input_value(), "")
        self.assertIn("临时候选结果", dialog.locator(".script-candidate-source").inner_text())
        dialog.locator('[name="text"]').fill("One chosen script, without the review notes.")
        dialog.locator('[name="translation_zh"]').fill("仅采用一条正文，不含审查说明。")
        dialog.get_by_role("button", name="核对并继续写回", exact=True).click()
        dialog.locator('[name="conclusion"]').wait_for()
        self.assertEqual(self.store.read_case(self.identifier), before)
        self.assertEqual(dialog.locator('[name="destination"]').input_value(), "new")
        dialog.get_by_role("link", name="脚本详情登记同一视频", exact=True).click()
        page.locator("#script-release").click()
        dialog.locator('[name="video_id"]').fill(before["current"]["metrics"]["video_id"])
        dialog.get_by_role("button", name="保存进度", exact=True).click()
        page.locator("#script-more-actions > summary").click()
        page.locator("#script-write-source").click()
        dialog.locator('[name="conclusion"]').fill("这一版先观察开头是否清楚。")
        changed = copy.deepcopy(before["current"])
        changed["transcript"]["text"] = "Concurrent case text, preserved in history."
        self.store.append(self.identifier, {"kind": "editorial", "text": "并行改稿", "current": changed})
        dialog.get_by_role("button", name="保存为案例修订", exact=True).click()
        dialog.locator('[data-error]').get_by_text("本次未写回", exact=False).wait_for()
        self.assertEqual(dialog.locator('[name="conclusion"]').input_value(), "这一版先观察开头是否清楚。")
        self.assertIn("Concurrent case text", dialog.locator('[data-original]').inner_text())
        self.assertEqual(self.store.read_case(self.identifier)["current"]["transcript"]["text"], changed["transcript"]["text"])
        dialog.get_by_role("button", name="保存为案例修订", exact=True).click()
        page.wait_for_url("**/case/**")
        self.assertEqual(self.store.read_case(self.identifier)["current"]["transcript"]["text"], "One chosen script, without the review notes.")
        script = self.catalog()["scripts"][0]
        self.assertEqual(script["source"]["task_id"], task["task_id"])
        self.assertEqual(script["case_id"], self.identifier)

    def test_unknown_save_response_retries_same_request_without_duplicate_script(self):
        page = self.open_page()
        page.locator("#add-top").click()  # the top bar "保存脚本" opens the manual editor
        dialog = page.locator("#script-editor-dialog")
        dialog.locator('[name="title"]').fill("保留请求标识")
        dialog.locator('[name="text"]').fill("One saved version, even after a lost response.")
        page.evaluate("""() => {
          const original = window.fetch.bind(window); window.testScriptCalls = [];
          window.fetch = async (path, options) => {
            const response = await original(path, options);
            if (path === '/api/scripts' && options?.method === 'POST') {
              window.testScriptCalls.push(JSON.parse(options.body));
              if (window.testScriptCalls.length === 1) {
                await response.clone().text(); throw new Error('fixture: lost response after durable save');
              }
            }
            return response;
          };
        }""")
        dialog.get_by_role("button", name="保存脚本版本", exact=True).click()
        dialog.get_by_role("button", name="重试同一请求", exact=True).wait_for()
        self.assertEqual(len(self.catalog()["scripts"]), 1)
        dialog.get_by_role("button", name="重试同一请求", exact=True).click()
        page.locator("#script-production-toggle").wait_for()
        self.assertEqual(len(self.catalog()["scripts"]), 1)
        calls = page.evaluate("window.testScriptCalls")
        self.assertEqual(calls[0], calls[1])

    def test_blank_task_needs_explicit_submission_and_avoids_undefined_case_link(self):
        page = self.open_page("#/202H/ai")
        page.locator("#ai-start-blank").click()
        page.locator("#ai-create-dialog[open]").wait_for()
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        page.locator("#ai-create-instruction").fill("围绕固定角度起草一条短稿。")
        page.locator("#ai-create-advanced").evaluate("details => { details.open = true; }")  # model and skills are folded
        page.locator("#ai-create-skills").select_option("none")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-rounds").wait_for()
        page.locator("[data-adopt-script]").wait_for()
        self.assertEqual(page.locator('a[href*="case/undefined"]').count(), 0)
        task = self.request("GET", "/api/ai/tasks")[2]["tasks"][0]
        self.assertFalse(task.get("case_id"))
        self.assertEqual(task["context"]["kind"], "blank")

    def test_script_draft_survives_cancelled_navigation(self):
        page = self.open_page()
        page.locator("#add-top").click()  # the top bar "保存脚本" opens the manual editor
        page.locator('#script-editor-dialog [name="text"]').fill("Keep this draft until I choose.")
        page.evaluate("location.hash='#/202H/library'")
        page.locator("#script-discard-dialog[open]").wait_for()
        page.get_by_role("button", name="继续编辑", exact=True).click()
        self.assertEqual(page.locator('#script-editor-dialog [name="text"]').input_value(), "Keep this draft until I choose.")
        self.assertIn("/202H/scripts", page.url)
        self.assertEqual(self.catalog()["scripts"], [])

    def test_direction_plan_prefills_request_and_keeps_basket_opt_in_explicit(self):
        directory = self.request("GET", "/api/direction-catalog?product=202H")[2]
        status, _, point = self.request("POST", "/api/selling-points", {
            "revision": directory["revision"], "request_id": uuid4().hex,
            "point": {"product": "202H", "code": "S9", "tier": "S", "title": "临时固定角度", "evidence": "隔离测试"},
        })
        self.assertEqual(status, 200, point)
        directory = self.request("GET", "/api/direction-catalog?product=202H")[2]
        status, _, direction_result = self.request("POST", "/api/direction-catalog", {
            "revision": directory["revision"], "request_id": uuid4().hex,
            "direction": {"product": "202H", "direction_id": "202H-S9", "selling_point_id": directory["selling_points"][0]["id"],
                          "concern": "测试顾虑", "closing": "测试收口"},
        })
        self.assertEqual(status, 200, direction_result)
        direction = direction_result["direction"]
        status, _, plan_result = self.request("POST", "/api/plans", {
            "revision": self.catalog()["revision"], "request_id": uuid4().hex,
            "plan": {"product": "202H", "direction_id": "202H-S9", "action": "execution", "note": "从同一方向补一条执行稿"},
        })
        self.assertEqual(status, 200, plan_result)
        plan = plan_result["plan"]
        page = self.open_page("#/202H/ai")
        page.evaluate("direction => window.CaseAI.startFromDirection(direction)", {**direction, "plan_id": plan["id"], "plan_note": plan["note"]})
        self.assertEqual(page.locator("#ai-create-instruction").input_value(), plan["note"])
        self.assertTrue(page.locator("#ai-include-basket").is_checked())
        self.assertEqual(self.request("GET", "/api/ai/tasks")[2]["tasks"], [])
        page.locator("#ai-create-advanced").evaluate("details => { details.open = true; }")  # model and skills are folded
        page.locator("#ai-create-skills").select_option("none")
        page.locator("#submit-ai-create").click()
        page.locator("#ai-rounds").wait_for()
        saved_plan = self.catalog()["plans"][0]
        self.assertEqual(saved_plan["status"], "working")
        self.assertTrue(saved_plan["task_id"])

    def test_direction_writing_ticks_recorded_variants_and_request_is_optional(self):
        directory = self.request("GET", "/api/direction-catalog?product=202H")[2]
        status, _, point = self.request("POST", "/api/selling-points", {
            "revision": directory["revision"], "request_id": uuid4().hex,
            "point": {"product": "202H", "code": "S8", "tier": "S", "title": "临时整套", "evidence": "隔离测试"},
        })
        self.assertEqual(status, 200, point)
        for extra in ({"concern": "基准顾虑", "closing": "基准收口"},
                      {"progression": "逐件对照", "concern": "逐件顾虑", "closing": "逐件收口", "execution_change": "逐件替换"}):
            directory = self.request("GET", "/api/direction-catalog?product=202H")[2]
            status, _, result = self.request("POST", "/api/direction-catalog", {
                "revision": directory["revision"], "request_id": uuid4().hex,
                "direction": {"product": "202H", "direction_id": "202H-S8", "selling_point_id": point["point"]["id"], **extra}})
            self.assertEqual(status, 200, result)
        direction = result["direction"]
        page = self.open_page("#/202H/ai")
        page.evaluate("direction => window.CaseAI.startFromDirection(direction)", direction)
        boxes = page.locator("[data-variant-index]")
        boxes.first.wait_for()
        self.assertEqual(boxes.count(), 2)
        self.assertTrue(all(boxes.nth(index).is_checked() for index in range(2)))
        self.assertEqual(page.locator("#ai-create-instruction-label").inner_text(), "补充要求（选填）")
        page.locator("#ai-create-advanced").evaluate("details => { details.open = true; }")  # model and skills are folded
        page.locator("#ai-create-skills").select_option("none")
        submit = page.locator("#submit-ai-create")
        self.assertTrue(submit.is_enabled())
        boxes.nth(0).uncheck()
        boxes.nth(1).uncheck()
        self.assertTrue(submit.is_disabled())
        self.assertEqual(page.locator("#ai-create-instruction-label").inner_text(), "这次希望怎么做？")
        page.locator('[data-variant-index="1"]').check()
        self.assertFalse(page.locator("#ai-carousel").is_checked())
        page.locator("#ai-carousel").check()
        self.assertIn("将按 1 条变体各写一稿，每条附图文版", page.locator("#ai-create-hint").inner_text())
        submit.click()
        page.locator("#ai-rounds").wait_for()
        task = self.request("GET", "/api/ai/tasks")[2]["tasks"][0]
        detail = self.request("GET", "/api/ai/tasks/" + task["task_id"])[2]["task"]
        self.assertEqual([item["number"] for item in detail["context"]["writing_plan"]["variants"]], [2])
        self.assertEqual(detail["rounds"][0]["instruction"], "按所选 1 条已登记变体（#2）各写一条，每条附图文版。")
        self.assertTrue(detail["context"]["carousel"])


if __name__ == "__main__":
    unittest.main()
