"""Permanent deletion of 内容制作 orders (archiving first is not required): record, request log and local files go; running ones are refused."""
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from elevenlabs_client import VoiceError
from production_operations import file_lock
from test_production import Bench, CANDIDATE as VIDEO_CANDIDATE, FakeEleven
from workbench_content import ContentWorkbenchMixin
from workbench_creation import CreationWorkbenchMixin
from workbench_editing import EditingWorkbenchMixin
from workbench_order_delete import OrderDeleteMixin


class OrderBench(Bench, EditingWorkbenchMixin, OrderDeleteMixin):
    pass


class CandidateBench(OrderBench, CreationWorkbenchMixin, ContentWorkbenchMixin):
    pass


class FakeResolve:
    """Stands in for ResolveBridge: records calls and answers timeline_ids / delete_timelines."""

    def __init__(self, present=(), closed=False, error=None):
        self.present, self.is_closed, self.error, self.calls = set(present), closed, error, []

    def closed(self):
        return self.is_closed

    def busy(self):
        return False

    def run(self, command, payload=None, timeout=180, held=False, launch=True):
        self.calls.append((command, payload, launch))
        if self.error:
            raise VoiceError(409, self.error)
        if command == "timeline_ids":
            return {"ok": True, "project": "202H", "unique_ids": sorted(self.present)}
        wanted = payload["unique_ids"]
        deleted = [uid for uid in wanted if uid in self.present]
        self.present -= set(deleted)
        return {"ok": True, "project": "202H", "deleted": deleted, "missing": [uid for uid in wanted if uid not in deleted],
                "failed": [], "saved": True}


def add_timelines(bench, job_id, *rows):
    data = bench.production.read()
    record = next(row for row in data["jobs"] if row["id"] == job_id)
    record["timelines"].extend({"take_id": "", "project": "202H", **row} for row in rows)
    bench.production.write(data)


