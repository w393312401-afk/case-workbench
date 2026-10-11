"""Daily backup scheduling and worker admission using disposable data only."""
from __future__ import annotations

from datetime import datetime
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
import data_transfer as transfer
import backup_worker as worker


class FixedDatetime(datetime):
    current = datetime(2026, 10, 11, 9, 30)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.replace(tzinfo=tz)


class DataTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-transfer-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.skill, self.runtime, self.app = (self.root / name for name in ("skill", "runtime", "app"))
        for folder in (self.skill, self.runtime, self.app):
            folder.mkdir()
        self.target = self.root / "backup folder"
        self.service = transfer.DataTransfer(self.skill, self.runtime, self.app)
        self.scheduler = patch.object(self.service.schedule, "configure", return_value={"installed": True, "mode": "test"}).start()
        self.addCleanup(patch.stopall)
        self.clock = patch.object(transfer, "datetime", FixedDatetime).start()

    def settings(self, **changes):
        return {**transfer.DEFAULT_SETTINGS, "enabled": True, "directory": str(self.target), **changes}

    def fake_archive(self, destination, kind="backup", preferences=None):
        destination.mkdir(parents=True, exist_ok=True)
        name = f"case-workbench-{kind}-{len(list(destination.glob('*.zip'))):03}.zip"
        path = destination / name
        path.write_bytes(b"isolated backup fixture")
        return {"name": name, "path": str(path), "bytes": path.stat().st_size, "created_at": FixedDatetime.current.isoformat()}

    def test_saved_daily_settings_persist_the_next_future_first_run(self):
        result = self.service.save_settings(self.settings(interval_days=3, time="08:00", keep=7))
        self.assertEqual(result["next_backup"], "2026-10-12T08:00:00")
        self.assertEqual(result["settings"]["interval_days"], 3)
        self.assertEqual(self.scheduler.call_args.args[1], "2026-10-12T08:00:00")
        reopened = transfer.DataTransfer(self.skill, self.runtime, self.app)
        self.assertEqual(reopened.config["settings"], result["settings"])
        self.assertEqual(reopened.config["skill_root"], str(self.skill))
        self.assertEqual(reopened.config["runtime_root"], str(self.runtime))
        self.assertEqual(reopened.config["app_root"], str(self.app))
        self.assertFalse(any(self.target.glob(".backup-write-check-*")))

    def test_invalid_settings_never_register_a_windows_task(self):
        bodies = [
            self.settings(enabled="yes"), self.settings(interval_days=True),
            self.settings(interval_days=0), self.settings(interval_days=366),
            self.settings(keep=-1), self.settings(keep=True), self.settings(keep=3651),
            self.settings(time="24:00"), self.settings(time="8:00"), self.settings(time="02:60"),
            self.settings(directory="relative-folder"), self.settings(directory=""),
            self.settings(directory=str(self.runtime / "backup")),
            self.settings(directory=str(self.skill / "backup")),
            self.settings(directory=str(self.app / "backup")),
            self.settings(directory=str(self.root)),
            {**self.settings(), "unknown": True},
        ]
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(transfer.TransferError):
                self.service.save_settings(body)
        self.scheduler.assert_not_called()
        self.assertFalse(self.service.config_path.exists())

    def test_disabling_needs_no_directory_and_clears_pending_auto_slot(self):
        self.service.config["last_auto_slot"] = "2026-10-10T02:00:00"
        result = self.service.save_settings(self.settings(enabled=False, directory=""))
        self.assertIsNone(result["next_backup"])
        self.assertIsNone(self.service.config["last_auto_slot"])
        self.assertFalse(self.scheduler.call_args.args[0]["enabled"])

    def test_scheduler_failure_preserves_prior_settings_and_file(self):
        self.service.save_settings(self.settings(time="10:00"))
        before = self.service.config_path.read_bytes()
        old = dict(self.service.config["settings"])
        self.scheduler.side_effect = transfer.TransferError("task scheduler unavailable", 503)
        with self.assertRaises(transfer.TransferError):
            self.service.save_settings(self.settings(interval_days=5))
        self.assertEqual(self.service.config["settings"], old)
        self.assertEqual(self.service.config_path.read_bytes(), before)

    def test_failed_settings_write_reinstalls_prior_windows_schedule(self):
        self.service.save_settings(self.settings(time="10:00"))
        old = dict(self.service.config["settings"])
        with patch.object(self.service, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.service.save_settings(self.settings(time="11:00"))
        self.assertEqual(self.service.config["settings"], old)
        self.assertEqual(self.scheduler.call_args.args[0], old)

    def test_due_backup_catches_up_once_at_the_latest_missed_day_slot(self):
        self.service.config.update(settings=self.settings(interval_days=3, keep=0),
                                   first_run="2026-10-01T02:00:00", last_auto_slot="2026-10-04T02:00:00")
        with patch.object(self.service.manager, "create_archive", side_effect=self.fake_archive) as create:
            result = self.service.due_backup()
            self.assertIsNotNone(result)
            self.assertEqual(create.call_args.kwargs["kind"], "auto")
            self.assertEqual(self.service.config["last_auto_slot"], "2026-10-10T02:00:00")
            self.assertIsNone(self.service.due_backup())
            create.assert_called_once()
        self.assertEqual(self.service.status()["next_backup"], "2026-10-13T02:00:00")

    def test_failed_automatic_backup_keeps_slot_due_and_persists_failure(self):
        self.service.config.update(settings=self.settings(), first_run="2026-10-10T02:00:00")
        with patch.object(self.service.manager, "create_archive", side_effect=OSError("backup disk unavailable")):
            with self.assertRaises(OSError):
                self.service.due_backup()
        self.assertFalse(self.service.busy)
        self.assertIsNone(self.service.config.get("last_auto_slot"))
        self.assertIn("backup disk unavailable", self.service.status()["last_error"])
        persisted = json.loads(self.service.config_path.read_text(encoding="utf-8"))
        self.assertIn("backup disk unavailable", persisted["last_error"])
        self.assertEqual(self.service.status()["next_backup"], "2026-10-11T02:00:00")

    def test_long_automatic_backup_leaves_the_next_day_due(self):
        self.service.config.update(settings=self.settings(keep=0), first_run="2026-10-10T02:00:00")

        def archive_across_next_slot(*args, **kwargs):
            FixedDatetime.current = datetime(2026, 10, 12, 2, 30)
            return self.fake_archive(*args, **kwargs)

        with patch.object(FixedDatetime, "current", datetime(2026, 10, 11, 23, 30)), \
                patch.object(self.service.manager, "create_archive", side_effect=archive_across_next_slot):
            self.service.due_backup()
            self.assertEqual(self.service.config["last_auto_slot"], "2026-10-11T02:00:00")
            self.assertEqual(self.service.status()["next_backup"], "2026-10-12T02:00:00")
            with patch.object(self.service.manager, "create_archive", side_effect=self.fake_archive):
                self.assertIsNotNone(self.service.due_backup())
            self.assertEqual(self.service.config["last_auto_slot"], "2026-10-12T02:00:00")

    def test_manual_backup_does_not_mark_the_automatic_slot_done(self):
        self.service.config.update(settings=self.settings(keep=0), first_run="2026-10-10T02:00:00")
        self.service.save_preferences({"preferences": {"workbench.product": "test-product"}})
        with patch.object(self.service.manager, "create_archive", side_effect=self.fake_archive) as create:
            self.service.backup()
        self.assertEqual(create.call_args.kwargs["preferences"], {"workbench.product": "test-product"})
        self.assertIsNone(self.service.config.get("last_auto_slot"))

    def test_backup_rejects_active_work_before_creating_an_archive_and_releases_busy(self):
        self.service.config["settings"] = self.settings()
        self.service.ensure_idle = Mock(side_effect=transfer.TransferError("AI work is active", 409))
        with patch.object(self.service.manager, "create_archive") as create:
            with self.assertRaises(transfer.TransferError) as caught:
                self.service.backup()
            self.assertEqual(caught.exception.status, 409)
            create.assert_not_called()
        self.assertFalse(self.service.busy)
        self.assertTrue(self.service.operation.acquire(blocking=False))
        self.service.operation.release()

    def test_retention_deletes_only_registered_valid_packages_in_selected_directory(self):
        self.target.mkdir()
        records = [self.fake_archive(self.target) for _ in range(3)]
        foreign = self.target / "user-important.zip"
        foreign.write_bytes(b"not an application backup")
        outside = self.fake_archive(self.root / "elsewhere")
        self.service.config["history"] = [*records, outside]
        with patch.object(self.service.manager, "inspect_archive", return_value={}) as inspect:
            self.service._prune(self.target, 1)
        self.assertFalse(Path(records[0]["path"]).exists())
        self.assertFalse(Path(records[1]["path"]).exists())
        self.assertTrue(Path(records[2]["path"]).exists())
        self.assertTrue(Path(outside["path"]).exists())
        self.assertTrue(foreign.exists())
        self.assertEqual(inspect.call_count, 2)

    def test_retention_keeps_corrupt_or_renamed_packages_and_keep_zero_means_unlimited(self):
        self.target.mkdir()
        records = [self.fake_archive(self.target) for _ in range(3)]
        records[0]["name"] = "mismatched-name.zip"
        self.service.config["history"] = records
        with patch.object(self.service.manager, "inspect_archive", side_effect=ValueError("not a verified package")) as inspect:
            self.service._prune(self.target, 0)
            inspect.assert_not_called()
            self.service._prune(self.target, 1)
            inspect.assert_called_once_with(Path(records[1]["path"]))
        self.assertTrue(all(Path(item["path"]).exists() for item in records))

    def test_preference_payload_rejects_unrelated_or_excessive_values(self):
        self.assertEqual(transfer.preferences({"case.view": "archive"}), {"case.view": "archive"})
        for value in ({"password": "secret"}, {"workbench.x": 5}, {"workbench.x": "x" * (1024 * 1024 + 1)}, []):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(transfer.TransferError):
                transfer.preferences(value)

    def test_real_small_export_preview_restore_preserves_data_and_recovery_backup(self):
        (self.skill / "sources").mkdir()
        source = self.skill / "sources" / "sample.md"
        source.write_text("sample source", encoding="utf-8")
        business = self.runtime / "business.json"
        business.write_text('{"value":"exported"}', encoding="utf-8")
        (self.app / "图文素材").mkdir()
        image = self.app / "图文素材" / "available-image.jpg"
        image.write_bytes(b"independent image fixture")
        exported = self.service.export({"preferences": {"workbench.view": "all"}})
        token = exported["download_url"].rsplit("/", 1)[1]
        archive = self.service.download(token)
        with archive.open("rb") as stream:
            preview = self.service.inspect_upload(stream, archive.stat().st_size)
        self.assertEqual(preview["summary"]["preferences"], {"workbench.view": "all"})
        self.assertEqual(preview["summary"]["files"], 3)
        business.write_text('{"value":"before import"}', encoding="utf-8")
        source.write_text("before import", encoding="utf-8")
        image.write_bytes(b"before import")
        callback = self.service.on_restore = Mock()
        result = self.service.restore({"import_id": preview["import_id"], "confirm": True})
        self.assertEqual(json.loads(business.read_text(encoding="utf-8"))["value"], "exported")
        self.assertEqual(source.read_text(encoding="utf-8"), "sample source")
        self.assertEqual(image.read_bytes(), b"independent image fixture")
        self.assertEqual(result["preferences"], {"workbench.view": "all"})
        callback.assert_called_once_with()
        self.assertTrue(result["restored"])
        self.assertNotIn(preview["import_id"], self.service._imports)
        with zipfile.ZipFile(result["recovery_path"]) as recovery:
            self.assertEqual(json.loads(recovery.read("runtime/business.json"))["value"], "before import")

    def test_restore_requires_current_preview_and_explicit_confirmation(self):
        for body in ({"import_id": "unknown", "confirm": True}, {"import_id": "unknown", "confirm": False},
                     {"import_id": "unknown", "confirm": 1}, {"import_id": "unknown"}):
            with self.subTest(body=body), self.assertRaises(transfer.TransferError):
                self.service.restore(body)
        self.assertFalse(self.service.busy)
        with patch.object(self.service.manager, "restore_archive") as restore:
            self.service._imports["expired"] = (self.root / "missing.zip", -1000000)
            with self.assertRaises(transfer.TransferError):
                self.service.restore({"import_id": "expired", "confirm": True})
            restore.assert_not_called()

    def test_partial_import_upload_is_removed_and_releases_operation(self):
        with self.assertRaises(transfer.TransferError):
            self.service.inspect_upload(io.BytesIO(b"truncated"), 100)
        self.assertFalse(self.service.busy)
        self.assertFalse(list((self.service.root / "imports").glob("*.zip")))

    @unittest.skipUnless(os.name == "nt", "Windows task registration")
    def test_windows_schedule_uses_current_user_no_credentials_and_catch_up(self):
        schedule = transfer.WindowsSchedule(self.runtime, self.app)
        with patch.object(transfer.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = schedule.configure(self.settings(interval_days=4), "2026-10-12T09:00:00", self.service.config_path)
        payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(payload["days"], 4)
        self.assertEqual(payload["at"], "2026-10-12T09:00:00")
        self.assertIn(str(self.app / "backup_worker.py"), payload["args"])
        self.assertIn(str(self.service.config_path), payload["args"])
        script = run.call_args.args[0][-1]
        self.assertIn("-StartWhenAvailable", script)
        self.assertIn("-MultipleInstances IgnoreNew", script)
        self.assertIn("-RestartInterval (New-TimeSpan -Minutes 5)", script)
        self.assertIn("-RestartCount 72", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertNotIn("-Password", script)
        self.assertEqual(result["mode"], "windows")


class BackupWorkerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-backup-worker-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = {"settings": {**transfer.DEFAULT_SETTINGS, "enabled": True},
                       "skill_root": str(self.root / "skill"), "runtime_root": str(self.root / "runtime"),
                       "app_root": str(self.root / "app"), "port": 28767}
        self.path = self.root / "settings.json"
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        self.live = {"app": "video-case-workbench", "token": "isolated-worker-token",
                     "paths": {"records": str(self.root / "skill" / "cases"),
                               "library": str(self.root / "runtime" / "library")}}

    def response(self, value):
        return io.BytesIO(json.dumps(value).encode("utf-8"))

    def test_disabled_worker_never_contacts_service_or_reads_data(self):
        self.config["settings"]["enabled"] = False
        self.path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(worker.urllib.request, "build_opener") as opener, patch.object(worker, "DataTransfer") as service:
            worker.run(self.path)
        opener.assert_not_called()
        service.assert_not_called()

    def test_identified_live_server_receives_authenticated_due_request(self):
        opener = Mock()
        opener.open.side_effect = [self.response(self.live), self.response({"ok": True})]
        with patch.object(worker.urllib.request, "build_opener", return_value=opener), patch.object(worker, "DataTransfer") as service:
            worker.run(self.path)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:28767/api/data/backup/due")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-workbench-token"), self.live["token"])
        self.assertEqual(request.data, b"{}")
        service.assert_not_called()

    def test_live_backup_failure_never_falls_back_to_offline_copy(self):
        for failure in (urllib.error.URLError("connection interrupted"),
                        urllib.error.HTTPError("http://localhost", 409, "busy", {}, None),
                        TimeoutError("backup timed out")):
            opener = Mock()
            opener.open.side_effect = [self.response(self.live), failure]
            with self.subTest(error=type(failure).__name__), \
                 patch.object(worker.urllib.request, "build_opener", return_value=opener), \
                 patch.object(worker, "DataTransfer") as service:
                with self.assertRaises((urllib.error.URLError, OSError)):
                    worker.run(self.path)
                service.assert_not_called()

    def test_other_service_or_other_root_cannot_trigger_offline_copy(self):
        mismatches = [
            {**self.live, "app": "another-application"},
            {**self.live, "paths": {**self.live["paths"], "records": str(self.root / "other" / "cases")}},
            {**self.live, "paths": {**self.live["paths"], "library": str(self.root / "other" / "library")}},
        ]
        for live in mismatches:
            opener = Mock()
            opener.open.return_value = self.response(live)
            with self.subTest(live=live), patch.object(worker.urllib.request, "build_opener", return_value=opener), \
                 patch.object(worker, "DataTransfer") as service:
                with self.assertRaises(transfer.TransferError):
                    worker.run(self.path)
                service.assert_not_called()

    def test_service_status_http_error_cannot_bypass_an_existing_server_lease(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("http://localhost", 503, "temporarily unavailable", {}, None)
        lease = worker.ServerLease(self.config["runtime_root"])
        self.assertTrue(lease.acquire())
        try:
            with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
                 patch.object(worker, "DataTransfer") as service:
                with self.assertRaises(transfer.TransferError):
                    worker.run(self.path)
                service.assert_not_called()
        finally:
            lease.close()

    def test_offline_worker_holds_server_lease_through_entire_backup(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "no service listener"))
        contender = worker.ServerLease(self.config["runtime_root"])
        service = Mock()

        def due_backup():
            self.assertFalse(contender.acquire(), "server startup must wait for offline snapshot")

        service.due_backup.side_effect = due_backup
        with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
             patch.object(worker, "DataTransfer", return_value=service):
            worker.run(self.path)
        service.due_backup.assert_called_once_with()
        self.assertTrue(contender.acquire(), "offline worker must release startup lease")
        contender.close()

    def test_connection_refused_cannot_bypass_a_server_startup_lease(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "no listener yet"))
        lease = worker.ServerLease(self.config["runtime_root"])
        self.assertTrue(lease.acquire())
        try:
            with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
                 patch.object(worker, "DataTransfer") as service:
                with self.assertRaises(transfer.TransferError) as caught:
                    worker.run(self.path)
                self.assertEqual(caught.exception.status, 409)
                service.assert_not_called()
        finally:
            lease.close()

    def test_timeout_and_unidentified_network_failure_never_enter_offline_mode(self):
        for failure in (urllib.error.URLError("unidentified network failure"),
                        urllib.error.URLError(TimeoutError("service probe timed out")), TimeoutError("service probe timed out")):
            opener = Mock()
            opener.open.side_effect = failure
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(worker.urllib.request, "build_opener", return_value=opener), \
                 patch.object(worker, "DataTransfer") as service:
                with self.assertRaises(transfer.TransferError):
                    worker.run(self.path)
                service.assert_not_called()

    def test_offline_backup_failure_releases_server_startup_lease(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "no listener"))
        contender = worker.ServerLease(self.config["runtime_root"])
        service = Mock()
        service.due_backup.side_effect = OSError("backup disk lost")
        with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
             patch.object(worker, "DataTransfer", return_value=service):
            with self.assertRaises(OSError):
                worker.run(self.path)
        self.assertTrue(contender.acquire())
        contender.close()


class ServerLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-server-lease-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_concurrent_lease_acquisition_allows_one_owner_and_releases_cleanly(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        leases = [worker.ServerLease(self.root) for _ in range(2)]
        results = []
        threads = []

        def acquire(lease):
            barrier.wait(timeout=3)
            result = lease.acquire()
            results.append(result)
            if result:
                release.wait(timeout=3)
                lease.close()

        try:
            for lease in leases:
                thread = threading.Thread(target=acquire, args=(lease,))
                thread.start()
                threads.append(thread)
            # The failed contender exits without waiting. Join it while the winner keeps its lease.
            for thread in threads:
                thread.join(timeout=.2)
            self.assertEqual(sorted(results), [False, True])
        finally:
            release.set()
            for thread in threads:
                thread.join(timeout=3)
            for lease in leases:
                lease.close()
        again = worker.ServerLease(self.root)
        self.assertTrue(again.acquire())
        again.close()

    def test_server_startup_cannot_initialize_business_stores_during_offline_copy(self):
        import test_server as fixture
        lease = worker.ServerLease(self.root / "runtime")
        self.assertTrue(lease.acquire())
        try:
            with patch.object(fixture.SERVER, "Workbench") as workbench:
                with self.assertRaises(RuntimeError):
                    fixture.SERVER.make_server(self.root / "skill", self.root / "runtime", port=0)
                workbench.assert_not_called()
        finally:
            lease.close()

    def test_server_initialization_has_lease_and_releases_it_on_failure(self):
        import test_server as fixture
        contender = worker.ServerLease(self.root / "runtime")

        def fail_initialization(*args, **kwargs):
            self.assertFalse(contender.acquire(), "store initialization must already hold the server lease")
            raise RuntimeError("isolated store initialization failure")

        with patch.object(fixture.SERVER, "Workbench", side_effect=fail_initialization):
            with self.assertRaises(RuntimeError):
                fixture.SERVER.make_server(self.root / "skill", self.root / "runtime", port=0)
        self.assertTrue(contender.acquire())
        contender.close()

    def test_server_close_releases_the_lifetime_lease(self):
        import test_server as fixture
        contender = worker.ServerLease(self.root / "runtime")
        with patch.object(fixture.SERVER, "Workbench", return_value=Mock()):
            server = fixture.SERVER.make_server(self.root / "skill", self.root / "runtime", port=0)
            try:
                self.assertFalse(contender.acquire())
            finally:
                server.server_close()
        self.assertTrue(contender.acquire())
        contender.close()


if __name__ == "__main__":
    unittest.main()
