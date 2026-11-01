"""A single production queue with honest review/delivery states and isolated services."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect
from content_management import build_content_items

import test_ui_regressions as browser
from test_content_flow_ui import content_filter, set_content_condition


def content_response(app, jobs, address, management=None):
    """Use the real content projection and filters with isolated source catalogs."""
    management = management or {"revision": "fixture-1", "items": {}}
    items = build_content_items([], [], jobs, management=management)
    with patch.object(app, "_content_snapshot", return_value=(items, management)):
        return app.content_catalog(parse_qs(urlsplit(address).query))


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
        self.data = {"jobs": [], "voices": [], "defaults": {}, "readings": []}
        self.deliveries = []
        self.delivery_calls = []
        self.management = {"revision": "fixture-1", "items": {}}

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

    def catalog(self, address):
        return content_response(self.server.app, self.data["jobs"], address, self.management)

    def open_queue(self, *, width=1080, hold_delivery=None):
        # Keep the helper name used by refresh regressions; its workspace is now the unified list.
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

        def content(route):
            self.delivery_calls.append(route.request.url)
            if hold_delivery is not None:
                hold_delivery.append(route)
            else:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(self.catalog(route.request.url)))

        page.route("**/api/production?*", lambda route: route.fulfill(status=200, content_type="application/json",
            body=json.dumps(self.data, ensure_ascii=False)))
        page.route("**/api/content?*", content)
        page.route("**/api/production/eleven", eleven)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/202H/content?view=all")
        page.locator("#content-flow-list").wait_for()
        if hold_delivery is None:
            page.locator("#content-flow-status").wait_for(state="hidden")
        return page

    def row(self, page, item):
        kind = "video"
        return page.locator(f'[data-content-id="{kind}:{item["id"]}"]')

    def all_view(self, page):
        page.locator('[data-flow-view="all"]').click()
        page.locator('#content-flow-status').wait_for(state='hidden')

    def start_batch(self, page):
        page.locator('.flow-main-menu > summary').click()
        page.locator('#content-flow-batch-open').click()
        page.locator('#content-flow-batch').wait_for()

    def test_service_details_and_rechecks_stay_in_the_content_settings_drawer(self):
        self.job(1)
        page = self.open_queue()
        self.assertEqual(self.eleven_calls, [])
        page.locator('.flow-main-menu > summary').click()
        page.locator('#content-production-settings').click()
        service = page.locator('#production-settings #production-eleven')
        service.get_by_text('本期已用 120 / 1,000 字符', exact=False).wait_for()
        self.eleven = {'ok':False, 'error':'测试连接暂不可用'}
        service.locator('[data-recheck]').click()
        service.get_by_text('测试连接暂不可用', exact=True).wait_for()
        page.locator('#production-settings [data-drawer-close]').click()
        expect(page.locator('.flow-row')).to_have_count(1)
        page.locator('.flow-main-menu > summary').click()
        page.locator('#content-production-settings').click()
        self.eleven = {'ok':True}
        service.locator('[data-recheck]').click()
        service.get_by_text('ElevenLabs 已连接', exact=True).wait_for()
        page.locator('#production-settings [data-drawer-close]').click()
        self.assertEqual(len(self.eleven_calls), 3)
        expect(page.locator('.flow-row')).to_have_count(1)

if __name__ == '__main__':
    unittest.main()
