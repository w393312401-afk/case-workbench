"""Overseas creator videos from the all-creator exports: import, own-account split, ranges, HTTP and the tab."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import test_performance as perf
from performance_store import PerformanceStore
from public_stats import PublicStatsStore

CREATOR_HEADER = [*perf.HEADER[:5], "Creator country", *perf.HEADER[5:]]
KIT = "SHARPAL 202H Precision Kitchen Knife Sharpening System(1732157836537861037)"
SUCTION = "SHARPAL 191H Black Small Compact Kitchen Knife Sharpener(1729474295948088237)"


def creator_row(video_id, account, gmv=0.0, orders=0, views=100, clicks=10, posted="2026/07/20 10:00:00", products=KIT, title="Creator demo #knife"):
    values = {"Creator name": account, "Creator ID": "9", "Video Info": title, "Video ID": video_id, "Time": posted,
              "Creator country": "United States" if account else "--", "Products": products, "Video views": str(views),
              "Product Impressions": str(views), "Product Clicks": str(clicks), "Attributed SKU orders": str(orders),
              "Video-attributed items sold": str(orders), "Video-attributed GMV ($)": str(gmv),
              "Click-through rate (Video)": "2.00%", "CTOR (SKU order)": "1.00%", "Diagnosis": "No issues"}
    return [values.get(name, "0") for name in CREATOR_HEADER]


def build_creator_exports(folder):
    """July (from the 14th) and August, plus a mid-August export that overlaps August and must not be added."""
    july = perf.export(folder / "达人" / "7月达人视频.xlsx", "2026-07-14", "2026-07-31", [
        creator_row("8100000000000000001", "alice", 100.0, 2, 1000, 40, products=f"{KIT},{SUCTION}", title="Alice shows the kit #SHARPAL"),
        creator_row("8100000000000000002", "sharpal.usa", 50.0, 1),        # brand handle: ours
        creator_row(perf.LINKED, "bushcraftedge", 20.0, 1),                 # account from the self-made exports: ours
        creator_row("8100000000000000004", None, 5.0, 1, posted="1969/12/31 16:00:00"),  # no creator name
    ], CREATOR_HEADER)
    perf.export(folder / "达人" / "8月达人视频.xlsx", "2026-08-01", "2026-08-30", [
        creator_row("8100000000000000001", "alice", 30.0, 1, 500, 20, products=f"{KIT},{SUCTION}", title="Alice shows the kit #SHARPAL"),
        creator_row("8100000000000000005", "bob", 0.0, 0, 300, 3, posted="2026/08/05 10:00:00", products=SUCTION),
        creator_row("8100000000000000006", "carol", 12.5, 1, 200, 5, posted="2026/08/09 10:00:00", products=SUCTION),
    ], CREATOR_HEADER)
    perf.export(folder / "达人" / "8月中旬.xlsx", "2026-08-10", "2026-08-20", [
        creator_row("8100000000000000001", "alice", 999.0, 9),
    ], CREATOR_HEADER)
    return july


class PerformanceCreatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="performance-creators-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / "exports"
        perf.build_exports(self.folder)
        self.july = build_creator_exports(self.folder)
        self.store = PerformanceStore(Path(self.temp.name) / "runtime", [self.folder])

    def test_creator_exports_stay_apart_from_self_made_figures(self):
        result = self.store.sync()
        creators = [f for f in result["imported"] if f["kind"] == "creator_periods"]
        self.assertEqual(sorted(f["file"] for f in creators), ["7月达人视频.xlsx", "8月中旬.xlsx", "8月达人视频.xlsx"])
        self.assertEqual([f["file"] for f in result["failed"]], ["other.xlsx"])
        # The self-made list, totals and coverage are what the self-made exports alone give.
        videos = {v["video_id"]: v for v in self.store.videos()}
        self.assertEqual(set(videos), {perf.LINKED, "7600000000000000002", "7600000000000000003", "7600000000000000004"})
        self.assertEqual(videos[perf.LINKED]["gmv"], 600)
        self.assertEqual(self.store.summary()["coverage"]["first"], "2026-08-30")

        data = self.store.creators({"8100000000000000001": "PVR-000000000009"})
        self.assertEqual([(p["label"], p["start"], p["end"], p["days"]) for p in data["periods"]],
                         [("7月", "2026-07-14", "2026-07-31", 18), ("8月", "2026-08-01", "2026-08-30", 30)])
        self.assertEqual(data["excluded_periods"], [{"start": "2026-08-10", "end": "2026-08-20", "files": ["8月中旬.xlsx"]}])
        kinds = {v[0]: (v[1], v[6]) for v in data["videos"]}
        self.assertEqual(kinds["8100000000000000001"], ("alice", "creator"))
        self.assertEqual(kinds["8100000000000000002"], ("sharpal.usa", "own"))
        self.assertEqual(kinds[perf.LINKED], ("bushcraftedge", "own"))
        self.assertEqual(kinds["8100000000000000004"], ("", "unnamed"))
        self.assertEqual(data["own_accounts"], ["bushcraftedge", "sharpal.usa"])
        alice = next(v for v in data["videos"] if v[0] == "8100000000000000001")
        self.assertEqual((alice[3], alice[4], alice[5]), ("Alice shows the kit", "2026-07-20", ["202H", "191H"]))
        self.assertIsNone(next(v for v in data["videos"] if v[0] == "8100000000000000004")[4])  # epoch = unknown
        index = {v[0]: i for i, v in enumerate(data["videos"])}
        alice_rows = sorted((r[1], r[5], r[7]) for r in data["rows"] if r[0] == index["8100000000000000001"])
        self.assertEqual(alice_rows, [(0, 2, 100.0), (1, 1, 30.0)])  # the overlapping 999 is never added
        self.assertEqual(data["cases"], {"8100000000000000001": "PVR-000000000009"})

    def test_reexported_file_replaces_its_earlier_range(self):
        self.store.sync()
        perf.export(self.july, "2026-07-01", "2026-07-31", [creator_row("8100000000000000001", "alice", 140.0, 3)], CREATOR_HEADER)
        changed = self.store.sync()
        self.assertEqual([f["file"] for f in changed["imported"]], ["7月达人视频.xlsx"])
        data = self.store.creators()
        self.assertEqual([(p["start"], p["days"]) for p in data["periods"]], [("2026-07-01", 31), ("2026-08-01", 30)])
        index = {v[0]: i for i, v in enumerate(data["videos"])}
        self.assertNotIn("8100000000000000002", index)  # July's old rows went with the old range
        self.assertEqual(sorted(r[7] for r in data["rows"] if r[0] == index["8100000000000000001"]), [30.0, 140.0])

    def test_empty_before_any_creator_export(self):
        store = PerformanceStore(Path(self.temp.name) / "runtime2", [])
        self.assertEqual({k: v for k, v in store.creators().items() if k != "cases"},
                         {"periods": [], "excluded_periods": [], "videos": [], "rows": [], "own_accounts": [], "public_stats": {}})

    def test_public_counts_refresh_outside_cached_period_rows_without_changing_ownership(self):
        self.store.sync()
        before = self.store.creators()
        self.store.public = PublicStatsStore(Path(self.temp.name) / "runtime")
        video_id, own_id = "8100000000000000001", "8100000000000000002"
        now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        values = {"views": 98765, "likes": 31, "comments": 0, "shares": None, "saves": 9}
        saved = self.store.public.record([{"id": video_id, **values}, {"id": own_id, **values}], self.store.known_video_ids, now=now)
        self.assertEqual(saved["saved"], 2)
        after = self.store.creators()
        self.assertEqual({key: value for key, value in after.items() if key != "public_stats"},
                         {key: value for key, value in before.items() if key != "public_stats"})
        expected = {**values, "observed_at": "2026-10-25T12:00:00Z", "last_seen_at": "2026-10-25T12:00:00Z"}
        self.assertEqual(after["public_stats"], {video_id: expected, own_id: expected})
        self.assertEqual(before["public_stats"], {})
        after["public_stats"][video_id]["likes"] = -1
        self.assertEqual(self.store.creators()["public_stats"][video_id]["likes"], 31)
        self.assertNotIn(video_id, {video["video_id"] for video in self.store.videos()})
        self.assertEqual(next(video[6] for video in after["videos"] if video[0] == own_id), "own")


class PerformanceCreatorHTTPTests(unittest.TestCase):
    setUp_ = perf.fixture.WorkbenchHTTPTests.setUp
    stop_server = perf.fixture.WorkbenchHTTPTests.stop_server
    request = perf.fixture.WorkbenchHTTPTests.request

    def setUp(self):
        self.setUp_()
        self.folder = self.root / "exports"
        perf.build_exports(self.folder)
        build_creator_exports(self.folder)
        self.server.app.performance.sources = [self.folder]

    def test_creators_endpoint_after_sync(self):
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        status, _, data = self.request("GET", "/api/performance/creators")
        self.assertEqual(status, 200, data)
        self.assertEqual([p["label"] for p in data["periods"]], ["7月", "8月"])
        self.assertEqual(data["cases"], {perf.LINKED: self.identifier})  # the fixture case links that video ID

    def test_extension_push_for_creator_only_video_reaches_creator_endpoint(self):
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        before = self.request("GET", "/api/performance/creators")[2]
        link = self.request("GET", "/api/public-stats/link")[2]
        video_id = "8100000000000000001"
        values = {"views": 98765, "likes": 0, "comments": 27, "shares": 3, "saves": None}
        status, _, result = self.request("POST", "/api/public-stats/push", {"videos": [{"id": video_id, **values}]},
            headers={"X-TKL-Key": link["key"], "Origin": "chrome-extension://abcdefghijklmnop"}, authenticated=False)
        self.assertEqual((status, result["saved"]), (200, 1))
        after = self.request("GET", "/api/performance/creators")[2]
        self.assertEqual({field: after["public_stats"][video_id][field] for field in values}, values)
        self.assertEqual(after["rows"], before["rows"])
        self.assertEqual(after["periods"], before["periods"])
        self.assertEqual(after["videos"], before["videos"])


class PerformanceCreatorBrowserTests(unittest.TestCase):
    setUp_ = perf.browser.WorkbenchBrowserTests.setUp
    setUpClass = classmethod(perf.browser.WorkbenchBrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(perf.browser.WorkbenchBrowserTests.tearDownClass.__func__)
    stop_server = perf.fixture.WorkbenchHTTPTests.stop_server
    request = perf.fixture.WorkbenchHTTPTests.request
    open_page = perf.browser.WorkbenchBrowserTests.open_page
    make_playable_video = perf.browser.WorkbenchBrowserTests.make_playable_video

    def setUp(self):
        self.setUp_()
        self.folder = self.root / "exports"
        perf.build_exports(self.folder)
        build_creator_exports(self.folder)
        self.server.app.performance.sources = [self.folder]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)

    def test_public_video_counts_refresh_in_top_videos_and_open_creator_without_changing_period_totals(self):
        page = self.open_page("#/all/videos?tab=creators")
        panel = page.locator("#perf-creators")
        panel.locator(".perf-kpis").wait_for()
        top = panel.locator(".perf-fold").filter(has_text="达人视频 Top 10")
        top.locator("summary").click()
        row = top.locator("tbody tr").filter(has=page.locator('[data-creator-open="alice"]'))
        read = "row => Object.fromEntries([...row.closest('table').querySelectorAll('thead th')].map((head,i) => [head.textContent.trim(),row.cells[i].textContent.trim()]))"
        before = row.evaluate(read)
        self.assertEqual((before["区间播放"], before["公开播放"], before["GPM"]), ("1,500", "—", "$86.67"))
        video_id = "8100000000000000001"
        values = {"views": 98765, "likes": 31, "comments": 0, "shares": None, "saves": 9}
        self.assertEqual(self.server.app.push_public_stats({"videos": [{"id": video_id, **values}]})["saved"], 1)
        page.evaluate("refresh(true)")
        page.wait_for_function("() => document.querySelector('[data-creator-open=alice]')?.closest('tr')?.cells[8]?.textContent === '98,765'")
        after = row.evaluate(read)
        self.assertEqual({key: after[key] for key in ("公开播放", "点赞", "评论", "转发", "收藏")},
                         {"公开播放": "98,765", "点赞": "31", "评论": "0", "转发": "—", "收藏": "9"})
        self.assertEqual({key: after[key] for key in ("区间播放", "订单", "归因 GMV", "GPM")},
                         {key: before[key] for key in ("区间播放", "订单", "归因 GMV", "GPM")})
        panel.locator('[data-creator-row="alice"] td.perf-title').click()
        drawer = page.locator("#perf-video-drawer[open]")
        drawer.get_by_role("heading", name="@alice").wait_for()
        video_row = drawer.locator(".perf-video-section").last.locator("tbody tr").first
        detail = video_row.evaluate(read)
        self.assertEqual((detail["区间播放"], detail["公开播放"], detail["点赞"], detail["评论"], detail["转发"], detail["收藏"]),
                         ("1,500", "98,765", "31", "0", "—", "9"))
        values.update(views=123456, likes=0, comments=None, shares=55, saves=0)
        self.assertEqual(self.server.app.push_public_stats({"videos": [{"id": video_id, **values}]})["saved"], 1)
        page.evaluate("refresh(true)")
        page.wait_for_function("() => document.querySelector('#perf-video-drawer[open] .perf-video-section:last-child tbody tr')?.cells[6]?.textContent === '123,456'")
        refreshed = video_row.evaluate(read)
        self.assertEqual((refreshed["区间播放"], refreshed["公开播放"], refreshed["点赞"], refreshed["评论"], refreshed["转发"], refreshed["收藏"]),
                         ("1,500", "123,456", "0", "—", "55", "0"))
        facts = drawer.locator(".perf-video-facts").inner_text()
        self.assertIn("$86.67", facts)
        self.assertIn("5.00%", facts)

    def test_own_public_videos_are_visible_for_195h_without_becoming_creators(self):
        ids = ("7679604579134197006", "7683152075529653518")
        perf.export(self.folder / "达人" / "9月自有账号.xlsx", "2026-09-01", "2026-09-30", [
            creator_row(video_id, "sharpalusa", 700, 10, 2000, 80, products="SHARPAL 195H Knife Sharpener(1732157836537861999)",
                        title=f"195H public example {index}") for index, video_id in enumerate(ids, 1)
        ], CREATOR_HEADER)
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)
        before = self.request("GET", "/api/performance/creators")[2]
        values = {"views": 23456, "likes": 45, "comments": 0, "shares": None, "saves": 6}
        result = self.server.app.push_public_stats({"videos": [{"id": video_id, **values} for video_id in (*ids, "8100000000000000002")]})
        self.assertEqual(result["saved"], 3)
        after = self.request("GET", "/api/performance/creators")[2]
        self.assertEqual(after["rows"], before["rows"])
        self.assertTrue(all(next(video[6] for video in after["videos"] if video[0] == video_id) == "own" for video_id in ids))
        page = self.open_page("#/195H/videos?tab=creators&period=2026-01-01~2026-01-31")
        own = page.locator(".perf-own-public")
        own.get_by_role("heading", name="自有账号公开数据").wait_for()
        self.assertEqual(own.locator("tbody tr").count(), 2)
        self.assertEqual(page.locator("[data-creator-row]").count(), 0)
        self.assertIn("不受报表区间筛选影响", own.inner_text())
        read = "row => Object.fromEntries([...row.closest('table').querySelectorAll('thead th')].map((head,i) => [head.textContent.trim(),row.cells[i].textContent.trim()]))"
        for video_id in ids:
            row = own.locator("tbody tr").filter(has=page.locator(f'a[href$="/video/{video_id}"]'))
            actual = row.evaluate(read)
            self.assertEqual({field: actual[field] for field in ("账号", "公开播放", "点赞", "评论", "转发", "收藏")},
                             {"账号": "@sharpalusa", "公开播放": "23,456", "点赞": "45", "评论": "0", "转发": "—", "收藏": "6"})
            self.assertNotEqual(actual["采集时间"], "—")
        page.goto(f"http://127.0.0.1:{self.server.server_port}/#/all/videos?tab=creators&period=all")
        page.locator("#perf-creators .perf-kpis").wait_for()
        self.assertEqual(page.locator("[data-creator-row]").count(), 2)
        overview = "data => PerfCreators.overview(data,{...PerfCreators.defaults,period:'all'}).total"
        self.assertEqual(page.evaluate(overview, after), page.evaluate(overview, before))
        self.assertEqual(page.evaluate(overview, after)["gmv"], 142.5)

    def test_creator_tab_ranks_creators_and_opens_one(self):
        page = self.open_page("#/all/videos?tab=creators")
        panel = page.locator("#perf-creators")
        panel.locator(".perf-kpis").wait_for()
        self.assertEqual(page.locator("#perf-tab-creators").get_attribute("aria-selected"), "true")
        self.assertEqual(page.locator("#perf-account").count(), 0)  # self-made account filter is not on this tab
        self.assertIn("$143", panel.locator(".perf-kpi").first.inner_text())  # alice 130 + carol 12.5; ours and unnamed left out
        self.assertEqual(panel.locator(".perf-week-data").first.locator("tbody tr").count(), 2)
        rows = panel.locator("[data-creator-row]")
        self.assertEqual(rows.all_inner_texts()[0].split("\n")[0].strip(), "@alice")
        self.assertEqual(rows.count(), 2)  # bob never sold, so the default "出单达人" leaves him out
        self.assertEqual(panel.locator("thead th", has_text="7月 GMV").count(), 1)

        page.locator("#perf-cshow").select_option("all")
        page.wait_for_function("() => document.querySelectorAll('[data-creator-row]').length === 3")
        self.assertTrue(page.locator("#perf-reset").is_visible())
        page.locator("#perf-date-trigger").click()
        while page.locator('[data-date="2026-08-01"]').count() == 0:
            page.locator('[data-nav="-1"]').first.click()
        page.locator('[data-date="2026-08-01"]').click()
        page.locator('[data-date="2026-08-31"]').click()
        page.wait_for_function("() => !document.querySelector('.perf-creator-table thead th[aria-sort] [data-creator-sort=p0]')")
        self.assertIn("2026-08-01 至 2026-08-31", panel.locator(".perf-kpi").first.inner_text())

        panel.locator('[data-creator-row="alice"] td.perf-title').click()
        drawer = page.locator("#perf-video-drawer[open]")
        drawer.get_by_role("heading", name="@alice").wait_for()
        self.assertIn("202H 等 2 款", drawer.inner_text())
        self.assertEqual(drawer.get_by_role("link", name="TikTok 主页").get_attribute("href"), "https://www.tiktok.com/@alice")
        drawer.get_by_role("button", name="关闭达人详情").click()

        page.locator("#perf-reset").click()
        page.wait_for_function("() => document.querySelectorAll('[data-creator-row]').length === 2")
        self.assertEqual(page.locator("#perf-date-trigger").inner_text().strip(), "全部周期")
        page.locator("#perf-tab-list").click()
        page.locator(".perf-table [data-video-row]").first.wait_for()
