"""Script filters survive quiet refreshes while a fresh list starts with defaults."""
from __future__ import annotations

import unittest

import test_phase4_writing_ui as writing_fixture
import test_server as fixture


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
        page = self.open_page()
        page.locator('#script-query').fill('Unique refresh')
        page.locator('#script-status-filter').select_option('adopted')
        for refresh in ('CaseScripts.route(true)', 'route(true,true)'):
            with self.subTest(refresh=refresh):
                page.evaluate('async () => await ' + refresh)
                self.assertEqual(page.locator('#script-query').input_value(), 'Unique refresh')
                self.assertEqual(page.locator('#script-status-filter').input_value(), 'adopted')
                self.assertEqual(page.locator('#script-list .script-card').count(), 1)
        page.evaluate('async () => await CaseScripts.route(false)')
        self.assertEqual(page.locator('#script-query').input_value(), '')
        self.assertEqual(page.locator('#script-status-filter').input_value(), '')


if __name__ == '__main__':
    unittest.main()
