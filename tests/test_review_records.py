"""What people do to the review library, and how it links to videos: the day each review first appeared, tags changed by hand,
picked quotes, competitor tags, the video filter and counts, and the guards around the records file and the database version."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import review_store
import test_performance as performance
import test_server as fixture
from review_store import ReviewError, ReviewStore
from test_review_store import build_digest_sources, build_sources, write

OTHER_VIDEO = "7000000000000000002"


def amazon_review(identifier, text, rating=5):
    return {"reviewId": identifier, "date": "2026-10-01", "ratingScore": rating, "reviewTitle": "", "reviewDescription": text, "isVerified": True,
            "isAmazonVine": False, "reviewReaction": "1", "reviewUrl": f"https://amazon.test/{identifier}", "productAsin": "B0TEST"}


def tiktok_comment(cid, text, video=OTHER_VIDEO, parent=None, likes=0):
    item = {"cid": cid, "videoWebUrl": f"https://www.tiktok.com/@bushcraftedge/video/{video}", "text": text, "diggCount": likes,
            "createTimeISO": "2026-06-05T10:00:00.000Z", "uniqueId": "someone"}
    if parent:
        item["repliesToId"] = parent
    return item


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-records-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        build_sources(self.store.sources)

    def by_text(self, **query):
        return {r["text"]: r for r in self.store.search(query, limit=200)["reviews"]}

    # ---- the day a review first appeared -------------------------------------------------------
    def test_what_was_there_at_the_start_is_not_new_and_later_reviews_are(self):
        first = self.store.sync()
        self.assertEqual(first["added"], 0)
        self.assertEqual(self.store.search({"since": "2000-01-01"})["total"], 0)
        self.assertEqual(self.store.summary()["new_recent"], 0)
        write(self.store.sources, "amazon", "later.json", [amazon_review("A9", "Brand new review about the clamp.")])
        second = self.store.sync()
        self.assertEqual((second["changed"], second["added"]), (True, 1))
        today = date.today().isoformat()
        found = self.store.search({"since": today})
        self.assertEqual([r["text"] for r in found["reviews"]], ["Brand new review about the clamp."])
        self.assertEqual(found["reviews"][0]["first_seen"], today)
        self.assertEqual(self.store.summary()["new_recent"], 1)
        self.store.sync(force=True)  # a rebuild keeps the day a review first appeared
        self.assertEqual(self.store.search({"since": today})["total"], 1)
        self.store.database.unlink()  # so does losing the database: the days live in the records file
        self.store.sync()
        self.assertEqual(self.store.search({"since": today})["total"], 1)
        with self.assertRaises(ReviewError):
            self.store.search({"since": "yesterday"})

    # ---- tags changed by hand ----------------------------------------------------------------------
    def test_tag_changes_by_hand_survive_rebuilds_and_restoring_the_rules_leaves_no_trace(self):
        self.store.sync()
        wobble = self.by_text()["That’s a lot of wobble"]
        self.assertIn("stability", wobble["tags"]["topic"])
        removed = self.store.set_tag(wobble["id"], "topic", "stability", False)
        self.assertNotIn("stability", removed["tags"]["topic"])
        self.assertEqual(removed["manual"], {"topic": {"stability": False}})
        self.assertEqual(self.store.search({"topic": ["stability"]})["total"], 0)
        added = self.store.set_tag(wobble["id"], "topic", "price", True)
        self.assertIn("price", added["tags"]["topic"])
        self.assertEqual(self.store.summary()["tag_edits"], 2)
        edits = self.store.tag_edits()
        self.assertEqual([(e["tag"], e["present"], e["text"]) for e in edits], [("price", True, "That’s a lot of wobble"), ("stability", False, "That’s a lot of wobble")])
        self.store.sync(force=True)  # a rebuild applies them again
        again = self.store.review(wobble["id"])
        self.assertEqual(("stability" in again["tags"]["topic"], "price" in again["tags"]["topic"]), (False, True))
        self.store.set_tag(wobble["id"], "topic", "stability", True)  # back to what the rules say: nothing left to remember
        self.assertEqual(self.store.review(wobble["id"])["manual"], {"topic": {"price": True}})
        self.store.set_tag(wobble["id"], "topic", "price", False)
        self.assertEqual(self.store.review(wobble["id"])["manual"], {})
        self.assertEqual((self.store.summary()["tag_edits"], self.store.tag_edits()), (0, []))
        self.assertEqual(json.loads(self.store.state.path.read_text(encoding="utf-8"))["tags"], {})

    def test_tag_changes_are_checked(self):
        self.store.sync()
        identifier = self.by_text()["That’s a lot of wobble"]["id"]
        for arguments in ((identifier, "topic", "nonsense", True), (identifier, "colour", "price", True), (identifier, "topic", "price", "yes"),
                          ("0123456789abcdef", "topic", "price", True)):
            with self.assertRaises(ReviewError, msg=arguments):
                self.store.set_tag(*arguments)

    def test_competitor_tags_and_the_untagged_filter(self):
        write(self.store.sources, "tiktok_video", "versus.json", [
            tiktok_comment("V1", "I have a Lansky and a Work Sharp, the 202H is easier."), tiktok_comment("V2", "Does it ship to Arkansas?"),
            tiktok_comment("V3", "ok")])
        self.store.sync()
        found = self.store.search({"competitor": ["lansky"]})
        self.assertEqual([r["text"] for r in found["reviews"]], ["I have a Lansky and a Work Sharp, the 202H is easier."])
        self.assertEqual(sorted(found["reviews"][0]["tags"]["competitor"]), ["lansky", "worksharp"])
        self.assertEqual(self.store.search({"competitor": ["worksharp"]})["total"], 1)
        facets = self.store.search({})["facets"]["competitor"]
        self.assertEqual((facets["lansky"], facets["worksharp"], "none" in facets), (1, 1, False))
        self.assertNotIn("Arkansas", " ".join(self.by_text(competitor=["lansky", "worksharp"])))  # a state is not a sharpener
        with self.assertRaises(ReviewError):
            self.store.search({"competitor": ["nonsense"]})
        # reviews the rules have nothing to say about can be listed, and the count in the facets agrees with the list
        untagged = self.store.search({"topic": ["none"]}, limit=200)
        self.assertEqual(untagged["total"], self.store.search({})["facets"]["topic"]["none"])
        self.assertTrue(untagged["reviews"] and all(r["tags"]["topic"] == [] for r in untagged["reviews"]))
        self.assertIn("ok", [r["text"] for r in untagged["reviews"]])

    # ---- picked quotes -------------------------------------------------------------------------------
    def test_picks_keep_their_notes_and_come_back_newest_first(self):
        self.store.sync()
        reviews = self.by_text()
        wobble, price = reviews["That’s a lot of wobble"]["id"], reviews["How much for the sharpener?"]["id"]
        first = self.store.set_pick(wobble, True, "  开头可以接这句  ")
        self.assertEqual((first["pick"]["note"], first["picks"]), ("开头可以接这句", 1))
        self.assertEqual(self.store.set_pick(price, True)["picks"], 2)
        self.assertEqual([p["id"] for p in self.store.picks()], [price, wobble])
        self.assertEqual(self.store.picks()[1]["pick"]["note"], "开头可以接这句")
        self.assertEqual(self.by_text()["That’s a lot of wobble"]["pick"]["note"], "开头可以接这句")  # the list shows what is picked
        self.assertIsNone(self.by_text()["Diamond stones are great. The case alone is worth it."]["pick"])
        self.assertEqual(self.store.set_pick(wobble, True)["pick"]["note"], "开头可以接这句")  # picking again keeps the note
        self.assertEqual(self.store.set_pick(wobble, True, "")["pick"]["note"], "")
        self.assertEqual(self.store.set_pick(wobble, False), {"pick": None, "picks": 1})
        self.assertEqual([p["id"] for p in self.store.picks()], [price])
        self.assertEqual(self.store.summary()["picks"], 1)
        with patch.object(review_store, "MAX_PICKS", 1):
            with self.assertRaises(ReviewError):
                self.store.set_pick(wobble, True)
        for arguments in (("0123456789abcdef", True), (price, "yes"), (price, True, "x" * 2001), (price, True, 5)):
            with self.assertRaises(ReviewError, msg=arguments):
                self.store.set_pick(*arguments)

    def test_picked_quotes_go_into_the_digest_and_only_there(self):
        digest_store = ReviewStore(Path(self.temp.name) / "digest-runtime")
        build_digest_sources(digest_store.sources)
        digest_store.sync()
        wobbly = next(r["id"] for r in digest_store.search({"q": "wobbly"})["reviews"])
        self.assertIn(wobbly, [i["id"] for i in digest_store.digest()["viewer_doubts"]])
        digest_store.set_pick(wobbly, True, "稿里别让夹具晃")
        digest = digest_store.digest()
        self.assertEqual([(i["id"], i["note"]) for i in digest["user_picks"]], [(wobbly, "稿里别让夹具晃")])
        self.assertNotIn(wobbly, [i["id"] for i in digest["viewer_doubts"]])  # not repeated in the automatic lists
        self.assertIn("user_picks", digest["read_me"])

    # ---- links to videos -------------------------------------------------------------------------------
    def test_video_filter_and_counts(self):
        write(self.store.sources, "tiktok_video", "other.json", [tiktok_comment("V1", "Will this do axes?"), tiktok_comment("V2", "Nice.", parent="V1")])
        self.store.sync()
        counts = self.store.video_counts()
        self.assertEqual(counts["7654572167790611743"]["comments"], 2)  # the wobble and Chinese comments; the creator's reply is not counted
        self.assertEqual((counts[OTHER_VIDEO]["comments"], counts[OTHER_VIDEO]["open_questions"]), (2, 0))  # the question has a reply
        write(self.store.sources, "tiktok_video", "third.json", [tiktok_comment("V3", "Will this do hatchets?", video="7000000000000000003")])
        self.store.sync()
        counts = self.store.video_counts()
        self.assertEqual((counts["7000000000000000003"]["comments"], counts["7000000000000000003"]["open_questions"]), (1, 1))
        self.assertEqual([v["video_id"] for v in self.store.summary()["videos"]][:2], [OTHER_VIDEO, "7654572167790611743"])  # most commented first
        self.assertEqual({r["text"] for r in self.store.search({"video": OTHER_VIDEO})["reviews"]}, {"Will this do axes?", "Nice."})
        self.assertEqual(self.store.search({"video": "7654572167790611743"})["total"], 2)
        self.assertEqual(self.store.search({"video": "7999999999999999999"})["total"], 0)
        with self.assertRaises(ReviewError):
            self.store.search({"video": "abc"})
        self.assertEqual(ReviewStore(Path(self.temp.name) / "never-built").video_counts(), {})
        self.assertFalse((Path(self.temp.name) / "never-built" / "reviews").exists())

    # ---- guards ------------------------------------------------------------------------------------------
    def test_an_unreadable_records_file_never_stops_reading_and_is_never_overwritten(self):
        self.store.sync()
        identifier = self.by_text()["That’s a lot of wobble"]["id"]
        self.store.state.path.write_text("{broken", encoding="utf-8")
        self.assertGreater(self.store.search({})["total"], 0)
        self.assertIn("手动记录", self.store.summary()["state_error"])
        with self.assertRaises(ReviewError):
            self.store.set_pick(identifier, True)
        with self.assertRaises(ReviewError):
            self.store.set_tag(identifier, "topic", "price", True)
        write(self.store.sources, "amazon", "later.json", [amazon_review("A9", "Another review.")])
        result = self.store.sync()  # a rebuild leaves the damaged file alone, and does not make everything look new
        self.assertEqual((result["changed"], result["added"]), (True, 0))
        self.assertIn("手动记录", result["state_error"])
        self.assertEqual(self.store.state.path.read_text(encoding="utf-8"), "{broken")
        self.assertEqual(self.store.search({"since": "2000-01-01"})["total"], 0)

    def test_an_older_database_is_dropped_and_rebuilt_and_changed_rules_rebuild_too(self):
        self.store.sync()
        connection = sqlite3.connect(self.store.database)
        connection.execute("PRAGMA user_version = 1")  # a database from before the first-seen column
        connection.close()
        self.assertEqual(self.store.summary()["total"], 0)  # opening it dropped the old tables
        result = self.store.sync()
        self.assertEqual((result["changed"], result["reviews"]), (True, 11))
        self.assertFalse(self.store.sync()["changed"])
        with patch.object(review_store, "CODE_SIGNATURE", "rules edited"):  # the parsers or tag rules changed: tags may differ now
            self.assertTrue(self.store.sync()["changed"])


class RecordHTTPTests(unittest.TestCase):
    setUp_ = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        self.setUp_()
        sources = self.runtime / "reviews" / "sources"
        build_sources(sources)
        write(sources, "tiktok_video", "linked.json", [
            tiktok_comment("L1", "Does it work on a serrated bread knife?", video=performance.LINKED, likes=4),
            tiktok_comment("L2", "That clamp looks flimsy", video=performance.LINKED)])
        performance.build_exports(self.root / "exports")
        self.server.app.performance.sources = [self.root / "exports"]
        self.assertEqual(self.request("POST", "/api/performance/sync", {})[0], 200)

    def test_summary_and_list_carry_the_videos_title_figures_and_case(self):
        status, _, summary = self.request("GET", "/api/reviews/summary")
        self.assertEqual(status, 200, summary)
        video = next(v for v in summary["videos"] if v["video_id"] == performance.LINKED)
        self.assertEqual((video["comments"], video["open_questions"]), (2, 1))
        self.assertEqual((video["headline"], video["account"], video["posted_on"]), ("🤔 Is A Mirror Edge Overrated?", "bushcraftedge", "2026-08-31"))
        self.assertEqual((video["views"], video["orders"], video["ctor_pct"], video["case_id"]), (12000, 4, 1.33, self.identifier))
        other = next(v for v in summary["videos"] if v["video_id"] == "7654572167790611743")  # a video the video data does not know
        self.assertEqual((other["comments"], other["case_id"], "title" in other), (2, None, False))
        status, _, listed = self.request("GET", f"/api/reviews?video={performance.LINKED}")
        self.assertEqual((status, listed["total"]), (200, 2))
        self.assertEqual(listed["videos"][performance.LINKED]["gmv"], 600)
        self.assertEqual(self.request("GET", "/api/reviews?video=abc")[0], 400)
        status, _, everything = self.request("GET", "/api/reviews?limit=100")
        self.assertIn(performance.LINKED, everything["videos"])  # context for every video that has a comment on the page

    def test_the_video_list_shows_how_many_comments_each_video_has(self):
        status, _, data = self.request("GET", "/api/performance/videos")
        linked = next(v for v in data["videos"] if v["video_id"] == performance.LINKED)
        self.assertEqual((status, linked["review_comments"], linked["review_open"]), (200, 2, 1))
        quiet = next(v for v in data["videos"] if v["video_id"] == "7600000000000000002")
        self.assertNotIn("review_comments", quiet)

    def test_picking_quotes_and_changing_tags_over_http(self):
        wobble = self.request("GET", "/api/reviews?q=wobble")[2]["reviews"][0]["id"]
        self.assertEqual(self.request("POST", f"/api/reviews/{wobble}/pick", {"picked": True}, authenticated=False)[0], 403)
        status, _, picked = self.request("POST", f"/api/reviews/{wobble}/pick", {"picked": True, "note": "举例"})
        self.assertEqual((status, picked["pick"]["note"], picked["picks"]), (200, "举例", 1))
        status, _, listed = self.request("GET", "/api/reviews/picks")
        self.assertEqual([(p["id"], p["pick"]["note"]) for p in listed["picks"]], [(wobble, "举例")])
        status, _, changed = self.request("POST", f"/api/reviews/{wobble}/tag", {"kind": "topic", "tag": "price", "present": True})
        self.assertEqual((status, "price" in changed["review"]["tags"]["topic"], changed["review"]["manual"]), (200, True, {"topic": {"price": True}}))
        status, _, edits = self.request("GET", "/api/reviews/tag-edits")
        self.assertEqual([(e["tag"], e["present"]) for e in edits["edits"]], [("price", True)])
        self.assertEqual(self.request("POST", f"/api/reviews/{wobble}/tag", {"kind": "topic", "tag": "nonsense", "present": True})[0], 400)
        self.assertEqual(self.request("POST", f"/api/reviews/{wobble}/tag", {"kind": "topic", "tag": "price"})[0], 400)
        self.assertEqual(self.request("POST", "/api/reviews/0123456789abcdef/pick", {"picked": True})[0], 400)
        self.assertEqual(self.request("POST", f"/api/reviews/{wobble}/pick", {"picked": False})[2]["picks"], 0)


if __name__ == "__main__":
    unittest.main()
