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
        catalog_ui.show_direction(page)
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
        page.locator(".direction-variant-list.has-progress").wait_for(state="attached")

    def open_related(self, page):
        related = page.locator("details.dir-related")
        if related.get_attribute("open") is None:
            related.locator(":scope > summary").click()

    def test_delayed_progress_keeps_first_paint_disclosure_and_related_tab(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        core_calls = self.show_detail(page, data)
        self.wait_for_held(page, held)
        self.assertTrue(page.locator("#direction-definition").is_visible())
        self.assertEqual(page.locator("details.dir-variant").count(), len(data["direction"]["variants"]))
        self.assertEqual(page.locator('[data-variant-orders="0"]').inner_text(), "…")
        details = page.locator('details.dir-variant[data-variant-index="0"]')
        details.locator(":scope > summary").click()
        self.open_related(page)
        page.locator("#direction-cases").click()
        page.evaluate("window.__workflowVariant=document.querySelector('details.dir-variant')")
        held[0].fulfill(status=200, json=self.progress_data(data))
        self.wait_for_progress(page)
        self.assertIsNotNone(details.get_attribute("open"))
        self.assertEqual(page.locator("#direction-cases").get_attribute("aria-selected"), "true")
        self.assertTrue(page.evaluate("window.__workflowVariant===document.querySelector('details.dir-variant')"))
        self.assertEqual(len(core_calls), 1)
        self.assertEqual(parse_qs(urlparse(held[0].request.url).query), {"product": ["202H"]})
        self.assertEqual(page.locator('[data-variant-orders="0"]').inner_text(), "—")

    def test_folded_angle_is_one_line_without_stage_dots_records_or_unassigned_fold(self):
        # Since 2026-11-22 an angle shows structure, change and orders; its scripts and orders live on their own pages.
        page = self.open_page(width=390)
        data = self.detail_data()
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=self.progress_data(data)))
        self.show_detail(page, data)
        self.wait_for_progress(page)
        row = page.locator(".dir-variant-row").first
        details = row.locator("details.dir-variant")
        self.assertIsNone(details.get_attribute("open"))
        self.assertLessEqual(row.bounding_box()["height"], 60)
        self.assertEqual(page.locator(".dir-dots, [data-variant-records], [data-variant-next], #direction-unassigned").count(), 0)
        self.assertNotIn("此变体已保存的文案", page.locator("#direction-variants").text_content())
        details.locator(":scope > summary").click()
        calls = page.evaluate("()=>{window.__writes=[];window.CaseAI.startFromDirection=(d,o)=>window.__writes.push(o);}")
        # 再写 sits on the row itself, so the angle need not be opened to write from it.
        row.get_by_role("button", name="按这条再写一稿", exact=True).click()
        self.assertEqual(page.evaluate("window.__writes"), [{"variantIndex": 0}])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_structure_is_free_text_saved_on_the_angle(self):
        page = self.open_page()
        data = self.detail_data()
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=self.progress_data(data)))
        self.show_detail(page, data)
        page.unroute(self.detail_pattern)
        details = page.locator('details.dir-variant[data-variant-index="0"]')
        details.locator(":scope > summary").click()
        details.locator('[data-variant-progression="0"]').fill("开箱逐件讲")
        details.get_by_role("button", name="保存结构", exact=True).click()
        page.locator('details.dir-variant[data-variant-index="0"] .dir-variant-structure').get_by_text("开箱逐件讲", exact=True).wait_for()
        saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")
        self.assertEqual(saved["variants"][0]["progression"], "开箱逐件讲")

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
        self.assertEqual(tags[1:], ["#1", "#2", "方向定义", "#3"])
        self.assertEqual(page.locator('details.dir-variant .dir-flag').count(), 0)  # no 基准 badge any more
        self.assertEqual(page.locator("#direction-variants h2 small").inner_text(), "3")
        structure = page.locator('details.dir-variant[data-variant-index="1"] .dir-variant-structure')
        self.assertEqual(structure.inner_text(), "原理讲解")
        self.assertEqual(structure.get_attribute("title"), "其他：原理讲解")
        for width in (1280, 390):
            page.set_viewport_size({"width": width, "height": 900})
            for row in page.locator(".dir-variant-row").all():
                self.assertLessEqual(row.bounding_box()["height"], 60)  # a long change wraps to two lines at most
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
        # Beside the list at this width the right side has room for videos and orders; CTOR joins on a wider window.
        self.assertEqual(columns.inner_text().split(), ["角度", "结构", "改了什么", "视频", "订单"])
        self.assertEqual(page.locator('[data-variant-videos="0"]').inner_text(), "2")
        self.assertEqual(page.locator('[data-variant-ctor="0"]').text_content(), "1.12%")
        page.wait_for_function("document.querySelector('[data-variant-bar]').style.width==='100%'")
        orders = page.locator('[data-variant-orders="0"]')
        self.assertEqual(orders.inner_text(), "31")
        self.assertEqual(orders.get_attribute("title"), "2 条视频，1 条有销售数据，截至 2026-10-18")
        # A variant without videos shows dashes, never zero orders.
        last = len(data["direction"]["variants"]) - 1
        if last:
            self.assertEqual(page.locator(f'[data-variant-orders="{last}"]').inner_text(), "—")
        # The orders figure ends where its column does.
        heading = columns.locator(".dir-variant-orders").bounding_box()
        cell = orders.bounding_box()
        self.assertAlmostEqual(heading["x"] + heading["width"], cell["x"] + cell["width"], delta=1)
        # Related lists sit in one fold and show five until asked.
        self.open_related(page)
        panel = page.locator("#direction-scripts-panel")
        self.assertEqual(sum(item.is_visible() for item in panel.locator(".direction-related-item").all()), 5)
        panel.get_by_role("button", name="显示全部 7 条", exact=True).click()
        self.assertEqual(sum(item.is_visible() for item in panel.locator(".direction-related-item").all()), 7)
        self.assertEqual(page.locator("details.dir-case-performance").count(), 0)
        page.set_viewport_size({"width": 390, "height": 900})
        self.assertTrue(orders.is_visible())
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
        self.assertEqual(page.locator(".direction-variant-list.has-progress").count(), 0)
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
        self.assertEqual(page.locator(".direction-variant-list.has-progress").count(), 0)
        self.assertNotIn("不应挂到当前变体的文案", page.locator("#direction-variants").text_content())

    def test_older_server_without_capability_keeps_basic_direction_usable(self):
        page = self.open_page()
        data = self.detail_data()
        data.pop("progress_available")
        calls = []
        page.route(self.progress_pattern, lambda route: (calls.append(route.request.url), route.fulfill(status=404, json={"error": "旧服务"})))
        # An older server says nothing about per-angle sales in the list either; everything on screen is re-read from it.
        listing = self.catalog()
        listing.pop("progress_available")
        page.route("**/api/direction-catalog?**", lambda route: route.fulfill(status=200, json=listing))
        page.route(self.detail_pattern, lambda route: route.fulfill(status=200, json=copy.deepcopy(data)))
        page.evaluate("document.querySelector('#direction-definition').setAttribute('data-stale','')")
        page.evaluate("async()=>await route(true,true)")
        page.locator("#direction-definition:not([data-stale])").wait_for()
        self.show_detail(page, data)
        page.locator('details.dir-variant > summary').first.click()
        self.assertTrue(page.locator('[data-variant-progression="0"]').is_visible())
        self.assertTrue(page.locator("#direction-write").is_visible())
        self.assertEqual(page.locator('[data-variant-orders="0"]').inner_text(), "—")
        self.assertEqual(calls, [])

    def test_test_cards_are_not_offered_even_when_progress_suggests_one(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        reply = self.progress_data(data, {"kind": "test", "label": "建立测试卡", "video_id": "7664986913497009439"})
        page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=reply))
        catalog_ui.show_direction(page)
        self.wait_for_progress(page)
        self.assertEqual(page.locator("#direction-tests, #direction-test-create, [data-variant-next], [data-variant-test]").count(), 0)
        self.assertNotIn("建立测试卡", page.locator("#content").text_content())

    def test_late_progress_response_cannot_replace_the_page_after_leaving_direction(self):
        page = self.open_page()
        data = self.detail_data()
        held = self.hold_progress(page)
        self.show_detail(page, data)
        self.wait_for_held(page, held)
        # Back on the list the same direction is still open beside it; another page is where a late answer must not land.
        page.evaluate("location.hash='#/202H/settings'")
        page.locator(".settings-page").wait_for()
        page.evaluate("window.__workflowNewPage=document.querySelector('.settings-page')")
        with page.expect_response(lambda response: "/api/direction-progress/202H-S1?" in response.url) as arrived:
            held[0].fulfill(status=200, json=self.progress_data(data))
        self.assertEqual(arrived.value.json()["catalog_revision"], data["revision"])
        page.evaluate("()=>new Promise(resolve=>requestAnimationFrame(()=>setTimeout(resolve,0)))")
        self.assertEqual(page.evaluate("location.hash"), "#/202H/settings")
        self.assertTrue(page.evaluate("window.__workflowNewPage===document.querySelector('.settings-page')"))
        self.assertEqual(page.locator("#direction-definition,#direction-progress-summary,[data-variant-next]").count(), 0)
        self.assertNotIn("此变体已保存的文案", page.locator("#content").text_content())


if __name__ == "__main__":
    unittest.main()
