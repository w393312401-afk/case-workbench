"""Manual step-four success stays bound to the current voiceover and built timeline."""
from __future__ import annotations

import copy
import tempfile
import unittest
from uuid import uuid4

import test_server as fixture
from direction_store import now
from elevenlabs_client import VoiceError
from production_store import ProductionStore, job_status, manual_completion_current
import test_production_verification_http as verification_fixture

JOB_ID = "PRD-0123456789ab"
TAKE_ID = "TAK-0123456789ab"
OTHER_TAKE_ID = "TAK-111111111111"


def job_fixture():
    take = {"id": TAKE_ID, "groups": [{"number": 1, "start": 0., "end": 1., "coverage": 1.}], "duration": 1.}
    return {"id": JOB_ID, "product": "202H", "title": "手工剪辑", "archived": False, "status": "timeline",
            "lines": [{"number": 1, "spoken": "Current words."}], "selected_take": TAKE_ID,
            "takes": [take, {**copy.deepcopy(take), "id": OTHER_TAKE_ID}],
            "timelines": [{"unique_id": "uid-1", "name": "Zack261013-202-1", "take_id": TAKE_ID,
                           "saved": True, "built_at": now()}], "edits": [], "history": [], "updated_at": now()}


class ManualCompletionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="manual-completion-")
        self.addCleanup(temp.cleanup)
        self.store = ProductionStore(temp.name)
        data = self.store.empty()
        data["jobs"].append(job_fixture())
        self.store.write(data)

    def complete(self, **values):
        body = {"request_id": uuid4().hex, "job_id": JOB_ID, "completed": True, "timeline_uid": "uid-1", **values}
        if body["completed"] is False and "completion_at" not in body:
            body["completion_at"] = (self.store.job(JOB_ID).get("manual_completion") or {}).get("at", "previous-completion")
        return self.store.mutate("job_completion", body)

    def change(self, change):
        data = self.store.read()
        change(data["jobs"][0])
        self.store.write(data)

    def mutate(self, kind, extra=None, **values):
        return self.store.mutate(kind, {"request_id": uuid4().hex, "revision": self.store.public()["revision"],
                                        "job_id": JOB_ID, **values}, extra)

    def test_mark_replay_reload_and_revoke_without_fabricating_ai_edits(self):
        body = {"request_id": uuid4().hex, "job_id": JOB_ID, "completed": True, "timeline_uid": "uid-1", "note": "  已手工剪完  "}
        first = self.store.mutate("job_completion", body)
        self.assertEqual((first["completed"], first["job"]["status"], first["job"]["edits"]), (True, "approved", []))
        receipt = first["manual_completion"]
        self.assertEqual((receipt["note"], receipt["take_id"], receipt["timeline_name"], receipt["basis"]),
                         ("已手工剪完", TAKE_ID, "Zack261013-202-1", "human_review"))
        saved = self.store.path.read_bytes()
        self.assertEqual(self.store.mutate("job_completion", body), first)
        self.assertEqual(self.store.path.read_bytes(), saved)
        reloaded = ProductionStore(self.store.root.parent)
        self.assertEqual(reloaded.job(JOB_ID)["manual_completion"], receipt)
        repeated = self.complete(note="新的备注")
        self.assertEqual(repeated, first)
        revoked = self.complete(completed=False)
        self.assertEqual((revoked["completed"], revoked["job"]["status"], revoked["manual_completion"]), (False, "timeline", None))
        self.assertEqual([entry["event"] for entry in revoked["job"]["history"]],
                         ["job_manual_completed", "job_manual_completion_revoked"])
        with self.assertRaises(VoiceError) as caught:
            self.complete(completed=False)
        self.assertEqual(caught.exception.status, 409)

    def test_request_id_cannot_confirm_a_different_result(self):
        request_id = uuid4().hex
        self.complete(request_id=request_id)
        before = self.store.path.read_bytes()
        with self.assertRaises(VoiceError) as caught:
            self.complete(request_id=request_id, completed=False)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_selected_take_change_clears_receipt_permanently(self):
        self.complete()
        same = self.mutate("select_take", take_id=TAKE_ID)["job"]
        self.assertTrue(manual_completion_current(same))
        changed = self.mutate("select_take", take_id=OTHER_TAKE_ID)["job"]
        self.assertNotIn("manual_completion", changed)
        self.assertEqual(changed["status"], "voiced")
        self.assertEqual(changed["history"][-2]["event"], "job_manual_completion_cleared")
        restored = self.mutate("select_take", take_id=TAKE_ID)["job"]
        self.assertEqual(restored["status"], "timeline")

    def test_new_current_timeline_clears_receipt_but_other_take_does_not(self):
        self.complete()
        other = {"unique_id": "uid-other", "name": "Other", "take_id": OTHER_TAKE_ID, "built_at": now()}
        result = self.mutate("timeline", other)["job"]
        self.assertTrue(manual_completion_current(result))
        current = {**other, "unique_id": "uid-2", "name": "Current", "take_id": TAKE_ID}
        result = self.mutate("timeline", current)["job"]
        self.assertNotIn("manual_completion", result)
        self.assertEqual(result["status"], "timeline")
        with self.assertRaises(VoiceError) as caught:
            self.complete()
        self.assertEqual(caught.exception.status, 409)

    def test_new_edit_clears_manual_receipt_and_counts_only_real_edit(self):
        self.complete()
        edit = {"id": "EDT-0123456789ab", "timeline_uid": "uid-1", "timeline_name": "Zack261013-202-1",
                "take_id": TAKE_ID, "started_at": now(), "status": "running", "materials": []}
        result = self.mutate("edit_start", edit)["job"]
        self.assertNotIn("manual_completion", result)
        self.assertEqual((result["status"], len(result["edits"])), ("editing", 1))
        self.assertEqual(result["history"][-2]["event"], "job_manual_completion_cleared")

    def test_manually_resolved_failed_edit_can_be_marked_and_revoke_restores_state(self):
        self.change(lambda job: job["edits"].append({"id": "EDT-0123456789ab", "timeline_uid": "uid-1", "status": "needs_repair"}))
        first = self.complete()
        self.assertEqual(first["job"]["status"], "approved")
        self.assertEqual(first["job"]["edits"][0]["status"], "needs_repair")
        revoked = self.complete(completed=False)
        self.assertEqual(revoked["job"]["status"], "needs_repair")

    def test_invalid_parameters_do_not_write(self):
        before = self.store.path.read_bytes()
        cases = [(400, {"completed": "yes"}), (400, {"completed": 1}), (400, {"note": None}),
                 (400, {"note": "x" * 2001}), (400, {"timeline_uid": None}), (409, {"timeline_uid": "old-uid"}),
                 (400, {"request_id": "bad"})]
        for status, values in cases:
            with self.subTest(values=values), self.assertRaises(VoiceError) as caught:
                self.complete(**values)
            self.assertEqual(caught.exception.status, status)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_archived_running_unbuilt_or_unsaved_orders_cannot_be_marked(self):
        for change in (lambda job: job.update(archived=True), lambda job: job.update(timelines=[]),
                       lambda job: job["timelines"][0].update(saved=False),
                       lambda job: job["edits"].append({"id": "EDT-0123456789ab", "status": "running"})):
            data = self.store.empty()
            data["jobs"].append(job_fixture())
            change(data["jobs"][0])
            self.store.write(data)
            before = self.store.path.read_bytes()
            with self.assertRaises(VoiceError) as caught:
                self.complete()
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.store.path.read_bytes(), before)

    def test_stale_receipt_is_not_an_approved_read_model(self):
        original = self.complete()["job"]
        for change in (lambda job: job.update(selected_take=OTHER_TAKE_ID),
                       lambda job: job["timelines"].append({"unique_id": "uid-new", "take_id": TAKE_ID}),
                       lambda job: job["edits"].append({"timeline_uid": "uid-1", "status": "running"})):
            job = copy.deepcopy(original)
            change(job)
            self.assertFalse(manual_completion_current(job))
            self.assertNotEqual(job_status(job), "approved")


class ManualCompletionHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def seed_job(self):
        identifier, _ = verification_fixture.VerificationHTTPTests.seed_job(self)
        data = self.server.app.production.read()
        data["jobs"][-1]["edits"] = []
        self.server.app.production.write(data)
        return identifier

    def test_route_marks_replays_lists_delivery_and_revokes_without_resolve(self):
        identifier = self.seed_job()
        path = f"/api/production/jobs/{identifier}/completion"
        body = {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1", "note": "人工完成"}
        status, _, first = self.request("POST", path, body)
        self.assertEqual(status, 200, first)
        self.assertEqual((first["completed"], first["job"]["status"], first["job"]["edits"]), (True, "approved", []))
        self.assertEqual(self.request("POST", path, body)[2], first)
        row = self.request("GET", "/api/production/deliveries")[2]["items"][0]
        self.assertEqual((row["id"], row["status"], row["edit_id"], row["timeline_uid"]), (identifier, "approved", None, "uid-1"))
        self.assertEqual(row["approved_at"], first["manual_completion"]["at"])
        detail = self.request("GET", f"/api/production/jobs/{identifier}")[2]
        self.assertEqual(detail["job"]["manual_completion"], first["manual_completion"])
        status, _, revoked = self.request("POST", path, {"request_id": uuid4().hex, "completed": False,
                                                        "timeline_uid": "uid-1", "completion_at": first["manual_completion"]["at"]})
        self.assertEqual((status, revoked["job"]["status"]), (200, "timeline"))
        self.assertEqual(self.request("GET", "/api/production/deliveries")[2]["items"], [])
        self.assertEqual(self.runner.calls, [])

    def test_route_rejects_unauthorized_invalid_stale_or_missing_job(self):
        identifier = self.seed_job()
        path = f"/api/production/jobs/{identifier}/completion"
        body = {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1"}
        before = self.server.app.production.path.read_bytes()
        self.assertEqual(self.request("POST", path, body, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", path, {**body, "completed": "yes"})[0], 400)
        self.assertEqual(self.request("POST", path, {**body, "timeline_uid": "old-uid"})[0], 409)
        self.assertEqual(self.request("POST", "/api/production/jobs/PRD-000000000000/completion", body)[0], 404)
        self.assertEqual(self.server.app.production.path.read_bytes(), before)
        self.assertEqual(self.runner.calls, [])

    def test_stale_revoke_does_not_remove_current_receipt(self):
        identifier = self.seed_job()
        path = f"/api/production/jobs/{identifier}/completion"
        body = {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1"}
        first = self.request("POST", path, body)[2]
        status, _, data = self.request("POST", path, {"request_id": uuid4().hex, "completed": False,
                                                     "timeline_uid": "old-uid", "completion_at": first["manual_completion"]["at"]})
        self.assertEqual(status, 409, data)
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex, "completed": False})[0], 400)
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex, "completed": False,
                         "timeline_uid": "uid-1", "completion_at": "older-completion"})[0], 409)
        self.assertEqual(self.server.app.production.job(identifier)["status"], "approved")

    def test_unsettled_running_record_exposes_the_same_completion_block_as_post(self):
        identifier = self.seed_job()
        app = self.server.app
        data = app.production.read()
        data["jobs"][-1]["edits"] = [{"id": "EDT-111111111111", "status": "running", "timeline_uid": "uid-1"}]
        app.production.write(data)
        detail = self.request("GET", f"/api/production/jobs/{identifier}")[2]
        self.assertEqual(detail["job"]["edits"][0]["status"], "interrupted")
        self.assertTrue(detail["job"]["manual_completion_blocked"])
        status, _, result = self.request("POST", f"/api/production/jobs/{identifier}/completion",
                                         {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1"})
        self.assertEqual(status, 409, result)
        self.assertEqual(self.runner.calls, [])

    def test_same_timeline_new_receipt_survives_old_tab_revoke(self):
        identifier = self.seed_job()
        path = f"/api/production/jobs/{identifier}/completion"
        first = self.request("POST", path, {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1"})[2]
        revoke = {"request_id": uuid4().hex, "completed": False, "timeline_uid": "uid-1", "completion_at": first["manual_completion"]["at"]}
        self.assertEqual(self.request("POST", path, revoke)[0], 200)
        second = self.request("POST", path, {"request_id": uuid4().hex, "completed": True, "timeline_uid": "uid-1"})[2]
        self.assertNotEqual(second["manual_completion"]["at"], first["manual_completion"]["at"])
        status, _, data = self.request("POST", path, {**revoke, "request_id": uuid4().hex})
        self.assertEqual(status, 409, data)
        self.assertEqual(self.server.app.production.job(identifier)["manual_completion"], second["manual_completion"])


if __name__ == "__main__":
    unittest.main()
