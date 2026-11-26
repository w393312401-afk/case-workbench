"""反面素材库: picks bad videos from the sales rows by thresholds a person can change, and keeps their notes."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import test_feedback_chain as feedback
import test_server as fixture
from creation_store import CreationError
from negative_library import DEFAULTS, NegativeLibrary, build


def video(video_id, posted, views, orders, product="202H", clicks=None, shop="local", products="SHARPAL 202H(1729000000001)"):
    clicks = clicks if clicks is not None else (round(views * 0.017) if views else None)
    return {"video_id": video_id, "shop": shop, "account": "sharpalusa", "title": f"T{video_id} #tag", "headline": f"T{video_id}",
            "posted_on": posted, "posted_at": posted, "products": products, "product": product, "views": views, "orders": orders,
            "gmv": None, "clicks": clicks, "last_data": "2026-11-01",
            "ctr_pct": round(clicks * 100 / views, 2) if views and clicks is not None else None,
            "ctor_pct": round(orders * 100 / clicks, 2) if clicks and orders is not None else None}


class BuildTests(unittest.TestCase):
    def data(self, **settings):
        return {"settings": {**DEFAULTS, **settings}, "notes": {}, "manual": []}

    def test_thresholds_pick_low_and_weak_and_skip_young_or_old_videos(self):
        rows = [
            video("1000000000000000001", "2026-10-01", 900, 0),        # low
            video("1000000000000000002", "2026-10-01", 6000, 2),       # weak: few orders
            video("1000000000000000003", "2026-10-01", 6000, 9),       # sells fine
            video("1000000000000000004", "2026-10-01", 40000, 3),      # weak: under 1 per 10k views
            video("1000000000000000005", "2026-10-25", 300, 0),        # too new to judge
            video("1000000000000000006", "2026-03-01", 300, 0),        # older than the window
            video("1000000000000000007", "2026-10-01", 3000, 0),       # in between: neither
            video("1000000000000000008", "2026-10-01", 6000, None),    # no order data: not weak
        ]
        result = build(rows, self.data())
        self.assertEqual([r["video_id"][-1] for r in result["low"]], ["1"])
        self.assertEqual(sorted(r["video_id"][-1] for r in result["weak"]), ["2", "4"])
        self.assertEqual(result["counts"]["too_new"], 1)
        self.assertEqual(result["counts"]["older"], 1)
        self.assertEqual(result["data_until"], "2026-11-01")
        wider = build(rows, self.data(recent_days=0, min_age_days=0))
        self.assertEqual(sorted(r["video_id"][-1] for r in wider["low"]), ["1", "5", "6"])
        stricter = build(rows, self.data(max_orders=0, orders_per_10k=0))
        self.assertEqual(stricter["weak"], [])

    def test_shop_filter_and_suspected_reasons(self):
        rows = [video(f"20000000000000000{i:02d}", "2026-10-01", 6000, 1, clicks=120) for i in range(5)]
        rows.append(video("2000000000000000099", "2026-10-01", 6000, 1, clicks=30))   # few clicks
        rows.append(video("2000000000000000098", "2026-10-01", 1000, 0, shop="cross"))
        result = build(rows, self.data(), "local")
        by_id = {r["video_id"]: r for r in result["weak"]}
        self.assertIn("很少点商品", by_id["2000000000000000099"]["auto_reason"])
        self.assertEqual(result["low"], [])
        self.assertEqual(len(build(rows, self.data(), "cross")["low"]), 1)
        unlinked = build([video("2000000000000000097", "2026-10-01", 6000, 0, products="")], self.data())
        self.assertEqual(unlinked["weak"][0]["auto_reason"], "没挂商品。")
        low = build([video("2000000000000000096", "2026-10-01", 500, 0), video("2000000000000000095", "2026-10-01", 9000, 50)], self.data())
        self.assertIn("明显偏低", low["low"][0]["auto_reason"])


class LibraryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="negative-test-")
        self.addCleanup(temporary.cleanup)
        self.library = NegativeLibrary(Path(temporary.name))
        self.rows = [video("3000000000000000001", "2026-10-01", 900, 0), video("3000000000000000002", "2026-10-01", 3000, 1),
                     video("3000000000000000003", "2026-01-01", 3000, 0)]

    def test_notes_hide_and_pin(self):
        self.library.mutate({"action": "note", "video_id": "3000000000000000001", "reason": "开头太慢"})
        result = self.library.public(self.rows)
        self.assertEqual(result["low"][0]["reason"], "开头太慢")
        self.library.mutate({"action": "note", "video_id": "3000000000000000001", "hidden": True})
        result = self.library.public(self.rows)
        self.assertEqual(result["low"], [])
        self.assertEqual([r["reason"] for r in result["hidden"]], ["开头太慢"])
        self.library.mutate({"action": "note", "video_id": "3000000000000000003", "pinned": "weak"})
        self.assertEqual([r["source"] for r in self.library.public(self.rows)["weak"]], ["pinned"], "A pin shows even outside the date window")
        self.library.mutate({"action": "note", "video_id": "3000000000000000003", "pinned": None})
        self.assertEqual(self.library.public(self.rows)["weak"], [])
        with self.assertRaises(CreationError):
            self.library.mutate({"action": "note", "video_id": "abc"})

    def test_settings_are_checked_and_saved(self):
        self.library.mutate({"action": "settings", "settings": {**DEFAULTS, "low_views": "1000"}})
        self.assertEqual(self.library.public(self.rows)["settings"]["low_views"], 1000)
        self.assertEqual(self.library.public(self.rows)["low"][0]["video_id"], "3000000000000000001")
        for bad in ({"low_views": -1}, {"low_views": 9000, "high_views": 5000}, {"recent_days": "x"}):
            with self.assertRaises(CreationError):
                self.library.mutate({"action": "settings", "settings": {**DEFAULTS, **bad}})
        self.library.mutate({"action": "reset_settings"})
        self.assertEqual(self.library.public(self.rows)["settings"], DEFAULTS)

    def test_manual_entries_and_known_videos_become_pins(self):
        saved = self.library.mutate({"action": "add_manual", "entry": {"kind": "weak", "title": "别的店的视频", "views": "12,000", "orders": "0", "posted_on": "2026-09-01"}})
        entry = saved["manual"][0]
        self.assertEqual((entry["views"], entry["orders"]), (12000, 0))
        self.library.mutate({"action": "update_manual", "id": entry["id"], "entry": {**entry, "reason": "人群不对"}})
        self.assertEqual(self.library.public(self.rows)["manual"][0]["reason"], "人群不对")
        self.library.mutate({"action": "add_manual", "entry": {"kind": "weak", "link": "https://www.tiktok.com/@x/video/3000000000000000002", "reason": "价格"}},
                            known=lambda ids: set(ids) & {"3000000000000000002"})
        result = self.library.public(self.rows)
        self.assertEqual(len(result["manual"]), 1)
        self.assertEqual([(r["video_id"], r["reason"]) for r in result["weak"]], [("3000000000000000002", "价格")])
        self.library.mutate({"action": "delete_manual", "id": entry["id"]})
        self.assertEqual(self.library.public(self.rows)["manual"], [])
        for bad in ({"kind": "x", "title": "a"}, {"kind": "low"}, {"kind": "low", "title": "a", "views": "-3"}, {"kind": "low", "title": "a", "posted_on": "11/01"}):
            with self.assertRaises(CreationError):
                self.library.mutate({"action": "add_manual", "entry": bad})


class NegativeLibraryHTTPTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    ok = feedback.FeedbackChainHTTPTests.ok
    no_download = staticmethod(feedback.FeedbackChainHTTPTests.no_download)

    def setUp(self):
        feedback.FeedbackChainHTTPTests.setUp(self)

    def test_http_round_trip(self):
        status, _, _ = self.request("POST", "/api/negative-library", {"action": "reset_settings"}, authenticated=False)
        self.assertEqual(status, 403)
        listing = self.ok("GET", "/api/negative-library?shop=local")
        self.assertEqual(listing["settings"], DEFAULTS)
        self.assertEqual(self.request("GET", "/api/negative-library?shop=nowhere")[0], 400)
        saved = self.ok("POST", "/api/negative-library", {"action": "settings", "shop": "local", "settings": {**DEFAULTS, "recent_days": 0, "min_age_days": 0, "low_views": 10000000, "high_views": 100000000}})
        self.assertEqual(saved["settings"]["recent_days"], 0)
        self.assertIn(self.video_id, [row["video_id"] for row in saved["low"]])
        noted = self.ok("POST", "/api/negative-library", {"action": "note", "shop": "local", "video_id": self.video_id, "reason": "测试原因"})
        self.assertEqual(next(r for r in noted["low"] if r["video_id"] == self.video_id)["reason"], "测试原因")
        self.assertTrue((self.server.app.runtime / "performance" / "negative-library.json").is_file())


if __name__ == "__main__":
    unittest.main()
