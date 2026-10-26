"""The campaign calendar: honest about how well each date is backed, editable, and visible to the review."""
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_feedback_chain as feedback
import test_performance as performance
import test_server as fixture
from campaign_impact import product_weekly, weekly_impact
from campaign_store import SEED, CampaignStore
from creation_store import CreationError


class CampaignStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="campaigns-test-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.store = CampaignStore(self.runtime)

    def by_id(self, result, identifier):
        return next((row for row in result["campaigns"] if row["id"] == identifier), None)

    def test_builtin_2026_nodes_say_how_well_their_dates_are_backed(self):
        result = self.store.public()
        rows = {row["id"]: row for row in result["campaigns"]}
        self.assertEqual(len(rows), len(SEED))
        self.assertEqual(result["campaigns"], sorted(result["campaigns"], key=lambda row: (row["start"], row["end"], row["id"])))
        bfcm = rows["TKC-2026-bfcm"]
        self.assertEqual((bfcm["start"], bfcm["end"], bfcm["confidence"]), ("2026-11-11", "2026-11-30", "official"))
        self.assertTrue(bfcm["source_url"].startswith("https://seller-us.tiktok.com/"))
        fall = rows["TKC-2026-fall-deals"]
        self.assertEqual((fall["confidence"], fall["start"], fall["end"]), ("disputed", "2026-10-04", "2026-10-18"))
        self.assertIn("10/2–10/11", fall["note"], "The dissenting source stays on the record")
        self.assertIn("与官方指南一致", rows["TKC-2026-bfcm"]["note"])
        # Every built-in node says what to check; a person's own entry carries no checklist text.
        self.assertTrue(all(row["check"] for row in rows.values()))
        # Nothing is presented as official without an official page behind it.
        self.assertEqual({row["id"] for row in rows.values() if row["confidence"] == "official"}, {"TKC-2026-bfcm"})
        self.assertTrue(all(row["builtin"] and not row["edited"] and not row["verified"] for row in rows.values()))

    def test_overlap_counts_touching_days_and_ignores_unknown_ranges(self):
        names = lambda start, end: [row["id"] for row in self.store.overlapping(start, end)]
        self.assertEqual(names("2026-10-18", "2026-10-18"), ["TKC-2026-fall-deals"])
        self.assertEqual(names("2026-10-19", "2026-10-31"), [])
        self.assertEqual(names("2026-10-09", "2026-11-11"), ["TKC-2026-fall-deals", "TKC-2026-bfcm"])
        self.assertEqual(names("2026-07-02", "2026-07-22"), ["TKC-2026-deals-for-you-days", "TKC-2026-summer-sale"])
        self.assertEqual((self.store.overlapping(None, "2026-10-11"), self.store.overlapping("2026-10-11", "")), ([], []))

    def test_edit_delete_restore_and_custom_entries_keep_built_in_ids_stable(self):
        revision = self.store.public()["revision"]
        saved = self.store.mutate({"action": "save", "revision": revision, "campaign": {
            "id": "TKC-2026-fall-deals", "name": "Fall Deals For You（秋促）", "start": "2026-10-04", "end": "2026-10-18",
            "confidence": "official", "source_label": "Seller Center 活动卡", "source_url": "", "note": "已核对", "verified": True}})
        row = self.by_id(saved, "TKC-2026-fall-deals")
        self.assertEqual((row["start"], row["end"], row["edited"], row["builtin"], row["verified"]), ("2026-10-04", "2026-10-18", True, True, True))
        custom = self.store.mutate({"action": "save", "revision": saved["revision"], "campaign": {
            "name": "品牌自己的闪促", "start": "2026-09-01", "end": "2026-09-03", "confidence": "manual"}})
        mine = next(row for row in custom["campaigns"] if not row["builtin"])
        self.assertRegex(mine["id"], r"^TKC-[0-9a-f]{12}$")
        removed = self.store.mutate({"action": "delete", "revision": custom["revision"], "id": "TKC-2026-bfcm"})
        self.assertIsNone(self.by_id(removed, "TKC-2026-bfcm"))
        self.assertIsNone(self.by_id(CampaignStore(self.runtime).public(), "TKC-2026-bfcm"), "A deleted built-in stays deleted after a restart")
        restored = self.store.mutate({"action": "restore", "revision": removed["revision"], "id": "TKC-2026-bfcm"})
        self.assertEqual(self.by_id(restored, "TKC-2026-bfcm")["confidence"], "official")
        reset = self.store.mutate({"action": "restore", "revision": restored["revision"], "id": "TKC-2026-fall-deals"})
        self.assertEqual((self.by_id(reset, "TKC-2026-fall-deals")["start"], self.by_id(reset, "TKC-2026-fall-deals")["edited"]), ("2026-10-04", False))
        gone = self.store.mutate({"action": "delete", "revision": reset["revision"], "id": mine["id"]})
        self.assertIsNone(self.by_id(gone, mine["id"]))

    def test_checklist_text_survives_edits_and_is_never_saved_with_them(self):
        revision = self.store.public()["revision"]
        saved = self.store.mutate({"action": "save", "revision": revision, "campaign": {
            "id": "TKC-2026-bfcm", "name": "BFCM", "start": "2026-11-11", "end": "2026-12-01", "confidence": "official", "verified": True}})
        self.assertIn("11/30", self.by_id(saved, "TKC-2026-bfcm")["check"])
        self.assertNotIn("check", self.store.read()["records"]["TKC-2026-bfcm"])
        custom = self.store.mutate({"action": "save", "revision": saved["revision"], "campaign": {
            "name": "我的活动", "start": "2026-09-01", "end": "2026-09-02", "confidence": "manual"}})
        self.assertEqual(next(row for row in custom["campaigns"] if not row["builtin"])["check"], "")
        with self.assertRaises(CreationError):
            self.store.mutate({"action": "save", "revision": custom["revision"], "campaign": {
                "name": "x", "start": "2026-09-01", "end": "2026-09-02", "check": "偷偷写入"}})

    def test_every_built_in_name_has_a_chinese_name_and_old_saved_names_follow_it(self):
        import re
        self.assertTrue(all(re.search(r"[\u4e00-\u9fff]", row["name"]) for row in self.store.public()["campaigns"]))
        revision = self.store.public()["revision"]
        # A confirmation saved while the name was English-only still shows the Chinese name...
        saved = self.store.mutate({"action": "save", "revision": revision, "campaign": {
            "id": "TKC-2026-summer-sale", "name": "Summer Sale", "start": "2026-07-23", "end": "2026-08-02",
            "confidence": "planning", "verified": True}})
        row = self.by_id(saved, "TKC-2026-summer-sale")
        self.assertEqual((row["name"], row["start"], row["verified"]), ("Summer Sale（夏季大促）", "2026-07-23", True))
        self.assertEqual(self.store.read()["records"]["TKC-2026-summer-sale"]["name"], "Summer Sale", "Only the display follows; the saved file is untouched")
        # ...while a name the person wrote themselves is kept.
        mine = self.store.mutate({"action": "save", "revision": saved["revision"], "campaign": {
            "id": "TKC-2026-back-to-school", "name": "返校季（我改的）", "start": "2026-08-13", "end": "2026-08-23", "confidence": "planning"}})
        self.assertEqual(self.by_id(mine, "TKC-2026-back-to-school")["name"], "返校季（我改的）")

    def test_stale_revision_and_bad_input_are_refused_without_writing(self):
        revision = self.store.public()["revision"]
        good = {"name": "测试活动", "start": "2026-09-01", "end": "2026-09-03", "confidence": "manual"}
        self.store.mutate({"action": "save", "revision": revision, "campaign": good})
        with self.assertRaises(CreationError) as raised:
            self.store.mutate({"action": "save", "revision": revision, "campaign": {**good, "name": "另一台设备"}})
        self.assertEqual(raised.exception.status, 409)
        current = self.store.public()["revision"]
        for bad in ({**good, "end": "2026-08-31"}, {**good, "start": "2026-9-1"}, {**good, "name": "  "},
                    {**good, "confidence": "certain"}, {**good, "source_url": "javascript:alert(1)"},
                    {**good, "id": "bad id"}, {**good, "extra": 1}, {**good, "end": "2027-03-01"}):
            with self.subTest(bad=bad), self.assertRaises(CreationError) as raised:
                self.store.mutate({"action": "save", "revision": current, "campaign": bad})
            self.assertEqual(raised.exception.status, 400)
        with self.assertRaises(CreationError):
            self.store.mutate({"action": "restore", "revision": current, "id": "TKC-abcdef123456"})
        self.assertEqual(self.store.public()["revision"], current)

    def test_unreadable_file_is_reported_not_replaced(self):
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.store.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(CreationError) as raised:
            self.store.public()
        self.assertEqual(raised.exception.status, 500)
        self.assertEqual(self.store.path.read_text(encoding="utf-8"), "{not json")


