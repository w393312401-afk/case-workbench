"""Carousel group overview, enlarged bases and one-modal frame picking in disposable browser fixtures."""
from pathlib import Path
import unittest
from uuid import uuid4

from playwright.sync_api import expect

import test_production_carousel_clips_ui as clip_fixture

SCREENSHOTS = Path("C:/Users/video/.codex/visualizations/2026/10/14/01a1380c-1107-7482-bede-a4053faa01ad")


@unittest.skipUnless(clip_fixture.HAVE_FFMPEG, "需要本机 ffmpeg/ffprobe")
class CarouselNavigationBrowserTests(unittest.TestCase):
    setUpClass = classmethod(clip_fixture.CarouselClipPickerBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(clip_fixture.CarouselClipPickerBrowserTests.tearDownClass.__func__)
    setUp = clip_fixture.CarouselClipPickerBrowserTests.setUp
    stop_server = clip_fixture.CarouselClipPickerBrowserTests.stop_server
    request = clip_fixture.CarouselClipPickerBrowserTests.request
    open_page = clip_fixture.CarouselClipPickerBrowserTests.open_page
    make_playable_video = clip_fixture.CarouselClipPickerBrowserTests.make_playable_video

    def open_picker(self, page, number=1):
        page.locator('.production-steps [data-step="pick"]').click()
        page.locator(f'[data-clip-slide="{number}"]').click()
        dialog = page.locator("dialog.clip-dialog")
        dialog.locator("[data-clip]").first.wait_for(timeout=15000)
        if not dialog.locator("[data-clip-stage]:not([hidden])").count():
            dialog.locator("[data-clip]").first.click()
        expect(dialog.locator("[data-submit]")).to_be_enabled(timeout=15000)
        return dialog

    def screenshot(self, page, name):
        SCREENSHOTS.mkdir(parents=True, exist_ok=True)
        page.evaluate("window.scrollTo(0,0)")
        page.screenshot(path=str(SCREENSHOTS / name), full_page=True)

    def test_overview_is_visible_in_each_step_and_base_description_expands(self):
        status, _, imported = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/import",
                                         {"request_id": uuid4().hex, "slide": 1, "path": "202H-S2/原图/03.jpg"})
        self.assertEqual(status, 200, imported)
        candidate = imported["candidate"]
        data = self.app.production.read()
        first = data["carousels"][0]["slides"][0]
        first["candidates"][0]["reason"] = "这是用来核对开箱实拍的完整说明。" * 15
        self.app.production.write(data)
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.set_viewport_size({"width": 1100, "height": 850})
        overview = page.locator("#carousel-overview")
        expect(overview.locator("button")).to_have_count(3)
        self.assertGreaterEqual(overview.bounding_box()["height"], 64)
        expect(overview.locator(".empty")).to_have_count(3)
        for key in ("slides", "images", "delivery"):
            page.locator(f'.production-steps [data-step="{key}"]').click()
            expect(overview).to_be_visible()
            expect(page.locator(f'.production-steps [data-step="{key}"]')).to_have_attribute("aria-selected", "true")
        overview.locator('[data-overview-slide="1"]').click()
        expect(page.locator('.production-steps [data-step="pick"]')).to_have_attribute("aria-selected", "true")
        tile = page.locator('[data-pick-slide="1"]')
        expect(tile.locator(".pick-description")).to_have_css("-webkit-line-clamp", "2")
        tile.locator("[data-pick-description]").click()
        expect(tile.locator("figcaption")).to_have_class("expanded")
        tile.locator('[data-pick="1"]').click()
        expect(tile.locator('[data-unpick="1"]')).to_be_visible()
        expect(tile.locator("figcaption")).to_have_class("expanded")
        expect(overview.locator('[data-overview-slide="1"]')).to_have_class("carousel-overview-tile based")
        self.assertGreaterEqual(tile.locator(".chosen img").bounding_box()["height"], 220)
        self.assertEqual(self.app.production.carousel(self.carousel["id"])["slides"][0]["base"]["id"], candidate["id"])
        tile.locator("[data-pick-description]").click()
        self.screenshot(page, "carousel-overview-and-base.png")

    def test_slide_navigation_and_keyboard_scrubbing_do_not_save_until_requested(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.set_viewport_size({"width": 1100, "height": 1000})
        requests = []
        page.on("request", lambda request: requests.append(request) if request.method == "POST" and request.url.endswith(("/clip-frame", "/base")) else None)
        dialog = self.open_picker(page)
        dialog.evaluate("node => node._navigationIdentity = 'same-dialog'")
        slider = dialog.locator("[data-clip-slider]")
        before = int(slider.input_value())
        dialog.locator('[data-step="1"]').focus()
        page.keyboard.press("ArrowRight")
        expect(slider).to_have_value(str(before + 1))
        page.keyboard.press("Shift+ArrowLeft")
        expect(slider).to_have_value(str(before - 9))
        search = dialog.locator("[data-clip-search]")
        search.fill("打开")
        search.press("ArrowLeft")
        expect(slider).to_have_value(str(before - 9))
        dialog.locator("[data-clip-next]").click()
        expect(dialog.locator(".dialog-heading h2")).to_have_text("第 2 张：从片段取帧")
        dialog.locator("[data-clip-previous]").click()
        expect(dialog.locator(".dialog-heading h2")).to_have_text("第 1 张：从片段取帧")
        self.assertEqual(dialog.evaluate("node => node._navigationIdentity"), "same-dialog")
        self.assertEqual(requests, [])
        expect(dialog.locator("[data-submit]")).to_be_enabled(timeout=15000)
        self.screenshot(page, "carousel-frame-picker-navigation.png")

    def test_generated_and_confirmed_overview_tiles_open_their_image_cards(self):
        detail = self.app.carousel_detail(self.carousel["id"])
        for number, approved in ((1, False), (2, True)):
            slide = detail["carousel"]["slides"][number - 1]
            output = {"id": f"OUT-{number:012x}", "slide": number, "approved": approved, "stale": False,
                      "aspect": "3:4", "created_at": "2026-10-14T10:00:00+08:00", "review": None, "stages": {}}
            slide["outputs"] = [output]
            slide["output_id"] = output["id"]
        self.app.carousel_detail = lambda identifier: detail
        page = self.open_page(f"#production/{self.carousel['id']}")
        overview = page.locator("#carousel-overview")
        expect(overview.locator('[data-overview-slide="1"]')).to_have_class("carousel-overview-tile generated")
        expect(overview.locator('[data-overview-slide="2"]')).to_have_class("carousel-overview-tile confirmed")
        page.locator('.production-steps [data-step="slides"]').click()
        expect(page.locator('.production-steps [data-step="slides"]')).to_have_attribute("aria-selected", "true")
        overview.locator('[data-overview-slide="2"]').click()
        expect(page.locator('.production-steps [data-step="images"]')).to_have_attribute("aria-selected", "true")
        expect(page.locator('[data-image-slide="2"]')).to_be_visible()

    def test_selecting_a_frame_advances_to_next_missing_base_and_closes_after_last(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        dialog = self.open_picker(page)
        dialog.evaluate("node => node._navigationIdentity = 'same-dialog'")
        for number in (1, 2, 3):
            expect(dialog.locator(".dialog-heading h2")).to_have_text(f"第 {number} 张：从片段取帧")
            if not dialog.locator("[data-clip-stage]:not([hidden])").count():
                dialog.locator("[data-clip]").first.click()
            expect(dialog.locator("[data-submit]")).to_be_enabled(timeout=15000)
            dialog.locator("[data-submit]").click()
            if number < 3:
                expect(dialog.locator(".dialog-heading h2")).to_have_text(f"第 {number + 1} 张：从片段取帧", timeout=15000)
                self.assertEqual(dialog.evaluate("node => node._navigationIdentity"), "same-dialog")
            else:
                expect(dialog).to_have_count(0, timeout=15000)
        self.assertTrue(all(slide["base"] for slide in self.app.production.carousel(self.carousel["id"])["slides"]))
        expect(page.locator('.production-steps [data-step="pick"]')).to_have_attribute("aria-selected", "true")
        expect(page.locator("#carousel-overview .based")).to_have_count(3)

    def test_lost_base_response_retries_the_same_request_before_advancing(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        attempts = []

        def lose_first_response(route):
            attempts.append(route.request.post_data_json)
            if len(attempts) == 1:
                response = route.fetch(headers={**route.request.headers, "Connection": "close"})
                response.dispose()
                route.abort("failed")
            else:
                route.continue_()

        page.route(f"**/api/production/carousels/{self.carousel['id']}/base", lose_first_response)
        dialog = self.open_picker(page)
        dialog.locator("[data-submit]").click()
        expect(dialog.locator("[data-error]")).to_contain_text("结果尚未确认", timeout=15000)
        expect(dialog.locator("[data-clip-next]")).to_be_disabled()
        expect(dialog.locator("[data-clip-slider]")).to_be_disabled()
        expect(dialog.locator("[data-submit]")).to_be_enabled()
        dialog.locator("[data-submit]").click()
        expect(dialog.locator(".dialog-heading h2")).to_have_text("第 2 张：从片段取帧", timeout=15000)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0], attempts[1])
        first = self.app.production.carousel(self.carousel["id"])["slides"][0]
        self.assertEqual(len(first["candidates"]), 1)
        self.assertIsNotNone(first["base"])


if __name__ == "__main__":
    unittest.main()
