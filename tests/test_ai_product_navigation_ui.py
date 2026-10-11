"""Mounted AI lists restore product-specific filters without retaining selections."""
from __future__ import annotations

import unittest

import test_ai_selection_ui as ai_fixture
import test_server as fixture


class AIProductNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(ai_fixture.AISelectionBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(ai_fixture.AISelectionBrowserTests.tearDownClass.__func__)
    setUp = ai_fixture.AISelectionBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    create_task = ai_fixture.AISelectionBrowserTests.create_task
    open_ai_list = ai_fixture.AISelectionBrowserTests.open_ai_list

    def test_product_switch_restores_its_filters_and_clears_selection_without_poll_resets(self):
        task = self.create_task("临时产品导航测试")
        page = self.open_ai_list()
        page.locator(f'[data-ai-task-id="{task["task_id"]}"]').wait_for()
        page.evaluate("WorkbenchShell.registerProducts(['129N'])")
        page.locator('[data-ai-filter="completed"]').click()
        page.locator('#ai-group-by').select_option('case')
        page.locator('#ai-select-toggle').click()
        page.locator(f'[data-ai-task-id="{task["task_id"]}"] input').check()
        self.assertEqual(page.locator('#ai-selected-count').inner_text(), '已选 1 条')

        page.locator('#global-product').select_option('129N')
        page.wait_for_function("location.hash === '#/129N/ai'")
        page.wait_for_function("document.querySelector('[data-ai-filter=all]').getAttribute('aria-pressed') === 'true'")
        self.assertEqual(page.locator('#ai-group-by').input_value(), 'direction')
        self.assertEqual(page.locator('#ai-select-toggle').get_attribute('aria-pressed'), 'false')
        self.assertFalse(page.locator('#ai-selection-toolbar').is_visible())
        page.locator('[data-ai-filter="active"]').click()

        page.locator('#global-product').select_option('202H')
        page.locator(f'[data-ai-task-id="{task["task_id"]}"]').wait_for()
        self.assertEqual(page.locator('[data-ai-filter="completed"]').get_attribute('aria-pressed'), 'true')
        self.assertEqual(page.locator('#ai-group-by').input_value(), 'case')
        self.assertEqual(page.locator('#ai-selected-count').inner_text(), '已选 0 条')
        self.assertFalse(page.locator(f'[data-ai-task-id="{task["task_id"]}"] input').is_checked())
        self.assertFalse(page.locator('#content').evaluate("node => node.classList.contains('ai-selecting')"))

        page.locator('[data-ai-filter="stopped"]').click()
        page.locator('#ai-group-by').select_option('direction')
        page.wait_for_timeout(2700)
        self.assertEqual(page.locator('[data-ai-filter="stopped"]').get_attribute('aria-pressed'), 'true')
        self.assertEqual(page.locator('#ai-group-by').input_value(), 'direction')


if __name__ == '__main__':
    unittest.main()
