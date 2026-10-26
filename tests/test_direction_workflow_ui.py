"""Variant workflow UI: delayed data stays honest and shortcuts retain exact context.

All saves go through a disposable HTTP fixture. Progress responses are controlled
to exercise late arrival, failure, and a changed catalog without production reads.
"""
import copy
import unittest
from urllib.parse import parse_qs, urlparse

import test_direction_catalog_ui as catalog_ui
import test_server as fixture
from test_plan_variant_binding import variant_ref


class DirectionWorkflowBrowserTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    detail_pattern = "**/api/direction-catalog/202H-S1?**"
    progress_pattern = "**/api/direction-progress/202H-S1?**"

    def detail_data(self):
        status, _, data = self.request("GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=0")
        self.assertEqual(status, 200, data)
        data.pop("tasks_pending", None)
        data["tasks"] = []
        data["progress_available"] = True
        return data

    def progress_data(self, data, next_action=None):
        variants = []
        for index in range(len(data["direction"]["variants"])):
            variants.append({"index": index, "key": f"fixture-key-{index}",
                "snapshot": variant_ref(data["direction"], index)["snapshot"],
                "records": {"tasks": [], "scripts": [{"id": "SCR-workflow-ui", "title": "此变体已保存的文案",
                    "status": "adopted", "version": 1}], "productions": [], "publications": [], "plans": []},
                "stages": {"writing": {"status": "done", "label": "已存 1 稿", "count": 1},
                           "production": {"status": "empty", "label": "待制作", "count": 0},
                           "publishing": {"status": "empty", "label": "待发布登记", "count": 0},
                           "testing": {"status": "empty", "label": "待建测试卡", "count": 0}},
                "counts": {"tasks": 0, "scripts": 1, "productions": 0, "publications": 0, "plans": 0},
                "next_action": next_action or {"kind": "script", "id": "SCR-workflow-ui", "label": "查看文案并制作"}})
        return {"product": "202H", "direction_id": "202H-S1", "catalog_revision": data["revision"],
                "variants": variants,
                "summary": {"variants_with_scripts": len(variants), "variants_in_production": 0,
                    "variants_published": 0, "variants_tested": 0, "unassigned_count": 1},
                "unassigned": {"tasks": [], "scripts": [{"id": "SCR-unassigned-ui", "title": "待关联的旧文案",
                    "status": "adopted", "reason": "缺少可唯一核对的变体来源"}],
                    "productions": [], "publications": [], "plans": [], "videos": []}}

    def show_detail(self, page, data):
        calls = []

        def respond(route):
            calls.append(route.request.url)
            route.fulfill(status=200, json=copy.deepcopy(data))

        page.route(self.detail_pattern, respond)
        page.evaluate("location.hash='#/202H/directions/202H-S1'")
        page.locator("#direction-definition").wait_for()
        return calls

    def hold_progress(self, page):
        held = []
        page.route(self.progress_pattern, lambda route: held.append(route))
        return held

    def wait_for_held(self, page, held, count=1):
        # Playwright delivers routing events while browser work is awaited.
        for _ in range(100):
            if len(held) >= count:
                break
            page.wait_for_timeout(10)
        self.assertEqual(len(held), count, "Progress should load independently after the core detail")

    def wait_for_progress(self, page):
        page.locator('[data-variant-progress="0"] ol.dir-flow').wait_for()

    def test_delayed_progress_keeps_first_paint_disclosure_and_related_tab(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        core_calls = self.show_detail(page, data)
        self.wait_for_held(page, held)
        self.assertTrue(page.locator("#direction-definition").is_visible())
        self.assertEqual(page.locator("details.dir-variant").count(), len(data["direction"]["variants"]))
        self.assertEqual(page.locator("ol.dir-flow").count(), 0)
        self.assertNotIn("待写稿", page.locator("#direction-progress-summary").inner_text())
        details = page.locator('details.dir-variant[data-variant-index="0"]')
        details.locator(":scope > summary").click()
        page.locator("#direction-cases").click()
        page.evaluate("window.__workflowVariant=document.querySelector('details.dir-variant')")
        held[0].fulfill(status=200, json=self.progress_data(data))
        self.wait_for_progress(page)
        self.assertIsNotNone(details.get_attribute("open"))
        self.assertEqual(page.locator("#direction-cases").get_attribute("aria-selected"), "true")
        self.assertTrue(page.evaluate("window.__workflowVariant===document.querySelector('details.dir-variant')"))
        self.assertEqual(len(core_calls), 1)
        self.assertEqual(parse_qs(urlparse(held[0].request.url).query), {"product": ["202H"]})
        details.locator('[data-variant-records="0"]').get_by_text("此变体已保存的文案", exact=False).wait_for()

    def test_progress_is_visible_when_variant_closed_has_four_stages_and_direct_script_link(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=self.progress_data(data)))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        row = page.locator(".dir-variant-row").first
        details = row.locator("details.dir-variant")
        self.assertIsNone(details.get_attribute("open"))
        flow = row.locator('[data-variant-progress="0"] ol.dir-flow')
        self.assertTrue(flow.is_visible())
        self.assertEqual(flow.locator(":scope > li").count(), 4)
        for label in ("已存 1 稿", "待制作", "待发布登记", "待建测试卡"):
            self.assertIn(label, flow.inner_text())
        self.assertEqual(row.locator('a[data-variant-next="0"]').get_attribute("href"), "#/202H/content/SCR-workflow-ui")
        self.assertFalse(details.locator('[data-variant-records="0"]').is_visible())
        unassigned = page.locator("#direction-unassigned")
        self.assertEqual(unassigned.evaluate("node=>node.tagName"), "DETAILS")
        self.assertIsNone(unassigned.get_attribute("open"))
        self.assertFalse(unassigned.get_by_text("待关联的旧文案", exact=False).is_visible())
        unassigned.locator(":scope > summary").click()
        unassigned.get_by_text("待关联的旧文案", exact=False).wait_for()
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_failure_is_unknown_and_retry_does_not_replace_the_direction(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        core_calls = self.show_detail(page, data)
        self.wait_for_held(page, held)
        page.evaluate("window.__workflowDefinition=document.querySelector('#direction-definition')")
        held[0].fulfill(status=503, json={"error": "隔离测试：进度暂时无法读取"})
        retry = page.locator("#direction-progress-retry")
        retry.wait_for()
        self.assertEqual(page.locator("ol.dir-flow").count(), 0)
        self.assertEqual(page.locator("[data-variant-next]").count(), 0)
        self.assertNotIn("待写稿", page.locator("#direction-progress-summary").inner_text())
        retry.click()
        self.wait_for_held(page, held, 2)
        held[1].fulfill(status=200, json=self.progress_data(data))
        self.wait_for_progress(page)
        self.assertEqual(len(core_calls), 1)
        self.assertTrue(page.evaluate("window.__workflowDefinition===document.querySelector('#direction-definition')"))

    def test_changed_catalog_does_not_attach_progress_to_a_reused_index(self):
        page = self.open_page()
        data = self.detail_data()
        stale = self.progress_data(data)
        stale["catalog_revision"] = "another-catalog-revision"
        stale["variants"][0]["records"]["scripts"][0]["title"] = "不应挂到当前变体的文案"
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=stale))
        self.show_detail(page, data)
        page.locator("#direction-progress-retry").wait_for()
        self.assertEqual(page.locator("ol.dir-flow").count(), 0)
        self.assertEqual(page.locator("[data-variant-next]").count(), 0)
        self.assertNotIn("不应挂到当前变体的文案", page.locator("#direction-variants").text_content())
        self.assertNotIn("待写稿", page.locator("#direction-progress-summary").inner_text())

    def test_older_server_without_capability_keeps_basic_direction_usable(self):
        page = self.open_page()
        data = self.detail_data()
        data.pop("progress_available")
        calls = []
        page.route(self.progress_pattern, lambda route: (calls.append(route.request.url), route.fulfill(status=404, json={"error": "旧服务"})))
        self.show_detail(page, data)
        page.locator('details.dir-variant > summary').first.click()
        self.assertTrue(page.locator('[data-variant-progression="0"]').is_visible())
        self.assertTrue(page.locator("#direction-write").is_visible())
        self.assertEqual(page.locator("ol.dir-flow").count(), 0)
        self.assertEqual(calls, [])

    def test_variant_next_step_prefills_test_card_and_saves_real_snapshot(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        reply = self.progress_data(data, {"kind": "test", "label": "建立测试卡", "video_id": "7664986913497009439"})
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=reply))
        # Only progress is mocked: detail, form catalog reads, and saving all use the real fixture.
        page.evaluate("location.hash='#/202H/directions/202H-S1'")
        self.wait_for_progress(page)
        page.locator('[data-variant-next="0"]').click()
        dialog = page.locator("#direction-plan-dialog")
        dialog.wait_for()
        self.assertEqual(dialog.locator('[name="variant_index"]').input_value(), "0")
        self.assertEqual(dialog.locator('[name="test_video_id"]').input_value(), "7664986913497009439")
        dialog.locator('[name="hypothesis"]').fill("这个变体先演示结果更容易理解")
        dialog.locator('[name="change"]').fill("先展示推磨结果，再解释装夹")
        dialog.get_by_role("button", name="保存测试卡", exact=True).click()
        dialog.wait_for(state="hidden")
        page.locator(".direction-test").get_by_text("这个变体先演示结果更容易理解", exact=True).wait_for()
        status, _, creation = self.request("GET", "/api/creation?product=202H")
        self.assertEqual(status, 200, creation)
        saved = creation["plans"][0]
        self.assertEqual(saved["variant_ref"], variant_ref(data["direction"]))
        self.assertEqual(saved["experiment"]["test_video_id"], "7664986913497009439")
        page.locator("[data-test-edit]").click()
        dialog.locator('[name="hypothesis"]').fill("仅修订假设，保留本变体归属")
        dialog.get_by_role("button", name="保存测试卡", exact=True).click()
        dialog.wait_for(state="hidden")
        self.assertEqual(self.request("GET", "/api/creation?product=202H")[2]["plans"][0]["variant_ref"], saved["variant_ref"])

    def test_general_test_card_can_explicitly_select_variant(self):
        page = self.open_page("#/202H/directions/202H-S1")
        data = self.detail_data()
        page.locator("#direction-test-create").click()
        dialog = page.locator("#direction-plan-dialog")
        selector = dialog.locator('[name="variant_index"]')
        self.assertEqual(selector.input_value(), "")
        selector.select_option("0")
        dialog.locator('[name="hypothesis"]').fill("从通用入口明确选择本变体")
        dialog.locator('[name="change"]').fill("保留同一方向，换说明顺序")
        dialog.get_by_role("button", name="保存测试卡", exact=True).click()
        dialog.wait_for(state="hidden")
        saved = self.request("GET", "/api/creation?product=202H")[2]["plans"][0]
        self.assertEqual(saved["variant_ref"], variant_ref(data["direction"]))

    def test_next_steps_open_the_right_production_stage_task_and_chosen_variant(self):
        page = self.open_page()
        data = self.detail_data()
        original = data["direction"]["variants"][0]
        data["direction"]["variants"] = [
            {**copy.deepcopy(original), "concern": f"快捷操作测试顾虑 {index + 1}",
             "closing": f"快捷操作测试收口 {index + 1}", "baseline": index == 0}
            for index in range(4)
        ]
        reply = self.progress_data(data)
        actions = [
            ({"kind": "production", "production_kind": "video", "id": "PRD-video-ui", "label": "登记发布"},
             "#/202H/production/PRD-video-ui?step=publish"),
            ({"kind": "production", "production_kind": "carousel", "id": "CAR-carousel-ui", "label": "登记发布"},
             "#/202H/production/CAR-carousel-ui?step=delivery"),
            ({"kind": "task", "id": "AIT-next-ui", "label": "采纳候选文案"}, "#/202H/ai/AIT-next-ui"),
            ({"kind": "write", "label": "写这一稿"}, None),
        ]
        for row, (action, _) in zip(reply["variants"], actions):
            row["next_action"] = action
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=reply))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        for index, (action, expected_url) in enumerate(actions):
            with self.subTest(action=action):
                button = page.locator(f'[data-variant-next="{index}"]')
                self.assertIn(action["label"], button.inner_text())
                if expected_url:
                    self.assertEqual(button.evaluate("node=>node.tagName"), "A")
                    self.assertEqual(button.get_attribute("href"), expected_url)
        page.evaluate("""()=>{
            window.__workflowWritingCalls=[];
            window.CaseAI.startFromDirection=(direction,options)=>{
                window.__workflowWritingCalls.push({direction,options});
            };
        }""")
        page.locator('[data-variant-next="3"]').click()
        calls = page.evaluate("window.__workflowWritingCalls")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["direction"], data["direction"])
        self.assertEqual(calls[0]["options"], {"variantIndex": 3})
        self.assertEqual(self.server.app.ai.list_tasks(), [], "The navigation check must not submit a real AI task")

    def test_late_progress_response_cannot_replace_the_page_after_leaving_direction(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        self.show_detail(page, data)
        self.wait_for_held(page, held)
        page.evaluate("location.hash='#/202H/directions'")
        page.locator("#direction-filters").wait_for()
        page.evaluate("window.__workflowNewPage=document.querySelector('#direction-filters')")
        with page.expect_response(lambda response: "/api/direction-progress/202H-S1?" in response.url) as arrived:
            held[0].fulfill(status=200, json=self.progress_data(data))
        self.assertEqual(arrived.value.json()["catalog_revision"], data["revision"])
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertEqual(page.evaluate("location.hash"), "#/202H/directions")
        self.assertTrue(page.evaluate("window.__workflowNewPage===document.querySelector('#direction-filters')"))
        self.assertEqual(page.locator("#direction-definition,#direction-progress-summary,[data-variant-next]").count(), 0)
        self.assertNotIn("此变体已保存的文案", page.locator("#content").text_content())


if __name__ == "__main__":
    unittest.main()
