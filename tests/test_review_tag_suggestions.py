"""AI tag suggestions for untagged reviews: suggest in the background, write nothing until accepted."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from review_store import ReviewStore
from review_tag_suggestions import ReviewTagSuggestions, TagSuggestError, parse_items
from test_review_store import write


class FakeBridge:
    def __init__(self):
        self.prompts = []

    def run(self, prompt, **options):
        self.prompts.append(prompt)
        ids = [json.loads(line)["id"] for line in prompt.splitlines() if line.startswith('{"id"')]
        # The first review gets a topic plus a made-up code (dropped); the second is a joke (noise); the rest nothing.
        items = [{"id": ids[0], "nature": ["question", "not-a-code"], "topic": ["clamp"]},
                 {"id": ids[1], "nature": ["noise"], "topic": []},
                 {"id": "made-up-id", "nature": ["question"], "topic": []}]
        return "```json\n" + json.dumps({"items": items}) + "\n```"


class FakeAI:
    def __init__(self):
        self.bridge = FakeBridge()

    def resolve_default_settings(self, body, kind=None):
        return {"model": body.get("model"), "reasoning_effort": body.get("reasoning_effort")}


class FakeApp:
    def __init__(self, store):
        self.reviews = store
        self.ai = FakeAI()


class ReviewTagSuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-tags-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        video = "https://www.tiktok.com/@someone/video/7600000000000000888"
        write(self.store.sources, "tiktok_video", "plain.json", [
            {"cid": f"P{n}", "videoWebUrl": video, "text": text, "diggCount": 10 - n, "createTimeISO": "2026-06-0%dT10:00:00.000Z" % (n + 1),
             "uniqueId": f"viewer{n}"} for n, text in enumerate(["Type of sandwich scooby and shaggy eats before solving the mystery", "one serving in america",
                                             "would the jaws hold a tiny pocket knife without slipping or not"])])
        self.store.sync()
        self.tags = ReviewTagSuggestions(FakeApp(self.store), self.store.root)

    def wait(self):
        for _ in range(200):
            if not self.tags.busy():
                return self.tags.status()
            time.sleep(0.02)
        self.fail("tag suggestions did not finish")

    def untagged_texts(self):
        return [row["text"] for row in self.store.search({"nature": ["none"], "topic": ["none"]})["reviews"]]

    def test_suggestions_wait_for_a_person_then_save_like_hand_edits(self):
        before = self.tags.status()
        self.assertEqual(before["remaining"], before["untagged"])
        self.assertGreaterEqual(before["untagged"], 2)
        self.tags.start({})
        status = self.wait()
        self.assertEqual(status["run"]["status"], "done")
        # Two suggestions, codes outside the taxonomy and ids the model made up are dropped.
        suggested = {item["text"]: item for item in status["pending"]}
        self.assertEqual(len(suggested), 2)
        first = next(iter(status["pending"]))
        self.assertNotIn("not-a-code", first["nature"])
        # Nothing reached the library yet.
        self.assertIn(first["text"], self.untagged_texts())
        # Accept the first with one tag taken off; dismiss the other.
        self.tags.accept({"items": [{"id": first["id"], "nature": [], "topic": first["topic"]}]})
        other = next(item for item in status["pending"] if item["id"] != first["id"])
        after = self.tags.dismiss({"ids": [other["id"]]})
        self.assertEqual(after["pending"], [])
        self.assertNotIn(first["text"], self.untagged_texts())
        self.assertIn(other["text"], self.untagged_texts())
        # The accepted tag is a hand edit: a rebuild keeps it.
        self.store.sync(force=True)
        row = next(r for r in self.store.search({})["reviews"] if r["id"] == first["id"])
        self.assertEqual(row["tags"]["topic"], first["topic"])
        self.assertEqual(row["manual"]["topic"], {code: True for code in first["topic"]})
        # Dismissed and accepted reviews are not offered again.
        self.assertEqual(self.tags.status()["remaining"], before["remaining"] - 2)

    def test_bad_requests_are_refused(self):
        with self.assertRaises(TagSuggestError):
            self.tags.accept({"items": [{"id": "x", "nature": ["not-a-code"]}]})
        with self.assertRaises(TagSuggestError):
            self.tags.dismiss({"ids": []})
        with self.assertRaises(TagSuggestError):
            parse_items("no json here")


if __name__ == "__main__":
    unittest.main()
