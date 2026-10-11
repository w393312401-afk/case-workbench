"""Closing the editing loop: the user's completion confirmation and snapshots for edits recorded before snapshots existed."""
import copy
import json
from uuid import uuid4
import unittest

import test_production_editing as editing
import test_server as fixture
from elevenlabs_client import VoiceError
from production_validation import MANUAL_SCOPE
from resolve_bridge import ResolveBridge
from workbench_production import meaning_markers

SHOTS = ((0, 200), (200, 400))


class CompletionTests(unittest.TestCase):
    setUp = editing.EditingTests.setUp
    mutate = editing.EditingTests.mutate
    timeline_job = editing.EditingTests.timeline_job
    start = editing.EditingTests.start
    wait = editing.EditingTests.wait

    def finished(self):
        job = self.timeline_job()
        self.bench.runner.inspect = editing.inspect_reply(shots=SHOTS)
        self.start(job)
        return job, self.wait(job["id"])

    def approve(self, job, edit, **values):
        body = {"request_id": uuid4().hex, "approved": True, "reviewed": list(MANUAL_SCOPE), "note": "已导出", **values}
        return self.bench.approve_edit(job["id"], edit["id"], body)

    def recheck(self, job, edit):
        return self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})

    def test_confirmation_reads_back_saves_and_completes_the_job(self):
        job, edit = self.finished()
        self.assertEqual(edit["status"], "awaiting_review")
        calls = len(self.bench.runner.calls)
        result = self.approve(job, edit)
        self.assertTrue(result["approved"])
        self.assertEqual(result["job"]["status"], "approved")
        self.assertEqual(result["edit"]["status"], "awaiting_review")
        self.assertEqual((result["edit"]["approval"]["note"], result["edit"]["approval"]["reviewed"]), ("已导出", list(MANUAL_SCOPE)))
        self.assertEqual(len(self.bench.runner.calls), calls + 1)
        self.assertEqual((self.bench.runner.calls[-1]["command"], self.bench.runner.calls[-1]["save"]), ("inspect", True))
        self.assertEqual(len(self.editor.calls), 1)

    def test_confirmation_requires_every_manual_check_before_touching_resolve(self):
        job, edit = self.finished()
        calls = len(self.bench.runner.calls)
        for reviewed in ([], list(MANUAL_SCOPE[:2]), [*MANUAL_SCOPE, MANUAL_SCOPE[0]], "全部"):
            with self.assertRaises(VoiceError) as caught:
                self.approve(job, edit, reviewed=reviewed)
            self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(VoiceError):
            self.approve(job, edit, approved="yes")
        self.assertEqual(len(self.bench.runner.calls), calls)
        self.assertEqual(self.bench.production.job(job["id"])["status"], "awaiting_review")

    def test_failed_readback_leaves_the_record_untouched(self):
        job, edit = self.finished()
        self.bench.runner.final_inspect = RuntimeError("达芬奇没有响应")
        with self.assertRaises(VoiceError) as caught:
            self.approve(job, edit)
        self.assertEqual(caught.exception.status, 502)
        stored = self.bench.production.job(job["id"])
        self.assertEqual((stored["status"], stored["edits"][-1].get("approval")), ("awaiting_review", None))
        self.assertEqual(stored["edits"][-1]["validation"], edit["validation"])

    def test_changed_timeline_is_recorded_and_not_confirmed(self):
        job, edit = self.finished()
        self.bench.runner.final_inspect = editing.inspect_reply(shots=((0, 200),))
        result = self.approve(job, edit)
        self.assertFalse(result["approved"])
        self.assertEqual(result["edit"]["status"], "needs_repair")
        self.assertEqual(result["job"]["status"], "needs_repair")
        self.assertIn("unmarked_picture_gap", [item["code"] for item in result["edit"]["validation"]["issues"]])

    def test_recheck_keeps_confirmation_only_while_the_cut_is_unchanged(self):
        job, edit = self.finished()
        self.approve(job, edit)
        same = copy.deepcopy(self.bench.runner.inspect)
        for track in same["tracks"]["video"] + same["tracks"]["audio"][:1]:  # Shot ids may differ between readbacks.
            for item in track["items"]:
                item["uid"] += "-reloaded"
                item["linked_ids"] = [value + "-reloaded" for value in item.get("linked_ids") or []]
        self.bench.runner.final_inspect = same
        kept = self.recheck(job, edit)
        self.assertEqual(kept["job"]["status"], "approved")
        self.assertTrue(kept["edit"]["approval"])
        self.bench.runner.final_inspect = editing.inspect_reply(shots=((0, 150), (150, 400)))
        changed = self.recheck(job, edit)
        self.assertEqual((changed["edit"]["status"], changed["edit"]["approval"]), ("awaiting_review", None))
        self.assertEqual(changed["job"]["status"], "awaiting_review")
        self.assertIn("edit_approval_cleared", [entry["event"] for entry in changed["job"]["history"]])

    def test_revoke_needs_no_resolve_and_only_works_on_a_confirmed_edit(self):
        job, edit = self.finished()
        with self.assertRaises(VoiceError) as caught:
            self.bench.approve_edit(job["id"], edit["id"], {"request_id": uuid4().hex, "approved": False})
        self.assertEqual(caught.exception.status, 409)
        self.approve(job, edit)
        calls = len(self.bench.runner.calls)
        result = self.bench.approve_edit(job["id"], edit["id"], {"request_id": uuid4().hex, "approved": False})
        self.assertEqual((result["approved"], result["job"]["status"]), (False, "awaiting_review"))
        self.assertEqual(len(self.bench.runner.calls), calls)

    def test_superseded_edit_cannot_be_confirmed(self):
        job, first = self.finished()
        self.start(self.bench.production.job(job["id"]))
        self.wait(job["id"])
        calls = len(self.bench.runner.calls)
        with self.assertRaises(VoiceError) as caught:
            self.approve(job, first)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.bench.runner.calls), calls)

    def legacy(self):
        """An edit made before pre-edit snapshots were kept, on a timeline whose markers are as built."""
        job, edit = self.finished()
        stored = self.bench.production.job(job["id"])
        timeline, take = stored["timelines"][-1], stored["takes"][-1]
        markers = [{"frame": marker["frame"], "duration": marker["duration"], "color": "Blue", "name": marker["name"],
                    "note": marker["note"], "custom": marker["custom"]} for marker in meaning_markers(stored, take, timeline["fps"])]
        self.bench.runner.inspect["markers"] = markers
        data = self.bench.production.read()
        old = next(item for item in data["jobs"] if item["id"] == job["id"])["edits"][-1]
        old.pop("baseline")
        old.pop("baseline_at")
        self.bench.production.write(data)
        result = self.recheck(job, edit)["edit"]
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("baseline_missing", [item["code"] for item in result["validation"]["issues"]])
        return job, edit

    def adopt(self, job, edit):
        return self.bench.adopt_edit_baseline(job["id"], edit["id"], {"request_id": uuid4().hex})

    def test_adopted_snapshot_lets_an_old_edit_pass_and_then_be_confirmed(self):
        job, edit = self.legacy()
        result = self.adopt(job, edit)
        stored = result["edit"]
        self.assertEqual((stored["status"], stored["baseline_origin"]), ("awaiting_review", "adopted"))
        self.assertTrue(stored["validation"]["checks"]["voice_preserved"])
        self.assertTrue(stored["validation"]["checks"]["groups_preserved"])
        self.assertEqual([track["items"] for track in stored["baseline"]["tracks"]["video"]], [[]])
        self.assertEqual(stored["baseline"]["tracks"]["audio"][0]["items"], [])
        self.assertEqual(len(stored["baseline"]["tracks"]["audio"][1]["items"]), 1)
        self.assertEqual(len(self.editor.calls), 1)
        self.assertTrue(self.approve(job, edit)["approved"])
        with self.assertRaises(VoiceError) as caught:
            self.adopt(job, edit)
        self.assertEqual(caught.exception.status, 409)

    def test_snapshot_is_refused_when_voice_or_markers_differ_from_the_build(self):
        job, edit = self.legacy()
        moved = copy.deepcopy(self.bench.runner.inspect)
        moved["markers"][1]["frame"] += 3
        voice = copy.deepcopy(self.bench.runner.inspect)
        voice["tracks"]["audio"][1]["items"][0]["end"] = 380
        for reply, text in ((moved, "意群标记 #2"), (voice, "配音片段")):
            self.bench.runner.final_inspect = reply
            with self.assertRaises(VoiceError) as caught:
                self.adopt(job, edit)
            self.assertEqual(caught.exception.status, 409)
            self.assertIn(text, str(caught.exception))
        stored = self.bench.production.job(job["id"])["edits"][-1]
        self.assertNotIn("baseline", stored)
        self.assertEqual(stored["status"], "needs_repair")

    def test_adopted_snapshot_still_checks_every_shot_against_other_timelines(self):
        job, edit = self.legacy()
        reply = copy.deepcopy(self.bench.runner.inspect)
        reply["other_shots"] = [{"timeline_uid": "uid-other", "timeline_name": "Zack261009-202-1", "media_id": "source-1",
                                 "media_path": "C:/media/source.mp4", "source_start": 100, "source_end": 150}]
        self.bench.runner.final_inspect = reply
        result = self.adopt(job, edit)["edit"]
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("source_range_used_elsewhere", [item["code"] for item in result["validation"]["issues"]])

    def historical_store_fixture(self):
        """Only tests provenance storage; the separate helper validates the historical file contents."""
        job, edit = self.legacy()
        captured = "2026-10-10T13:49:53+00:00"
        data = self.bench.production.read()
        data["jobs"][0]["edits"][-1]["resumption"] = {"started_at": captured}
        self.bench.production.write(data)
        evidence = {"schema_version": 1, "kind": "historical_resume", "captured_at": captured,
                    "restored_at": "2026-10-11T05:00:00+00:00", "scope": "Codex接续剪辑前",
                    "unrecorded_fields": ["voice.clip_enabled"], "configured_fields": ["width", "height"],
                    "sources": [{"path": str((self.bench.production.root / f"{role}.json").resolve()),
                                 "sha256": "a" * 64, "role": role}
                                for role in ("resume_snapshot", "native_timeline_backup", "handoff", "pre_edit_catalog")]}
        extra = {"baseline": {"fixture": "synthetic historical resume state", "tracks": {"audio": [{"index": 2, "items": [{"enabled": None}]}]}}, "baseline_at": captured,
                 "baseline_origin": "historical_resume", "baseline_evidence": evidence,
                 "status": "awaiting_review", "validation": copy.deepcopy(edit["validation"])}
        extra["validation"]["status"] = "awaiting_review"
        body = {"job_id": job["id"], "edit_id": edit["id"], "source": "historical_resume", "request_id": uuid4().hex}
        return body, extra

    def test_store_preserves_historical_time_sources_and_prior_verification_history(self):
        body, extra = self.historical_store_fixture()
        result = self.bench.production.mutate("edit_baseline", body, extra)
        stored = result["edit"]
        self.assertEqual((stored["baseline_origin"], stored["baseline_at"]), ("historical_resume", extra["baseline_at"]))
        self.assertEqual(stored["baseline_evidence"], extra["baseline_evidence"])
        self.assertEqual(stored["status"], "awaiting_review")
        self.assertFalse(stored.get("approval"))
        self.assertEqual(stored["verification_history"][-1]["status"], "needs_repair")
        self.assertIn("恢复", result["job"]["history"][-1]["note"])
        self.assertNotIn("补拍", result["job"]["history"][-1]["note"])

    def test_store_rejects_false_historical_origin_missing_source_or_changed_capture_time(self):
        body, original = self.historical_store_fixture()
        before = self.bench.production.path.read_bytes()
        cases = []
        invalid = copy.deepcopy(original)
        invalid["baseline_origin"] = "adopted"
        cases.append(invalid)
        invalid = copy.deepcopy(original)
        invalid["baseline_evidence"]["sources"].pop()
        cases.append(invalid)
        invalid = copy.deepcopy(original)
        invalid["baseline_at"] = invalid["baseline_evidence"]["restored_at"]
        cases.append(invalid)
        invalid = copy.deepcopy(original)
        invalid["baseline_evidence"]["sources"][0]["sha256"] = "invalid"
        cases.append(invalid)
        for extra in cases:
            with self.subTest(extra=extra):
                with self.assertRaises(VoiceError) as caught:
                    self.bench.production.mutate("edit_baseline", {**body, "request_id": uuid4().hex}, extra)
                self.assertEqual(caught.exception.status, 500)
                self.assertEqual(self.bench.production.path.read_bytes(), before)

    def test_store_matches_missing_voice_enabled_audit_to_historical_sole_voice_item(self):
        body, original = self.historical_store_fixture()
        before = self.bench.production.path.read_bytes()
        for enabled, fields in ((None, []), (True, ["voice.clip_enabled"]), (None, ["other"]), ("yes", [])):
            with self.subTest(enabled=enabled, fields=fields):
                extra = copy.deepcopy(original)
                extra["baseline"]["tracks"]["audio"][0]["items"][0]["enabled"] = enabled
                extra["baseline_evidence"]["unrecorded_fields"] = fields
                with self.assertRaises(VoiceError) as caught:
                    self.bench.production.mutate("edit_baseline", {**body, "request_id": uuid4().hex}, extra)
                self.assertEqual(caught.exception.status, 500)
                self.assertEqual(self.bench.production.path.read_bytes(), before)
        extra = copy.deepcopy(original)
        extra["baseline"]["tracks"]["audio"][0]["items"][0]["enabled"] = True
        extra["baseline_evidence"]["unrecorded_fields"] = []
        stored = self.bench.production.mutate("edit_baseline", body, extra)["edit"]
        self.assertEqual(stored["baseline_evidence"]["unrecorded_fields"], [])
        self.assertIs(stored["baseline"]["tracks"]["audio"][0]["items"][0]["enabled"], True)


class CompletionHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_routes_confirm_replay_and_revoke(self):
        from test_production_verification_http import VerificationHTTPTests
        job, edit = VerificationHTTPTests.seed_job(self)
        path = f"/api/production/jobs/{job}/edits/{edit}/approval"
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex, "approved": True, "reviewed": []})[0], 400)
        self.assertEqual(self.request("POST", path, {"request_id": uuid4().hex, "approved": True, "reviewed": list(MANUAL_SCOPE)},
                                      authenticated=False)[0], 403)
        self.assertEqual(self.runner.calls, [])
        body = {"request_id": uuid4().hex, "approved": True, "reviewed": list(MANUAL_SCOPE), "note": ""}
        status, _, first = self.request("POST", path, body)
        self.assertEqual(status, 200, first)
        self.assertEqual((first["approved"], first["job"]["status"]), (True, "approved"))
        self.assertEqual(self.request("POST", path, body)[2], first)
        self.assertEqual(len(self.runner.calls), 1)
        ready = self.request("GET", "/api/production/deliveries")[2]["items"]
        self.assertEqual(ready[0]["approved_at"], first["edit"]["approval"]["at"])
        status, _, revoked = self.request("POST", path, {"request_id": uuid4().hex, "approved": False})
        self.assertEqual((status, revoked["job"]["status"]), (200, "awaiting_review"))
        status, _, refused = self.request("POST", f"/api/production/jobs/{job}/edits/{edit}/baseline", {"request_id": uuid4().hex})
        self.assertEqual(status, 409, refused)


if __name__ == "__main__":
    unittest.main()
