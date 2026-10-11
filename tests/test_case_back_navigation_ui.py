"""Case return actions share the library scope without scrolling a new explicit scope."""
from __future__ import annotations

import unittest

import test_shell_ui as shell_fixture
import test_ui_regressions as browser_fixture
import test_server as fixture


class CaseBackNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(shell_fixture.ShellBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(shell_fixture.ShellBrowserTests.tearDownClass.__func__)
    setUp = shell_fixture.ShellBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser_fixture.WorkbenchBrowserTests.make_playable_video
    open_page = shell_fixture.ShellBrowserTests.open_page

    def filtered_library(self):
        page = self.open_page('#/202H/library')
        page.locator('#case-search').fill('系统')
        browser_fixture.open_library_filters(page)
        page.locator('#direction-filter').select_option('202H-S1')
        page.locator('#variant-filter').select_option('direction')
        page.locator('#sort-filter').select_option('views')
        page.locator('#sort-direction').select_option('asc')
        browser_fixture.close_library_filters(page)
        page.evaluate("""() => {
            const sheet = document.styleSheets[0];
            sheet.insertRule('#case-results { padding-top: 850px !important; padding-bottom: 1500px !important; }', sheet.cssRules.length);
        }""")
        return page

    def open_case(self, page):
        card = page.locator(f'.case-card[href="#case/{self.identifier}"]')
        card.scroll_into_view_if_needed()
        page.wait_for_function('window.scrollY > 300')
        scroll = page.evaluate('window.scrollY')
        card.click()
        page.locator('.case-dock-back').wait_for()
        return scroll

    def test_page_breadcrumb_top_and_escape_preserve_filters_sort_and_scroll(self):
        page = self.filtered_library()
        list_hash = page.evaluate('location.hash')
        for action in ('page', 'breadcrumb', 'top', 'escape'):
            with self.subTest(action=action):
                scroll = self.open_case(page)
                parent = page.locator('#breadcrumb').get_by_role('link', name='案例库', exact=True)
                for link in (parent, page.locator('#navigate-up'), page.locator('.case-dock-back')):
                    self.assertEqual(link.get_attribute('href'), list_hash)
                if action == 'page':
                    page.locator('.case-dock-back').click()
                elif action == 'breadcrumb':
                    parent.click()
                elif action == 'top':
                    page.locator('#navigate-up').click()
                else:
                    page.locator('.case-dock-title').click()
                    page.keyboard.press('Escape')
                page.locator('#case-search').wait_for()
                page.wait_for_function('expected => location.hash === expected', arg=list_hash)
                page.wait_for_function('expected => Math.abs(window.scrollY - expected) <= 2', arg=scroll)
                self.assertEqual(page.locator('#case-search').input_value(), '系统')
                self.assertEqual(page.locator('#direction-filter').input_value(), '202H-S1')
                self.assertEqual(page.locator('#variant-filter').input_value(), 'direction')
                self.assertEqual(page.locator('#sort-filter').input_value(), 'views')
                self.assertEqual(page.locator('#sort-direction').input_value(), 'asc')

    def test_explicit_different_library_scope_starts_at_top(self):
        page = self.filtered_library()
        scroll = self.open_case(page)
        self.assertGreater(scroll, 300)
        destination = '#/202H/library?q=no-matching-case&sort=updated'
        page.evaluate('next => { location.hash = next; }', destination)
        page.locator('#case-search').wait_for()
        page.wait_for_function('expected => location.hash === expected', arg=destination)
        page.wait_for_function('window.scrollY === 0')
        self.assertEqual(page.locator('#case-search').input_value(), 'no-matching-case')
        self.assertEqual(page.locator('#sort-filter').input_value(), 'updated')
        self.assertEqual(page.locator('#direction-filter').input_value(), '')


if __name__ == '__main__':
    unittest.main()
