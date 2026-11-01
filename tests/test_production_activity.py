"""Activity follows durable production outcomes without repeating external work."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from task_activity import ActivityStore
from elevenlabs_client import VoiceError
import test_production_recovery as recovery_fixture
import test_production_editing as editing_fixture


class ActivityAssertions:
    def attach_activity(self):
        self.bench.activity = ActivityStore(self.bench.production.root / "activity-test")

    def runs(self, kind):
        return [row for row in self.bench.activity.feed(limit=200)["items"] if row["kind"] == kind]

    def logs(self, run):
        return self.bench.activity.logs(run["run_id"])["logs"]


class VoiceActivityTests(ActivityAssertions, unittest.TestCase):
    job = recovery_fixture.ProductionRecoveryTests.job
    voice_body = staticmethod(recovery_fixture.ProductionRecoveryTests.voice_body)
    speech_calls = recovery_fixture.ProductionRecoveryTests.speech_calls
    operation = recovery_fixture.ProductionRecoveryTests.operation
    error = recovery_fixture.ProductionRecoveryTests.error
    mutate = recovery_fixture.ProductionRecoveryTests.mutate
    voiced_job = recovery_fixture.ProductionRecoveryTests.voiced_job
    configured_job = recovery_fixture.ProductionRecoveryTests.configured_job

    def setUp(self):
        recovery_fixture.ProductionRecoveryTests.setUp(self)
        self.attach_activity()

    def test_voice_replay_has_one_run_and_no_extra_notification_or_business_revision(self):
        job, body = self.job(), self.voice_body()
        saved = self.bench.generate_take(job["id"], body)
        first = self.runs("voice")[0]
        revision = self.bench.production.public()["revision"]
        self.assertEqual((first["status"], first["source_id"], first["result_count"]), ("completed", saved["take"]["id"], 1))
        self.bench.generate_take(job["id"], body)
        self.assertEqual(self.runs("voice"), [first])
        self.assertEqual(self.bench.production.public()["revision"], revision)
        self.assertEqual(self.speech_calls(), 1)
        self.assertEqual(self.bench.activity.feed()["counts"]["unread"], 1)

    def test_catalog_failure_notifies_pending_and_recovers_same_run_without_speech(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.production, "write", side_effect=OSError("disk failure with secret=do-not-log")):
            self.error(503, self.bench.generate_take, job["id"], body)
        pending = self.runs("voice")[0]
        self.assertEqual(pending["status"], "needs_attention")
        self.assertNotIn("do-not-log", json.dumps(self.logs(pending)))
        self.assertFalse(any(log["status"] == "completed" for log in self.logs(pending)))
        self.bench.recover_production_operation(job["id"], {"request_id": body["request_id"], "kind": "take"})
        recovered = self.runs("voice")[0]
        self.assertEqual((recovered["run_id"], recovered["status"]), (pending["run_id"], "completed"))
        self.assertNotEqual(recovered["event_id"], pending["event_id"])
        self.assertEqual(self.speech_calls(), 1)

    def test_unknown_voice_result_never_replays_or_copies_provider_error(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.voices, "speech", side_effect=TimeoutError("provider-secret=never-store")) as speech:
            self.error(503, self.bench.generate_take, job["id"], body)
            self.error(503, self.bench.generate_take, job["id"], body)
        run = self.runs("voice")[0]
        self.assertEqual(run["status"], "needs_attention")
        self.assertEqual(speech.call_count, 1)
        self.assertNotIn("never-store", json.dumps(self.logs(run)))

    def test_bad_alignment_is_attention_not_voice_success(self):
        job = self.job()
        with patch("workbench_production.take_quality", return_value=False):
            self.bench.generate_take(job["id"], self.voice_body())
        run = self.runs("voice")[0]
        self.assertEqual(run["status"], "needs_attention")
        self.assertIn("对齐待核对", run["message"])

    def test_unavailable_log_does_not_change_or_repeat_voice(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.activity, "record", side_effect=OSError("activity database unavailable")):
            first = self.bench.generate_take(job["id"], body)
            replay = self.bench.generate_take(job["id"], body)
        self.assertEqual(first, replay)
        self.assertEqual(self.speech_calls(), 1)

    def test_timeline_lost_response_recovers_same_run_and_existing_external_timeline(self):
        job = self.configured_job()
        self.bench.resolve.lost_response = True
        body = {"request_id": uuid4().hex, "creator": "Lab"}
        self.error(504, self.bench.build_timeline, job["id"], body)
        pending = self.runs("timeline")[0]
        self.assertEqual(pending["status"], "needs_attention")
        saved = self.bench.recover_production_operation(job["id"], {"request_id": body["request_id"], "kind": "timeline"})
        recovered = self.runs("timeline")[0]
        self.assertEqual((recovered["run_id"], recovered["status"], recovered["source_id"]),
                         (pending["run_id"], "completed", saved["timeline"]["unique_id"]))
        self.assertEqual(len(self.bench.resolve.timelines), 1)
        self.assertEqual(len(self.runs("timeline")), 1)


class EditingActivityTests(ActivityAssertions, unittest.TestCase):
    mutate = editing_fixture.EditingTests.mutate
    timeline_job = editing_fixture.EditingTests.timeline_job
    start = editing_fixture.EditingTests.start
    wait = editing_fixture.EditingTests.wait

    def setUp(self):
        editing_fixture.EditingTests.setUp(self)
        convert = patch("workbench_production.convert_wav", return_value=(False, None))
        convert.start()
        self.addCleanup(convert.stop)
        self.attach_activity()

    def test_edit_logs_saved_delivery_without_automatic_validation_then_manual_validation_has_own_request(self):
        job = self.timeline_job()
        edit = self.start(job)["edit"]
        self.wait(job["id"])
        run = self.runs("edit")[0]
        self.assertEqual((run["source_id"], run["status"]), (edit["id"], "completed"))
        self.assertIn("交人工审查", run["message"])
        self.assertNotIn("工程核验", [log["phase"] for log in self.logs(run)])
        self.assertEqual(editing_fixture.judging_inspects(self.bench.runner.calls), [])
        self.assertNotIn("开头要不要换切纸", json.dumps(self.logs(run), ensure_ascii=False))
        body = {"request_id": uuid4().hex}
        self.bench.recheck_edit(job["id"], edit["id"], body)
        self.bench.recheck_edit(job["id"], edit["id"], body)
        validation = self.runs("validation")
        self.assertEqual(len(validation), 1)
        self.assertEqual((validation[0]["source_id"], validation[0]["status"]), (edit["id"], "needs_attention"))

    def test_cancel_keeps_stopping_step_and_final_cancelled_history(self):
        self.editor.block = True
        job = self.timeline_job()
        edit = self.start(job)["edit"]
        self.assertTrue(self.editor.started.wait(5))
        self.bench.cancel_edit(job["id"], edit["id"])
        self.wait(job["id"])
        run = self.runs("edit")[0]
        self.assertEqual(run["status"], "cancelled")
        self.assertIn("stopping", [log["status"] for log in self.logs(run)])

    def test_resolve_wait_is_queued_until_the_edit_can_execute(self):
        self.editor.block = True
        job = self.timeline_job()
        waiting = threading.Event()
        phase = self.bench._edit_phase

        def observe(edit_id, name, message):
            phase(edit_id, name, message)
            if name == "waiting_preparing":
                waiting.set()

        self.bench.resolve.lock.acquire()
        try:
            with patch.object(self.bench, "_edit_phase", side_effect=observe):
                edit = self.start(job)["edit"]
                self.assertTrue(waiting.wait(5))
                run = self.runs("edit")[0]
                self.assertEqual((run["status"], run["finished_at"]), ("queued", None))
                self.assertEqual(self.bench.activity.feed()["counts"]["active"], 1)
                self.assertEqual(self.editor.calls, [])
                self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["status"], "running")
        finally:
            self.bench.resolve.lock.release()
        try:
            self.assertTrue(self.editor.started.wait(5))
            run = self.runs("edit")[0]
            self.assertEqual((run["status"], run["finished_at"]), ("running", None))
            self.assertIn("queued", [log["status"] for log in self.logs(run)])
        finally:
            self.bench.cancel_edit(job["id"], edit["id"])
            self.wait(job["id"])
        self.assertEqual(self.runs("edit")[0]["status"], "cancelled")

    def test_late_phase_updates_keep_a_cancelled_edit_stopping(self):
        self.editor.block = True
        job = self.timeline_job()
        edit = self.start(job)["edit"]
        self.assertTrue(self.editor.started.wait(5))
        with self.bench.lock:
            self.bench.cancel_edit(job["id"], edit["id"])
            for phase in ("waiting_preparing", "preparing", "planning"):
                self.bench._edit_phase(edit["id"], phase, "迟到的阶段更新")
            run = self.runs("edit")[0]
            self.assertEqual((run["status"], run["phase"]), ("stopping", "正在停止"))
        self.wait(job["id"])
        self.assertEqual(self.runs("edit")[0]["status"], "cancelled")

    def test_pending_edit_recovery_notifies_actual_saved_result_without_reediting(self):
        job = self.timeline_job()
        mutate = self.bench.production.mutate

        def fail_finish(kind, body, result=None):
            if kind == "edit_finish" and body["request_id"].startswith("finish-"):
                raise OSError("catalog disk failure")
            return mutate(kind, body, result)

        with patch.object(self.bench.production, "mutate", side_effect=fail_finish):
            edit = self.start(job)["edit"]
            self.wait(job["id"])
        pending = self.runs("edit")[0]
        self.assertEqual((pending["status"], pending["phase"]), ("needs_attention", "结果待恢复"))
        recovered = self.bench.recover_edit_results()
        self.assertIn(edit["id"], recovered["recovered"])
        saved = self.runs("edit")[0]
        self.assertEqual((saved["run_id"], saved["status"], saved["phase"]),
                         (pending["run_id"], "completed", "剪辑结果已保存"))
        self.assertNotEqual(saved["event_id"], pending["event_id"])
        self.assertEqual(len(self.editor.calls), 1)


if __name__ == "__main__":
    unittest.main()
