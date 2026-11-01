"""Batch review drafts keep per-video provenance and never save human reviews."""
import copy
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_feedback_chain as fixture
from test_ai_tasks import FakeBridge


class ReviewBatchDraftTests(unittest.TestCase):
    stop_server = fixture.FeedbackChainHTTPTests.stop_server
    request = fixture.FeedbackChainHTTPTests.request
    ok = fixture.FeedbackChainHTTPTests.ok
    review = fixture.FeedbackChainHTTPTests.review
    no_download = staticmethod(fixture.FeedbackChainHTTPTests.no_download)

    def setUp(self):
        fixture.FeedbackChainHTTPTests.setUp(self)
        self.server.app.ai.bridge = FakeBridge(output="结论：人群：磨刀用户；S：无；A：无；B：无\n参考价值：弱（有具体发现）")

    def videos(self):
        return self.ok("GET", "/api/performance/videos?shop=local")["videos"]

    def body(self, videos):
        return {"request_id": uuid4().hex, "shop": "local", "videos": [
            {key: row[key] for key in ("product", "video_id", "window_end")} for row in videos]}

    def batch(self, body):
        return self.ok("POST", "/api/performance/batch-draft", body)

    def wait_tasks(self):
        for _ in range(200):
            tasks = self.server.app.ai.list_tasks()
            if all(not task.get("busy") for task in tasks):
                return tasks
            time.sleep(.02)
        self.fail("Batch tasks did not finish")

    def test_creates_bound_drafts_and_replays_without_saving_reviews(self):
        candidates = self.videos()[:2]
        self.assertTrue(all(row["review_draft"]["eligible"] for row in candidates))
        body = self.body(candidates)
        first = self.batch(body)
        self.assertEqual(["queued", "queued"], [row["status"] for row in first["results"]])
        tasks = self.wait_tasks()
        self.assertEqual(2, len(tasks))
        for task in tasks:
            raw = self.server.app.ai._read(task["task_id"])
            context = raw["snapshot"]["creation_context"]
            self.assertTrue(context["review_draft"])
            self.assertEqual("retrospective", raw["action"])
            frozen = context["review_video"]
            self.assertIn({key: frozen[key] for key in ("product", "video_id", "window_end")}, body["videos"])
        replay = self.batch(body)
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["results"], replay["results"])
        again = self.batch(self.body(candidates))
        self.assertEqual(["skipped", "skipped"], [row["status"] for row in again["results"]])
        self.assertEqual(2, len(self.server.app.ai.list_tasks()))
        self.assertEqual([], self.server.app.review_followups.public()["entries"])
        by_id = {row["video_id"]: row for row in self.videos()}
        self.assertTrue(all(not by_id[row["video_id"]]["review_draft"]["eligible"] for row in candidates))

    def test_bad_window_and_product_do_not_block_another_video(self):
        rows = self.videos()[:3]
        body = self.body(rows)
        body["videos"][0]["window_end"] = "2000-01-01"
        body["videos"][1]["product"] = "129N"
        result = self.batch(body)
        self.assertEqual(["failed", "failed", "queued"], [row["status"] for row in result["results"]])
        self.assertEqual(1, len(self.wait_tasks()))
        self.assertEqual([], self.server.app.review_followups.public()["entries"])

    def test_validates_batch_structure_before_creating_any_task(self):
        original = self.body(self.videos()[:1])
        invalid = []
        for values in ([], [original["videos"][0]] * 2, [original["videos"][0]] * 11):
            invalid.append({**copy.deepcopy(original), "videos": values})
        invalid.extend([{**copy.deepcopy(original), "shop": "unknown"},
                        {**copy.deepcopy(original), "shop": []},
                        {**copy.deepcopy(original), "request_id": "x"},
                        {**copy.deepcopy(original), "videos": [{"video_id": "bad"}]}])
        for body in invalid:
            with self.subTest(body=body):
                status, _, _ = self.request("POST", "/api/performance/batch-draft", body)
                self.assertEqual(400, status)
        self.assertEqual([], self.server.app.ai.list_tasks())

    def test_other_shop_cannot_start_a_local_video(self):
        body = self.body(self.videos()[:1])
        body["shop"] = "cross"
        result = self.batch(body)
        self.assertEqual("failed", result["results"][0]["status"])
        self.assertEqual([], self.server.app.ai.list_tasks())

    def test_retry_recovers_task_created_before_journal_write_failed(self):
        body = self.body(self.videos()[:2])
        replace = self.server.app.creation._replace
        failed = False

        def interrupted_write(path, data):
            nonlocal failed
            if path.parent.name == "operations" and data.get("results") and not failed:
                failed = True
                raise OSError("fixture interrupted receipt write")
            return replace(path, data)

        with patch.object(self.server.app.creation, "_replace", side_effect=interrupted_write):
            self.assertEqual(500, self.request("POST", "/api/performance/batch-draft", body)[0])
        self.assertTrue(failed)
        self.assertEqual(1, len(self.server.app.ai.list_tasks()))
        recovered = self.batch(body)
        self.assertEqual(["queued", "queued"], [row["status"] for row in recovered["results"]])
        self.assertEqual(2, len({row["task_id"] for row in recovered["results"]}))
        self.assertEqual(2, len(self.wait_tasks()))
        self.assertEqual(recovered["results"], self.batch(body)["results"])

    def test_saved_review_and_registration_only_are_ineligible(self):
        self.review(reference_value="weak")
        rows = self.videos()
        row = next(row for row in rows if row["video_id"] == self.video_id)
        self.assertFalse(row["review_draft"]["eligible"])
        self.assertEqual("skipped", self.batch(self.body([row]))["results"][0]["status"])
        pending = next(row for row in rows if row["video_id"] != self.video_id)
        registered = self.server.app.annotate_review_drafts([{**pending, "registration_only": True}])[0]
        self.assertFalse(registered["review_draft"]["eligible"])

    def test_active_is_skipped_failed_can_retry_and_old_window_does_not_block(self):
        row = self.videos()[0]
        bridge = FakeBridge(blocked=True, error=RuntimeError("fixture failure"))
        self.server.app.ai.bridge = bridge
        first = self.batch(self.body([row]))
        self.assertTrue(bridge.started.wait(5))
        duplicate = self.batch(self.body([row]))
        self.assertEqual("skipped", duplicate["results"][0]["status"])
        bridge.gate.set()
        self.assertEqual("failed", self.wait_tasks()[0]["status"])
        self.assertTrue(next(video for video in self.videos() if video["video_id"] == row["video_id"])["review_draft"]["eligible"])
        self.server.app.ai.bridge = FakeBridge(output="结论：一个具体发现。\n参考价值：弱（用于下轮）")
        retry = self.batch(self.body([row]))
        self.assertEqual("queued", retry["results"][0]["status"])
        self.assertNotEqual(first["results"][0]["task_id"], retry["results"][0]["task_id"])
        self.wait_tasks()
        changed = {**row, "window_end": "2099-01-01"}
        self.assertTrue(self.server.app.annotate_review_drafts([changed])[0]["review_draft"]["eligible"])

    def test_request_id_cannot_be_reused_for_different_videos(self):
        rows = self.videos()[:2]
        body = self.body(rows[:1])
        self.batch(body)
        changed = {**body, "videos": self.body(rows[1:])["videos"]}
        self.assertEqual(409, self.request("POST", "/api/performance/batch-draft", changed)[0])
        self.assertEqual(1, len(self.wait_tasks()))

    def test_task_catalog_failure_keeps_sales_list_readable_and_disables_drafting(self):
        with patch.object(self.server.app.ai, "list_tasks", side_effect=ValueError("fixture task catalog unavailable")):
            rows = self.videos()
        self.assertTrue(rows)
        self.assertTrue(all(not row["review_draft"]["eligible"] for row in rows))
        self.assertTrue(all(row["review_draft"]["unavailable"] for row in rows))
        self.assertTrue(all(row["review_draft"]["reason"] for row in rows))
        self.assertIn("views", rows[0])


if __name__ == "__main__":
    unittest.main()
