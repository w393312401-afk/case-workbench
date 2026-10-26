"""Publication build checks using isolated, disposable local fixtures only."""
from __future__ import annotations

from contextlib import redirect_stdout
import copy
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
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
            '<html><head><title>TK工作台</title>'
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

    def test_product_catalog_is_authoritative_without_removing_any_cases(self):
        bundle = self.bundle(second_case=True)
        original_cases = copy.deepcopy(bundle["cases"])
        for visible in (["198H"], []):
            with self.subTest(products=visible):
                bundle["product_catalog"] = {"products": visible, "removed": ["hidden product"], "deleted": ["202H"]}
                self.build(copy.deepcopy(bundle))
                manifest = self.manifest()
                self.assertEqual(manifest["products"], visible)
                self.assertEqual(manifest["product_catalog"], bundle["product_catalog"])
                self.assertEqual(manifest["state"]["cases"], bundle["state"]["cases"])
                saved = json.loads((self.site.parent / "text-snapshot.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["product_catalog"], bundle["product_catalog"])
                self.assertEqual(saved["cases"], original_cases)
                for identifier, case in original_cases.items():
                    fragment = json.loads((self.site / "data" / f"case-{identifier}.json").read_text(encoding="utf-8"))
                    self.assertEqual(fragment["cases"][identifier], case)

    def test_export_reads_catalog_and_rejects_catalog_changes_during_export(self):
        bundle = self.bundle()
        responses = {
            "/api/state": bundle["state"], "/api/products": {"products": [], "removed": [], "deleted": ["202H"]},
            "/api/reviews/summary": {}, "/api/reviews/picks": {"picks": []}, "/api/reviews/tag-edits": {"edits": []},
            "/api/performance/summary": {}, "/api/performance/analytics": {}, "/api/performance/videos": {"videos": []},
            "/api/direction-catalog": bundle["direction_catalog"], "/api/directions": bundle["directions"],
            "/api/directions?archive=all": bundle["directions_all"],
            "/api/creation": {**bundle["creation"], "revision": "creation-1"}, "/api/materials": bundle["materials"],
            "/api/ai/tasks": {"tasks": []}, "/api/review-followups": {"entries": [], "revision": "followups-1"},
            "/api/cases/" + CASE_A: bundle["cases"][CASE_A],
        }
        for changing in (False, True):
            with self.subTest(changing_catalog=changing):
                catalog_reads = []
                def read(url, **unused):
                    path = url.removeprefix("http://fixture")
                    result = responses.get(path)
                    if path.startswith("/api/reviews?"):
                        result = {"reviews": [], "total": 0}
                    if path == "/api/products":
                        catalog_reads.append(path)
                        if changing and len(catalog_reads) > 1:
                            result = {"products": ["202H"], "removed": [], "deleted": []}
                    if result is None:
                        raise AssertionError("Unexpected fixture request: " + path)
                    return io.BytesIO(json.dumps(result).encode("utf-8"))
                with patch.object(exporter, "urlopen", side_effect=read), patch.object(exporter, "video_details", return_value={}), \
                        patch.object(exporter, "build_site") as build:
                    if changing:
                        with self.assertRaisesRegex(RuntimeError, "导出期间"):
                            exporter.export("http://fixture")
                        build.assert_not_called()
                    else:
                        exporter.export("http://fixture")
                        exported = build.call_args.args[0]
                        self.assertEqual(exported["product_catalog"], responses["/api/products"])
                        self.assertEqual(set(exported["cases"]), {CASE_A})
                        self.assertEqual({key: exported["cases"][CASE_A][key] for key in bundle["cases"][CASE_A]},
                                         bundle["cases"][CASE_A])
                self.assertEqual(len(catalog_reads), 2)

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

    def test_sales_details_use_real_normalization_without_persistent_database_changes(self):
        from performance_store import PerformanceStore, SCHEMA

        runtime = self.root / "runtime"
        database = runtime / "performance" / "performance.sqlite"
        database.parent.mkdir(parents=True)
        (self.root / "config.json").write_text(json.dumps({"runtime_root": str(runtime)}), encoding="utf-8")
        video_id = "7600000000000000001"
        connection = sqlite3.connect(database)
        try:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO periods (video_id, range_start, range_end, span_days, views, clicks, orders, items, gmv, source_file) VALUES (?, ?, ?, 7, ?, ?, ?, ?, ?, ?)",
                [(video_id, "2026-09-01", "2026-09-07", 700, 70, 7, 7, 70, "older.xlsx"),
                 (video_id, "2026-09-05", "2026-09-11", 900, 90, 9, 9, 90, "newer.xlsx")])
            connection.executemany(
                "INSERT INTO daily (video_id, date, views, clicks, orders, items, gmv, source_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(video_id, "2026-09-06", 999999, 999, 99, 99, 999, "covered.xlsx"),
                 (video_id, "2026-09-12", 50, 5, 1, 1, 5, "outside.xlsx")])
            connection.commit()
        finally:
            connection.close()
        before = {file.name: file.read_bytes() for file in database.parent.iterdir()}
        videos = [{"video_id": video_id, "status": "focus", "window_views": 950, "gmv": 95}]
        with patch.object(PerformanceStore, "connect", side_effect=AssertionError("Exporter must use its read-only connection")), \
                patch.object(exporter.sqlite3, "connect", wraps=sqlite3.connect) as connect:
            details = exporter.video_details(videos)
        connect.assert_called_once_with(database.as_uri() + "?mode=ro", uri=True)
        self.assertEqual(details[video_id]["series"], [
            {"start": "2026-09-05", "end": "2026-09-11", "views": 900.0, "clicks": 90.0, "orders": 9.0, "items": 9.0, "gmv": 90.0},
            {"start": "2026-09-12", "end": "2026-09-12", "views": 50.0, "clicks": 5.0, "orders": 1.0, "items": 1.0, "gmv": 5.0},
        ])
        self.assertEqual(details[video_id]["judged_videos"], 1)
        self.assertEqual(details[video_id]["gmv_top_pct"], 100)
        self.assertEqual(before, {file.name: file.read_bytes() for file in database.parent.iterdir()})

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
