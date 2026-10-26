"""Content filters survive quiet refreshes after the former script list redirects to content."""
from __future__ import annotations

import unittest

import test_phase4_writing_ui as writing_fixture
import test_server as fixture
from test_content_flow_ui import content_filter, set_content_condition


class ScriptRefreshNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(writing_fixture.WritingLifecycleBrowserTests.tearDownClass.__func__)
    setUp = writing_fixture.WritingLifecycleBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    detail = fixture.WorkbenchHTTPTests.detail
    catalog = writing_fixture.WritingLifecycleBrowserTests.catalog
    create_script = writing_fixture.WritingLifecycleBrowserTests.create_script
    open_page = writing_fixture.WritingLifecycleBrowserTests.open_page

    def test_module_and_main_router_quiet_refreshes_preserve_mounted_filters(self):
        self.create_script('Unique refresh fixture.')
        page = self.open_page("#/202H/scripts")
        page.locator('#content-flow-query').fill('临时脚本')
        set_content_condition(page, 'kind', ['script'])
        expected_filter = content_filter(page)
        for refresh in ('ContentFlow.route(true)', 'route(true,true)'):
            with self.subTest(refresh=refresh):
                page.evaluate('async () => await ' + refresh)
                self.assertEqual(page.locator('#content-flow-query').input_value(), '临时脚本')
                self.assertEqual(content_filter(page), expected_filter)
                self.assertEqual(page.locator('#content-flow-list .flow-row').count(), 1)
        page.evaluate("async () => { history.replaceState(null, '', '#/202H/content'); await ContentFlow.route(false); }")
        self.assertEqual(page.locator('#content-flow-query').input_value(), '')
        self.assertNotIn('filter=', page.evaluate('location.hash'))
        self.assertEqual(page.locator('[data-flow-view="attention"]').get_attribute('aria-pressed'), 'true')


if __name__ == '__main__':
    unittest.main()
