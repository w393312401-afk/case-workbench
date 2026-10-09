"""Real-user review library: import, privacy, classification, search and HTTP."""
from __future__ import annotations

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import test_server as fixture
from review_store import ReviewError, ReviewStore, parse_query
from review_taxonomy import classify, detect_language, excerpt, zh_groups

SECRET_NAMES = ("secretuser", "Jane Doe", "reviewer_handle_77", "Vine Pro Person")


def write(folder, platform, name, items):
    target = Path(folder) / platform
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")


def build_sources(folder):
    write(folder, "amazon", "amazon.json", [
        {"reviewId": "A1", "date": "2026-07-23", "ratingScore": 5, "reviewTitle": "Great kit", "reviewDescription": "Diamond stones are great. The case alone is worth it.",
         "isVerified": True, "isAmazonVine": False, "reviewReaction": "22", "variant": "Set name: with Marker &amp; Compound", "reviewUrl": "https://amazon.test/A1",
         "productAsin": "B0TEST", "userName": "Jane Doe"},
        {"reviewId": "A2", "date": "2026-09-10", "ratingScore": 4, "reviewTitle": "Pivot stuck", "reviewDescription": "As of today, I can no longer swivel the part that holds the knife.",
         "isVerified": True, "isAmazonVine": True, "reviewReaction": "14", "reviewUrl": "https://amazon.test/A2", "productAsin": "B0TEST", "userName": "Vine Pro Person"},
    ])
    write(folder, "tiktok_shop", "shop.json", [
        {"review_id": 111, "review_rating": 1, "review_time": "1786496734804", "review_text": "Illegal bait and switch business practices. All I got was a leather strop.",
         "is_verified_purchase": True, "is_incentivized_review": False, "sku_specification": "Item: Leather Strop Refill", "product_id": "1732157836537861037",
         "review_images": [], "reviewer_name": "secretuser"},
        {"review_id": 112, "review_rating": 5, "review_time": "1786496734805", "review_text": "", "is_verified_purchase": True, "is_incentivized_review": False,
         "sku_specification": "Item: Full Sharpener Set", "product_id": "1732157836537861037", "review_images": [], "reviewer_name": "secretuser"},
    ])
    video = "https://www.tiktok.com/@bushcraftedge/video/7654572167790611743"
    write(folder, "tiktok_video", "video.json", [
        {"cid": "C1", "videoWebUrl": video, "text": "That’s a lot of wobble", "diggCount": 3, "createTimeISO": "2026-06-01T10:00:00.000Z", "uniqueId": "reviewer_handle_77", "uid": "99"},
        {"cid": "C2", "videoWebUrl": video, "text": "Thanks for watching!", "diggCount": 1, "createTimeISO": "2026-06-02T10:00:00.000Z", "uniqueId": "bushcraftedge", "repliesToId": "C1"},
        {"cid": "C3", "videoWebUrl": video, "text": "夹具抓不牢短刀", "diggCount": 0, "createTimeISO": "2026-06-03T10:00:00.000Z", "uniqueId": "reviewer_handle_77"},
        {"error": "Post is sensitive content.", "errorCode": "POST_SENSITIVE", "url": video},
    ])
    write(folder, "reddit", "reddit.json", [
        {"dataType": "comment", "id": "t1_r1", "body": "Get the Sharpal 202H &amp; a strop, it&#39;s the best bang for the buck.", "communityName": "r/sharpening",
         "parsedCommunityName": "sharpening", "createdAt": "2026-04-13T21:01:53.000Z", "url": "https://reddit.test/r1", "username": "secretuser"},
        {"dataType": "comment", "id": "t1_r2", "body": "The Spyderco Sharpmaker is great.", "communityName": "r/knives", "parsedCommunityName": "knives",
         "createdAt": "2026-04-14T21:01:53.000Z", "url": "https://reddit.test/r2", "username": "secretuser"},
    ])
    write(folder, "facebook", "fb.json", [
        {"id": "F1", "text": "How much for the sharpener?", "date": "2026-09-19T14:06:01.000Z", "likesCount": "2", "threadingDepth": 0, "inputUrl": "https://facebook.test/post1",
         "profileName": "Jane Doe", "postTitle": "What Comes After 1000 Grit?"},
        {"id": "F2", "text": "The SHARPAL 202H is now part of our Fall Sale!", "date": "2026-09-20T14:06:01.000Z", "likesCount": "3", "threadingDepth": 0,
         "inputUrl": "https://facebook.test/post1", "profileName": "Sharpal Inc."},
    ])


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-store-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        build_sources(self.store.sources)

    def test_sync_reads_all_five_platforms_and_skips_unchanged(self):
        result = self.store.sync()
        self.assertEqual((result["changed"], result["reviews"], result["failed"]), (True, 11, []))
        self.assertFalse(self.store.sync()["changed"])
        self.assertTrue(self.store.sync(force=True)["changed"])
        platforms = {p["platform"]: p["total"] for p in self.store.summary()["platforms"]}
        self.assertEqual(platforms, {"tiktok_video": 3, "tiktok_shop": 2, "amazon": 2, "reddit": 2, "facebook": 2})

    def test_reviewer_names_are_never_stored(self):
        self.store.sync()
        connection = self.store.connect()
        try:
            dump = "\n".join(connection.iterdump())
        finally:
            connection.close()
        for name in SECRET_NAMES:
            self.assertNotIn(name, dump)

    def test_roles_and_hidden_by_default(self):
        self.store.sync()
        everything = self.store.search({"include_brand": True, "include_empty": True, "include_irrelevant": True}, limit=100)
        roles = {r["id"]: r["role"] for r in everything["reviews"]}
        self.assertEqual(sorted(set(roles.values())), ["brand_account", "incentivized_buyer", "peer", "verified_buyer", "viewer"])
        default = self.store.search({}, limit=100)
        self.assertNotIn("brand_account", {r["role"] for r in default["reviews"]})
        self.assertEqual(default["total"], 7)  # 11 minus 2 brand replies, 1 empty review, 1 reddit comment that does not mention the product
        self.assertEqual(self.store.search({"role": ["brand_account"]})["total"], 2)

    def test_reply_thread_and_error_items_are_handled(self):
        self.store.sync()
        reply = next(r for r in self.store.search({"include_brand": True})["reviews"] if r["is_reply"] and r["platform"] == "tiktok_video")
        detail = self.store.review(reply["id"])
        self.assertEqual([t["text"] for t in detail["thread"]], ["That’s a lot of wobble"])
        imports = {i["source_file"]: i for i in self.store.summary()["imports"]}
        self.assertEqual(imports["tiktok_video/video.json"]["skipped"], 1)

    def test_same_comment_in_two_files_is_stored_once(self):
        write(self.store.sources, "tiktok_video", "video-again.json", [
            {"cid": "C1", "videoWebUrl": "https://www.tiktok.com/@a/video/7654572167790611743", "text": "That’s a lot of wobble", "diggCount": 3,
             "createTimeISO": "2026-06-01T10:00:00.000Z", "uniqueId": "someone"}])
        self.assertEqual(self.store.sync()["reviews"], 11)

    def test_bad_file_is_reported_and_does_not_stop_the_others(self):
        (self.store.sources / "amazon" / "broken.json").write_text("{not json", encoding="utf-8")
        write(self.store.sources, "amazon", "not-a-list.json", {"a": 1})
        result = self.store.sync()
        self.assertEqual(sorted(f["file"] for f in result["failed"]), ["broken.json", "not-a-list.json"])
        self.assertEqual(result["reviews"], 11)

    def test_missing_source_folder_is_reported(self):
        store = ReviewStore(Path(self.temp.name) / "other-runtime")
        result = store.sync()
        self.assertEqual((result["reviews"], len(result["missing_sources"])), (0, 1))

    def test_search_english_chinese_and_filters(self):
        self.store.sync()
        self.assertEqual(self.store.search({"q": "wobble"})["total"], 1)
        self.assertEqual(self.store.search({"q": "夹具抓"})["total"], 1)       # trigram: 3+ characters
        self.assertEqual(self.store.search({"q": "夹具"})["total"], 1)           # 2 characters fall back to LIKE
        self.assertEqual(self.store.search({"q": "diamond case"})["total"], 1)   # every word must match
        self.assertEqual(self.store.search({"q": "100%_"})["total"], 0)           # LIKE wildcards are escaped
        self.assertEqual(self.store.search({"platform": ["amazon"], "rating_max": 4})["total"], 1)
        self.assertEqual(self.store.search({"topic": ["pivot"]})["reviews"][0]["rating"], 4)
        self.assertEqual(self.store.search({"nature": ["buy_info"]})["total"], 1)
        self.assertEqual(self.store.search({"language": "other"})["total"], 1)

    def test_chinese_words_are_also_searched_as_english(self):
        self.store.sync()
        found = self.store.search({"q": "晃动"})
        self.assertEqual([r["text"] for r in found["reviews"]], ["That’s a lot of wobble"])
        self.assertEqual(found["query"]["unmapped"], [])
        self.assertEqual([e["term"] for e in found["query"]["expanded"]], ["晃动"])
        self.assertIn("wobbl", found["query"]["expanded"][0]["english"])
        priced = self.store.search({"q": "价格"}, limit=10)
        self.assertEqual({r["text"] for r in priced["reviews"]}, {"Diamond stones are great. The case alone is worth it.", "How much for the sharpener?"})
        # every term must match: the wobble comment says nothing about a clamp, and the Chinese comment nothing about wobble
        self.assertEqual(self.store.search({"q": "夹具 晃动"})["total"], 0)
        # real Chinese text still matches as typed
        self.assertEqual([r["text"] for r in self.store.search({"q": "夹具"})["reviews"]], ["夹具抓不牢短刀"])
        # a word with no English entry is searched as typed and reported, never guessed at
        unknown = self.store.search({"q": "重要"})
        self.assertEqual((unknown["total"], unknown["query"]["unmapped"], unknown["query"]["expanded"]), (0, ["重要"], []))
        self.assertEqual(self.store.search({"q": "wobble"})["query"], {"expanded": [], "unmapped": []})

    def test_chinese_word_table_rules(self):
        self.assertEqual([word for word, _ in zh_groups("夹具问题")], ["夹具", "问题"])  # two ideas in one word: both must match
        self.assertEqual([word for word, _ in zh_groups("抛光膏")], ["抛光膏"])          # the longer word wins over 抛光
        self.assertEqual([word for word, _ in zh_groups("夹具夹子")], ["夹具"])          # two words for one idea ask once
        self.assertEqual(zh_groups("重要"), [])                                        # one character only counts when it is the whole word
        self.assertNotEqual(zh_groups("重"), [])
        self.assertEqual(zh_groups("clamp"), [])
        self.assertEqual([(t["term"], t["chinese"]) for t in parse_query("clamp夹具 pivot")], [("clamp", False), ("夹具", True), ("pivot", False)])
        self.assertEqual(len(parse_query("a b c d e f g h i j")), 8)

    def test_reply_counts_and_the_replied_filter(self):
        self.store.sync()
        by_text = {r["text"]: r for r in self.store.search({"include_brand": True}, limit=100)["reviews"]}
        self.assertEqual(by_text["That’s a lot of wobble"]["replies"], {"total": 1, "brand": 1})
        self.assertEqual(by_text["夹具抓不牢短刀"]["replies"], {"total": 0, "brand": 0})
        self.assertIsNone(by_text["Thanks for watching!"]["replies"])           # a reply itself
        self.assertIsNone(by_text["How much for the sharpener?"]["replies"])    # only TikTok video comments record replies
        self.assertEqual([r["text"] for r in self.store.search({"replied": "yes"})["reviews"]], ["That’s a lot of wobble"])
        self.assertEqual([r["text"] for r in self.store.search({"replied": "no"})["reviews"]], ["夹具抓不牢短刀"])
        with self.assertRaises(ReviewError):
            self.store.search({"replied": "maybe"})

    def test_covers_matches_the_product_code_like_a_sku(self):
        self.assertTrue(self.store.covers("SHARPAL 202H Precision Kitchen Knife Sharpening System"))
        self.assertTrue(self.store.covers("202h套装"))
        self.assertFalse(self.store.covers("X1202H"))
        self.assertFalse(self.store.covers("SHARPAL 303"))
        self.assertFalse(self.store.covers(None))
        self.assertFalse(ReviewStore(Path(self.temp.name) / "no-library").covers("SHARPAL 202H"))  # nothing on disk yet

    def test_sort_pagination_and_facets(self):
        self.store.sync()
        first = self.store.search({"platform": ["amazon"]}, limit=1, sort="likes")
        self.assertEqual((first["total"], first["reviews"][0]["likes"]), (2, 22))
        second = self.store.search({"platform": ["amazon"]}, limit=1, offset=1, sort="likes")
        self.assertEqual(second["reviews"][0]["likes"], 14)
        newest = self.store.search({"platform": ["amazon"]}, sort="newest")["reviews"]
        self.assertEqual([r["posted_on"] for r in newest], ["2026-09-10", "2026-07-23"])
        facets = self.store.search({"platform": ["amazon"]})["facets"]
        self.assertGreater(facets["platform"]["reddit"], 0)  # source counts ignore the source filter itself
        self.assertEqual(facets["rating"], {"4": 1, "5": 1})

    def test_invalid_queries_raise_review_errors(self):
        self.store.sync()
        for query in ({"platform": ["myspace"]}, {"topic": ["nonsense"]}, {"nature": ["x"]}, {"rating_min": "abc"}):
            with self.assertRaises(ReviewError):
                self.store.search(query)
        with self.assertRaises(ReviewError):
            self.store.search({}, sort="random")


