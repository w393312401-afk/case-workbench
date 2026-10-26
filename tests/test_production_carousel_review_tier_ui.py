"""The independent check splits a group into 核对全通过 and 需人看; one-click confirm never covers slides with hints."""
import json
import unittest

import test_production_carousel_finished_ui as finished_fixture


def passing_review():
    checks = [{"id": key, "status": "pass", "note": ""} for key in ("product", "evidence", "text", "visual")]
    return {"status": "pass", "completed": True, "summary": "", "issues": [], "checks": checks}


class CarouselReviewTierBrowserTests(unittest.TestCase):
    setUpClass = classmethod(finished_fixture.CarouselFinishedBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(finished_fixture.CarouselFinishedBrowserTests.tearDownClass.__func__)
    setUp = finished_fixture.CarouselFinishedBrowserTests.setUp
    stop_server = finished_fixture.CarouselFinishedBrowserTests.stop_server
    request = finished_fixture.CarouselFinishedBrowserTests.request
    open_page = finished_fixture.CarouselFinishedBrowserTests.open_page
    make_playable_video = finished_fixture.CarouselFinishedBrowserTests.make_playable_video
    make_finished = finished_fixture.CarouselFinishedBrowserTests.make_finished
    import_ui = finished_fixture.CarouselFinishedBrowserTests.import_ui
    confirm_ui = finished_fixture.CarouselFinishedBrowserTests.confirm_ui
    detail = finished_fixture.CarouselFinishedBrowserTests.detail

    def with_reviews(self, page, reviews):
        """Serve the carousel with a chosen review per slide number (None keeps the stored record)."""
        endpoint = f"/api/production/carousels/{self.carousel['id']}"

        def respond(route):
            payload = self.request('GET', endpoint)[2]
            for slide in payload['carousel']['slides']:
                review = reviews.get(slide['number'])
                if review is not None:
                    for output in slide.get('outputs', []):
                        output['review'] = review
            route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

        page.route('**' + endpoint, respond)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}?step=images")
        page.reload()
        page.locator('#carousel-tier').wait_for(state='attached')

    def test_all_passing_group_is_one_click_and_marked_clean(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        count = len(self.detail()['slides'])
        self.with_reviews(page, {number: passing_review() for number in range(1, count + 1)})
        tier = page.locator('#carousel-tier')
        self.assertTrue(tier.is_visible())
        self.assertEqual(tier.get_attribute('data-tier'), 'clean')
        self.assertEqual(page.locator('[data-review-tier="clean"]').count(), count)
        self.assertEqual(page.locator('#carousel-approve-all').inner_text(), f'一键全部选用（{count} 张）')

    def test_slide_with_hint_is_left_out_of_the_one_click_confirm(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        count = len(self.detail()['slides'])
        hinted = {**passing_review(), "status": "pass"}
        hinted["checks"] = [{**check, "status": "uncertain"} if check["id"] == "evidence" else check for check in hinted["checks"]]
        reviews = {number: passing_review() for number in range(1, count + 1)}
        reviews[2] = hinted
        self.with_reviews(page, reviews)
        tier = page.locator('#carousel-tier')
        self.assertEqual(tier.get_attribute('data-tier'), 'attention')
        self.assertIn('第 2 张', tier.inner_text())
        self.assertEqual(page.locator('[data-image-slide="2"]').get_attribute('data-review-tier'), 'attention')
        self.assertEqual(page.locator('#carousel-approve-all').inner_text(), f'确认核对通过的（{count - 1} 张）')
        page.locator('#carousel-approve-all').click()
        page.wait_for_function("() => document.querySelector('#carousel-approve-all').textContent.includes('逐张确认')")
        approved = {slide['number']: bool(slide['outputs'][-1].get('approved')) for slide in self.detail()['slides']}
        self.assertFalse(approved[2])
        self.assertTrue(all(value for number, value in approved.items() if number != 2))

    def test_slides_without_a_check_keep_the_plain_one_click_and_show_no_banner(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        count = len(self.detail()['slides'])
        self.with_reviews(page, {})
        self.assertFalse(page.locator('#carousel-tier').is_visible())
        self.assertEqual(page.locator('[data-review-tier="unchecked"]').count(), count)
        self.assertEqual(page.locator('#carousel-approve-all').inner_text(), f'一键全部选用（{count} 张）')

    def test_unfinished_check_counts_as_needing_a_person(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        count = len(self.detail()['slides'])
        broken = {"status": "uncertain", "completed": False, "summary": "自动核对没有返回完整结果", "issues": ["x"], "checks": []}
        self.with_reviews(page, {number: broken for number in range(1, count + 1)})
        self.assertEqual(page.locator('#carousel-tier').get_attribute('data-tier'), 'attention')
        self.assertTrue(page.locator('#carousel-approve-all').is_disabled())

if __name__ == '__main__':
    unittest.main()
