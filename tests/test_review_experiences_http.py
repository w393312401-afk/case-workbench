"""Review lessons keep exact provenance through HTTP, task selection and backup."""
import copy
from uuid import uuid4
from unittest.mock import patch
import unittest

import test_feedback_chain as feedback
import test_data_backup as backup_fixture
from creation_store import CreationError
from data_backup import BackupManager
from review_experiences import ReviewExperienceStore
import test_review_experiences as store_fixture


class ReviewExperienceHTTPTests(unittest.TestCase):
    setUp = feedback.FeedbackChainHTTPTests.setUp
    stop_server = feedback.FeedbackChainHTTPTests.stop_server
    request = feedback.FeedbackChainHTTPTests.request
    no_download = staticmethod(feedback.FeedbackChainHTTPTests.no_download)
    ok = feedback.FeedbackChainHTTPTests.ok
    chain = feedback.FeedbackChainHTTPTests.chain
    review = feedback.FeedbackChainHTTPTests.review
    direction = feedback.FeedbackChainHTTPTests.direction
    task = feedback.FeedbackChainHTTPTests.task

    def catalog(self, query=""):
        return self.ok("GET", "/api/review-experiences" + ("?" + query if query else ""))

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.catalog()["revision"],
                "source_review": self.chain()["source_review"], "experience": {
                    "product": "202H", "kind": "success", "dimension": "开头", "practice": "先演示夹持稳定，再说明步骤。",
                    "conditions": "同产品且有真实夹持演示，不能用于没有实测的素材。",
                    "evidence_note": "单条视频观察，销量不等于此动作的独立因果。", "confidence": "preliminary",
                    "scope": "product", "direction_id": "", **values}}

    def save(self, body):
        return self.ok("POST", "/api/review-experiences", body)

    def test_confirmed_source_required_and_forged_sources_rejected(self):
        self.assertEqual(self.request("POST", "/api/review-experiences", self.body())[0], 400)
        self.review()
        base = self.body()
        for field, val in (("note", "伪造来源"), ("product", "129N"), ("window_end", "2026-09-20")):
            body = copy.deepcopy(base)
            body["source_review"][field] = val
            self.assertEqual(self.request("POST", "/api/review-experiences", body)[0], 409)
        wrong = self.body(scope="direction", direction_id="202H-MISSING")
        self.assertIn(self.request("POST", "/api/review-experiences", wrong)[0], (400, 404, 409))
        self.assertEqual(self.catalog()["total"], 0)

    def test_source_freezes_metrics_and_exact_publication_version(self):
        self.review(legacy=True, outcome="positive", evidence="preliminary", decision="scale")
        body = self.body()
        receipt = {"id": "PUB-test", "post_id": self.video_id, "job_id": "JOB-test", "script_id": "SCR-test",
                   "script_version": 2, "source": {"script": {"product": "202H", "title": "发布时标题", "script_version": 2,
                   "direction_id": "202H-D1", "text_sha256": "original"}}}
        corrected = {**receipt, "id": "PUB-corrected", "published_on": "2026-09-01"}
        publications = self.server.app._video_publications()
        with patch.object(self.server.app, "_video_publications", return_value=publications), patch.object(publications, "read", return_value={"jobs": {"job": {"history": [receipt], "publication": corrected}}}):
            item = self.save(body)["item"]
        source = item["source"]
        self.assertEqual(source["review"]["outcome"], "positive")
        self.assertEqual(source["video"]["metrics"]["orders"], self.server.app.performance.video(self.video_id)["orders"])
        self.assertEqual(source["video"]["data_status"], "captured_when_saved")
        self.assertTrue(source["video"]["captured_at"])
        self.assertEqual(len(source["publications"]), 1)
        self.assertEqual(source["publications"][0]["id"], "PUB-corrected")
        self.assertEqual(source["publications"][0]["script_version"], 2)
        self.assertEqual(source["publications"][0]["script"]["title"], "发布时标题")
        with patch.object(self.server.app.performance, "video", return_value={"orders": 999}):
            self.assertEqual(self.catalog()["items"][0]["source"], source)
        self.assertEqual(self.chain()["experiences"][0]["id"], item["id"])

    def test_changed_source_pauses_new_tasks_but_preserves_search_and_replay(self):
        self.review()
        body = self.body()
        saved = self.save(body)
        app = self.server.app
        self.assertEqual(app.review_experiences_for_task("202H")[0]["id"], saved["item"]["id"])
        self.review("后续改判，需重新提炼。")
        self.assertEqual(app.review_experiences_for_task("202H"), [])
        self.assertEqual(self.catalog()["items"][0]["source_status"], "changed")
        self.assertEqual(self.save(body), saved)
        self.assertEqual(self.request("POST", "/api/review-experiences", {**body, "request_id": uuid4().hex})[0], 409)
        self.assertEqual(self.catalog("q=" + self.video_id)["total"], 1)
        self.assertEqual(self.catalog("product=129N")["total"], 0)

    def test_historical_review_never_copies_different_window_metrics(self):
        self.review()
        body = self.body()
        current = {**self.server.app.performance.video(self.video_id), "window_end": "2026-09-20", "orders": 999}
        with patch.object(self.server.app.performance, "video", return_value=current):
            source = self.save(body)["item"]["source"]
        self.assertEqual(source["video"]["data_status"], "historical_data_unavailable")
        self.assertEqual(source["video"]["metrics"], {})
        self.assertEqual(source["review"]["window_end"], self.window)

    def test_scope_retirement_restore_and_edit_keep_source(self):
        self.review()
        direction = self.direction()
        original = self.save(self.body(scope="direction", direction_id=direction["direction_id"]))["item"]
        app = self.server.app
        self.assertEqual(app.review_experiences_for_task("202H"), [])
        self.assertEqual(app.review_experiences_for_task("202H", direction["direction_id"])[0]["id"], original["id"])
        def edit(**values):
            return self.save({"request_id": uuid4().hex, "revision": self.catalog()["revision"],
                              "experience": {"id": original["id"], **values}})["item"]
        edit(status="retired")
        self.assertEqual(self.catalog()["items"], [])
        self.assertEqual(app.review_experiences_for_task("202H", direction["direction_id"]), [])
        self.assertEqual(self.catalog("status=retired")["total"], 1)
        restored = edit(status="active", conditions="仅匹配方向且必须有实拍近景。")
        self.assertEqual(restored["source"], original["source"])
        self.assertEqual(restored["version"], 3)
        self.assertEqual(len(restored["history"]), 2)
        self.assertEqual(self.request("GET", "/api/review-experiences?kind=invalid")[0], 400)
        self.assertEqual(self.request("GET", "/api/review-experiences?product=202H&product=129N")[0], 400)

    def test_saved_lessons_reach_real_writing_and_review_prompts_with_frozen_versions(self):
        self.review()
        success = self.save(self.body())["item"]
        failure = self.save(self.body(kind="failure", practice="不要让步骤说明挤掉夹持结果镜头。"))["item"]
        app = self.server.app
        for action in ("variants", "review"):
            task, _ = self.task(action=action)
            selected = task["context"]["review_experiences"]
            self.assertEqual({row["id"] for row in selected}, {success["id"], failure["id"]})
            self.assertEqual({row["kind"] for row in selected}, {"success", "failure"})
            for row in selected:
                self.assertEqual(row["source"]["review"]["video_id"], self.video_id)
                self.assertIn(row["practice"], app.ai.bridge.calls[-1]["prompt"])
        self.save({"request_id": uuid4().hex, "revision": self.catalog()["revision"],
                   "experience": {"id": success["id"], "status": "retired"}})
        self.assertEqual({row["id"] for row in app.review_experiences_for_task("202H")}, {failure["id"]})
        frozen = app.ai.get(task["task_id"])["context"]["review_experiences"]
        self.assertEqual({row["id"] for row in frozen}, {success["id"], failure["id"]})
        self.assertTrue(all(row["status"] == "active" and row["version"] == 1 for row in frozen))

    def test_minimal_lesson_keeps_automatic_source_without_inventing_conditions(self):
        self.review()
        body = self.body()
        del body["experience"]["conditions"]
        del body["experience"]["evidence_note"]
        item = self.save(body)["item"]
        self.assertEqual(item["conditions"], "")
        self.assertEqual(item["evidence_note"], "")
        self.assertEqual(item["source"]["review"]["video_id"], self.video_id)
        self.assertIn("orders", item["source"]["video"]["metrics"])
        task, _ = self.task(action="review")
        selected = task["context"]["review_experiences"][0]
        self.assertEqual(selected["conditions"], "")
        self.assertEqual(selected["source"], item["source"])

    def test_stopped_negative_review_can_keep_sales_and_inform_later_writing(self):
        app = self.server.app
        metrics_before = copy.deepcopy(app.performance.video(self.video_id))
        review = self.review("有成交，但文案和剪辑主题混乱；结束本条。", legacy=True, decision="stop", outcome="negative", evidence="preliminary")
        self.assertEqual(review["state"], "reviewed")
        self.assertEqual(app.creation.public()["plans"], [])
        item = self.save(self.body(kind="failure", dimension="文案与剪辑", practice="避免主题飘忽，围绕一个明确主题展开。"))["item"]
        self.assertEqual(item["source"]["review"]["decision"], "stop")
        self.assertEqual(item["source"]["review"]["outcome"], "negative")
        self.assertEqual(app.performance.video(self.video_id), metrics_before)
        self.assertEqual(item["source"]["video"]["metrics"]["orders"], metrics_before["orders"])
        task, _ = self.task(action="variants")
        self.assertEqual(task["context"]["review_experiences"][0]["id"], item["id"])
        self.assertEqual(task["context"]["review_experiences"][0]["kind"], "failure")

    def test_classification_keeps_source_and_controls_only_future_automatic_reuse(self):
        app = self.server.app
        metrics = copy.deepcopy(app.performance.video(self.video_id))
        review = self.review("有成交但主题混乱，停止本条。", legacy=True, decision="stop", outcome="negative", evidence="preliminary")
        lesson = self.save(self.body(kind="failure", practice="保持文案和剪辑的主题一致。"))["item"]
        old_task, _ = self.task(action="variants")
        self.assertEqual(old_task["context"]["review_experiences"][0]["reference_value"], "")

        def classify(value):
            return self.ok("POST", "/api/review-followups", {"request_id": uuid4().hex,
                "revision": self.chain()["review_revision"], "product": "202H", "video_id": self.video_id,
                "window_end": self.window, "classification_only": True, "reference_value": value})["entry"]

        weak = classify("weak")
        self.assertEqual(weak["at"], review["at"])
        self.assertEqual(weak["decision"], "stop")
        self.assertEqual(weak["outcome"], "negative")
        current = self.catalog()["items"][0]
        self.assertEqual(current["source_status"], "current")
        self.assertEqual(current["reference_value"], "weak")
        self.assertEqual(current["source"], lesson["source"])
        weak_task, _ = self.task(action="review")
        self.assertEqual(weak_task["context"]["review_experiences"][0]["reference_value"], "weak")
        self.assertIn("reference_value 是人工复盘后的参考价值分类", app.ai.bridge.calls[-1]["prompt"])

        classify("none")
        self.assertEqual(self.catalog("q=" + self.video_id)["total"], 1)
        self.assertEqual(self.catalog()["items"][0]["source_status"], "current")
        empty_task, _ = self.task(action="variants")
        self.assertEqual(empty_task["context"]["review_experiences"], [])
        self.assertEqual(app.ai.get(weak_task["task_id"])["context"]["review_experiences"][0]["reference_value"], "weak")
        self.assertEqual(app.ai.get(old_task["task_id"])["context"]["review_experiences"][0]["reference_value"], "")

        classify("high")
        self.assertEqual(app.review_experiences_for_task("202H")[0]["reference_value"], "high")
        self.assertEqual(self.chain()["review"]["evidence"], "preliminary")
        self.assertEqual(app.performance.video(self.video_id), metrics)
        self.assertEqual(app.creation.public()["plans"], [])


class ReviewExperienceBackupTests(unittest.TestCase):
    setUp = backup_fixture.DataBackupTests.setUp
    write = staticmethod(backup_fixture.DataBackupTests.write)
    export = backup_fixture.DataBackupTests.export

    def test_full_backup_restores_versioned_lessons_and_request_receipts(self):
        fixture = store_fixture.ReviewExperienceStoreTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = ReviewExperienceStore(self.runtime)
        original = store.save(fixture.body(scope="product", direction_id=""), fixture.source)
        store.save({"request_id": uuid4().hex, "revision": store.public()["revision"],
                    "experience": {"id": original["item"]["id"], "status": "retired"}})
        archive = self.export()
        second = self.root / "restored"
        BackupManager(second / "skill", second / "runtime", second / "app").restore_archive(archive, second / "recovery")
        restored = ReviewExperienceStore(second / "runtime")
        self.assertEqual(restored.read(), store.read())
        self.assertEqual(restored.public(status="all"), store.public(status="all"))


if __name__ == "__main__":
    unittest.main()
