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
import test_production_carousel_clips as clips_fixture
import test_production_carousel_table as table_fixture


def runs(store, kind):
    return [row for row in store.feed(limit=200)["items"] if row["kind"] == kind]


class TableActivityTests(unittest.TestCase):
    fixture = table_fixture.CarouselTableTests.fixture
    stop_server = table_fixture.CarouselTableTests.stop_server
    request = table_fixture.CarouselTableTests.request
    setUp = table_fixture.CarouselTableTests.setUp
    ok = table_fixture.CarouselTableTests.ok
    script = table_fixture.CarouselTableTests.script
    wait = table_fixture.CarouselTableTests.wait
    start = table_fixture.CarouselTableTests.start

    def test_table_run_follows_progress_and_outcome_and_links_to_its_script(self):
        self.server.app.ai.bridge = FakeBridge(output=table_fixture.TABLE)
        script = self.script()
        table = self.wait(self.start(script)["id"])
        activity = self.server.app.activity
        run = runs(activity, "table")[0]
        self.assertEqual((run["owner_id"], run["source_id"], run["status"], run["result_count"]), (script["id"], table["id"], "completed", 1))
        self.assertEqual(run["url"], f"#/202H/scripts/{script['id']}?table={table['id']}")
        self.assertIn("测试助手已接单。", [row["message"] for row in activity.logs(run["run_id"])["logs"]])
        self.server.app.ai.bridge = FakeBridge(output="好的，已经做好了。")
        failed = self.wait(self.start(script)["id"])
        run = next(row for row in runs(activity, "table") if row["source_id"] == failed["id"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("逐张表", run["message"])


@unittest.skipUnless(clips_fixture.HAVE_FFMPEG, "需要本机 ffmpeg/ffprobe")
class ClipActivityTests(unittest.TestCase):
    listing = clips_fixture.ClipTests.listing

    def setUp(self):
        clips_fixture.ClipTests.setUp(self)
        self.bench.activity = ActivityStore(Path(self.bench.production.root) / "activity-test")

    def test_pick_run_logs_progress_reports_gaps_and_never_forwards_the_reply(self):
        key = self.listing()["clips"][0]["key"]
        answer = json.dumps({"slides": {"1": [{"key": key, "reason": "ok"}]}, "gaps": [{"slide": 2, "need": "一排刀"}]})

        def reply(prompt, **options):
            options["on_progress"]("Read candidates.md")
            return answer
        self.bench.fake_ai.run = reply
        self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials, "per_slide": 1})
        run = runs(self.bench.activity, "clips")[0]
        self.assertEqual((run["owner_id"], run["status"], run["result_count"]), (self.item["id"], "needs_attention", 1))
        self.assertIn("缺合适画面", run["message"])
        self.assertIn("step=stills", run["url"])
        self.assertEqual(len(self.bench.activity.logs(run["run_id"])["logs"]), 3)
        self.bench.fake_ai.run = lambda prompt, **options: "我没法回答"
        with self.assertRaises(VoiceError):
            self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials})
        failed = [row for row in runs(self.bench.activity, "clips") if row["status"] == "failed"]
        self.assertEqual(len(failed), 1)
        self.assertNotIn("我没法回答", failed[0]["message"])


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


class ReconcileKindsTests(unittest.TestCase):
    def test_tables_and_data_runs_survive_restart_without_a_production_owner(self):
        temporary = tempfile.TemporaryDirectory(prefix="activity-kinds-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store = ActivityStore(root / "runtime")
        tables = root / "production" / "carousel_tables"
        (tables / "TBL-000000000001").mkdir(parents=True)
        (tables / "TBL-000000000001" / "state.json").write_text(json.dumps({"id": "TBL-000000000001", "status": "running"}), encoding="utf-8")
        store.record("table:TBL-000000000001", kind="table", owner_id="SCR-1", source_id="TBL-000000000001", product="202H", status="running", message="正在生成")
        store.record("table:TBL-000000000002", kind="table", owner_id="SCR-1", source_id="TBL-000000000002", product="202H", status="completed", message="已生成")
        store.record("data:abc", kind="data", owner_id="data", source_id="abc", status="running", message="正在打包")
        app = SimpleNamespace(activity=store, lock=threading.RLock(), runtime=root / "runtime", ai=SimpleNamespace(_locations={}),
                              production=SimpleNamespace(root=root / "production", read=lambda: {"jobs": [], "carousels": []}))
        reconcile_activity(app)
        rows = {row["run_id"]: row for row in store.feed()["items"]}
        self.assertEqual(rows["table:TBL-000000000001"]["status"], "interrupted")
        self.assertNotIn("table:TBL-000000000002", rows)
        self.assertEqual(rows["data:abc"]["status"], "interrupted")
        self.assertIn("备份记录", rows["data:abc"]["message"])
        # A run without a product shows in every product's view.
        self.assertIn("data:abc", {row["run_id"] for row in store.feed(product="202H")["items"]})
        self.assertNotIn("table:TBL-000000000001", {row["run_id"] for row in store.feed(product="129N")["items"]})


if __name__ == "__main__":
    unittest.main()
