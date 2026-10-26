"""The bundled read-only review search used by writing and review workflows."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from review_store import ReviewStore
from test_review_store import build_digest_sources, build_sources

APP = Path(__file__).resolve().parents[1]
CLI = APP / "scripts" / "review_search.py"


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-cli-")
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        (self.runtime / "workbench").mkdir()
        for name in ("review_store.py", "review_taxonomy.py"):  # the CLI shares the workbench's own filter code
            shutil.copy2(APP / name, self.runtime / "workbench" / name)
        self.store = ReviewStore(self.runtime)
        build_sources(self.store.sources)
        build_digest_sources(self.store.sources)
        self.store.sync()

    def run_cli(self, *args, ok=True):
        result = subprocess.run([sys.executable, "-B", "-X", "utf8", str(CLI), "--runtime", str(self.runtime), *args], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return json.loads(result.stdout) if ok else result.stderr

    def test_search_filters_and_chinese_words(self):
        found = self.run_cli("search", "--query", "晃动")
        self.assertEqual(found["query"]["expanded"][0]["term"], "晃动")
        self.assertIn("That is very wobbly", [r["text"] for r in found["reviews"]])
        unanswered = self.run_cli("search", "--nature", "question", "--replied", "no")
        self.assertIn("Will this do swords?", [r["text"] for r in unanswered["reviews"]])
        self.assertNotIn("Does it work on serrated knives?", [r["text"] for r in unanswered["reviews"]])  # it has a reply
        self.assertIn("video_id", unanswered["reviews"][0])
        untagged = self.run_cli("search", "--topic", "none")
        self.assertTrue(untagged["reviews"] and all(r["topic_codes"] == [] for r in untagged["reviews"]))
        self.assertEqual(self.run_cli("search", "--since", "2000-01-01")["total"], 0)  # everything was there at the first sync
        self.assertIn("日期", self.run_cli("search", "--since", "yesterday", ok=False))

    def test_competitor_and_video_filters(self):
        self.assertEqual(self.run_cli("search", "--video", "7654572167790611743", "--limit", "100")["total"], 15)
        self.assertEqual(self.run_cli("search", "--video", "7999999999999999999")["total"], 0)
        self.assertEqual(self.run_cli("search", "--competitor", "lansky")["total"], 1)  # "Lansky made these back in the 80’s…"

    def test_brief_carries_the_digest_and_the_users_picks(self):
        picked = self.run_cli("search", "--query", "wobbly")["reviews"][0]["id"]
        self.store.set_pick(picked, True, "举例用")
        digest = self.run_cli("brief")["digest"]
        self.assertEqual([(i["id"], i["note"]) for i in digest["user_picks"]], [(picked, "举例用")])
        self.assertEqual(self.run_cli("search", "--query", "wobbly")["reviews"][0]["picked_note"], "举例用")
        small = self.run_cli("brief", "--max-chars", "900")["digest"]
        self.assertLess(len(json.dumps(small, ensure_ascii=False)), len(json.dumps(digest, ensure_ascii=False)))

    def test_a_library_that_was_never_built_says_so(self):
        message = subprocess.run([sys.executable, "-B", "-X", "utf8", str(CLI), "--runtime", str(self.runtime / "elsewhere"), "summary"],
                                 capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(message.returncode, 0)


if __name__ == "__main__":
    unittest.main()
