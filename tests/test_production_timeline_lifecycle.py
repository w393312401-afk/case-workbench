"""Archive is reversible metadata, while manual progress never invents an AI run."""
from __future__ import annotations

import copy
import tempfile
import unittest
from uuid import uuid4

import test_server as http_fixture
import test_production_manual_completion as fixture
from direction_store import now
from elevenlabs_client import VoiceError
from production_store import ProductionStore, current_edit, current_timeline, job_status, manual_completion_current, public_job


class TimelineLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="timeline-lifecycle-")
        self.addCleanup(temp.cleanup)
        self.store = ProductionStore(temp.name)
        data = self.store.empty()
        job = fixture.job_fixture()
        job["timelines"].append({**copy.deepcopy(job["timelines"][0]), "unique_id": "uid-2", "name": "Current"})
        data["jobs"].append(job)
        self.store.write(data)

    def mutate(self, kind, extra=None, **values):
        return self.store.mutate(kind, {"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                                       "job_id": fixture.JOB_ID, "timeline_uid": "uid-2", **values}, extra)

    def change(self, change):
        data = self.store.read()
        change(data["jobs"][0])
        self.store.write(data)

    def test_legacy_projection_is_read_only_and_independent(self):
        before = self.store.path.read_bytes()
        job = self.store.job(fixture.JOB_ID)
        self.assertEqual([(row["archived"], row["task_status"]) for row in job["timelines"]],
                         [(False, "pending"), (False, "pending")])
        self.assertEqual(self.store.path.read_bytes(), before)
        source = fixture.job_fixture()
        source["edits"] = [{"id": "EDT-0123456789ab", "timeline_uid": "uid-1", "status": "completed"}]
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "in_progress")
        source["edits"][0]["approval"] = {"at": now()}
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "in_progress")
        source["edits"][0]["status"] = "awaiting_review"
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "completed")
        source["edits"][0]["status"] = "needs_repair"
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "in_progress")
        source["manual_completion"] = {"at": now(), "timeline_uid": "uid-1", "take_id": "another-take"}
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "in_progress")
        source["manual_completion"]["take_id"] = fixture.TAKE_ID
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "completed")
        source["timelines"][0]["task_status"] = "pending"
        self.assertEqual(public_job(source)["timelines"][0]["task_status"], "pending")
        self.assertEqual(source["edits"][0]["status"], "needs_repair")

    def test_archive_restore_preserves_content_order_receipt_and_replays(self):
        completed = self.mutate("job_completion", completed=True)["job"]
        receipt = copy.deepcopy(completed["manual_completion"])
        original = copy.deepcopy(completed["timelines"])
        body = {"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                "job_id": fixture.JOB_ID, "timeline_uid": "uid-2", "archived": True}
        first = self.store.mutate("timeline_archive", body)
        archived = first["job"]
        self.assertEqual(archived["manual_completion"], receipt)
        self.assertFalse(manual_completion_current(archived))
        self.assertEqual(current_timeline(archived)["unique_id"], "uid-1")
        self.assertEqual(job_status(archived), "timeline")
        self.assertEqual(archived["edits"], [])
        self.assertTrue(first["timeline"]["archived"])
        self.assertEqual(first["timeline"]["task_status"], "completed")
        saved = self.store.path.read_bytes()
        self.assertEqual(self.store.mutate("timeline_archive", body), first)
        self.assertEqual(self.store.path.read_bytes(), saved)
        restored = self.mutate("timeline_archive", archived=False)["job"]
        self.assertTrue(manual_completion_current(restored))
        self.assertEqual(restored["status"], "approved")
        self.assertEqual([row["unique_id"] for row in restored["timelines"]], ["uid-1", "uid-2"])
        for row, before in zip(restored["timelines"], original):
            self.assertEqual({key: row[key] for key in before}, before)
        reloaded = ProductionStore(self.store.root.parent)
        self.assertFalse(reloaded.job(fixture.JOB_ID)["timelines"][-1]["archived"])
        self.assertEqual(restored["manual_completion"], receipt)

    def test_archiving_all_timelines_leaves_voice_ready(self):
        self.mutate("timeline_archive", archived=True)
        job = self.mutate("timeline_archive", timeline_uid="uid-1", archived=True)["job"]
        self.assertIsNone(current_timeline(job))
        self.assertEqual(job_status(job), "voiced")

    def test_archived_edit_does_not_determine_status_or_accept_approval(self):
        self.change(lambda job: job["edits"].append({"id": "EDT-0123456789ab", "timeline_uid": "uid-2",
                                                    "status": "awaiting_review", "approval": {"at": now()}}))
        job = self.mutate("timeline_archive", archived=True)["job"]
        self.assertEqual(job_status(job), "timeline")
        self.assertFalse(current_edit(job, job["edits"][-1]))
        self.assertEqual(job["timelines"][-1]["task_status"], "completed")
        with self.assertRaisesRegex(VoiceError, "当前版本"):
            self.mutate("edit_approve", edit_id="EDT-0123456789ab", approved=True)

    def test_new_work_does_not_revive_old_receipt_when_restored(self):
        self.mutate("job_completion", completed=True)
        self.mutate("timeline_archive", archived=True)
        self.mutate("timeline", {"unique_id": "uid-new", "name": "New", "take_id": fixture.TAKE_ID, "built_at": now()})
        job = self.mutate("timeline_archive", archived=False)["job"]
        self.assertNotIn("manual_completion", job)
        self.assertEqual(job["status"], "timeline")
        self.assertEqual(current_timeline(job)["unique_id"], "uid-new")
        self.assertEqual(job["timelines"][1]["task_status"], "completed")

    def test_task_status_does_not_fabricate_or_mutate_ai_and_review_records(self):
        self.change(lambda job: job["edits"].append({"id": "EDT-0123456789ab", "timeline_uid": "uid-2",
                                                    "status": "needs_repair", "report": {"saved": True}}))
        before = copy.deepcopy(self.store.job(fixture.JOB_ID)["edits"])
        for status in ("completed", "pending", "in_progress"):
            result = self.mutate("timeline_task_status", task_status=status)
            self.assertEqual(result["timeline"]["task_status"], status)
            self.assertEqual(result["job"]["edits"], before)
            self.assertEqual(result["job"]["status"], "needs_repair")
            self.assertNotIn("manual_completion", result["job"])
        self.mutate("timeline_task_status", timeline_uid="uid-1", task_status="completed")
        self.assertEqual(len(self.store.job(fixture.JOB_ID)["edits"]), 1)

    def test_manual_completion_and_revocation_sync_progress(self):
        job = self.mutate("job_completion", completed=True)["job"]
        self.assertEqual(job["timelines"][-1]["task_status"], "completed")
        self.mutate("timeline_task_status", task_status="in_progress")
        job = self.mutate("job_completion", completed=True)["job"]
        self.assertEqual(job["timelines"][-1]["task_status"], "completed")
        job = self.mutate("job_completion", completed=False, completion_at=job["manual_completion"]["at"])["job"]
        self.assertEqual(job["timelines"][-1]["task_status"], "pending")

    def test_ai_start_resets_progress_and_archived_target_is_rejected(self):
        self.mutate("timeline_task_status", task_status="completed")
        edit = {"id": "EDT-0123456789ab", "timeline_uid": "uid-2", "timeline_name": "Current",
                "status": "running", "started_at": now(), "materials": []}
        self.mutate("timeline_archive", archived=True)
        with self.assertRaisesRegex(VoiceError, "已归档"):
            self.mutate("edit_start", edit)
        self.mutate("timeline_archive", archived=False)
        job = self.mutate("edit_start", edit)["job"]
        self.assertEqual(job["timelines"][-1]["task_status"], "in_progress")

    def test_running_and_queued_edit_block_metadata_changes_but_unrelated_edits_do_not(self):
        for queued in (False, True):
            with self.subTest(queued=queued):
                self.change(lambda job: (job["timelines"][-1].update(operation_key="op-2"), job.update(edits=[{
                    "id": "EDT-0123456789ab", "timeline_uid": "queued-op" if queued else "uid-2", "status": "running",
                    "waiting_timeline": queued, "timeline_operation_key": "op-2"}])) )
                before = self.store.path.read_bytes()
                for kind, values in (("timeline_archive", {"archived": True}),
                                     ("timeline_task_status", {"task_status": "completed"})):
                    with self.assertRaisesRegex(VoiceError, "仍在运行"):
                        self.mutate(kind, **values)
                    self.assertEqual(self.store.path.read_bytes(), before)
                self.assertTrue(self.mutate("timeline_archive", timeline_uid="uid-1", archived=True)["timeline"]["archived"])

    def test_invalid_stale_archived_and_missing_changes_do_not_write(self):
        for kind, values, status in (("timeline_archive", {"archived": "yes"}, 400),
                                     ("timeline_task_status", {"task_status": "approved"}, 400),
                                     ("timeline_task_status", {"task_status": []}, 400),
                                     ("timeline_archive", {"archived": True, "timeline_uid": "missing"}, 404),
                                     ("timeline_archive", {"archived": True, "revision": "stale"}, 409)):
            before = self.store.path.read_bytes()
            with self.subTest(values=values), self.assertRaises(VoiceError) as caught:
                self.mutate(kind, **values)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(self.store.path.read_bytes(), before)
        self.mutate("timeline_archive", archived=True)
        with self.assertRaisesRegex(VoiceError, "时间线已归档"):
            self.mutate("timeline_task_status", task_status="completed")
        self.change(lambda job: job.update(archived=True))
        with self.assertRaisesRegex(VoiceError, "制作单已归档"):
            self.mutate("timeline_archive", archived=False)


class TimelineLifecycleHTTPTests(unittest.TestCase):
    setUp = http_fixture.WorkbenchHTTPTests.setUp
    stop_server = http_fixture.WorkbenchHTTPTests.stop_server
    request = http_fixture.WorkbenchHTTPTests.request
    seed_job = fixture.ManualCompletionHTTPTests.seed_job

    def body(self, **values):
        return {"request_id": uuid4().hex, "revision": self.server.app.production.public()["revision"],
                "timeline_uid": "uid-1", **values}

    def test_routes_persist_archive_restore_and_progress_without_resolve_calls(self):
        identifier = self.seed_job()
        base = f"/api/production/jobs/{identifier}"
        status, _, result = self.request("POST", base + "/timeline/task-status", self.body(task_status="completed"))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["timeline"]["task_status"], "completed")
        body = self.body(archived=True)
        status, _, result = self.request("POST", base + "/timeline/archive", body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["timeline"]["archived"])
        self.assertEqual(self.request("POST", base + "/timeline/archive", body)[2], result)
        detail = self.request("GET", base)[2]["job"]
        self.assertTrue(detail["timelines"][0]["archived"])
        self.assertEqual(detail["status"], "voiced")
        status, _, result = self.request("POST", base + "/timeline/archive", self.body(archived=False))
        self.assertEqual(status, 200, result)
        self.assertFalse(result["timeline"]["archived"])
        self.assertEqual(result["timeline"]["task_status"], "completed")
        self.assertEqual(result["job"]["edits"], [])
        self.assertEqual(self.runner.calls, [])

    def test_routes_reject_unauthorized_invalid_and_stale_requests_without_write(self):
        identifier = self.seed_job()
        base = f"/api/production/jobs/{identifier}/timeline"
        before = self.server.app.production.path.read_bytes()
        for endpoint, values in (("archive", {"archived": True}), ("task-status", {"task_status": "pending"})):
            body = self.body(**values)
            self.assertEqual(self.request("POST", base + "/" + endpoint, body, authenticated=False)[0], 403)
            self.assertEqual(self.request("POST", base + "/" + endpoint, {**body, "revision": "stale"})[0], 409)
            self.assertEqual(self.request("POST", base + "/" + endpoint, {**body, "timeline_uid": "missing"})[0], 404)
        self.assertEqual(self.request("POST", base + "/archive", self.body(archived="yes"))[0], 400)
        self.assertEqual(self.request("POST", base + "/task-status", self.body(task_status="bad"))[0], 400)
        self.assertEqual(self.server.app.production.path.read_bytes(), before)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
