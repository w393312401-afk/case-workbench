"""End-to-end total-data transfer using isolated cases, media and recovery files."""
from __future__ import annotations

import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

import test_server as fixture


class DataTransferAPITests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def isolate_transfer(self):
        data = self.server.app.data_transfer
        app = self.root / "isolated-app"
        (app / "图文素材").mkdir(parents=True)
        (app / "图文素材" / "one.jpg").write_bytes(b"isolated image")
        data.app_root = app
        data.manager.app_root = app
        data.manager.extra_sources = ()
        return data

    def export(self, prefs=None):
        self.isolate_transfer()
        status, _, result = self.request("POST", "/api/data/export", {"preferences": prefs or {}})
        self.assertEqual(status, 200, result)
        status, headers, raw = self.request("GET", result["download_url"])
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("manifest.json", zipfile.ZipFile(io.BytesIO(raw)).namelist())
        return raw

    def inspect(self, raw):
        return self.request("POST", "/api/data/import/inspect", raw=raw, headers={"Content-Type": "application/zip"})

    def test_default_status_and_write_authentication(self):
        status, _, result = self.request("GET", "/api/data/status")
        self.assertEqual(status, 200)
        self.assertFalse(result["settings"]["enabled"])
        self.assertIsNone(result["next_backup"])
        self.assertIsNone(result["last_backup"])
        status, _, _ = self.request("POST", "/api/data/export", {"preferences": {}}, authenticated=False)
        self.assertEqual(status, 403)

    def test_export_inspect_restore_media_preferences_and_recovery(self):
        raw = self.export({"case-library-layout": "list"})
        before = self.server.app.store.read_case(self.identifier)
        current = copy.deepcopy(before["current"])
        current["conclusion"] = "changed after exporting"
        self.server.app.store.append(self.identifier, {"kind": "editorial", "text": "after export", "current": current})
        status, _, inspected = self.inspect(raw)
        self.assertEqual(status, 200, inspected)
        self.assertGreater(inspected["summary"]["files"], 0)
        self.assertEqual(self.server.app.store.read_case(self.identifier)["current"]["conclusion"], "changed after exporting")
        body = {"import_id": inspected["import_id"], "confirm": True}
        status, _, result = self.request("POST", "/api/data/import", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["preferences"], {"case-library-layout": "list"})
        recovery = Path(result["recovery_path"])
        self.assertTrue(recovery.is_file())
        restored = self.server.app.store.read_case(self.identifier)
        self.assertEqual(restored["current"]["conclusion"], before["current"]["conclusion"])
        self.assertEqual(Path(restored["current"]["assets"]["video"]).read_bytes(), self.video_bytes)
        with zipfile.ZipFile(recovery) as archive:
            events = [archive.read(name).decode() for name in archive.namelist() if "/events/" in name]
        self.assertTrue(any("changed after exporting" in text for text in events))
        self.assertFalse(self.server.data_maintenance)
        self.assertFalse(self.server.app.data_transfer.busy)
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(state["cases"][0]["current"]["conclusion"], before["current"]["conclusion"])

    def test_restore_needs_explicit_confirmation_and_current_preview(self):
        raw = self.export()
        status, _, inspected = self.inspect(raw)
        self.assertEqual(status, 200, inspected)
        for body in ({"import_id": inspected["import_id"], "confirm": False},
                     {"import_id": "missing", "confirm": True}):
            status, _, _ = self.request("POST", "/api/data/import", body)
            self.assertEqual(status, 400)
        self.assertFalse(self.server.data_maintenance)

    def test_recovery_keeps_current_preferences_and_post_restore_failure_is_reported(self):
        raw = self.export({"case-library-layout": "list"})
        self.request("POST", "/api/data/preferences", {"preferences": {"case-library-layout": "card"}})
        status, _, inspected = self.inspect(raw)
        self.assertEqual(status, 200, inspected)
        data = self.server.app.data_transfer
        with patch.object(data, "on_restore", side_effect=OSError("fixture cache failure")):
            status, _, result = self.request("POST", "/api/data/import", {"import_id": inspected["import_id"], "confirm": True})
        self.assertEqual(status, 200, result)
        self.assertIn("warning", result)
        self.assertNotIn(inspected["import_id"], data._imports)
        with zipfile.ZipFile(result["recovery_path"]) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["preferences"], {"case-library-layout": "card"})
        self.assertEqual(json.loads(data.config_path.read_text(encoding="utf-8"))["preferences"], {"case-library-layout": "list"})

    def test_rejects_invalid_archives_without_changing_records(self):
        original = self.server.app.store.read_case(self.identifier)
        for raw in (b"not a zip", b"PK\x03\x04invalid"):
            status, _, value = self.inspect(raw)
            self.assertEqual(status, 400, value)
        self.assertEqual(self.server.app.store.read_case(self.identifier), original)
        self.assertFalse(self.server.app.data_transfer.busy)
        self.assertFalse(self.server.data_maintenance)

    def test_manual_backup_custom_directory_and_settings_persist(self):
        data = self.isolate_transfer()
        destination = self.root / "local backups"
        body = {"enabled": False, "directory": str(destination), "interval_days": 1, "time": "02:00", "keep": 2}
        with patch.object(data.schedule, "configure", return_value={"installed": False, "mode": "windows"}):
            status, _, value = self.request("POST", "/api/data/settings", body)
        self.assertEqual(status, 200, value)
        status, _, value = self.request("POST", "/api/data/backup", {})
        self.assertEqual(status, 200, value)
        self.assertEqual(Path(value["backup"]["path"]).parent, destination)
        self.assertTrue(Path(value["backup"]["path"]).is_file())
        persisted = json.loads(data.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["settings"]["directory"], str(destination))
        self.assertEqual(persisted["history"][-1]["path"], value["backup"]["path"])

    def test_running_creation_blocks_snapshot(self):
        self.isolate_transfer()
        with patch.object(self.server.app, "image_running", return_value=True):
            status, _, value = self.request("POST", "/api/data/export", {"preferences": {}})
        self.assertEqual(status, 409, value)
        self.assertFalse(self.server.data_maintenance)
        self.assertFalse(self.server.app.data_transfer.busy)

    def test_admitted_upload_or_write_blocks_data_operation(self):
        self.isolate_transfer()
        with self.server._write_gate:
            self.server._active_writes += 1
        try:
            status, _, value = self.request("POST", "/api/data/export", {"preferences": {}})
            self.assertEqual(status, 409, value)
        finally:
            with self.server._write_gate:
                self.server._active_writes -= 1
        self.assertFalse(self.server.data_maintenance)
        self.assertFalse(self.server.app.data_transfer.busy)

    def test_maintenance_rejects_other_writes_but_status_remains_readable(self):
        self.server.data_maintenance = True
        try:
            status, _, value = self.request("POST", "/api/sync", {})
            self.assertEqual(status, 409, value)
            status, _, _ = self.request("GET", "/api/data/status")
            self.assertEqual(status, 200)
        finally:
            self.server.data_maintenance = False

    def test_export_and_backup_keep_pages_and_business_reads_available(self):
        transfer = self.server.app.data_transfer
        for operation in ("export", "backup"):
            with self.subTest(operation=operation):
                transfer.operation_kind = operation
                self.server.data_maintenance = True
                try:
                    for path in ("/", "/app.js", "/production.js", "/production.css", "/api/state", "/api/data/status"):
                        self.assertEqual(self.request("GET", path)[0], 200, path)
                    status, _, value = self.request("POST", "/api/sync", {})
                    self.assertEqual(status, 409)
                    self.assertTrue(value["maintenance"])
                    self.assertEqual(value["operation"], operation)
                finally:
                    self.server.data_maintenance = False
                    transfer.operation_kind = None

    def test_restore_keeps_shell_open_and_blocks_only_business_reads(self):
        transfer = self.server.app.data_transfer
        transfer.operation_kind = "restore"
        self.server.data_maintenance = True
        try:
            for path in ("/", "/app.js", "/production.js", "/production.css", "/api/data/status", "/api/service/status"):
                self.assertEqual(self.request("GET", path)[0], 200, path)
            status, _, value = self.request("GET", "/api/state")
            self.assertEqual(status, 409)
            self.assertTrue(value["maintenance"])
            self.assertEqual(value["operation"], "restore")
        finally:
            self.server.data_maintenance = False
            transfer.operation_kind = None
        self.assertEqual(self.request("GET", "/api/state")[0], 200)


if __name__ == "__main__":
    unittest.main()
