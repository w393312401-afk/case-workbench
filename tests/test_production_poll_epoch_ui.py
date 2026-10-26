"""An old background snapshot cannot roll back a newer local carousel mutation."""
import copy
import json
import unittest

from playwright.sync_api import expect

import test_production_carousel_images_ui as image_fixture


class ProductionPollEpochBrowserTests(unittest.TestCase):
    setUpClass = classmethod(image_fixture.CarouselImageBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(image_fixture.CarouselImageBrowserTests.tearDownClass.__func__)
    setUp = image_fixture.CarouselImageBrowserTests.setUp
    stop_server = image_fixture.CarouselImageBrowserTests.stop_server
    request = image_fixture.CarouselImageBrowserTests.request
    open_page = image_fixture.CarouselImageBrowserTests.open_page
    make_playable_video = image_fixture.CarouselImageBrowserTests.make_playable_video
    stop_images = image_fixture.CarouselImageBrowserTests.stop_images
    add_base = image_fixture.CarouselImageBrowserTests.add_base
    open_images = image_fixture.CarouselImageBrowserTests.open_images

    def test_delayed_ended_poll_does_not_restore_a_base_the_user_just_unpicked(self):
        old_finished = self.app.carousel_detail(self.carousel["id"])
        initial = copy.deepcopy(old_finished)
        initial["carousel"]["generations"] = [{"id": "IMG-0123456789ab", "status": "running",
            "slides": [1, 2, 3], "requested_slides": [1, 2, 3], "started_at": "2026-10-14T10:00:00Z", "stages": {}}]
        page = self.open_images()
        delayed = []
        calls = []

        def detail_response(route):
            calls.append(route.request)
            if len(calls) == 1:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(initial))
            elif len(calls) == 2:
                delayed.append(route)
            else:
                route.continue_()

        page.route(f"**/api/production/carousels/{self.carousel['id']}", detail_response)
        page.reload()
        page.locator("[data-cancel-generation]").wait_for()
        # Hold the scheduled poll's older, ended-run snapshot while the user makes a newer change.
        page.wait_for_timeout(3500)
        self.assertEqual(len(delayed), 1)
        page.locator('.production-steps [data-step="pick"]').click()
        page.locator('[data-unpick="1"]').click()
        count = page.locator('.production-steps [data-step="pick"] small')
        expect(count).to_have_text("2/3 已选")
        delayed[0].fulfill(status=200, content_type="application/json", body=json.dumps(old_finished))
        page.wait_for_timeout(200)
        expect(count).to_have_text("2/3 已选")
        expect(page.locator('[data-overview-slide="1"]')).to_have_class("carousel-overview-tile empty")
        # Discarding a stale terminal result must schedule another poll rather than silently stop.
        page.wait_for_timeout(3250)
        self.assertGreaterEqual(len(calls), 4)
        expect(count).to_have_text("2/3 已选")
        self.assertIsNone(self.app.production.carousel(self.carousel["id"])["slides"][0]["base"])


if __name__ == "__main__":
    unittest.main()
