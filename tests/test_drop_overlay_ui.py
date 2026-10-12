"""The video-import overlay shows only for video drags; pictures and handled drops never leave it stuck."""
from __future__ import annotations

import unittest

from playwright.sync_api import expect

import test_production_carousel_ui as carousel_fixture


class DropOverlayBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    DRAG = """(spec) => {
        const transfer = new DataTransfer();
        transfer.items.add(new File([new Uint8Array([1,2,3])], spec.name, {type: spec.type}));
        const target = document.querySelector(spec.selector) || document.body;
        const over = new DragEvent('dragover', {dataTransfer: transfer, bubbles: true, cancelable: true});
        target.dispatchEvent(over);
        const overlay = document.querySelector('#video-drop-overlay');
        return {visible: !overlay.hidden, prevented: over.defaultPrevented, effect: transfer.dropEffect};
    }"""

    def test_picture_drag_does_not_show_the_video_overlay(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        result = page.evaluate(self.DRAG, {"name": "a.png", "type": "image/png", "selector": "body"})
        self.assertEqual((result["visible"], result["prevented"]), (False, True))
        # over a slide tile the tile accepts it; the overlay stays hidden as well
        result = page.evaluate(self.DRAG, {"name": "a.png", "type": "image/png", "selector": '[data-pick-slide="1"]'})
        self.assertFalse(result["visible"])

    def test_video_drag_still_shows_it_and_dropping_a_picture_elsewhere_only_warns(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        result = page.evaluate(self.DRAG, {"name": "clip.mp4", "type": "video/mp4", "selector": "body"})
        self.assertTrue(result["visible"])
        page.evaluate("""() => {
            const transfer = new DataTransfer();
            transfer.items.add(new File([new Uint8Array([1])], 'photo.jpg', {type: 'image/jpeg'}));
            document.body.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
        }""")
        expect(page.locator("#video-drop-overlay")).to_be_hidden()
        expect(page.locator("body")).to_contain_text("只接收视频文件")


if __name__ == "__main__":
    unittest.main()
