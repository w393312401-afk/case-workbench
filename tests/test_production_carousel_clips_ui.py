"""Picking a base frame from a labeled clip inside the page: list, scrub, crop, save and select, open in DaVinci."""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from unittest.mock import patch

from playwright.sync_api import expect

import test_production_carousel_ui as carousel_fixture
from resolve_bridge import ResolveBridge
from test_production_carousel_stills import Runner as StillRunner

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class ClipRunner(StillRunner):
    def __init__(self, media):
        super().__init__()
        self.media = media

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] in ("candidates", "reveal"):
            self.calls.append(request)
            if request["command"] == "reveal":
                body = {"ok": True, "name": request["name"], "timecode": "01:00:03:00"}
            else:
                body = {"ok": True, "missing": [], "used": [], "used_complete": True, "materials": [
                    {"name": "蓝点折刀成品素材", "unique_id": "uid-material", "start_frame": 0, "fps": "30", "drop": False, "clips": [
                        {"uid": "clip-a", "start": 0, "end": 90, "name": "a.mp4", "media_id": "m1", "media_path": str(self.media),
                         "source_start": 30, "source_end": 119, "labels": ["打开的箱子 ZS161"], "verified_labels": ["打开的箱子 ZS161"]},
                        {"uid": "clip-b", "start": 90, "end": 180, "name": "b.mp4", "media_id": "m1", "media_path": str(self.media),
                         "source_start": 150, "source_end": 239, "labels": ["夹刀 TJR165"], "verified_labels": []}]}]}
            return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")
        return super().__call__(args, input, **options)


