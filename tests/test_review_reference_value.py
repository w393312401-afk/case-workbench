"""Manual reference value is independent metadata, with durable source identities."""
import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from creation_store import CreationError
from review_followups import ReviewFollowupStore
import test_feedback_chain as feedback_fixture
from review_legacy_fixture import seed_legacy_followup


class ReferenceValueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = ReviewFollowupStore(self.temp.name)
        self.video = {"product": "202H", "video_id": "7600000000000000001", "window_end": "2026-09-13",
                      "review_context": {"closing_allowed": True}, "views": 0, "orders": 0}

    def body(self, **values):
        return {key: self.video[key] for key in ("product", "video_id", "window_end")} | {
            "request_id": uuid4().hex, "revision": self.store.public()["revision"],
            "state": "reviewed", "decision": "stop",
            "note": "剪辑、文案逻辑混乱，主题飘忽不定。", **values}

    def classify(self, value, **values):
        return {key: self.video[key] for key in ("product", "video_id", "window_end")} | {
            "request_id": uuid4().hex, "revision": self.store.public()["revision"],
            "classification_only": True, "reference_value": value, **values}

    def test_manual_classification_never_infers_from_metrics_or_changes_judgment(self):
        seed_legacy_followup(self.store, self.video, decision="stop", outcome="negative", evidence="preliminary")
        for value in ("", "none", "weak", "medium", "high"):
            with self.subTest(value=value):
                saved = self.store.save(self.body(reference_value=value), self.video)["entry"]
                self.assertEqual(saved["reference_value"], value)
                self.assertEqual((saved["outcome"], saved["evidence"], saved["decision"]),
                                 ("negative", "preliminary", "stop"))
        self.video.update(views=1000000, orders=1000)
        saved = self.store.save(self.body(reference_value="none"), self.video)["entry"]
        self.assertEqual(saved["reference_value"], "none")
        self.assertEqual(self.video["orders"], 1000)

    def test_classification_preserves_frozen_source_and_audits_separate_time(self):
        with patch("review_followups.now", return_value="2026-10-01T12:00:00+00:00"):
            old = self.store.save(self.body(), self.video)["entry"]
        source = self.store.source_snapshot(old)
        with patch("review_followups.now", return_value="2026-10-25T12:00:00+00:00"):
            classified = self.store.save(self.classify("weak"), None)["entry"]
        for key in ("at", "note", "state", "decision", "outcome", "evidence", "next_review_on", "defer_until"):
            self.assertEqual(classified[key], old[key], key)
        self.assertEqual(classified["classified_at"], "2026-10-25T12:00:00+00:00")
        self.assertEqual(classified["history"][:-1], old["history"])
        self.assertEqual(classified["history"][-1]["kind"], "classification")
        self.assertEqual(classified["history"][-1]["at"], classified["classified_at"])
        self.assertEqual(classified["history"][-1]["reviewed_at"], old["at"])
        self.assertEqual(self.store.validate_source(source, "202H"), source)
        self.store = ReviewFollowupStore(self.temp.name)
        self.assertEqual(self.store.public()["entries"][0], classified)

    def test_replay_conflict_and_repeated_classification_do_not_duplicate_history(self):
        self.store.save(self.body(), self.video)
        body = self.classify("medium")
        first = self.store.save(body, None)
        self.assertEqual(self.store.save(body, None), first)
        with self.assertRaises(CreationError) as caught:
            self.store.save({**body, "request_id": uuid4().hex, "reference_value": "high"}, None)
        self.assertEqual(caught.exception.status, 409)
        same = self.store.save(self.classify("medium"), None)
        self.assertEqual(same["entry"], first["entry"])
        with self.assertRaises(CreationError):
            self.store.save({**body, "reference_value": "weak"}, None)

    def test_classification_requires_a_reviewed_record_and_valid_minimal_payload(self):
        for state in (None, "pending", "observing", "deferred"):
            if state:
                values = {"state": state, "decision": "observe" if state == "observing" else ""}
                if state == "deferred":
                    values["defer_until"] = "2099-01-01"
                if state == "observing":
                    seed_legacy_followup(self.store, self.video, **values)
                else:
                    self.store.save(self.body(**values), self.video)
            with self.subTest(state=state), self.assertRaises(CreationError) as caught:
                self.store.save(self.classify("weak"), None)
            self.assertEqual(caught.exception.status, 409)
        self.store.save(self.body(), self.video)
        for value in (None, [], 1, "positive", "low"):
            with self.subTest(value=value), self.assertRaises(CreationError):
                self.store.save(self.classify(value), None)
        for change in ({"note": "意外改写"}, {"state": "pending"}, {"decision": "scale"},
                       {"outcome": "positive"}, {"classification_only": "true"}, {"classification_only": 1}):
            with self.subTest(change=change), self.assertRaises(CreationError):
                self.store.save(self.classify("weak", **change), None)
        body = self.classify("weak")
        del body["reference_value"]
        with self.assertRaises(CreationError):
            self.store.save(body, None)

    def test_missing_current_data_does_not_prevent_classifying_a_completed_snapshot(self):
        old = self.store.save(self.body(), self.video)["entry"]
        self.video["review_context"]["closing_allowed"] = False
        result = self.store.save(self.classify("none"), self.video)["entry"]
        self.assertEqual(result["state"], "reviewed")
        self.assertEqual(result["at"], old["at"])
        self.assertEqual(self.store.save(self.body(), self.video)["entry"]["state"], "reviewed")

    def test_normal_state_changes_preserve_classification_but_cannot_assign_before_review(self):
        for state in ("pending", "observing", "deferred"):
            values = {"state": state, "decision": "observe" if state == "observing" else "",
                      "reference_value": "weak"}
            if state == "deferred":
                values["defer_until"] = "2099-01-01"
            with self.subTest(state=state), self.assertRaises(CreationError):
                self.store.save(self.body(**values), self.video)
        reviewed = self.store.save(self.body(reference_value="weak"), self.video)["entry"]
        pending = self.store.save(self.body(state="pending", decision=""), self.video)["entry"]
        self.assertEqual((pending["reference_value"], pending["classified_at"]),
                         (reviewed["reference_value"], reviewed["classified_at"]))
        with self.assertRaises(CreationError):
            self.store.save(self.body(state="observing", decision="observe", reference_value=""), self.video)
        restored = self.store.save(self.body(), self.video)["entry"]
        self.assertEqual(restored["reference_value"], "weak")
        cleared = self.store.save(self.classify(""), None)["entry"]
        self.assertEqual(cleared["reference_value"], "")

    def test_legacy_records_are_not_classified_or_rewritten_during_read(self):
        old = {"product": "202H", "video_id": self.video["video_id"], "window_end": self.video["window_end"],
               "state": "reviewed", "decision": "stop", "note": "已看", "at": "2026-10-01T12:00:00+00:00", "history": []}
        self.store.root.mkdir(parents=True)
        self.store.path.write_text(json.dumps({"schema_version": 1, "entries": [old], "requests": {}}), encoding="utf-8")
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.public()["entries"], [old])
        self.assertEqual(self.store.path.read_bytes(), before)
        classified = self.store.save(self.classify("high"), None)["entry"]
        self.assertEqual(self.store.source_snapshot(classified), self.store.source_snapshot(old))


