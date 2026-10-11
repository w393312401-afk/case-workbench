"""Existing finished pictures through the UI, with an isolated read-only review fixture."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
import zipfile

from PIL import Image, ImageDraw

from codex_bridge import BridgeError
import test_production_carousel_ui as carousel_fixture


class ReadOnlyReviewer:
    def __init__(self):
        self.calls = []
        self.fail_next = False

    def status(self):
        return {"available": True, "label": "Isolated read-only review fixture"}

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, read_only=False, **options):
        snapshot = json.loads((Path(cwd) / "input.json").read_text(encoding="utf-8"))
        self.calls.append({"read_only": read_only, "slide": snapshot["slide"]["number"],
                           "scope": snapshot.get("scope"), "requirements": snapshot["requirements"]})
        if not read_only:
            raise AssertionError("Existing-finished UI must never generate a picture")
        if self.fail_next:
            self.fail_next = False
            raise BridgeError("隔离测试：核对暂时失败，已有图片保留")
        slide = snapshot["slide"]
        return json.dumps({"summary": "隔离界面核对回执，不代替实图视觉验收",
                           "observed_title_lines": slide["title_lines"], "observed_body": slide["body"],
                           "checks": [{"id": key, "status": "pass", "note": "隔离界面测试"}
                                      for key in ("visual", "text", "product", "evidence")],
                           "issues": []}, ensure_ascii=False)


class CarouselFinishedBrowserTests(unittest.TestCase):
    setUpClass = classmethod(carousel_fixture.CarouselBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(carousel_fixture.CarouselBrowserTests.tearDownClass.__func__)
    stop_server = carousel_fixture.CarouselBrowserTests.stop_server
    request = carousel_fixture.CarouselBrowserTests.request
    make_playable_video = carousel_fixture.CarouselBrowserTests.make_playable_video

    def open_page(self, hash_route="#library"):
        page = carousel_fixture.CarouselBrowserTests.open_page(self, hash_route)
        page.locator('[data-step="images"]').click()
        return page

    def setUp(self):
        carousel_fixture.CarouselBrowserTests.setUp(self)
        self.reviewer = ReadOnlyReviewer()
        self.app.image_generator = self.reviewer
        self.app.carousel_finished_root = self.root / "图文成品"
        self.folder = self.make_finished("ui-finished")
        self.addCleanup(self.app.stop_images)

    def detail(self):
        return self.request("GET", f"/api/production/carousels/{self.carousel['id']}")[2]["carousel"]

    def make_finished(self, name, offset=0):
        folder = self.app.carousel_finished_root / name
        for subfolder in ("PNG", "JPG", "sources", "native"):
            (folder / subfolder).mkdir(parents=True)
        item = self.detail()
        for slide in item["slides"]:
            number = slide["number"]
            image = Image.new("RGB", (240, 320), "#d4c4a6")
            draw = ImageDraw.Draw(image)
            draw.rectangle((40 + offset, 88, 196, 236), fill="#374653", outline="#ebbb48", width=4)
            draw.rectangle((60, 115 + number * 8, 180, 126 + number * 8), fill="#b0bec7")
            draw.text((16, 18), " / ".join(slide["title_lines"]), fill="#1c252a")
            draw.text((16, 278), slide["body"], fill="#1c252a")
            image.save(folder / "PNG" / f"{number:02}.png")
            image.save(folder / "JPG" / f"{number:02}.jpg", quality=95)
            image.save(folder / "sources" / f"{number:02}.png")
        locked = {"direction_id": item["direction_id"],
                  "slides": [{key: slide.get(key) for key in ("number", "title_lines", "body", "title_zh", "body_zh")}
                             for slide in item["slides"]],
                  "caption_en": item["caption_en"], "caption_zh": item["caption_zh"]}
        (folder / "copy.json").write_text(json.dumps(locked, ensure_ascii=False), encoding="utf-8")
        (folder / "native" / "fixture.txt").write_text("明确的隔离测试档案，没有生图回执。", encoding="utf-8")
        return folder

    def import_ui(self, page, folder):
        page.locator("#carousel-import-finished").click()
        modal = page.locator(".production-dialog[open]")
        modal.locator('[name="directory"]').fill(str(folder))
        modal.locator('[name="import_hands_authorized"]').check()
        modal.get_by_role("button", name="登记为待核对候选", exact=True).click()
        modal.wait_for(state="detached")
        page.locator(".carousel-output").first.wait_for()

    def recheck_ui(self, page, output_id):
        page.locator(f'[data-output-id="{output_id}"] [data-recheck-output]').click()
        modal = page.locator(".production-dialog[open]")
        modal.get_by_text("不会再次生图", exact=False).wait_for()
        modal.get_by_role("button", name="开始重新核对", exact=True).click()
        modal.wait_for(state="detached")
        page.wait_for_function("id => { const n=document.querySelector(`[data-output-id='${id}'] [data-recheck-output]`); return n && !n.disabled; }", arg=output_id)

    def confirm_ui(self, page, output_id, actor="human"):
        page.locator(f'[data-output-id="{output_id}"] [data-approve-output]').click()
        modal = page.locator(".production-dialog[open]")
        if modal.locator('[name="actor"]').count():
            self.assertEqual(modal.locator('[name="actor"]').input_value(), "human")
            modal.locator('[name="actor"]').select_option(actor)
        if modal.locator('[name="acknowledge"]').count():
            modal.locator('[name="acknowledge"]').check()
        modal.get_by_role("button", name="确认并选用", exact=True).click()
        page.locator(f'[data-output-id="{output_id}"] [data-revoke-output]').wait_for(state="attached")

    def test_import_without_base_is_pending_deduplicated_and_can_be_reviewed_without_generation(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator('[data-step="pick"]').click()
        page.locator('[data-import-slide="1"]').get_by_text("导入底图", exact=True).wait_for()
        page.locator('[data-step="images"]').click()
        self.import_ui(page, self.folder)
        self.assertEqual(page.locator(".carousel-output").count(), 3)
        page.locator(".carousel-output").first.get_by_text("模型：未知", exact=False).wait_for()
        self.assertTrue(page.locator('[data-image-slide="1"] [data-approve-output]').is_disabled())
        first = self.detail()["slides"][0]["outputs"][0]
        self.assertFalse(first["approved"])
        self.assertIsNone(first["generation_id"])
        self.import_ui(page, self.folder)
        self.assertEqual(page.locator(".carousel-output").count(), 3)
        self.recheck_ui(page, first["id"])
        self.confirm_ui(page, first["id"], actor="assistant_on_user_authorization")
        confirmed = next(output for output in self.detail()["slides"][0]["outputs"] if output["id"] == first["id"])
        self.assertEqual(confirmed["approval_actor"], "assistant_on_user_authorization")
        page.locator(f'[data-output-id="{first["id"]}"] figcaption').get_by_text("确认来源：助手按用户授权核对", exact=True).wait_for()
        page.wait_for_function("() => { const n=document.querySelector('#carousel-first-preflight [data-preflight-result]'); return n && n.textContent && !n.classList.contains('ai-error'); }")
        self.assertTrue(self.request("GET", f"/api/production/carousels/{self.carousel['id']}/preflight")[2]["first_ready"])
        self.assertTrue(page.locator("#carousel-prepare-delivery").is_disabled())
        self.assertTrue(all(call["read_only"] for call in self.reviewer.calls))
        self.assertEqual(self.detail()["generations"], [])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_failed_new_candidate_retains_confirmed_selection_and_independent_retry_can_select(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        old = self.detail()["slides"][0]["outputs"][0]["id"]
        self.recheck_ui(page, old)
        self.confirm_ui(page, old)
        second_folder = self.make_finished("ui-new-candidate", offset=8)
        self.import_ui(page, second_folder)
        slide = self.detail()["slides"][0]
        new = slide["outputs"][-1]["id"]
        self.assertEqual(slide["output_id"], old)
        self.reviewer.fail_next = True
        self.recheck_ui(page, new)
        self.assertTrue(page.locator(f'[data-output-id="{new}"] [data-approve-output]').is_disabled())
        self.assertEqual(self.detail()["slides"][0]["output_id"], old)
        page.locator(f'[data-output-id="{new}"] .ai-error').get_by_text("核对暂时失败", exact=False).wait_for()
        self.recheck_ui(page, new)
        self.confirm_ui(page, new)
        self.assertEqual(self.detail()["slides"][0]["output_id"], new)
        self.confirm_ui(page, old)
        self.assertEqual(self.detail()["slides"][0]["output_id"], old)
        self.assertTrue(all(call["read_only"] for call in self.reviewer.calls))

    def test_ui_download_tracks_collection_of_selected_bytes_and_keeps_unpublished(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        for slide in self.detail()["slides"]:
            output_id = slide["outputs"][0]["id"]
            self.recheck_ui(page, output_id)
            self.confirm_ui(page, output_id)
        page.locator('[data-step="delivery"]').click()
        # The confirmed group is already packaged; pressing the button reuses that package.
        page.locator("#carousel-prepare-delivery").click()
        page.locator('#toast:has-text("成品包已本地导出")').wait_for()
        self.assertEqual(len(self.detail()["deliveries"]), 1)
        page.locator("[data-download-delivery]").wait_for()
        page.locator(".delivery-package-state").get_by_text("尚未领取", exact=False).wait_for()
        with page.expect_download() as pending:
            page.locator("[data-download-delivery]").click()
        download = pending.value
        target = self.root / "ui-picked-up.zip"
        download.save_as(target)
        with zipfile.ZipFile(target) as archive:
            for number in (1, 2, 3):
                self.assertEqual(hashlib.sha256(archive.read(f"{number:02}.png")).hexdigest(),
                                 hashlib.sha256((self.folder / "PNG" / f"{number:02}.png").read_bytes()).hexdigest())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["carousel_id"], self.carousel["id"])
        page.locator(".delivery-package-state").get_by_text("已领取", exact=False).wait_for()
        page.locator(".delivery-publication-state").get_by_text("未发布", exact=True).wait_for()
        self.assertIsNone(self.detail().get("publication"))

    def test_caption_style_is_layout_only_and_does_not_change_generation_requirements(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        field = page.locator('[data-caption-style="image"]')
        self.assertTrue(field.is_hidden())
        page.locator('#carousel-requirements-toggle > summary').click()
        page.locator('[name="image_mode"]').select_option("layout_only")
        self.assertTrue(field.is_visible())
        page.locator('[name="image_caption_style"]').select_option("rugged")
        page.locator('[name="image_caption_area"]').select_option("top")
        page.locator('#carousel-image-form').get_by_text("普通文字或粗犷花字排版可复用已有画面，无需生图。", exact=False).wait_for()
        page.locator('#carousel-save-requirements').click()
        page.wait_for_function("() => document.querySelector('[name=\"image_caption_style\"]')?.value === 'rugged' && document.querySelector('[data-caption-style=\"image\"]')?.hidden === false")
        self.assertEqual(self.detail()["requirements"]["caption_style"], "rugged")
        page.locator('[name="image_mode"]').select_option("generated_visual")
        self.assertTrue(field.is_hidden())
        page.locator('#carousel-save-requirements').click()
        page.wait_for_function("() => document.querySelector('[name=\"image_mode\"]')?.value === 'generated_visual' && !document.querySelector('#carousel-save-requirements')?.disabled")
        self.assertNotIn("caption_style", self.detail()["requirements"])
        self.assertEqual(self.reviewer.calls, [])
        self.assertEqual(self.detail()["generations"], [])

    def test_confirmation_conflict_preserves_actor_note_and_requires_manual_retry(self):
        page = self.open_page(f"#production/{self.carousel['id']}")
        self.import_ui(page, self.folder)
        output_id = self.detail()["slides"][0]["outputs"][0]["id"]
        self.recheck_ui(page, output_id)
        page.locator(f'[data-output-id="{output_id}"] [data-approve-output]').click()
        modal = page.locator('.production-dialog[open]')
        modal.locator('[name="actor"]').select_option("assistant_on_user_authorization")
        modal.locator('[name="note"]').fill("已按授权核对，保留生成视觉版的实拍证据限制")
        modal.locator('[name="acknowledge"]').check()
        sent = []

        def conflict_once(route):
            sent.append(route.request.post_data_json)
            if len(sent) == 1:
                self.app.production.mutate("carousel_archive", {"request_id": "ui-conflict-" + output_id,
                    "revision": self.app.production.public()["revision"], "carousel_id": self.carousel["id"], "archived": False})
                route.fulfill(status=409, content_type="application/json",
                              body=json.dumps({"error": "制作资料已更新"}, ensure_ascii=False))
            else:
                route.continue_()

        page.route("**/api/production/carousels/*/approval", conflict_once)
        modal.get_by_role("button", name="确认并选用", exact=True).click()
        modal.locator('[data-error]').get_by_text("资料已更新，输入已保留，检查后重试。", exact=False).wait_for()
        self.assertEqual(len(sent), 1, "The UI must not auto-submit after refreshing the revision")
        self.assertEqual(modal.locator('[name="actor"]').input_value(), "assistant_on_user_authorization")
        self.assertEqual(modal.locator('[name="note"]').input_value(), "已按授权核对，保留生成视觉版的实拍证据限制")
        self.assertTrue(modal.locator('[name="acknowledge"]').is_checked())
        modal.get_by_role("button", name="确认并选用", exact=True).click()
        page.locator(f'[data-output-id="{output_id}"] [data-revoke-output]').wait_for(state="attached")
        self.assertEqual(len(sent), 2)
        self.assertNotEqual(sent[1]["revision"], sent[0]["revision"])
        saved = next(output for output in self.detail()["slides"][0]["outputs"] if output["id"] == output_id)
        self.assertEqual(saved["approval_actor"], "assistant_on_user_authorization")
        self.assertEqual(saved["note"], "已按授权核对，保留生成视觉版的实拍证据限制")


if __name__ == "__main__":
    unittest.main()
