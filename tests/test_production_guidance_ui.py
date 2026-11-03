"""Next-step guidance and completed controls, using isolated production fixtures."""
from __future__ import annotations

from playwright.sync_api import expect
import unittest

from test_production_manual_completion_ui import ProductionManualCompletionBrowserTests


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
        page.locator('[data-next-action]').get_by_text('下一步：查看发布记录 →', exact=True).wait_for(state='attached')

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
        page.locator('[data-next-action]').get_by_text('下一步：查看发布记录 →', exact=True).wait_for(state='attached')


# Reuse fixture setup without rediscovering inherited test cases.
for case, base in ((VideoGuidanceBrowserTests, ProductionManualCompletionBrowserTests),):
    for name in dir(base):
        if name.startswith('test_') and name not in case.__dict__:
            setattr(case, name, None)
del ProductionManualCompletionBrowserTests, case, base, name


if __name__ == '__main__':
    unittest.main()
