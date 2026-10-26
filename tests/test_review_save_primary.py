"""Saving a human conclusion is independent of next-round planning or actions."""
import copy
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from creation_store import CreationError
from review_followups import ReviewFollowupStore
from review_legacy_fixture import seed_legacy_followup
import test_feedback_chain as fixture


class PrimaryReviewSaveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewFollowupStore(self.temp.name)
        self.video = {"product": "202H", "video_id": "7600000000000000001", "window_end": "2026-09-13",
                      "review_context": {"closing_allowed": True}}

    def body(self, **values):
        return {key: self.video[key] for key in ("product", "video_id", "window_end")} | {
            "request_id": uuid4().hex, "revision": self.store.public()["revision"],
            "state": "reviewed", "note": "剪辑和文案逻辑混乱，主题飘忽不定。", "reference_value": "weak", **values}

    def test_primary_save_without_action_requires_conclusion_but_supports_unclassified_older_clients(self):
        for note in ("", "  ", None):
            with self.subTest(note=note), self.assertRaises(CreationError) as caught:
                self.store.save(self.body(note=note), self.video)
            self.assertEqual(caught.exception.status, 400)
        body = self.body()
        del body["reference_value"]
        saved = self.store.save(body, self.video)["entry"]
        self.assertEqual((saved["state"], saved["decision"], saved["reference_value"]), ("reviewed", "", ""))

    def test_primary_edit_preserves_completed_action_including_legacy_actions(self):
        for decision in ("stop", "scale", "execution", "none", "observe"):
            old = seed_legacy_followup(self.store, self.video, decision=decision, outcome="negative", evidence="preliminary")
            saved = self.store.save(self.body(), self.video)["entry"]
            with self.subTest(decision=decision):
                self.assertEqual(saved["decision"], decision)
                self.assertEqual((saved["outcome"], saved["evidence"]), ("negative", "preliminary"))
                self.assertEqual(saved["history"][:-1], old["history"])
                self.assertEqual(saved["note"], self.body()["note"])
                self.assertEqual(saved["reference_value"], "weak")

    def test_omission_only_preserves_action_when_both_records_are_completed(self):
        seed_legacy_followup(self.store, self.video, state="observing", decision="observe")
        completed = self.store.save(self.body(), self.video)["entry"]
        self.assertEqual(completed["decision"], "")
        seed_legacy_followup(self.store, self.video, decision="stop", reference_value="weak")
        pending = self.store.save(self.body(state="pending"), self.video)["entry"]
        self.assertEqual(pending["decision"], "")
        completed = self.store.save(self.body(), self.video)["entry"]
        self.assertEqual(completed["decision"], "")

    def test_explicit_action_still_validates_and_can_be_cleared_or_changed(self):
        seed_legacy_followup(self.store, self.video, decision="none")
        before = self.store.path.read_bytes()
        for decision in ("none", "observe", None, False, 0, [], {}):
            with self.subTest(decision=decision), self.assertRaises(CreationError) as caught:
                self.store.save(self.body(decision=decision), self.video)
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(self.store.path.read_bytes(), before)
        for decision in ("stop", "", "scale", "execution"):
            with self.subTest(decision=decision):
                saved = self.store.save(self.body(decision=decision), self.video)["entry"]
                self.assertEqual(saved["decision"], decision)

    def test_classification_preserves_source_while_conclusion_edits_expire_previous_source(self):
        with patch("review_followups.now", return_value="2026-10-01T12:00:00+00:00"):
            reviewed = self.store.save(self.body(decision="stop"), self.video)["entry"]
        source = self.store.source_snapshot(reviewed)
        frozen = copy.deepcopy(source)
        body = self.body()
        classify = {key: body[key] for key in ("product", "video_id", "window_end", "request_id", "revision")}
        classify.update(classification_only=True, reference_value="medium")
        classified = self.store.save(classify, None)["entry"]
        self.assertEqual(self.store.validate_source(source, "202H"), frozen)
        self.assertEqual(classified["decision"], "stop")
        with patch("review_followups.now", return_value="2026-10-25T12:00:00+00:00"):
            edited = self.store.save(self.body(note="补充：每次剪辑只围绕一个主题。"), self.video)["entry"]
        self.assertEqual(edited["decision"], "stop")
        self.assertEqual(source, frozen)
        with self.assertRaises(CreationError) as caught:
            self.store.validate_source(source, "202H")
        self.assertEqual(caught.exception.status, 409)
        current = self.store.source_snapshot(edited)
        self.assertEqual(self.store.validate_source(current, "202H"), current)


class PrimaryReviewSaveHTTPTests(unittest.TestCase):
    setUp = fixture.FeedbackChainHTTPTests.setUp
    stop_server = fixture.FeedbackChainHTTPTests.stop_server
    request = fixture.FeedbackChainHTTPTests.request
    no_download = staticmethod(fixture.FeedbackChainHTTPTests.no_download)
    ok = fixture.FeedbackChainHTTPTests.ok
    chain = fixture.FeedbackChainHTTPTests.chain

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.chain()["review_revision"], "product": "202H",
                "video_id": self.video_id, "window_end": self.window, "state": "reviewed",
                "note": "主题不清晰，下一次只证明一个使用收益。", "reference_value": "weak", **values}

    def test_primary_save_is_idempotent_and_does_not_create_a_plan(self):
        app = self.server.app
        creation = app.creation.read()
        body = self.body()
        self.assertNotIn("decision", body)
        self.assertEqual(self.request("POST", "/api/review-followups", body, authenticated=False)[0], 403)
        saved = self.ok("POST", "/api/review-followups", body)
        before_replay = app.review_followups.path.read_bytes()
        self.assertEqual(self.ok("POST", "/api/review-followups", body), saved)
        self.assertEqual(app.review_followups.path.read_bytes(), before_replay)
        self.assertEqual(saved["entry"]["decision"], "")
        self.assertEqual(self.chain()["review_status"]["key"], "weak")
        self.assertEqual(self.chain()["plans"], [])
        self.assertEqual(app.creation.read(), creation)
        stale = {**body, "request_id": uuid4().hex, "note": "过期页面的结论"}
        self.assertEqual(self.request("POST", "/api/review-followups", stale)[0], 409)
        self.assertEqual(app.review_followups.path.read_bytes(), before_replay)
        self.assertEqual(self.request("POST", "/api/review-followups", {**body, "note": "更改重试内容"})[0], 409)

    def test_primary_edit_keeps_stop_and_metadata_classification_keeps_updated_source(self):
        app = self.server.app
        seed_legacy_followup(app.review_followups, {"product": "202H", "video_id": self.video_id, "window_end": self.window},
                             decision="stop", outcome="negative", reference_value="weak")
        creation = app.creation.read()
        saved = self.ok("POST", "/api/review-followups", self.body())
        self.assertEqual(saved["entry"]["decision"], "stop")
        self.assertEqual(saved["entry"]["outcome"], "negative")
        source = self.chain()["source_review"]
        body = {"request_id": uuid4().hex, "revision": saved["revision"], "product": "202H",
                "video_id": self.video_id, "window_end": self.window, "classification_only": True, "reference_value": "medium"}
        self.ok("POST", "/api/review-followups", body)
        self.assertEqual(self.chain()["source_review"], source)
        self.assertEqual(self.chain()["review"]["decision"], "stop")
        self.assertEqual(self.chain()["plans"], [])
        self.assertEqual(app.creation.read(), creation)


if __name__ == "__main__":
    unittest.main()
