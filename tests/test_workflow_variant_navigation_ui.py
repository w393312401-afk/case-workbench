"""Workflow links locate execution output by stable identity, using disposable UI data."""
import copy
import unittest
from urllib.parse import quote

from direction_progress import variant_key
import test_direction_workflow_ui as workflow_ui


class WorkflowVariantNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(workflow_ui.DirectionWorkflowBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(workflow_ui.DirectionWorkflowBrowserTests.tearDownClass.__func__)
    setUp = workflow_ui.DirectionWorkflowBrowserTests.setUp
    stop_server = workflow_ui.DirectionWorkflowBrowserTests.stop_server
    request = workflow_ui.DirectionWorkflowBrowserTests.request
    make_playable_video = workflow_ui.DirectionWorkflowBrowserTests.make_playable_video
    catalog = workflow_ui.DirectionWorkflowBrowserTests.catalog
    create_point = workflow_ui.DirectionWorkflowBrowserTests.create_point
    open_page = workflow_ui.DirectionWorkflowBrowserTests.open_page
    detail_data = workflow_ui.DirectionWorkflowBrowserTests.detail_data
    progress_data = workflow_ui.DirectionWorkflowBrowserTests.progress_data
    wait_for_progress = workflow_ui.DirectionWorkflowBrowserTests.wait_for_progress
    wait_for_held = workflow_ui.DirectionWorkflowBrowserTests.wait_for_held
    detail_pattern = workflow_ui.DirectionWorkflowBrowserTests.detail_pattern
    progress_pattern = workflow_ui.DirectionWorkflowBrowserTests.progress_pattern

    def workflow_data(self):
        data = self.detail_data()
        original = data["direction"]["variants"][0]
        data["direction"]["variants"] = [
            {**copy.deepcopy(original), "concern": f"第 {index + 1} 稿的顾虑", "baseline": index == 0,
             "variant_type": "execution", "execution_change": f"第 {index + 1} 稿的改动"}
            for index in range(3)
        ]
        progress = self.progress_data(data)
        for row in progress["variants"]:
            row["key"] = variant_key(row["snapshot"])
        return data, progress

    def show_link(self, page, data, query, progress=None):
        page.route(self.detail_pattern, lambda route: route.fulfill(status=200, json=copy.deepcopy(data)))
        if progress is not None:
            page.route(self.progress_pattern, lambda route: route.fulfill(status=200, json=copy.deepcopy(progress)))
        page.evaluate("target => {location.hash=target;}", "#/202H/directions/202H-S1?" + query)
        page.locator("#direction-definition").wait_for()

    def test_link_waits_for_verified_progress_then_opens_only_exact_variant_output(self):
        page = self.open_page(width=390)
        data, progress = self.workflow_data()
        key = progress["variants"][2]["key"]
        held = []
        page.route(self.progress_pattern, lambda route: held.append(route))
        self.show_link(page, data, "variant=" + quote(key))
        self.wait_for_held(page, held)
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 0)
        held[0].fulfill(status=200, json=progress)
        self.wait_for_progress(page)
        details = page.locator('details.dir-variant[data-variant-index="2"]')
        self.assertIsNotNone(details.get_attribute("open"))
        self.assertEqual(details.get_attribute("data-variant-key"), key)
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 1)
        self.assertTrue(details.locator('[data-variant-records="2"]').is_visible())
        self.assertTrue(details.locator(":scope > summary").evaluate("node => node === document.activeElement"))
        self.assertFalse(page.locator("#direction-workflow-target-note").is_visible())
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth>document.documentElement.clientWidth"))
        self.assertEqual(self.server.app.ai.list_tasks(), [])

    def test_stable_key_follows_catalog_reordering_and_structure_annotations(self):
        page = self.open_page()
        data, progress = self.workflow_data()
        key = progress["variants"][0]["key"]
        data["direction"]["variants"] = [data["direction"]["variants"][2],
                                          data["direction"]["variants"][0],
                                          data["direction"]["variants"][1]]
        data["direction"]["variants"][1].update(baseline=False, progression="结果倒叙")
        progress = self.progress_data(data)
        for row in progress["variants"]:
            row["key"] = variant_key(row["snapshot"])
        self.show_link(page, data, "variant=" + key, progress)
        self.wait_for_progress(page)
        self.assertEqual(page.locator("details.dir-variant[open]").count(), 1)
        self.assertIsNotNone(page.locator('details.dir-variant[data-variant-index="1"]').get_attribute("open"))
        self.assertEqual(page.locator('details.dir-variant[data-variant-index="1"]').get_attribute("data-variant-key"), key)

    def test_missing_ambiguous_or_conflicted_key_never_opens_another_variant(self):
        for kind in ("missing", "numeric", "duplicate", "conflict"):
            with self.subTest(kind=kind):
                page = self.open_page()
                data, progress = self.workflow_data()
                key = progress["variants"][0]["key"]
                if kind == "missing":
                    key = "removed-variant-key"
                elif kind == "numeric":
                    key = "0"
                elif kind == "duplicate":
                    progress["variants"][1]["key"] = key
                else:
                    data["direction"]["variants"][0]["conflict"] = True
                self.show_link(page, data, "variant=" + key, progress)
                self.wait_for_progress(page)
                self.assertEqual(page.locator("details.dir-variant[open]").count(), 0)
                notice = page.locator("#direction-workflow-target-note")
                self.assertTrue(notice.is_visible())
                self.assertIn("核对", notice.inner_text())

    def test_plan_link_reveals_exact_historical_test_card_without_opening_editor(self):
        page = self.open_page()
        data, progress = self.workflow_data()
        data["plans"] = [{"id": "PLN-workflow-ui", "product": "202H", "direction_id": "202H-S1",
                          "action": "execution", "status": "done", "experiment": {
                              "hypothesis": "检查历史测试卡定位", "change": "改开头", "metric": "orders",
                              "baseline_video_id": "111111", "test_video_id": "222222",
                              "window_start": "2026-10-01", "window_end": "2026-10-02", "decision": "observe"}}]
        self.show_link(page, data, "plan=PLN-workflow-ui", progress)
        self.wait_for_progress(page)
        self.assertIsNotNone(page.locator("details.direction-tests-history").get_attribute("open"))
        target = page.locator('[data-test-result="PLN-workflow-ui"]')
        self.assertTrue(target.is_visible())
        self.assertTrue(target.evaluate("node => node === document.activeElement"))
        self.assertEqual(page.locator("#direction-plan-dialog[open]").count(), 0)
        self.assertFalse(page.locator("#direction-workflow-target-note").is_visible())


if __name__ == "__main__":
    unittest.main()
