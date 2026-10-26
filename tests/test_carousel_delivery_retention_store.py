"""Latest-only package identities in disposable production stores; no live files or services."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_store import ProductionStore, scrub_retired_delivery_data


class CarouselDeliveryRetentionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="delivery-retention-store-")
        self.addCleanup(temporary.cleanup)
        self.store = ProductionStore(temporary.name)
        self.identifier = "CAR-0123456789ab"
        self.ids = ["DLV-" + str(number) * 12 for number in (1, 2, 3)]
        self.outputs = ["OUT-" + str(number) * 12 for number in (1, 2, 3)]
        self.generation = "IMG-0123456789ab"
        self.signature = "selection-signature"
        data = self.store.empty()
        data["carousels"].append({"id": self.identifier, "script_id": "SCR-0123456789ab", "script_version": 1,
            "archived": False, "history": [], "deliveries": [], "image_aspect": "3:4", "product": "202H",
            "slides": [{"number": 1, "title_lines": ["Exact title", "Keep punctuation!"], "body": "Exact body.",
                "outputs": [{"id": identifier, "generation_id": self.generation, "stages": {}}
                            for identifier in self.outputs]}],
            "generations": [{"id": self.generation, "stages": {"1": {}}}], "autopilot_runs": []})
        self.store.write(data)
        self.addCleanup(patch.stopall)
        patch.object(ProductionStore, "_carousel_status", return_value="approved").start()
        patch("workbench_carousel_delivery.carousel_selection_signature", return_value=self.signature).start()

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.store.current_revision(),
                "carousel_id": self.identifier, **values}

    def record(self, index=0):
        identifier = self.ids[index]
        return {"id": identifier, "created_at": now(), "script_id": "SCR-0123456789ab", "script_version": 1,
            "sha256": str(index + 1) * 64, "aspect": "3:4", "selection_signature": self.signature,
            "caption_en": "Exact caption.", "caption_zh": "原文案。", "filename": identifier + ".zip",
            "zip_file": "deliveries/" + identifier + "/" + identifier + ".zip",
            "source": {"kind": "carousel", "carousel_id": self.identifier,
                       "script": {"script_id": "SCR-0123456789ab", "script_version": 1},
                       "slides": [{"number": 1, "title_lines": ["Exact title", "Keep punctuation!"], "body": "Exact body."}]},
            "slides": [{"number": 1, "output_id": self.outputs[index], "sha256": str(index + 1) * 64, "file": "01.png"}],
            "packaging": {"status": "completed", "finished_at": now()}}

    def create(self, index=0, body=None):
        return self.store.mutate("carousel_delivery", body or self.body(), self.record(index))

    def current(self):
        return self.store.carousel(self.identifier)

    def seed_legacy(self):
        data = self.store.read()
        item = data["carousels"][0]
        item["deliveries"] = [self.record(index) for index in range(3)]
        item["publication"] = {"id": "PUB-0123456789ab", "delivery_id": self.ids[0], "delivery_sha256": "1" * 64,
                               "post_id": "7999999999999999901"}
        item["publication_history"] = [copy.deepcopy(item["publication"])]
        item["autopilot_runs"] = [{"id": "AUT-0123456789ab", "delivery_id": self.ids[0],
                                  "result": {"delivery_id": self.ids[1]}}]
        self.store.write(data)
        return item

    def test_new_package_replaces_old_records_receipts_and_package_stages(self):
        first_body = self.body()
        first = self.create(body=first_body)["delivery"]
        collected_body = self.body(delivery_id=first["id"])
        self.store.mutate("carousel_collection", collected_body, {"collected_at": now()})
        # Publications keep their original package identity even after newer work.
        publication = {"id": "PUB-0123456789ab", "delivery_id": first["id"], "delivery_sha256": first["sha256"],
            "script_id": first["script_id"], "script_version": 1, "selection_signature": self.signature,
            "post_id": "7999999999999999901", "updated_at": now(), "expected_publication": fingerprint(None)}
        saved = self.store.mutate("carousel_publication", self.body(), publication)["publication"]
        self.assertEqual(saved["delivery_snapshot"]["source"], first["source"])
        result = self.create(1)
        current = self.current()
        self.assertEqual([entry["id"] for entry in current["deliveries"]], [self.ids[1]])
        self.assertEqual(result["removed_ids"], [self.ids[0]])
        self.assertIn(first_body["request_id"], result["removed_request_ids"])
        self.assertIn(collected_body["request_id"], result["removed_request_ids"])
        self.assertNotIn("package", current["slides"][0]["outputs"][0]["stages"])
        self.assertEqual(current["slides"][0]["outputs"][1]["stages"]["package"]["delivery_id"], self.ids[1])
        self.assertEqual(current["generations"][0]["stages"]["1"]["package"]["delivery_id"], self.ids[1])
        self.assertEqual(current["publication"], saved)
        self.assertEqual(current["publication_history"], [saved])
        self.assertNotIn("zip_file", saved["delivery_snapshot"])
        self.assertNotIn("filename", saved["delivery_snapshot"])
        self.assertNotIn("file", saved["delivery_snapshot"]["slides"][0])
        self.assertIsNone(self.store.replay("carousel_delivery", first_body))
        self.assertIsNone(self.store.replay("carousel_collection", collected_body))

    def test_prune_only_named_history_preserves_publication_and_is_idempotent(self):
        prior = self.seed_legacy()
        body = self.body(keep_id=self.ids[2], old_ids=self.ids[:2])
        result = self.store.mutate("carousel_delivery_prune", body)
        self.assertEqual([entry["id"] for entry in result["carousel"]["deliveries"]], [self.ids[2]])
        publication = result["carousel"]["publication"]
        self.assertEqual(publication["delivery_id"], self.ids[0])
        self.assertEqual(publication["delivery_sha256"], prior["deliveries"][0]["sha256"])
        self.assertEqual(publication["delivery_snapshot"]["source"], prior["deliveries"][0]["source"])
        self.assertEqual(result["carousel"]["publication_history"][0], publication)
        run = result["carousel"]["autopilot_runs"][0]
        self.assertNotIn("delivery_id", run)
        self.assertNotIn("delivery_id", run["result"])
        self.assertTrue(run["delivery_retired"])
        self.assertEqual(self.store.mutate("carousel_delivery_prune", body), result)
        replay_body = self.body(keep_id=self.ids[2], old_ids=self.ids[:2])
        repeated = self.store.mutate("carousel_delivery_prune", replay_body)
        self.assertEqual(repeated["removed_ids"], self.ids[:2], "file cleanup can safely retry named retired folders")
        self.assertEqual(repeated["carousel"]["history"], result["carousel"]["history"])

    def test_prune_rejects_wrong_latest_or_deleting_kept_identity_without_any_save(self):
        self.seed_legacy()
        for values, status in (({"keep_id": self.ids[0], "old_ids": self.ids[1:]}, 409),
                ({"keep_id": self.ids[2], "old_ids": self.ids}, 400),
                ({"keep_id": self.ids[2], "old_ids": ["../../catalog.json"]}, 400),
                ({"keep_id": self.ids[2], "old_ids": self.ids[0]}, 400)):
            with self.subTest(values=values):
                before = self.store.path.read_bytes()
                with self.assertRaises(VoiceError) as raised:
                    self.store.mutate("carousel_delivery_prune", self.body(**values))
                self.assertEqual(raised.exception.status, status)
                self.assertEqual(self.store.path.read_bytes(), before)

    def test_prune_removes_only_requested_ids_and_archived_history_can_be_cleaned(self):
        self.seed_legacy()
        data = self.store.read()
        data["carousels"][0]["archived"] = True
        self.store.write(data)
        result = self.store.mutate("carousel_delivery_prune", self.body(keep_id=self.ids[2], old_ids=self.ids[:1]))
        self.assertEqual([entry["id"] for entry in result["carousel"]["deliveries"]], self.ids[1:])

    def test_reuse_existing_package_retires_the_other_versions(self):
        self.seed_legacy()
        data = self.store.read()
        data["carousels"][0]["slides"][0]["outputs"][0]["stages"]["package"] = {"delivery_id": self.ids[1]}
        self.store.write(data)
        result = self.store.mutate("carousel_delivery", self.body(), self.record(0))
        self.assertEqual([entry["id"] for entry in result["carousel"]["deliveries"]], self.ids[:1])
        self.assertEqual(result["removed_ids"], self.ids[1:])
        self.assertEqual(result["carousel"]["slides"][0]["outputs"][0]["stages"]["package"]["delivery_id"], self.ids[0])

    def test_retired_replay_is_blocked_even_when_receipt_cleanup_was_not_run(self):
        first_body = self.body()
        self.create(body=first_body)
        old_receipt = copy.deepcopy(self.store._read_catalog()["requests"][first_body["request_id"]])
        self.create(1)
        with self.assertRaises(VoiceError) as raised:
            self.store._replayed(self.store.records(), old_receipt, self.store.root)
        self.assertEqual(raised.exception.status, 409)

    def test_sidecar_receipts_are_dropped_and_unshared_refs_returned_without_deleting_files(self):
        self.seed_legacy()
        data = self.store.read()
        snapshot = copy.deepcopy(data["carousels"][0])
        snapshot["large_fixture"] = "irrelevant" * 3000
        key = "sidecar-history-fixture"
        data["requests"][key] = {"hash": fingerprint(key), "at": now(), "result": {"carousel": snapshot}}
        self.store.write(data)
        raw = json.loads(self.store.path.read_text(encoding="utf-8"))
        ref = raw["requests"][key]["result_ref"]["file"]
        result = self.store.mutate("carousel_delivery_prune", self.body(keep_id=self.ids[2], old_ids=self.ids[:2]))
        self.assertIn(key, result["removed_request_ids"])
        self.assertIn(ref, result["receipt_refs"])
        self.assertTrue((self.store.root / ref).is_file(), "only the file layer removes sidecars after catalog commit")

    def test_failed_persistence_leaves_old_record_and_receipts_available(self):
        body = self.body()
        self.create(body=body)
        before = self.store.path.read_bytes()
        with patch.object(self.store, "write", side_effect=OSError("fixture persistence failure")):
            with self.assertRaises(OSError):
                self.create(1)
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.replay("carousel_delivery", body)["delivery"]["id"], self.ids[0])

    def test_backup_scrubber_needs_no_surviving_package_and_leaves_unrelated_business_data(self):
        self.seed_legacy()
        data = self.store.read()
        item = data["carousels"][0]
        item["deliveries"] = item["deliveries"][:1]
        unrelated = {"id": "CAR-ffffffffffff", "deliveries": [{"id": self.ids[1]}], "arbitrary": [1, 2, 3]}
        data["carousels"].append(copy.deepcopy(unrelated))
        data["jobs"] = [{"id": "PRD-0123456789ab", "important": "keep all unrelated jobs"}]
        data["requests"]["backup-old-delivery"] = {"hash": "fixture", "result": {"delivery": {"id": self.ids[0]},
            "carousel": {"id": self.identifier}}, "result_ref": {"file": "request-results/" + "f" * 64 + ".json"}}
        before_file = self.store.path.read_bytes()
        result = scrub_retired_delivery_data(data, self.identifier, self.ids[:2])
        self.assertTrue(result["changed"])
        self.assertEqual(item["deliveries"], [])
        self.assertEqual(item["publication"]["delivery_snapshot"]["id"], self.ids[0])
        self.assertEqual(data["carousels"][1], unrelated)
        self.assertEqual(data["jobs"], [{"id": "PRD-0123456789ab", "important": "keep all unrelated jobs"}])
        self.assertEqual(self.store.path.read_bytes(), before_file, "the scrubber is pure in-memory data processing")
        self.assertEqual(result["receipt_refs"], ["request-results/" + "f" * 64 + ".json"])
        again = scrub_retired_delivery_data(data, self.identifier, self.ids[:2])
        self.assertFalse(again["changed"])
        self.assertEqual(again["receipt_refs"], [])

    def test_scrubber_does_not_return_a_sidecar_still_used_by_an_unrelated_receipt(self):
        self.seed_legacy()
        data = self.store.read()
        ref = {"file": "request-results/" + "a" * 64 + ".json"}
        data["requests"]["retired-receipt"] = {"result": {"carousel": {"id": self.identifier}}, "result_ref": ref}
        data["requests"]["unrelated-receipt"] = {"result": {"job": {"id": "PRD-0123456789ab"}}, "result_ref": ref}
        result = scrub_retired_delivery_data(data, self.identifier, self.ids[:2])
        self.assertEqual(result["receipt_refs"], [])
        self.assertIn("unrelated-receipt", data["requests"])

    def test_scrubber_removes_orphaned_replays_even_when_the_task_is_missing(self):
        data = self.store.empty()
        data["requests"]["old-receipt-only"] = {"result": {"carousel": {"id": self.identifier},
                                                          "delivery": {"id": self.ids[0]}}}
        result = scrub_retired_delivery_data(data, self.identifier, self.ids[:1])
        self.assertTrue(result["changed"])
        self.assertEqual(data["carousels"], [])
        self.assertEqual(data["requests"], {})


if __name__ == "__main__":
    unittest.main()
