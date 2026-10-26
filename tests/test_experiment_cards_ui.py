"""Direction test card workflow against a disposable workbench."""
import unittest

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class ExperimentCardBrowserTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    def test_create_edit_decision_and_pending_comparison_on_mobile(self):
        page = self.open_page("#/202H/directions/202H-S1", width=390)
        page.locator("#direction-test-create").click()
        page.locator('#direction-plan-dialog [name="hypothesis"]').fill("先演示稳定性可提高下单率")
        page.locator('#direction-plan-dialog [name="change"]').fill("首段由介绍改为装夹演示")
        page.get_by_role("button", name="保存测试卡", exact=True).click()
        page.locator("#direction-plan-dialog").wait_for(state="hidden")
        page.locator(".direction-test").get_by_text("先演示稳定性可提高下单率", exact=True).wait_for()
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))
        page.locator("[data-test-edit]").click()
        page.locator('#direction-plan-dialog [name="decision"]').select_option("scale")
        page.locator('#direction-plan-dialog [name="decision_note"]').fill("人工检查后决定先增加一条样本")
        page.get_by_role("button", name="保存测试卡", exact=True).click()
        page.locator("#direction-plan-dialog").wait_for(state="hidden")
        page.locator(".direction-test-facts").get_by_text("加码 · 人工检查后决定先增加一条样本", exact=True).wait_for()
        page.reload()
        page.locator(".direction-test-facts").get_by_text("加码 · 人工检查后决定先增加一条样本", exact=True).wait_for()
        page.locator("[data-test-result]").click()
        page.locator("[data-test-comparison]").get_by_text("尚不能比较", exact=True).wait_for()
        self.assertIn("修改记录（2 次）", page.locator(".direction-test-history").inner_text())
        status, _, result = self.request("GET", "/api/creation?product=202H")
        self.assertEqual(status, 200)
        self.assertEqual(result["plans"][0]["experiment"]["decision"], "scale")


if __name__ == "__main__":
    unittest.main()
