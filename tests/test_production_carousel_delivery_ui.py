"""Carousel pickup and publication through isolated HTTP and a fake image engine."""
from __future__ import annotations

import copy
import io
import json
import unittest
from uuid import uuid4
import zipfile

import test_production_carousel_images_ui as image_fixture
from test_production_queue_ui import content_response


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
        page.locator('.page-heading .script-status').get_by_text("可领取", exact=True).wait_for()

    def prepare(self, page):
        # Confirming the whole group already packages it; pressing the button again reuses that package.
        page.locator('[data-step="delivery"]').click()
        page.locator("#carousel-prepare-delivery").click()
        page.locator('#toast:has-text("成品包已本地导出")').wait_for()
        return self.detail()["deliveries"][-1]

    def publication_dialog(self, page, delivery_id, *, post_id="7664986913497009442"):
        # Registration sits inline under the publish copy, like the video order's; extra fields fold under 都可不填.
        page.locator('[data-step="delivery"]').click()
        modal = page.locator('#carousel-publication-editor')
        if modal.get_attribute('open') is None:
            modal.locator(':scope > summary').click()
        more = modal.locator('.video-publication-more')
        if more.get_attribute('open') is None:
            more.locator(':scope > summary').click()
        modal.locator('[name="delivery_id"]').select_option(delivery_id)
        previous_post = modal.locator('[name="post_id"]').input_value()
        previous_date = modal.locator('[name="published_on"]').input_value()
        modal.locator('[name="post_id"]').fill(post_id)
        # The fixture IDs were posted 2026-07-21 (UTC−8); the ID fills that in before the test sets its own date.
        # A manually chosen date on an existing registration remains intentional during correction.
        self.assertEqual(modal.locator('[name="published_on"]').input_value(),
                         previous_date if previous_post and previous_date != "2026-07-21" else "2026-07-21")
        modal.locator('[name="published_on"]').evaluate("node => { node.value = '2026-10-10'; }")
        modal.locator('[name="post_url"]').fill(f"https://www.tiktok.com/@fixture/photo/{post_id}")
        modal.locator('[name="account"]').fill("fixture")
        modal.locator('[name="note"]').fill("实际使用的图片包已逐张核对")
        return modal

    def test_registered_post_ids_copy_from_the_current_registration_and_the_history(self):
        page = self.open_images()
        page.route('**/api/direction-progress/202H-S2?product=202H', lambda route: route.fulfill(
            json={'product': '202H', 'direction_id': '202H-S2', 'variants': []}))
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
        page.locator('[data-step="delivery"]').click()
        self.confirm_group(page)
        original = self.prepare(page)
        page.reload()
        modal = self.publication_dialog(page, original["id"])
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        record = page.locator('#carousel-publication .publication-record')
        record.get_by_text("已登记发布", exact=True).wait_for()
        record.locator('[data-copy-id]').click()
        page.get_by_text("图文 ID已复制").wait_for()
        self.assertEqual(page.evaluate("navigator.clipboard.readText()"), "7664986913497009442")
        # Binding a second post keeps the first in the history, which has its own copy button.
        modal = self.publication_dialog(page, original["id"], post_id="7664986913497009450")
        modal.get_by_role("button", name="保存修正并保留原登记", exact=True).click()
        record.locator('[data-copy-id][data-copy-id="7664986913497009450"]').wait_for()
        history = page.locator('#carousel-publication-history')
        history.locator(':scope > summary').click()
        old = history.locator('[data-publication-history] [data-copy-id="7664986913497009442"]')
        old.click()
        page.wait_for_function("() => navigator.clipboard.readText().then(text => text === '7664986913497009442')")
        self.assertEqual(history.locator('[data-publication-history] dd span', has_text="7664986913497009442").count(), 1)

    def test_confirm_pickup_replace_old_pack_and_preserve_the_original_publication_source(self):
        before = self.source.read_bytes()
        page = self.open_images()
        page.route('**/api/direction-progress/202H-S2?product=202H', lambda route: route.fulfill(
            json={'product': '202H', 'direction_id': '202H-S2', 'variants': []}))
        console_errors = []
        page.on('console', lambda message: console_errors.append(message.text) if message.type == 'error' else None)
        self.assertTrue(page.locator("#carousel-prepare-delivery").is_disabled())
        page.locator('[data-step="delivery"]').click()
        page.locator('#carousel-delivery-panel').get_by_text("还有 3 张未工作台确认", exact=False).wait_for()
        self.confirm_group(page)
        original = self.prepare(page)
        self.assertEqual(page.locator('#carousel-deliveries .delivery-package').count(), 1)
        with page.expect_download() as downloaded:
            page.locator(f'[data-download-delivery="{original["id"]}"]').click()
        self.assertEqual(downloaded.value.suggested_filename, original["filename"])
        status, headers, first_zip = self.request("GET", f"/api/production/carousels/{self.carousel['id']}/deliveries/{original['id']}/download")
        self.assertEqual(status, 200)
        self.assertIn("zip", headers["Content-Type"])
        with zipfile.ZipFile(io.BytesIO(first_zip)) as archive:
            for slide in original["slides"]:
                self.assertIn(slide["file"], archive.namelist())
        page.evaluate("window.__deliveryCopied = ''; window.copyText = text => { window.__deliveryCopied = text; return Promise.resolve(); }")
        page.locator('#carousel-delivery-panel [data-copy-publish="text"]').click()
        expected = "\n\n".join(text for text in (original["caption_zh"], original["caption_en"]) if text)
        self.assertEqual(page.evaluate("window.__deliveryCopied"), expected)
        page.reload()
        modal = self.publication_dialog(page, original["id"])
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        page.locator('#carousel-publication .publication-record').get_by_text("已登记发布", exact=True).wait_for()
        frozen_publication = copy.deepcopy(self.detail()["publication"])
        self.assertEqual(frozen_publication["delivery_id"], original["id"])

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
        self.assertEqual(page.locator('#carousel-deliveries .delivery-package').count(), 1)
        self.assertEqual(page.locator(f'[data-delivery-id="{original["id"]}"]').count(), 0)
        self.assertEqual([entry["id"] for entry in self.detail()["deliveries"]], [latest["id"]],
                         "The backend catalog must contain just the latest package; hiding older cards is insufficient.")
        self.assertFalse((self.app._carousel_dir(self.carousel["id"]) / original["zip_file"]).exists())
        status, _, _ = self.request("GET", f"/api/production/carousels/{self.carousel['id']}/deliveries/{original['id']}/download")
        self.assertEqual(status, 404)
        with page.expect_download() as downloaded:
            page.locator(f'[data-download-delivery="{latest["id"]}"]').click()
        self.assertEqual(downloaded.value.suggested_filename, latest["filename"])
        self.assertEqual(self.detail()["publication"], frozen_publication,
                         "Replacing a local ZIP must not rebind an existing publication.")

        page.reload()
        page.locator('[data-step="delivery"]').click()
        page.locator('.publication-record').get_by_text("原发布来源（文件已清理）", exact=False).wait_for()
        editor = page.locator('#carousel-publication-editor')
        if editor.get_attribute('open') is None:
            editor.locator(':scope > summary').click()
        self.assertNotEqual(editor.locator('[name="delivery_id"]').input_value(), latest["id"],
                            "Opening an old publication must not silently select the new package.")
        more = editor.locator('.video-publication-more')
        if more.get_attribute('open') is None:
            more.locator(':scope > summary').click()
        editor.locator('[name="note"]').fill("修正旧登记，来源保持原包")
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/publication')) as corrected:
            editor.get_by_role("button", name="保存修正并保留原登记", exact=True).click()
        self.assertEqual(corrected.value.status, 200, corrected.value.json())
        page.locator('.publication-record').get_by_text("修正旧登记，来源保持原包", exact=True).wait_for()
        self.assertEqual(self.detail()["publication"]["delivery_id"], original["id"])
        self.assertEqual(self.detail()["publication"]["delivery_snapshot"], frozen_publication["delivery_snapshot"])
        modal = self.publication_dialog(page, latest["id"], post_id="7664986913497009444")
        modal.locator('[name="note"]').fill("实际发布使用最新成品包")
        modal.get_by_role("button", name="保存修正并保留原登记", exact=True).click()
        page.locator('.publication-record').get_by_text("实际发布使用最新成品包", exact=True).wait_for()
        publication = self.detail()["publication"]
        self.assertEqual(publication["delivery_id"], latest["id"])
        self.assertEqual(publication["post_id"], "7664986913497009444")
        self.assertEqual(self.app.creation.get_script(self.carousel["script_id"])["status"], "published")
        page.locator(f'[data-delivery-id="{latest["id"]}"]').get_by_text("用于当前发布登记", exact=True).wait_for()
        self.assertEqual(page.locator('#carousel-publication-performance .facts-grid').count(), 0)
        history = page.locator('#carousel-publication-history')
        self.assertEqual(history.locator('[data-publication-history]').count(), 3)
        self.assertIsNone(history.get_attribute('open'))
        history.locator(':scope > summary').click()
        entries = history.locator('[data-publication-history]')
        entries.nth(0).get_by_text("7664986913497009444", exact=True).wait_for()
        entries.nth(1).get_by_text("修正旧登记，来源保持原包", exact=True).wait_for()
        entries.nth(2).get_by_text("7664986913497009442", exact=True).wait_for()
        entries.nth(2).get_by_text("实际使用的图片包已逐张核对", exact=True).wait_for()
        entries.nth(2).locator(':scope > dl.publication-facts').get_by_text(original["id"], exact=False).wait_for()
        self.assertEqual(self.detail()["publication_history"][0], frozen_publication)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/content?view=all")
        page.locator(f'[data-content-id="carousel:{self.carousel["id"]}"]').wait_for()
        self.assertEqual(page.locator('#production-queue').count(), 0)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}?step=delivery")
        page.locator('.publication-record a[href="https://www.tiktok.com/@fixture/photo/7664986913497009444"]').wait_for()
        self.assertEqual(page.locator('#carousel-deliveries .delivery-package').count(), 1)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 1081)
        self.assertEqual(console_errors, [])

    def test_publication_conflict_keeps_mobile_inputs_and_retries_with_fresh_revision(self):
        page = self.open_images(width=390)
        self.confirm_group(page)
        delivery = self.prepare(page)
        modal = self.publication_dialog(page, delivery["id"], post_id="7664986913497009443")
        modal.locator('[name="post_id"]').fill("123")
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        modal.locator('[data-publication-error]').get_by_text("6–25 位数字", exact=False).wait_for()
        modal.locator('[name="post_id"]').fill("7664986913497009443")
        fields = {name: modal.locator(f'[name="{name}"]').input_value() for name in
                  ("delivery_id", "post_id", "published_on", "post_url", "account", "note")}
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/import",
                                         {"request_id": uuid4().hex, "slide": 1, "path": "202H-S2/原图/03.jpg"})
        self.assertEqual(status, 200, result)
        with page.expect_response(lambda response: response.url.endswith('/publication')) as failed:
            modal.get_by_role("button", name="保存发布登记", exact=True).click()
        self.assertEqual(failed.value.status, 409)
        modal.locator('[data-publication-error]').get_by_text("本次输入已保留", exact=False).wait_for()
        self.assertEqual({name: modal.locator(f'[name="{name}"]').input_value() for name in fields}, fields)
        self.assertFalse(modal.locator('[name="post_id"]').is_disabled())
        self.assertLessEqual(modal.evaluate("el => el.scrollWidth - el.clientWidth"), 1)
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/publication')) as saved:
            modal.get_by_role("button", name="保存发布登记", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.json())
        page.locator('.publication-record a[href="https://www.tiktok.com/@fixture/photo/7664986913497009443"]').wait_for()
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
        self.assertEqual(page.locator('#carousel-publication-performance .facts-grid').count(), 0)

    def test_successful_package_shows_real_cleanup_warnings_and_clears_them_after_a_clean_retry(self):
        page = self.open_images()
        self.confirm_group(page)
        calls = []
        warning = "隔离测试：有一份旧领取副本仍被占用"

        def prepare_with_warning(route):
            status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/delivery",
                                             route.request.post_data_json)
            self.assertEqual(status, 200, result)
            calls.append(result)
            if len(calls) == 1:
                result["cleanup_warnings"] = [warning]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(result, ensure_ascii=False))

        page.route(f"**/api/production/carousels/{self.carousel['id']}/delivery", prepare_with_warning)
        package = self.prepare(page)
        note = page.locator('[data-delivery-cleanup-warning]')
        note.get_by_text(warning, exact=False).wait_for()
        self.assertIn("清理未完成", note.inner_text())
        self.assertEqual(page.locator(f'[data-download-delivery="{package["id"]}"]').count(), 1)
        self.prepare(page)
        note.wait_for(state="detached")
        self.assertEqual(len(calls), 2)

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
        modal.locator('[data-publication-error]').get_by_text("请直接重试同一登记", exact=False).wait_for()
        self.assertEqual(len(self.detail()["publication_history"]), 1)
        for name in fields:
            self.assertTrue(modal.locator(f'[name="{name}"]').is_disabled())
        self.assertEqual({name: modal.locator(f'[name="{name}"]').input_value() for name in fields}, fields)
        self.assertFalse(modal.locator('[type="submit"]').is_disabled())
        with page.expect_response(lambda response: response.request.method == 'POST' and response.url.endswith('/publication')) as retried:
            modal.get_by_role("button", name="重试同一登记", exact=True).click()
        self.assertEqual(retried.value.status, 200, retried.value.json())
        page.locator('.publication-record a[href="https://www.tiktok.com/@fixture/photo/7664986913497009445"]').wait_for()
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        self.assertEqual(len(self.detail()["publication_history"]), 1)

    def test_collected_current_package_waits_for_registration_in_header_and_delivery_step(self):
        page = self.open_images()
        self.confirm_group(page)
        delivery = self.prepare(page)
        heading = page.locator('.page-heading .script-status')
        step = page.locator('.production-steps [data-step="delivery"] small')
        # A package nobody has downloaded still reads as ready to collect.
        step.get_by_text("最新成品包可领取", exact=True).wait_for()
        self.assertEqual(heading.inner_text(), "可领取")
        self.assertEqual(page.locator('[data-next-state]').text_content(), "可领取")
        with page.expect_download():
            page.locator(f'[data-download-delivery="{delivery["id"]}"]').click()
        page.locator(f'[data-delivery-state="{delivery["id"]}"]').get_by_text("已领取", exact=False).wait_for()
        self.assertTrue(self.detail()["deliveries"][-1]["collected_at"])
        self.assertFalse(self.detail().get("publication"))

        # The header follows the pickup on the same page, and after a reload.
        heading.get_by_text("已领取 · 待登记发布", exact=True).wait_for()
        step.get_by_text("已领取 · 待登记", exact=True).wait_for()
        # Header, the hidden 下一步 strip and the step bar all say the pack is out but no post is registered yet.
        page.reload()
        heading.get_by_text("已领取 · 待登记发布", exact=True).wait_for()
        self.assertEqual(page.locator('[data-next-state]').text_content(), "已领取 · 待登记发布")
        self.assertEqual(page.locator('[data-next-action]').text_content(), "下一步：登记发布 →")
        self.assertEqual(page.locator('[data-next-note]').text_content(), "成品包已领取，发帖后登记帖子 ID")
        self.assertEqual(step.inner_text(), "已领取 · 待登记")

        # Registering the post supersedes the pickup state.
        modal = self.publication_dialog(page, delivery["id"], post_id="7664986913497009446")
        modal.get_by_role("button", name="保存发布登记", exact=True).click()
        heading.get_by_text("已登记发布", exact=True).wait_for()
        step.get_by_text("已登记发布", exact=True).wait_for()
        self.assertEqual(page.locator('[data-next-state]').text_content(), "已登记发布")
        # The receipt points at a package still listed above, so its sources render once, on that package card.
        expect_count = page.locator('#carousel-publication .delivery-provenance').count()
        self.assertEqual((expect_count, page.locator(f'[data-delivery-id="{delivery["id"]}"] .delivery-provenance').count()), (0, 1))
        # Until the sales table knows the post, its empty state names a carousel, on one line.
        performance = page.locator("#carousel-publication-performance")
        performance.get_by_text("销售数据表未匹配到此图文", exact=True).wait_for()
        self.assertEqual(performance.locator(".context-note").count(), 0)

    def test_published_order_step_reads_checking_until_delivery_status_returns(self):
        page = self.open_images()
        self.confirm_group(page)
        delivery = self.prepare(page)
        status, _, result = self.request("POST", f"/api/production/carousels/{self.carousel['id']}/publication", {
            "request_id": uuid4().hex, "revision": self.app.production.public()["revision"],
            "delivery_id": delivery["id"], "post_id": "7664986913497009447", "published_on": "2026-10-10",
            "account": "fixture"})
        self.assertEqual(status, 200, result)
        held, released = [], []

        def delivery_read(route):
            if released:
                route.continue_()
            else:
                held.append(route)

        page.route("**/api/production/deliveries?*", delivery_read)
        page.reload()
        step = page.locator('.production-steps [data-step="delivery"] small')
        # The stored publication alone cannot tell current from old version; wait for the status read.
        step.get_by_text("核对中", exact=True).wait_for()
        page.evaluate('() => new Promise(resolve => setTimeout(resolve,100))')
        self.assertEqual(step.inner_text(), "核对中")
        self.assertEqual(page.locator('.page-heading .script-status').inner_text(), "领取状态核对中")
        self.assertTrue(held)
        released.append(True)
        for route in held:
            route.continue_()
        step.get_by_text("已登记发布", exact=True).wait_for()
        page.locator('.page-heading .script-status').get_by_text("已登记发布", exact=True).wait_for()

    def test_content_list_includes_video_and_carousel_while_its_package_stays_in_details(self):
        page = self.open_images(width=390)
        page.route("**/api/production/eleven", lambda route: route.fulfill(status=200,
            content_type="application/json", body=json.dumps({"ok": True})))
        self.confirm_group(page)
        package = self.prepare(page)
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
        page.route('**/api/content?*', lambda route: route.fulfill(status=200, content_type='application/json',
            body=json.dumps(content_response(self.app, catalog_data['jobs'], catalog_data['carousels'], route.request.url))))
        page.goto(f'http://127.0.0.1:{self.server.server_port}/#/202H/content?view=all')
        video = page.locator('[data-content-id="video:PRD-0123456789ab"]')
        video.get_by_text('待人工审查', exact=True).wait_for()
        self.assertEqual(video.locator('[download]').count(), 0)
        self.assertIn('PRD-0123456789ab', video.locator('.flow-title').get_attribute('href'))
        carousel = page.locator(f'[data-content-id="carousel:{self.carousel["id"]}"]')
        carousel.wait_for()
        self.assertEqual(page.locator('.flow-row').count(), 2)
        self.assertEqual(page.locator('#production-queue, #production-type').count(), 0)
        page.locator('#content-flow-filter-toggle').click()
        page.locator('[data-filter-add]').click()
        page.locator('[data-filter-field]').select_option('status')
        self.assertEqual(page.locator('[data-filter-value="ready"], [data-filter-value="ready_to_package"]').count(), 0)
        page.locator('[data-filter-cancel]').click()
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#production/{self.carousel['id']}?step=delivery")
        page.locator(f'[data-download-delivery="{package["id"]}"]').wait_for()
        self.assertEqual(self.detail()["deliveries"][-1]["id"], package["id"])
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)


if __name__ == "__main__":
    unittest.main()
