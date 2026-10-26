"""Selectable lettering previews and their saved/generation inputs in an isolated workbench."""
import copy
from pathlib import Path
import unittest
from uuid import uuid4

from playwright.sync_api import expect

import test_production_carousel_images_ui as image_fixture


class CarouselCaptionStyleBrowserTests(unittest.TestCase):
    setUpClass = classmethod(image_fixture.CarouselImageBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(image_fixture.CarouselImageBrowserTests.tearDownClass.__func__)
    setUp = image_fixture.CarouselImageBrowserTests.setUp
    stop_server = image_fixture.CarouselImageBrowserTests.stop_server
    request = image_fixture.CarouselImageBrowserTests.request
    open_page = image_fixture.CarouselImageBrowserTests.open_page
    make_playable_video = image_fixture.CarouselImageBrowserTests.make_playable_video
    stop_images = image_fixture.CarouselImageBrowserTests.stop_images
    add_base = image_fixture.CarouselImageBrowserTests.add_base
    open_images = image_fixture.CarouselImageBrowserTests.open_images
    generate = image_fixture.CarouselImageBrowserTests.generate
    detail = image_fixture.CarouselImageBrowserTests.detail

    STYLES = {
        "yellow_impact": "黄黑冲击（默认）", "orange_depth": "橙白立体",
        "red_comic": "红白漫画", "lime_pop": "荧光青绿",
    }

    def open_settings(self, page):
        toggle = page.locator("#carousel-requirements-toggle")
        if toggle.get_attribute("open") is None:
            toggle.locator("summary").first.click()
        return page.locator('[name="image_caption_style"]')

    def assert_preview(self, page, style, prefix="image", compact=False):
        scope = page.locator('.production-dialog[open]') if prefix == "import" else page.locator("#carousel-image-form")
        selector = f'[data-caption-preview="{prefix}"]'
        selector += ".carousel-caption-preview--compact" if compact else ":not(.carousel-caption-preview--compact)"
        preview = scope.locator(selector)
        expect(preview).to_be_visible()
        image = preview.locator("img")
        expect(image).to_have_attribute("src", f"/caption-styles/{style}.png")
        expect(preview.locator("[data-caption-preview-label]")).to_have_text(self.STYLES[style].replace("（默认）", ""))
        expect(image).to_have_js_property("complete", True)
        self.assertGreater(image.evaluate("image => image.naturalWidth"), 0, "The actual lettering preview must load.")

    def save_style(self, style):
        requirements = copy.deepcopy(self.detail()["requirements"])
        if style is None:
            requirements.pop("caption_style", None)
        else:
            requirements["caption_style"] = style
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/requirements", {
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"], "requirements": requirements,
        })
        self.assertEqual(status, 200, result)

    def test_four_choices_preview_immediately_save_reload_and_reach_generation_without_losing_a_dirty_choice(self):
        # The reusable fixture explicitly creates an older photo_preserving order.
        # Create a real new order without requirements to exercise the new default.
        self.carousel = self.app.create_carousel({"request_id": uuid4().hex,
            "revision": self.app.production.public()["revision"],
            "carousel": {"script_id": self.carousel["script_id"]}})["carousel"]
        for number in (1, 2, 3):
            self.add_base(number, "202H-S2/原图/03.jpg")
        page = self.open_images()
        self.assertEqual(page.locator('[name="image_caption_style"]').input_value(), "yellow_impact")
        self.assert_preview(page, "yellow_impact", compact=True)
        select = self.open_settings(page)
        for style, label in self.STYLES.items():
            self.assertEqual(select.locator(f'option[value="{style}"]').inner_text(), label)
            select.select_option(style)
            self.assert_preview(page, style)
        select.select_option("yellow_impact")
        self.assert_preview(page, "yellow_impact")
        screenshots = Path(__file__).resolve().parents[1] / ".test-artifacts"
        screenshots.mkdir(exist_ok=True)
        for theme, filename in (("light", "caption-styles-ui.png"), ("dark", "caption-styles-ui-dark.png")):
            page.emulate_media(color_scheme=theme)
            page.locator(".carousel-caption-style").scroll_into_view_if_needed()
            page.screenshot(path=str(screenshots / filename), full_page=False, animations="disabled")
        page.emulate_media(color_scheme="light")
        select.select_option("orange_depth")
        with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/requirements")) as response:
            page.locator("#carousel-save-requirements").click()
        self.assertEqual(response.value.status, 200)
        expect(page.locator('[name="image_caption_style"]')).to_have_value("orange_depth")
        page.reload()
        page.locator('[data-step="images"]').click()
        select = self.open_settings(page)
        expect(select).to_have_value("orange_depth")
        self.assert_preview(page, "orange_depth")

        page.locator('[name="image_mode"]').select_option("photo_preserving")
        select.select_option("red_comic")
        sent = []
        page.on("request", lambda request: sent.append(request.post_data_json)
                if request.method == "POST" and request.url.endswith("/generate") else None)
        self.generate(page, slides=[1])
        self.assertEqual(sent[-1]["requirements"]["caption_style"], "red_comic")
        output = self.detail()["slides"][0]["outputs"][-1]
        self.assertEqual(output["requirements_snapshot"]["caption_style"], "red_comic")

        # An independent confirmation refreshes the same result region used by background polling.
        # Its saved requirements are older than this unsaved form choice and must not replace it.
        select = self.open_settings(page)
        select.select_option("lime_pop")
        page.locator('[data-image-slide="1"] [data-approve-output]').first.click()
        page.locator('[data-image-slide="1"] [data-revoke-output]').wait_for()
        expect(page.locator('[name="image_caption_style"]')).to_have_value("lime_pop")
        self.assert_preview(page, "lime_pop")
        self.assertEqual(self.detail()["requirements"]["caption_style"], "red_comic")

    def test_import_preview_and_draft_follow_the_same_select_without_replacing_legacy_choices(self):
        self.save_style("rugged")
        page = self.open_images(width=560)
        select = self.open_settings(page)
        expect(select).to_have_value("rugged")
        expect(page.locator('[data-caption-preview="image"]:not(.carousel-caption-preview--compact)')).to_be_hidden()
        for old in ("plain", "rugged", ""):
            self.assertEqual(select.locator(f'option[value="{old}"]').count(), 1)
        page.locator("#carousel-import-finished").click()
        modal = page.locator('.production-dialog[open]')
        imported = modal.locator('[name="import_caption_style"]')
        expect(imported).to_have_value("rugged")
        imported.select_option("lime_pop")
        self.assert_preview(page, "lime_pop", prefix="import")
        modal.get_by_role("button", name="取消", exact=True).click()
        page.locator("#carousel-import-finished").click()
        expect(page.locator('.production-dialog[open] [name="import_caption_style"]')).to_have_value("lime_pop")
        self.assert_preview(page, "lime_pop", prefix="import")
        page.locator('.production-dialog[open]').get_by_role("button", name="取消", exact=True).click()
        self.assertEqual(self.detail()["requirements"]["caption_style"], "rugged")
        self.save_style(None)
        page.reload()
        page.locator('[data-step="images"]').click()
        expect(self.open_settings(page)).to_have_value("")
        self.assertNotIn("caption_style", self.detail()["requirements"])


if __name__ == "__main__":
    unittest.main()
