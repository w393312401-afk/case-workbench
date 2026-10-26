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


def content_response(app, jobs, carousels, address, management=None):
    """Use the real content projection and filters with isolated source catalogs."""
    management = management or {"revision": "fixture-1", "items": {}}
    items = build_content_items([], [], jobs, carousels, management=management)
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
        self.data = {"jobs": [], "carousels": [], "voices": [], "defaults": {}, "readings": []}
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

    def carousel(self, number, *, current=True, available=True, published=False, archived=False):
        item = {"id": f"CAR-{number:012x}", "title": f"图文 {number}", "product": "202H", "direction_id": "202H-S2",
                "updated_at": "2026-10-11", "status": "approved" if current else "reviewing", "slides": [],
                "archived": archived}
        if published:
            item["publication"] = {"post_id": "7999999999999999911", "published_on": "2026-10-11"}
        delivery_id = f"DLV-{number:012x}"
        self.deliveries.append({"kind": "carousel", "id": item["id"], "ready_to_package": current,
            "delivery_available": available, "current_delivery_id": delivery_id if current else "",
            "delivery_id": delivery_id, "published": published,
            "delivery_error": "成品包文件已变化，请重新整理。" if current and not available else ""})
        self.data["carousels"].append(item)
        return item

    def catalog(self, address):
        return content_response(self.server.app, self.data["jobs"], self.data["carousels"], address, self.management)

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
        kind = "carousel" if item["id"].startswith("CAR-") else "video"
        return page.locator(f'[data-content-id="{kind}:{item["id"]}"]')

    def all_view(self, page):
        page.locator('[data-flow-view="all"]').click()
        page.locator('#content-flow-status').wait_for(state='hidden')

    def start_batch(self, page):
        page.locator('.flow-main-menu > summary').click()
        page.locator('#content-flow-batch-open').click()
        page.locator('#content-flow-batch').wait_for()

    def test_video_and_carousel_share_the_list_with_status_kind_and_archive_filters(self):
        draft, review, approved, failed = (self.job(1), self.job(2, "awaiting_review"), self.job(3, "approved"), self.job(4, "failed"))
        ready, published = self.carousel(1), self.carousel(2, published=True)
        archived_video, archived_carousel = self.job(5, archived=True), self.carousel(3, archived=True)
        page = self.open_queue(width=390)
        expect(page.locator('.flow-row')).to_have_count(8)
        for item in (draft, review, approved, failed, ready, published, archived_video, archived_carousel):
            expect(self.row(page, item)).to_have_count(1)
        self.assertEqual(page.locator('#production-queue, .scripts-tabs').count(), 0)
        self.row(page, review).get_by_text('待人工审查', exact=True).wait_for()
        self.row(page, approved).get_by_text('待发布', exact=True).wait_for()
        self.row(page, published).get_by_text('已发布', exact=True).wait_for()
        set_content_condition(page, 'status', ['awaiting_review'])
        expect(page.locator('.flow-row')).to_have_count(1)
        expect(self.row(page, review)).to_have_count(1)
        set_content_condition(page, 'status', ['approved'])
        expect(self.row(page, approved)).to_have_count(1)
        set_content_condition(page, 'status', ['archived'])
        expect(page.locator('.flow-row')).to_have_count(2)
        set_content_condition(page, 'kind', ['carousel'])
        expect(page.locator('.flow-row')).to_have_count(1)
        expect(self.row(page, archived_carousel)).to_have_count(1)
        page.locator('#content-flow-filter-toggle').click()
        status_rule = page.locator('[data-filter-row]').filter(has=page.locator('[data-filter-field] option[value="status"]:checked'))
        status_rule.locator('[data-filter-remove]').click()
        with page.expect_response(lambda response: '/api/content?' in response.url):
            page.locator('[data-filter-apply]').click()
        expect(page.locator('.flow-row')).to_have_count(3)
        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 391)

    def test_slow_refresh_preserves_filters_and_ignores_an_older_catalog(self):
        review = self.job(1, 'awaiting_review')
        self.carousel(1)
        page = self.open_queue()
        held = []
        page.route('**/api/content?*', lambda route: held.append(route))
        page.locator('#content-flow-refresh').click()
        page.wait_for_timeout(50)
        # Both responses stay held so the stale refresh can arrive after the edited filter.
        page.locator('#content-flow-filter-toggle').click()
        page.locator('[data-filter-add]').click()
        page.locator('[data-filter-value="video"]').check()
        page.locator('[data-filter-apply]').click()
        page.wait_for_timeout(50)
        self.assertEqual(len(held), 2)
        latest = held.pop()
        latest.fulfill(status=200, content_type='application/json', body=json.dumps(self.catalog(latest.request.url)))
        expect(page.locator('.flow-row')).to_have_count(1)
        stale = held.pop()
        stale.fulfill(status=200, content_type='application/json', body=json.dumps(self.catalog(stale.request.url)))
        page.wait_for_timeout(100)
        self.assertEqual(content_filter(page), {'match': 'all', 'conditions': [
            {'field': 'kind', 'values': ['video']}]})
        expect(self.row(page, review)).to_have_count(1)
        expect(page.locator('.flow-row')).to_have_count(1)

    def test_returning_to_the_list_shows_the_last_rows_before_the_refresh_answers(self):
        review = self.job(1, 'awaiting_review')
        carousel = self.carousel(1)
        page = self.open_queue()
        expect(page.locator('.flow-row')).to_have_count(2)
        page.evaluate("location.hash = '#/202H/directions'")
        page.locator('#content-flow-list').wait_for(state='detached')
        held = []
        page.route('**/api/content?*', lambda route: held.append(route))
        page.evaluate("location.hash = '#/202H/content?view=all'")
        # The read is still pending, yet the rows read last time are already on screen.
        expect(page.locator('.flow-row')).to_have_count(2)
        expect(self.row(page, review)).to_have_count(1)
        expect(self.row(page, carousel)).to_have_count(1)
        self.assertEqual(len(held), 1)
        self.assertNotIn('reuse=1', held[0].request.url, 'Opening the list always asks for a fresh read')
        self.job(2, 'awaiting_review')
        held[0].fulfill(status=200, content_type='application/json', body=json.dumps(self.catalog(held[0].request.url)))
        expect(page.locator('.flow-row')).to_have_count(3)

    def test_legacy_video_queue_preference_does_not_hide_carousels(self):
        video, carousel = self.job(1), self.carousel(2)
        self.carousel(3, archived=True)
        page = self.open_queue()
        expect(page.locator('.flow-row')).to_have_count(3)
        page.evaluate("localStorage.setItem('case-workbench:production-type:202H', 'video')")
        page.reload()
        expect(page.locator('.flow-row')).to_have_count(3)
        expect(self.row(page, video)).to_have_count(1)
        expect(self.row(page, carousel)).to_have_count(1)
        self.data['jobs'] = []
        page.reload()
        expect(page.locator('.flow-row')).to_have_count(2)
        expect(self.row(page, carousel)).to_have_count(1)
        self.assertEqual(page.locator('#production-type, #production-queue').count(), 0)

    def test_batch_archive_restore_and_delete_handle_both_order_kinds_in_one_workspace(self):
        first, second, old = self.job(1), self.job(2), self.job(3, archived=True)
        carousel, archived_carousel = self.carousel(1), self.carousel(2, archived=True)
        page = self.open_queue()
        managed, deleted = [], []
        def manage(route):
            body = route.request.post_data_json
            managed.append(body)
            self.management['items'][body['id']] = body['management']
            if not body['management']['archived']:
                for item in [*self.data['jobs'], *self.data['carousels']]:
                    if body['id'].endswith(':' + item['id']):
                        item['archived'] = False
            self.management['revision'] += 'x'
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'revision': self.management['revision']}))
        def remove(route):
            body = route.request.post_data_json
            deleted.append(body)
            identifiers = {item['id'] for item in body['items']}
            for key in ('jobs', 'carousels'):
                self.data[key] = [item for item in self.data[key] if item['id'] not in identifiers]
            route.fulfill(status=200, content_type='application/json', body=json.dumps({
                'changed':len(identifiers), 'failed':0, 'skipped':0,
                'results':[{**item, 'ok':True, 'warnings':[]} for item in body['items']]}))
        page.route('**/api/content/manage', manage)
        page.route('**/api/production/orders/batch', remove)
        self.start_batch(page)
        hash_before = page.evaluate('location.hash')
        self.row(page, first).locator('[data-flow-select]').check()
        self.assertEqual(page.evaluate('location.hash'), hash_before)
        expect(page.locator('#content-flow-batch')).to_contain_text('已选 1 项')
        page.locator('[data-flow-select-all]').click()
        page.locator('[data-flow-archive-selected]').click()
        expect(page.locator('#content-flow-select')).to_be_hidden()
        self.assertEqual({body['id'] for body in managed}, {'video:' + first['id'], 'video:' + second['id'], 'carousel:' + carousel['id']})
        self.start_batch(page)
        self.row(page, old).locator('[data-flow-select]').check()
        page.locator('[data-flow-restore-selected]').click()
        expect(page.locator('#content-flow-select')).to_be_hidden()
        self.assertEqual(managed[-1]['id'], 'video:' + old['id'])
        self.assertFalse(managed[-1]['management']['archived'])
        self.start_batch(page)
        page.locator('[data-flow-select-all]').click()
        page.locator('[data-flow-delete-selected]').click()
        page.locator('.production-dialog input[name="sure"]').check()
        page.locator('.production-dialog [data-submit]').click()
        expect(page.locator('.flow-row')).to_have_count(0)
        self.assertEqual(len(deleted), 1)
        self.assertEqual(len(deleted[0]['items']), 5)
        self.assertEqual({item['kind'] for item in deleted[0]['items']}, {'job', 'carousel'})

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

    def test_failed_catalog_refresh_keeps_records_and_retry_reads_latest_status(self):
        review = self.job(1, 'awaiting_review')
        self.carousel(1)
        page = self.open_queue()
        held = []
        page.route('**/api/content?*', lambda route: held.append(route))
        page.locator('#content-flow-refresh').click()
        page.wait_for_timeout(50)
        held.pop().fulfill(status=503, content_type='application/json', body=json.dumps({'error':'隔离测试读取失败'}))
        page.locator('#content-flow-error').get_by_text('隔离测试读取失败', exact=True).wait_for()
        expect(self.row(page, review)).to_have_count(1)
        self.row(page, review).get_by_text('待人工审查', exact=True).wait_for()
        review['status'] = 'approved'
        page.locator('#content-flow-refresh').click()
        page.wait_for_timeout(50)
        latest = held.pop()
        latest.fulfill(status=200, content_type='application/json', body=json.dumps(self.catalog(latest.request.url)))
        self.row(page, review).get_by_text('待发布', exact=True).wait_for()
        expect(page.locator('#content-flow-error')).to_be_hidden()
        expect(page.locator('.flow-row')).to_have_count(2)


if __name__ == '__main__':
    unittest.main()
