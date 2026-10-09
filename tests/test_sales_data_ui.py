"""Browser checks for read-only sales data; all saves use disposable cases."""
from __future__ import annotations

import copy
import json
import re
import unittest
from urllib.parse import urlparse

import test_server as fixture
import test_ui_regressions as browser


def sales(case, *, status="matched", sold=42):
    identity = (case.get("current") or {}).get("metrics") or case.get("metrics") or {}
    video_id = identity.get("video_id", "")
    return {
        "status": status, "source_label": "销售数据表",
        "metrics": {"video_id": video_id, "creator_handle": "@sales_account", "published_title": "Sales title",
                    "views_display": "100001", "product_impressions_display": "110002", "product_clicks_display": "1000",
                    "items_sold": sold, "ctor_display": "4.20%"} if status == "matched" else {"video_id": video_id},
        "evidence": {"status": "source_checked", "source": "销售数据表", "window": "已导入覆盖 2026-10-01 至 2026-10-07",
                     "as_of": "2026-10-07", "context": "中间有缺口，仅显示已覆盖的实际数据。"} if status == "matched" else {},
        "video": {"gmv": 3210, "orders": 40, "first_data": "2026-10-01", "last_data": "2026-10-07"} if status == "matched" else None,
    }


class SalesDataBrowserTests(unittest.TestCase):
    setUp = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def open_sales_page(self, hash_route="#library", statuses=None, sales_snapshots=None):
        statuses = statuses or {}
        sales_snapshots = sales_snapshots if sales_snapshots is not None else {}
        context = self.browser.new_context(viewport={"width": 1080, "height": 680})
        self.addCleanup(context.close)
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        self.addCleanup(lambda: self.assertEqual(errors, [], "Browser JavaScript errors"))

        def response(route):
            if route.request.method != "GET":
                route.continue_(); return
            parsed = urlparse(route.request.url)
            direction_detail = parsed.path.startswith("/api/direction-catalog/")
            if parsed.path != "/api/state" and parsed.path != "/api/directions" and not direction_detail and not re.fullmatch(r"/api/cases/PVR-[0-9a-f]{12}", parsed.path):
                route.continue_(); return
            status, _, data = self.request("GET", parsed.path + ("?" + parsed.query if parsed.query and parsed.path != "/api/state" else ""))
            if status == 200:
                records = data.get("cases") if parsed.path == "/api/state" else [data] if "/api/cases/" in parsed.path else []
                for case in records or []:
                    case["sales_data"] = sales_snapshots.get(case["case_id"]) or sales(case, status=statuses.get(case["case_id"], "matched"))
                if parsed.path == "/api/state":
                    data["signature"] += json.dumps([case["sales_data"] for case in records], sort_keys=True)
                if parsed.path == "/api/directions" or direction_detail:
                    groups = [data["group"]] if direction_detail and data.get("group") else data.get("groups", [])
                    for group in groups:
                        for observation in group["observations"]:
                            observation["sales_data"] = sales(observation, status=statuses.get(observation["case_id"], "matched"))
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

        page.route("**/api/**", response)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/{hash_route}")
        page.locator("#connection-text").get_by_text("已连接本地案例库").wait_for()
        return page

    def add_direction(self, identifier):
        current = copy.deepcopy(self.store.read_case(identifier)["current"])
        current["test_direction"] = {"direction_id": "202H-lock-angle", "main_difference": "锁角操作",
                                     "concern": "角度不好重复", "closing": "查看整套工具", "variant_type": "direction"}
        self.store.append(identifier, {"kind": "editorial", "text": "隔离方向", "current": current})

    def test_editor_removes_sales_inputs_and_preserves_hidden_record_fields(self):
        previous = self.store.read_case(self.identifier)
        current = copy.deepcopy(previous["current"])
        screenshot = self.root / "legacy-sales.png"
        screenshot.write_bytes(b"historical screenshot reference")
        current["assets"]["screenshot"] = str(screenshot)
        self.store.append(self.identifier, {"kind": "editorial", "text": "保留旧截图", "current": current})
        page = self.open_sales_page(f"#case/{self.identifier}")
        page.locator('.detail-tabs [data-tab="data"]').click()
        section = page.locator("#detail-section-performance")
        section.get_by_text("100001", exact=True).wait_for()
        self.assertEqual(page.locator("#sales-import,.evidence-block,#case-backend-data,#case-backend-fill").count(), 0)
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        self.assertEqual(page.locator('[name="metric_views_display"],[name="metric_items_sold"],[name="metric_ctor_display"],[name^="evidence_"],[name="asset_screenshot"],[data-upload="screenshot"]').count(), 0)
        self.assertEqual(page.locator('[name="metric_video_id"]').input_value(), current["metrics"]["video_id"])
        page.locator('[name="title"]').fill("只修改案例标题")
        page.locator('[name="note"]').fill("销售数据仍统一读取销售表")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        saved = self.store.read_case(self.identifier)["current"]
        self.assertEqual(saved["metrics"], current["metrics"])
        self.assertEqual(saved["evidence"], current["evidence"])
        self.assertEqual(saved["assets"]["screenshot"], str(screenshot))

    def test_current_views_use_sales_table_and_not_historical_screenshot_metrics(self):
        self.add_direction(self.identifier)
        second = self.store.add({"title": "第二条", "product": "SHARPAL 202H", "original": {"text": "Fixture"},
                                 "feedback": {"kind": "editorial", "text": "隔离案例"}, "current": fixture.new_case_snapshot(self.video)})
        self.add_direction(second["case_id"])
        page = self.open_sales_page()
        page.locator(".card-metrics").first.get_by_text("100001", exact=True).wait_for()
        self.assertEqual(page.locator(".card-metrics").get_by_text("0.94%", exact=True).count(), 0)
        self.assertEqual(page.locator(".card-foot").get_by_text("客单价背景", exact=False).count(), 0)
        page.get_by_role("button", name="列表", exact=True).click()
        self.assertEqual(page.locator(".case-table tbody").get_by_text("4.20%", exact=True).count(), 2)
        page.locator("#select-visible").check()
        page.locator("#compare-selected").click()
        page.locator("#case-comparison").get_by_text("100,001", exact=True).first.wait_for()
        self.assertEqual(page.locator("#case-comparison").get_by_text("0.94%", exact=True).count(), 0)
        page.locator("#close-comparison").click()
        page.locator("#directions-nav").click()
        page.locator(".direction-card-link").first.click()
        page.locator(".direction-observations").get_by_text("100001", exact=True).first.wait_for()
        self.assertEqual(page.locator(".direction-observations").get_by_text("0.94%", exact=True).count(), 0)
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{self.identifier}")
        page.locator('.detail-tabs [data-tab="data"]').click()
        section = page.locator("#detail-section-performance")
        section.get_by_text("中间有缺口，仅显示已覆盖的实际数据。", exact=True).wait_for()
        section.locator(".facts-grid").get_by_text("42", exact=True).wait_for()
        section.locator(".facts-grid").get_by_text("40", exact=True).wait_for()
        self.assertEqual(section.locator('a[href="#videos?video=7664986913497009439"]').count(), 1)
        self.assertEqual(section.get_by_text("0.94%", exact=True).count(), 0)

    def test_unmatched_unavailable_and_missing_identity_do_not_show_old_figures(self):
        for status, label in (("not_found", "销售数据表未匹配到此视频"), ("unavailable", "销售数据表暂不可用"),
                              ("missing_video_id", "未填写视频 ID")):
            with self.subTest(status=status):
                page = self.open_sales_page(f"#case/{self.identifier}", {self.identifier: status})
                page.locator('.detail-tabs [data-tab="data"]').click()
                section = page.locator("#detail-section-performance")
                section.get_by_text(label, exact=True).wait_for()
                self.assertEqual(section.locator(".facts-grid").count(), 0)
                self.assertEqual(section.get_by_text("0.94%", exact=True).count(), 0)
                self.assertEqual(section.get_by_role("link", name="查看销售数据表").count(), 1)
                page.close()

    def test_collect_from_sales_table_prefills_only_video_identity_and_publish_date(self):
        page = self.open_sales_page()
        video = {"video_id": "7664986913497009499", "headline": "销售表新视频", "title": "Sales title", "products": "SHARPAL 202H",
                 "product": "SHARPAL 202H", "account": "sales_account", "posted_on": "2026-10-01", "posted_at": "2026-10-01",
                 "views": 100001, "items": 42, "gmv": 3210, "ctor_pct": 4.2, "status": "follow", "status_label": "跟进",
                 "window_views": 5000, "this_week_gmv": 0, "review_comments": 0}
        page.route("**/api/performance/videos", lambda route: route.fulfill(status=200, content_type="application/json",
                                                                           body=json.dumps({"videos": [video]})))
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#videos?video={video['video_id']}")
        page.get_by_role("button", name="收为案例", exact=True).click()
        self.assertEqual(page.locator('[name="metric_video_id"]').input_value(), video["video_id"])
        self.assertEqual(page.locator('[name="publish_published_on"]').input_value(), "2026-10-01")
        self.assertEqual(page.locator('[name="metric_items_sold"]').count(), 0)
        page.locator('[name="conclusion"]').fill("仅补文案判断，表现读取销售表")
        page.locator("#save-button").click()
        page.locator("#edit-dialog").wait_for(state="hidden")
        new = next(c for c in self.store.read_index()["cases"] if c["current"].get("metrics", {}).get("video_id") == video["video_id"])
        self.assertEqual(set(new["current"]["metrics"]), {"video_id", "creator_handle", "published_title"})
        self.assertEqual(new["current"]["evidence"], {"status": "unverified"})

    def test_sales_refresh_updates_cutoff_and_historical_note_without_replacing_player(self):
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["conclusion"] = "播放量 100001，售出 42 件。"
        current["publish"] = {"published_on": "2026-10-01"}
        self.store.append(self.identifier, {"kind": "editorial", "text": "隔离实时数据刷新", "current": current})
        data = {self.identifier: sales({"current": current})}
        page = self.open_sales_page(f"#case/{self.identifier}", sales_snapshots=data)
        page.locator('.detail-tabs [data-tab="data"]').click()
        page.locator("#detail-section-performance").get_by_text("100001", exact=True).wait_for()
        page.evaluate("window.savedPlayer = document.querySelector('#case-player')")
        self.assertEqual(page.locator("#detail-section-consistency").count(), 0)
        data[self.identifier]["metrics"]["views_display"] = "200002"
        data[self.identifier]["evidence"]["as_of"] = "2026-10-08"
        page.evaluate("refresh(true)")
        page.locator("#detail-section-performance").get_by_text("200002", exact=True).wait_for()
        page.locator("#detail-section-release").get_by_text("2026-10-08", exact=True).wait_for()
        page.locator("#detail-section-consistency").get_by_text("销售数据表当前为 200002", exact=False).wait_for()
        self.assertTrue(page.evaluate("window.savedPlayer === document.querySelector('#case-player')"))
        page.locator("#release-edit").click()
        page.locator('[name="publish_published_on"]').wait_for()
        self.assertEqual(self.store.read_case(self.identifier)["current"]["conclusion"], current["conclusion"])


if __name__ == "__main__":
    unittest.main()