class CampaignReviewTests(unittest.TestCase):
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request
    ok = feedback.FeedbackChainHTTPTests.ok
    chain = feedback.FeedbackChainHTTPTests.chain
    no_download = staticmethod(feedback.FeedbackChainHTTPTests.no_download)

    def setUp(self):
        feedback.FeedbackChainHTTPTests.setUp(self)
        self.video = self.server.app.performance.video(self.video_id)

    def test_http_is_authenticated_and_round_trips(self):
        status, _, denied = self.request("POST", "/api/campaigns", {"action": "delete", "id": "TKC-2026-bfcm", "revision": "x"}, authenticated=False)
        self.assertEqual(status, 403, denied)
        listing = self.ok("GET", "/api/campaigns")
        self.assertEqual(listing["timezone"], "America/Los_Angeles")
        self.assertEqual(self.request("GET", "/api/campaigns?x=1")[0], 400)
        saved = self.ok("POST", "/api/campaigns", {"action": "save", "revision": listing["revision"], "campaign": {
            "name": "测试促销", "start": "2026-09-01", "end": "2026-09-02", "confidence": "manual"}})
        self.assertEqual(self.ok("GET", "/api/campaigns"), saved)
        status, _, conflict = self.request("POST", "/api/campaigns", {"action": "save", "revision": listing["revision"], "campaign": {
            "name": "旧版本", "start": "2026-09-01", "end": "2026-09-02"}})
        self.assertEqual(status, 409, conflict)

    def add_campaign(self, start, end, **values):
        revision = self.ok("GET", "/api/campaigns")["revision"]
        return self.ok("POST", "/api/campaigns", {"action": "save", "revision": revision, "campaign": {
            "name": "窗口内活动", "start": start, "end": end, "confidence": "planning", **values}})

    def test_review_lists_campaigns_that_touch_the_observation_window(self):
        posted, window_end = self.video["posted_on"], self.video["window_end"]
        before = self.chain()
        self.assertEqual(before["video"]["posted_on"], posted)
        self.assertEqual([row["id"] for row in before["campaigns"]], [row["id"] for row in self.server.app.campaigns.overlapping(posted, window_end)])
        self.add_campaign(window_end, window_end)
        after = self.chain()
        self.assertEqual([row["name"] for row in after["campaigns"] if row["name"] == "窗口内活动"], ["窗口内活动"])
        self.add_campaign("2000-01-01", "2000-01-05", name="很久以前")
        self.assertNotIn("很久以前", [row["name"] for row in self.chain()["campaigns"]])

    def test_ai_draft_brief_names_overlapping_campaigns_and_how_far_to_trust_them(self):
        app = self.server.app
        context = {"review_video": {**self.video, "product": "202H"}, "review_summary": {"status_label": "观察"}}
        self.add_campaign(self.video["posted_on"], self.video["window_end"], name="待核对的大促", confidence="planning")
        figures = app._draft_figures(context, self.video_id)
        brief = figures["观察窗内的平台活动"]
        self.assertTrue(any("待核对的大促" in line and "日期待核对" in line for line in brief["活动"]), brief)
        self.assertIn("高客单", brief["判读要求"])
        self.assertIn("不能当成事实", brief["判读要求"])
        # A window no campaign touches adds nothing to the brief.
        for row in app.campaigns.public()["campaigns"]:
            revision = app.campaigns.public()["revision"]
            app.campaigns.mutate({"action": "delete", "revision": revision, "id": row["id"]})
        self.assertNotIn("观察窗内的平台活动", app._draft_figures(context, self.video_id))


