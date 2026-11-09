"""Reviewer avatars: downloaded once per reviewer, failures not retried, attached to cards only when the file exists."""
import json
from pathlib import Path
import tempfile
import unittest

from review_avatars import ReviewAvatars, author_key, avatar_link
from review_store import PARSERS, ReviewStore, read_source

PNG = b"\x89PNG\r\n\x1a\nfake"


class AvatarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-avatars-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name))
        folder = self.store.sources / "tiktok_shop"
        folder.mkdir(parents=True)
        rows = [{"review_id": "1", "review_text": "great", "review_time": "1793000000000", "reviewer_name": "Ann",
                 "reviewer_avatar_url": "https://cdn.example/ann.png"},
                {"review_id": "2", "review_text": "again", "review_time": "1793000000000", "reviewer_name": "Ann",
                 "reviewer_avatar_url": "https://cdn.example/ann.png"},
                {"review_id": "3", "review_text": "meh", "review_time": "1793000000000", "reviewer_name": "Bob",
                 "reviewer_avatar_url": "https://cdn.example/expired.png"}]
        (folder / "shop.json").write_text(json.dumps(rows), encoding="utf-8")
        self.calls = []

        def fetch(url):
            self.calls.append(url)
            if "expired" in url:
                raise OSError("403")
            return "image/png", PNG
        self.store.avatars.fetch = fetch

    def test_download_once_per_reviewer_and_skip_failed_links(self):
        avatars = self.store.avatars
        wanted = avatars.wanted_from_sources(self.store.sources, read_source, PARSERS)
        self.assertEqual(len(wanted), 2)
        self.assertEqual(avatars.download(wanted), 1)
        ann = author_key("tiktok_shop", "", "Ann")
        self.assertEqual(avatars.file(ann).read_bytes(), PNG)
        self.assertEqual(avatars.download(wanted), 0)
        self.assertEqual(len(self.calls), 2)  # neither the saved nor the failed link is fetched again
        self.assertIsNone(avatars.file("../etc"))

    def test_cards_carry_avatar_only_when_saved_and_sync_never_downloads_by_default(self):
        self.store.sync()
        self.assertEqual(self.calls, [])
        self.store.avatars.download(self.store.avatars.wanted_from_sources(self.store.sources, read_source, PARSERS))
        rows = {row["author_name"]: row for row in self.store.search({"product": "202H"}, limit=10)["reviews"]}
        self.assertEqual(rows["Ann"]["avatar"], author_key("tiktok_shop", "", "Ann"))
        self.assertIsNone(rows["Bob"]["avatar"])

    def test_links_are_read_from_each_platform(self):
        self.assertEqual(avatar_link("facebook", {"profilePicture": "https://fb/x.jpg"}), "https://fb/x.jpg")
        self.assertEqual(avatar_link("tiktok_video", {"avatarThumbnail": "http://insecure"}), None)
        self.assertEqual(avatar_link("reddit", {"avatar": "https://x"}), None)


if __name__ == "__main__":
    unittest.main()
