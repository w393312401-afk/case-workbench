"""Completed publication replay cleanup in disposable runtime directories."""
import copy
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

from carousel_delivery_operations import cleanup_delivery_operations, operation_delivery_history
from elevenlabs_client import VoiceError

CAROUSEL = "CAR-0123456789ab"
OLD = "DLV-111111111111"
LATEST = "DLV-222222222222"


class DeliveryOperationRetentionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="delivery-operation-retention-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "production"
        self.root.mkdir()
        self.folder = self.root.parent / "creation" / "operations"
        self.folder.mkdir(parents=True)

    def record(self, request_id="publication-fixture-001", **fields):
        package = {"id": OLD, "sha256": "1" * 64, "caption_en": "Exact caption.",
            "source": {"slides": [{"number": 1, "title_lines": ["Frozen title."]}]},
            "slides": [{"number": 1, "output_id": "OUT-111111111111", "sha256": "1" * 64, "file": "01.png"}],
            "zip_file": "deliveries/" + OLD + "/old.zip", "filename": "old.zip"}
        publication = {"id": "PUB-0123456789ab", "delivery_id": OLD, "delivery_sha256": "1" * 64,
                       "post_id": "7999999999999999901", "published_on": "2026-10-11"}
        return {"kind": "carousel_publication", "complete": True, "hash": "request-hash-remains",
            "request_id": request_id, "publication": {**copy.deepcopy(publication), "expected_publication": "old-hash"},
            "production_body": {"request_id": request_id, "revision": "original-revision", "carousel_id": CAROUSEL},
            "script_body": {"request_id": "script-update-fixture-001", "script_status": {"status": "published"}},
            "result": {"publication": copy.deepcopy(publication), "script": {"id": "SCR-0123456789ab", "status": "published"},
                "carousel": {"id": CAROUSEL, "deliveries": [package, {"id": LATEST, "sha256": "2" * 64}],
                    "publication": copy.deepcopy(publication), "publication_history": [copy.deepcopy(publication)],
                    "slides": [{"number": 1, "outputs": [{"id": "OUT-111111111111",
                        "stages": {"package": {"delivery_id": OLD}, "review": {"status": "pass"}}}]}]}}, **fields}

    def save(self, data):
        path = self.folder / (data["request_id"] + ".json")
        path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        stamp = 1_700_000_000_000_000_000
        os.utime(path, ns=(stamp, stamp))
        return path

    def test_cleanup_keeps_publication_facts_hash_bodies_and_completed_state(self):
        original = self.record()
        path = self.save(original)
        size, mtime = path.stat().st_size, path.stat().st_mtime_ns
        result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
        saved = json.loads(path.read_bytes())
        self.assertEqual(result["files"], 1)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["bytes"], size - path.stat().st_size)
        self.assertEqual(path.stat().st_mtime_ns, mtime)
        for key in ("publication", "production_body", "script_body", "hash", "complete", "request_id", "kind"):
            self.assertEqual(saved[key], original[key], key)
        carousel = saved["result"]["carousel"]
        self.assertEqual(carousel["deliveries"], [{"id": LATEST, "sha256": "2" * 64}])
        self.assertNotIn("package", carousel["slides"][0]["outputs"][0]["stages"])
        self.assertEqual(carousel["slides"][0]["outputs"][0]["stages"]["review"], {"status": "pass"})
        publication = saved["result"]["publication"]
        self.assertEqual((publication["delivery_id"], publication["delivery_sha256"]), (OLD, "1" * 64))
        self.assertEqual(publication["delivery_snapshot"], carousel["publication"]["delivery_snapshot"])
        self.assertNotIn("zip_file", publication["delivery_snapshot"])
        self.assertEqual(publication["delivery_snapshot"]["source"], original["result"]["carousel"]["deliveries"][0]["source"])
        self.assertEqual(cleanup_delivery_operations(self.root, CAROUSEL, [OLD]), {"files": 0, "bytes": 0, "warnings": []})

    def test_dry_run_and_history_read_are_both_read_only(self):
        data = self.record()
        path = self.save(data)
        original = path.read_bytes()
        history = operation_delivery_history(self.root, CAROUSEL, LATEST)
        self.assertEqual(history, data["result"]["carousel"]["deliveries"][:1])
        result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD], dry_run=True)
        self.assertEqual(result["files"], 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_unfinished_wrong_kind_and_other_tasks_are_untouched_and_history_deduplicates(self):
        paths = []
        for index, changes in enumerate(({"complete": False}, {"kind": "script_case"}, {})):
            data = self.record("publication-other-" + str(index), **changes)
            if index == 2:
                data["result"]["carousel"]["id"] = "CAR-ffffffffffff"
            paths.append(self.save(data))
        selected = self.record()
        selected["result"]["carousel"]["deliveries"] += [{"id": OLD}, {"id": "not-a-package"}]
        first = self.save(selected)
        second = self.save(self.record("publication-fixture-002"))
        before = {path: path.read_bytes() for path in paths}
        self.assertEqual([entry["id"] for entry in operation_delivery_history(self.root, CAROUSEL, LATEST)], [OLD])
        result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 2)
        self.assertTrue(first.exists() and second.exists())
        for path in paths:
            self.assertEqual(path.read_bytes(), before[path])

    def test_wrong_publication_identity_does_not_receive_another_posts_snapshot(self):
        data = self.record()
        data["result"]["publication"]["delivery_sha256"] = "f" * 64
        path = self.save(data)
        cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
        result = json.loads(path.read_bytes())["result"]
        self.assertNotIn("delivery_snapshot", result["publication"])
        self.assertEqual(result["publication"]["delivery_sha256"], "f" * 64)
        self.assertIn("delivery_snapshot", result["carousel"]["publication"])

    def test_file_changed_during_cleanup_is_never_overwritten(self):
        data = self.record()
        path = self.save(data)
        concurrent = {**data, "concurrent_update": "must survive"}
        def change_original(_):
            path.write_text(json.dumps(concurrent), encoding="utf-8")
        with patch("carousel_delivery_operations.os.fsync", side_effect=change_original):
            result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(json.loads(path.read_bytes()), concurrent)
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_replace_failure_keeps_original_journal_and_removes_own_temporary_file(self):
        path = self.save(self.record())
        original = path.read_bytes()
        with patch("carousel_delivery_operations.os.replace", side_effect=OSError("fixture replace error")):
            result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [path])

    def test_reparse_files_are_skipped_and_reparse_parent_is_rejected(self):
        path = self.save(self.record())
        original_lstat = Path.lstat
        def file_info(target):
            info = original_lstat(target)
            if target == path:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, "lstat", file_info):
            result = cleanup_delivery_operations(self.root, CAROUSEL, [OLD])
            self.assertEqual(operation_delivery_history(self.root, CAROUSEL, LATEST), [])
        self.assertEqual(result["files"], 0)
        self.assertEqual(len(result["warnings"]), 1)
        def parent_info(target):
            info = original_lstat(target)
            if target == self.folder.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return info
        with patch.object(Path, "lstat", parent_info), self.assertRaises(VoiceError):
            cleanup_delivery_operations(self.root, CAROUSEL, [OLD])


if __name__ == "__main__":
    unittest.main()
