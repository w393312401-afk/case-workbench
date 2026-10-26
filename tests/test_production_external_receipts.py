"""External retry payloads retain exact replay, atomic saves and legacy policies."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_store import ProductionStore


class ExternalReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="production-external-receipts-")
        self.addCleanup(temporary.cleanup)
        self.store = ProductionStore(temporary.name)
        self.body = {"request_id": "receipt-save-0001", "carousel_id": "CAR-000000000001"}
        self.kind = "carousel_generation_start"
        self.result = {"carousel": {"id": self.body["carousel_id"], "title": "原始回执",
                                   "history": [{"note": "生成图片及其完整历史" * 4000}]},
                       "generation": {"id": "IMG-000000000001", "status": "running"}, "ok": True}
        self.data = self.store.empty()
        self.data["carousels"] = [{"id": self.body["carousel_id"], "title": "current business record"}]
        self.data["requests"][self.body["request_id"]] = {
            "hash": fingerprint([self.kind, self.body]), "at": now(), "result": copy.deepcopy(self.result)}

    def legacy_file(self):
        self.store.root.mkdir(parents=True)
        original = json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")
        self.store.path.write_bytes(original)
        return original

    def receipt(self):
        return json.loads(self.store.path.read_text(encoding="utf-8"))["requests"][self.body["request_id"]]

    def test_migration_shrinks_catalog_keeps_full_backup_and_replays_exactly_after_restart(self):
        original = self.legacy_file()
        before_revision = self.store.current_revision()
        with patch("production_store.CATALOG_MAX_BYTES", 4096):
            self.store.write(self.store.read())
        self.assertLess(self.store.path.stat().st_size, 4096)
        self.assertEqual(self.store.current_revision(), before_revision)
        self.assertEqual(self.store.records()["carousels"], self.data["carousels"])
        saved = self.receipt()
        self.assertEqual(saved["result"]["carousel"], {"id": self.body["carousel_id"]})
        ref = saved["result_ref"]
        blob = self.store.root / ref["file"]
        self.assertEqual((blob.stat().st_size, hashlib.sha256(blob.read_bytes()).hexdigest()), (ref["bytes"], ref["sha256"]))
        self.assertEqual(json.loads(blob.read_text(encoding="utf-8")), self.result)
        backup = self.store.root / "backups" / (hashlib.sha256(original).hexdigest() + ".json")
        self.assertEqual(backup.read_bytes(), original)
        other = ProductionStore(self.store.root.parent)
        self.assertEqual(other.replay(self.kind, self.body), self.result)
        self.assertEqual(other.mutate(self.kind, self.body), self.result)
        self.assertEqual(other.read()["requests"][self.body["request_id"]]["result"], self.result)
        replay = other.replay(self.kind, self.body)
        replay["carousel"]["history"][0]["note"] = "caller mutation"
        self.assertEqual(other.replay(self.kind, self.body), self.result)
        with self.assertRaises(VoiceError) as caught:
            other.mutate(self.kind, {**self.body, "changed": True})
        self.assertEqual(caught.exception.status, 409)
        # Round-trip a public full read without expanding the stored index.
        other.write(other.read())
        self.assertLess(other.path.stat().st_size, 4096)
        self.assertEqual(len(list((other.root / "request-results").glob("*.json"))), 1)

    def test_corrupt_missing_and_escaped_payloads_stop_replay_without_reexecuting(self):
        self.store.write(self.data)
        saved = self.receipt()
        blob = self.store.root / saved["result_ref"]["file"]
        original = blob.read_bytes()
        with patch.object(self.store, "_carousel_generation_start", side_effect=AssertionError("must not execute again")):
            for damaged in (b"x" + original[1:], None):
                with self.subTest(damaged=damaged is not None):
                    if damaged is None:
                        blob.unlink()
                    else:
                        blob.write_bytes(damaged)
                    with self.assertRaises(VoiceError) as caught:
                        self.store.mutate(self.kind, self.body)
                    self.assertEqual(caught.exception.status, 500)
                    blob.write_bytes(original)
            raw = json.loads(self.store.path.read_text(encoding="utf-8"))
            raw["requests"][self.body["request_id"]]["result_ref"]["file"] = "../outside.json"
            self.store.path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(VoiceError):
                self.store.replay(self.kind, self.body)

    def test_failed_catalog_replace_keeps_original_and_safe_retry_uses_saved_blob(self):
        original = self.legacy_file()
        replacement = copy.deepcopy(self.data)
        replacement["carousels"][0]["title"] = "new business data"
        import os
        real_replace = os.replace

        def replace(source, destination):
            if Path(destination) == self.store.path:
                raise OSError("simulated catalog replace failure")
            return real_replace(source, destination)

        with patch("production_store.os.replace", side_effect=replace), self.assertRaises(OSError):
            self.store.write(replacement)
        self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.replay(self.kind, self.body), self.result)
        blobs = list((self.store.root / "request-results").glob("*.json"))
        self.assertEqual(len(blobs), 1)
        self.assertEqual(json.loads(blobs[0].read_text(encoding="utf-8")), self.result)
        self.store.write(replacement)
        self.assertEqual(self.store.records()["carousels"][0]["title"], "new business data")
        self.assertEqual(self.store.replay(self.kind, self.body), self.result)

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

    def test_external_receipts_keep_existing_age_compaction_and_expiry_policy(self):
        self.data["requests"][self.body["request_id"]]["at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        self.store.write(self.data)
        data = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertIn("result_ref", data["requests"][self.body["request_id"]])
        self.store._prune_requests(data)
        saved = data["requests"][self.body["request_id"]]
        self.assertTrue(saved["compact"])
        self.assertNotIn("result_ref", saved)
        self.assertEqual(self.store._replayed(data, saved)["carousel"], self.data["carousels"][0])
        saved["at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        self.store._prune_requests(data)
        self.assertNotIn(self.body["request_id"], data["requests"])


if __name__ == "__main__":
    unittest.main()
