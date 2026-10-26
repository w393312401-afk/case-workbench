"""List pages remember their own filters across browser visits."""
from __future__ import annotations

import unittest
from urllib.parse import parse_qs

import test_direction_catalog_ui as catalog_ui
import test_server as fixture


class ListFilterMemoryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(catalog_ui.DirectionCatalogBrowserTests.tearDownClass.__func__)
    setUp = catalog_ui.DirectionCatalogBrowserTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = catalog_ui.DirectionCatalogBrowserTests.make_playable_video
    catalog = catalog_ui.DirectionCatalogBrowserTests.catalog
    create_point = catalog_ui.DirectionCatalogBrowserTests.create_point
    open_page = catalog_ui.DirectionCatalogBrowserTests.open_page

    filtered_list = "#/202H/directions?q=%E5%AE%9A%E8%A7%92&tier=S&status=testing&archive=all"

    def fresh_visit(self, page, route="#/202H/directions"):
        """A new tab in the same browser profile, as after closing and reopening the workbench."""
        page.close()
        other = page.context.new_page()
        other.set_default_timeout(10000)
        other.goto(f"http://127.0.0.1:{self.server.server_port}/{route}")
        other.locator("#direction-filters").wait_for()
        return other

    def query(self, page):
        return parse_qs(page.evaluate("location.hash").partition("?")[2])

    def test_filters_return_after_leaving_the_list_and_reopening(self):
        page = self.open_page(self.filtered_list)
        page.evaluate("location.hash = '#/202H/home'")
        page.wait_for_function("location.hash.startsWith('#/202H/home')")
        again = self.fresh_visit(page)
        self.assertEqual(self.query(again), parse_qs(self.filtered_list.partition("?")[2]))
        self.assertEqual(again.locator('#direction-filters [name="tier"]').input_value(), "S")

    def test_filters_survive_closing_the_tab_while_still_on_the_list(self):
        page = self.open_page(self.filtered_list)
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        again = self.fresh_visit(page)
        self.assertEqual(self.query(again), parse_qs(self.filtered_list.partition("?")[2]))

    def test_an_explicit_address_with_its_own_filters_wins_over_memory(self):
        page = self.open_page(self.filtered_list)
        page.evaluate("window.dispatchEvent(new Event('pagehide'))")
        again = self.fresh_visit(page, "#/202H/directions?tier=A")
        self.assertEqual(self.query(again), {"tier": ["A"]})


if __name__ == "__main__":
    unittest.main()
