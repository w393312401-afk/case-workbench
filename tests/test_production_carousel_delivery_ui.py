"""Carousel pickup and publication through isolated HTTP and a fake image engine."""
from __future__ import annotations

import io
import json
import unittest
from uuid import uuid4
import zipfile

import test_production_carousel_images_ui as image_fixture


class CarouselDeliveryBrowserTests(unittest.TestCase):
    setUpClass = classmethod(image_fixture.CarouselImageBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(image_fixture.CarouselImageBrowserTests.tearDownClass.__func__)
    stop_server = image_fixture.CarouselImageBrowserTests.stop_server
    request = image_fixture.CarouselImageBrowserTests.request
    open_page = image_fixture.CarouselImageBrowserTests.open_page
    make_playable_video = image_fixture.CarouselImageBrowserTests.make_playable_video
    stop_images = image_fixture.CarouselImageBrowserTests.stop_images
    add_base = image_fixture.CarouselImageBrowserTests.add_base
    open_images = image_fixture.CarouselImageBrowserTests.open_images
    generate = image_fixture.CarouselImageBrowserTests.generate
    approve = image_fixture.CarouselImageBrowserTests.approve
    detail = image_fixture.CarouselImageBrowserTests.detail

    def setUp(self):
        image_fixture.CarouselImageBrowserTests.setUp(self)
        self.images.risky_slides.clear()

    def confirm_group(self, page):
        self.generate(page)
        for number in (1, 2, 3):
            self.approve(page, number)
        page.locator('.page-heading .script-status').get_by_text("成图已确认", exact=True).wait_for()

    def prepare(self, page):
        # Confirming the whole group already packages it; pressing the button again reuses that package.
        page.locator('[data-step="delivery"]').click()
        page.locator("#carousel-prepare-delivery").click()
        page.locator('#toast:has-text("成品包已本地导出")').wait_for()
        return self.detail()["deliveries"][-1]

    def publication_dialog(self, page, delivery_id, *, post_id="7664986913497009442"):
        page.locator('[data-step="delivery"]').click()
        page.locator(f'[data-publish-delivery="{delivery_id}"]').click()
        modal = page.locator('.production-dialog[open]')
        modal.locator('[name="post_id"]').fill(post_id)
        modal.locator('[name="published_on"]').fill("2026-10-10")
        modal.locator('[name="post_url"]').fill(f"https://www.tiktok.com/@fixture/photo/{post_id}")
        modal.locator('[name="account"]').fill("fixture")
        modal.locator('[name="note"]').fill("实际使用的图片包已逐张核对")
        return modal

    def test_confirm_pickup_keep_old_pack_and_register_actual_published_package(self):
        before = self.source.read_bytes()
        page = self.open_images()
        console_errors = []
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        self.assertTrue(page.locator("#carousel-prepare-delivery").is_disabled())
        page.locator('[data-step="delivery"]').click()
        page.locator('#carousel-delivery-panel').get_by_text("还有 3 张未工作台确认", exact=False).wait_for()
        self.confirm_group(page)
        original = self.prepare(page)
        with page.expect_download() as downloaded:
            page.locator(f'[data-download-delivery="{original["id"]}"]').click()
        self.assertEqual(downloaded.value.suggested_filename, original["filename"])
        status, headers, first_zip = self.request("GET", f"/api/production/carousels/{self.carousel['id']}/deliveries/{original['id']}/download")
        self.assertEqual(status, 200)
        self.assertIn("zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(first_zip)) as archive:
            names = archive.namelist()
            for slide in original["slides"]:
                self.assertIn(slide["file"], names)
        page.evaluate("window.__deliveryCopied = ''; window.copyText = text => { window.__deliveryCopied = text; return Promise.resolve(); }")
        page.locator(f'[data-copy-delivery="{original["id"]}"]').click()
        self.assertEqual(page.evaluate("window.__deliveryCopied"), original["caption_en"])
        page.locator('[data-step="images"]').click()
        page.locator('[data-redo-slide="1"]').click()
        redo = page.locator('.production-dialog[open]')
        redo.locator('[name="instruction"]').fill("开头图片的文字留白加大")
        redo.get_by_role("button", name="开始重做", exact=True).click()
        page.wait_for_function("() => document.querySelectorAll('[data-image-slide=\"1\"] .carousel-output').length === 2", timeout=15000)
        self.assertFalse(page.locator("#carousel-prepare-delivery").is_disabled(), "The new candidate must retain the already confirmed group")
        self.approve(page, 1)
        latest = self.prepare(page)
        self.assertNotEqual(latest["id"], original["id"])
        previous_card = page.locator(f'[data-delivery-id="{original["id"]}"]')
        previous_card.get_by_text("历史版本 · 保留", exact=True).wait_for()
        status, _, retained_zip = self.request("GET", f"/api/production/carousels/{self.carousel['id']}/deliveries/{original['id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(retained_zip, first_zip)
        # Reading a package is now a real collection record. Reload the live
        # version before registering the actual historical package.
        page.reload()
        modal = self.publication_dialog(page, latest["id"])
        modal.locator('[name="delivery_id"]').select_option(original["id"])
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        page.locator('#carousel-publication').get_by_role("heading", name="已登记发布", exact=True).wait_for()
        data = self.request("GET", f"/api/production/carousels/{self.carousel['id']}")[2]
        publication = data.get("publication") or data["carousel"]["publication"]
        self.assertEqual(publication["delivery_id"], original["id"])
        self.assertEqual(publication["post_id"], "7664986913497009442")
        self.assertEqual(self.app.creation.get_script(self.carousel["script_id"])["status"], "published")
        self.assertEqual(page.locator('#carousel-publication-performance .facts-grid').count(), 0)
        page.locator(f'[data-delivery-id="{original["id"]}"]').get_by_text("用于当前发布登记", exact=True).wait_for()
        history = page.locator('#carousel-publication-history')
        self.assertEqual(history.locator('[data-publication-history]').count(), 1)
        self.assertIsNone(history.get_attribute('open'))
        history_count = len(self.detail()["history"])
        page.locator('#carousel-publication-edit').click()
        modal = page.locator('.production-dialog[open]')
        self.assertEqual(modal.locator('[name="delivery_id"]').input_value(), original["id"])
        modal.locator('[name="post_id"]').fill("7664986913497009444")
        modal.locator('[name="post_url"]').fill("https://www.tiktok.com/@fixture/photo/7664986913497009444")
        modal.locator('[name="note"]').fill("补充说明：使用的是第一包")
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        page.locator('.publication-record').get_by_text("补充说明：使用的是第一包", exact=True).wait_for()
        self.assertGreater(len(self.detail()["history"]), history_count)
        self.assertEqual(len(self.detail()["publication_history"]), 2)
        history.locator('summary').click()
        entries = history.locator('[data-publication-history]')
        self.assertEqual(entries.count(), 2)
        entries.nth(0).get_by_text("7664986913497009444", exact=True).wait_for()
        entries.nth(1).get_by_text("7664986913497009442", exact=True).wait_for()
        entries.nth(1).get_by_text("实际使用的图片包已逐张核对", exact=True).wait_for()
        entries.nth(1).get_by_text(original["id"], exact=False).wait_for()
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production")
        row = page.locator(f'[data-production-id="{self.carousel["id"]}"]')
        row.locator('.production-next-action').wait_for()
        row.click()
        page.locator('.publication-record').get_by_text("7664986913497009444", exact=True).wait_for()
        self.assertEqual(self.source.read_bytes(), before)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1081)
        self.assertEqual(console_errors, [])

    def test_publication_conflict_keeps_mobile_inputs_and_retries_with_fresh_revision(self):
        page = self.open_images(width=390)
        self.confirm_group(page)
        delivery = self.prepare(page)
        modal = self.publication_dialog(page, delivery["id"], post_id="7664986913497009443")
        modal.locator('[name="post_id"]').fill("123")
        self.assertFalse(modal.locator('[name="post_id"]').evaluate("input => input.checkValidity()"))
        modal.locator('[name="post_id"]').fill("7664986913497009443")
        fields = {name: modal.locator(f'[name="{name}"]').input_value() for name in
                  ("delivery_id", "post_id", "published_on", "post_url", "account", "note")}
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/import",
                                         {"request_id": uuid4().hex, "slide": 1, "path": "202H-S2/原图/03.jpg"})
        self.assertEqual(status, 200, result)
        with page.expect_response(lambda response: response.url.endswith('/publication')) as failed:
            modal.get_by_role("button", name="保存发布登记", exact=True).click()
        self.assertEqual(failed.value.status, 409)
        modal.locator('[data-error]').get_by_text("本次输入已保留", exact=False).wait_for()
        self.assertEqual({name: modal.locator(f'[name="{name}"]').input_value() for name in fields}, fields)
        self.assertFalse(modal.locator('[name="post_id"]').is_disabled())
        self.assertLessEqual(modal.evaluate("el => el.scrollWidth - el.clientWidth"), 1)
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        page.locator('.publication-record').get_by_text("7664986913497009443", exact=True).wait_for()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertEqual(page.locator('#carousel-publication-performance .facts-grid').count(), 0)

    def test_unconfirmed_publication_retries_original_request_after_reopening(self):
        page = self.open_images(width=390)
        self.confirm_group(page)
        delivery = self.prepare(page)
        sent = []

        def interrupted_response(route):
            sent.append(route.request.post_data_json)
            if len(sent) == 1:
                status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/publication", sent[-1])
                self.assertEqual(status, 200, result)
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"error": "隔离测试：登记回应中断"}, ensure_ascii=False))
            else:
                route.continue_()

        page.route("**/publication", interrupted_response)
        modal = self.publication_dialog(page, delivery["id"], post_id="7664986913497009445")
        fields = {name: modal.locator(f'[name="{name}"]').input_value() for name in
                  ("delivery_id", "post_id", "published_on", "post_url", "account", "note")}
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        modal.locator('[data-error]').get_by_text("请直接重试原登记", exact=False).wait_for()
        self.assertEqual(len(self.detail()["publication_history"]), 1)
        for name in fields:
            self.assertTrue(modal.locator(f'[name="{name}"]').is_disabled())
        self.assertFalse(modal.locator('[data-submit]').is_disabled())
        modal.get_by_role("button", name="取消", exact=True).click()
        page.locator(f'[data-publish-delivery="{delivery["id"]}"]').click()
        modal = page.locator('.production-dialog[open]')
        self.assertEqual({name: modal.locator(f'[name="{name}"]').input_value() for name in fields}, fields)
        self.assertTrue(modal.locator('[name="post_id"]').is_disabled())
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        page.locator('.publication-record').get_by_text("7664986913497009445", exact=True).wait_for()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(len(self.detail()["publication_history"]), 1)

    def test_unified_queue_keeps_video_review_separate_from_carousel_collection(self):
        page = self.open_images(width=390)
        page.route("**/api/production/eleven", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": True})))
        self.confirm_group(page)
        self.prepare(page)
        video_row = {"kind": "video", "id": "PRD-0123456789ab", "product": "202H", "title": "待人工审查的剪辑",
                     "direction_id": "202H-S1", "timeline_name": "Lab261011-202-1", "updated_at": "2026-10-10",
                     "status": "awaiting_review", "edit_id": "EDT-0123456789ab"}

        catalog_data = self.request("GET", "/api/production?product=202H")[2]
        catalog_data["jobs"].append({**video_row, "lines": [], "takes": [], "selected_take": "TAK-0123456789ab",
            "timelines": [{"unique_id": "video-fixture", "take_id": "TAK-0123456789ab"}],
            "edits": [{"id": video_row["edit_id"], "timeline_uid": "video-fixture", "status": "awaiting_review",
                       "validation": {"status": "awaiting_review"}}]})
        delivery_data = self.request("GET", "/api/production/deliveries?product=202H")[2]
        delivery_data["items"].append(video_row)
        page.route("**/api/production?*", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps(catalog_data, ensure_ascii=False)))
        page.route("**/api/production/deliveries?*", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps(delivery_data, ensure_ascii=False)))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production")
        video = page.locator('[data-delivery-kind="video"]')
        video.get_by_text("去审查", exact=True).wait_for()
        video.get_by_text("待人工审查", exact=True).wait_for()
        self.assertEqual(video.locator('[download]').count(), 0)
        self.assertIn("PRD-0123456789ab", video.get_attribute("href"))
        carousel = page.locator(f'[data-production-id="{self.carousel["id"]}"]')
        self.assertEqual(carousel.count(), 1)
        carousel.get_by_text("可领取", exact=True).wait_for()
        carousel.get_by_text("领取成品", exact=True).wait_for()
        self.assertEqual(page.locator("#production-queue .production-card").count(), 2)
        page.locator("#production-status").select_option("ready")
        self.assertEqual(page.locator("#production-queue .production-card").count(), 1)
        self.assertEqual(page.locator('[data-production-kind="video"]').count(), 0)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
