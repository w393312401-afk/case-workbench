"""Immutable package contents, archive hashes and actual publication version binding."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_production_carousel_import as import_fixture
from direction_store import fingerprint
from workbench_carousel_delivery import CarouselDeliveryMixin, _manifest
from workbench_carousel_images import CarouselImagesMixin
from workbench_carousel_stills import CarouselStillsMixin


class TraceBench(import_fixture.Bench, CarouselImagesMixin, CarouselStillsMixin, CarouselDeliveryMixin):
    pass


class CarouselTraceabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = import_fixture.FinishedImportTests("test_import_cannot_claim_pending_review_as_confirmation")
        with patch.object(import_fixture, "Bench", TraceBench):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bench = self.fixture.bench
        self.identifier = self.fixture.identifier
        self.store = self.fixture.store

    def current(self):
        return self.fixture.current()

    def ready(self):
        outputs = self.fixture.import_images()["outputs"]
        for output in outputs:
            self.fixture.review(output)
            self.fixture.approve(output)
        return outputs

    def create(self):
        return self.bench.create_carousel_delivery(self.identifier, self.fixture.body())["delivery"]

    def test_package_contains_selected_bytes_locked_copy_reviews_and_external_hashes(self):
        outputs = self.ready()
        record = self.create()
        path = self.bench.carousel_delivery_file(self.identifier, record["id"])
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["packaging"]["status"], "completed")
        self.assertTrue(record["packaging"]["started_at"] <= record["packaging"]["finished_at"])
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest, _manifest(self.identifier, record))
            self.assertEqual(manifest["publication_status"], "unpublished_at_export")
            files = set(archive.namelist())
            self.assertEqual(files, {"01.png", "02.png", "caption_en.txt", "caption_zh.txt", "manifest.json", "领取说明.txt",
                "copy.json", "records/requirements.json", "records/reviews/01.json", "records/reviews/02.json"})
            for number, output in enumerate(outputs, 1):
                self.assertEqual(hashlib.sha256(archive.read(f"{number:02d}.png")).hexdigest(), output["sha256"])
                review = json.loads(archive.read(f"records/reviews/{number:02d}.json"))
                self.assertEqual(review["file_sha256"], output["sha256"])
                self.assertEqual(review["review"]["execution_status"], "completed")
                self.assertEqual(set(review["review"]["scope"]), {"visual", "text", "product", "evidence"})
                self.assertEqual(review["approval"]["actor"], "assistant_on_user_authorization")
                self.assertIsNone(review["provenance"]["model"])
            locked = json.loads(archive.read("copy.json"))
            self.assertEqual(locked["caption_en"], self.fixture.item["caption_en"])
            self.assertEqual(locked["slides"], [{key: slide[key] for key in ("number", "title_lines", "body", "title_zh", "body_zh")}
                                              for slide in self.fixture.item["slides"]])
            for entry in record["attachments"]:
                self.assertIn(entry["file"], files)
                self.assertEqual(hashlib.sha256(archive.read(entry["file"])).hexdigest(), entry["sha256"])
            for entry in manifest["external_archives"]:
                self.assertEqual(entry["kind"], "external_archive")
                self.assertFalse(entry["included_in_package"])
                self.assertTrue(Path(entry["path"]).is_absolute())
                self.assertEqual(hashlib.sha256(Path(entry["path"]).read_bytes()).hexdigest(), entry["sha256"])
        self.assertFalse(self.current().get("publication"))
        for slide in self.current()["slides"]:
            self.assertEqual(slide["outputs"][0]["stages"]["package"]["delivery_id"], record["id"])
            self.assertEqual(slide["outputs"][0]["stages"]["package"]["finished_at"], record["packaging"]["finished_at"])

    def _rewritten_record(self, record, mutate_payloads):
        value = copy.deepcopy(record)
        path = self.bench.carousel_delivery_file(self.identifier, record["id"])
        with zipfile.ZipFile(path) as archive:
            payloads = {name: archive.read(name) for name in archive.namelist()}
        mutate_payloads(payloads, value)
        payloads["manifest.json"] = (json.dumps(_manifest(self.identifier, value), ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in payloads.items():
                archive.writestr(name, payload)
        value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        return value

    def test_rehashed_package_still_rejects_changed_record_attachment(self):
        self.ready()
        record = self.create()
        changed = self._rewritten_record(record, lambda payloads, value: payloads.update({"copy.json": b'{"wrong":true}'}))
        self.fixture.error(409, self.bench._checked_delivery, self.identifier, changed, complete=True)

    def test_manifest_cannot_reference_missing_package_attachment(self):
        self.ready()
        record = self.create()
        def add_missing(payloads, value):
            value["attachments"].append({"file": "records/not-present.json", "sha256": "0" * 64})
        changed = self._rewritten_record(record, add_missing)
        self.fixture.error(409, self.bench._checked_delivery, self.identifier, changed, complete=True)

    def test_source_archive_change_rejects_new_package_and_retains_selected_images(self):
        outputs = self.ready()
        source = self.fixture.folder / "references" / "prompt-1.txt"
        source.write_text("Changed source prompt", encoding="utf-8")
        self.fixture.error(409, self.create)
        self.assertFalse(self.current().get("deliveries"))
        self.assertEqual(self.current()["slides"][0]["output_id"], outputs[0]["id"])
        self.assertTrue(self.current()["slides"][0]["outputs"][0]["approved"])

    def test_failed_recheck_removes_new_package_eligibility_but_old_package_remains(self):
        outputs = self.ready()
        record = self.create()
        original = self.bench.carousel_delivery_file(self.identifier, record["id"]).read_bytes()
        self.fixture.review(outputs[0], "needs_fix")
        self.fixture.error(409, self.create)
        live = self.bench.carousel_delivery_live(self.current())
        self.assertTrue(live["deliveries"][0]["stale"])
        self.assertEqual(self.bench.carousel_delivery_file(self.identifier, record["id"]).read_bytes(), original)

    def test_only_partial_review_or_first_slide_confirmation_cannot_package_full_group(self):
        outputs = self.fixture.import_images()["outputs"]
        self.fixture.review(outputs[0])
        self.fixture.approve(outputs[0])
        self.fixture.error(409, self.create)
        self.assertFalse(self.current().get("deliveries"))

    def test_managed_reference_change_blocks_delivery_without_changing_final_picture(self):
        outputs = self.ready()
        root = self.store.root / "carousels" / self.identifier
        reference = root / outputs[0]["product_reference_files"][0]["file"]
        reference.write_bytes(b"Changed reference bytes")
        self.fixture.error(409, self.create)
        self.assertEqual(hashlib.sha256((root / outputs[0]["file"]).read_bytes()).hexdigest(), outputs[0]["sha256"])

    def test_real_collection_timestamp_does_not_change_zip_or_publication(self):
        self.ready()
        record = self.create()
        original = self.bench.carousel_delivery_file(self.identifier, record["id"]).read_bytes()
        self.assertFalse(record.get("collected_at"))
        collected = self.bench.collect_carousel_delivery(self.identifier, record["id"])["delivery"]
        self.assertTrue(collected["collected_at"])
        self.assertEqual(collected["download_count"], 1)
        repeated = self.bench.collect_carousel_delivery(self.identifier, record["id"])["delivery"]
        self.assertEqual(repeated["download_count"], 2)
        self.assertEqual(self.bench.carousel_delivery_file(self.identifier, record["id"]).read_bytes(), original)
        self.assertFalse(self.current().get("publication"))

    def test_publication_is_bound_to_actual_package_version_in_isolated_store(self):
        self.ready()
        record = self.create()
        publication = {"id": "PUB-" + uuid4().hex[:12], "delivery_id": record["id"], "delivery_sha256": record["sha256"],
            "script_id": record["script_id"], "script_version": record["script_version"], "selection_signature": record["selection_signature"],
            "post_id": "100000000001", "published_on": "2026-10-11", "updated_at": "2026-10-11T12:00:00Z", "expected_publication": fingerprint(None)}
        for field in ("delivery_sha256", "script_id", "script_version", "selection_signature"):
            wrong = {**publication, field: "wrong version"}
            self.fixture.error(409, self.store.mutate, "carousel_publication", self.fixture.body(carousel_id=self.identifier), wrong)
        self.assertFalse(self.current().get("publication"))
        saved = self.store.mutate("carousel_publication", self.fixture.body(carousel_id=self.identifier), publication)["publication"]
        self.assertEqual(saved["delivery_id"], record["id"])
        self.assertEqual(saved["selection_signature"], record["selection_signature"])


if __name__ == "__main__":
    unittest.main()
