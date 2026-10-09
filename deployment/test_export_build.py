"""Publication build checks using isolated, disposable local fixtures only."""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


spec = importlib.util.spec_from_file_location("export_build_under_test", Path(__file__).with_name("export_text_site.py"))
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)

CASE_A = "PVR-0123456789ab"
CASE_B = "PVR-1123456789ab"


class ExportBuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="case-workbench-export-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.support = self.root / "deployment" / "text-only"
        self.support.mkdir(parents=True)
        self.site = self.root / "deployment" / "site"
        static = self.root / "static"
        static.mkdir()
        (static / "index.html").write_text(
            '<html><head><title>案例工作台 · 视频参考库</title>'
            '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
            '</head><body>文字</body></html>', encoding="utf-8")
        (static / "app.js").write_text("function productFilterGroup(item) { return item.product; }", encoding="utf-8")
        (static / "style.css").write_text("body { color: black; }", encoding="utf-8")
        (static / "example.mp4").write_bytes(b"fixture-media-that-must-not-be-published")
        (self.root / "server.py").write_text("PRIVATE_SERVICE = True", encoding="utf-8")
        (self.root / "config.json").write_text('{"private":"fixture"}', encoding="utf-8")
        (self.root / "server.log").write_text("private fixture log", encoding="utf-8")
        for name, content in {
            "_worker.js": "export default { fetch() {} };",
            "public-mode.js": "window.PUBLIC_MODE = true;",
            "public-mode.css": "video,img { display:none; }",
        }.items():
            (self.support / name).write_text(content, encoding="utf-8")
        self.constants = patch.multiple(exporter, ROOT=self.root, SUPPORT=self.support, SITE=self.site)
        self.constants.start()
        self.addCleanup(self.constants.stop)

    def bundle(self, *, second_case=False, exported_at="2026-10-08T20:00:00+08:00"):
        ids = [CASE_A, CASE_B] if second_case else [CASE_A]
        cases = [{"case_id": identifier, "product": "SHARPAL 202H", "title": "Text case",
                  "archived": False, "current": {"title": "Text case", "assets": {}}} for identifier in ids]
        return {
            "meta": {"exported_at": exported_at, "mode": "read-only-text", "schema": 1},
            "state": {"app": "video-case-workbench", "token": "fixture-write-token", "signature": "local-signature",
                      "digest": "local-digest", "cases": cases, "navigation": {}, "paths": {}},
            "cases": {case["case_id"]: copy.deepcopy(case) for case in cases},
            "reviews": {"summary": {}, "items": [], "picks": [], "tag_edits": [], "videos": {}},
            "performance": {"summary": {}, "analytics": {}, "videos": [], "details": {}},
            "direction_catalog": {"directions": [], "selling_points": [], "video_links": []},
            "directions": {"groups": []}, "directions_all": {"groups": []}, "direction_details": {},
            "creation": {"scripts": [], "materials": [], "plans": [], "theme_links": []},
            "materials": {"items": [], "theme_links": []}, "scripts": {},
            "ai": {"status": {"available": False}, "options": {}, "tasks": [], "details": {}},
        }

    def build(self, bundle):
        with redirect_stdout(io.StringIO()):
            exporter.build_site(bundle)

    def manifest(self):
        return json.loads((self.site / "public-data.json").read_text(encoding="utf-8"))

    def published_bytes(self):
        result = {"site/" + file.relative_to(self.site).as_posix(): file.read_bytes()
                  for file in self.site.rglob("*") if file.is_file()}
        for name in ("text-snapshot.json", "export-report.json", "case-workbench-text.zip"):
            path = self.site.parent / name
            if path.exists():
                result[name] = path.read_bytes()
        return result

    def test_export_timestamp_alone_does_not_change_public_content_revision(self):
        self.build(self.bundle())
        first = self.manifest()
        next_bundle = self.bundle(exported_at="2026-10-08T20:05:00+08:00")
        next_bundle["state"].update(signature="different-local-signature", digest="different-local-digest", token="different-local-write-token")
        self.build(next_bundle)
        second = self.manifest()
        self.assertEqual(first["meta"]["content_revision"], second["meta"]["content_revision"])
        self.assertNotEqual(first["meta"]["exported_at"], second["meta"]["exported_at"])
        self.assertEqual(second["state"]["token"], "")

    def test_changed_public_text_or_frontend_source_changes_content_revision(self):
        self.build(self.bundle())
        first = self.manifest()["meta"]["content_revision"]
        changed = self.bundle()
        changed["cases"][CASE_A]["current"]["title"] = "Updated public title"
        self.build(changed)
        second = self.manifest()["meta"]["content_revision"]
        self.assertNotEqual(first, second)
        (self.support / "public-mode.js").write_text("window.PUBLIC_MODE = 'updated';", encoding="utf-8")
        self.build(changed)
        self.assertNotEqual(second, self.manifest()["meta"]["content_revision"])

    def test_deleted_cases_and_stale_media_are_absent_from_the_next_site_and_zip(self):
        self.build(self.bundle(second_case=True))
        self.assertTrue((self.site / "data" / f"case-{CASE_B}.json").exists())
        (self.site / "stale.mp4").write_bytes(b"stale media")
        self.build(self.bundle())
        self.assertFalse((self.site / "data" / f"case-{CASE_B}.json").exists())
        self.assertFalse((self.site / "stale.mp4").exists())
        with zipfile.ZipFile(self.site.parent / "case-workbench-text.zip") as archive:
            names = archive.namelist()
            self.assertNotIn(f"data/case-{CASE_B}.json", names)
            self.assertFalse(any(Path(name).suffix in {".mp4", ".py", ".log"} or name == "config.json" for name in names))
            self.assertIsNone(archive.testzip())

    def test_zip_write_failure_preserves_the_previous_site_and_all_artifacts(self):
        self.build(self.bundle())
        previous = self.published_bytes()
        with patch.object(exporter.zipfile.ZipFile, "write", side_effect=OSError("fixture ZIP write failure")):
            with self.assertRaisesRegex(OSError, "fixture ZIP write failure"):
                self.build(self.bundle(second_case=True))
        self.assertEqual(previous, self.published_bytes())
        self.assertFalse(list(self.site.parent.glob(".text-site-build-*")))

    def test_raw_path_or_credential_is_rejected_without_replacing_previous_package(self):
        self.build(self.bundle())
        previous = self.published_bytes()
        for unsafe in ("C:/Users/private/hidden.txt", r"C:\Users\private\hidden.txt", r"\\private-host\private-share\hidden.txt", "Bearer " + "x" * 24):
            with self.subTest(unsafe_type="path" if ":/" in unsafe else "credential"):
                bad = self.bundle()
                bad["cases"][CASE_A]["current"]["title"] = unsafe
                with self.assertRaisesRegex(RuntimeError, "本机路径或凭据"):
                    self.build(bad)
                self.assertEqual(previous, self.published_bytes())

    def test_clean_keeps_private_data_and_media_out_of_public_text(self):
        private = {
            "token": "fixture-token", "api_key": "short-secret", "password": "private", "thread_id": "private-thread",
            "source_file": "C:/Users/private/source.json", "assets": {"video": {"path": "C:/private.mp4"}},
            "sources": [{"raw": "private"}],
            "source_analysis": {"content": "Contact test@example.com; file C:/Users/private.txt; ![shot](https://example.com/photo.jpg); Bearer " + "x" * 24,
                                "output_path": "C:/private/output"},
            "nested": [{"authorization": "secret", "audio_file": "private.wav", "text": "public words"}],
        }
        cleaned = exporter.clean(private)
        for key in ("api_key", "password", "thread_id", "source_file"):
            self.assertNotIn(key, cleaned)
        self.assertEqual(cleaned["token"], "")
        self.assertEqual(cleaned["assets"], {})
        self.assertEqual(cleaned["sources"], [])
        self.assertEqual(cleaned["nested"], [{"text": "public words"}])
        serialized = json.dumps(cleaned, ensure_ascii=False)
        for omitted in ("test@example.com", "C:/Users", "photo.jpg", "x" * 24, "output_path"):
            self.assertNotIn(omitted, serialized)

    def test_final_artifact_commit_failure_preserves_the_entire_previous_publication(self):
        self.build(self.bundle())
        previous = self.published_bytes()
        original_replace = exporter.os.replace

        for name in ("site", "text-snapshot.json", "export-report.json", "case-workbench-text.zip"):
            with self.subTest(failed_artifact=name):
                def fail_install(source, target):
                    source, target = Path(source), Path(target)
                    if source.name == name and target == self.site.parent / name:
                        raise PermissionError("fixture final artifact replace failure")
                    return original_replace(source, target)

                with patch.object(exporter.os, "replace", side_effect=fail_install):
                    with self.assertRaisesRegex(PermissionError, "fixture final artifact replace failure"):
                        self.build(self.bundle(second_case=True))
                self.assertEqual(previous, self.published_bytes(), "A failed final commit must not mix a new site with old upload artifacts")
                self.assertFalse(list(self.site.parent.glob(".text-site-build-*")))

    def test_partial_backup_failure_restores_every_saved_artifact(self):
        self.build(self.bundle())
        previous = self.published_bytes()
        original_replace = exporter.os.replace

        def fail_backup(source, target):
            if Path(target).name == "previous-export-report.json":
                raise PermissionError("fixture backup replace failure")
            return original_replace(source, target)

        with patch.object(exporter.os, "replace", side_effect=fail_backup):
            with self.assertRaisesRegex(PermissionError, "fixture backup replace failure"):
                self.build(self.bundle(second_case=True))
        self.assertEqual(previous, self.published_bytes())

    def test_failed_rollback_preserves_a_recoverable_backup_instead_of_cleaning_it(self):
        self.build(self.bundle())
        previous_case = (self.site / "data" / f"case-{CASE_A}.json").read_bytes()
        original_replace = exporter.os.replace

        def fail_install_and_restore(source, target):
            source, target = Path(source), Path(target)
            if source.name == "case-workbench-text.zip" and target == self.site.parent / "case-workbench-text.zip":
                raise PermissionError("fixture install failure")
            if source.name == "previous-site" and target == self.site:
                raise PermissionError("fixture rollback failure")
            return original_replace(source, target)

        with patch.object(exporter.os, "replace", side_effect=fail_install_and_restore):
            with self.assertRaisesRegex(RuntimeError, "上一版备份保留"):
                self.build(self.bundle(second_case=True))
        backups = list(self.site.parent.glob(".text-site-build-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "previous-site" / "data" / f"case-{CASE_A}.json").read_bytes(), previous_case)


if __name__ == "__main__":
    unittest.main()
