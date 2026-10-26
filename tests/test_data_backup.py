"""Full-data import/export checks. All files and replacements stay in temporary roots."""
from __future__ import annotations

import hashlib
from contextlib import closing
import json
from pathlib import Path
import os
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from data_backup import BackupError, BackupManager


class DataBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="workbench-backup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill, self.runtime, self.app = (self.root / name for name in ("skill", "runtime", "app"))
        self.out = self.root / "archives"
        self.manager = BackupManager(self.skill, self.runtime, self.app)
        self.original = b'{"original": {"text": "exact  spaces\\nkeep Unicode \\u4e2d"}, "events": []}\n'
        self.write(self.skill / "cases" / "case.json", self.original)
        self.write(self.skill / "sources" / "source.md", b"source text\r\n")
        self.write(self.skill / "references" / "products" / "product.md", b"product facts")
        self.write(self.runtime / "creation" / "catalog.json", json.dumps({"schema_version": 1,
            "scripts": [], "materials": [], "theme_links": [], "plans": [], "requests": {}}).encode())
        self.write(self.runtime / "navigation-assignments.json", b'{"product": "202H"}')
        self.write(self.runtime / "auth" / "secret.json", b'{"token": "local only"}')
        self.write(self.runtime / "thumbnail-cache" / "thumb.png", b"cache")
        self.write(self.runtime / "data-transfer" / "config.json", b'{"enabled": false}')
        self.write(self.app / "图文素材" / "202H" / "unused.png", b"unreferenced image")

    @staticmethod
    def write(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def export(self, **kwargs):
        return Path(self.manager.create_archive(self.out, **kwargs)["path"])

    def rewrite(self, source, *, mutate_manifest=None, mutate_entries=None):
        with zipfile.ZipFile(source) as incoming:
            entries = [(entry, incoming.read(entry)) for entry in incoming.infolist()]
        manifest = json.loads(next(raw for entry, raw in entries if entry.filename == "manifest.json"))
        if mutate_manifest:
            mutate_manifest(manifest)
        target = self.root / ("tampered-" + str(len(list(self.root.glob("tampered-*")))) + ".zip")
        with zipfile.ZipFile(target, "w") as outgoing:
            for entry, raw in entries:
                outgoing.writestr(entry, json.dumps(manifest).encode() if entry.filename == "manifest.json" else raw)
            if mutate_entries:
                mutate_entries(outgoing)
        return target

    def test_full_replacement_preserves_original_bytes_and_excluded_local_settings(self):
        archive = self.export(preferences={"backup": {"daily_time": "03:00"}})
        self.write(self.skill / "cases" / "case.json", b'{"changed": true}')
        self.write(self.skill / "cases" / "new.json", b'{}')
        self.write(self.runtime / "new-business" / "file.txt", b"newer")
        self.write(self.app / "图文素材" / "202H" / "unused.png", b"changed image")
        result = self.manager.restore_archive(archive, self.runtime / "data-transfer" / "recovery")
        self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)
        self.assertFalse((self.skill / "cases" / "new.json").exists())
        self.assertFalse((self.runtime / "new-business").exists())
        self.assertEqual((self.app / "图文素材" / "202H" / "unused.png").read_bytes(), b"unreferenced image")
        self.assertTrue((self.runtime / "auth" / "secret.json").exists())
        self.assertTrue((self.runtime / "thumbnail-cache" / "thumb.png").exists())
        self.assertEqual((self.runtime / "data-transfer" / "config.json").read_bytes(), b'{"enabled": false}')
        self.assertTrue(Path(result["recovery_path"]).is_file())
        self.assertEqual(result["preferences"]["backup"]["daily_time"], "03:00")
        with zipfile.ZipFile(result["recovery_path"]) as recovery:
            self.assertEqual(recovery.read("skill/cases/case.json"), b'{"changed": true}')
            self.assertEqual(recovery.read("runtime/new-business/file.txt"), b"newer")
        with zipfile.ZipFile(archive) as packed:
            names = packed.namelist()
            self.assertFalse(any("auth/" in name or "thumbnail-cache/" in name or "data-transfer/" in name for name in names))

    def test_portable_restore_remaps_json_keys_values_text_and_external_assets(self):
        external = self.write(self.root / "outside" / "source video.mp4", bytes(range(256)) * 8)
        local = self.write(self.runtime / "media" / "local.mp4", b"managed media")
        source = self.skill / "sources" / "source.md"
        raw = json.dumps({"assets": {"video": str(external), "screenshot": str(local)},
            "migration": {"sources": {str(source): "digest"}}, "original": "unchanged  exact words"}, indent=4).encode()
        self.write(self.skill / "cases" / "case.json", raw)
        self.write(self.runtime / "output" / "links.md", ("media: " + str(local) + "\r\n").encode())
        archive = self.export()
        skill, runtime, app = (self.root / "second" / name for name in ("skill", "runtime", "app"))
        restored = BackupManager(skill, runtime, app)
        restored.restore_archive(archive, runtime / "data-transfer" / "recovery")
        content = json.loads((skill / "cases" / "case.json").read_bytes())
        self.assertEqual(content["assets"]["screenshot"].replace("\\", "/"), (runtime / "media" / "local.mp4").as_posix())
        self.assertEqual(content["original"], "unchanged  exact words")
        self.assertEqual(list(content["migration"]["sources"]), [str(skill / "sources" / "source.md")])
        self.assertEqual(Path(content["assets"]["video"]).read_bytes(), external.read_bytes())
        self.assertTrue(Path(content["assets"]["video"]).is_relative_to(runtime / "restored-assets"))
        self.assertIn(str(runtime / "media" / "local.mp4"), (runtime / "output" / "links.md").read_text())
        # Formatting survives relocation; neither indentation nor original prose is reserialized.
        self.assertIn(b'    "original": "unchanged  exact words"', (skill / "cases" / "case.json").read_bytes())

    def test_sqlite_wal_commits_included_consistently_and_file_references_relocated(self):
        video = self.write(self.root / "outside" / "db-video.mp4", b"referenced only in SQLite")
        database = self.runtime / "performance" / "performance.sqlite"
        database.parent.mkdir(parents=True)
        connection = sqlite3.connect(database)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE imports(source_file TEXT PRIMARY KEY, rows INTEGER, payload TEXT)")
        connection.execute("INSERT INTO imports VALUES (?, ?, ?)", (str(video), 7, json.dumps({"file": str(video)})))
        connection.commit()
        self.assertTrue(Path(str(database) + "-wal").exists())
        archive = self.export()
        target_runtime = self.root / "second-runtime"
        restore = BackupManager(self.root / "second-skill", target_runtime, self.root / "second-app")
        restore.restore_archive(archive, target_runtime / "data-transfer" / "recovery")
        with closing(sqlite3.connect(target_runtime / "performance" / "performance.sqlite")) as restored:
            path, count, payload = restored.execute("SELECT * FROM imports").fetchone()
            self.assertEqual(count, 7)
            self.assertEqual(Path(path).read_bytes(), video.read_bytes())
            self.assertEqual(json.loads(payload)["file"], path)
        with zipfile.ZipFile(archive) as packed:
            self.assertFalse(any(name.endswith(("-wal", "-shm")) for name in packed.namelist()))

    def test_extra_source_folder_includes_unimported_files_and_subfolders(self):
        sources = self.root / "sales"
        first = self.write(sources / "new.xlsx", b"spreadsheet")
        second = self.write(sources / "older" / "same-name.xlsx", b"historical spreadsheet")
        manager = BackupManager(self.skill, self.runtime, self.app, extra_sources=(sources,))
        archive = Path(manager.create_archive(self.out)["path"])
        target_runtime = self.root / "new-runtime"
        restored = BackupManager(self.root / "new-skill", target_runtime, self.root / "new-app")
        result = restored.restore_archive(archive, target_runtime / "data-transfer" / "recovery")
        relocated = Path(result["extra_source_roots"][0])
        self.assertEqual((relocated / first.name).read_bytes(), first.read_bytes())
        self.assertEqual((relocated / "older" / second.name).read_bytes(), second.read_bytes())

    def test_review_sqlite_full_text_search_survives_portable_relocation(self):
        database = self.runtime / "reviews" / "reviews.sqlite"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("CREATE VIRTUAL TABLE review_fts USING fts5(title, text, tokenize='trigram')")
            connection.execute("INSERT INTO review_fts(title, text) VALUES (?, ?)",
                ("sharpener", "file: " + str(self.skill / "sources" / "source.md")))
            connection.commit()
        archive = self.export()
        restored = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        restored.restore_archive(archive, self.root / "recoveries")
        with closing(sqlite3.connect(restored.runtime_root / "reviews" / "reviews.sqlite")) as connection:
            title, text = connection.execute("SELECT title, text FROM review_fts WHERE review_fts MATCH 'sharpener'").fetchone()
            self.assertEqual(title, "sharpener")
            self.assertIn(str(restored.skill_root / "sources" / "source.md"), text)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone(), ("ok",))

    def test_second_export_restore_preserves_relocated_sales_source_folder(self):
        sources = self.root / "sales"
        self.write(sources / "daily.xlsx", b"unimported daily workbook")
        original = BackupManager(self.skill, self.runtime, self.app, extra_sources=(sources,))
        archive = Path(original.create_archive(self.out)["path"])
        migrated = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        first_result = migrated.restore_archive(archive, self.root / "recovery1")
        migrated.extra_sources = tuple(Path(path) for path in first_result["extra_source_roots"])
        second_archive = Path(migrated.create_archive(self.out)["path"])
        final = BackupManager(self.root / "final-skill", self.root / "final-runtime", self.root / "final-app")
        result = final.restore_archive(second_archive, self.root / "recovery2")
        self.assertEqual(len(result["extra_source_roots"]), 1)
        restored_source = Path(result["extra_source_roots"][0])
        self.assertTrue(restored_source.is_relative_to(final.runtime_root))
        self.assertEqual((restored_source / "daily.xlsx").read_bytes(), b"unimported daily workbook")

    def test_traversal_undeclared_duplicate_and_symlink_are_rejected_before_recovery(self):
        archive = self.export()
        link = zipfile.ZipInfo("skill/cases/linked.json")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for name, write in (("traversal", lambda z: z.writestr("../escape.txt", b"bad")),
                ("undeclared", lambda z: z.writestr("runtime/hidden.json", b"{}")),
                ("duplicate", lambda z: z.writestr("SKILL/cases/case.json", b"{}")),
                ("symlink", lambda z: z.writestr(link, b"target"))):
            with self.subTest(name=name):
                bad = self.rewrite(archive, mutate_entries=write)
                with self.assertRaises(BackupError):
                    self.manager.restore_archive(bad, self.root / "recoveries")
                self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)
                self.assertFalse((self.root / "recoveries").exists())

    def test_modified_bytes_and_hash_manifest_rejected(self):
        archive = self.export()
        bad = self.rewrite(archive, mutate_manifest=lambda manifest: manifest["files"][0].update(sha256="0" * 64))
        with self.assertRaisesRegex(BackupError, "校验失败"):
            self.manager.restore_archive(bad, self.root / "recoveries")
        self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)
        self.assertFalse((self.root / "recoveries").exists())

    def test_compressed_media_is_stored_and_text_is_deflated_with_verified_roundtrip(self):
        video = bytes(range(256)) * 1024
        text = b"readable source text\n" * 1024
        self.write(self.runtime / "media" / "clip.MP4", video)
        self.write(self.runtime / "media" / "recording.wav", b"PCM waveform" * 1024)
        self.write(self.skill / "sources" / "source.md", text)
        archive = self.export()
        with zipfile.ZipFile(archive) as packed:
            self.assertEqual(packed.getinfo("runtime/media/clip.MP4").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(packed.getinfo("app/图文素材/202H/unused.png").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(packed.getinfo("skill/sources/source.md").compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(packed.getinfo("runtime/media/recording.wav").compress_type, zipfile.ZIP_DEFLATED)
            self.assertEqual(packed.getinfo("manifest.json").compress_type, zipfile.ZIP_DEFLATED)
        self.manager.inspect_archive(archive)
        self.write(self.runtime / "media" / "clip.MP4", b"changed")
        self.write(self.skill / "sources" / "source.md", b"changed")
        self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual((self.runtime / "media" / "clip.MP4").read_bytes(), video)
        self.assertEqual((self.skill / "sources" / "source.md").read_bytes(), text)

    def test_catalog_schema_validation_precedes_live_changes(self):
        self.write(self.runtime / "creation" / "catalog.json", b'{"schema_version": 99}')
        archive = self.export()
        self.write(self.runtime / "creation" / "catalog.json", b'{"live": "safe"}')
        with self.assertRaisesRegex(BackupError, "结构无效"):
            self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual((self.runtime / "creation" / "catalog.json").read_bytes(), b'{"live": "safe"}')
        self.assertFalse((self.root / "recoveries").exists())

    def test_custom_case_validator_precedes_recovery_and_changes(self):
        archive = self.export()
        called = []
        def invalid_case(skill, runtime):
            called.append((skill, runtime))
            raise BackupError("invalid case schema")
        manager = BackupManager(self.skill, self.runtime, self.app, validator=invalid_case)
        with self.assertRaisesRegex(BackupError, "invalid case schema"):
            manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual(len(called), 1)
        self.assertFalse((self.root / "recoveries").exists())
        self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)

    def test_apply_failure_rolls_back_and_keeps_verified_recovery(self):
        archive = self.export()
        changed = self.write(self.skill / "cases" / "case.json", b'{"live": "newer"}')
        original_replace = os.replace
        failed = []
        def fail_once(source, destination):
            if not failed and str(source).endswith(str(Path("incoming") / "runtime" / "creation")):
                failed.append(True)
                raise PermissionError("simulated locked directory")
            return original_replace(source, destination)
        with patch("data_backup.os.replace", side_effect=fail_once):
            with self.assertRaisesRegex(BackupError, "已自动恢复原数据"):
                self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertTrue(failed)
        self.assertEqual(changed.read_bytes(), b'{"live": "newer"}')
        recovery = next((self.root / "recoveries").glob("*.zip"))
        self.manager.inspect_archive(recovery)
        self.assertTrue((self.runtime / "creation" / "catalog.json").exists())
        self.assertTrue((self.app / "图文素材" / "202H" / "unused.png").exists())

    def test_recovery_failure_leaves_live_data_untouched(self):
        archive = self.export()
        with patch.object(self.manager, "_create_archive", side_effect=BackupError("destination unavailable")):
            with self.assertRaisesRegex(BackupError, "destination unavailable"):
                self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)

    def test_good_archive_can_replace_damaged_live_json_with_raw_recovery_copy(self):
        archive = self.export()
        damaged = b'{"unfinished": '
        self.write(self.skill / "cases" / "case.json", damaged)
        result = self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)
        with zipfile.ZipFile(result["recovery_path"]) as recovery:
            self.assertEqual(recovery.read("skill/cases/case.json"), damaged)
            self.assertTrue(json.loads(recovery.read("manifest.json"))["warnings"])

    def test_nested_symlinks_rejected_and_resolved_root_links_allowed(self):
        target = self.write(self.root / "outside" / "data.json", b'{}').parent
        link = self.skill / "cases" / "linked-folder"
        def directory_link(path, destination):
            try:
                path.symlink_to(destination, target_is_directory=True)
            except OSError:
                if os.name != "nt":
                    raise
                # Junctions, used for personal skill entries, need no admin privileges.
                result = subprocess.run(["cmd", "/c", "mklink", "/J", str(path), str(destination)], capture_output=True)
                if result.returncode:
                    self.skipTest("OS does not permit directory links in this test session")
        directory_link(link, target)
        with self.assertRaisesRegex(BackupError, "链接"):
            self.export()
        os.rmdir(link) if getattr(link, "is_junction", lambda: False)() else link.unlink()
        alias = self.root / "skill-alias"
        directory_link(alias, self.skill)
        self.write(self.skill / "cases" / "alias.json", json.dumps({"source": str(alias / "sources" / "source.md")}).encode())
        manager = BackupManager(alias, self.runtime, self.app)
        archive = Path(manager.create_archive(self.out)["path"])
        second = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        second.restore_archive(archive, self.root / "recoveries")
        content = json.loads((second.skill_root / "cases" / "alias.json").read_bytes())
        self.assertEqual(content["source"], str(second.skill_root / "sources" / "source.md"))

    def test_exact_directory_root_aliases_are_safe_and_portable(self):
        aliases = {"runtime": str(self.runtime / "creation" / ".."),
            "skill": str(self.skill / "sources" / ".."), "app": str(self.app / "图文素材" / "..")}
        self.write(self.skill / "cases" / "root-aliases.json", json.dumps(aliases).encode())
        archive = self.export()
        with zipfile.ZipFile(archive) as packed:
            manifest = json.loads(packed.read("manifest.json"))
            self.assertEqual(set(manifest["path_aliases"].values()), {"runtime", "skill", "app"})
            self.assertFalse(any(value.endswith("/.") for value in manifest["path_aliases"].values()))
        migrated = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        migrated.restore_archive(archive, self.root / "recoveries")
        content = json.loads((migrated.skill_root / "cases" / "root-aliases.json").read_bytes())
        self.assertEqual(content, {"runtime": str(migrated.runtime_root), "skill": str(migrated.skill_root), "app": str(migrated.app_root)})
        for unsafe in ("runtime/.", "runtime/../escaped", "runtime/auth:NUL", "elsewhere"):
            with self.subTest(alias=unsafe):
                bad = self.rewrite(archive, mutate_manifest=lambda manifest: manifest["path_aliases"].update({aliases["runtime"]: unsafe}))
                with self.assertRaises(BackupError):
                    self.manager.inspect_archive(bad)

    def test_nonexistent_prose_containing_multiple_paths_is_not_recorded_as_alias(self):
        prose = str(self.runtime / "media" / "nonexistent.mp4") + "；已有拆解：" + str(self.skill / "sources" / "source.md")
        self.write(self.skill / "cases" / "prose.json", json.dumps({"note": prose, "search_blob": {"legacy_note": prose}}).encode())
        # Windows canonicalizes existing directory casing even for a nonexistent
        # final path. A dot-parent also reproduces differing raw/canonical strings.
        unsafe = str(self.runtime / "media" / ".." / "missing.txt") + "；" + str(self.runtime / "media" / "second.jpg")
        self.write(self.runtime / "creation" / "prose.json", json.dumps({"note": unsafe}).encode())
        database = self.runtime / "reviews" / "reviews.sqlite"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO meta VALUES ('search_blob', ?)", (json.dumps({"history": unsafe}),))
            connection.commit()
        archive = self.export()
        with zipfile.ZipFile(archive) as packed:
            manifest = json.loads(packed.read("manifest.json"))
            self.assertNotIn(prose, manifest["path_aliases"])
            self.assertNotIn(unsafe, manifest["path_aliases"])
        self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual(json.loads((self.skill / "cases" / "prose.json").read_bytes())["note"], prose)
        migrated = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        migrated.restore_archive(archive, self.root / "migration-recoveries")
        expected = prose.replace(str(self.runtime), str(migrated.runtime_root)).replace(str(self.skill), str(migrated.skill_root))
        data = json.loads((migrated.skill_root / "cases" / "prose.json").read_bytes())
        self.assertEqual(data["search_blob"]["legacy_note"], expected)
        with closing(sqlite3.connect(migrated.runtime_root / "reviews" / "reviews.sqlite")) as connection:
            value = json.loads(connection.execute("SELECT value FROM meta WHERE key='search_blob'").fetchone()[0])
            self.assertEqual(value["history"], unsafe.replace(str(self.runtime), str(migrated.runtime_root)))

    def test_untrusted_absolute_comment_paths_do_not_collect_local_credentials(self):
        sensitive = [self.root / ".codex" / "auth.json", self.root / ".claude" / ".credentials.json",
            self.root / "credentials.json", self.root / ".netrc", self.root / ".env", self.root / ".env.local",
            self.root / "id_rsa", self.root / "id_ed25519", self.root / "private.pem", self.root / "private.key",
            self.root / "account.p12", self.root / "account.pfx"]
        sensitive.extend(self.root / folder / "unusual-name.json" for folder in (".ssh", ".aws", ".azure", ".kube", ".gnupg"))
        for path in sensitive:
            self.write(path, b'{"dummy": "isolated fixture, never real credentials"}')
        video = self.write(self.root / "outside" / "safe-video.mp4", b"ordinary external video")
        comments = {"comments": [{"text": str(path)} for path in sensitive], "assets": {"video": str(video)}}
        self.write(self.skill / "cases" / "credential-comments.json", json.dumps(comments).encode())
        original_open = Path.open
        def avoid_credential_read(path, *args, **kwargs):
            if path in sensitive:
                raise AssertionError("auto-discovery tried to read credential contents")
            return original_open(path, *args, **kwargs)
        with patch("data_backup.Path.open", new=avoid_credential_read):
            archive = self.export()
        with zipfile.ZipFile(archive) as packed:
            manifest = json.loads(packed.read("manifest.json"))
            self.assertEqual(set(manifest["external_files"]), {str(video)})
            record = json.loads(packed.read("skill/cases/credential-comments.json"))
            self.assertEqual(record["comments"], comments["comments"])
            self.assertEqual(packed.read(manifest["external_files"][str(video)]), video.read_bytes())
        self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual(json.loads((self.skill / "cases" / "credential-comments.json").read_bytes())["comments"], comments["comments"])
        self.assertTrue(all(path.is_file() for path in sensitive))

    def with_remote_access(self, archive, files, entry="remote-access", *, mutate=None):
        """An archive as written before remote-access was kept out of exports."""
        def declare(manifest):
            manifest["runtime_entries"] = sorted([*manifest["runtime_entries"], entry])
            manifest["directories"] = sorted([*manifest["directories"], "runtime/" + entry])
            for name, content in files.items():
                manifest["files"].append({"path": "runtime/" + entry + "/" + name, "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest()})
                manifest["bytes"] += len(content)
            if mutate:
                mutate(manifest)
        def add(outgoing):
            for name, content in files.items():
                outgoing.writestr("runtime/" + entry + "/" + name, content)
        return self.rewrite(archive, mutate_manifest=declare, mutate_entries=add)

    @staticmethod
    def short_name(path):
        if os.name != "nt":
            return None
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer)):
            return None
        name = Path(buffer.value).name
        return name if name.casefold() != path.name.casefold() else None

    def test_remote_access_secrets_are_not_exported_and_survive_restore(self):
        token = self.write(self.runtime / "remote-access" / "tunnel-token.txt", b"live tunnel token")
        account = self.write(self.runtime / "remote-access" / "account.json", b'{"password_hash": "live"}')
        self.write(self.runtime / "remote-access" / "guest" / "site" / "index.html", b"<p>guest</p>")
        archive = self.export()
        with zipfile.ZipFile(archive) as packed:
            self.assertFalse(any("remote-access" in name.casefold() for name in packed.namelist()))
            manifest = json.loads(packed.read("manifest.json"))
            self.assertNotIn("remote-access", manifest["runtime_entries"])
            self.assertFalse(any("remote-access" in name for name in manifest["directories"]))
        self.write(token, b"rotated tunnel token")
        result = self.manager.restore_archive(archive, self.root / "recoveries")
        self.assertEqual(token.read_bytes(), b"rotated tunnel token")
        self.assertEqual(account.read_bytes(), b'{"password_hash": "live"}')
        self.assertTrue((self.runtime / "remote-access" / "guest" / "site" / "index.html").is_file())
        with zipfile.ZipFile(result["recovery_path"]) as recovery:
            self.assertFalse(any("remote-access" in name.casefold() for name in recovery.namelist()))

    def test_older_archive_with_remote_access_imports_without_touching_it(self):
        archive = self.export()
        plain = self.manager.inspect_archive(archive)
        secrets = {"tunnel-token.txt": b"old tunnel token", "account.json": b'{"password_hash": "old"}'}
        for name, files, entry in (("with secrets", secrets, "remote-access"), ("directory only", {}, "remote-access"),
                ("other letter case", secrets, "Remote-Access")):
            with self.subTest(name=name):
                legacy = self.with_remote_access(archive, files, entry)
                summary = self.manager.inspect_archive(legacy)
                self.assertEqual((summary["files"], summary["bytes"]), (plain["files"], plain["bytes"]))
                token = self.write(self.runtime / "remote-access" / "tunnel-token.txt", b"live tunnel token")
                self.write(self.skill / "cases" / "case.json", b'{"changed": true}')
                self.manager.restore_archive(legacy, self.root / "recoveries")
                self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), self.original)
                self.assertEqual(token.read_bytes(), b"live tunnel token")
                self.assertEqual(sorted(path.name for path in (self.runtime / "remote-access").iterdir()), ["tunnel-token.txt"])
        skill, runtime, app = (self.root / "second" / name for name in ("skill", "runtime", "app"))
        BackupManager(skill, runtime, app).restore_archive(self.with_remote_access(archive, secrets), runtime / "data-transfer" / "recovery")
        self.assertTrue((skill / "cases" / "case.json").is_file())
        self.assertFalse((runtime / "remote-access").exists())

    def test_tampered_remote_access_entry_still_fails_archive_verification(self):
        legacy = self.with_remote_access(self.export(), {"tunnel-token.txt": b"old tunnel token"})
        bad = self.rewrite(legacy, mutate_manifest=lambda manifest: next(row for row in manifest["files"]
            if row["path"] == "runtime/remote-access/tunnel-token.txt").update(sha256="0" * 64))
        with self.assertRaisesRegex(BackupError, "校验失败"):
            self.manager.restore_archive(bad, self.root / "recoveries")
        self.assertFalse((self.root / "recoveries").exists())

    def test_short_name_alias_of_a_local_only_folder_is_rejected_before_changes(self):
        token = self.write(self.runtime / "remote-access" / "tunnel-token.txt", b"live tunnel token")
        for folder, name, live in (("remote-access", "tunnel-token.txt", b"live tunnel token"), ("thumbnail-cache", "thumb.png", b"cache")):
            with self.subTest(folder=folder):
                alias = self.short_name(self.runtime / folder)
                if alias is None:
                    self.skipTest("this volume has no 8.3 short names")
                legacy = self.with_remote_access(self.export(), {name: b"replacement"}, alias)
                self.write(self.skill / "cases" / "case.json", b'{"changed": true}')
                with self.assertRaisesRegex(BackupError, "不允许导入"):
                    self.manager.restore_archive(legacy, self.root / "recoveries")
                self.assertEqual((self.runtime / folder / name).read_bytes(), live)
                self.assertEqual((self.skill / "cases" / "case.json").read_bytes(), b'{"changed": true}')
                self.assertFalse((self.root / "recoveries").exists())
        self.assertEqual(token.read_bytes(), b"live tunnel token")

    def test_older_archive_sales_folder_inside_remote_access_is_not_restored_as_source(self):
        old_sales = str(self.root / "old-machine" / "sales")
        def include(manifest):
            manifest["directories"].append("runtime/remote-access/sales")
            manifest["included_source_roots"] = {old_sales: "runtime/remote-access/sales"}
        legacy = self.with_remote_access(self.export(), {"sales/report.xlsx": b"old sales"}, mutate=include)
        result = self.manager.restore_archive(legacy, self.root / "recoveries")
        self.assertEqual(result["extra_source_roots"], [])
        self.assertFalse((self.runtime / "remote-access").exists())

    def test_sales_folder_containing_runtime_data_is_rejected(self):
        self.write(self.runtime / "remote-access" / "tunnel-token.txt", b"live tunnel token")
        elsewhere = tempfile.TemporaryDirectory(prefix="workbench-backup-out-")
        self.addCleanup(elsewhere.cleanup)
        manager = BackupManager(self.skill, self.runtime, self.app, extra_sources=(self.root,))
        with self.assertRaisesRegex(BackupError, "不能包含运行资料目录"):
            manager.create_archive(elsewhere.name)
        self.assertEqual(list(Path(elsewhere.name).iterdir()), [])

    def test_backup_destinations_inside_business_data_are_rejected_and_ordinary_locks_work(self):
        for destination in (self.skill / "cases" / "backup", self.runtime / "creation" / "backup", self.app / "图文素材" / "backup",
                self.runtime / "remote-access" / "guest" / "site"):
            with self.subTest(destination=destination), self.assertRaises(BackupError):
                self.manager.create_archive(destination)
        manager = BackupManager(self.skill, self.runtime, self.app, locks=(threading.Lock(),))
        archive = Path(manager.create_archive(self.out)["path"])
        manager.restore_archive(archive, self.runtime / "data-transfer" / "recovery")

    def test_sqlite_exclusive_writer_times_out_and_releases_business_locks(self):
        database = self.runtime / "busy.sqlite"
        writer = sqlite3.connect(database)
        self.addCleanup(writer.close)
        writer.execute("CREATE TABLE data(value TEXT)")
        writer.execute("INSERT INTO data VALUES ('committed')")
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO data VALUES ('uncommitted')")
        business = threading.Lock()
        manager = BackupManager(self.skill, self.runtime, self.app, locks=(business,))
        started = time.monotonic()
        with patch("data_backup.SQLITE_SNAPSHOT_TIMEOUT", .1):
            with self.assertRaisesRegex(BackupError, "数据库快照超时") as caught:
                manager.create_archive(self.out)
        self.assertEqual(caught.exception.status, 503)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(business.acquire(blocking=False))
        business.release()
        self.assertFalse(list(self.out.glob("*.zip")))
        self.assertFalse(list(self.out.glob("*.partial")))
        self.assertFalse(list(manager.transfer_root.glob("backup-sqlite-*")))
        writer.rollback()
        archive = Path(manager.create_archive(self.out)["path"])
        self.assertTrue(archive.is_file())
        self.assertEqual(writer.execute("SELECT value FROM data").fetchall(), [("committed",)])

    def test_business_read_lock_is_available_while_export_compresses_snapshots(self):
        database = self.runtime / "wide.sqlite"
        with closing(sqlite3.connect(database)) as db:
            db.execute("CREATE TABLE rows(first TEXT, second TEXT, count INTEGER, payload BLOB)")
            db.execute("INSERT INTO rows VALUES (?, ?, ?, ?)", (str(self.skill / "sources" / "source.md"), "unchanged", 7, b"opaque"))
            db.commit()
        business = threading.Lock()
        manager = BackupManager(self.skill, self.runtime, self.app, locks=(business,))
        original_open = zipfile.ZipFile.open
        checked = []
        def check_lock(archive, name, mode="r", *args, **kwargs):
            if mode == "w":
                acquired = business.acquire(blocking=False)
                self.assertTrue(acquired, "compression kept the business read lock")
                if acquired:
                    business.release()
                checked.append(name)
            return original_open(archive, name, mode, *args, **kwargs)
        with patch("data_backup.zipfile.ZipFile.open", new=check_lock):
            archive = Path(manager.create_archive(self.out)["path"])
        self.assertTrue(checked)
        restored = BackupManager(self.root / "new-skill", self.root / "new-runtime", self.root / "new-app")
        restored.restore_archive(archive, self.root / "recovery")
        with closing(sqlite3.connect(restored.runtime_root / "wide.sqlite")) as db:
            self.assertEqual(db.execute("SELECT second, count, payload FROM rows").fetchall(), [("unchanged", 7, b"opaque")])

    def test_sync_migration_cannot_mix_new_catalog_with_old_archive_file_list(self):
        catalog = self.runtime / "directions" / "catalog.json"
        previous = self.runtime / "directions" / "backups" / "before.json"
        updated = previous.with_name("after.json")
        self.write(previous, b'{"revision":1}')
        self.write(catalog, json.dumps({"revision": 1, "history": str(previous)}).encode())
        external = self.root / "sales"
        self.write(external / "source.csv", b"external source")
        business = threading.Lock()
        manager = BackupManager(self.skill, self.runtime, self.app, locks=(business,), extra_sources=(external,))
        attempted, migrated = threading.Event(), threading.Event()
        writers = []
        original_walk = manager._walk

        def sync_migration():
            attempted.set()
            with business:
                self.write(updated, b'{"revision":2}')
                self.write(catalog, json.dumps({"revision": 2, "history": str(updated)}).encode())
                previous.unlink()  # Sync can also prune a history file after the snapshot.
            migrated.set()

        def walk_with_sync(path, root, archive_path, directories):
            if path == external and not writers:
                writer = threading.Thread(target=sync_migration)
                writers.append(writer)
                writer.start()
                self.assertTrue(attempted.wait(2))
                # An old two-lock implementation let sync finish in this gap.
                acquired = business.acquire(blocking=False)
                if acquired:
                    business.release()
                    self.assertTrue(migrated.wait(2))
            yield from original_walk(path, root, archive_path, directories)

        def progress(message):
            if message == "正在整理素材引用并打包":
                self.assertTrue(migrated.wait(2), "metadata lock was not released before packaging")

        manager.on_progress = progress
        try:
            with patch.object(manager, "_walk", side_effect=walk_with_sync):
                archive = Path(manager.create_archive(self.out)["path"])
        finally:
            for writer in writers:
                writer.join(2)
        self.assertTrue(migrated.is_set())
        self.assertEqual(json.loads(catalog.read_bytes())["revision"], 2)
        with zipfile.ZipFile(archive) as packed:
            snapshot = json.loads(packed.read("runtime/directions/catalog.json"))
            self.assertEqual(snapshot["revision"], 1)
            self.assertIn("runtime/directions/backups/" + Path(snapshot["history"]).name, packed.namelist())
            self.assertNotIn("runtime/directions/backups/after.json", packed.namelist())


if __name__ == "__main__":
    unittest.main()
