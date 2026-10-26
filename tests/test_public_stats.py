"""Public page counts pushed by the TK 看板 extension: storage, pairing and where they show up."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import test_performance as exports
import test_server as http
from performance_store import PerformanceStore
from public_stats import COUNTS, PublicStatsError, PublicStatsStore
from sales_data import SalesDataResolver
from types import SimpleNamespace

LINKED = "7664986913497009439"  # the HTTP fixture case's video ID
NOW = datetime(2026, 10, 17, 8, 0, tzinfo=timezone.utc)


def record(video_id=LINKED, views=1100000, observed=NOW, **extra):
    return {"id": video_id, "author": "bushcraftedge", "views": views, "likes": 1780, "comments": 19, "shares": 615,
            "saves": 635, "title": "Sharpal 198H", "publishedAt": "2026-05-20T12:00:00.000Z",
            "observedAt": observed.timestamp() * 1000, **extra}


class PublicStatsStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="public-stats-")
        self.addCleanup(temp.cleanup)
        self.store = PublicStatsStore(Path(temp.name))

    def test_keeps_known_videos_only_and_folds_repeated_counts(self):
        known = lambda ids: {i for i in ids if i == LINKED}
        result = self.store.record([record(), record("7000000000000000001"), {"id": "x"}, {"id": LINKED}], known, now=NOW)
        self.assertEqual((result["saved"], result["ignored"], result["invalid"]), (1, 1, 2))
        later = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        again = self.store.record([record(observed=later)], known, now=later)
        self.assertEqual((again["saved"], again["unchanged"]), (0, 1))
        latest = self.store.latest([LINKED])[LINKED]
        self.assertEqual((latest["views"], latest["snapshot_count"]), (1100000, 1))
        self.assertEqual(latest["last_seen_at"], "2026-10-17T09:00:00Z")
        grown = self.store.record([record(views=1200000, observed=later)], known, now=later)
        self.assertEqual(grown["saved"], 1)
        self.assertEqual(self.store.latest([LINKED])[LINKED]["views"], 1200000)
        # An older page read never replaces a newer snapshot.
        stale = self.store.record([record(views=5, observed=NOW)], known, now=later)
        self.assertEqual((stale["saved"], self.store.latest([LINKED])[LINKED]["views"]), (0, 1200000))

    def test_missing_counts_stay_null_and_bad_values_are_rejected(self):
        self.store.record([record(views=None, saves=None)], lambda ids: set(ids), now=NOW)
        latest = self.store.latest([LINKED])[LINKED]
        self.assertIsNone(latest["views"])
        self.assertEqual(latest["likes"], 1780)
        result = self.store.record([record(views=-1), record(likes=True), record(views=None, likes=None, comments=None,
                                    shares=None, saves=None)], lambda ids: set(ids), now=NOW)
        self.assertEqual(result["invalid"], 3)
        with self.assertRaises(PublicStatsError):
            self.store.record([], lambda ids: set(ids))

    def test_older_observation_cannot_replace_or_rewind_folded_latest_counts(self):
        known = lambda ids: set(ids)
        newer = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
        stale = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        self.store.record([record()], known, now=NOW)
        self.store.record([record(observed=newer)], known, now=newer)
        folded = self.store.latest([LINKED])[LINKED]
        self.assertEqual((folded["observed_at"], folded["last_seen_at"]),
                         ("2026-10-17T08:00:00Z", "2026-10-17T10:00:00Z"))
        for old in (record(observed=stale, likes=7, shares=None), record(observed=stale)):
            result = self.store.record([old], known, now=newer)
            self.assertEqual((result["saved"], result["unchanged"]), (0, 1))
            self.assertEqual(self.store.latest([LINKED])[LINKED], folded)

    def test_pairing_key_is_stable_until_rotated(self):
        key = self.store.link_key()
        self.assertEqual(self.store.link_key(), key)
        self.assertTrue(self.store.check_key(key))
        self.assertFalse(self.store.check_key(""))
        rotated = self.store.rotate_key()
        self.assertNotEqual(rotated, key)
        self.assertFalse(self.store.check_key(key))

    def test_signature_reads_without_creating_data_and_detects_external_wal_writes(self):
        absent = self.store.signature()
        self.assertEqual(self.store.signature(), absent)
        self.assertFalse(self.store.database.parent.exists())
        self.store.record([record()], lambda ids: set(ids), now=NOW)
        connection = self.store.connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            before = self.store.signature()
            with connection:
                connection.execute("UPDATE snapshots SET views = ? WHERE video_id = ?", (1200000, LINKED))
            after = self.store.signature()
            self.assertEqual(before[0], after[0])  # the main database has not been checkpointed
            self.assertNotEqual(before, after)
            self.assertEqual(self.store.signature(), after)
        finally:
            connection.close()


class PublicViewsReplaceCumulativeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="public-views-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        exports.build_exports(root / "exports")
        self.performance = PerformanceStore(root / "runtime", [root / "exports"])
        self.performance.sync()
        self.public = PublicStatsStore(root / "runtime")

    def case(self, video_id, case_id):
        return {"case_id": case_id, "current": {"metrics": {"video_id": video_id}}}

    def test_every_read_of_a_video_uses_public_views_while_rates_and_status_keep_the_export(self):
        before = SalesDataResolver(self.performance).resolve_case(self.case(LINKED, "PVR-000000000001"))
        self.assertEqual(before["metrics"]["views_display"], "12000")
        shop = next(video for video in self.performance.videos() if video["video_id"] == LINKED)
        self.performance.public = self.public
        self.public.record([record()], lambda ids: set(ids), now=NOW)
        video = next(video for video in self.performance.videos() if video["video_id"] == LINKED)
        self.assertEqual((video["views"], video["views_shop"]), (1100000, 12000))
        self.assertEqual((video["ctr_pct"], video["gpm"], video["status"], video["window_views"]),
                         (shop["ctr_pct"], shop["gpm"], shop["status"], shop["window_views"]))
        self.assertEqual(self.performance.video(LINKED)["views"], 1100000)
        self.assertEqual(self.performance.video_brief([LINKED])[LINKED]["views"], 1100000)
        resolved = SalesDataResolver(self.performance).resolve_case(self.case(LINKED, "PVR-000000000001"))
        self.assertEqual(resolved["metrics"]["views_display"], "1100000")
        self.assertEqual(resolved["evidence"], before["evidence"])  # no added source note
        self.assertEqual(resolved["metrics"]["items_sold"], before["metrics"]["items_sold"])
        for actual in (video, self.performance.video(LINKED), self.performance.video_brief([LINKED])[LINKED], resolved["video"]):
            for field in COUNTS:
                self.assertEqual(actual[field], record()[field])
                self.assertEqual(actual[field + "_shop"], shop.get(field))
                self.assertEqual(actual[field + "_source"], "public")
                self.assertEqual(actual["public_stats"][field], record()[field])
            self.assertEqual(actual["public_stats"]["observed_at"], "2026-10-17T08:00:00Z")
            self.assertEqual(actual["public_stats"]["last_seen_at"], "2026-10-17T08:00:00Z")
        # Insights rates stay on the export's views.
        from case_insights import _ctr
        self.assertAlmostEqual(_ctr(video), _ctr(shop))
        # Without a pushed count, a video keeps its export figure; cached reads are not mutated.
        other = next(video for video in self.performance.videos() if video["video_id"] != LINKED)
        self.assertNotIn("views_shop", other)
        self.assertNotIn("public_stats", other)
        self.assertNotIn("likes_source", other)
        video["public_stats"]["likes"] = video["likes"] = -1
        self.assertEqual(self.performance.video(LINKED)["likes"], 1780)
        self.assertEqual(self.performance.video(LINKED)["public_stats"]["likes"], 1780)
        self.performance.public = None
        restored = next(v for v in self.performance.videos() if v["video_id"] == LINKED)
        self.assertEqual(restored, shop)

    def test_latest_public_interactions_preserve_zero_and_unknown_without_shop_fallback(self):
        shop = self.performance.video(LINKED)
        self.performance.public = self.public
        self.public.record([record()], lambda ids: set(ids), now=NOW)
        later = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
        pushed = record(views=None, observed=later, likes=None, comments=0, shares=None, saves=0)
        self.public.record([pushed], lambda ids: set(ids), now=later)
        readers = (next(v for v in self.performance.videos() if v["video_id"] == LINKED),
                   self.performance.video(LINKED), self.performance.video_brief([LINKED])[LINKED],
                   SalesDataResolver(self.performance).resolve_case(self.case(LINKED, "PVR-000000000001"))["video"])
        for actual in readers:
            self.assertEqual(actual["views"], 12000)  # existing missing-public-views behavior
            self.assertNotIn("views_source", actual)
            for field in COUNTS[1:]:
                self.assertEqual(actual[field], pushed[field])
                self.assertEqual(actual[field + "_shop"], shop.get(field))
                self.assertEqual(actual[field + "_source"], "public")
            self.assertEqual(actual["public_stats"], {**{field: pushed[field] for field in COUNTS},
                "observed_at": "2026-10-17T09:00:00Z", "last_seen_at": "2026-10-17T09:00:00Z"})

    def test_excel_export_puts_export_and_public_views_side_by_side(self):
        import openpyxl
        self.performance.public = self.public
        self.public.record([record(likes=1780, comments=0, shares=None, saves=635)], lambda ids: set(ids), now=NOW)
        folder = self.performance.database.parent / "public-exports"
        def annotate(videos):
            return [{**video, "review_status": {"key": "weak" if video["video_id"] == LINKED else "pending",
                "label": "弱参考价值" if video["video_id"] == LINKED else "待复盘"}} for video in videos]
        bench = SimpleNamespace(performance=self.performance, public_stats=self.public, public_exports=folder,
                                annotate_review_status=annotate)
        ids = [video["video_id"] for video in self.performance.videos()]
        result = http.SERVER.Workbench.export_public_views(bench, {"video_ids": ids})
        self.assertEqual((result["rows"], result["with_public"]), (len(ids), 1))
        self.assertRegex(result["download_url"], r"^/api/performance/public-export/\d{8}-\d{6}-[0-9a-f]{6}$")
        sheet = openpyxl.load_workbook(result["path"]).worksheets[0]
        header = [cell.value for cell in sheet[1]]
        rows = {row[0]: dict(zip(header, row)) for row in sheet.iter_rows(min_row=2, values_only=True)}
        self.assertEqual((rows[LINKED]["后台累计播放"], rows[LINKED]["公开页播放"]), (12000, 1100000))
        snapshot = self.public.latest([LINKED])[LINKED]
        for column, field in (("公开页点赞", "likes"), ("公开页评论", "comments"), ("公开页转发", "shares"), ("公开页收藏", "saves")):
            self.assertEqual(rows[LINKED][column], snapshot[field])
        self.assertNotIn("跟进状态", header)
        self.assertEqual(rows[LINKED]["参考价值"], "弱参考价值")
        others = [row for video_id, row in rows.items() if video_id != LINKED]
        self.assertTrue(others and all(row["公开页播放"] is None for row in others))
        self.assertTrue(all(row["参考价值"] == "待复盘" for row in others))
        self.assertNotIn("public-exports", str(self.performance.sources[0]))
        for body in ({"video_ids": []}, {"video_ids": ["abc"]}, {"video_ids": ids, "x": 1}):
            with self.assertRaises(http.SERVER.ApiError):
                http.SERVER.Workbench.export_public_views(bench, body)


class PublicStatsHTTPTests(unittest.TestCase):
    setUp = http.WorkbenchHTTPTests.setUp
    stop_server = http.WorkbenchHTTPTests.stop_server
    request = http.WorkbenchHTTPTests.request

    def push(self, body, key=None, origin="chrome-extension://abcdefghijklmnop"):
        headers = {"X-TKL-Key": self.key if key is None else key}
        if origin:
            headers["Origin"] = origin
        return self.request("POST", "/api/public-stats/push", body, headers=headers, authenticated=False)

    def test_extension_push_requires_origin_and_key_and_reaches_case_detail(self):
        status, _, link = self.request("GET", "/api/public-stats/link")
        self.assertEqual(status, 200)
        self.key = link["key"]
        self.assertEqual(link["address"], f"http://127.0.0.1:{self.server.server_port}")
        self.assertEqual(self.push({"videos": [record()]}, key="wrong")[0], 403)
        self.assertEqual(self.push({"videos": [record()]}, origin="https://www.tiktok.com")[0], 403)
        self.assertEqual(self.push({"videos": [record()]}, origin="")[0], 403)
        status, _, ping = self.request("POST", "/api/public-stats/ping", {}, headers={
            "X-TKL-Key": self.key, "Origin": "chrome-extension://abcdefghijklmnop"}, authenticated=False)
        self.assertEqual((status, ping["ok"]), (200, True))
        status, _, result = self.push({"videos": [record(), record("7000000000000000001")], "extension_version": "0.4.8"})
        self.assertEqual(status, 200, result)
        self.assertEqual((result["saved"], result["ignored"]), (1, 1))
        self.assertEqual(self.push({"videos": [record()], "cookies": "x"})[0], 400)
        # Rotating the key from the workbench page invalidates the extension's old one.
        status, _, rotated = self.request("POST", "/api/public-stats/link/rotate", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.push({"videos": [record()]})[0], 403)
        self.key = rotated["key"]
        self.assertEqual(self.push({"videos": [record()]})[0], 200)

    def prepare_sales_and_public_link(self):
        folder = self.root / "exports"
        exports.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.server.app.performance.sync()
        self.key = self.request("GET", "/api/public-stats/link")[2]["key"]

    def test_push_refreshes_cached_case_state_and_polling_digest(self):
        self.prepare_sales_and_public_link()
        before = self.request("GET", "/api/state")[2]
        self.assertEqual(before["cases"][0]["sales_data"]["metrics"]["views_display"], "12000")
        status, _, pushed = self.push({"videos": [record(views=987654)]})
        self.assertEqual((status, pushed["saved"]), (200, 1))
        status, _, changed = self.request("GET", "/api/state?since=" + before["digest"])
        self.assertEqual(status, 200)
        self.assertNotIn("unchanged", changed)
        self.assertNotEqual(changed["public_stats_revision"], before["public_stats_revision"])
        self.assertNotEqual(changed["signature"], before["signature"])
        case = next(item for item in changed["cases"] if item["case_id"] == self.identifier)
        detail = self.request("GET", "/api/cases/" + self.identifier)[2]
        self.assertEqual(case["sales_data"]["metrics"]["views_display"], "987654")
        self.assertEqual(case["sales_data"], detail["sales_data"])
        self.assertEqual(case["sales_data"]["video"]["views_shop"], 12000)
        listed = next(item for item in self.request("GET", "/api/performance/videos")[2]["videos"] if item["video_id"] == LINKED)
        full_video = self.request("GET", "/api/performance/videos/" + LINKED)[2]["video"]
        expected = record(views=987654)
        for actual in (listed, full_video, case["sales_data"]["video"], detail["sales_data"]["video"]):
            self.assertEqual({field: actual[field] for field in COUNTS}, {field: expected[field] for field in COUNTS})
            self.assertEqual(actual["public_stats"], listed["public_stats"])
        self.assertEqual(case["revision"], before["cases"][0]["revision"])
        same = self.request("GET", "/api/state?since=" + changed["digest"])[2]
        self.assertIs(same["unchanged"], True)

    def test_push_for_video_without_case_still_notifies_state_pollers(self):
        self.prepare_sales_and_public_link()
        before = self.request("GET", "/api/state")[2]
        video_id = "7600000000000000002"
        status, _, pushed = self.push({"videos": [record(video_id, views=54321)]})
        self.assertEqual((status, pushed["saved"]), (200, 1))
        status, _, changed = self.request("GET", "/api/state?since=" + before["digest"])
        self.assertEqual(status, 200)
        self.assertNotIn("unchanged", changed)
        self.assertNotEqual(changed["public_stats_revision"], before["public_stats_revision"])
        self.assertNotEqual(changed["digest"], before["digest"])
        self.assertEqual(changed["cases"], before["cases"])
        video = self.request("GET", "/api/performance/videos/" + video_id)[2]["video"]
        self.assertEqual((video["views"], video["views_shop"]), (54321, 800))
        same = self.request("GET", "/api/state?since=" + changed["digest"])[2]
        self.assertIs(same["unchanged"], True)

    def test_extension_reads_products_and_a_product_brief_with_its_key(self):
        products = self.skill / "references" / "products"
        products.mkdir(parents=True, exist_ok=True)
        (products / "202H-selling-point-grading.md").write_text(
            "# 202H\n\n| 编号 | 卖点 | 为什么是这一级 | 依据状态 | 状态 |\n|---|---|---|---|---|\n"
            "| S8 | 贵刀风险控制 | 对不放心把好刀交给拉槽的人 | 已核 | 在测 |\n\n## 每个 S 各带一套组合\n\n"
            "| 测试方向 | 目标需求与比较对象 | A/B 组合及顾虑链 | 核心证明 |\n|---|---|---|---|\n"
            "| 202H-S8 贵刀风险控制 | 有贵价厨刀的人 | 不敢磨 → S8 → B4 价格 | 装夹与上色 |\n"
            "| 202H-S6 价位（已并入 B4，暂停） | 比价的人 | S6 → B4 | 配置 |\n\n## 分级依据\n\n| a | b | c | d |\n", encoding="utf-8")
        status, _, link = self.request("GET", "/api/public-stats/link")
        extension = {"X-TKL-Key": link["key"], "Origin": "chrome-extension://abcdefghijklmnop"}
        ask = lambda path, body, headers=extension: self.request("POST", path, body, headers=headers, authenticated=False)
        self.assertEqual(ask("/api/public-stats/products", {}, {**extension, "X-TKL-Key": "wrong"})[0], 403)
        self.assertEqual(ask("/api/public-stats/products", {}, {"X-TKL-Key": link["key"]})[0], 403)
        status, _, listed = ask("/api/public-stats/products", {})
        self.assertEqual(status, 200, listed)
        self.assertIn("202H", listed["products"])
        status, _, brief = ask("/api/public-stats/product-brief", {"product": "202H"})
        self.assertEqual(status, 200, brief)
        self.assertEqual(brief["product"], "202H")
        self.assertEqual(brief["cases"][0]["title"], "完整系统价值")
        self.assertEqual(brief["cases"][0]["opening"], "The whole setup in one case.")
        self.assertEqual([(p["code"], p["rationale"]) for p in brief["selling_points"]], [("S8", "对不放心把好刀交给拉槽的人")])
        self.assertEqual(brief["combos"], [{"direction": "202H-S8 贵刀风险控制", "audience": "有贵价厨刀的人",
                                            "chain": "不敢磨 → S8 → B4 价格", "proof": "装夹与上色"}])
        self.assertNotIn("assets", json.dumps(brief, ensure_ascii=False))
        self.assertEqual(ask("/api/public-stats/product-brief", {"product": "999Z"})[0], 404)
        self.assertEqual(ask("/api/public-stats/product-brief", {"product": "202H", "x": 1})[0], 400)
        self.assertEqual(ask("/api/public-stats/products", {"x": 1})[0], 400)

    def test_export_download_rejects_unknown_files(self):
        self.assertEqual(self.request("GET", "/api/performance/public-export/20261017-120000-abcdef")[0], 404)
        self.assertEqual(self.request("POST", "/api/performance/public-export", {"video_ids": ["x"]})[0], 400)

    def test_rotate_still_needs_the_page_token(self):
        status, _, _ = self.request("POST", "/api/public-stats/link/rotate", {}, authenticated=False)
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/api/public-stats/link/rotate", {}, headers={"Origin": "chrome-extension://abc"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
