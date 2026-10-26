"""Carousel stage receipts and superseded runs through the isolated live UI."""
from __future__ import annotations

import unittest
from uuid import uuid4

from production_store import carousel_input_signature
import test_production_carousel_images_ui as image_fixture


class CarouselReceiptsBrowserTests(unittest.TestCase):
    setUpClass = classmethod(image_fixture.CarouselImageBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(image_fixture.CarouselImageBrowserTests.tearDownClass.__func__)
    setUp = image_fixture.CarouselImageBrowserTests.setUp
    stop_server = image_fixture.CarouselImageBrowserTests.stop_server
    request = image_fixture.CarouselImageBrowserTests.request
    open_page = image_fixture.CarouselImageBrowserTests.open_page
    make_playable_video = image_fixture.CarouselImageBrowserTests.make_playable_video
    add_base = image_fixture.CarouselImageBrowserTests.add_base
    stop_images = image_fixture.CarouselImageBrowserTests.stop_images
    open_images = image_fixture.CarouselImageBrowserTests.open_images
    generate = image_fixture.CarouselImageBrowserTests.generate
    detail = image_fixture.CarouselImageBrowserTests.detail

    def body(self, **extra):
        return {'request_id': uuid4().hex, 'carousel_id': self.carousel['id'], **extra}

    def record_review(self, output, checks, status):
        slide = next(slide for slide in self.detail()['slides'] if any(candidate['id'] == output['id'] for candidate in slide.get('outputs', [])))
        signature = carousel_input_signature(self.detail(), slide, output['aspect'])
        review_id = 'REV-' + uuid4().hex[:12]
        self.app.production.mutate('carousel_review_start', self.body(slide=slide['number'], output_id=output['id']),
            {'id': review_id, 'status': 'running', 'started_at': '2026-10-14T09:00:00Z',
             'file_sha256': output['sha256'], 'input_signature': signature,
             'requirements_signature': output.get('requirements_signature'),
             'scope': ['product', 'evidence', 'text', 'visual']})
        self.app.production.mutate('carousel_review_finish', self.body(slide=slide['number'], output_id=output['id'], review_id=review_id),
            {'status': 'completed', 'finished_at': '2026-10-14T09:01:00Z',
             'file_sha256': output['sha256'], 'input_signature': signature,
             'review': {'status': status, 'summary': '请人工核对', 'checks': checks, 'issues': []}})

    def test_running_slide_stages_refresh_without_losing_generation_draft(self):
        self.images.hold = True
        page = self.open_images(width=390)
        page.locator('#carousel-generate-picked').click()
        page.locator('[data-cancel-generation]').wait_for()
        stage = page.locator('[data-image-slide="2"] [data-image-phase="generation"]')
        page.wait_for_function("() => document.querySelector('[data-image-slide=\"2\"] [data-image-phase=\"generation\"]')?.dataset.phaseStatus === 'running'")
        self.assertEqual(page.locator('[data-image-slide="2"] .carousel-stage-strip li').count(), 6)
        page.locator('#carousel-image-form [name="instruction"]').fill('下次保留这一段要求')
        run = self.detail()['generations'][-1]
        self.app.production.mutate('carousel_generation_stage', self.body(generation_id=run['id'], slide=2, stage='generation'),
            {'status': 'failed', 'note': '第 2 张素材暂时不可读'})
        page.wait_for_function("() => document.querySelector('[data-image-slide=\"2\"] [data-image-phase=\"generation\"]')?.dataset.phaseStatus === 'failed'", timeout=10000)
        self.assertIn('第 2 张素材暂时不可读', stage.get_attribute('title'))
        self.assertEqual(page.locator('#carousel-image-form [name="instruction"]').input_value(), '下次保留这一段要求')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_direct_stage_receipts_show_skipped_review_and_real_confirmation(self):
        page = self.open_images()
        self.generate(page)
        tile = page.locator('[data-image-slide="1"]')
        self.assertEqual(tile.locator('[data-image-phase="generation"]').get_attribute('data-phase-status'), 'completed')
        self.assertEqual(tile.locator('[data-image-phase="review"]').get_attribute('data-phase-status'), 'skipped')
        self.assertIn('没有执行自动核对', tile.locator('[data-image-phase="review"]').get_attribute('title'))
        self.assertEqual(tile.locator('[data-image-phase="confirmation"]').get_attribute('data-phase-status'), 'pending')
        self.assertEqual(tile.locator('.image-review').count(), 0)
        self.assertIn('无自动核对记录', tile.locator('.image-review-empty').inner_text())
        tile.locator('[data-approve-output]').click()
        page.wait_for_function("() => document.querySelector('[data-image-slide=\"1\"] [data-image-phase=\"confirmation\"]')?.dataset.phaseStatus === 'completed'")
        self.assertEqual(tile.locator('[data-image-phase="package"]').get_attribute('data-phase-status'), 'pending')
        tile.locator('[data-revoke-output]').click()
        page.wait_for_function("() => document.querySelector('[data-image-slide=\"1\"] [data-image-phase=\"confirmation\"]')?.dataset.phaseStatus === 'pending'")

    def test_existing_four_checks_warn_and_keep_manual_confirmation_available(self):
        page = self.open_images(width=390)
        self.generate(page)
        output = self.detail()['slides'][0]['outputs'][0]
        self.record_review(output, [{'id': key, 'status': 'fail' if key == 'text' else 'pass',
            'note': '<script>文字没有对齐</script>' if key == 'text' else '已对照底图'}
            for key in ('subject', 'evidence', 'text', 'layout')], 'needs_fix')
        page.reload()
        page.locator('[data-step="images"]').click()
        card = page.locator(f'[data-output-id="{output["id"]}"]')
        self.assertEqual(card.locator('[data-review-check]').count(), 4)
        self.assertIn('review-needs-fix', card.get_attribute('class'))
        text = card.locator('[data-review-check="text"]')
        self.assertEqual(text.get_attribute('data-review-status'), 'fail')
        self.assertIn('<script>文字没有对齐</script>', text.get_attribute('title'))
        self.assertEqual(card.locator('script').count(), 0)
        self.assertEqual(card.locator('[data-review-check="product"]').get_attribute('data-review-status'), 'pass')
        self.assertEqual(card.locator('[data-review-check="visual"]').get_attribute('data-review-status'), 'pass')
        self.assertFalse(card.locator('[data-approve-output]').is_disabled())
        card.locator('[data-approve-output]').click()
        page.locator(f'[data-output-id="{output["id"]}"] [data-revoke-output]').wait_for()
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_superseded_incomplete_run_folds_but_unresolved_retry_stays_available(self):
        page = self.open_images()
        self.generate(page)
        # A historical first-image gate has been superseded by these newer outputs.
        old_id = 'IMG-' + uuid4().hex[:12]
        self.app.production.mutate('carousel_generation_start', self.body(),
            {'id': old_id, 'status': 'running', 'aspect': '3:4', 'slides': [1, 2, 3], 'requested_slides': [1, 2, 3],
             'started_at': '2020-01-01T00:00:00Z', 'added': 0, 'stages': {}})
        self.app.production.mutate('carousel_generation_finish', self.body(generation_id=old_id),
            {'status': 'awaiting_first_approval', 'finished_at': '2020-01-01T00:01:00Z', 'added': 0, 'error': ''})
        page.reload()
        page.locator('[data-step="images"]').click()
        old = page.locator(f'[data-generation-id="{old_id}"]')
        self.assertFalse(old.is_visible())
        self.assertEqual(page.locator('#carousel-image-runs>.image-run').count(), 0)
        self.assertEqual(page.locator('#carousel-continue-images').count(), 0)
        page.locator('.image-run-history>summary').click()
        self.assertTrue(old.is_visible())
        self.assertIn('第 1、2、3 张', old.inner_text())
        # A newer failure without a saved replacement is still the current actionable run.
        retry_id = 'IMG-' + uuid4().hex[:12]
        self.app.production.mutate('carousel_generation_start', self.body(),
            {'id': retry_id, 'status': 'running', 'aspect': '3:4', 'slides': [3], 'requested_slides': [3],
             'started_at': '2099-01-01T00:00:00Z', 'added': 0, 'stages': {}})
        self.app.production.mutate('carousel_generation_finish', self.body(generation_id=retry_id),
            {'status': 'failed', 'finished_at': '2099-01-01T00:01:00Z', 'added': 0, 'error': '尚未保存新图'})
        page.reload()
        page.locator('[data-step="images"]').click()
        self.assertTrue(page.locator(f'#carousel-image-runs>[data-generation-id="{retry_id}"]').is_visible())
        self.assertTrue(page.locator('#carousel-continue-images').is_visible())
        self.assertFalse(page.locator('#carousel-continue-images').is_disabled())
        # Confirming the older picture does not erase the newer failure.
        output = self.detail()['slides'][2]['outputs'][-1]
        self.app.approve_carousel_output(self.carousel['id'], self.body(revision=self.app.production.public()['revision'], slide=3, output_id=output['id'], approved=True))
        page.reload()
        page.locator('[data-step="images"]').click()
        self.assertTrue(page.locator(f'#carousel-image-runs>[data-generation-id="{retry_id}"]').is_visible())


if __name__ == '__main__':
    unittest.main()
