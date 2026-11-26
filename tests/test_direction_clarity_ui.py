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
        catalog_ui.show_direction(page)

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
        row = page.locator('#dir-list [data-direction-id="202H-S1"]')
        summary = row.locator(".dir-sub").inner_text()
        self.assertNotIn("方向 202H-S1", summary)  # the code badge already names it
        self.assertNotIn("唯一可检索证据", summary)
        # A direction in the list keeps its orders (as a figure and a bar) and how much was made for it; rates stay
        # with the direction opened beside it.
        self.assertIn("3 条视频", summary)
        self.assertEqual(row.locator(".dx-num").inner_text(), "987单")
        page.wait_for_function("document.querySelector('#dir-list [data-bar]').style.width==='100%'")
        self.assertNotIn("7.99%", row.inner_text())
        self.assertNotIn("12345", row.inner_text())
        # Evidence is read on the direction page; the row keeps the chain only.
        self.assertEqual(row.locator("details.dir-evidence").count(), 0)
        page.locator('[name="q"]').fill("唯一可检索证据")
        page.wait_for_function("()=>new URLSearchParams(location.hash.split('?')[1]).get('q')==='唯一可检索证据'")
        self.assertEqual(page.locator("#dir-list .dir-row").count(), 1)
        self.assertEqual(page.locator("#dir-list .dir-row").get_attribute("data-direction-id"), "202H-S1")
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
                    supporting:document.querySelector('#direction-variants').getBoundingClientRect().top,
                    overflowing:document.documentElement.scrollWidth>document.documentElement.clientWidth
                })""")
                self.assertLess(geometry["definition"], geometry["supporting"])
                self.assertFalse(geometry["overflowing"])
        evidence.locator("summary").click()
        evidence.get_by_text(self.evidence, exact=False).wait_for()
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_variants_start_compact_and_support_keyboard_and_bulk_disclosure(self):
        page = self.open_page(width=390)
        self.show_detail(page, self.detail_data())
        self.assertEqual(page.locator("details.dir-variant").count(), 9)
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 0)
        last = page.locator('details.dir-variant[data-variant-index="8"]')
        self.assertIn("结果倒叙", last.locator("summary").inner_text())
        # On a phone the one-line row hides the change; it is still the row's text for search and hover.
        self.assertIn("先演示第 9 个结果再解释原因", last.locator("summary").text_content())
        self.assertFalse(last.locator("[data-variant-progression]").is_visible())
        last.locator("summary").focus()
        page.keyboard.press("Enter")
        last.get_by_text("完整顾虑正文 9", exact=True).wait_for()
        self.assertTrue(last.locator("[data-variant-progression]").is_visible())
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 1)
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))

    def test_judgement_is_one_row_of_choices_and_test_cards_are_no_longer_shown(self):
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
        self.assertEqual(page.locator("#direction-tests, [data-show-tests]").count(), 0)
        self.assertEqual(page.get_by_text("进行中测试的完整假设").count(), 0)
        # The open decision is the pressed choice, with its note beside it; finished ones and test cards stay out.
        plans = page.locator("#direction-plans")
        self.assertEqual(plans.locator("button").all_inner_texts(), ["加码", "换执行", "观察", "停测"])
        self.assertEqual(plans.locator("[aria-pressed=true]").all_inner_texts(), ["加码"])
        page.locator(".dx-plan-line").get_by_text("下一轮只改变开头", exact=True).wait_for()
        self.assertEqual(page.get_by_text("已结束的普通安排", exact=True).count(), 0)

    def test_one_click_judgement_reaches_the_list_and_stopping_moves_the_direction_to_paused(self):
        page = self.open_page()
        page.locator("#direction-pane:not([aria-busy]) #direction-plans").wait_for()
        pressed = lambda: page.locator("#direction-plans [aria-pressed=true]").all_inner_texts()
        plans = lambda: [(plan["action"], plan["status"]) for plan in self.server.app.creation.public()["plans"]]
        status = lambda: next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")["status"]
        self.assertEqual(pressed(), ["观察"])
        # 加码 is saved on the click, with no dialog and no reason asked for.
        page.locator('#direction-plans [data-direction-plan="scale"]').click()
        page.locator('#dir-list [data-direction-id="202H-S1"] .state-tag').get_by_text("加码中", exact=True).wait_for()
        page.locator('#direction-plans [data-direction-plan="scale"][aria-pressed=true]').wait_for()
        self.assertEqual(page.locator("dialog[open]").count(), 0)
        self.assertEqual(plans(), [("scale", "pending")])
        # 停测 asks once, settles the open plan and moves the direction under 已暂停.
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator('#direction-plans [data-direction-plan="stop"]').click()
        page.locator('#dir-list .dx-list.is-paused [data-direction-id="202H-S1"]').wait_for()
        page.locator('#direction-plans [data-direction-plan="stop"][aria-pressed=true]').wait_for()
        self.assertEqual((plans(), status()), ([("scale", "cancelled")], "paused"))
        # Any other choice brings it back.
        page.locator('#direction-plans [data-direction-plan="observe"]').click()
        page.locator('#dir-list .dx-list:not(.is-paused) [data-direction-id="202H-S1"]').wait_for()
        self.assertEqual((pressed(), status()), (["观察"], "testing"))
        self.assertFalse(page.evaluate(
            "document.documentElement.scrollWidth>document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
