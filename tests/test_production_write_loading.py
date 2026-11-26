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
from production_store import ProductionStore, public_job


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

    def test_job_context_projects_only_one_order_and_keeps_complete_options(self):
        identifier = "PRD-000000000001"
        data = self.store.empty()
        data["jobs"] = [
            {"id": identifier, "product": "202H", "title": "current", "lines": [], "edits": []},
            {"id": "PRD-000000000002", "product": "202H", "title": "other", "edits": [
                {"materials": ["素材甲", "素材乙"], "started_at": "2026-11-01"}]},
            {"id": "PRD-000000000003", "product": "129N", "title": "other product", "edits": [
                {"materials": ["别的产品素材"], "started_at": "2026-11-02"}]}]
        data["voices"] = [{"voice_id": "voice", "settings": {"stability": 0.5}}]
        data["defaults"] = {"202H": "voice"}
        data["readings"] = [{"product": "202H", "written": "202H", "spoken": "two oh two H"},
                            {"product": "129N", "written": "129N", "spoken": "one two nine N"}]
        data["settings"] = {"creators": ["Lab"], "products": {"202H": {"fps": "30"}}}
        self.store.write(data)
        full = self.store.public()
        with patch("production_store.public_job", wraps=public_job) as project:
            context = self.store.job_context(identifier)
        self.assertEqual([call.args[0]["id"] for call in project.call_args_list], [identifier])
        self.assertEqual(context["job"], full["jobs"][0])
        self.assertEqual(context["revision"], full["revision"])
        self.assertEqual(context["known_job_ids"], frozenset(item["id"] for item in full["jobs"]))
        self.assertEqual(context["readings"], [full["readings"][0]])
        self.assertEqual(context["voices"], full["voices"])
        self.assertEqual(context["defaults"], full["defaults"])
        self.assertEqual(context["creators"], full["settings"]["creators"])
        self.assertEqual(context["product_settings"], self.store.product_settings(full, "202H"))
        self.assertEqual(context["material_combos"], self.store.material_combos(full, "202H"))
        expected = copy.deepcopy(context)
        context["job"]["lines"].append({"spoken": "unsaved"})
        context["voices"][0]["settings"]["stability"] = 0.1
        context["defaults"].clear()
        context["readings"][0]["spoken"] = "unsaved"
        context["creators"].append("Unsaved")
        context["product_settings"]["fps"] = "60"
        context["material_combos"][0]["materials"].append("unsaved")
        self.assertEqual(self.store.job_context(identifier), expected)

    def test_job_context_sees_external_updates_and_deleted_counterparts(self):
        identifier = "PRD-000000000001"
        counterpart = "PRD-000000000002"
        data = self.store.empty()
        data["jobs"] = [{"id": identifier, "product": "202H", "title": "current", "edits": []},
                        {"id": counterpart, "product": "202H", "title": "other", "edits": []}]
        self.store.write(data)
        before = self.store.job_context(identifier)
        other = ProductionStore(self.store.root.parent)
        changed = other.read()
        changed["jobs"].pop()
        changed["jobs"][0]["title"] = "updated"
        other.write(changed)
        after = self.store.job_context(identifier)
        self.assertEqual(after["job"]["title"], "updated")
        self.assertNotIn(counterpart, after["known_job_ids"])
        self.assertNotEqual(after["revision"], before["revision"])
        self.assertEqual(after["revision"], self.store.current_revision())
        with self.assertRaises(VoiceError) as caught:
            self.store.job_context(counterpart)
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
