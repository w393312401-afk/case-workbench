"""Attention is a current projection; historical outcomes and unknown recoveries survive."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from task_activity import ActivityStore, settle_resolved_activity, refresh_activity_projection


class NoiseActivityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ActivityStore(Path(temporary.name))
        self.app = SimpleNamespace(activity=self.store)

    def fail(self, source="IMG-one", **fields):
        run = "images:CAR-test:" + source
        self.store.record(run, kind="images", owner_id="CAR-test", source_id=source,
                          status="failed", title="生成图片", phase="images", message="连接中断", **fields)
        return run

    def carousel(self):
        return {"id": "CAR-test", "slides": [{"number": 1, "base": {"id": "B"}, "output_id": "O",
                "outputs": [{"id": "O", "approved": True, "stale": False}]}],
                "generations": [{"id": "IMG-one", "status": "failed", "requested_slides": [1]}]}

    def test_confirmation_resolves_failed_scope_without_rewriting_failure_and_reopens_on_invalidation(self):
        run = self.fail()
        original = copy.deepcopy(self.store.logs(run)["logs"])
        item = self.carousel()
        settle_resolved_activity(self.app, "carousel", item)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        saved = self.store.feed()["items"][0]
        self.assertEqual(saved["status"], "failed")
        self.assertTrue(saved["attention_resolved"])
        self.assertEqual(self.store.logs(run)["logs"], original)
        item["slides"][0]["outputs"][0]["stale"] = True
        settle_resolved_activity(self.app, "carousel", item)
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)
        self.assertFalse(self.store.feed()["items"][0]["attention_resolved"])

    def test_later_generation_for_other_slides_does_not_settle_failure(self):
        run = self.fail()
        self.store.record("images:CAR-test:IMG-two", kind="images", owner_id="CAR-test", source_id="IMG-two", status="completed")
        item = self.carousel()
        item["generations"][0]["requested_slides"] = [2]
        settle_resolved_activity(self.app, "carousel", item)
        self.assertIn(run, {row["run_id"] for row in self.store.feed(filter="current")["items"]})

    def test_archiving_and_restoring_are_independent_of_content_archive_and_keep_logs(self):
        run = self.fail()
        logs = self.store.logs(run)["logs"]
        cursor = self.store.feed()["cursor"]
        self.store.archive_owner("CAR-test", True)
        self.store.archive_owner("CAR-test", True, source="content")
        delta = self.store.feed(after=cursor, filter="current")
        self.assertIn(run, delta["removed"])
        self.assertEqual(delta["items"], [])
        self.assertEqual(delta["counts"]["unread"], 0)
        self.assertEqual(self.store.logs(run)["logs"], logs)
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")
        self.store.archive_owner("CAR-test", False)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        self.store.archive_owner("CAR-test", False, source="content")
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)

    def test_unknown_result_recovery_is_not_hidden_by_confirmation(self):
        run = self.fail(reconcile_pending=True)
        settle_resolved_activity(self.app, "carousel", self.carousel())
        self.assertFalse(self.store.feed()["items"][0]["attention_resolved"])
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)

    def test_archived_timeline_hides_edit_attention_and_restore_reopens_it(self):
        run = "edit:EDT-one"
        self.store.record(run, kind="edit", owner_id="PRD-test", source_id="EDT-one", status="failed")
        item = {"id": "PRD-test", "timelines": [{"unique_id": "uid-one", "archived": True}],
                "edits": [{"id": "EDT-one", "timeline_uid": "uid-one", "status": "failed"}]}
        settle_resolved_activity(self.app, "job", item)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")
        item["timelines"][0]["archived"] = False
        settle_resolved_activity(self.app, "job", item)
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)

    def test_panel_refresh_rechecks_artifact_invalidation_without_opening_order_detail(self):
        run = self.fail()
        catalog = {"revision": "v1", "jobs": [], "carousels": [self.carousel()]}
        stale = False
        def live(item):
            item["slides"][0]["outputs"][0]["stale"] = stale
        app = SimpleNamespace(activity=self.store, lock=threading.RLock(),
                              production=SimpleNamespace(public=lambda: copy.deepcopy(catalog), current_revision=lambda: "v1"),
                              carousel_images_live=live)
        refresh_activity_projection(app)
        self.assertEqual(self.store.feed(filter="current")["items"], [])
        stale = True
        refresh_activity_projection(app)
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], run)
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")

    def test_failed_or_changed_integrity_projection_never_keeps_old_resolution(self):
        run = self.fail()
        settle_resolved_activity(self.app, "carousel", self.carousel())
        self.assertTrue(self.store.feed()["items"][0]["attention_resolved"])
        app = SimpleNamespace(activity=self.store, lock=threading.RLock(),
                              production=SimpleNamespace(public=lambda: {"revision": "v1", "carousels": [self.carousel()]}, current_revision=lambda: "v2"),
                              carousel_images_live=lambda item: None)
        refresh_activity_projection(app)
        self.assertIn(run, {row["run_id"] for row in self.store.feed(filter="current")["items"]})
