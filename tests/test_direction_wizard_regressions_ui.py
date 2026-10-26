"""Direction wizard field routing and lead changes, in an isolated case library."""
import unittest

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class DirectionWizardRegressionTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    def choose_point(self, dialog, point):
        if point["tier"] != "S" and not dialog.locator(".direction-non-s[open]").count():
            dialog.locator(".direction-non-s > summary").click()
        dialog.locator(f'[name="lead_point"][value="{point["id"]}"]').check()
        self.assertTrue(dialog.locator(f'[name="lead_point"][value="{point["id"]}"]').is_checked())
        self.assertEqual(dialog.locator("#direction-generated-id").inner_text(), f'202H-{point["code"]}')

    def test_variant_suggestions_target_chain_fields_and_save_without_changing_execution(self):
        before = self.store.read_case(self.identifier)
        page = self.open_page("#/202H/directions/202H-S1")
        page.route("**/api/materials?product=202H", lambda route: route.fulfill(status=200, json={
            "theme_links": [{"selling_point_id": self.point_s["id"], "concern": "评论里的装夹顾虑"}],
        }))
        page.locator("#direction-add-variant").click()
        dialog = page.locator("#direction-create-dialog")
        dialog.get_by_role("button", name="下一步", exact=True).click()
        dialog.locator('[name="progression"]').select_option("结果倒叙")
        execution = "先看结果，再回到准备步骤"
        dialog.locator('[name="execution_change"]').fill(execution)
        concern = dialog.locator('[name="concern"]')
        concern.fill("观众想先看什么")
        dialog.locator("[data-review-concern]").click()
        self.assertEqual(concern.input_value(), "观众想先看什么 → 评论里的装夹顾虑")
        self.assertEqual(dialog.locator('[name="execution_change"]').input_value(), execution)
        dialog.locator("[data-suggestion]").filter(has_text="原顾虑 → 实际接触").click()
        self.assertEqual(concern.input_value(), "原顾虑 → 实际接触")
        self.assertEqual(dialog.locator('[name="execution_change"]').input_value(), execution)
        dialog.locator("[data-chain-point]").filter(has_text="A8").click()
        expected_concern = "原顾虑 → 实际接触 → A8 一次准备，两面继续"
        self.assertEqual(concern.input_value(), expected_concern)
        self.assertEqual(dialog.locator('[name="execution_change"]').input_value(), execution)
        dialog.get_by_role("button", name="下一步", exact=True).click()
        dialog.locator('[name="closing"]').fill("会被已有表达替换的收口")
        dialog.locator("[data-suggestion]").filter(has_text="原收口").click()
        self.assertEqual(dialog.locator('[name="closing"]').input_value(), "原收口")
        dialog.locator("[data-chain-point]").filter(has_text="B9").click()
        expected_closing = "原收口 → B9 检查接触后再决定"
        self.assertEqual(dialog.locator('[name="closing"]').input_value(), expected_closing)
        self.assertIn(expected_closing, dialog.locator(".direction-chain-preview").inner_text())
        dialog.get_by_role("button", name="上一步", exact=True).click()
        self.assertEqual(dialog.locator('[name="execution_change"]').input_value(), execution)
        self.assertEqual(dialog.locator('[name="concern"]').input_value(), expected_concern)
        dialog.get_by_role("button", name="下一步", exact=True).click()
        self.assertEqual(dialog.locator('[name="closing"]').input_value(), expected_closing)
        dialog.get_by_role("button", name="保存方向", exact=True).click()
        dialog.wait_for(state="hidden")
        saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == "202H-S1")
        variant = next(item for item in saved["variants"] if item.get("execution_change") == execution)
        self.assertEqual(variant["concern"], expected_concern)
        self.assertEqual(variant["closing"], expected_closing)
        self.assertEqual(variant["progression"], "结果倒叙")
        self.assertEqual(saved["concern"], "原顾虑 → 实际接触")
        self.assertEqual(self.store.read_case(self.identifier), before)

    def test_global_create_updates_a_and_b_reason_and_existing_context_and_saves_both(self):
        before = self.store.read_case(self.identifier)
        new_s = self.create_point("S2", "第二个独立主打")
        page = self.open_page(width=390)
        for point in (self.point_a, self.point_b):
            with self.subTest(tier=point["tier"]):
                page.get_by_role("button", name="新建方向", exact=True).click()
                dialog = page.locator("#direction-create-dialog")
                dialog.locator('[name="lead_point"]').first.wait_for()
                self.choose_point(dialog, point)
                reason = dialog.locator('[name="lead_reason"]')
                reason.wait_for()
                self.assertEqual(dialog.locator("#direction-create-title").inner_text(), "新建方向")
                self.assertNotIn("这个主打卖点已有方向", dialog.inner_text())
                dialog.get_by_role("button", name="下一步", exact=True).click()
                self.assertIn("不是 S 级卖点", dialog.locator("[role=alert]").inner_text())
                reason_text = f'只测 {point["code"]} 对应的使用环节'
                reason.fill(reason_text)
                self.choose_point(dialog, self.point_s)
                self.assertEqual(dialog.locator('[name="lead_reason"]').count(), 0)
                self.assertEqual(dialog.locator("#direction-create-title").inner_text(), "新增执行变体")
                self.assertIn("这个主打卖点已有方向", dialog.inner_text())
                self.choose_point(dialog, new_s)
                self.assertEqual(dialog.locator('[name="lead_reason"]').count(), 0)
                self.assertEqual(dialog.locator("#direction-create-title").inner_text(), "新建方向")
                self.assertNotIn("这个主打卖点已有方向", dialog.inner_text())
                self.choose_point(dialog, point)
                self.assertEqual(dialog.locator('[name="lead_reason"]').input_value(), reason_text)
                self.assertEqual(dialog.locator("[role=alert]").inner_text(), "")
                dialog.get_by_role("button", name="下一步", exact=True).click()
                concern_text = f'{point["code"]} 的使用条件 → A8 一次准备，两面继续'
                dialog.locator('[name="concern"]').fill(concern_text)
                dialog.get_by_role("button", name="上一步", exact=True).click()
                self.assertTrue(dialog.locator(f'[name="lead_point"][value="{point["id"]}"]').is_checked())
                self.assertEqual(dialog.locator('[name="lead_reason"]').input_value(), reason_text)
                dialog.get_by_role("button", name="下一步", exact=True).click()
                self.assertEqual(dialog.locator('[name="concern"]').input_value(), concern_text)
                dialog.get_by_role("button", name="下一步", exact=True).click()
                dialog.locator('[name="closing"]').fill("B9 检查接触后再决定")
                self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
                dialog.get_by_role("button", name="保存方向", exact=True).click()
                dialog.wait_for(state="hidden")
                page.locator("#direction-definition").wait_for()
                saved = next(item for item in self.catalog()["directions"] if item["direction_id"] == f'202H-{point["code"]}')
                self.assertIn("非 S 主打理由：" + reason_text, saved["notes"])
                self.assertEqual(saved["concern"], concern_text)
                self.assertEqual(saved["closing"], "B9 检查接触后再决定")
                self.assertEqual(saved["selling_point_id"], point["id"])
                self.assertEqual(saved["lead_tier"], point["tier"])
        self.assertEqual(self.store.read_case(self.identifier), before)


if __name__ == "__main__":
    unittest.main()
