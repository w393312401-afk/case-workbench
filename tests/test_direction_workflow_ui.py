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
        page.locator('[data-variant-dots="0"] i').first.wait_for()

    def test_delayed_progress_keeps_first_paint_disclosure_and_related_tab(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        core_calls = self.show_detail(page, data)
        self.wait_for_held(page, held)
        self.assertTrue(page.locator("#direction-definition").is_visible())
        self.assertEqual(page.locator("details.dir-variant").count(), len(data["direction"]["variants"]))
        self.assertEqual(page.locator(".dir-dots i").count(), 0)
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

    def test_folded_variant_is_one_line_with_stage_dots_and_direct_script_link(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        progress = self.progress_data(data)
        progress["variants"][0]["stages"]["production"] = {"status": "attention", "label": "制作待处理",
                                                          "history_label": "已有 1 张制作完成", "count": 1}
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=progress))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        row = page.locator(".dir-variant-row").first
        details = row.locator("details.dir-variant")
        self.assertIsNone(details.get_attribute("open"))
        self.assertLessEqual(row.bounding_box()["height"], 40)
        # No test card in this direction: three dots, no test stage anywhere, no summary line.
        dots = row.locator('[data-variant-dots="0"]')
        self.assertEqual(dots.locator("i").count(), 3)
        self.assertEqual(dots.get_attribute("role"), "img")
        for label in ("写稿：已存 1 稿", "制作：制作待处理（已有 1 张制作完成）", "发布：待发布登记"):
            self.assertIn(label, dots.get_attribute("aria-label"))
        self.assertEqual(dots.locator("i.is-attention").get_attribute("title"), "制作：制作待处理（已有 1 张制作完成）")
        self.assertNotIn("测试", page.locator(".direction-variant-list").text_content())
        self.assertTrue(page.locator("#direction-progress-summary").is_hidden())
        self.assertNotIn("已关联记录", page.locator("#direction-progress-summary").text_content())
        self.assertEqual(row.locator('a[data-variant-next="0"]').get_attribute("href"), "#/202H/content/SCR-workflow-ui")
        # On a phone a row with a button lets the dot colour carry the state.
        self.assertFalse(row.locator('[data-variant-state="0"]').is_visible())
        self.assertEqual(row.locator('[data-variant-state="0"]').text_content(), "制作待处理")
        self.assertFalse(details.locator('[data-variant-records="0"]').is_visible())
        # Clicking the next step does not fold or unfold the row; the summary does.
        page.evaluate("window.CaseAI.startFromDirection=()=>{}")
        details.locator(":scope > summary").click()
        self.assertIsNotNone(details.get_attribute("open"))
        self.assertIn("制作 制作待处理", details.locator('[data-variant-stages="0"]').inner_text())
        unassigned = page.locator("#direction-unassigned")
        self.assertEqual(unassigned.evaluate("node=>node.tagName"), "DETAILS")
        self.assertIsNone(unassigned.get_attribute("open"))
        self.assertFalse(unassigned.get_by_text("待关联的旧文案", exact=False).is_visible())
        unassigned.locator(":scope > summary").click()
        unassigned.get_by_text("待关联的旧文案", exact=False).wait_for()
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_numbers_skip_the_direction_definition_and_long_changes_stay_on_one_line(self):
        page = self.open_page()
        data = self.detail_data()
        original = data["direction"]["variants"][0]
        types = ["execution", "execution", "direction", "execution"]
        data["direction"]["variants"] = [
            {**copy.deepcopy(original), "variant_type": kind, "baseline": index == 0, "progression": "其他：原理讲解",
             "concern": f"编号测试顾虑 {index + 1}", "closing": f"编号测试收口 {index + 1}",
             "execution_change": "很长的改动说明" * 40}
            for index, kind in enumerate(types)
        ]
        progress = self.progress_data(data)
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=progress))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        tags = page.locator(".dir-variant-tag").all_inner_texts()
        self.assertEqual(tags, ["#1", "#2", "方向定义", "#3"])
        self.assertEqual(page.locator('details.dir-variant[data-variant-index="0"] .dir-flag').inner_text(), "基准")
        self.assertEqual(page.locator("#direction-variants h2 small").inner_text(), "3")
        structure = page.locator('details.dir-variant[data-variant-index="1"] .dir-variant-structure')
        self.assertEqual(structure.inner_text(), "原理讲解")
        self.assertEqual(structure.get_attribute("title"), "其他：原理讲解")
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900})
            for row in page.locator(".dir-variant-row").all():
                self.assertLessEqual(row.bounding_box()["height"], 40)
            self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_variant_rows_carry_their_own_sales_and_long_lists_fold(self):
        page = self.open_page(width=1280)
        data = self.detail_data()
        data["scripts"] = [{"script_id": f"SCR-fold-{index}", "title": f"折叠测试采用稿 {index}", "version": 1,
                            "status": "adopted"} for index in range(7)]
        progress = self.progress_data(data)
        progress["variants"][0]["performance"] = {"video_count": 2, "matched_count": 1, "orders": 31, "clicks": 2767,
                                                  "ctor_pct": 1.12, "data_through": "2026-10-18"}
        progress["variants"][0]["records"]["videos"] = [{"id": "7664986913497009439", "video_id": "7664986913497009439",
            "headline": "隔离测试视频", "orders": 31, "ctor_pct": 1.12}]
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=progress))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        columns = page.locator(".dir-variant-columns")
        self.assertEqual(columns.inner_text().split(), ["变体", "进度", "视频", "订单", "CTOR", "下一步"])
        self.assertEqual(page.locator('[data-variant-videos="0"]').inner_text(), "2")
        orders = page.locator('[data-variant-orders="0"]')
        self.assertEqual(orders.inner_text(), "31")
        self.assertEqual(orders.get_attribute("title"), "1/2 条视频有销售数据，截至 2026-10-18")
        self.assertEqual(page.locator('[data-variant-ctor="0"]').inner_text(), "1.12%")
        # A variant without videos shows dashes, never zero orders.
        last = len(data["direction"]["variants"]) - 1
        if last:
            self.assertEqual(page.locator(f'[data-variant-orders="{last}"]').inner_text(), "—")
        # Numbers line up under their headings.
        heading = columns.locator(".dir-variant-num").nth(1).bounding_box()
        cell = orders.bounding_box()
        self.assertAlmostEqual(heading["x"] + heading["width"], cell["x"] + cell["width"], delta=1)
        page.locator('details.dir-variant[data-variant-index="0"] > summary').click()
        page.locator('[data-variant-records="0"]').get_by_text("隔离测试视频", exact=True).wait_for()
        self.assertIn("31 单 · CTOR 1.12%", page.locator('[data-variant-records="0"]').inner_text())
        # Related lists show five until asked; per-case performance stays folded.
        panel = page.locator("#direction-scripts-panel")
        self.assertEqual(sum(item.is_visible() for item in panel.locator(".direction-related-item").all()), 5)
        panel.get_by_role("button", name="显示全部 7 条", exact=True).click()
        self.assertEqual(sum(item.is_visible() for item in panel.locator(".direction-related-item").all()), 7)
        performance = page.locator("details.dir-case-performance")
        if performance.count():
            self.assertIsNone(performance.get_attribute("open"))
        page.set_viewport_size({"width": 390, "height": 900})
        self.assertFalse(columns.is_visible())
        self.assertFalse(orders.is_visible())
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
        self.assertEqual(page.locator(".dir-dots i").count(), 0)
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
        self.assertEqual(page.locator(".dir-dots i").count(), 0)
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
        self.assertEqual(page.locator(".dir-dots i").count(), 0)
        self.assertEqual(calls, [])

    def test_without_test_cards_one_entry_creates_one_and_brings_the_test_tools_back(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        reply = self.progress_data(data, {"kind": "test", "label": "建立测试卡", "video_id": "7664986913497009439"})
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=reply))
        # Only progress is mocked: detail, form catalog reads, and saving all use the real fixture.
        page.evaluate("location.hash='#/202H/directions/202H-S1'")
        self.wait_for_progress(page)
        self.assertEqual(page.locator("#direction-tests, [data-variant-next], [data-variant-test]").count(), 0)
        entry = page.locator("#direction-plans #direction-test-create")
        self.assertEqual(entry.inner_text(), "＋ 测试卡")
        entry.click()
        dialog = page.locator("#direction-plan-dialog")
        dialog.wait_for()
        dialog.locator('[name="variant_index"]').select_option("0")
        dialog.locator('[name="hypothesis"]').fill("这个变体先演示结果更容易理解")
        dialog.locator('[name="change"]').fill("先展示推磨结果，再解释装夹")
        dialog.locator('[name="test_video_id"]').fill("7664986913497009439")
        dialog.get_by_role("button", name="保存测试卡", exact=True).click()
        dialog.wait_for(state="hidden")
        page.locator(".direction-test").get_by_text("这个变体先演示结果更容易理解", exact=True).wait_for()
        status, _, creation = self.request("GET", "/api/creation?product=202H")
        self.assertEqual(status, 200, creation)
        saved = creation["plans"][0]
        self.assertEqual(saved["variant_ref"], variant_ref(data["direction"]))
        self.assertEqual(saved["experiment"]["test_video_id"], "7664986913497009439")
        # With one card the full set returns: card section, fourth dot, the row's 建立测试卡, and no second entry.
        self.wait_for_progress(page)
        self.assertEqual(page.locator('[data-variant-dots="0"] i').count(), 4)
        self.assertEqual(page.locator("#direction-plans #direction-test-create").count(), 0)
        self.assertEqual(page.locator("#direction-tests #direction-test-create").count(), 1)
        self.assertIn("建立测试卡", page.locator('[data-variant-next="0"]').inner_text())
        page.locator("[data-test-edit]").click()
        dialog.locator('[name="hypothesis"]').fill("仅修订假设，保留本变体归属")
        dialog.get_by_role("button", name="保存测试卡", exact=True).click()
        dialog.wait_for(state="hidden")
        self.assertEqual(self.request("GET", "/api/creation?product=202H")[2]["plans"][0]["variant_ref"], saved["variant_ref"])

    def test_choosing_the_variant_fills_its_change_only_when_empty(self):
        page = self.open_page()
        data = self.detail_data()
        data["direction"]["variants"][0]["execution_change"] = "先展示推磨结果"
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=self.progress_data(data)))
        self.show_detail(page, data)
        page.locator("#direction-test-create").click()
        dialog = page.locator("#direction-plan-dialog")
        change = dialog.locator('[name="change"]')
        self.assertEqual(change.input_value(), "")
        dialog.locator('[name="variant_index"]').select_option("0")
        self.assertEqual(change.input_value(), "先展示推磨结果")
        change.fill("我自己写的改动")
        dialog.locator('[name="variant_index"]').select_option("")
        dialog.locator('[name="variant_index"]').select_option("0")
        self.assertEqual(change.input_value(), "我自己写的改动")

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
