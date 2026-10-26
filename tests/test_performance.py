"""Video data from TikTok Shop exports: import, dedup, follow-up status, HTTP and page."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import openpyxl

import test_server as fixture
import test_ui_regressions as browser
from performance_store import PerformanceStore

LINKED = "7664986913497009439"  # the fixture case's TikTok video ID
HEADER = ["Creator name", "Creator ID", "Video Info", "Video ID", "Time", "Products", "Video views", "Likes", "Comments",
          "Shares", "New followers", "V-to-L clicks", "Product Impressions", "Product Clicks", "Unique customers",
          "Attributed SKU orders", "Video SKU orders", "Video indirect SKU orders", "Video-attributed items sold",
          "Video items sold", "Video indirect items sold", "Video-attributed GMV ($)", "Video GMV ($)",
          "Video indirect GMV ($)", "GPM ($)", "Click-through rate (Video)", "V-to-L rate", "Video Finish Rate",
          "CTOR (SKU order)", "Diagnosis"]
TOTALS_HEADER = ["Time", "Video-attributed GMV ($)", "Video GMV ($)", "Video indirect GMV ($)", "Video views", "GPM ($)",
                 "Attributed SKU orders", "Video SKU orders", "Video indirect SKU orders", "Avg. daily customers",
                 "Product viewers", "Product Impressions", "Product Clicks", "Click-through rate (Video)", "CTOR (SKU order)"]
VIDEOS = {  # id: (account, title, posted)
    LINKED: ("bushcraftedge", "🤔 Is A Mirror Edge Overrated?#knifesharpening #edc", "2026/08/31 05:00:00"),
    "7600000000000000002": ("sharpal_us", "🔴Angle guide demo#SHARPAL", "2026/09/02 06:00:00"),
    "7600000000000000003": ("bushcraftedge", "🤔 Is A Full Kit Overkill?#knife", "2026/09/09 06:00:00"),
    "7600000000000000004": ("sharpal_us", "🔴Older video#SHARPAL", "2026/08/20 06:00:00"),
}


def row(video_id, views, orders=0, clicks=0, gmv=0.0, header="Video views"):
    account, title, posted = VIDEOS[video_id]
    values = {"Creator name": account, "Creator ID": "1", "Video Info": title, "Video ID": video_id, "Time": posted,
              "Products": "SHARPAL 202H Precision Kitchen Knife Sharpener", header: str(views),
              "Product Impressions": str(views), "Product Clicks": str(clicks), "Attributed SKU orders": str(orders),
              "Video-attributed items sold": str(orders), "Video-attributed GMV ($)": str(gmv),
              "Click-through rate (Video)": "2.00%", "CTOR (SKU order)": "1.00%", "Diagnosis": "No issues"}
    return [values.get(name, "0") for name in (HEADER if header == "Video views" else [header if h == "Video views" else h for h in HEADER])]


def export(path, start, end, rows, header=HEADER):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append([f"[Date Range]: {start} ~ {end}\n"])
    sheet.append([None])
    sheet.append(header)
    for values in rows:
        sheet.append(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    book.save(path)
    return path


def build_exports(folder):
    """Weeks 8/31–9/6 and 9/7–9/13, a daily file inside week one (must be ignored),
    a daily file on 8/30 (outside any week), all-video totals and a bad file."""
    export(folder / "9月" / "第一周视频.xlsx", "2026-08-31", "2026-09-06",
           [row(LINKED, 8000, 3, 200, 450.0), row("7600000000000000002", 500)])
    export(folder / "9月" / "第二周视频.xlsx", "2026-09-07", "2026-09-13",
           [row(LINKED, 4000, 1, 100, 150.0), row("7600000000000000002", 300), row("7600000000000000003", 900)])
    daily_header = ["VV" if h == "Video views" else h for h in HEADER]
    export(folder / "8月" / "260901.xlsx", "2026-09-01", "2026-09-01", [row(LINKED, 999999, header="VV")], daily_header)
    export(folder / "8月" / "260830.xlsx", "2026-08-30", "2026-08-30", [row("7600000000000000004", 50, header="VV")], daily_header)
    export(folder / "9月" / "第二周销售.xlsx", "2026-09-07", "2026-09-13",
           [["2026-09-07", "100", "90", "10", "5000", "20", "3", "2", "1", "3", "4000", "5100", "120", "0.0244", "0.0125"]], TOTALS_HEADER)
    export(folder / "other.xlsx", "2026-09-01", "2026-09-01", [["x"]], ["Something else"])
    (folder / "~$第一周视频.xlsx").write_bytes(b"lock file")


class PerformanceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="performance-test-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "exports"
        build_exports(self.folder)
        self.store = PerformanceStore(Path(self.temp.name) / "runtime", [self.folder])

    def test_weekly_rows_replace_daily_days_and_status_follows_window(self):
        result = self.store.sync()
        self.assertEqual(len(result["imported"]), 5)
        self.assertEqual([f["file"] for f in result["failed"]], ["other.xlsx"])
        videos = {v["video_id"]: v for v in self.store.videos({LINKED: "PVR-000000000001"})}
        linked = videos[LINKED]
        # 9/1 is covered by the weekly export, so its daily 999,999 views are not added.
        self.assertEqual((linked["views"], linked["items"], linked["gmv"]), (12000, 4, 600))
        self.assertEqual((linked["window_views"], linked["status"], linked["case_id"]), (12000, "focus", "PVR-000000000001"))
        self.assertEqual(linked["headline"], "🤔 Is A Mirror Edge Overrated?")
        self.assertEqual((linked["product"], linked["ctor_pct"], linked["this_week_gmv"]), ("202H", 1.33, 150))
        self.assertEqual((videos["7600000000000000002"]["window_views"], videos["7600000000000000002"]["status"]), (800, "drop"))
        self.assertEqual(videos["7600000000000000003"]["status"], "pending")  # window ends 9/20, data ends 9/13
        self.assertEqual(videos["7600000000000000004"]["status"], "incomplete")  # posted 8/20, data starts 8/30
        connection = sqlite3.connect(self.store.database)
        try:
            self.assertAlmostEqual(connection.execute("SELECT ctr_pct FROM totals_daily").fetchone()[0], 2.44)
        finally:
            connection.close()
        summary = self.store.summary()
        self.assertEqual(summary["coverage"], {"first": "2026-08-30", "last": "2026-09-13", "gaps": []})
        self.assertEqual([(w["week"], w["complete"], round(w["gmv"])) for w in summary["weeks"]],
                         [("2026-08-24", False, 0), ("2026-08-31", True, 450), ("2026-09-07", True, 150)])
        detail = self.store.video(LINKED)
        self.assertEqual([s["start"] for s in detail["series"]], ["2026-08-31", "2026-09-07"])
        # Rank among judged windows (12,000 vs 800) and among every video's GMV (600 vs three at 0).
        self.assertEqual((detail["window_top_pct"], detail["judged_videos"], detail["gmv_top_pct"], detail["all_videos"]),
                         (50, 2, 25, 4))
        self.assertEqual(self.store.video("7600000000000000002")["window_top_pct"], 100)
        self.assertNotIn("window_top_pct", self.store.video("7600000000000000003"))  # window still open

    def test_video_brief_gives_the_light_figures_for_just_the_videos_asked_for(self):
        fresh = PerformanceStore(Path(self.temp.name) / "never-imported", [self.folder])
        self.assertEqual(fresh.video_brief([LINKED]), {})
        self.assertFalse((Path(self.temp.name) / "never-imported" / "performance").exists())  # asking creates nothing
        self.store.sync()
        brief = self.store.video_brief([LINKED, "7999999999999999999", "", None])
        self.assertEqual(list(brief), [LINKED])  # a video the exports do not know is left out
        self.assertEqual({key: brief[LINKED][key] for key in ("headline", "account", "posted_on", "views", "clicks", "orders", "items", "gmv", "ctor_pct")},
                         {"headline": "🤔 Is A Mirror Edge Overrated?", "account": "bushcraftedge", "posted_on": "2026-08-31", "views": 12000,
                          "clicks": 300, "orders": 4, "items": 4, "gmv": 600, "ctor_pct": 1.33})
        # the same figures the full list works out, without the follow-up status that makes it slow
        full = {v["video_id"]: v for v in self.store.videos()}[LINKED]
        self.assertEqual((brief[LINKED]["views"], brief[LINKED]["gmv"], brief[LINKED]["ctor_pct"]), (full["views"], full["gmv"], full["ctor_pct"]))
        self.assertEqual(self.store.video_brief([]), {})

    def test_resync_skips_unchanged_files_and_reimport_replaces_rows(self):
        self.store.sync()
        again = self.store.sync()
        self.assertEqual((again["imported"], again["skipped"]), ([], 5))
        export(self.folder / "9月" / "第二周视频.xlsx", "2026-09-07", "2026-09-13", [row(LINKED, 6000, 1, 100, 150.0)])
        changed = self.store.sync()
        self.assertEqual([f["file"] for f in changed["imported"]], ["第二周视频.xlsx"])
        videos = {v["video_id"]: v for v in self.store.videos()}
        self.assertEqual(videos[LINKED]["views"], 14000)
        # The week's old rows were replaced, not appended: video 3 is gone from week two.
        self.assertNotIn("7600000000000000003", videos)

    def test_missing_folder_is_reported(self):
        store = PerformanceStore(Path(self.temp.name) / "runtime2", [Path(self.temp.name) / "nowhere"])
        result = store.sync()
        self.assertEqual((result["imported"], result["missing_sources"]), ([], [str(Path(self.temp.name) / "nowhere")]))
        self.assertEqual(store.summary()["coverage"]["first"], None)


class PerformanceHTTPTests(unittest.TestCase):
    setUp_ = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        self.setUp_()
        self.folder = self.root / "exports"
        build_exports(self.folder)
        self.server.app.performance.sources = [self.folder]

    def test_sync_list_detail_and_case_link(self):
        status, _, result = self.request("POST", "/api/performance/sync", {})
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result["imported"]), 5)
        status, _, data = self.request("GET", "/api/performance/videos")
        linked = next(v for v in data["videos"] if v["video_id"] == LINKED)
        self.assertEqual((linked["case_id"], linked["review_status"]), (self.identifier, {"key": "pending", "label": "待复盘"}))
        status, _, detail = self.request("GET", f"/api/performance/videos/{LINKED}")
        self.assertEqual((status, detail["video"]["gmv"]), (200, 600))
        self.assertEqual(self.request("GET", "/api/performance/videos/7999999999999999999")[0], 404)
        status, _, summary = self.request("GET", "/api/performance/summary")
        self.assertEqual(summary["coverage"]["last"], "2026-09-13")
        self.assertEqual(self.request("POST", "/api/performance/sync", {"x": 1})[0], 400)
        self.assertEqual(self.request("POST", "/api/performance/sync", {}, authenticated=False)[0], 403)

    def test_upload_then_import_keeps_a_copy_and_rejects_outside_paths(self):
        source = self.folder / "9月" / "第一周视频.xlsx"
        status, _, staged = self.request("POST", "/api/upload?kind=performance&name=%E7%AC%AC%E4%B8%80%E5%91%A8.xlsx",
                                         raw=source.read_bytes(), headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, staged)
        status, _, result = self.request("POST", "/api/performance/import", {"paths": [staged["path"]]})
        self.assertEqual(status, 200, result)
        self.assertEqual(result["imported"][0]["rows"], 2)
        kept = list((self.runtime / "performance" / "uploads").glob("*.xlsx"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(self.request("POST", "/api/performance/import", {"paths": [str(source)]})[0], 400)
        self.assertEqual(self.request("POST", "/api/performance/import", {"paths": []})[0], 400)
        self.assertEqual(self.request("POST", "/api/upload?kind=performance&name=a.csv", raw=b"x,y",
                                      headers={"Content-Type": "application/octet-stream"})[0], 400)


class PerformanceAnalyticsTests(unittest.TestCase):
    setUp = PerformanceStoreTests.setUp

    def test_analytics_rows_cover_weeks_months_and_shop_totals(self):
        self.store.sync()
        data = self.store.analytics()
        videos = {v[0]: i for i, v in enumerate(data["videos"])}
        self.assertEqual(data["videos"][videos[LINKED]][1:], ["bushcraftedge", "202H", "2026-08-31"])
        linked = [r for r in data["weekly"] if r[0] == videos[LINKED]]
        # 9/1 is inside the weekly export, so the daily row is not counted again.
        self.assertEqual([(r[1], r[2], r[7]) for r in linked], [("2026-08-31", 8000, 450.0), ("2026-09-07", 4000, 150.0)])
        self.assertEqual(sum(r[7] for r in data["weekly"]), sum(v["gmv"] for v in self.store.videos()))
        self.assertEqual(data["months"], {"2026-08": "日报表明细合计", "2026-09": "周报表合计"})
        self.assertEqual(sum(r[7] for r in data["monthly"] if r[1] == "2026-09"), 450.0 + 150.0 + sum(r[7] for r in data["weekly"] if r[1] == "2026-09-07" and r[0] != videos[LINKED]))
        self.assertEqual(data["shop_daily"], [{"date": "2026-09-07", "gmv": 100.0, "orders": 3, "views": 5000, "clicks": 120}])


class PerformanceBrowserTests(unittest.TestCase):
    setUp_ = browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    open_page = browser.WorkbenchBrowserTests.open_page
    make_playable_video = browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        self.setUp_()
        self.folder = self.root / "exports"
        build_exports(self.folder)
        self.server.app.performance.sources = [self.folder]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)

    def open_list(self):
        page = self.open_page("#videos")
        page.locator(".perf-tabs [data-perf-tab=list]").click()
        page.locator(".perf-table").wait_for()
        return page

    def add_product_without_video_data(self):
        self.store.add({"title": "另一产品", "product": "SHARPAL 999H",
                        "original": {"text": "Isolated product context fixture"},
                        "feedback": {"kind": "editorial", "text": "隔离验证"},
                        "current": fixture.new_case_snapshot(self.video)})

    def test_dashboard_draws_charts_and_follows_filters(self):
        self.add_product_without_video_data()
        page = self.open_page("#videos")
        page.locator(".perf-kpis").wait_for()
        self.assertEqual(page.locator(".perf-kpi").count(), 6)  # six metric tiles; each one picks the main chart
        self.assertEqual(page.locator("#perf-tab-board").get_attribute("aria-selected"), "true")
        self.assertGreaterEqual(page.locator(".perf-board svg.perf-chart").count(), 5)
        page.locator('[data-perf-metric="views"]').click()
        page.locator('[data-perf-metric="views"][aria-pressed="true"]').wait_for()
        self.assertIn("播放量", page.locator(".perf-main .perf-mini-head").inner_text())
        self.assertIn("$", page.locator(".perf-kpi").first.inner_text())
        page.locator(".perf-hit").first.hover()
        page.locator(".perf-tooltip:not([hidden])").wait_for()
        page.locator("#perf-account").select_option(index=1)
        self.assertEqual(page.locator("#perf-reset").is_visible(), True)
        page.locator("#global-product").select_option("999H")
        page.locator(".perf-board .empty").wait_for()
        self.assertIn("#/999H/videos", page.url)
        page.locator("#perf-reset").click()
        self.assertEqual(page.locator("#global-product").input_value(), "999H")
        self.assertEqual(page.locator("#perf-account").input_value(), "")
        page.locator(".perf-board .empty").wait_for()
        page.locator("#global-product").select_option("202H")
        page.locator(".perf-kpis").wait_for()
        page.locator(".perf-board .perf-more[data-perf-tab=list]").click()
        page.locator(".perf-table").wait_for()
        self.assertEqual(page.locator("#perf-tab-list").get_attribute("aria-selected"), "true")

    def test_deleted_export_rows_are_hidden_unless_searched_by_id(self):
        deleted = "7695409803794926878"
        values = row("7600000000000000003", 0)
        values[HEADER.index("Video Info")], values[HEADER.index("Video ID")] = "", deleted
        values[HEADER.index("Time")] = "2026/09/12 06:00:42"
        export(self.folder / "9月" / "删除视频.xlsx", "2026-09-07", "2026-09-13", [values])
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        page = self.open_list()
        self.assertEqual(page.locator(f'[data-video-row="{deleted}"]').count(), 0)
        self.assertIn("已隐藏 1 条空视频", page.locator(".perf-toolbar .result-count").inner_text())
        page.locator("#perf-q").fill(deleted)
        page.locator(f'[data-video-row="{deleted}"]').wait_for()
        self.assertNotIn("已隐藏", page.locator(".perf-toolbar .result-count").inner_text())

    def test_table_headers_sort_rows(self):
        page = self.open_list()
        table = page.locator(".perf-table")
        table.locator("tbody tr").first.wait_for()
        self.assertEqual(page.locator("#perf-sort").count(), 0)
        column = lambda: [int(t.replace("$", "").replace(",", "")) for t in table.locator("tbody tr td:nth-child(7)").all_inner_texts()]
        table.locator('button[data-sort="gmv"]').click()
        self.assertEqual(table.locator('th[aria-sort="descending"] [data-sort="gmv"]').count(), 1)
        self.assertEqual(column(), sorted(column(), reverse=True))
        table.locator('button[data-sort="gmv"]').click()
        self.assertEqual(table.locator('th[aria-sort="ascending"] [data-sort="gmv"]').count(), 1)
        self.assertEqual(column(), sorted(column()))

    def test_global_product_filter_and_page_reset_keep_context(self):
        self.add_product_without_video_data()
        page = self.open_list()
        rows = page.locator(".perf-table tbody tr")
        rows.first.wait_for()
        total = rows.count()
        self.assertEqual(page.locator("#perf-product").count(), 0)
        self.assertEqual(page.locator("#perf-reset").is_hidden(), True)
        picker = page.locator("#global-product")
        self.assertEqual(picker.input_value(), "202H")
        picker.select_option("999H")
        page.wait_for_function("() => !document.querySelector('.perf-table')")
        self.assertIn("#/999H/videos", page.url)
        self.assertEqual(picker.input_value(), "999H")
        self.assertEqual(page.locator("#perf-reset").is_hidden(), True)
        picker.select_option("202H")
        page.wait_for_function("(n) => document.querySelectorAll('.perf-table tbody tr').length === n", arg=total)
        page.locator("#perf-account").select_option(index=1)
        self.assertLess(rows.count(), total)
        self.assertEqual(page.locator("#perf-reset").is_visible(), True)
        page.locator("#perf-reset").click()
        self.assertEqual(picker.input_value(), "202H")
        self.assertEqual(page.locator("#perf-account").input_value(), "")
        self.assertEqual(rows.count(), total)
        self.assertEqual(page.locator("#perf-reset").is_hidden(), True)
        picker.select_option("")
        page.wait_for_url("**/#/all/videos*")
        page.wait_for_function("(n) => document.querySelectorAll('.perf-table tbody tr').length === n", arg=total)

    def test_video_page_collect_and_case_sales_table_display(self):
        page = self.open_list()
        table = page.locator(".perf-table")
        table.get_by_text("🤔 Is A Mirror Edge Overrated?").wait_for()
        linked_row = table.locator("tr", has_text="Is A Mirror Edge Overrated?")
        linked_row.get_by_text("待复盘").wait_for()
        linked_row.locator("td.perf-title").click()  # a row opens the video's details drawer
        drawer = page.locator("#perf-video-drawer[open]")
        drawer.get_by_role("link", name="查看案例").wait_for()
        drawer.get_by_role("button", name="关闭视频详情").click()
        self.assertEqual(page.locator("#videos-nav.active").count(), 1)
        page.locator("#perf-q").fill("Angle guide demo")
        page.wait_for_function("() => document.querySelectorAll('.perf-table tbody tr').length === 1")
        self.assertEqual(table.locator("tbody tr").count(), 1)
        table.locator("tr", has_text="Angle guide demo").locator("td.perf-title").click()
        page.locator("#perf-video-drawer[open]").get_by_role("button", name="收为案例").click()
        self.assertEqual(page.locator('[name="title"]').input_value(), "🔴Angle guide demo")
        self.assertEqual(page.locator('[name="metric_video_id"]').input_value(), "7600000000000000002")
        self.assertEqual(page.locator('[name="publish_published_on"]').input_value(), "2026-09-02")
        self.assertEqual(page.locator('[name="content_origin"]').input_value(), "self_produced")
        self.assertEqual(page.locator('[name^="evidence_"],[name="metric_views_display"],[name="metric_items_sold"]').count(), 0)
        page.locator("#cancel-editor").click()
        page.locator("#discard-edits").click()
        copied = page.evaluate("window.CasePerformance.reviewMaterial()")
        self.assertIn("本周刚满两周观察期的新视频", copied)
        self.assertIn("Is A Mirror Edge Overrated?", copied)

        page.goto(f"http://127.0.0.1:{self.server.server_port}/#case/{self.identifier}")
        page.locator('.detail-tabs [data-tab="data"]').click()
        box = page.locator("#detail-section-performance")
        box.get_by_role("link", name="查看销售数据表").wait_for()
        box.get_by_text("12000", exact=True).first.wait_for()
        box.get_by_text("1.33%", exact=True).wait_for()
        self.assertEqual(page.locator("#case-backend-data,#case-backend-fill,#sales-import").count(), 0)
        previous = self.store.read_case(self.identifier)["current"]
        page.locator(".detail-heading").wait_for()
        page.evaluate("openEditor(state.detail, {advanced: true})")
        page.locator('[name="note"]').fill("普通编辑保留历史数据，销售读取表格")
        page.locator("#save-button").click()
        page.wait_for_function("() => !document.querySelector('#edit-dialog').open")
        current = self.store.read_case(self.identifier)["current"]
        self.assertEqual(current["metrics"], previous["metrics"])
        self.assertEqual(current["evidence"], previous["evidence"])
        self.assertNotIn("publish", current)


if __name__ == "__main__":
    unittest.main()
