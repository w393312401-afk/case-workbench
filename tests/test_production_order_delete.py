"""Permanent deletion of 内容制作 orders (archiving first is not required): record, request log and local files go; running ones are refused."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from elevenlabs_client import VoiceError
from test_production import Bench, CANDIDATE as VIDEO_CANDIDATE, FakeEleven
import test_production_carousel as carousel_fixture
from variant_parse import carousel_text, parse_carousels
from workbench_carousel import CarouselWorkbenchMixin
from workbench_carousel_stills import CarouselStillsMixin
from workbench_editing import EditingWorkbenchMixin
from workbench_order_delete import OrderDeleteMixin


class OrderBench(Bench, EditingWorkbenchMixin, CarouselWorkbenchMixin, CarouselStillsMixin, OrderDeleteMixin):
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
        key = "job_archive" if kind == "job" else "carousel_archive"
        return self.bench.production.mutate(key, {"request_id": uuid4().hex, "revision": self.revision(),
                                                  f"{kind}_id": identifier, "archived": archived})

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

    # -- carousel --------------------------------------------------------------------------------------------------
    def carousel(self):
        script = self.script(carousel_text(parse_carousels(carousel_fixture.CANDIDATE)[0]), carousel_fixture.CANDIDATE)
        item = self.bench.create_carousel({"request_id": uuid4().hex, "revision": self.revision(), "carousel": {"script_id": script["id"]}})["carousel"]
        folder = self.root / "carousels" / item["id"]
        for name in ("candidates", "searches/STS-0123456789ab", "outputs", "deliveries/DLV-0123456789ab"):
            (folder / name).mkdir(parents=True)
            (folder / name / "file.bin").write_bytes(b"y" * 1024)
        return item, folder

    def test_archived_carousel_goes_with_its_folder(self):
        item, folder = self.carousel()
        self.archive("carousel", item["id"])
        preview = self.bench.delete_carousel_order(item["id"], {"preview": True})
        self.assertEqual((preview["kind"], preview["files"], preview["published"]), ("carousel", 4, False))
        self.assertTrue(folder.is_dir())
        result = self.bench.delete_carousel_order(item["id"], {"request_id": uuid4().hex})
        self.assertEqual((result["deleted_carousel"], result["warnings"]), (item["id"], []))
        self.assertFalse(folder.exists())
        self.assertEqual([entry["id"] for entry in self.bench.production.public()["carousels"]], [])
        # The script version stays so the order can be created again.
        self.assertTrue(self.bench.creation.get_script(item["script_id"]))

    def test_a_running_generation_blocks_carousel_deletion(self):
        item, folder = self.carousel()
        self.archive("carousel", item["id"])
        data = self.bench.production.read()
        data["carousels"][0].setdefault("generations", []).append({"id": "IMG-0123456789ab", "status": "running", "slides": [1]})
        self.bench.production.write(data)
        with self.assertRaises(VoiceError) as caught:
            self.bench.delete_carousel_order(item["id"], {"request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue(folder.is_dir())


if __name__ == "__main__":
    unittest.main()
