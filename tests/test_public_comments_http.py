"""Paired plugin comments reach user voices without changing video counters."""
from __future__ import annotations

import unittest

import test_server as fixture
import test_performance as exports
from review_store import review_id

VIDEO = "7664986913497009439"
COMMENT = "7664986913497009001"


def payload(**fields):
    comment = {"id": COMMENT, "videoId": VIDEO, "text": "Does this fit a short blade?",
               "likes": 0, "authorName": "Blade Friend", "authorHandle": "blade.friend",
               "createdAt": "2026-10-25T08:00:00Z", **fields}
    return {"videos": [{"id": VIDEO, "author": "example", "comments": [comment]}]}


class PublicCommentsHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def push(self, body, key=None, origin="chrome-extension://abcdefghijklmnop"):
        if not hasattr(self, "key"):
            self.key = self.request("GET", "/api/public-stats/link")[2]["key"]
        return self.request("POST", "/api/public-stats/comments", body,
                            headers={"X-TKL-Key": self.key if key is None else key, "Origin": origin},
                            authenticated=False)

    def test_paired_push_imports_names_and_comment_without_duplicate_or_counter_change(self):
        self.key = self.request("GET", "/api/public-stats/link")[2]["key"]
        before = self.request("GET", "/api/state")[2]
        self.assertEqual(self.push(payload(), key="wrong")[0], 403)
        self.assertEqual(self.push(payload(), origin="https://www.tiktok.com")[0], 403)
        self.assertEqual(self.push({**payload(), "cookie": "not allowed"})[0], 400)
        status, _, saved = self.push(payload())
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["saved"], 1)
        result = self.request("GET", "/api/reviews?product=202H&video=" + VIDEO)[2]
        self.assertEqual(result["total"], 1)
        review = result["reviews"][0]
        self.assertEqual((review["id"], review["author_name"], review["author_handle"], review["likes"]),
                         (review_id("tiktok_video", COMMENT), "Blade Friend", "blade.friend", 0))
        self.assertEqual(self.push(payload())[2]["unchanged"], 1)
        self.assertEqual(self.request("GET", "/api/reviews?video=" + VIDEO)[2]["total"], 1)
        after = self.request("GET", "/api/state?since=" + before["digest"])[2]
        self.assertNotEqual(after["reviews_revision"], before["reviews_revision"])
        self.assertNotEqual(after["digest"], before["digest"])
        self.assertEqual(after["public_stats_revision"], before["public_stats_revision"])

    def test_unknown_video_is_visible_without_being_assigned_to_default_product(self):
        body = payload()
        body["videos"][0].update(id="7000000000000000001")
        body["videos"][0]["comments"][0]["videoId"] = "7000000000000000001"
        status, _, result = self.push(body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["saved"], 1)
        self.assertEqual(self.request("GET", "/api/reviews")[2]["total"], 1)
        self.assertEqual(self.request("GET", "/api/reviews?product=202H")[2]["total"], 0)
        self.assertEqual(self.request("GET", "/api/reviews")[2]["reviews"][0]["product"], "")

    def test_explicit_existing_product_can_classify_a_video_without_a_report(self):
        body = payload()
        body["videos"][0].update(id="7000000000000000001", product="202H")
        body["videos"][0]["comments"][0]["videoId"] = "7000000000000000001"
        self.assertEqual(self.push(body)[0], 200)
        self.assertEqual(self.request("GET", "/api/reviews?product=202H")[2]["total"], 1)

    def test_comment_links_on_video_detail_match_the_user_voice_library(self):
        folder = self.root / "exports"
        exports.build_exports(folder)
        self.server.app.performance.sources = [folder]
        self.server.app.performance.sync()
        self.assertEqual(self.push(payload())[0], 200)
        listed = next(video for video in self.request("GET", "/api/performance/videos")[2]["videos"]
                      if video["video_id"] == VIDEO)
        detail = self.request("GET", "/api/performance/videos/" + VIDEO)[2]["video"]
        self.assertEqual((listed["review_comments"], detail["review_comments"]), (1, 1))
        self.assertEqual(self.request("GET", "/api/reviews?video=" + VIDEO)[2]["total"], 1)


if __name__ == "__main__":
    unittest.main()
