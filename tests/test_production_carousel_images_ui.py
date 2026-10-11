"""Phase-three image generation and approval through an isolated HTTP workbench."""
from __future__ import annotations

import json
from pathlib import Path
import threading
import time
import unittest
from uuid import uuid4

from PIL import Image

from codex_bridge import BridgeCancelled
import test_production_carousel_ui as carousel_fixture


class FakeImages:
    def __init__(self):
        self.calls = []
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.risky_slides = {2}

    def status(self):
        return {"available": True, "label": "Isolated image fixture"}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, read_only=False, **options):
        snapshot = json.loads((Path(cwd) / "input.json").read_text(encoding="utf-8"))
        slide = snapshot["slide"]
        self.calls.append({"slide": slide["number"], "review": read_only,
                           "aspect": snapshot["aspect"], "instruction": snapshot["instruction"]})
        on_progress("隔离测试正在核对" if read_only else "隔离测试正在出图")
        if not read_only and self.hold:
            self.entered.set()
            while not self.release.wait(.02):
                if cancel_event.is_set():
                    raise BridgeCancelled("Fixture cancelled")
        if cancel_event.is_set():
            raise BridgeCancelled("Fixture cancelled")
        if read_only:
            risky = slide["number"] in self.risky_slides
            return json.dumps({"summary": "需要人工核对文字" if risky else "四项核对通过",
                               "observed_title_lines": slide["title_lines"], "observed_body": slide["body"],
                               "checks": [{"id": key, "status": "uncertain" if risky and key == "text" else "pass",
                                           "note": "请逐字核对" if risky and key == "text" else "隔离核对通过"}
                                          for key in ("subject", "evidence", "text", "layout")],
                               "issues": ["文字需要人工确认"] if risky else []}, ensure_ascii=False)
        count = sum(not call["review"] for call in self.calls)
        size = (180, 320) if snapshot["aspect"] == "9:16" else (180, 240)
        Image.new("RGB", size, (40 + count * 12, 80, 160)).save(Path(cwd) / "render.png")
        return json.dumps({"file": "render.png", "summary": "隔离生成结果"})


class CarouselImageBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    open_page = carousel_fixture.CarouselBrowserTests.open_page
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def setUp(self):
        carousel_fixture.CarouselBrowserTests.setUp(self)
        self.images = FakeImages()
        self.app.image_generator = self.images
        self.addCleanup(self.stop_images)
        for number in (1, 2, 3):
            self.add_base(number, "202H-S2/原图/03.jpg")

    def stop_images(self):
        self.app.stop_images()
        self.images.release.set()
        deadline = time.monotonic() + 5
        while self.app.image_running() and time.monotonic() < deadline:
            time.sleep(.02)

    def add_base(self, number, path):
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/import",
                                         {"request_id": uuid4().hex, "slide": number, "path": path})
        self.assertEqual(status, 200, result)
        candidate = result["candidate"]
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/base",
                                         {"request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
                                          "slide": number, "candidate": candidate["id"]})
        self.assertEqual(status, 200, result)

    def open_images(self, width=1080):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.set_viewport_size({"width": width, "height": 900 if width > 600 else 844})
        page.locator('[data-step="images"]').click()
        page.locator("#carousel-images-service").get_by_text("Codex 出图服务已就绪。", exact=True).wait_for()
        return page

    def generate(self, page, slides=None):
        page.locator('[data-step="images"]').click()
        if slides is not None:
            for checkbox in page.locator('#carousel-image-form input[name="image_slide"]').all():
                checkbox.set_checked(int(checkbox.get_attribute("value")) in slides)
        selected = [int(node.get_attribute("value")) for node in page.locator('#carousel-image-form input[name="image_slide"]:checked').all()]
        before = self.detail()
        count_before = sum(len(slide.get("outputs", [])) for slide in before["slides"])
        first = next((output for output in before["slides"][0].get("outputs", []) if output["id"] == before["slides"][0].get("output_id")), None)
        gated = len(selected) > 1 and not (first and first.get("approved") and not first.get("stale"))
        page.locator('#carousel-image-form button[type="submit"]').click()
        page.locator(".carousel-output").first.wait_for(timeout=15000)
        if gated:
            page.locator('#carousel-image-runs .script-status').get_by_text("首张待确认", exact=True).wait_for(timeout=15000)
            checkpoint = self.detail()
            self.assertEqual(checkpoint["generations"][-1]["status"], "awaiting_first_approval")
            self.assertEqual(checkpoint["generations"][-1]["slides"], [1])
            self.assertTrue(page.locator("#carousel-prepare-delivery").is_disabled())
            self.assertTrue(page.locator("#carousel-continue-images").is_disabled())
            self.approve(page, 1)
            page.locator("#carousel-continue-images:not([disabled])").wait_for()
            self.assertTrue(self.request("GET", f"/api/production/carousels/{self.carousel['id']}/preflight")[2]["first_ready"])
            page.locator("#carousel-continue-images").click()
        page.wait_for_function("count => document.querySelectorAll('.carousel-output').length >= count", arg=count_before + len(selected), timeout=15000)
        page.wait_for_function("() => !document.querySelector('[data-cancel-generation]')", timeout=15000)
        page.wait_for_function("() => [...document.querySelectorAll('.carousel-output img')].every(img => img.complete && img.naturalWidth > 0)")

    def approve(self, page, number, *, acknowledge=False, note=""):
        page.locator('[data-step="images"]').click()
        selected = page.locator(f'[data-image-slide="{number}"] [data-revoke-output]')
        if selected.count():
            selected.click()
            selected.wait_for(state="detached")
        page.locator(f'[data-image-slide="{number}"] [data-approve-output]').first.click()
        modal = page.locator(".production-dialog[open]")
        modal.locator('[name="note"]').fill(note)
        if acknowledge:
            self.assertTrue(modal.locator('[data-submit]').is_disabled())
            modal.locator('[name="acknowledge"]').check()
        modal.get_by_role("button", name="确认并选用", exact=True).click()
        page.locator(f'[data-image-slide="{number}"] [data-revoke-output]').wait_for(state="attached")

    def detail(self):
        return self.request("GET", f"/api/production/carousels/{self.carousel['id']}")[2]["carousel"]

    def test_generate_review_approve_redo_keep_versions_and_invalidate_after_base_changes(self):
        original = self.source.read_bytes()
        script = self.app.creation.get_script(self.carousel["script_id"])
        page = self.open_images()
        page.locator('#carousel-image-form [name="instruction"]').fill("背景简洁，保留原主体")
        self.generate(page)
        self.assertEqual(page.locator(".carousel-output").count(), 3)
        self.assertEqual(page.locator(".carousel-output img").first.evaluate("img => getComputedStyle(img).objectFit"), "contain")
        self.approve(page, 1, note="已核对主体和文字")
        self.approve(page, 2, acknowledge=True, note="人工逐字核对后保留")
        self.approve(page, 3)
        page.locator('.page-heading .script-status').get_by_text("成图已确认", exact=True).wait_for()
        self.assertEqual(self.detail()["status"], "approved")
        previous = self.detail()["slides"][2]["output_id"]
        page.locator('[data-step="images"]').click()
        page.locator('[data-redo-slide="3"]').click()
        modal = page.locator(".production-dialog[open]")
        modal.locator('[name="instruction"]').fill("把底部正文留白加大")
        modal.get_by_role("button", name="开始重做", exact=True).click()
        page.wait_for_function("() => document.querySelectorAll('[data-image-slide=\"3\"] .carousel-output').length === 2", timeout=15000)
        third = self.detail()["slides"][2]
        self.assertEqual(third["output_id"], previous, "A new candidate must preserve the confirmed selection")
        self.assertEqual(len(third["outputs"]), 2)
        self.assertEqual(self.request("GET", f"/api/production/carousels/{self.carousel['id']}/outputs/{previous}?size=full")[0], 200)
        self.approve(page, 3)
        self.assertNotEqual(self.detail()["slides"][2]["output_id"], previous)
        page.locator('[data-step="images"]').click()
        page.locator(f'[data-output-id="{previous}"] [data-approve-output]').click()
        page.locator('.production-dialog[open]').get_by_role("button", name="确认并选用", exact=True).click()
        page.locator(f'[data-output-id="{previous}"] [data-revoke-output]').wait_for(state="attached")
        self.assertEqual(self.detail()["slides"][2]["output_id"], previous)
        page.locator('[data-step="images"]').click()
        page.locator(f'[data-output-id="{previous}"] [data-revoke-output]').click()
        page.locator(f'[data-output-id="{previous}"] [data-approve-output]').wait_for()
        self.assertFalse(next(output for output in self.detail()["slides"][2]["outputs"] if output["id"] == previous)["approved"])
        replacement = self.source.parent / "replacement.jpg"
        Image.new("RGB", (120, 160), "navy").save(replacement)
        self.add_base(3, "202H-S2/原图/replacement.jpg")
        page.reload()
        page.locator(f'[data-output-id="{previous}"].stale').wait_for()
        self.assertTrue(page.locator(f'[data-output-id="{previous}"] [data-approve-output]').is_disabled())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.app.creation.get_script(script["id"]), script)
        self.assertTrue(any(call["instruction"] == "把底部正文留白加大" for call in self.images.calls))

    def test_progress_keeps_draft_and_running_job_restores_after_reload_then_stops(self):
        self.images.hold = True
        page = self.open_images(width=390)
        page.locator('#carousel-image-form [name="aspect"]').select_option("9:16")
        page.locator('#carousel-image-form button[type="submit"]').click()
        page.locator('[data-cancel-generation]').wait_for()
        page.locator('#carousel-image-form [name="instruction"]').fill("下次出图时主体再放大")
        page.locator('#carousel-image-runs').get_by_text("第 1 张：隔离测试正在出图", exact=True).wait_for()
        page.wait_for_timeout(3200)
        self.assertEqual(page.locator('#carousel-image-form [name="instruction"]').input_value(), "下次出图时主体再放大")
        page.reload()
        page.locator('[data-cancel-generation]').wait_for()
        self.assertEqual(page.locator('#carousel-image-form [name="aspect"]').input_value(), "9:16")
        self.assertTrue(page.locator('#carousel-image-form button[type="submit"]').is_disabled())
        page.locator('[data-cancel-generation]').click()
        page.locator('#carousel-image-runs .script-status').get_by_text("已停止", exact=True).wait_for(timeout=15000)
        self.assertEqual(self.detail()["generations"][-1]["status"], "cancelled")
        self.assertEqual(page.locator(".carousel-output").count(), 0)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_aspect_change_warns_and_invalidates_old_group_versions(self):
        self.images.risky_slides.clear()
        page = self.open_images(width=390)
        self.generate(page)
        before = [slide["outputs"][0]["id"] for slide in self.detail()["slides"]]
        page.locator('#carousel-image-form [name="aspect"]').select_option("9:16")
        page.locator('#carousel-image-aspect-note').get_by_text("整组都需按新比例重新出图并核对", exact=False).wait_for()
        self.generate(page, slides=[1])
        page.wait_for_function("() => document.querySelectorAll('[data-image-slide=\"1\"] .carousel-output').length === 2", timeout=15000)
        data = self.detail()
        self.assertEqual(data["image_aspect"], "9:16")
        self.assertTrue(all(next(output for output in slide["outputs"] if output["id"] == identifier)["stale"] for slide, identifier in zip(data["slides"], before)))
        page.reload()
        page.locator(f'[data-output-id="{before[1]}"].stale').wait_for()
        self.assertEqual(page.locator('#carousel-image-form [name="aspect"]').input_value(), "9:16")
        self.assertTrue(page.locator(f'[data-output-id="{before[1]}"] [data-approve-output]').is_disabled())
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
