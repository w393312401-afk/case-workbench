"""Direction information hierarchy and disclosure, with disposable fixture data."""
import copy
import unittest

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class DirectionClarityTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    evidence = "唯一可检索证据。" + "这里保留需要按需查阅的完整依据。" * 32 + "证据全文结束。"

    def detail_data(self):
        status, _, data = self.request(
            "GET", "/api/direction-catalog/202H-S1?product=202H&view=page&include_tasks=0")
        self.assertEqual(status, 200, data)
        data.pop("tasks_pending", None)
        data["tasks"] = []
        data["selling_point"]["evidence"] = self.evidence
        prototype = copy.deepcopy(data["direction"]["variants"][0])
        data["direction"]["variants"] = [
            {**copy.deepcopy(prototype), "baseline": index == 0,
             "variant_type": "execution", "progression": "结果倒叙",
             "concern": f"完整顾虑正文 {index + 1}", "closing": f"完整收口正文 {index + 1}",
             "execution_change": f"先演示第 {index + 1} 个结果再解释原因"}
            for index in range(9)
        ]
        return data

    def show_detail(self, page, data):
        page.route("**/api/direction-catalog/202H-S1?**",
                   lambda route: route.fulfill(status=200, json=copy.deepcopy(data)))
        page.evaluate("location.hash='#/202H/directions/202H-S1'")
        page.locator("#direction-definition").wait_for()

    def test_list_keeps_counts_without_long_evidence_or_sales_metrics_and_still_searches_evidence(self):
        page = self.open_page()
        data = self.catalog()
        point = next(item for item in data["selling_points"] if item["code"] == "S1")
        point["evidence"] = self.evidence
        direction = next(item for item in data["directions"] if item["direction_id"] == "202H-S1")
        direction["performance"] = {
            "video_count": 3, "matched_count": 3, "orders": 987,
            "clicks": 12345, "ctor_pct": 7.99, "data_through": "2026-10-14",
        }
        page.route("**/api/direction-catalog?**",
                   lambda route: route.fulfill(status=200, json=copy.deepcopy(data)))
        page.evaluate("async()=>await route(true,true)")
        row = page.locator('[data-direction-id="202H-S1"]')
        summary = row.locator(".dir-sub").inner_text()
        self.assertNotIn("方向 202H-S1", summary)  # the code badge already names it
        self.assertIn("3 条视频", summary)
        self.assertNotIn("唯一可检索证据", summary)
        self.assertNotIn("987", summary)
        self.assertNotIn("12345", summary)
        self.assertNotIn("CTOR", summary)
        evidence = row.locator("details.dir-evidence")
        self.assertIsNone(evidence.get_attribute("open"))
        self.assertFalse(evidence.get_by_text(self.evidence, exact=True).is_visible())
        evidence.locator("summary").click()
        evidence.get_by_text(self.evidence, exact=True).wait_for()
        page.locator('[name="q"]').fill("唯一可检索证据")
        page.wait_for_function("()=>document.querySelectorAll('.dir-row').length===1")
        self.assertEqual(page.locator(".dir-row").get_attribute("data-direction-id"), "202H-S1")
        for width in (1280, 390):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertFalse(page.evaluate(
                    "document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_direction_precedes_supporting_cards_and_long_evidence_is_available_on_demand(self):
        page = self.open_page()
        self.show_detail(page, self.detail_data())
        evidence = page.locator("details.dir-point-evidence")
        self.assertIsNone(evidence.get_attribute("open"))
        self.assertFalse(evidence.get_by_text(self.evidence, exact=True).is_visible())
        for width in (1280, 390):
            with self.subTest(width=width):
                page.set_viewport_size({"width": width, "height": 900})
                geometry = page.evaluate("""()=>({
                    definition:document.querySelector('#direction-definition').getBoundingClientRect().top,
                    supporting:document.querySelector('.dir-aside').getBoundingClientRect().top,
                    overflowing:document.documentElement.scrollWidth>document.documentElement.clientWidth
                })""")
                self.assertLess(geometry["definition"], geometry["supporting"])
                self.assertFalse(geometry["overflowing"])
        evidence.locator("summary").click()
        evidence.get_by_text(self.evidence, exact=True).wait_for()
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_variants_start_compact_and_support_keyboard_and_bulk_disclosure(self):
        page = self.open_page(width=390)
        self.show_detail(page, self.detail_data())
        self.assertEqual(page.locator("details.dir-variant").count(), 9)
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 0)
        last = page.locator('details.dir-variant[data-variant-index="8"]')
        self.assertIn("结果倒叙", last.locator("summary").inner_text())
        self.assertIn("先演示第 9 个结果再解释原因", last.locator("summary").inner_text())
        self.assertFalse(last.locator("[data-variant-progression]").is_visible())
        last.locator("summary").focus()
        page.keyboard.press("Enter")
        last.get_by_text("完整顾虑正文 9", exact=True).wait_for()
        self.assertTrue(last.locator("[data-variant-progression]").is_visible())
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 1)
        toggle = page.locator("#direction-variants-toggle")
        toggle.click()
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 9)
        toggle.click()
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 0)
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_test_cards_and_completed_plans_have_one_full_record_with_history_disclosure(self):
        page = self.open_page(width=390)
        data = self.detail_data()
        def test_plan(identifier, status, hypothesis):
            return {"id": identifier, "action": "execution", "status": status, "note": "",
                    "experiment": {"hypothesis": hypothesis, "change": "先给结果再解释",
                                   "metric": "ctor_pct", "decision": "observe"}}
        data["plans"] = [
            test_plan("PLAN-active", "pending", "进行中测试的完整假设"),
            test_plan("PLAN-done", "done", "已完成测试的完整假设"),
            test_plan("PLAN-cancelled", "cancelled", "已取消测试的完整假设"),
            {"id": "PLAN-next", "action": "scale", "status": "pending", "note": "下一轮只改变开头"},
            {"id": "PLAN-old", "action": "stop", "status": "done", "note": "已结束的普通安排"},
        ]
        self.show_detail(page, data)
        tests = page.locator("#direction-tests")
        plans = page.locator("#direction-plans")
        for hypothesis in ("进行中测试的完整假设", "已完成测试的完整假设", "已取消测试的完整假设"):
            self.assertNotIn(hypothesis, plans.text_content())
            self.assertEqual(tests.get_by_text(hypothesis, exact=True).count(), 1)
        tests.get_by_text("进行中测试的完整假设", exact=True).wait_for()
        self.assertFalse(tests.get_by_text("已完成测试的完整假设", exact=True).is_visible())
        self.assertFalse(tests.get_by_text("已取消测试的完整假设", exact=True).is_visible())
        history = tests.locator("details.direction-tests-history")
        self.assertIsNone(history.get_attribute("open"))
        history.locator(":scope > summary").click()
        tests.get_by_text("已完成测试的完整假设", exact=True).wait_for()
        tests.get_by_text("已取消测试的完整假设", exact=True).wait_for()
        plans.get_by_text("下一轮只改变开头", exact=True).wait_for()
        self.assertFalse(plans.get_by_text("已结束的普通安排", exact=True).is_visible())
        plans.locator("details.dir-plan-history > summary").click()
        plans.get_by_text("已结束的普通安排", exact=True).wait_for()
        plans.locator("[data-show-tests]").click()
        page.wait_for_function("""()=>{
            const rect=document.querySelector('#direction-tests').getBoundingClientRect();
            return rect.top>=0&&rect.top<innerHeight;
        }""")
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
