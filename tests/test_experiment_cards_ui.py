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

    def test_direction_page_no_longer_offers_test_cards(self):
        # Test cards were never used and left the direction page on 2026-11-22; the API and stored cards stay.
        page = self.open_page("#/202H/directions/202H-S1", width=390)
        self.assertEqual(page.locator("#direction-test-create, #direction-tests, [data-test-edit]").count(), 0)
        # The next-round judgement is one row of choices on the direction, not a menu.
        self.assertEqual(page.locator("#direction-plans [data-direction-plan]").all_inner_texts(), ["加码", "换执行", "观察", "停测"])
        self.assertFalse(page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth"))


if __name__ == "__main__":
    unittest.main()