class ClassificationTests(unittest.TestCase):
    """Real comments from the library: what each should (and should not) be tagged as."""

    def tags(self, text, role="viewer", rating=None, title=""):
        nature, topics, language = classify(text, title=title, rating=rating, role=role)
        return nature, topics, language

    def test_hand_sharpening_snark_is_an_objection_not_a_defect(self):
        nature, topics, _ = self.tags("Skill issue")
        self.assertIn("objection", nature)
        self.assertIn("handsharp", topics)
        nature, _, _ = self.tags("If you can’t sharpen a knife without all these gadgets you don’t need a knife.")
        self.assertNotIn("defect", nature)

    def test_buyers_mentioning_whetstones_are_not_hand_sharpening_critics(self):
        _, topics, _ = self.tags("I've used standard whetstones in the past but could never get the technique down.", role="verified_buyer", rating=5)
        self.assertNotIn("handsharp", topics)

    def test_hardware_complaints(self):
        nature, topics, _ = self.tags("It doesn't securely hold short blades, and I feel like I'm going to strip the screws if I really try to clamp down on a blade.", role="verified_buyer", rating=4)
        self.assertTrue({"defect", "owner"} <= nature)
        self.assertTrue({"clamp", "small_blade"} <= topics)
        nature, topics, _ = self.tags("As of today, I can no longer swivel the part that holds the knife.", role="verified_buyer", rating=4)
        self.assertIn("pivot", topics)
        _, topics, _ = self.tags("That’s a lot of wobble")
        self.assertIn("stability", topics)
        nature, topics, _ = self.tags("Everything works good except the degree of angle is not accurate.", role="verified_buyer", rating=4)
        self.assertTrue("angle" in topics and "defect" in nature)

    def test_wrong_listing_option(self):
        _, topics, _ = self.tags("Illegal bait and switch business practices. All I got was a leather strop.", role="verified_buyer", rating=1)
        self.assertIn("wrong_sku", topics)

    def test_asking_about_price_is_buy_info_but_praising_value_is_not(self):
        nature, _, _ = self.tags("How much for the sharpening kit? its sweet")
        self.assertIn("buy_info", nature)
        nature, topics, _ = self.tags("Not deliverable to Alaska? Damn shame…")
        self.assertTrue("buy_info" in nature and "shipping" in topics)
        nature, topics, _ = self.tags("Great price, worth the money", role="verified_buyer", rating=5)
        self.assertNotIn("buy_info", nature)
        self.assertIn("price", topics)

    def test_results_and_use_cases(self):
        nature, topics, _ = self.tags("I've even been able to completely reshape a blade that had a huge chip in the belly and turn it into a tanto and it's razor sharp now.", role="verified_buyer", rating=5)
        self.assertTrue("result" in nature and "repair" in topics)
        _, topics, _ = self.tags("The first night I received it I must have sharpened 20 knives from the kitchen alone", role="verified_buyer", rating=5)
        self.assertIn("many_knives", topics)

    def test_owner_and_comparison(self):
        nature, _, _ = self.tags("So, I admit, I bought this set. I had a lansky set but the wire rods constantly twisted.")
        self.assertTrue({"owner", "compare"} <= nature)

    def test_questions(self):
        nature, topics, _ = self.tags("Can you sharpen a katana?")
        self.assertTrue("question" in nature and "blade_fit" in topics)
        nature, _, _ = self.tags("When you get towards the tip or the handle, readjust the angle to match the middle")
        self.assertNotIn("question", nature)
        self.assertIn("advice", nature)

    def test_noise_and_foreign_language(self):
        nature, _, language = self.tags("🥰🥰🥰")
        self.assertIn("noise", nature)
        self.assertEqual(language, "none")
        nature, _, language = self.tags("Sowas Umständliches !!! Das macht man Freihand .")
        self.assertEqual(language, "other")
        self.assertNotIn("noise", nature)
        nature, _, _ = self.tags("Costo")
        self.assertIn("buy_info", nature)

    def test_language_detection(self):
        self.assertEqual(detect_language("Works great and easy to use"), "en")
        self.assertEqual(detect_language("Quiero una de eso para mi abuela"), "other")
        self.assertEqual(detect_language("夹具抓不牢"), "other")


