"""Batch translations use the full review library and durable, source-specific cache."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_store import ReviewStore
from review_translations import ReviewTranslations, TranslationError
from test_review_store import build_sources, write


class BatchTranslationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="review-translations-")
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewStore(Path(self.temp.name) / "runtime")
        build_sources(self.store.sources)
        self.store.sync()
        self.app = SimpleNamespace(reviews=self.store)
        self.job = ReviewTranslations(self.app, self.store.root)

    def rows(self):
        connection = self.store.connect()
        try:
            return [dict(row) for row in connection.execute("SELECT id, text, title FROM reviews WHERE TRIM(text) != '' OR TRIM(COALESCE(title, '')) != ''")]
        finally:
            connection.close()

    @staticmethod
    def inputs(prompt):
        return json.loads(re.search(r"<source>\s*(.*?)\s*</source>", prompt, re.S).group(1))

    @classmethod
    def response(cls, app, prompt, workdir, **kwargs):
        rows = cls.inputs(prompt)
        return json.dumps({"items": [{"id": r["id"], "zh": "正文-" + r["id"] if r["text"].strip() else "",
                                     "title_zh": "标题-" + r["id"] if (r["title"] or "").strip() else ""}
                                    for r in reversed(rows)]}, ensure_ascii=False)

    def wait(self, job=None):
        job = job or self.job
        job.worker.join(timeout=5)
        self.assertFalse(job.worker.is_alive(), "background translation did not finish")
        return job.status()

    def test_all_comments_include_hidden_brand_and_replies_and_cache_survives_restart(self):
        rows = self.rows()
        with patch("case_breakdown_runner.run_prompt", side_effect=self.response) as run:
            self.job.start({})
            result = self.wait()
            self.assertEqual(result["remaining"], 0)
            self.assertEqual(result["run"]["status"], "done")
            self.assertEqual(result["run"]["done"], len(rows))
            supplied = [row for call in run.call_args_list for row in self.inputs(call.args[1])]
            self.assertEqual({row["id"] for row in supplied}, {row["id"] for row in rows})
            self.assertIn("Thanks for watching!", {row["text"] for row in supplied})
            self.assertIn("The Spyderco Sharpmaker is great.", {row["text"] for row in supplied})
            cached = json.loads((self.store.root / "translations.json").read_text(encoding="utf-8"))
            for row in rows:
                entry = cached[row["id"]]
                self.assertEqual(entry["zh"], "正文-" + row["id"] if row["text"].strip() else "")
                self.assertEqual(entry["title_zh"], "标题-" + row["id"] if (row["title"] or "").strip() else "")
            restarted = ReviewTranslations(self.app, self.store.root)
            self.assertEqual(restarted.status()["remaining"], 0)

    def test_invalid_batch_does_not_attach_wrong_translations_and_can_retry(self):
        calls = []

        def partial(app, prompt, workdir, **kwargs):
            rows = self.inputs(prompt)
            calls.extend(row["id"] for row in rows)
            if len(calls) == len(rows):
                return json.dumps({"items": [{"id": "invented", "zh": "错误", "title_zh": ""}]})
            return self.response(app, prompt, workdir, **kwargs)

        with patch("review_translations.BATCH_SIZE", 2), patch("case_breakdown_runner.run_prompt", side_effect=partial):
            self.job.start({})
            first = self.wait()
        self.assertGreater(first["remaining"], 0)
        self.assertLess(first["remaining"], len(self.rows()))
        cache = json.loads((self.store.root / "translations.json").read_text(encoding="utf-8"))
        self.assertNotIn("invented", cache)
        self.assertFalse(set(calls[:2]) & set(cache))
        with patch("review_translations.BATCH_SIZE", 2), patch("case_breakdown_runner.run_prompt", side_effect=self.response):
            self.job.start({})
            self.assertEqual(self.wait()["remaining"], 0)

    def test_changed_source_invalidates_cache_and_stop_preserves_completed_work(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def interruptible(app, prompt, workdir, **kwargs):
            calls.append(prompt)
            if len(calls) == 2:
                started.set()
                release.wait(3)
            return self.response(app, prompt, workdir, **kwargs)

        with patch("review_translations.BATCH_SIZE", 2), patch("case_breakdown_runner.run_prompt", side_effect=interruptible):
            self.job.start({})
            self.assertTrue(started.wait(3))
            self.job.stop()
            release.set()
            stopped = self.wait()
        self.assertGreater(stopped["remaining"], 0)
        self.assertLess(stopped["remaining"], len(self.rows()))
        with patch("review_translations.BATCH_SIZE", 2), patch("case_breakdown_runner.run_prompt", side_effect=self.response):
            self.job.start({})
            self.assertEqual(self.wait()["remaining"], 0)
            row = self.rows()[0]
            connection = self.store.connect()
            connection.execute("UPDATE reviews SET text = ? WHERE id = ?", ("changed source", row["id"]))
            connection.commit()
            connection.close()
            self.assertEqual(self.job.status()["remaining"], 1)
            self.job.start({})
            self.assertEqual(self.wait()["remaining"], 0)

    def test_lookup_returns_cached_at_once_and_translates_a_few_missing_per_request(self):
        rows = self.rows()
        ids = [row["id"] for row in rows]
        with patch("review_translations.LOOKUP_TRANSLATE", 2), patch("case_breakdown_runner.run_prompt", side_effect=self.response) as run:
            first = self.job.lookup({"ids": ids + ["0" * 16]})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(self.inputs(run.call_args.args[1])), 2)
            self.assertEqual(len(first["items"]), 2)
            self.assertEqual(len(first["pending"]), len(rows) - 2)
            self.assertEqual(first["failed"], [])
            self.assertNotIn("0" * 16, first["items"])
            for identifier, item in first["items"].items():
                self.assertTrue(item["zh"].startswith("正文-" + identifier) or item["title_zh"])
            # The two translated ones are now cached: asking again does not send them to the AI.
            second = self.job.lookup({"ids": list(first["items"])})
            self.assertEqual(run.call_count, 1)
            self.assertEqual(second["items"], first["items"])
            self.assertEqual(second["pending"], [])
        self.assertEqual(self.job.status()["remaining"], len(rows) - 2)

    def test_lookup_failure_is_reported_and_off_switch_never_calls_the_ai(self):
        ids = [row["id"] for row in self.rows()][:3]
        with patch("case_breakdown_runner.run_prompt", side_effect=RuntimeError("token=abc offline")):
            result = self.job.lookup({"ids": ids})
        self.assertEqual(result["items"], {})
        self.assertEqual(sorted(result["failed"]), sorted(ids))
        self.assertIn("offline", result["error"])
        self.assertNotIn("abc", result["error"])
        self.job.auto_translate = False
        with patch("case_breakdown_runner.run_prompt") as run:
            result = self.job.lookup({"ids": ids})
        run.assert_not_called()
        self.assertEqual((result["items"], result["pending"], result["failed"]), ({}, [], []))
        for body in ({}, {"ids": []}, {"ids": ["bad"]}, {"ids": ids, "extra": 1}, {"ids": ["0" * 16] * 201}):
            with self.assertRaises(TranslationError):
                self.job.lookup(body)

    def test_prompt_carries_context_but_not_as_text_to_translate(self):
        connection = self.store.connect()
        try:
            reply = connection.execute("SELECT r.id, p.text FROM reviews r JOIN reviews p ON p.id = r.parent_id LIMIT 1").fetchone()
            posted = connection.execute("SELECT id, context FROM reviews WHERE length(coalesce(context,'')) > 0 LIMIT 1").fetchone()
        finally:
            connection.close()
        with patch("case_breakdown_runner.run_prompt", side_effect=self.response) as run:
            self.job.lookup({"ids": [row[0] for row in (reply, posted) if row]})
        prompt = run.call_args.args[1]
        self.assertIn("context 本身不翻译", prompt)
        supplied = {row["id"]: row for row in self.inputs(prompt)}
        if reply:
            self.assertEqual(supplied[reply[0]]["context"]["reply_to"], reply[1].strip()[:300])
        if posted:
            self.assertEqual(supplied[posted[0]]["context"]["post"], posted[1].strip()[:300])
        self.assertTrue(reply or posted, "fixture has neither replies nor post context")

    def test_new_reviews_from_a_sync_are_translated_automatically(self):
        self.store.on_added = self.job.auto
        with patch("case_breakdown_runner.run_prompt", side_effect=self.response):
            self.job.start({})
            self.wait()
            self.assertEqual(self.job.status()["remaining"], 0)
            write(self.store.sources, "tiktok_video", "later.json", [{"cid": "N1", "videoWebUrl": "https://www.tiktok.com/@a/video/7000000000000000001",
                                                                        "text": "Brand new comment", "diggCount": 1, "createTimeISO": "2026-11-01T10:00:00.000Z", "uniqueId": "x"}])
            self.assertEqual(self.store.sync()["added"], 1)
            self.assertTrue(self.job.worker.is_alive() or self.job.status()["remaining"] == 0)
            self.assertEqual(self.wait()["remaining"], 0)
        self.job.auto_translate = False
        self.assertFalse(self.job.auto())


if __name__ == "__main__":
    unittest.main()
