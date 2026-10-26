"""Single slide selection, comparisons, review keys and galleries in the real UI."""
from __future__ import annotations

import json
import unittest

import test_production_carousel_images_ui as fixture


class CarouselReviewControlsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CarouselImageBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CarouselImageBrowserTests.tearDownClass.__func__)
    setUp = fixture.CarouselImageBrowserTests.setUp
    stop_server = fixture.CarouselImageBrowserTests.stop_server
    request = fixture.CarouselImageBrowserTests.request
    open_page = fixture.CarouselImageBrowserTests.open_page
    make_playable_video = fixture.CarouselImageBrowserTests.make_playable_video
    add_base = fixture.CarouselImageBrowserTests.add_base
    stop_images = fixture.CarouselImageBrowserTests.stop_images
    open_images = fixture.CarouselImageBrowserTests.open_images
    generate = fixture.CarouselImageBrowserTests.generate
    detail = fixture.CarouselImageBrowserTests.detail

    def select(self, page, numbers):
        for checkbox in page.locator('[data-redo-pick]').all():
            checkbox.set_checked(int(checkbox.get_attribute('data-redo-pick')) in numbers)

    def watch_csp(self, page):
        page.evaluate("""() => {
            window.carouselCsp = [];
            document.addEventListener('securitypolicyviolation', event =>
                window.carouselCsp.push(event.violatedDirective));
        }""")

    def test_one_selection_set_drives_mixed_footer_actions_and_local_confirmation(self):
        page = self.open_images()
        self.assertEqual(page.locator('input[name="image_slide"]').count(), 0)
        self.assertEqual(page.locator('[data-redo-pick]').count(), 3)
        self.select(page, [1])
        self.assertEqual(page.locator('#carousel-generate-picked').inner_text(), '生成 1 张')
        self.generate(page, slides=[1])
        self.select(page, [1, 2])
        self.assertEqual(page.locator('#carousel-generate-picked').inner_text(), '生成 1 张')
        self.assertEqual(page.locator('#carousel-redo-picked').inner_text(), '重做 1 张')
        self.assertEqual(page.locator('#carousel-approve-all').inner_text(), '确认 1 张')
        page.locator('#carousel-requirements-toggle>summary').click()
        page.locator('#carousel-image-form [name="instruction"]').fill('保留本次填写的额外要求')
        page.evaluate("window.savedCarouselForm = document.querySelector('#carousel-image-form')")
        status_reads = []
        page.on('request', lambda request: status_reads.append(request.url) if request.url.endswith('/api/production/codex-images') else None)
        page.locator('#carousel-approve-all').click()
        page.locator('[data-image-slide="1"] [data-revoke-output]').wait_for()
        self.assertEqual(self.detail()['slides'][0]['status'], 'approved')
        self.assertEqual(len(self.detail()['slides'][1]['outputs']), 0)
        self.assertTrue(page.evaluate("document.querySelector('#carousel-image-form') === window.savedCarouselForm"))
        self.assertEqual(page.locator('#carousel-image-form [name="instruction"]').input_value(), '保留本次填写的额外要求')
        self.assertTrue(page.locator('#carousel-requirements-toggle').evaluate('node => node.open'))
        self.assertEqual(page.locator('[data-redo-pick]:checked').count(), 2)
        self.assertEqual(status_reads, [], 'Card confirmation must retain the existing service binding')
        self.select(page, [])
        self.assertFalse(page.locator('#carousel-generate-picked').is_visible())

    def test_comparison_binds_its_actual_base_and_fits_mobile_and_desktop_without_csp(self):
        page = self.open_images()
        self.generate(page)
        self.watch_csp(page)
        output = self.detail()['slides'][0]['outputs'][0]
        card = page.locator(f'[data-output-id="{output["id"]}"]')
        for width in (1080, 390):
            page.set_viewport_size({'width': width, 'height': 1100})
            toggle = card.locator('[data-image-compare]')
            if toggle.get_attribute('aria-expanded') != 'true':
                toggle.click()
            page.wait_for_function("() => [...document.querySelectorAll('.carousel-output.comparing img')].every(image => image.complete && image.naturalWidth > 0)")
            self.assertIn(output['base_id'], card.locator('.image-compare-base a').get_attribute('href'))
            base, result = card.locator('.image-compare-base').bounding_box(), card.locator(':scope>a').bounding_box()
            self.assertLess(base['x'], result['x'])
            self.assertGreater(base['width'], 80)
            self.assertGreater(result['width'], 80)
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width + 1)
        page.locator('[data-image-slide="1"] [data-approve-output]').click()
        page.locator('[data-image-slide="1"] [data-revoke-output]').wait_for()
        self.assertEqual(card.locator('[data-image-compare]').get_attribute('aria-expanded'), 'true')
        self.assertTrue(card.locator('.image-compare-base').is_visible())
        card.locator('[data-image-compare]').click()
        self.assertFalse(card.locator('.image-compare-base').is_visible())
        self.assertEqual(page.evaluate('window.carouselCsp'), [])

    def test_missing_base_is_explicit_and_does_not_offer_a_false_comparison(self):
        page = self.open_images()
        self.generate(page, slides=[1])
        endpoint = f'/api/production/carousels/{self.carousel["id"]}'

        def no_original_base(route):
            payload = self.request('GET', endpoint)[2]
            payload['carousel']['slides'][0].update(base=None, candidates=[])
            route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))

        page.route('**' + endpoint, no_original_base)
        page.reload()
        page.locator('[data-step="images"]').click()
        toggle = page.locator('[data-image-slide="1"] [data-image-compare]')
        self.assertTrue(toggle.is_disabled())
        self.assertEqual(toggle.inner_text(), '无底图可对比')
        self.assertIn('没有可用的底图', toggle.get_attribute('title'))
        self.assertEqual(page.locator('.image-compare-base img').count(), 0)

    def test_review_keys_ignore_typing_dialogs_and_other_steps(self):
        page = self.open_images()
        self.generate(page)
        page.locator('[data-image-slide="1"]').focus()
        page.keyboard.press('j')
        self.assertEqual(page.evaluate('document.activeElement.dataset.imageSlide'), '2')
        page.keyboard.press('Enter')
        page.locator('[data-image-slide="2"] [data-revoke-output]').wait_for()
        page.locator('#carousel-image-form [name="instruction"]').focus()
        page.keyboard.type('jkr')
        self.assertEqual(page.locator('.image-slide.keyboard-current').get_attribute('data-image-slide'), '2')
        self.assertEqual(page.locator('.production-dialog[open]').count(), 0)
        page.locator('[data-image-slide="2"]').focus()
        page.keyboard.press('r')
        modal = page.locator('.production-dialog[open]')
        modal.wait_for()
        self.assertIn('第 2 张', modal.inner_text())
        modal.locator('[name="instruction"]').focus()
        page.keyboard.type('jk')
        self.assertEqual(page.locator('.image-slide.keyboard-current').get_attribute('data-image-slide'), '2')
        page.keyboard.press('Escape')
        modal.wait_for(state='detached')
        page.locator('[data-step="pick"]').click()
        page.keyboard.press('j')
        self.assertEqual(page.locator('.image-slide.keyboard-current').get_attribute('data-image-slide'), '2')
        page.locator('[data-step="images"]').click()
        page.keyboard.press('k')
        self.assertEqual(page.evaluate('document.activeElement.dataset.imageSlide'), '1')

    def test_lightbox_arrows_visit_hidden_versions_and_other_slides(self):
        page = self.open_images()
        self.generate(page)
        self.generate(page, slides=[1])
        tile = page.locator('[data-image-slide="1"]')
        self.assertEqual(tile.locator('.carousel-output:visible').count(), 1)
        tile.locator('.image-stack-cover .carousel-output>a').click()
        dialog = page.locator('dialog.image-lightbox[open]')
        dialog.wait_for()
        self.assertEqual(dialog.locator('img').get_attribute('alt'), '第 1 张成图第 2 版')
        self.assertIn('1 / 4', dialog.locator('[data-lightbox-label]').inner_text())
        page.keyboard.press('ArrowRight')
        self.assertEqual(dialog.locator('img').get_attribute('alt'), '第 1 张成图第 1 版')
        dialog.locator('[data-lightbox-next]').click()
        self.assertEqual(dialog.locator('img').get_attribute('alt'), '第 2 张成图第 1 版')
        page.keyboard.press('ArrowLeft')
        self.assertEqual(dialog.locator('img').get_attribute('alt'), '第 1 张成图第 1 版')
        dialog.locator('[data-close]').click()
        self.assertEqual(page.locator('dialog.image-lightbox').count(), 0)


if __name__ == '__main__':
    unittest.main()
