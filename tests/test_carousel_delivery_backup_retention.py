"""Content-addressed backup retirement over temporary directories only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from carousel_delivery_backups import cleanup_delivery_backups
from elevenlabs_client import VoiceError
from production_store import ProductionStore

CAROUSEL = "CAR-0123456789ab"
OLD = "DLV-111111111111"
CURRENT = "DLV-222222222222"
REF = "request-results/" + "a" * 64 + ".json"


class DeliveryBackupRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="delivery-backup-retention-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "production"
        self.folder = self.root / "backups"
        self.folder.mkdir(parents=True)

    def data(self, *, include_current=False):
        data = ProductionStore.empty()
        old = {"id": OLD, "sha256": "1" * 64, "caption_en": "Exact caption.", "caption_zh": "原文案。",
            "source": {"slides": [{"number": 1, "title_lines": ["Original words."]}]},
            "slides": [{"number": 1, "output_id": "OUT-111111111111", "sha256": "1" * 64, "file": "01.png"}],
            "zip_file": "deliveries/" + OLD + "/old.zip", "filename": "old.zip"}
        publication = {"delivery_id": OLD, "delivery_sha256": old["sha256"], "post_id": "7999999999999999901"}
        data["carousels"] = [{"id": CAROUSEL, "deliveries": [old] + ([{"id": CURRENT}] if include_current else []),
            "publication": publication, "publication_history": [copy.deepcopy(publication)], "history": [{"note": "keep audit"}]}]
        data["jobs"] = [{"id": "PRD-0123456789ab", "voice": "keep", "takes": [1, 2]}]
        data["requests"] = {"old-package-request": {"result": {"carousel": {"id": CAROUSEL}, "delivery": {"id": OLD}},
                                                      "result_ref": {"file": REF}}}
        return data

    def backup(self, data, *, mtime_ns=1_700_000_000_123_456_700):
        encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path = self.folder / (hashlib.sha256(encoded).hexdigest() + ".json")
        path.write_bytes(encoded)
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def read_only_backup(self):
        files = list(self.folder.glob("*.json"))
        self.assertEqual(len(files), 1)
        path = files[0]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), path.stem)
        return path, json.loads(path.read_bytes())

    def test_rewrites_then_removes_old_hash_preserves_other_data_and_original_mtime(self):
        original = self.data(include_current=True)
        path = self.backup(original)
        before_size, before_time = path.stat().st_size, path.stat().st_mtime_ns
        receipt = self.root / REF
        receipt.parent.mkdir()
        receipt.write_text("must remain for global reference audit", encoding="utf-8")
        result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(path.exists())
        rewritten, data = self.read_only_backup()
        self.assertEqual(rewritten.stat().st_mtime_ns, before_time)
        self.assertEqual(data["jobs"], original["jobs"])
        self.assertEqual(data["carousels"][0]["deliveries"], [{"id": CURRENT}])
        publication = data["carousels"][0]["publication"]
        self.assertEqual(publication["delivery_id"], OLD)
        self.assertEqual(publication["delivery_snapshot"]["source"], original["carousels"][0]["deliveries"][0]["source"])
        self.assertNotIn("zip_file", publication["delivery_snapshot"])
        self.assertEqual(result["receipt_refs"], [REF])
        self.assertTrue(receipt.exists())
        self.assertEqual(result["bytes"], before_size - rewritten.stat().st_size)

    def test_dry_run_reports_same_plan_and_changes_no_files(self):
        path = self.backup(self.data())
        original = path.read_bytes()
        before_time = path.stat().st_mtime_ns
        result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD], dry_run=True)
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["receipt_refs"], [REF])
        self.assertEqual(list(self.folder.iterdir()), [path])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(path.stat().st_mtime_ns, before_time)
        actual = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual((actual["files"], actual["bytes"]), (result["files"], result["bytes"]))

    def test_historical_backup_can_have_no_surviving_package_and_cleanup_is_idempotent(self):
        self.backup(self.data())
        cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        path, data = self.read_only_backup()
        self.assertEqual(data["carousels"][0]["deliveries"], [])
        self.assertEqual(data["requests"], {})
        result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result, {"files": 0, "bytes": 0, "warnings": [], "receipt_refs": []})
        self.assertTrue(path.is_file())

    def test_corrupt_hash_invalid_json_and_non_hash_files_are_never_rewritten(self):
        mismatch = self.folder / ("0" * 64 + ".json")
        mismatch.write_text('{"unrelated":true}', encoding="utf-8")
        malformed = b"not JSON"
        invalid = self.folder / (hashlib.sha256(malformed).hexdigest() + ".json")
        invalid.write_bytes(malformed)
        other = self.folder / "manual-copy.json"
        other.write_text("keep me", encoding="utf-8")
        result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 2)
        self.assertTrue(all(path.is_file() for path in (mismatch, invalid, other)))

    def test_existing_clean_target_deduplicates_and_preserves_the_newest_source_date(self):
        original = self.data()
        path = self.backup(original, mtime_ns=1_700_000_000_000_000_000)
        cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        clean, _ = self.read_only_backup()
        newest = 1_710_000_000_000_000_000
        path = self.backup(original, mtime_ns=newest)
        old_size = path.stat().st_size
        result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["bytes"], old_size)
        self.assertEqual(clean.stat().st_mtime_ns, newest)
        self.assertFalse(path.exists())

    def test_symlinked_backup_is_skipped_and_reparse_folder_is_rejected(self):
        path = self.backup(self.data())
        original_lstat = Path.lstat
        def attributes(target):
            info = original_lstat(target)
            if target == path:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, "lstat", attributes):
            result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertTrue(path.exists())
        def folder_attributes(target):
            info = original_lstat(target)
            if target == self.folder:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, "lstat", folder_attributes), self.assertRaises(VoiceError):
            cleanup_delivery_backups(self.root, CAROUSEL, [OLD])

    def test_new_snapshot_write_failure_keeps_the_original_backup(self):
        path = self.backup(self.data())
        original_open = Path.open
        def open_file(target, mode="r", *args, **kwargs):
            if mode == "xb":
                raise OSError("fixture disk error")
            return original_open(target, mode, *args, **kwargs)
        with patch.object(Path, "open", open_file):
            result = cleanup_delivery_backups(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