class CandidateOrderDeleteTests(unittest.TestCase):
    """Deleting an order from the candidate desk also puts its draft back, unless the script is used elsewhere."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="candidate-order-delete-")
        self.addCleanup(temp.cleanup)
        self.bench = CandidateBench(temp.name, FakeEleven())
        self.bench.ai = Mock()
        self.bench.ai.settle_variant_choices.return_value = {"task_id": "AIT-0123456789ab"}
        self.script = self.bench.creation.mutate("script", {
            "request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "变体 #1", "text": "One kit, replacing this jig.\nFull 202H kit's below.", "confirmed": True}},
            {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H", "case_id": None,
             "candidate_text": VIDEO_CANDIDATE, "candidate_sha256": "x", "snapshot": {}})["script"]

    def order(self):
        return self.bench.create_production_job({"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                 "job": {"script_id": self.script["id"]}})["job"]["id"]

    def archived(self):
        return self.bench.creation.get_script(self.script["id"])["archived"]

    def made(self):
        return self.bench.ai.settle_variant_choices.call_args.args[1]

    def test_only_order_returns_the_draft_and_the_script_can_be_restored(self):
        job = self.order()
        preview = self.bench.delete_candidate_order(job, {"preview": True})
        self.assertEqual((preview["draft_returned"], preview["draft_kept"]), (True, ""))
        self.assertFalse(self.archived())
        self.assertTrue(self.bench.production.job(job))
        result = self.bench.delete_candidate_order(job, {"request_id": uuid4().hex})
        self.assertEqual((result["deleted_job"], result["draft_returned"], result["warnings"]), (job, True, []))
        with self.assertRaises(VoiceError):
            self.bench.production.job(job)
        self.assertTrue(self.archived())
        self.assertEqual(self.made(), {})
        self.bench.ai.release_adoption.assert_called_once_with("AIT-0123456789ab")
        self.assertEqual(result["task"], {"task_id": "AIT-0123456789ab"})

    def test_retrying_the_same_request_changes_nothing_more(self):
        job, body = self.order(), {"request_id": uuid4().hex}
        first = self.bench.delete_candidate_order(job, body)
        again = self.bench.delete_candidate_order(job, body)
        self.assertEqual((first["deleted_job"], again["deleted_job"], again["draft_returned"]), (job, job, True))
        self.assertTrue(self.archived())

    def test_script_stays_while_another_order_uses_it(self):
        first, second = self.order(), self.order()
        preview = self.bench.delete_candidate_order(first, {"preview": True})
        self.assertFalse(preview["draft_returned"])
        self.assertIn("别的制作单", preview["draft_kept"])
        self.assertFalse(self.bench.delete_candidate_order(first, {"request_id": uuid4().hex})["draft_returned"])
        self.assertFalse(self.archived())
        self.bench.ai.release_adoption.assert_not_called()
        self.assertEqual(self.made(), {("AIT-0123456789ab", "AIR-0123456789ab"): {0}})
        self.assertTrue(self.bench.delete_candidate_order(second, {"request_id": uuid4().hex})["draft_returned"])
        self.assertTrue(self.archived())

    def test_published_script_is_never_archived_by_the_delete(self):
        job = self.order()
        self.bench.creation.mutate("script_status", {"request_id": uuid4().hex, "revision": self.bench.creation.current_revision(),
                                                     "script_status": {"script_id": self.script["id"], "status": "published",
                                                                       "video_id": "1234567", "published_on": "2026-11-01"}})
        self.assertFalse(self.bench.delete_candidate_order(job, {"request_id": uuid4().hex})["draft_returned"])
        self.assertFalse(self.archived())

    def test_script_stays_by_default_and_goes_only_when_ticked(self):
        job = self.order()
        preview = self.bench.delete_candidate_order(job, {"preview": True})
        self.assertTrue(preview["script"]["deletable"])
        self.assertEqual(preview["script"]["versions"], 1)
        result = self.bench.delete_candidate_order(job, {"request_id": uuid4().hex, "delete_script": True})
        self.assertTrue(result["script_deleted"])
        self.assertTrue(result["draft_returned"])
        with self.assertRaises(Exception):
            self.bench.creation.get_script(self.script["id"])
        self.bench.ai.release_adoption.assert_called_once_with("AIT-0123456789ab")

    def test_ticking_a_script_another_order_uses_refuses_the_whole_delete(self):
        first, second = self.order(), self.order()
        preview = self.bench.delete_candidate_order(first, {"preview": True})
        self.assertFalse(preview["script"]["deletable"])
        self.assertIn("别的制作单", preview["script"]["blocker"])
        with self.assertRaisesRegex(VoiceError, "别的制作单"):
            self.bench.delete_candidate_order(first, {"request_id": uuid4().hex, "delete_script": True})
        self.assertTrue(self.bench.production.job(first))
        self.assertTrue(self.bench.creation.get_script(self.script["id"]))

    def test_order_with_running_work_is_refused_and_nothing_moves(self):
        job = self.order()
        self.bench._job_blockers = lambda record: "这张制作单正在生成配音，请等它结束。"
        with self.assertRaises(VoiceError):
            self.bench.delete_candidate_order(job, {"request_id": uuid4().hex})
        self.assertTrue(self.bench.production.job(job))
        self.assertFalse(self.archived())


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

    def generated_take(self):
        job = self.video_job()
        body = {"request_id": uuid4().hex, "voice_id": "VOICE123456789", "model_id": "eleven_v4", "settings": {"stability": 0.45}}
        take = self.bench.generate_take(job["id"], body)["take"]
        return job, take, body

    def test_delete_take_previews_and_clears_selection_without_recovery(self):
        job, take, generated = self.generated_take()
        identifier, take_id = job["id"], take["id"]
        self.bench.select_take(identifier, {"request_id": uuid4().hex, "revision": self.revision(), "take_id": take_id})
        folder = self.bench.production.audio / identifier / take_id
        preview = self.bench.delete_take(identifier, take_id, {"preview": True})
        self.assertTrue(preview["selected"])
        self.assertEqual(preview["blockers"], [])
        self.assertGreater(preview["files"], 0)
        self.assertTrue(folder.exists())
        body = {"request_id": uuid4().hex, "revision": self.revision()}
        result = self.bench.delete_take(identifier, take_id, body)
        self.assertEqual(result["deleted_take"], take_id)
        self.assertEqual(result["warnings"], [])
        self.assertFalse(folder.exists())
        self.assertEqual(result["job"]["selected_take"], "")
        self.assertEqual(result["job"]["status"], "draft")
        self.assertIn(take_id, self.bench.production.job(identifier)["deleted_take_ids"])
        self.assertEqual(self.bench.delete_take(identifier, take_id, body)["deleted_take"], take_id)
        with self.assertRaisesRegex(VoiceError, "已删除"):
            self.bench.generate_take(identifier, generated)
        with self.assertRaisesRegex(VoiceError, "已删除"):
            self.bench.production.mutate("take", {"job_id": identifier, "request_id": uuid4().hex}, take)

    def test_take_delete_blocks_references_and_stale_preview(self):
        job, take, _ = self.generated_take()
        identifier, take_id = job["id"], take["id"]
        old_revision = self.revision()
        data = self.bench.production.read()
        record = next(row for row in data["jobs"] if row["id"] == identifier)
        record["timelines"].append({"unique_id": "existing", "name": "旧时间线", "take_id": take_id, "archived": True})
        self.bench.production.write(data)
        preview = self.bench.delete_take(identifier, take_id, {"preview": True})
        self.assertTrue(any("时间线" in item for item in preview["blockers"]))
        with self.assertRaises(VoiceError):
            self.bench.delete_take(identifier, take_id, {"request_id": uuid4().hex, "revision": old_revision})
        self.assertTrue((self.bench.production.audio / identifier / take_id).exists())
        data = self.bench.production.read()
        record = next(row for row in data["jobs"] if row["id"] == identifier)
        record["timelines"] = []
        record["edits"].append({"id": "EDT-aaaaaaaaaaaa", "take_id": take_id, "status": "completed"})
        self.bench.production.write(data)
        self.assertTrue(any("剪辑" in item for item in self.bench.delete_take(identifier, take_id, {"preview": True})["blockers"]))

    def test_take_delete_refuses_inflight_build_and_reports_cleanup_failure(self):
        job, take, _ = self.generated_take()
        identifier, take_id = job["id"], take["id"]
        operations = self.bench._production_operations()
        with file_lock(operations.root / "locks" / (identifier + ".lock"), "occupied"):
            with self.assertRaisesRegex(VoiceError, "建立时间线"):
                self.bench.delete_take(identifier, take_id, {"preview": True})
        operation = operations.prepare("timeline", {"request_id": uuid4().hex, "job_id": identifier}, {"take_id": take_id})
        preview = self.bench.delete_take(identifier, take_id, {"preview": True})
        self.assertTrue(preview["blockers"])
        with self.assertRaises(VoiceError):
            self.bench.delete_take(identifier, take_id, {"request_id": uuid4().hex, "revision": self.revision()})
        operations.update(operation, "safe_failed")
        body = {"request_id": uuid4().hex, "revision": self.revision()}
        with patch("workbench_order_delete.shutil.rmtree", side_effect=OSError("fixture failure")):
            result = self.bench.delete_take(identifier, take_id, body)
        self.assertTrue(result["warnings"])
        self.assertTrue(self.bench.delete_take(identifier, take_id, body)["warnings"])
        self.assertTrue((self.bench.production.audio / identifier / take_id).exists())

    def test_unarchived_video_job_can_be_deleted_directly(self):
        job = self.video_job()
        self.assertFalse(self.bench.production.job(job["id"]).get("archived"))
        result = self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
        self.assertEqual(result["deleted_job"], job["id"])
        self.assertFalse((self.root / "audio" / job["id"]).exists())

    def test_order_delete_refuses_linked_asset_ancestors_before_mutating(self):
        job = self.video_job()
        is_junction = Path.is_junction
        for parent in ("audio", "handoffs"):
            with self.subTest(parent=parent), patch.object(
                Path, "is_junction", lambda path: path == self.root / parent or is_junction(path)
            ):
                with self.assertRaisesRegex(VoiceError, "无法安全清理"):
                    self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex})
            self.assertEqual(self.bench.production.job(job["id"])["id"], job["id"])
            self.assertTrue((self.root / "audio" / job["id"]).is_dir())

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

    # -- DaVinci timelines ------------------------------------------------------------------------------------------
    def test_preview_lists_timelines_and_which_are_in_the_open_project(self):
        job = self.video_job()
        add_timelines(self.bench, job["id"], {"unique_id": "TL-1", "name": "202H-01"},
                      {"unique_id": "TL-2", "name": "202H-01-旧", "archived": True, "project": "别的项目"})
        self.bench.resolve = FakeResolve(present={"TL-1"})
        info = self.bench.delete_job_order(job["id"], {"preview": True})["resolve"]
        self.assertEqual((info["state"], info["project"]), ("open", "202H"))
        self.assertEqual([(row["unique_id"], row["in_open_project"], row["archived"]) for row in info["timelines"]],
                         [("TL-1", True, False), ("TL-2", False, True)])
        self.assertEqual(self.bench.resolve.calls, [("timeline_ids", {}, False)])
        self.assertTrue(self.bench.production.job(job["id"]))

    def test_ticked_timelines_go_after_the_record_and_leftovers_are_named(self):
        job = self.video_job()
        add_timelines(self.bench, job["id"], {"unique_id": "TL-1", "name": "202H-01"}, {"unique_id": "TL-2", "name": "202H-02"},
                      {"unique_id": "TL-3", "name": "202H-03"})
        self.bench.resolve = FakeResolve(present={"TL-1", "TL-3"})
        result = self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex, "delete_timelines": ["TL-1", "TL-2"]})
        self.assertEqual(result["resolve_deleted"], ["202H-01"])
        self.assertEqual(self.bench.resolve.present, {"TL-3"})
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("202H-02", result["warnings"][0])
        self.assertEqual(self.bench.resolve.calls[-1][2], False)
        with self.assertRaises(VoiceError):
            self.bench.production.job(job["id"])

    def test_closed_or_failing_davinci_never_stops_the_order_delete(self):
        for resolve in (FakeResolve(present={"TL-1"}, closed=True), FakeResolve(present={"TL-1"}, error="达芬奇正在执行另一项工作台操作")):
            job = self.video_job()
            add_timelines(self.bench, job["id"], {"unique_id": "TL-1", "name": "202H-01"})
            self.bench.resolve = resolve
            preview = self.bench.delete_job_order(job["id"], {"preview": True})["resolve"]
            self.assertNotEqual(preview["state"], "open")
            self.assertTrue(preview["message"])
            result = self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex, "delete_timelines": ["TL-1"]})
            self.assertEqual(result["deleted_job"], job["id"])
            self.assertTrue(any("202H-01" in warning and "手动删除" in warning for warning in result["warnings"]))
            self.assertEqual(resolve.present, {"TL-1"})
            self.assertFalse(any(call[0] == "delete_timelines" and call[2] for call in resolve.calls))

    def test_a_timeline_another_order_records_cannot_be_ticked(self):
        job, other = self.video_job(), self.video_job()
        add_timelines(self.bench, job["id"], {"unique_id": "TL-1", "name": "202H-01"})
        add_timelines(self.bench, other["id"], {"unique_id": "TL-1", "name": "202H-01"})
        self.bench.resolve = FakeResolve(present={"TL-1"})
        self.assertTrue(self.bench.delete_job_order(job["id"], {"preview": True})["resolve"]["timelines"][0]["shared"])
        with self.assertRaisesRegex(VoiceError, "别的制作单"):
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex, "delete_timelines": ["TL-1"]})
        self.assertTrue(self.bench.production.job(job["id"]))
        with self.assertRaises(VoiceError):
            self.bench.delete_job_order(job["id"], {"request_id": uuid4().hex, "delete_timelines": ["TL-9"]})

    def test_worker_deletes_only_listed_timelines_in_the_open_project(self):
        import resolve_worker

        class Timeline:
            def __init__(self, uid):
                self.uid = uid

            def GetUniqueId(self):
                return self.uid

        class Project:
            def __init__(self):
                self.items = [Timeline("A"), Timeline("B"), Timeline("C")]
                self.current = self.items[0]

            def GetName(self):
                return "202H"

            def GetTimelineCount(self):
                return len(self.items)

            def GetTimelineByIndex(self, index):
                return self.items[index - 1]

            def GetCurrentTimeline(self):
                return self.current

            def SetCurrentTimeline(self, item):
                self.current = item
                return True

            def GetMediaPool(self):
                project = self

                class Pool:
                    def DeleteTimelines(self, doomed):
                        project.items = [item for item in project.items if item not in doomed]
                        return True
                return Pool()

        project = Project()
        manager = Mock()
        manager.GetCurrentProject.return_value = project
        manager.SaveProject.return_value = True
        resolve = Mock()
        resolve.GetProjectManager.return_value = manager
        result = resolve_worker.delete_timelines(resolve, {"unique_ids": ["A", "Z"]})
        self.assertEqual((result["deleted"], result["missing"], result["failed"], result["saved"]), (["A"], ["Z"], [], True))
        self.assertEqual([item.uid for item in project.items], ["B", "C"])
        self.assertEqual(project.current.uid, "B")
        self.assertEqual(resolve_worker.timeline_ids(resolve, {})["unique_ids"], ["B", "C"])

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
