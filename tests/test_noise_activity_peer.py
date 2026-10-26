"""Current-feed projection must revoke stale or unproven resolution without opening details."""
import copy
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest

from task_activity import ActivityStore, refresh_activity_projection


class ProjectionPeerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ActivityStore(Path(temporary.name))
        self.run = "images:CAR-peer:IMG-one"
        self.store.record(self.run, kind="images", owner_id="CAR-peer", source_id="IMG-one", status="failed")
        self.item = {"id": "CAR-peer", "slides": [{"number": 1, "output_id": "O",
                     "outputs": [{"id": "O", "approved": True, "stale": False}]}],
                     "generations": [{"id": "IMG-one", "status": "failed", "requested_slides": [1]}]}
        self.revision = "one"
        self.invalidated = False
        self.failed_read = False
        self.race = False

        def live(item):
            if self.failed_read:
                raise OSError("Fixture integrity check failed")
            item["slides"][0]["outputs"][0]["stale"] = self.invalidated
            if self.race:
                self.revision = "two"
        self.app = SimpleNamespace(activity=self.store, lock=threading.RLock(),
            production=SimpleNamespace(public=lambda: {"revision": self.revision, "jobs": [], "carousels": [copy.deepcopy(self.item)]},
                                       current_revision=lambda: self.revision),
            carousel_images_live=live)

    def resolved(self):
        return self.store.feed()["items"][0]["attention_resolved"]

    def test_refresh_after_invalidation_reopens_the_original_failure_without_detail_read(self):
        logs = copy.deepcopy(self.store.logs(self.run)["logs"])
        refresh_activity_projection(self.app)
        self.assertTrue(self.resolved())
        self.invalidated = True
        refresh_activity_projection(self.app)
        self.assertFalse(self.resolved())
        self.assertEqual(self.store.feed(filter="current")["items"][0]["run_id"], self.run)
        self.assertEqual(self.store.feed()["items"][0]["status"], "failed")
        self.assertEqual(self.store.logs(self.run)["logs"], logs)

    def test_failed_integrity_read_revokes_previous_resolution_instead_of_assuming_success(self):
        refresh_activity_projection(self.app)
        self.assertTrue(self.resolved())
        self.failed_read = True
        refresh_activity_projection(self.app)
        self.assertFalse(self.resolved())

    def test_catalog_changed_during_integrity_read_cannot_project_old_completion(self):
        refresh_activity_projection(self.app)
        self.assertTrue(self.resolved())
        self.race = True
        refresh_activity_projection(self.app)
        self.assertFalse(self.resolved())


if __name__ == "__main__":
    unittest.main()