# The same figures the browser test feeds to Campaigns.weeklyImpact in test_campaigns_ui.py; both must agree.
MONDAYS = ["2030-01-14", "2030-01-21", "2030-01-28", "2030-02-04", "2030-02-11", "2030-02-18", "2030-02-25", "2030-03-04", "2030-03-11"]
GMV = {"2030-02-18": 7777, "2030-02-25": 9999, "2030-03-04": 300, "2030-03-11": 300}
BIG = {"id": "big", "start": "2030-03-04", "end": "2030-03-17"}
SMALL = {"id": "small", "start": "2030-02-18", "end": "2030-02-19"}


def synthetic(product_a_only=True):
    totals = {}
    for week in MONDAYS:
        entry = {"gmv": GMV.get(week, 100), "orders": 2, "items": 2}
        if not product_a_only:
            entry = {"gmv": entry["gmv"] + 1000, "orders": entry["orders"] + 50, "items": entry["items"] + 50}
        totals[week] = entry
    return totals, {week for week in MONDAYS if week != "2030-02-25"}


class WeeklyImpactTests(unittest.TestCase):
    def test_matches_the_browser_on_the_same_figures(self):
        totals, complete = synthetic()
        result = weekly_impact(BIG, [BIG, SMALL], totals, complete)
        self.assertEqual((result["gmv"], result["orders"], result["weeks"], result["baseline"]), ([300, 100], [2, 2], 2, 4),
                         "the gap week (02-25) and the week inside another campaign (02-18) stay out of the baseline")
        everyone = weekly_impact(BIG, [BIG, SMALL], *synthetic(False))
        self.assertEqual(everyone["gmv"], [1300, 1100])
        short = weekly_impact(BIG, [BIG, SMALL], totals, {week for week in complete if week >= "2030-02-18"})
        self.assertEqual(short["missing"], "baseline")
        self.assertEqual(weekly_impact(SMALL, [BIG, SMALL], totals, complete), {"missing": "weeks"})

    def test_product_weekly_counts_only_that_products_videos_and_whole_weeks(self):
        analytics = {"videos": [["A"], ["B"]], "weekly": [[0, "2030-01-14", 0, 0, 0, 2, 3, 100.5], [1, "2030-01-14", 0, 0, 0, 50, 50, 1000],
                                                           [0, "2030-01-21", 0, 0, 0, None, None, None]]}
        videos = [{"video_id": "A", "product": "202h"}, {"video_id": "B", "product": "103N"}]
        weeks = [{"week": "2030-01-14", "complete": True}, {"week": "2030-01-21", "complete": False}]
        totals, complete = product_weekly(analytics, videos, weeks, "202H")
        self.assertEqual(totals, {"2030-01-14": {"gmv": 100.5, "orders": 2, "items": 3}, "2030-01-21": {"gmv": 0, "orders": 0, "items": 0}})
        self.assertEqual(complete, {"2030-01-14"})
        self.assertEqual(product_weekly(analytics, videos, weeks)[0]["2030-01-14"]["gmv"], 1100.5)


