"""Filesystem checks use disposable files only, never the user's media."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
from managed_video import ManagedVideoFiles, VideoFileError, safe_name

FIRST = "PVR-0123456789ab"
SECOND = "PVR-abcdef123456"


class FakeStore:
    def __init__(self, cases):
        self.cases = {case["case_id"]: case for case in cases}

    def read_index(self):
        return {"cases": [{"case_id": identifier} for identifier in self.cases]}

    def read_case(self, identifier):
        return self.cases[identifier]


class VideoFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="managed-video-tests-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = ManagedVideoFiles(self.root / "runtime")
        self.source = self.root / "原片 same name.mp4"
        self.data = bytes(range(256)) * 9000
        self.source.write_bytes(self.data)

    def test_video_copy_is_independent_reused_only_by_owner(self):
        first, created = self.files.copy_for_case(self.source, FIRST)
        self.assertTrue(created)
        self.assertEqual(first.read_bytes(), self.data)
        self.assertEqual(self.files.info(first, FIRST), {"managed": True, "owned": True, "temporary": False})
        same, created = self.files.copy_for_case(first, FIRST)
        self.assertEqual(same, first)
        self.assertFalse(created)
        second, created = self.files.copy_for_case(first, SECOND)
        self.assertTrue(created)
        self.assertNotEqual(first, second)
        self.assertEqual(self.files.owner(second), SECOND)
        self.source.unlink()
        first.unlink()
        self.assertEqual(second.read_bytes(), self.data)

    def test_same_named_sources_get_separate_complete_copies(self):
        first, _ = self.files.copy_for_case(self.source, FIRST)
        self.source.write_bytes(b"replacement")
        second, _ = self.files.copy_for_case(self.source, FIRST)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), self.data)
        self.assertEqual(second.read_bytes(), b"replacement")
        self.assertEqual(first.name, second.name)

    def test_upload_atomic_success_and_short_upload_cleanup(self):
        staged = self.files.stage(io.BytesIO(self.data), len(self.data), self.source.name)
        self.assertTrue(self.files.temporary(staged))
        self.assertEqual(staged.read_bytes(), self.data)
        before = set(self.files.imports.iterdir())
        with self.assertRaisesRegex(VideoFileError, "中断"):
            self.files.stage(io.BytesIO(b"short"), 100, "broken.mp4")
        self.assertEqual(set(self.files.imports.iterdir()), before)
        self.assertFalse(list(self.files.imports.rglob(".uploading")))

    def test_write_error_leaves_no_partial_video(self):
        with patch("managed_video.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.files.copy_for_case(self.source, FIRST)
        self.assertEqual(list(self.files.cases.rglob("*.mp4")), [])
        self.assertEqual(list(self.files.cases.rglob(".uploading")), [])
        self.assertEqual(self.source.read_bytes(), self.data)

    def test_source_change_during_copy_rolls_back_its_new_file(self):
        original_write = self.files._write

        def change_source(*args):
            result = original_write(*args)
            with self.source.open("ab") as stream:
                stream.write(b"change")
            return result

        with patch.object(self.files, "_write", side_effect=change_source):
            with self.assertRaisesRegex(VideoFileError, "变化"):
                self.files.copy_for_case(self.source, FIRST)
        self.assertEqual(list(self.files.cases.rglob("*.mp4")), [])

    def test_cleanup_does_not_remove_current_history_or_original_references(self):
        for position in ("current", "history", "original", "source_case"):
            with self.subTest(position=position):
                staged = self.files.stage(io.BytesIO(b"clip"), 4, "clip.mp4")
                current = {"assets": {"video": str(staged)}}
                case = {"case_id": FIRST, "events": [], "original": {}}
                if position == "current":
                    case["current"] = current
                elif position == "history":
                    case["events"].append({"current": current})
                elif position == "original":
                    case["original"]["source"] = str(staged)
                else:
                    case["current"] = {"source_case": {"file_path": str(staged)}}
                self.assertFalse(self.files.cleanup_stage(staged, FakeStore([case])))
                self.assertTrue(staged.exists())
                self.assertTrue(self.files.cleanup_stage(staged, FakeStore([])))
                self.assertFalse(staged.exists())
                self.assertFalse(staged.parent.exists())

    def test_cleanup_never_removes_external_or_case_owned_sources(self):
        owned, _ = self.files.copy_for_case(self.source, FIRST)
        for source in (self.source, owned):
            self.assertFalse(self.files.cleanup_stage(source, FakeStore([])))
            self.assertTrue(source.exists())
        with self.assertRaises(VideoFileError):
            self.files.remove_created(owned, SECOND)
        with self.assertRaises(VideoFileError):
            self.files.remove_created(self.source, FIRST)

    def test_hardlinked_owned_file_gets_independent_copy_and_stage_is_preserved(self):
        owned, _ = self.files.copy_for_case(self.source, FIRST)
        shared = self.root / "hardlinked.mp4"
        try:
            os.link(owned, shared)
        except OSError:
            self.skipTest("Host does not permit hardlinks")
        independent, created = self.files.copy_for_case(owned, FIRST)
        self.assertTrue(created)
        self.assertNotEqual(independent, owned)
        self.assertEqual(independent.stat().st_nlink, 1)
        staged = self.files.stage(io.BytesIO(b"clip"), 4, "clip.mp4")
        os.link(staged, self.root / "staging-link.mp4")
        self.assertFalse(self.files.cleanup_stage(staged, FakeStore([])))
        self.assertTrue(staged.exists())

    def test_reject_empty_invalid_and_outside_destination(self):
        self.source.write_bytes(b"")
        with self.assertRaises(VideoFileError):
            self.files.copy_for_case(self.source, FIRST)
        with self.assertRaises(VideoFileError):
            self.files.copy_for_case(self.source, "../../elsewhere")
        with self.assertRaises(VideoFileError):
            self.files.safe(self.root / "outside")
        self.assertNotIn(":", safe_name("C:/elsewhere/name.mp4"))
        self.assertNotIn("/", safe_name("C:/elsewhere/name.mp4"))
        self.assertEqual(safe_name("CON.mp4"), "video_CON.mp4")

    def test_symlink_source_and_destination_are_rejected(self):
        link = self.root / "link.mp4"
        try:
            link.symlink_to(self.source)
        except OSError:
            self.skipTest("Host does not permit creating symlinks")
        with self.assertRaises(VideoFileError):
            self.files.copy_for_case(link, FIRST)
        (self.files.runtime / "evidence").mkdir(parents=True)
        self.files.imports.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(VideoFileError):
            self.files.stage(io.BytesIO(b"clip"), 4, "clip.mp4")


if __name__ == "__main__":
    unittest.main()
