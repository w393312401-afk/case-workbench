"""Fast writes retain replay isolation, failed-write rollback and exact backups."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from elevenlabs_client import VoiceError
from production_store import ProductionStore


class ProductionWriteLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="production-write-loading-")
        self.addCleanup(temporary.cleanup)
        self.store = ProductionStore(temporary.name)

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.public()["revision"], **values}

    def reading(self):
        body = self.body(reading={"product": "202H", "written": "202H", "spoken": "two oh two H"})
        return body, self.store.mutate("reading", body)

    def test_later_mutation_and_caller_edits_cannot_change_saved_replay(self):
        body, result = self.reading()
        expected = copy.deepcopy(result)
        result["reading"]["spoken"] = "unsaved caller result"
        self.store.mutate("reading", self.body(reading={"product": "202H", "written": "A", "spoken": "B"}))
        replay = self.store.replay("reading", body)
        self.assertEqual(replay, expected)
        replay["reading"]["spoken"] = "unsaved replay"
        read = self.store.read()
        read["requests"][body["request_id"]]["result"]["reading"]["spoken"] = "unsaved raw read"
        self.assertEqual(self.store.mutate("reading", body), expected)
        self.assertEqual(self.store.replay("reading", body), expected)

    def test_failed_write_rolls_back_business_changes_and_receipt_pruning(self):
        identifier = "PRD-000000000001"
        data = self.store.empty()
        data["jobs"] = [{"id": identifier, "product": "202H", "title": "draft", "edits": []}]
        data["requests"] = {"old-request": {"hash": "old", "result": {"job": copy.deepcopy(data["jobs"][0])}}}
        self.store.write(data)
        expected = self.store.read()
        before = self.store.path.read_bytes()
        with patch.object(self.store, "write", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.store.mutate("job_delete", {"request_id": uuid4().hex, "job_id": identifier})
        self.assertEqual(self.store.read(), expected)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.store.mutate("job_delete", {"request_id": uuid4().hex, "job_id": identifier})
        self.assertEqual(self.store.read()["jobs"], [])
        self.assertNotIn("old-request", self.store.read()["requests"])

    def test_backup_preserves_exact_previous_bytes_and_unicode_history(self):
        body, result = self.reading()
        before = self.store.path.read_bytes()
        self.store.mutate("reading", self.body(reading={"product": "202H", "written": "刀", "spoken": "knife"}))
        backup = self.store.root / "backups" / (hashlib.sha256(before).hexdigest() + ".json")
        self.assertEqual(backup.read_bytes(), before)
        saved = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["requests"][body["request_id"]]["result"], result)
        self.assertEqual(saved["readings"][-1]["written"], "刀")
        invalid = self.store.read()
        invalid["defaults"]["invalid"] = float("nan")
        before_invalid = self.store.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.write(invalid)
        self.assertEqual(self.store.path.read_bytes(), before_invalid)

    def test_cross_instance_stale_revision_and_conflicting_request_still_fail(self):
        body, result = self.reading()
        other = ProductionStore(self.store.root.parent)
        stale = self.body(reading={"product": "202H", "written": "A", "spoken": "B"})
        other.mutate("reading", {**stale, "request_id": uuid4().hex})
        with self.assertRaises(VoiceError) as caught:
            self.store.mutate("reading", stale)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.mutate("reading", body), result)
        with self.assertRaises(VoiceError) as caught:
            other.mutate("reading", {**body, "reading": {"product": "202H", "written": "x", "spoken": "y"}})
        self.assertEqual(caught.exception.status, 409)

    def test_current_revision_reads_fresh_changes_without_copying_receipts(self):
        self.reading()
        expected = self.store.revision(self.store.read())
        with patch("production_store.copy.deepcopy", side_effect=AssertionError("revision copied receipts")):
            self.assertEqual(self.store.current_revision(), expected)
        other = ProductionStore(self.store.root.parent)
        other.mutate("reading", self.body(reading={"product": "202H", "written": "A", "spoken": "B"}))
        self.assertNotEqual(self.store.current_revision(), expected)
        self.assertEqual(self.store.current_revision(), self.store.revision(self.store.read()))


if __name__ == "__main__":
    unittest.main()
