"""A deferred real picker request keeps its stage, clock and form visible."""
from __future__ import annotations

import json
import threading
import unittest

import test_production_carousel_clips_ui as fixture


@unittest.skipUnless(fixture.HAVE_FFMPEG, '需要本机 ffmpeg/ffprobe')
class CarouselClipRuntimeBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.CarouselClipPickerBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.CarouselClipPickerBrowserTests.tearDownClass.__func__)
    setUp = fixture.CarouselClipPickerBrowserTests.setUp
    stop_server = fixture.CarouselClipPickerBrowserTests.stop_server
    request = fixture.CarouselClipPickerBrowserTests.request
    open_page = fixture.CarouselClipPickerBrowserTests.open_page
    make_playable_video = fixture.CarouselClipPickerBrowserTests.make_playable_video

    def test_picker_polls_actual_stage_ticks_keeps_form_and_cleans_after_success_and_error(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        failed = []

        def answer(prompt, **options):
            if failed:
                raise RuntimeError('isolated deferred picker failure')
            options['on_progress']('AI 正在根据标签核对候选片段')
            entered.set()
            if not release.wait(15):
                raise RuntimeError('fixture release timed out')
            clips = self.app.carousel_clips(self.carousel['id'], ['蓝点折刀成品素材'])['clips']
            return json.dumps({'slides': {'1': [{'key': clips[0]['key'], 'reason': '标签相符'}]}, 'gaps': []})

        self.catalog.run = answer
        page = self.open_page(f'#production/{self.carousel["id"]}')
        page.locator('[data-step="pick"]').click()
        page.locator('#still-panel input[name="material"]').first.wait_for()
        page.locator('#still-panel input[name="material"]').first.check()
        page.evaluate("window.savedStillPanel = document.querySelector('#still-panel')")
        reads = []
        endpoint = f'/api/production/carousels/{self.carousel["id"]}'
        page.on('request', lambda request: reads.append(request.url) if request.url.endswith(endpoint) and request.method == 'GET' else None)
        page.locator('#clip-ai-pick').click()
        page.locator('#still-panel>.production-runtime').wait_for()
        self.assertTrue(entered.wait(2))
        self.assertTrue(page.locator('#clip-ai-pick').is_disabled())
        page.locator('#still-panel textarea[name="instruction"]').fill('下次挑片段时保留这一段要求')
        page.wait_for_function("() => document.querySelector('#still-panel>.production-runtime')?.textContent.includes('AI 挑候选片段')", timeout=8000)
        page.wait_for_function("() => /^[1-9]/.test(document.querySelector('#still-panel>.production-runtime [data-run-started]')?.textContent || '')", timeout=8000)
        self.assertGreaterEqual(len(reads), 1)
        release.set()
        page.locator('#clip-ai-summary').wait_for(timeout=10000)
        page.wait_for_function("() => !document.querySelector('#still-panel>.production-runtime')")
        self.assertFalse(page.locator('#clip-ai-pick').is_disabled())
        self.assertTrue(page.evaluate("window.savedStillPanel === document.querySelector('#still-panel')"))
        self.assertEqual(page.locator('#still-panel textarea[name="instruction"]').input_value(), '下次挑片段时保留这一段要求')
        self.assertIn('AI 1', page.locator('[data-clip-slide="1"]').inner_text())
        self.assertIsNone(self.request('GET', endpoint)[2]['carousel']['clip_picking_live'])
        failed.append(True)
        page.locator('#clip-ai-pick').click()
        page.locator('#still-panel [data-error]:visible').wait_for()
        self.assertFalse(page.locator('#clip-ai-pick').is_disabled())
        self.assertEqual(page.locator('#still-panel>.production-runtime').count(), 0)
        self.assertTrue(page.locator('#clip-ai-summary').is_visible())


if __name__ == '__main__':
    unittest.main()
