"""External retry payloads retain exact replay, atomic saves and legacy policies."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from direction_store import fingerprint, now
from production_store import ProductionStore


class ExternalReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="production-external-receipts-")
        self.addCleanup(temporary.cleanup)
        self.store = ProductionStore(temporary.name)

    def test_external_index_preserves_explicit_job_deletion_behavior(self):
        identifier = "PRD-000000000001"
        data = self.store.empty()
        data["jobs"] = [{"id": identifier, "title": "old job", "edits": []}]
        data["requests"]["old-job-request"] = {"hash": "old", "at": now(),
            "result": {"job": {"id": identifier, "notes": "complete history" * 4000}}}
        self.store.write(data)
        blob = next((self.store.root / "request-results").glob("*.json"))
        self.store.mutate("job_delete", {"request_id": "delete-job-0001", "job_id": identifier})
        self.assertEqual(self.store.records()["jobs"], [])
        self.assertNotIn("old-job-request", self.store.read()["requests"])
        self.assertTrue(blob.is_file(), "Old catalog backups still need their referenced bytes.")

    def test_new_large_mutation_replays_original_result_after_later_business_changes(self):
        identifier = "PRD-000000000001"
        data = self.store.empty()
        data["jobs"] = [{"id": identifier, "archived": False, "history": [], "notes": "long job history" * 4000}]
        self.store.write(data)
        body = {"request_id": "archive-job-0001", "job_id": identifier, "archived": True,
                "revision": self.store.current_revision()}
        result = self.store.mutate("job_archive", body)
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertIn("result_ref", raw["requests"][body["request_id"]])
        self.store.mutate("job_archive", {"request_id": "restore-job-0001", "job_id": identifier, "archived": False,
                                          "revision": self.store.current_revision()})
        with patch.object(self.store, "_job_archive", side_effect=AssertionError("replay cannot repeat mutation")):
            self.assertEqual(self.store.mutate("job_archive", body), result)
        self.assertTrue(self.store.replay("job_archive", body)["job"]["archived"])
        self.assertFalse(self.store.records()["jobs"][0]["archived"])

if __name__ == "__main__":
    unittest.main()
