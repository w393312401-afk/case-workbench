"""Multiple image versions collapse without hiding selection or result-link actions."""
import json
import unittest

from PIL import Image

import test_production_carousel_finished_ui as finished_fixture


class CarouselImageStackBrowserTests(unittest.TestCase):
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

    def import_versions(self, page):
        self.import_ui(page, self.folder)
        self.import_ui(page, self.make_finished("stack-second", offset=8))
        self.import_ui(page, self.make_finished("stack-third", offset=16))
        return self.detail()["slides"][0]["outputs"]

    def test_single_image_has_no_stack_switch_and_still_opens_lightbox(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        self.assertEqual(page.locator('[data-image-versions-toggle]').count(), 0)
        self.assertEqual(page.locator('.carousel-output:visible').count(), 3)
        page.locator('[data-image-slide="1"] .carousel-output>a').click()
        page.locator('dialog.image-lightbox[open] img').wait_for()
        self.assertIn('/outputs/', page.locator('dialog.image-lightbox img').get_attribute('src'))
        page.locator('dialog.image-lightbox [data-close]').click()
        self.assertEqual(page.locator('dialog.image-lightbox').count(), 0)
        self.assertEqual(self.reviewer.calls, [])
        self.assertEqual(self.detail()['generations'], [])

    def test_stack_shows_latest_then_confirmed_image_and_can_select_older_version(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.set_viewport_size({"width": 390, "height": 844})
        outputs = self.import_versions(page)
        tile = page.locator('[data-image-slide="1"]')
        toggle = tile.locator('[data-image-versions-toggle]')
        front = tile.locator('.image-stack-cover [data-output-id]')
        self.assertEqual(front.get_attribute('data-output-id'), outputs[-1]['id'])
        self.assertEqual(front.locator('img').get_attribute('alt'), '第 1 张成图第 3 版')
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 1)
        self.assertEqual(toggle.inner_text(), '查看其他图片（2）')
        self.assertEqual(toggle.get_attribute('aria-expanded'), 'false')
        self.assertEqual(tile.locator('.image-version-others').evaluate('node => node.getBoundingClientRect().height'), 0)
        self.assertNotEqual(tile.locator('.image-stack-cover').evaluate('node => getComputedStyle(node, "::before").content'), 'none')
        self.assertNotEqual(tile.locator('.image-stack-cover').evaluate('node => getComputedStyle(node, "::after").content'), 'none')
        toggle.click()
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 3)
        self.assertEqual(toggle.get_attribute('aria-expanded'), 'true')
        self.assertEqual(toggle.inner_text(), '收起')
        toggle.click()
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 1)
        toggle.click()
        self.confirm_ui(page, outputs[0]['id'])
        tile = page.locator('[data-image-slide="1"]')
        self.assertEqual(tile.locator('.image-stack-cover [data-output-id]').get_attribute('data-output-id'), outputs[0]['id'])
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 1)
        self.assertEqual(tile.locator('[data-image-versions-toggle]').get_attribute('aria-expanded'), 'false')
        page.reload()
        page.locator('[data-step="images"]').click()
        self.assertEqual(page.locator('[data-image-slide="1"] .image-stack-cover [data-output-id]').get_attribute('data-output-id'), outputs[0]['id'])
        page.locator('[data-image-slide="1"] [data-image-versions-toggle]').click()
        self.confirm_ui(page, outputs[1]['id'])
        self.assertEqual(self.detail()['slides'][0]['output_id'], outputs[1]['id'])
        self.assertEqual(page.locator('[data-image-slide="1"] .carousel-output:visible').count(), 1)
        self.assertEqual(page.locator('.carousel-output figcaption,.carousel-output details,.carousel-output p,[data-recheck-output]').count(), 0)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)
        self.assertEqual(self.reviewer.calls, [])

    def test_result_link_reveals_a_hidden_older_image_and_preserves_expansion(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        outputs = self.import_versions(page)
        older = outputs[0]
        run_id = 'IMG-stack-result'
        endpoint = f"/api/production/carousels/{self.carousel['id']}"

        def response_with_source(route):
            payload = self.request('GET', endpoint)[2]
            for slide in payload['carousel']['slides']:
                for output in slide.get('outputs', []):
                    if output['id'] == older['id']:
                        output['generation_id'] = run_id
            route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

        page.route('**' + endpoint, response_with_source)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}?step=images&run={run_id}")
        page.wait_for_function('id => document.activeElement?.dataset.outputId === id', arg=older['id'])
        tile = page.locator('[data-image-slide="1"]')
        self.assertTrue(tile.locator(f'[data-output-id="{older["id"]}"]').is_visible())
        self.assertEqual(tile.locator('[data-image-versions-toggle]').get_attribute('aria-expanded'), 'true')
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 3)
        self.assertEqual(page.locator('.task-activity-logs details[open]').count(), 0)
        page.locator('[data-step="pick"]').click()
        page.locator('[data-step="images"]').click()
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 3)
        tile.locator('[data-image-versions-toggle]').click()
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 1)
        self.assertEqual(self.reviewer.calls, [])

    def test_changed_confirmed_file_falls_back_to_latest_valid_image(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        outputs = self.import_versions(page)
        page.locator('[data-image-slide="1"] [data-image-versions-toggle]').click()
        self.confirm_ui(page, outputs[0]['id'])
        path = self.app._carousel_dir(self.carousel['id']) / outputs[0]['file']
        Image.new('RGB', (180, 320), 'tomato').save(path)
        page.reload()
        page.locator('[data-step="images"]').click()
        tile = page.locator('[data-image-slide="1"]')
        self.assertEqual(tile.locator('.image-stack-cover [data-output-id]').get_attribute('data-output-id'), outputs[-1]['id'])
        tile.locator('[data-image-versions-toggle]').click()
        changed = tile.locator(f'[data-output-id="{outputs[0]["id"]}"]')
        self.assertTrue(changed.is_visible())
        self.assertIn('stale', changed.get_attribute('class'))
        self.assertTrue(changed.locator('button').is_disabled())
        self.assertEqual(self.reviewer.calls, [])


if __name__ == '__main__':
    unittest.main()
