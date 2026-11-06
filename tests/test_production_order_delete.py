"""Permanent deletion of 内容制作 orders (archiving first is not required): record, request log and local files go; running ones are refused."""
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from elevenlabs_client import VoiceError
from test_production import Bench, CANDIDATE as VIDEO_CANDIDATE, FakeEleven
from workbench_editing import EditingWorkbenchMixin
from workbench_order_delete import OrderDeleteMixin


class OrderBench(Bench, EditingWorkbenchMixin, OrderDeleteMixin):
    pass


class OrderDeleteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="order-delete-")
        self.addCleanup(temp.cleanup)
        self.bench = OrderBench(temp.name, FakeEleven())
        self.root = self.bench.production.root

    def revision(self):
        return self.bench.production.public()["revision"]

    def script(self, text, candidate):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": candidate, "candidate_sha256": "x", "snapshot": {}}
        return self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "稿", "text": text, "confirmed": True}}, source)["script"]

    def archive(self, kind, identifier, archived=True):
        return self.bench.production.mutate("job_archive", {"request_id": uuid4().hex, "revision": self.revision(),
                                                            "job_id": identifier, "archived": archived})

    # -- video -----------------------------------------------------------------------------------------------------
    def video_job(self):
        script = self.script("One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.", VIDEO_CANDIDATE)
        job = self.bench.create_production_job({"request_id": uuid4().hex, "revision": self.revision(),
                                                "job": {"script_id": script["id"]}})["job"]
        audio = self.root / "audio" / job["id"] / "TAK-0123456789ab"
        audio.mkdir(parents=True)
        (audio / "voiceover.mp3").write_bytes(b"x" * 2048)
        handoffs = self.root / "handoffs"
        handoffs.mkdir(exist_ok=True)
        (handoffs / f"{job['id']}-20261012-000000.md").write_text("交接", encoding="utf-8")
        (handoffs / "PRD-ffffffffffff-20261012-000000.md").write_text("别的单", encoding="utf-8")
        operations = self.root / "operations"
        operations.mkdir(exist_ok=True)
        record = {"schema_version": 1, "job_id": job["id"], "kind": "take", "state": "committed", "request_id": "r", "operation_key": "k", "body": {}}
        (operations / ("a" * 64 + ".json")).write_text(json.dumps(record), encoding="utf-8")
        return job

    def test_unarchived_video_job_can_be_deleted_directly(self):
        job = self.video_job()
        self.assertFalse(self.bench.production.job(job["id"]).get("archived"))
        result = self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertEqual(result["deleted_job"], job["id"])
        self.assertFalse((self.root / "audio" / job["id"]).exists())

    def test_preview_files_are_removed_with_the_job_and_running_previews_block_deletion(self):
        job = self.video_job()
        folder = self.root / "video-previews" / job["id"] / "timeline"
        folder.mkdir(parents=True)
        (folder / "preview.mp4").write_bytes(b"preview")
        self.bench.video_previews = Mock()
        self.bench.video_previews.busy.return_value = True
        with self.assertRaisesRegex(VoiceError, "视频预览"):
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertTrue(folder.is_dir())
        self.bench.video_previews.busy.return_value = False
        self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertFalse(folder.parent.exists())

    def test_archived_video_job_goes_with_its_audio_handoffs_and_finished_operation_records(self):
        job = self.video_job()
        self.archive("job", job["id"])
        preview = self.bench.delete_job_order(job["id"], {"preview": True})
        self.assertEqual((preview["kind"], preview["takes"], preview["edits"]), ("job", 0, 0))
        self.assertGreaterEqual(preview["files"], 3)
        self.assertTrue((self.root / "audio" / job["id"]).is_dir())
        body = {"request_id": uuid4().hex}
        result = self.bench.delete_job_order(job["id"], body)
        self.assertEqual((result["deleted_job"], result["warnings"]), (job["id"], []))
        self.assertFalse((self.root / "audio" / job["id"]).exists())
        self.assertEqual(list((self.root / "handoffs").glob(f"{job['id']}-*.md")), [])
        self.assertTrue((self.root / "handoffs" / "PRD-ffffffffffff-20261012-000000.md").is_file())
        self.assertEqual(list((self.root / "operations").glob("*.json")), [])
        with self.assertRaises(VoiceError) as caught:
            self.bench.production.job(job["id"])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.bench.delete_job_order(job["id"], body)["deleted_job"], job["id"])
        with self.assertRaises(VoiceError):
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})

    def test_a_pending_voice_operation_blocks_deletion(self):
        job = self.video_job()
        pending = {"schema_version": 1, "job_id": job["id"], "kind": "take", "state": "inflight", "request_id": "r2", "operation_key": "k2",
                   "take_id": "TAK-aaaaaaaaaaaa", "body": {}, "updated_at": "2026-10-12T00:00:00+00:00", "error": ""}
        (self.root / "operations" / ("b" * 64 + ".json")).write_text(json.dumps(pending), encoding="utf-8")
        self.archive("job", job["id"])
        with self.assertRaises(VoiceError) as caught:
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue((self.root / "audio" / job["id"]).is_dir())

    def test_a_build_that_never_reached_davinci_does_not_block_deletion(self):
        job = self.video_job()
        stale = {"schema_version": 1, "job_id": job["id"], "kind": "timeline", "state": "uncertain", "request_id": "r3", "operation_key": "c" * 64,
                 "take_id": "", "body": {}, "updated_at": "2026-10-14T03:07:02+00:00", "error": "达芬奇正在执行另一项工作台操作"}
        (self.root / "operations" / ("c" * 64 + ".json")).write_text(json.dumps(stale), encoding="utf-8")
        journaled = {**stale, "request_id": "r4", "operation_key": "d" * 64}
        (self.root / "operations" / ("d" * 64 + ".json")).write_text(json.dumps(journaled), encoding="utf-8")
        (self.root / "operations" / ("d" * 64)).mkdir()
        (self.root / "operations" / ("d" * 64) / "resolve.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(VoiceError) as caught:  # A journaled build may have changed DaVinci: recover it first.
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 409)
        shutil.rmtree(self.root / "operations" / ("d" * 64))
        (self.root / "operations" / ("d" * 64 + ".json")).unlink()
        self.assertEqual(self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})["deleted_job"], job["id"])
        self.assertEqual(list((self.root / "operations").glob("*.json")), [])

    def test_batch_handles_each_order_on_its_own(self):
        job, other = self.video_job(), self.video_job()
        request = {"request_id": uuid4().hex, "action": "archive", "items": [{"kind": "job", "id": job["id"]}, {"kind": "job", "id": other["id"]}]}
        self.assertEqual(self.bench.batch_orders(request)["changed"], 2)
        self.assertTrue(self.bench.production.job(job["id"]).get("archived"))
        result = self.bench.batch_orders({"request_id": uuid4().hex, "action": "delete", "items": [{"kind": "job", "id": job["id"]}]})
        self.assertEqual(result["changed"], 1)
        with self.assertRaises(VoiceError):
            self.bench.batch_orders({"request_id": uuid4().hex, "action": "delete", "items": [{"kind": "carousel", "id": "CAR-000000000001"}]})


if __name__ == "__main__":
    unittest.main()
