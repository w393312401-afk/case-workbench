"""Cached media checks remain sensitive to tampering, access and declarations."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

import test_production_carousel_delivery as delivery_fixture
from elevenlabs_client import VoiceError
from media_integrity_cache import FileIntegrityCache, INTEGRITY_CACHE
from workbench_carousel_delivery import CarouselDeliveryMixin
from workbench_carousel_images import image_hash


class MediaIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="media-integrity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "image.png"
        self.path.write_bytes(b"the original picture")
        self.cache = FileIntegrityCache(max_entries=2)

    def test_unchanged_file_reuses_digest_without_retaining_media_bytes(self):
        with patch.object(self.cache, "_digest", wraps=self.cache._digest) as digest:
            first = self.cache.sha256(self.path)
            self.assertEqual(self.cache.sha256(self.path), first)
            self.assertEqual(digest.call_count, 1)
        self.assertEqual(len(self.cache._hashes), 1)
        self.assertIsInstance(next(iter(self.cache._hashes.values()))[1], str)

    def test_same_length_inplace_change_with_restored_mtime_invalidates_digest(self):
        first = self.cache.sha256(self.path)
        before = self.path.stat()
        self.path.write_bytes(b"the modified picture")
        self.assertEqual(self.path.stat().st_size, before.st_size)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        second = self.cache.sha256(self.path)
        self.assertNotEqual(first, second)
        self.assertEqual(second, hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_replacement_removal_and_permission_failure_do_not_reuse_success(self):
        self.cache.sha256(self.path)
        replacement = self.root / "replacement"
        replacement.write_bytes(b"another saved image")
        os.replace(replacement, self.path)
        self.assertEqual(self.cache.sha256(self.path), hashlib.sha256(self.path.read_bytes()).hexdigest())
        with patch.object(Path, "open", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.cache.sha256(self.path)
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.cache.sha256(self.path)

    def test_mutation_during_digest_does_not_cache_a_success(self):
        original = self.cache._digest
        def changed(stream):
            result = original(stream)
            self.path.write_bytes(b"mutated during digest")
            return result
        with patch.object(self.cache, "_digest", side_effect=changed):
            with self.assertRaises(OSError):
                self.cache.sha256(self.path)
        self.assertEqual(len(self.cache._hashes), 0)

    def test_successful_checks_are_bounded_and_declarations_are_independent(self):
        expected = hashlib.sha256(self.path.read_bytes()).hexdigest()
        validate = unittest.mock.Mock()
        self.cache.check(self.path, "declaration-a", expected, validate)
        self.cache.check(self.path, "declaration-a", expected, validate)
        self.assertEqual(validate.call_count, 1)
        self.cache.check(self.path, "declaration-b", expected, validate)
        self.cache.check(self.path, "declaration-c", expected, validate)
        self.assertEqual(validate.call_count, 3)
        self.assertEqual(len(self.cache._checks), 2)
        self.cache.check(self.path, "declaration-a", expected, validate)
        self.assertEqual(validate.call_count, 4)

    def test_hardlink_rejected_after_cache_warms(self):
        self.cache.sha256(self.path)
        alias = self.root / "alias.png"
        try:
            os.link(self.path, alias)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(OSError):
            self.cache.sha256(self.path)

    def test_image_hash_uses_fresh_cache_identity_and_rejects_removed_image(self):
        INTEGRITY_CACHE.clear()
        first = image_hash(self.path)
        self.path.write_bytes(b"new bytes")
        self.assertNotEqual(image_hash(self.path), first)
        self.path.unlink()
        with self.assertRaises(VoiceError):
            image_hash(self.path)


class DeliveryIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="delivery-integrity-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.identifier = "CAR-000000000001"
        self.delivery_id = "DLV-000000000001"
        self.folder = self.root / self.identifier / "deliveries" / self.delivery_id
        self.folder.mkdir(parents=True)
        self.path = self.folder / "fixture.zip"
        self.payload = b"fixture image content"
        with zipfile.ZipFile(self.path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("01.png", self.payload)
        self.record = {"id": self.delivery_id, "created_at": "2026-10-14", "script_id": "SCR-fixture", "script_version": 1,
            "aspect": "3:4", "caption_en": "Fixture caption", "caption_zh": "示例", "images_only": True,
            "slides": [{"file": "01.png", "number": 1, "sha256": hashlib.sha256(self.payload).hexdigest()}],
            "selection_signature": "fixture", "filename": self.path.name,
            "zip_file": f"deliveries/{self.delivery_id}/{self.path.name}",
            "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest()}
        self.bench = CarouselDeliveryMixin()
        self.bench._carousel_dir = lambda identifier: self.root / identifier
        INTEGRITY_CACHE.clear()

    def test_complete_zip_receipt_reuses_hash_and_archive_validation(self):
        with patch.object(INTEGRITY_CACHE, "_digest", wraps=INTEGRITY_CACHE._digest) as digest, \
                patch("workbench_carousel_delivery.zipfile.ZipFile", wraps=zipfile.ZipFile) as archive:
            self.assertEqual(self.bench._checked_delivery(self.identifier, self.record, complete=True), self.path)
            self.assertEqual(self.bench._checked_delivery(self.identifier, self.record, complete=True), self.path)
            self.assertEqual((digest.call_count, archive.call_count), (1, 1))

    def test_wrong_expected_slide_hash_cannot_borrow_valid_receipt(self):
        self.bench._checked_delivery(self.identifier, self.record, complete=True)
        wrong = copy.deepcopy(self.record)
        wrong["slides"][0]["sha256"] = "0" * 64
        with self.assertRaises(VoiceError):
            self.bench._checked_delivery(self.identifier, wrong, complete=True)
        self.assertEqual(self.bench._checked_delivery(self.identifier, self.record, complete=True), self.path)

    def test_tampered_zip_same_size_restored_mtime_and_corrupt_zip_are_rejected(self):
        self.bench._checked_delivery(self.identifier, self.record, complete=True)
        before = self.path.stat()
        payload = bytearray(self.path.read_bytes())
        payload[10] ^= 1
        self.path.write_bytes(payload)
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(VoiceError):
            self.bench._checked_delivery(self.identifier, self.record, complete=True)
        self.path.write_bytes(b"corrupt zip")
        wrong = copy.deepcopy(self.record)
        wrong["sha256"] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(VoiceError):
            self.bench._checked_delivery(self.identifier, wrong, complete=True)

    def test_simple_hash_check_cannot_substitute_for_complete_zip_check(self):
        self.path.write_bytes(b"not a valid zip")
        self.record["sha256"] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.bench._checked_delivery(self.identifier, self.record)
        with self.assertRaises(VoiceError):
            self.bench._checked_delivery(self.identifier, self.record, complete=True)

    def test_download_hash_does_not_hold_the_global_workbench_lock(self):
        self.bench.lock = threading.RLock()
        item = {"deliveries": [self.record]}
        class Production:
            def carousel(self, identifier):
                return item
        self.bench.production = Production()
        entered, release = threading.Event(), threading.Event()
        checked = self.bench._checked_delivery
        def block(*args, **kwargs):
            entered.set()
            release.wait(3)
            return checked(*args, **kwargs)
        with patch.object(self.bench, "_checked_delivery", side_effect=block):
            worker = threading.Thread(target=self.bench.carousel_delivery_file, args=(self.identifier, self.delivery_id))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.assertTrue(self.bench.lock.acquire(timeout=.2))
                self.bench.lock.release()
            finally:
                release.set()
                worker.join(3)


if __name__ == "__main__":
    unittest.main()
