"""Verified package copies can be removed without deleting unrelated downloads."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from carousel_delivery_copies import cleanup_delivery_copies


class DeliveryCopyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="retired-delivery-copies-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.production = self.root / "production"
        self.carousel = "CAR-0123456789ab"
        self.identifier = "DLV-0123456789ab"
        self.filename = self.identifier + ".zip"
        folder = self.production / "carousels" / self.carousel / "deliveries" / self.identifier
        folder.mkdir(parents=True)
        self.source = folder / self.filename
        with zipfile.ZipFile(self.source, "w") as archive:
            archive.writestr("01.png", b"first exact original image")
            archive.writestr("02.jpg", b"second exact original image")
        self.record = {"id": self.identifier, "filename": self.filename,
                       "sha256": hashlib.sha256(self.source.read_bytes()).hexdigest()}
        self.downloads = self.root / "Downloads" / "Compressed"
        self.downloads.mkdir(parents=True)
        self.zip = self.downloads / self.filename
        self.zip.write_bytes(self.source.read_bytes())
        self.folder = self.downloads / self.identifier
        self.folder.mkdir()
        with zipfile.ZipFile(self.source) as archive:
            for name in archive.namelist():
                (self.folder / name).write_bytes(archive.read(name))

    def clean(self, **options):
        return cleanup_delivery_copies(self.production, self.carousel, [self.record],
                                       download_roots=[self.downloads], **options)

    def test_only_exact_zip_and_exact_extracted_folder_are_removed(self):
        source = self.source.read_bytes()
        unrelated = self.downloads / "family-photo.jpg"
        unrelated.write_bytes(b"keep my photo")
        result = self.clean()
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["files"], 3)
        self.assertFalse(self.zip.exists())
        self.assertFalse(self.folder.exists())
        self.assertEqual(self.source.read_bytes(), source)
        self.assertEqual(unrelated.read_bytes(), b"keep my photo")
        self.assertEqual(self.clean()["files"], 0)

    def test_preview_never_deletes_or_writes_any_file(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.clean(dry_run=True)["files"], 3)
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_folder_with_other_work_is_preserved_in_full(self):
        note = self.folder / "my-notes.txt"
        note.write_bytes(b"unrelated notes")
        result = self.clean()
        self.assertTrue(result["warnings"])
        self.assertTrue((self.folder / "01.png").exists())
        self.assertEqual(note.read_bytes(), b"unrelated notes")

    def test_modified_zip_with_same_name_is_preserved(self):
        self.zip.write_bytes(b"this is a different file")
        self.clean()
        self.assertEqual(self.zip.read_bytes(), b"this is a different file")


if __name__ == "__main__":
    unittest.main()
