"""Apify scrape: targets and the suggested start come from the library, only in-range comments are kept and imported."""
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from review_scrape import ApifyClient, ReviewScraper, ScrapeError, keep_in_range
from review_store import ReviewStore


def tiktok(cid, day, video="7600000000000000001"):
    return {"cid": cid, "text": f"comment {cid}", "createTimeISO": f"{day}T10:00:00.000Z", "uniqueId": "viewer",
            "videoWebUrl": f"https://www.tiktok.com/@maker/video/{video}?lang=en"}


def amazon(rid, day):
    return {"reviewId": rid, "reviewDescription": f"review {rid}", "date": day, "ratingScore": 5, "productAsin": "B0FS7ZPGCV"}


class FakeApify:
    def __init__(self, items):
        self.items, self.calls = items, []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers, json.loads(body) if body else None))
        if method == "POST" and "/runs" in url:
            actor = url.split("/acts/")[1].split("/")[0]
            return 201, json.dumps({"data": {"id": "run-" + actor, "defaultDatasetId": "ds-" + actor}}).encode()
        if method == "GET" and "/actor-runs/" in url:
            return 200, json.dumps({"data": {"status": "SUCCEEDED", "usageTotalUsd": 0.12}}).encode()
        if "/datasets/" in url:
            actor = url.split("/datasets/ds-")[1].split("/")[0]
            return 200, json.dumps(self.items.get(actor, [])).encode()
        return 404, b"{}"


class ScrapeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-scrape-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name))
        for platform, name, rows in (("tiktok_video", "old.json", [tiktok("a", "2026-10-01"), tiktok("b", "2026-10-20")]),
                                     ("amazon", "old.json", [amazon("R1", "2026-09-15")])):
            folder = self.store.sources / platform
            folder.mkdir(parents=True)
            (folder / name).write_text(json.dumps(rows), encoding="utf-8")
        self.store.sync()
        self.fake = FakeApify({})
        self.app = SimpleNamespace(reviews=self.store)
        self.scraper = ReviewScraper(self.app, ApifyClient(token_provider=lambda: "apify_api_secret", transport=self.fake), poll_seconds=0)

    def wait(self):
        for _ in range(200):
            if not self.scraper.overview("202H")["busy"]:
                return
            time.sleep(0.02)
        self.fail("scrape never finished")

    def test_overview_suggests_continuing_from_newest_comment(self):
        data = self.scraper.overview("202H")
        rows = {row["platform"]: row for row in data["platforms"]}
        self.assertEqual(rows["tiktok_video"]["latest"], "2026-10-20")
        self.assertEqual(rows["tiktok_video"]["targets"], ["https://www.tiktok.com/@maker/video/7600000000000000001"])
        self.assertEqual(rows["amazon"]["targets"], ["https://www.amazon.com/dp/B0FS7ZPGCV"])
        self.assertEqual(rows["reddit"]["targets"], [])
        # Earliest of the platforms' newest dates, so no platform is left with a gap.
        self.assertEqual(data["recommended_start"], "2026-09-15")
        self.assertTrue(data["token"])

    def test_scrape_keeps_only_in_range_and_imports(self):
        self.fake.items = {"clockworks~tiktok-comments-scraper": [tiktok("b", "2026-10-20"), tiktok("c", "2026-11-02"), tiktok("d", "2026-08-01")],
                           "junglee~amazon-reviews-scraper": [amazon("R2", "2026-10-30")]}
        self.scraper.start({"product": "202H", "start": "2026-10-20", "end": "2099-01-01", "platforms": {
            "tiktok_video": {"targets": ["https://www.tiktok.com/@maker/video/7600000000000000001"]},
            "amazon": {"targets": ["https://www.amazon.com/dp/B0FS7ZPGCV"]}, "reddit": {"targets": []}}})
        self.wait()
        job = self.scraper.overview("202H")["job"]
        self.assertEqual(job["status"], "done")
        self.assertEqual((job["platforms"]["tiktok_video"]["fetched"], job["platforms"]["tiktok_video"]["kept"]), (3, 2))
        self.assertNotIn("reddit", job["platforms"])
        self.assertEqual(job["added"], 2)  # c and R2; b was already in the library
        saved = json.loads((self.store.sources / "tiktok_video" / job["platforms"]["tiktok_video"]["file"]).read_text(encoding="utf-8"))
        self.assertEqual([row["cid"] for row in saved], ["b", "c"])
        self.assertEqual(saved[0]["product"], "202H")
        starts = [call for call in self.fake.calls if call[0] == "POST"]
        amazon_input = next(body for _, url, _, body in starts if "amazon" in url)
        self.assertEqual(amazon_input["reviewsCutoffDate"], "2026-10-20")
        for _, url, headers, _ in self.fake.calls:
            self.assertNotIn("secret", url)
            self.assertEqual(headers["Authorization"], "Bearer apify_api_secret")
        self.assertIn("maxItems=", starts[0][1])

    def test_bad_requests_and_missing_token(self):
        for body in ({"product": "all", "start": "2026-10-01", "end": "2026-10-02", "platforms": {"amazon": {"targets": ["x"]}}},
                     {"product": "202H", "start": "2026-10-05", "end": "2026-10-02", "platforms": {"amazon": {"targets": ["x"]}}},
                     {"product": "202H", "start": "2026-10-01", "end": "2026-10-02", "platforms": {"amazon": {"targets": []}}}):
            with self.assertRaises(ScrapeError):
                self.scraper.start(body)
        bare = ReviewScraper(self.app, ApifyClient(token_provider=lambda: "", transport=self.fake), poll_seconds=0)
        self.assertFalse(bare.overview("202H")["token"])
        with self.assertRaises(ScrapeError) as caught:
            bare.start({"product": "202H", "start": "2026-10-01", "end": "2026-10-02", "platforms": {"amazon": {"targets": ["x"]}}})
        self.assertEqual(caught.exception.status, 503)

    def test_tiktok_shop_nested_reviews_are_flattened(self):
        items = [{"productUrl": "https://www.tiktok.com/view/product/1", "reviews": [
            {"review_id": "s1", "review_text": "good", "review_time": "1793000000000", "product_id": "1"},
            {"review_id": "s2", "review_text": "old", "review_time": "1700000000000", "product_id": "1"}]}]
        kept, dropped = keep_in_range("tiktok_shop", items, "202H", "2026-10-01", "2026-12-31")
        self.assertEqual(([row["review_id"] for row in kept], dropped), (["s1"], 1))
        self.assertEqual(kept[0]["productUrl"], "https://www.tiktok.com/view/product/1")


if __name__ == "__main__":
    unittest.main()
