"""Next-step guidance and completed controls, using isolated production fixtures."""
from __future__ import annotations

import unittest

from test_production_carousel_delivery_ui import CarouselDeliveryBrowserTests
from test_production_manual_completion_ui import ProductionManualCompletionBrowserTests


class CarouselGuidanceBrowserTests(CarouselDeliveryBrowserTests):
    def test_next_button_switches_only_when_clicked_and_completed_controls_fold(self):
        page = self.open_images(width=390)
        page.locator('[data-step="slides"]').click()
        next_action = page.locator('[data-next-action]')
        next_action.get_by_text("下一步：生成第 1、2、3 张成图 →", exact=True).wait_for(state='attached')
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'slides')
        # The next-step banner is hidden; its action still drives the step bar.
        next_action.dispatch_event('click')
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'images')
        page.locator('[data-step="pick"]').click()
        pick_controls = page.locator('[data-production-controls="pick"]')
        self.assertFalse(pick_controls.evaluate('node => node.open'))
        self.assertFalse(page.locator('#clip-ai-pick').is_visible())
        pick_controls.locator(':scope > summary').click()
        self.assertTrue(page.locator('#clip-ai-pick').is_visible())
        self.confirm_group(page)
        controls = page.locator('[data-production-controls="images"]')
        self.assertFalse(controls.evaluate('node => node.open'))
        self.assertFalse(page.locator('#carousel-image-form').is_visible())
        self.assertTrue(page.locator('.carousel-output').first.is_visible())
        self.assertTrue(page.evaluate("document.querySelector('#carousel-image-slides').compareDocumentPosition(document.querySelector('[data-production-controls=images]')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        page.locator('[data-next-state]').get_by_text('可领取', exact=True).wait_for(state='attached')
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_pending_slide_guidance_and_open_controls_survive_confirmation_refresh(self):
        page = self.open_images()
        self.generate(page)
        self.approve(page, 1)
        page.locator('[data-next-action]').get_by_text('下一步：确认第 2、3 张成图 →', exact=True).wait_for(state='attached')
        for number in (2, 3):
            self.approve(page, number)
        controls = page.locator('[data-production-controls="images"]')
        controls.locator(':scope > summary').click()
        page.locator('[data-revoke-output]').first.click()
        page.locator('[data-production-controls="images"]').wait_for(state='detached')
        page.locator('[data-image-slide="1"] [data-approve-output]').first.click()
        controls.wait_for()
        self.assertTrue(controls.evaluate('node => node.open'))
        self.assertTrue(page.locator('#carousel-image-form').is_visible())


class VideoGuidanceBrowserTests(ProductionManualCompletionBrowserTests):
    def test_manual_completion_puts_results_before_folded_controls_and_explicit_publish_action(self):
        self.payload['job'].update(status='approved', manual_completion=self.receipt())
        page = self.open_edit_step(width=390)
        controls = page.locator('[data-production-controls="edit"]')
        self.assertFalse(controls.evaluate('node => node.open'))
        self.assertFalse(page.locator('#edit-panel #edit-form').is_visible())
        self.assertTrue(page.locator('[data-revoke-job-completion]').is_visible())
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'edit')
        page.locator('[data-next-action]').get_by_text('下一步：登记发布视频 ID →', exact=True).wait_for(state='attached')
        page.locator('[data-next-action]').dispatch_event('click')
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'publish')
        page.locator('[data-step="voice"]').click()
        self.assertTrue(page.locator('#take-panel').is_visible())
        self.assertFalse(page.locator('#take-form').is_visible())
        self.assertTrue(page.evaluate("document.querySelector('#take-panel').compareDocumentPosition(document.querySelector('#voice-panel')) & Node.DOCUMENT_POSITION_FOLLOWING"))
        page.locator('[data-step="timeline"]').click()
        self.assertTrue(page.locator('.timeline-card').is_visible())
        self.assertFalse(page.locator('#timeline-form').is_visible())
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_publication_step_uses_loaded_current_receipt(self):
        self.payload['job'].update(status='approved', manual_completion=self.receipt())
        self.payload['publication'] = {
            'id':'PUB-fixture', 'account':'fixture', 'published_on':'2026-10-14',
            'post_id':'7664986913497009442', 'script_version':1, 'source':{},
            'file':{'name':'published.mp4','size':100,'sha256':'test'},
            'source_current':True, 'file_available':True,
        }
        page = self.open_edit_step()
        page.locator('[data-step="publish"].done').wait_for()
        page.locator('[data-step="publish"] small').get_by_text('已登记发布', exact=True).wait_for()
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'edit')
        page.locator('[data-next-action]').get_by_text('下一步：查看发布登记 →', exact=True).wait_for(state='attached')

    def test_historical_publication_does_not_complete_current_step(self):
        self.payload['job'].update(status='approved', manual_completion=self.receipt())
        self.payload['publication'] = {
            'id':'PUB-fixture', 'account':'fixture', 'published_on':'2026-10-14',
            'post_id':'7664986913497009442', 'script_version':1, 'source':{},
            'file':{'name':'published.mp4','size':100,'sha256':'test'},
            'source_current':False, 'file_available':True,
        }
        page = self.open_edit_step()
        page.locator('[data-step="publish"] small').get_by_text('登记版本需核对', exact=True).wait_for()
        self.assertNotIn('done', page.locator('[data-step="publish"]').get_attribute('class') or '')
        page.locator('[data-next-action]').get_by_text('下一步：核对发布登记与当前版本 →', exact=True).wait_for(state='attached')

    def test_video_id_only_publication_completes_step(self):
        self.payload['job'].update(status='approved', manual_completion=self.receipt())
        self.payload['publication'] = {
            'id':'PUB-fixture', 'account':'', 'published_on':'2026-10-14',
            'post_id':'7664986913497009442', 'script_version':1, 'source':{},
            'file':None, 'source_current':True, 'file_available':False,
        }
        page = self.open_edit_step()
        page.locator('[data-step="publish"].done').wait_for()
        page.locator('[data-next-action]').get_by_text('下一步：查看发布登记 →', exact=True).wait_for(state='attached')


# Reuse fixture setup without rediscovering inherited test cases.
for case, base in ((CarouselGuidanceBrowserTests, CarouselDeliveryBrowserTests),
                   (VideoGuidanceBrowserTests, ProductionManualCompletionBrowserTests)):
    for name in dir(base):
        if name.startswith('test_') and name not in case.__dict__:
            setattr(case, name, None)
del CarouselDeliveryBrowserTests, ProductionManualCompletionBrowserTests, case, base, name


if __name__ == '__main__':
    unittest.main()
