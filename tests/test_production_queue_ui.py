"""A single production queue with honest review/delivery states and isolated services."""
from __future__ import annotations

import json
import unittest

import test_ui_regressions as browser


class ProductionQueueBrowserTests(unittest.TestCase):
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = browser.WorkbenchBrowserTests.stop_server
    request = browser.WorkbenchBrowserTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        browser.WorkbenchBrowserTests.setUp(self)
        self.eleven = {"ok": True, "tier": "fixture", "character_count": 120, "character_limit": 1000}
        self.eleven_calls = []
        self.data = {"jobs": [], "carousels": [], "voices": [], "defaults": {}, "readings": []}
        self.deliveries = []

    def job(self, number, state="draft", archived=False):
        item = {"id": f"PRD-{number:012x}", "title": f"视频 {number}", "product": "202H", "direction_id": "202H-S1",
                "updated_at": "2026-10-11", "status": state, "lines": [], "takes": [], "edits": [], "timelines": [],
                "archived": archived}
        if state in {"approved", "awaiting_review"}:
            item["selected_take"] = "TAK-0123456789ab"
            item["timelines"] = [{"unique_id": f"timeline-{number}", "take_id": item["selected_take"]}]
            edit = {"id": f"EDT-{number:012x}", "timeline_uid": f"timeline-{number}", "status": "awaiting_review",
                    "validation": {"status": "awaiting_review"}}
            item["edits"] = [edit]
            self.deliveries.append({"kind": "video", "id": item["id"], "status": "awaiting_review", "edit_id": edit["id"],
                                    "approved_at": "2026-10-11" if state == "approved" else ""})
        self.data["jobs"].append(item)
        return item

    def carousel(self, number, *, current=True, available=True, published=False, archived=False):
        item = {"id": f"CAR-{number:012x}", "title": f"图文 {number}", "product": "202H", "direction_id": "202H-S2",
                "updated_at": "2026-10-11", "status": "approved" if current else "reviewing", "slides": [],
                "archived": archived}
        delivery_id = f"DLV-{number:012x}"
        self.deliveries.append({"kind": "carousel", "id": item["id"], "ready_to_package": current,
            "delivery_available": available, "current_delivery_id": delivery_id if current else "",
            "delivery_id": delivery_id, "published": published,
            "delivery_error": "成品包文件已变化，请重新整理。" if current and not available else ""})
        self.data["carousels"].append(item)
        return item

    def open_queue(self, *, width=1080, hold_delivery=None):
        context = self.browser.new_context(viewport={"width": width, "height": 760})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def eleven(route):
            self.eleven_calls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(self.eleven))

        def deliveries(route):
            if hold_delivery is not None:
                hold_delivery.append(route)
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps({"items": self.deliveries}))

        page.route("**/api/production?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps(self.data, ensure_ascii=False)))
        page.route("**/api/production/deliveries?*", deliveries)
        page.route("**/api/production/eleven", eleven)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/production")
        page.locator("#production-queue").wait_for()
        if hold_delivery is None:
            page.locator("#production-queue-status").wait_for(state="hidden")
        return page

    def row(self, page, item):
        return page.locator(f'[data-production-id="{item["id"]}"]')

    def test_each_order_appears_once_and_type_status_filters_distinguish_review_from_collection(self):
        draft, review, approved, failed = (self.job(1), self.job(2, "awaiting_review"), self.job(3, "approved"), self.job(4, "failed"))
        ready, published = self.carousel(1), self.carousel(2, published=True)
        damaged, historical = self.carousel(3, available=False), self.carousel(4, current=False, published=True)
        archived = self.job(5, archived=True)
        page = self.open_queue(width=390)
        self.assertEqual(page.locator(".production-card").count(), 8)
        self.assertEqual(page.locator("#production-delivery-overview").count(), 0)
        for item in (draft, review, approved, failed, ready, published, damaged, historical):
            self.assertEqual(self.row(page, item).count(), 1)
        self.row(page, review).get_by_text("待人工审查", exact=True).wait_for()
        self.row(page, review).get_by_text("去审查", exact=True).wait_for()
        self.row(page, approved).get_by_text("已人工审查", exact=True).wait_for()
        self.row(page, ready).get_by_text("可领取", exact=True).wait_for()
        self.row(page, ready).get_by_text("领取成品", exact=True).wait_for()
        self.assertEqual(self.row(page, published).get_attribute("data-production-state"), "published")
        for item in (review, approved, damaged, historical):
            self.assertNotEqual(self.row(page, item).get_attribute("data-production-state"), "ready")
        self.assertEqual(self.row(page, historical).locator("[download]").count(), 0)
        page.locator("#production-status").select_option("ready")
        self.assertEqual(page.locator(".production-card").count(), 1)
        self.assertEqual(self.row(page, ready).count(), 1)
        page.locator("#production-type").select_option("video")
        self.assertEqual(page.locator(".production-card").count(), 0)
        page.locator("#production-status").select_option("awaiting_review")
        self.assertEqual(page.locator(".production-card").count(), 1)
        self.assertEqual(self.row(page, review).count(), 1)
        page.locator("#production-status").select_option("approved")
        self.assertEqual(self.row(page, approved).count(), 1)
        page.locator("#production-status").select_option("all")
        page.locator("#production-archived").check()
        self.assertEqual(self.row(page, archived).count(), 1)
        self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)

    def test_slow_delivery_check_preserves_filter_and_never_claims_a_package_before_verification(self):
        ready, held = self.carousel(1), []
        page = self.open_queue(hold_delivery=held)
        self.assertNotEqual(self.row(page, ready).get_attribute("data-production-state"), "ready")
        page.locator("#production-type").select_option("carousel")
        page.locator("#production-status").select_option("ready")
        self.assertEqual(page.locator(".production-card").count(), 0)
        self.assertEqual(len(held), 1)
        held.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"items": self.deliveries}))
        self.row(page, ready).get_by_text("领取成品", exact=True).wait_for()
        self.assertEqual(page.locator("#production-type").input_value(), "carousel")
        self.assertEqual(page.locator("#production-status").input_value(), "ready")
        self.assertEqual(page.locator(".production-card").count(), 1)

    def test_healthy_service_details_live_only_in_settings_and_errors_clear_after_recheck(self):
        self.job(1)
        page = self.open_queue()
        page.locator("#production-eleven").get_by_text("ElevenLabs 已连接", exact=False).wait_for(state="attached")
        self.assertFalse(page.locator("#production-eleven").is_visible())
        self.assertFalse(page.locator("#production-service-warning").is_visible())
        page.locator("#production-settings-open").click()
        service = page.locator("#production-settings #production-eleven")
        service.get_by_text("本期已用 120 / 1,000 字符", exact=False).wait_for()
        self.eleven = {"ok": False, "error": "测试连接暂不可用"}
        service.locator("[data-recheck]").click()
        service.get_by_text("测试连接暂不可用", exact=True).wait_for()
        page.locator("#production-settings [data-drawer-close]").click()
        page.locator("#production-service-warning").get_by_text("测试连接暂不可用", exact=False).wait_for()
        page.locator("#production-settings-open").click()
        self.eleven = {"ok": True}
        service.locator("[data-recheck]").click()
        service.get_by_text("ElevenLabs 已连接", exact=True).wait_for()
        page.locator("#production-settings [data-drawer-close]").click()
        self.assertFalse(page.locator("#production-service-warning").is_visible())
        self.assertEqual(len(self.eleven_calls), 3)

    def test_failed_delivery_read_keeps_orders_accessible_and_retry_refreshes_states(self):
        ready, held = self.carousel(1), []
        page = self.open_queue(hold_delivery=held)
        held.pop().fulfill(status=503, content_type="application/json", body=json.dumps({"error": "隔离测试读取失败"}))
        page.locator("#production-queue-status [data-delivery-reload]").wait_for()
        self.assertTrue(self.row(page, ready).is_visible())
        self.assertNotEqual(self.row(page, ready).get_attribute("data-production-state"), "ready")
        page.locator("#production-queue-status [data-delivery-reload]").click()
        page.locator("#production-queue-status").get_by_text("正在核对审查与领取状态…", exact=True).wait_for()
        self.assertEqual(len(held), 1)
        held.pop().fulfill(status=200, content_type="application/json", body=json.dumps({"items": self.deliveries}))
        self.row(page, ready).get_by_text("可领取", exact=True).wait_for()
        self.assertEqual(page.locator(".production-card").count(), 1)


if __name__ == "__main__":
    unittest.main()
