"""Phase-three image generation and approval through an isolated HTTP workbench."""
from __future__ import annotations

import json
import re
from pathlib import Path
import threading
import time
import unittest
from uuid import uuid4

from PIL import Image

from codex_bridge import BridgeCancelled
import test_production_carousel_ui as carousel_fixture


class FakeImages:
    direct_image_edit = True
    source = "cockpit_image_edit"

    def __init__(self):
        self.calls = []
        self.hold = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.risky_slides = {2}

    def status(self):
        return {"available": True, "label": "Isolated image fixture", "source": self.source, "review_available": False}

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
        self.app.image_reviewer = self.images
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
        page.locator("#carousel-images-service").get_by_text("Isolated image fixture已就绪。", exact=True).wait_for(state="attached")
        return page

    def generate(self, page, slides=None):
        page.locator('[data-step="images"]').click()
        folded = page.locator('[data-production-controls="images"]')
        if folded.count() and folded.get_attribute('open') is None:
            folded.locator('summary').first.click()
        # Finished groups keep alternate-generation controls behind explicit folds.
        page.locator('.image-redo-controls').evaluate_all('nodes => nodes.forEach(node => {node.open = true;})')
        if slides is not None or not page.locator('[data-redo-pick]:checked').count():
            for checkbox in page.locator('[data-redo-pick]').all():
                checkbox.set_checked(slides is None or int(checkbox.get_attribute("data-redo-pick")) in slides)
        selected = [int(node.get_attribute("data-redo-pick")) for node in page.locator('[data-redo-pick]:checked').all()]
        before = self.detail()
        count_before = sum(len(slide.get("outputs", [])) for slide in before["slides"])
        # Every chosen slide is made in one run; nothing waits for the first slide to be confirmed.
        self.assertEqual(page.locator("#carousel-generate-first").count(), 0)
        if page.locator('#carousel-generate-picked').is_visible():
            page.locator('#carousel-generate-picked').click()
        else:
            page.locator('#carousel-redo-picked').click()
            modal = page.locator('.production-dialog[open]')
            modal.get_by_role('button', name='开始重做', exact=True).click()
            modal.wait_for(state='detached')
        page.locator(".carousel-output").first.wait_for(timeout=15000)
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
        page.locator(f'[data-image-slide="{number}"] [data-revoke-output]').wait_for(state="attached")

    def detail(self):
        return self.request("GET", f"/api/production/carousels/{self.carousel['id']}")[2]["carousel"]

    def test_generate_direct_approve_redo_keep_versions_and_invalidate_after_base_changes(self):
        original = self.source.read_bytes()
        script = self.app.creation.get_script(self.carousel["script_id"])
        page = self.open_images()
        page.locator('#carousel-image-form [name="instruction"]').fill("背景简洁，保留原主体")
        self.generate(page)
        self.assertEqual(page.locator(".carousel-output").count(), 3)
        self.assertEqual(page.locator('.carousel-output figcaption,.carousel-output details,.carousel-output p,.image-review,.image-stages,[data-recheck-output]').count(), 0)
        self.assertEqual(page.locator('.carousel-output').nth(1).locator('[data-approve-output]').inner_text(), '确认选用')
        self.assertIn('无自动核对记录', page.locator('.carousel-output').nth(1).locator('.image-review-empty').inner_text())
        self.assertEqual(page.locator('#carousel-images-panel h2').inner_text(), '出图')
        self.assertTrue(all(not call['review'] for call in self.images.calls))
        self.assertEqual(page.locator(".carousel-output img").first.evaluate("img => getComputedStyle(img).objectFit"), "contain")
        self.approve(page, 1, note="已核对主体和文字")
        self.approve(page, 2, acknowledge=True, note="人工逐字核对后保留")
        self.approve(page, 3)
        page.locator('.page-heading .script-status').get_by_text("可领取", exact=True).wait_for()
        self.assertEqual(self.detail()["status"], "approved")
        previous = self.detail()["slides"][2]["output_id"]
        page.locator('[data-step="images"]').click()
        page.locator('[data-image-slide="3"] .image-redo-controls > summary').click()
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
        page.locator('[data-image-slide="3"] [data-image-versions-toggle]').click()
        page.locator(f'[data-output-id="{previous}"] [data-approve-output]').click()
        page.locator(f'[data-output-id="{previous}"] [data-revoke-output]').wait_for(state="attached")
        self.assertEqual(self.detail()["slides"][2]["output_id"], previous)
        page.locator('[data-step="images"]').click()
        page.locator(f'[data-output-id="{previous}"] [data-revoke-output]').click()
        page.locator(f'[data-output-id="{previous}"] [data-approve-output]').wait_for(state="attached")
        self.assertFalse(next(output for output in self.detail()["slides"][2]["outputs"] if output["id"] == previous)["approved"])
        replacement = self.source.parent / "replacement.jpg"
        Image.new("RGB", (120, 160), "navy").save(replacement)
        self.add_base(3, "202H-S2/原图/replacement.jpg")
        page.reload()
        page.locator(f'[data-output-id="{previous}"].stale').wait_for(state="attached")
        self.assertTrue(page.locator(f'[data-output-id="{previous}"] [data-approve-output]').is_disabled())
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.app.creation.get_script(script["id"]), script)
        self.assertTrue(any(call["instruction"] == "把底部正文留白加大" for call in self.images.calls))

    def test_approve_all_in_one_click_then_redo_several_picked_slides_together(self):
        page = self.open_images()
        self.generate(page)
        approve_all = page.locator("#carousel-approve-all")
        approve_all.get_by_text("一键全部选用（3 张）", exact=True).wait_for()
        approve_all.click()
        page.locator("#carousel-approve-all").get_by_text("已全部选用", exact=True).wait_for(state="attached", timeout=15000)
        item = self.detail()
        self.assertEqual(item["status"], "approved")
        self.assertTrue(all(slide["outputs"][-1]["approved"] for slide in item["slides"]))
        self.assertEqual(page.locator("[data-revoke-output]").count(), 3)
        calls_before = len(self.images.calls)
        page.locator('[data-production-controls="images"] > summary').click()
        page.locator('[data-image-slide="1"] .image-redo-controls > summary').click()
        page.locator('[data-image-slide="3"] .image-redo-controls > summary').click()
        page.locator('[data-redo-pick="1"]').check()
        page.locator('[data-redo-pick="3"]').check()
        page.locator("#carousel-redo-picked").get_by_text("重做 2 张", exact=True).click()
        dialog = page.locator(".production-dialog[open]")
        dialog.locator('[name="instruction"]').fill("三张统一把文字放大")
        dialog.get_by_role("button", name="开始重做", exact=True).click()
        page.wait_for_function("() => document.querySelectorAll('[data-image-slide=\"1\"] .carousel-output').length === 2 && document.querySelectorAll('[data-image-slide=\"3\"] .carousel-output').length === 2", timeout=15000)
        page.wait_for_function("() => !document.querySelector('[data-cancel-generation]')", timeout=15000)
        run = self.detail()["generations"][-1]
        self.assertEqual(run["slides"], [1, 3])
        redone = [call for call in self.images.calls[calls_before:] if not call["review"]]
        self.assertEqual(sorted(call["slide"] for call in redone), [1, 3])
        self.assertTrue(all(call["instruction"] == "三张统一把文字放大" for call in redone))
        self.assertEqual(len(self.detail()["slides"][1]["outputs"]), 1)
        self.assertEqual(page.locator("[data-redo-pick]:checked").count(), 0)

    def test_progress_keeps_draft_and_running_job_restores_after_reload_then_stops(self):
        self.images.hold = True
        page = self.open_images(width=390)
        page.locator('#carousel-image-form [name="aspect"]').select_option("9:16")
        page.locator('#carousel-generate-picked').click()
        page.locator('[data-cancel-generation]').wait_for()
        page.locator('#carousel-image-form [name="instruction"]').fill("下次出图时主体再放大")
        page.locator('#carousel-image-runs').get_by_text(re.compile(r"^第 \d 张：隔离测试正在出图$")).first.wait_for()
        page.wait_for_timeout(3200)
        self.assertEqual(page.locator('#carousel-image-form [name="instruction"]').input_value(), "下次出图时主体再放大")
        page.reload()
        page.locator('[data-cancel-generation]').wait_for()
        self.assertEqual(page.locator('#carousel-image-form [name="aspect"]').input_value(), "9:16")
        self.assertTrue(page.locator('#carousel-generate-picked').is_disabled())
        page.locator('[data-cancel-generation]').click()
        page.locator('#carousel-image-runs .script-status').get_by_text("已停止", exact=True).wait_for(timeout=15000)
        self.assertEqual(self.detail()["generations"][-1]["status"], "cancelled")
        self.assertEqual(page.locator(".carousel-output").count(), 0)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_task_result_link_focuses_the_image_without_opening_logs(self):
        page = self.open_images()
        self.generate(page, slides=[1])
        item = self.detail()
        output = item["slides"][0]["outputs"][0]
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}?step=images&run={output['generation_id']}")
        page.locator(f'[data-output-id="{output["id"]}"]').wait_for()
        page.wait_for_function("id => document.activeElement?.dataset.outputId === id", arg=output["id"])
        self.assertEqual(page.locator('.production-steps [aria-selected="true"]').get_attribute('data-step'), 'images')
        self.assertEqual(page.locator('.task-activity-logs details[open]').count(), 0)

    def test_aspect_change_warns_and_invalidates_old_group_versions(self):
        self.images.risky_slides.clear()
        page = self.open_images(width=390)
        self.generate(page)
        before = [slide["outputs"][0]["id"] for slide in self.detail()["slides"]]
        page.locator('#carousel-image-form [name="aspect"]').select_option("9:16")
        page.locator('#carousel-image-aspect-note').get_by_text("整组都需按新比例重新出图", exact=False).wait_for()
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
