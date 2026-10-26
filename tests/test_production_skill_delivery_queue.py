"""Queue receipts include current skill delivery without introducing Resolve readback or writes."""
import copy
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import test_production_manual_completion as manual_fixture
from production_store import ProductionStore
from workbench_carousel_delivery import CarouselDeliveryMixin


class SkillDeliveryQueueTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="skill-delivery-queue-")
        self.addCleanup(temporary.cleanup)
        self.store = ProductionStore(temporary.name)
        self.bench = SimpleNamespace(lock=threading.RLock(), production=self.store)
        self.job = manual_fixture.job_fixture()
        self.edit = {"id": "EDT-0123456789ab", "timeline_uid": "uid-1", "timeline_name": "Zack261013-202-1",
                     "take_id": self.job["selected_take"], "status": "completed", "completion_policy": "skill_delivery",
                     "report": {"saved": True, "gaps": []}, "validation": None}
        self.job["edits"] = [self.edit]

    def rows(self):
        data = self.store.empty()
        data["jobs"] = [copy.deepcopy(self.job)]
        self.store.write(data)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "mutate", side_effect=AssertionError("queue must stay read-only")):
            rows = CarouselDeliveryMixin.production_deliveries(self.bench)["items"]
        self.assertEqual(self.store.path.read_bytes(), before)
        return rows

    def test_saved_skill_result_is_reviewable_and_approval_receipt_is_preserved(self):
        row = self.rows()[0]
        self.assertEqual((row["id"], row["edit_id"], row["timeline_uid"], row["status"], row["approved_at"]),
                         (self.job["id"], self.edit["id"], "uid-1", "awaiting_review", ""))
        self.assertEqual(self.store.job(self.job["id"])["status"], "awaiting_review")
        self.edit["approval"] = {"at": "2026-10-14T09:00:00+08:00", "basis": "human_review"}
        row = self.rows()[0]
        self.assertEqual(row["approved_at"], self.edit["approval"]["at"])
        self.assertEqual(self.store.job(self.job["id"])["status"], "approved")
        # Keep the queue's shared contract; approved_at distinguishes reviewed results.
        self.assertEqual(row["status"], "awaiting_review")

    def test_missing_save_or_material_gaps_never_create_a_review_receipt(self):
        for report in ({}, {"saved": False}, {"saved": "true"},
                       {"saved": True, "gaps": [{"start": 1, "end": 10, "need": "补画面"}]}):
            with self.subTest(report=report):
                self.edit["report"] = report
                self.assertEqual(self.rows(), [])
        self.edit["report"] = {"saved": True}
        self.assertEqual(len(self.rows()), 1)

    def test_later_attempt_on_same_timeline_supersedes_previous_good_result(self):
        for status in ("running", "failed", "cancelled", "interrupted", "needs_material"):
            with self.subTest(status=status):
                self.job["edits"] = [self.edit, {**copy.deepcopy(self.edit), "id": "EDT-111111111111", "status": status}]
                self.assertEqual(self.rows(), [])
        newest = {**copy.deepcopy(self.edit), "id": "EDT-222222222222"}
        self.job["edits"] = [self.edit, newest]
        self.assertEqual([row["edit_id"] for row in self.rows()], [newest["id"]])

    def test_other_voiceover_or_unowned_timeline_cannot_certify_this_selection(self):
        original = copy.deepcopy(self.job)
        for update in ({"selected_take": manual_fixture.OTHER_TAKE_ID}, {"timelines": []}, {"archived": True}):
            with self.subTest(update=update):
                self.job = {**copy.deepcopy(original), **update}
                self.assertEqual(self.rows(), [])
        self.job = copy.deepcopy(original)
        self.job["edits"][0]["timeline_name"] = ""
        self.assertEqual(self.rows(), [])

    def test_attempt_on_another_voiceover_does_not_hide_current_result(self):
        self.job["timelines"].append({"unique_id": "uid-other", "name": "Other voice",
                                      "take_id": manual_fixture.OTHER_TAKE_ID})
        self.job["edits"].append({**copy.deepcopy(self.edit), "id": "EDT-111111111111", "timeline_uid": "uid-other",
                                   "timeline_name": "Other voice", "take_id": manual_fixture.OTHER_TAKE_ID, "status": "running"})
        self.assertEqual([row["edit_id"] for row in self.rows()], [self.edit["id"]])

    def test_legacy_engineering_eligibility_remains_and_unverified_completed_is_excluded(self):
        self.edit.pop("completion_policy")
        self.assertEqual(self.rows(), [])
        self.edit.update(status="awaiting_review", validation={"status": "awaiting_review"})
        self.assertEqual(len(self.rows()), 1)
        for validation in (None, {"status": "verification_failed"}, {"status": "needs_repair"}):
            with self.subTest(validation=validation):
                self.edit["validation"] = validation
                self.assertEqual(self.rows(), [])


if __name__ == "__main__":
    unittest.main()