def build_digest_sources(folder):
    """Enough TikTok comments and Amazon reviews to fill each list of the digest."""
    video = "https://www.tiktok.com/@bushcraftedge/video/7654572167790611743"

    def comment(cid, text, likes=0, user="viewer_one", parent=None):
        item = {"cid": cid, "videoWebUrl": video, "text": text, "diggCount": likes, "createTimeISO": "2026-06-01T10:00:00.000Z", "uniqueId": user}
        if parent:
            item["repliesToId"] = parent
        return item

    asked = ["Will this do swords?"] + [f"Does it work on {thing}?" for thing in ("axes", "machetes", "scissors", "chisels", "cleavers", "hatchets", "katanas")]
    write(folder, "tiktok_video", "digest.json", [
        *[comment(f"Q{i}", text, likes=10 - i) for i, text in enumerate(asked)],
        comment("Q9", "Does it work on serrated knives?", 3),
        comment("R9", "Not serrated, sorry.", 0, user="bushcraftedge", parent="Q9"),
        comment("D1", "That is very wobbly", 9),
        comment("D2", "Why do you use an AI white person?", 4),
        comment("H1", "Skill issue", 50),
        comment("L1", "Lansky made these back in the 80’s and you don’t need blue ink.", 40),
    ])
    long_text = " ".join(["The kit came in a nice case and the plates feel solid."] * 6 + ["It doesn't securely hold short blades because of the clamping system."]
                         + ["I use it every weekend."] * 6)

    def review(identifier, rating, title, text, likes="1"):
        return {"reviewId": identifier, "date": "2026-08-01", "ratingScore": rating, "reviewTitle": title, "reviewDescription": text, "isVerified": True, "isAmazonVine": False,
                "reviewReaction": likes, "reviewUrl": f"https://amazon.test/{identifier}", "productAsin": "B0TEST", "userName": "Jane Doe"}

    write(folder, "amazon", "digest.json", [
        review("B1", 3, "Mixed", long_text, "5"),
        review("B2", 5, "Great", "The first night I received it I must have sharpened 20 knives from the kitchen alone", "2"),
        review("B3", 5, "Learn", "There is a learning curve! Take your time with it and you will be a pro in no time."),
    ])


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-digest-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        build_digest_sources(self.store.sources)
        self.store.sync()
        self.ids = {r["text"]: r["id"] for r in self.store.search({"include_brand": True}, limit=100)["reviews"]}

    def quotes(self, digest, name):
        return [item["quote"] for item in digest[name]]

    def test_each_list_holds_what_it_is_for(self):
        digest = self.store.digest()
        self.assertEqual((digest["product"], digest["reviews"]), ("202H", 16))  # 13 comments and 3 reviews; the creator's reply stays out
        self.assertEqual(sorted(digest["sources"]), ["TikTok 视频评论", "亚马逊评价"])
        # viewers' doubts about the demo, not hand-sharpening snark or competitor chatter
        self.assertEqual(set(self.quotes(digest, "viewer_doubts")), {"That is very wobbly", "Why do you use an AI white person?"})
        # questions nobody answered: the serrated question has a reply, so it is not here
        unanswered = self.quotes(digest, "unanswered_questions")
        self.assertIn("Will this do swords?", unanswered)
        self.assertNotIn("Does it work on serrated knives?", unanswered)
        self.assertLessEqual(len(unanswered), 8)
        # a buyer's limit is cut to the sentence that states it, out of a review that is mostly praise
        limits = digest["buyer_limits"]
        self.assertEqual([item["id"] for item in limits], [self.ids[next(t for t in self.ids if t.startswith("The kit came in a nice case"))]])
        self.assertIn("doesn't securely hold short blades", limits[0]["quote"])
        self.assertLessEqual(len(limits[0]["quote"]), 262)
        self.assertEqual((limits[0]["who"], limits[0]["stars"], limits[0]["source"]), ("已验证买家", 3, "亚马逊评价"))
        self.assertEqual(len(digest["buyer_results"]), 1)
        self.assertIn("20 knives", digest["buyer_results"][0]["quote"])
        self.assertEqual(len(digest["learning_curve"]), 1)
        self.assertIn("learning curve", digest["learning_curve"][0]["quote"])
        # one review is never listed twice
        ids = [item["id"] for name in ("buyer_limits", "viewer_doubts", "unanswered_questions", "buyer_results", "learning_curve") for item in digest[name]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_digest_carries_no_reviewer_names_and_says_it_is_unverified(self):
        text = json.dumps(self.store.digest(), ensure_ascii=False)
        for name in ("viewer_one", "Jane Doe", "bushcraftedge"):
            self.assertNotIn(name, text)
        self.assertIn("均未核实", text)

    def test_digest_stays_inside_the_size_limit_it_is_given(self):
        full = self.store.digest()
        self.assertGreater(len(full["unanswered_questions"]), 2)
        small = self.store.digest(max_chars=900)
        self.assertLess(len(json.dumps(small, ensure_ascii=False)), len(json.dumps(full, ensure_ascii=False)))
        for name in ("buyer_limits", "viewer_doubts", "unanswered_questions", "buyer_results", "learning_curve"):
            self.assertLessEqual(len(small[name]), 2)  # trimmed from the tail, never emptied
        self.assertEqual(self.quotes(small, "unanswered_questions"), self.quotes(full, "unanswered_questions")[:2])

    def test_no_digest_without_reviews(self):
        self.assertIsNone(ReviewStore(Path(self.temp.name) / "never-built").digest())
        empty = ReviewStore(Path(self.temp.name) / "empty")
        empty.sync()  # no sources folder: an empty library
        self.assertIsNone(empty.digest())


class ExcerptTests(unittest.TestCase):
    def test_short_text_comes_back_whole_and_one_line(self):
        self.assertEqual(excerpt("Short  review\nwith a break.", re.compile("review")), "Short review with a break.")

    def test_long_review_is_cut_to_the_sentence_a_rule_matched(self):
        text = " ".join([f"Filler sentence number {i} is here." for i in range(12)] + ["The clamp does not hold short blades."] + [f"More filler {i}." for i in range(6)])
        out = excerpt(text, re.compile("clamp"), limit=120)
        self.assertIn("The clamp does not hold short blades.", out)
        self.assertTrue(out.startswith("…") and out.endswith("…"))
        self.assertLessEqual(len(out), 122)

    def test_one_huge_sentence_is_windowed_around_the_match(self):
        out = excerpt(("blah " * 200) + "the clamp slips" + (" blah" * 200), re.compile("clamp"), limit=100)
        self.assertIn("clamp", out)
        self.assertLessEqual(len(out), 102)

    def test_no_match_uses_the_opening(self):
        text = "First sentence here. " + "Another sentence follows. " * 30
        self.assertTrue(excerpt(text, re.compile("zzz"), limit=60).startswith("First sentence here."))
        self.assertEqual(excerpt(text, limit=60), excerpt(text, re.compile("zzz"), limit=60))


class ReviewHTTPTests(unittest.TestCase):
    setUp_ = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def setUp(self):
        self.setUp_()
        build_sources(self.runtime / "reviews" / "sources")

    def test_summary_syncs_on_first_use_and_lists(self):
        status, _, summary = self.request("GET", "/api/reviews/summary")
        self.assertEqual(status, 200, summary)
        self.assertEqual((summary["total"], summary["default_visible"]), (11, 7))
        self.assertIn("topic", summary["labels"])
        status, _, data = self.request("GET", "/api/reviews?platform=amazon&sort=likes&limit=1")
        self.assertEqual((status, data["total"], len(data["reviews"])), (200, 2, 1))
        status, _, found = self.request("GET", "/api/reviews?q=wobble")
        self.assertEqual(found["total"], 1)
        status, _, detail = self.request("GET", f"/api/reviews/{found['reviews'][0]['id']}")
        self.assertEqual((status, detail["review"]["text"]), (200, "That’s a lot of wobble"))

    def test_chinese_search_and_replied_filter_over_http(self):
        status, _, found = self.request("GET", "/api/reviews?q=" + quote("晃动"))
        self.assertEqual((status, found["total"]), (200, 1))
        self.assertEqual(found["query"]["expanded"][0]["term"], "晃动")
        status, _, unanswered = self.request("GET", "/api/reviews?replied=no")
        self.assertEqual((status, [r["text"] for r in unanswered["reviews"]]), (200, ["夹具抓不牢短刀"]))
        status, _, answered = self.request("GET", "/api/reviews?replied=yes")
        self.assertEqual(answered["reviews"][0]["replies"], {"total": 1, "brand": 1})
        status, _, thread = self.request("GET", f"/api/reviews/{answered['reviews'][0]['id']}")
        self.assertEqual([t["text"] for t in thread["review"]["thread"]], ["Thanks for watching!"])
        self.assertEqual(self.request("GET", "/api/reviews?replied=maybe")[0], 400)

    def test_unknown_review_and_bad_queries(self):
        self.assertEqual(self.request("GET", "/api/reviews/0123456789abcdef")[0], 404)
        status, _, error = self.request("GET", "/api/reviews?platform=myspace")
        self.assertEqual(status, 400)
        self.assertTrue(error["error"].startswith("评论库："), error)
        self.assertEqual(self.request("GET", "/api/reviews?limit=abc")[0], 400)
        self.assertEqual(self.request("GET", "/api/reviews?q=a&q=b")[0], 400)

    def test_sync_requires_token_and_empty_body(self):
        status, _, result = self.request("POST", "/api/reviews/sync", {})
        self.assertEqual((status, result["reviews"]), (200, 11))
        self.assertEqual(self.request("POST", "/api/reviews/sync", {"x": 1})[0], 400)
        self.assertEqual(self.request("POST", "/api/reviews/sync", {}, authenticated=False)[0], 403)

    def test_page_script_is_served(self):
        status, headers, body = self.request("GET", "/reviews.js")
        self.assertEqual(status, 200)
        self.assertIn(b"CaseReviews", body)


if __name__ == "__main__":
    unittest.main()