class CampaignBriefTests(CampaignReviewTests):
    def test_brief_carries_the_products_multiplier_and_says_when_it_cannot_compare(self):
        app = self.server.app
        video = {"video_id": "X", "product": "202H", "shop": "local", "posted_on": "2030-03-05", "window_end": "2030-03-17"}
        for name, start, end in (("测试大促", "2030-03-04", "2030-03-17"), ("测试小促", "2030-02-18", "2030-02-19")):
            app.campaigns.mutate({"action": "save", "revision": app.campaigns.public()["revision"], "campaign": {
                "name": name, "start": start, "end": end, "confidence": "planning"}})
        rows = app.campaigns.overlapping("2030-03-05", "2030-03-17")
        with patch.object(app, "_campaign_weekly", return_value=synthetic()):
            lines = app._campaign_lifts(video, rows)
            context = {"review_video": video, "review_summary": {}}
            brief = app._draft_figures(context, "X")["观察窗内的平台活动"]
        text = lines[0]["同产品按周对比"]
        self.assertEqual(lines[0]["活动"], "测试大促")
        self.assertIn("202H 活动周（2 周）周均 GMV $300", text)
        self.assertIn("此前 4 个无活动周 $100", text)
        self.assertIn("×3.0", text)
        self.assertIn("周均订单 2.0 对 2.0（×1.0）", text)
        self.assertEqual(brief["该产品在这些活动里的周数据（整个产品的所有视频，不是这一条视频）"], lines)
        self.assertIn("不等于活动造成的", brief["判读要求"])
        self.assertIn("要同时看订单数", brief["判读要求"])
        # The whole brief, multipliers included, is what the model is shown.
        from prepublish_review import draft_prompt
        prompt = draft_prompt([], None, source="测试", review_text="", figures={"观察窗内的平台活动": brief})
        self.assertIn("×3.0", prompt)
        self.assertIn("周均订单 2.0 对 2.0", prompt)
        with patch.object(app, "_campaign_weekly", return_value=({}, set())):
            self.assertIn("无法按周对比", app._campaign_lifts(video, rows)[0]["同产品按周对比"])
        with patch.object(app, "_campaign_weekly", side_effect=RuntimeError("no figures")):
            self.assertEqual(app._campaign_lifts(video, rows), [], "The brief never fails because the weekly figures cannot be read")

    def test_real_weekly_rows_of_the_fixture_product_add_up(self):
        video = self.server.app.performance.video(self.video_id)
        totals, complete = self.server.app._campaign_weekly({**video, "shop": video.get("shop") or "local"})
        analytics = self.server.app.performance.analytics(video.get("shop") or "local")
        ids = {str(row["video_id"]) for row in self.server.app.performance.videos(shop=video.get("shop") or "local")
               if row["product"] == video["product"]}
        expected = sum(row[7] or 0 for row in analytics["weekly"] if str(analytics["videos"][row[0]][0]) in ids)
        self.assertAlmostEqual(sum(entry["gmv"] for entry in totals.values()), expected, places=2)
        self.assertTrue(complete <= set(totals) | {entry["week"] for entry in self.server.app.performance.summary(video.get("shop") or "local")["weeks"]})


if __name__ == "__main__":
    unittest.main()
