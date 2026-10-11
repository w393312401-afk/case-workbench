"""Worker exit status and persisted failure messages using temporary files."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))
import backup_worker as worker
from data_transfer import TransferError


class BackupWorkerCompletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-worker-completion-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "settings.json"
        self.error_path = self.root / "worker-error.log"
        self.config = {
            "settings": {"enabled": True}, "port": 28767,
            "skill_root": str(self.root / "skill"), "runtime_root": str(self.root / "runtime"),
            "app_root": str(self.root / "app"),
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.argv = patch.object(sys, "argv", ["backup_worker.py", "--config", str(self.config_path)])
        self.argv.start()
        self.addCleanup(self.argv.stop)

    def test_successful_retry_for_an_already_completed_slot_clears_the_prior_error(self):
        self.error_path.write_text("previous HTTP 409: backup was busy", encoding="utf-8")
        before = self.config_path.read_bytes()
        live = {"app": "video-case-workbench", "token": "temporary-worker-token",
                "paths": {"records": str(self.root / "skill" / "cases"),
                          "library": str(self.root / "runtime" / "library")}}
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(json.dumps(live).encode()), io.BytesIO(b'{"unchanged":true}')]
        with patch.object(worker.urllib.request, "build_opener", return_value=opener), \
             patch.object(worker, "DataTransfer") as offline:
            self.assertEqual(worker.main(), 0)
        self.assertFalse(self.error_path.exists())
        self.assertEqual(self.config_path.read_bytes(), before)
        offline.assert_not_called()
        self.assertEqual(opener.open.call_args.args[0].get_method(), "POST")

    def test_success_without_a_prior_log_does_not_create_a_log(self):
        with patch.object(worker, "run", return_value=None) as run:
            self.assertEqual(worker.main(), 0)
        run.assert_called_once_with(str(self.config_path))
        self.assertFalse(self.error_path.exists())

    def test_current_failure_replaces_the_previous_message_and_returns_failure(self):
        self.error_path.write_text("old busy message", encoding="utf-8")
        with patch.object(worker, "run", side_effect=TransferError("backup directory unavailable", 503)):
            self.assertEqual(worker.main(), 1)
        self.assertEqual(self.error_path.read_text(encoding="utf-8"), "backup directory unavailable\n")

    def test_locked_old_log_does_not_turn_a_successful_backup_into_failure(self):
        self.error_path.write_text("locked old message", encoding="utf-8")
        with patch.object(worker, "run", return_value=None), \
             patch.object(Path, "unlink", side_effect=PermissionError("temporary log lock")) as unlink:
            self.assertEqual(worker.main(), 0)
        unlink.assert_called_once_with(missing_ok=True)
        self.assertEqual(self.error_path.read_text(encoding="utf-8"), "locked old message")


if __name__ == "__main__":
    unittest.main()