@unittest.skipUnless(HAVE_FFMPEG, "需要本机 ffmpeg/ffprobe")
class CarouselClipPickerBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    setUp = carousel_fixture.CarouselBrowserTests.setUp
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def setUp(self):
        carousel_fixture.CarouselBrowserTests.setUp(self)
        media = self.root / "clip-source.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10",
                        "-pix_fmt", "yuv420p", "-g", "15", str(media)], check=True)
        self.runner = ClipRunner(media)
        self.app.resolve = ResolveBridge(runner=self.runner)

    def test_pick_a_frame_from_a_clip_and_make_it_the_base(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.locator('[data-clip-slide="1"]').click()
        dialog = page.locator("dialog.clip-dialog")
        dialog.locator("[data-clip]").first.wait_for(timeout=15000)
        expect(dialog.locator("[data-clip-facts]")).to_contain_text("ZS161", timeout=15000)
        page.wait_for_function("() => document.querySelector('[data-clip-save]') && !document.querySelector('[data-clip-save]').disabled", timeout=15000)
        self.assertEqual(dialog.locator("[data-clip-strip] button").count(), 6)
        self.assertLessEqual(dialog.locator("[data-clip]").count(), 3 + 3)
        dialog.locator('[data-step="10"]').click()
        dialog.locator("[data-clip-brightness]").fill("120")
        dialog.locator("[data-clip-evidence]").check()
        page.wait_for_function("() => document.querySelector('[data-clip-save]') && !document.querySelector('[data-clip-save]').disabled", timeout=15000)
        dialog.locator("[data-submit]").click()
        page.locator('[data-unpick="1"]').wait_for(state="attached", timeout=15000)
        slide = self.app.production.carousel(self.carousel["id"])["slides"][0]
        self.assertEqual(slide["base"]["origin"], "clip")
        self.assertEqual(slide["base"]["adjustments"]["brightness"], 1.2)
        self.assertEqual(slide["base"]["adjustments"]["rotate"], 0)
        self.assertEqual((round(slide["base"]["adjustments"]["crop"]["w"], 4), round(slide["base"]["adjustments"]["crop"]["h"], 4)), (0.5625, 1.0))
        self.assertTrue(slide["base"]["evidence"])
        self.assertEqual(slide["base"]["height"] / slide["base"]["width"], 240 / 180)

    def test_open_in_davinci_sends_the_clip_position(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.locator('[data-clip-slide="2"]').click()
        dialog = page.locator("dialog.clip-dialog")
        dialog.locator("[data-clip]").first.wait_for(timeout=15000)
        dialog.locator("[data-clip]").first.click()
        dialog.locator("[data-clip-reveal]").wait_for(timeout=15000)
        page.wait_for_function("() => document.querySelector('[data-clip-save]') && !document.querySelector('[data-clip-save]').disabled", timeout=15000)
        dialog.locator("[data-clip-reveal]").click()
        page.wait_for_function("() => document.body.textContent.includes('已在达芬奇打开')", timeout=15000)
        call = [entry for entry in self.runner.calls if entry["command"] == "reveal"][-1]
        self.assertEqual(call["unique_id"], "uid-material")
        self.assertGreaterEqual(call["frame"], 0)

    def test_ai_picks_clips_from_labels_and_the_picker_shows_them_first(self):
        import json as _json
        asked = []

        def answer(prompt, **options):
            asked.append(prompt)
            keys = [row["key"] for row in self.app.carousel_clips(self.carousel["id"], ["蓝点折刀成品素材"])["clips"]]
            payload = {"slides": {"1": [{"key": keys[1], "reason": "标签写了夹刀"}]}, "gaps": []}
            return "```json" + _json.dumps(payload, ensure_ascii=False) + "```"

        self.catalog.run = answer
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        self.assertEqual(page.locator("#still-form").count(), 0)  # the AI frame search form is gone
        page.locator('#still-panel input[name="material"]').first.wait_for()
        page.locator('#still-panel input[name="material"]').first.check()
        page.locator("#clip-ai-pick").click()
        page.wait_for_function("() => document.querySelector('#clip-ai-summary')", timeout=20000)
        self.assertEqual(len(asked), 1)
        self.assertIn("片段清单", asked[0])
        self.assertIn("AI 1", page.locator('[data-clip-slide="1"]').inner_text())
        page.locator('[data-clip-slide="1"]').click()
        dialog = page.locator("dialog.clip-dialog")
        dialog.locator(".clip-reason").first.wait_for(timeout=15000)
        self.assertIn("标签写了夹刀", dialog.locator(".clip-reason").first.inner_text())
        self.assertIn("AI 按标签推荐", dialog.locator("[data-clip-list]").inner_text())

    def test_upload_a_local_picture_and_use_it_as_the_base(self):
        from PIL import Image as PILImage
        picture = self.root / "local-photo.jpg"
        PILImage.new("RGB", (240, 320), "teal").save(picture, "JPEG")
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.locator('[data-import-slide="1"]').click()
        dialog = page.locator("dialog[open]")
        dialog.locator("[data-upload-file]").set_input_files(str(picture))
        page.wait_for_function("() => !document.querySelector('dialog[open] [data-upload-file]')", timeout=15000)
        slide = self.app.production.carousel(self.carousel["id"])["slides"][0]
        self.assertEqual(slide["base"]["origin"], "upload")
        self.assertEqual(slide["base"]["original_name"], "local-photo.jpg")
        self.assertEqual([entry["id"] for entry in slide["candidates"]], [slide["base"]["id"]])

    def test_drop_pictures_straight_onto_a_slide_tile(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.locator('[data-step="pick"]').click()
        page.locator('[data-pick-slide="1"]').wait_for()
        drop = """async (spec) => {
            const tile = document.querySelector(`[data-pick-slide="${spec.slide}"]`);
            const transfer = new DataTransfer();
            for (const [index, color] of spec.colors.entries()) {
              const canvas = document.createElement('canvas');canvas.width = 60;canvas.height = 80;
              const context = canvas.getContext('2d');context.fillStyle = color;context.fillRect(0,0,60,80);
              const blob = await new Promise(resolve => canvas.toBlob(resolve,'image/png'));
              transfer.items.add(new File([blob],`dropped-${index}.png`,{type:'image/png'}));
            }
            const entered = new DragEvent('dragenter',{dataTransfer:transfer,bubbles:true,cancelable:true});
            tile.dispatchEvent(entered);
            const highlighted = tile.classList.contains('drop-over');
            tile.dispatchEvent(new DragEvent('drop',{dataTransfer:transfer,bubbles:true,cancelable:true}));
            return {highlighted, prevented: entered.defaultPrevented};
        }"""
        result = page.evaluate(drop, {"slide": 1, "colors": ["red"]})
        self.assertEqual(result, {"highlighted": True, "prevented": True})
        page.wait_for_function("() => document.querySelector('[data-pick-slide=\"1\"] [data-unpick]')", timeout=15000)
        slide = self.app.production.carousel(self.carousel["id"])["slides"][0]
        self.assertEqual((slide["base"]["origin"], len(slide["candidates"])), ("upload", 1))
        # several pictures stay candidates for the user to choose from
        page.evaluate(drop, {"slide": 2, "colors": ["green", "blue"]})
        page.wait_for_function("() => document.querySelectorAll('[data-pick-slide=\"2\"] .pick-card').length === 2", timeout=15000)
        second = self.app.production.carousel(self.carousel["id"])["slides"][1]
        self.assertEqual((second["base"], len(second["candidates"])), (None, 2))


if __name__ == "__main__":
    unittest.main()
