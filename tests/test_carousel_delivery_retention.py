"""Latest-only package cleanup must stay inside its disposable package directories."""
import copy
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))

from elevenlabs_client import VoiceError
from carousel_delivery_retention import delivery_folder, remove_delivery_folder
import test_production_carousel_delivery as delivery_fixture


class CarouselDeliveryRetentionTests(unittest.TestCase):
    setUp = delivery_fixture.CarouselDeliveryTests.setUp
    ready = delivery_fixture.CarouselDeliveryTests.ready
    create = delivery_fixture.CarouselDeliveryTests.create
    current = delivery_fixture.CarouselDeliveryTests.current

    def seed_history(self):
        """Install an older checked copy as a legacy state; never touch live user data."""
        self.ready()
        latest = self.create()["delivery"]
        latest_folder = self.bench.carousel_delivery_file(self.identifier, latest["id"]).parent
        old = copy.deepcopy(latest)
        old_id = "DLV-" + uuid4().hex[:12]
        old.update(id=old_id, created_at="2000-01-01T00:00:00+00:00",
                   filename=latest["filename"].replace(latest["id"], old_id))
        old["zip_file"] = f"deliveries/{old_id}/{old['filename']}"
        old_folder = latest_folder.parent / old_id
        shutil.copytree(latest_folder, old_folder)
        (old_folder / latest["filename"]).rename(old_folder / old["filename"])
        with self.bench.lock:
            catalog = self.bench.production.read()
            item = next(row for row in catalog["carousels"] if row["id"] == self.identifier)
            item["deliveries"] = [copy.deepcopy(old), copy.deepcopy(latest)]
            self.bench.production.write(catalog)
        return old, old_folder, latest, latest_folder

    @staticmethod
    def snapshot(folder):
        return {path: path.read_bytes() for path in folder.rglob("*") if path.is_file() and not path.is_symlink()}

    def assert_same_files(self, snapshot):
        for path, payload in snapshot.items():
            self.assertTrue(path.is_file(), str(path))
            self.assertEqual(path.read_bytes(), payload, str(path))

    def test_cleanup_removes_only_old_package_and_is_idempotent(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        selected = self.snapshot(latest_folder)
        protected = dict(self.fixture.originals)
        car_root = self.bench._carousel_dir(self.identifier)
        for name in ("outputs", "candidates"):
            protected.update(self.snapshot(car_root / name))
        old_files = self.snapshot(old_folder)
        result = self.bench.prune_carousel_deliveries(self.identifier)
        self.assertEqual(result["removed_ids"], [old["id"]])
        self.assertGreaterEqual(result["files"], len(old_files))
        self.assertGreaterEqual(result["bytes"], sum(map(len, old_files.values())))
        self.assertFalse(old_folder.exists())
        self.assertEqual([row["id"] for row in self.current()["deliveries"]], [latest["id"]])
        self.assert_same_files(selected)
        self.assert_same_files(protected)
        repeated = self.bench.prune_carousel_deliveries(self.identifier)
        self.assertEqual(repeated["removed_ids"], [])
        self.assertEqual((repeated["files"], repeated["bytes"]), (0, 0))
        self.assert_same_files(selected)
        self.assert_same_files(protected)

    def test_dry_run_reports_candidates_without_writing_or_deleting(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        before = self.snapshot(self.bench.production.root)
        result = self.bench.prune_carousel_deliveries(self.identifier, dry_run=True)
        self.assertEqual(result["removed_ids"], [old["id"]])
        self.assertGreater(result["files"], 0)
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(self.snapshot(self.bench.production.root), before)
        self.assertEqual([row["id"] for row in self.current()["deliveries"]], [old["id"], latest["id"]])
        self.assertTrue(old_folder.is_dir())
        self.assertTrue(latest_folder.is_dir())

    def test_global_cleanup_preserves_unrelated_carousels_and_latest(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        other = self.fixture.carousel()
        other_before = self.bench.production.carousel(other["id"])
        selected = self.snapshot(latest_folder)
        result = self.bench.prune_all_carousel_deliveries()
        self.assertIn(old["id"], result["removed_ids"])
        self.assertFalse(old_folder.exists())
        self.assert_same_files(selected)
        self.assertEqual(self.bench.production.carousel(other["id"]), other_before)

    def test_tampered_zip_path_never_deletes_the_target_outside_deliveries(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        outside = self.bench.production.root / "outside-user-document.txt"
        outside.write_bytes(b"unrelated bytes must survive")
        with self.bench.lock:
            catalog = self.bench.production.read()
            item = next(row for row in catalog["carousels"] if row["id"] == self.identifier)
            item["deliveries"][0]["zip_file"] = "../../outside-user-document.txt"
            self.bench.production.write(catalog)
        selected = self.snapshot(latest_folder)
        try:
            result = self.bench.prune_carousel_deliveries(self.identifier)
        except (VoiceError, ValueError):
            pass
        else:
            self.assertTrue(result["warnings"], "a tampered record must be rejected explicitly")
        self.assertEqual(outside.read_bytes(), b"unrelated bytes must survive")
        self.assert_same_files(selected)

    def test_invalid_delivery_identifier_cannot_target_a_sibling_directory(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        outside = self.bench.production.root / "must-survive"
        outside.mkdir()
        sentinel = outside / "document.txt"
        sentinel.write_bytes(b"never traverse a record identifier")
        with self.bench.lock:
            catalog = self.bench.production.read()
            item = next(row for row in catalog["carousels"] if row["id"] == self.identifier)
            item["deliveries"][0]["id"] = "../../../../must-survive"
            item["deliveries"][0]["zip_file"] = "../../../../must-survive/document.txt"
            self.bench.production.write(catalog)
        try:
            result = self.bench.prune_carousel_deliveries(self.identifier)
        except (VoiceError, ValueError):
            pass
        else:
            self.assertTrue(result["warnings"])
        self.assertEqual(sentinel.read_bytes(), b"never traverse a record identifier")
        self.assertTrue(latest_folder.exists())

    def test_symbolic_link_package_is_refused_without_following_its_target(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        outside = self.bench.production.root / "external-package"
        shutil.copytree(old_folder, outside)
        protected = self.snapshot(outside)
        # Only remove the known, fixture-owned package copy when installing the test link.
        self.assertTrue(old_folder.resolve().is_relative_to(self.bench.production.root.resolve()))
        shutil.rmtree(old_folder)
        try:
            old_folder.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest("directory symlinks unavailable: " + str(exc))
        selected = self.snapshot(latest_folder)
        try:
            result = self.bench.prune_carousel_deliveries(self.identifier)
        except (VoiceError, ValueError):
            pass
        else:
            self.assertTrue(result["warnings"], "a linked package must not be traversed")
        self.assert_same_files(protected)
        self.assert_same_files(selected)

    def test_failed_replacement_keeps_old_package_until_new_one_is_checked(self):
        self.ready()
        old = self.create()["delivery"]
        old_path = self.bench.carousel_delivery_file(self.identifier, old["id"])
        old_bytes = old_path.read_bytes()
        self.fixture.generate(slides=[1])
        self.fixture.approve(1, self.current()["slides"][0]["outputs"][-1]["id"])
        with patch("workbench_carousel_delivery.zipfile.ZipFile", side_effect=OSError("simulated full disk")):
            with self.assertRaises(VoiceError):
                self.create()
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertEqual([row["id"] for row in self.current()["deliveries"]], [old["id"]])
        for path, payload in self.fixture.originals.items():
            self.assertEqual(path.read_bytes(), payload)

    def test_cleanup_keeps_old_package_when_the_newest_zip_is_corrupt(self):
        old, old_folder, latest, latest_folder = self.seed_history()
        old_files = self.snapshot(old_folder)
        (latest_folder / latest["filename"]).write_bytes(b"incomplete new archive")
        try:
            result = self.bench.prune_carousel_deliveries(self.identifier)
        except (VoiceError, ValueError):
            pass
        else:
            self.assertTrue(result["warnings"], "an unchecked newest package cannot authorize deleting the older one")
        self.assertTrue(old_folder.is_dir())
        self.assert_same_files(old_files)
        self.assertIn(old["id"], [row["id"] for row in self.current()["deliveries"]])


class DeliveryPathBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="delivery-path-boundary-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.carousel = "CAR-0123456789ab"
        self.delivery = "DLV-0123456789ab"
        self.folder = self.root / "carousels" / self.carousel / "deliveries" / self.delivery
        self.folder.mkdir(parents=True)
        self.sentinel = self.folder / "01.png"
        self.sentinel.write_bytes(b"protected while a reparse point is present")

    def mark_reparse(self, flagged):
        original = Path.lstat

        def lstat(path, *args, **kwargs):
            actual = original(path, *args, **kwargs)
            if path == flagged:
                return SimpleNamespace(st_mode=actual.st_mode,
                    st_file_attributes=getattr(actual, "st_file_attributes", 0) | stat.FILE_ATTRIBUTE_REPARSE_POINT)
            return actual

        return patch.object(Path, "lstat", lstat)

    def test_windows_reparse_attribute_on_package_blocks_removal_even_without_symlink_privilege(self):
        with self.mark_reparse(self.folder), self.assertRaises(VoiceError):
            remove_delivery_folder(self.root, self.carousel, self.delivery)
        self.assertEqual(self.sentinel.read_bytes(), b"protected while a reparse point is present")

    def test_nested_reparse_attribute_blocks_entire_package_removal(self):
        nested = self.folder / "unexpected-junction"
        nested.mkdir()
        (nested / "user-file.txt").write_bytes(b"never traverse this directory")
        with self.mark_reparse(nested), self.assertRaises(VoiceError):
            remove_delivery_folder(self.root, self.carousel, self.delivery)
        self.assertTrue(self.sentinel.is_file())
        self.assertEqual((nested / "user-file.txt").read_bytes(), b"never traverse this directory")

    def test_invalid_identifiers_are_rejected_before_resolving_any_outside_path(self):
        for car, delivery in (("../outside", self.delivery), (self.carousel, "../../outside")):
            with self.subTest(carousel=car, delivery=delivery), self.assertRaises(VoiceError):
                delivery_folder(self.root, car, delivery)
        self.assertTrue(self.sentinel.is_file())


if __name__ == "__main__":
    unittest.main()