class ReferenceValueHTTPTests(unittest.TestCase):
    setUp = feedback_fixture.FeedbackChainHTTPTests.setUp
    stop_server = feedback_fixture.FeedbackChainHTTPTests.stop_server
    request = feedback_fixture.FeedbackChainHTTPTests.request
    no_download = staticmethod(feedback_fixture.FeedbackChainHTTPTests.no_download)
    ok = feedback_fixture.FeedbackChainHTTPTests.ok
    chain = feedback_fixture.FeedbackChainHTTPTests.chain
    review = feedback_fixture.FeedbackChainHTTPTests.review

    def classify_body(self, value):
        return {"request_id": uuid4().hex, "revision": self.ok("GET", "/api/review-followups")["revision"],
                "product": "202H", "video_id": self.video_id, "window_end": self.window,
                "classification_only": True, "reference_value": value}

    def test_http_classification_preserves_source_sales_and_closed_state(self):
        self.review(legacy=True, decision="stop", outcome="negative", evidence="preliminary")
        before = self.chain()
        metrics = copy.deepcopy(self.server.app.performance.video(self.video_id))
        body = self.classify_body("weak")
        self.assertEqual(self.request("POST", "/api/review-followups", body, authenticated=False)[0], 403)
        saved = self.ok("POST", "/api/review-followups", body)
        self.assertEqual(self.ok("POST", "/api/review-followups", body), saved)
        after = self.chain()
        self.assertEqual(after["source_review"], before["source_review"])
        self.assertEqual(after["review"]["reference_value"], "weak")
        self.assertEqual(after["review"]["state"], "reviewed")
        self.assertEqual(after["review"]["decision"], "stop")
        self.assertEqual(after["plans"], [])
        self.assertEqual(self.server.app.performance.video(self.video_id), metrics)

    def test_http_classification_uses_existing_snapshot_without_live_video_lookup(self):
        self.review(decision="stop")
        body = self.classify_body("none")
        with patch.object(self.server.app, "review_videos", side_effect=AssertionError("classification does not need current performance")):
            saved = self.ok("POST", "/api/review-followups", body)
        self.assertEqual(saved["entry"]["reference_value"], "none")
        wrong = self.classify_body("high")
        wrong["window_end"] = "2026-09-20"
        self.assertEqual(self.request("POST", "/api/review-followups", wrong)[0], 409)


if __name__ == "__main__":
    unittest.main()
