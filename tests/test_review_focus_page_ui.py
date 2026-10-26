"""The review landing page keeps the next action ahead of optional detail."""
import unittest

import test_review_ui as review


class ReviewFocusPageTests(unittest.TestCase):
    setUpClass = classmethod(review.ReviewPageTests.setUpClass.__func__)
    tearDownClass = classmethod(review.ReviewPageTests.tearDownClass.__func__)
    setUp = review.ReviewPageTests.setUp
    stop_server = review.ReviewPageTests.stop_server
    request = review.ReviewPageTests.request
    open_page = review.ReviewPageTests.open_page

    def test_default_today_list_leads_to_compact_review_without_losing_details(self):
        page = self.open_page(route='#/202H/home', ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        page.locator('.followup-row').first.wait_for()
        panel = page.locator('#home-due-title-panel')
        self.assertLessEqual(panel.locator('.followup-row').count(), 5)
        self.assertFalse(panel.locator('[data-followup-search]').is_visible())
        self.assertFalse(panel.locator('[data-home-experiences]').is_visible())
        row = panel.locator('.followup-row').first
        self.assertTrue(row.locator('.followup-reason').inner_text().strip())
        self.assertTrue(row.locator('[data-followup-chain]').is_visible())
        self.assertFalse(row.locator('.followup-assessment').is_visible())
        row.locator('.followup-details > summary').click()
        self.assertTrue(row.locator('.followup-assessment').is_visible())
        row.locator('[data-followup-chain]').click()
        dialog = page.locator('#perf-video-drawer-floating')
        dialog.locator('.feedback-data-details').wait_for()
        self.assertEqual(page.locator('.feedback-chain-dialog').count(), 0)
        self.assertEqual(page.evaluate('location.hash'), '#/202H/home')
        source = dialog.locator('.perf-video-source')
        video_id = row.get_attribute('data-followup-video')
        self.assertEqual(source.locator('[data-copy-label="视频 ID"]').get_attribute('data-copy-id'), video_id)
        link = source.locator('a[href*="tiktok.com"]')
        self.assertTrue(link.get_attribute('href').endswith('/video/' + video_id))
        self.assertEqual(link.get_attribute('target'), '_blank')
        page.context.grant_permissions(['clipboard-read', 'clipboard-write'])
        source.locator('[data-copy-label="视频 ID"]').click()
        self.assertEqual(page.evaluate('navigator.clipboard.readText()'), video_id)
        source.locator('[data-copy-video-link]').click()
        self.assertEqual(page.evaluate('navigator.clipboard.readText()'), link.get_attribute('href'))
        for scheme in ('light', 'dark'):
            page.emulate_media(color_scheme=scheme)
            box = dialog.bounding_box()
            self.assertAlmostEqual(box['x'] + box['width'], page.viewport_size['width'], delta=2)
            self.assertGreater(box['x'], 0)
            self.assertFalse(dialog.evaluate('node => node.scrollWidth > node.clientWidth'))
        self.assertEqual(dialog.locator('.feedback-source').count(), 0)
        self.assertFalse(dialog.locator('.feedback-data-details').evaluate('(el) => el.open'))
        self.assertFalse(dialog.locator('.feedback-more-actions').evaluate('(el) => el.open'))
        self.assertTrue(dialog.locator('[data-feedback-note]').is_visible())
        self.assertEqual(dialog.locator('[data-feedback-decide]').all_text_contents(), ['加码', '换执行', '停测'])
        self.assertTrue(dialog.locator('[data-feedback-save]').is_visible())
        self.assertFalse(dialog.locator('[data-feedback-decide=stop]').is_visible())
        self.assertEqual(dialog.locator('[data-feedback-outcome],[data-feedback-evidence],[data-feedback-decide=observe],[data-feedback-decide=none]').count(), 0)
        dialog.locator('.feedback-data-details > summary').click()
        self.assertTrue(dialog.locator('.feedback-evidence').is_visible())

    def test_experience_library_remains_accessible_below_today_list_on_mobile(self):
        page = self.open_page(width=390, route='#/202H/home', ready='[data-home-tab]')
        page.locator('[data-home-tab=due]').click()
        page.locator('.followup-row').first.wait_for()
        extras = page.locator('.home-review-more')
        self.assertFalse(extras.evaluate('(el) => el.open'))
        self.assertGreater(extras.bounding_box()['y'], page.locator('#home-followups').bounding_box()['y'])
        extras.locator('summary').click()
        extras.locator('[data-home-experiences]').click()
        page.locator('.experience-library-dialog').wait_for()
        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > innerWidth'))
        self.assertEqual(self.request('GET', '/api/review-experiences')[2]['total'], 0)


if __name__ == '__main__':
    unittest.main()
