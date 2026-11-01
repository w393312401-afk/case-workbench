"""Per-slide tables, AI clip picks and data backups appear in the task panel, using disposable data only."""
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from elevenlabs_client import VoiceError
from task_activity import ActivityStore, reconcile_activity
import data_transfer as transfer
from test_ai_tasks import FakeBridge
import test_data_transfer as transfer_fixture


def runs(store, kind):
    return [row for row in store.feed(limit=200)["items"] if row["kind"] == kind]


class DataActivityTests(unittest.TestCase):
    setUp = transfer_fixture.DataTransferTests.setUp
    settings = transfer_fixture.DataTransferTests.settings
    fake_archive = transfer_fixture.DataTransferTests.fake_archive

    def test_backup_and_export_runs_record_outcome_and_keep_packing_progress_sparse(self):
        store = self.service.activity = ActivityStore(self.runtime / "activity")
        self.service.config["settings"] = self.settings(keep=0)

        def archive(destination, kind="backup", preferences=None):
            for number in range(50):
                self.service.manager.on_progress(f"正在打包 {number}/50：file{number}")
            self.service.manager.on_progress("正在保存数据库快照：cases.sqlite")
            return self.fake_archive(destination, kind, preferences)
        with patch.object(self.service.manager, "create_archive", side_effect=archive):
            self.service.backup(automatic=True)
        run = runs(store, "data")[0]
        self.assertEqual((run["title"], run["status"], run["url"]), ("自动备份", "completed", "#/all/settings"))
        self.assertIn(".zip", run["message"])
        messages = [row["message"] for row in store.logs(run["run_id"])["logs"]]
        self.assertLessEqual(sum(text.startswith("正在打包") for text in messages), 2)
        self.assertIn("正在保存数据库快照：cases.sqlite", messages)
        self.service.ensure_idle = Mock(side_effect=transfer.TransferError("AI 协作正在运行，请结束后再备份。", 409))
        with self.assertRaises(transfer.TransferError):
            self.service.backup()
        failed = next(row for row in runs(store, "data") if row["status"] == "failed")
        self.assertEqual((failed["title"], failed["message"]), ("总数据备份", "AI 协作正在运行，请结束后再备份。"))
        self.service.ensure_idle = Mock()
        with patch.object(self.service.manager, "create_archive", side_effect=self.fake_archive):
            self.service.export({"preferences": {}})
        self.assertEqual(sum(row["title"] == "总数据导出" and row["status"] == "completed" for row in runs(store, "data")), 1)
        # Inspect and restore are not task runs; their progress never lands on an earlier run.
        before = sum(len(store.logs(row["run_id"])["logs"]) for row in runs(store, "data"))
        self.service._progress("正在读取备份包")
        self.assertEqual(sum(len(store.logs(row["run_id"])["logs"]) for row in runs(store, "data")), before)


if __name__ == "__main__":
    unittest.main()
