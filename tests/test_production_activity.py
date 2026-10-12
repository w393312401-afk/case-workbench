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
import test_production_carousel_images as image_fixture
import test_production_carousel_stills as stills_fixture
from workbench_carousel_images import CarouselImagesMixin


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
        self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))
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


class StillsActivityTests(ActivityAssertions, unittest.TestCase):
    carousel = stills_fixture.StillTests.carousel
    start = stills_fixture.StillTests.start
    wait = stills_fixture.StillTests.wait

    def setUp(self):
        stills_fixture.StillTests.setUp(self)
        self.attach_activity()

    def test_candidates_are_attention_with_count_after_catalog_save(self):
        search = self.start(per_slide=2)["search"]
        self.wait()
        run = self.runs("stills")[0]
        self.assertEqual((run["source_id"], run["status"], run["result_count"]), (search["id"], "needs_attention", 2))


class ImageActivityTests(ActivityAssertions, unittest.TestCase):
    carousel = image_fixture.CarouselImageTests.carousel
    body = image_fixture.CarouselImageTests.body
    current = image_fixture.CarouselImageTests.current
    pick_new_base = image_fixture.CarouselImageTests.pick_new_base
    start = image_fixture.CarouselImageTests.start
    wait = image_fixture.CarouselImageTests.wait
    stop = image_fixture.CarouselImageTests.stop
    generate = image_fixture.CarouselImageTests.generate
    approve = image_fixture.CarouselImageTests.approve
    approve_all = image_fixture.CarouselImageTests.approve_all
    complete_group = image_fixture.CarouselImageTests.complete_group
    recheck = image_fixture.CarouselImageTests.recheck

    def setUp(self):
        image_fixture.CarouselImageTests.setUp(self)
        self.attach_activity()

    def test_internal_reviews_log_under_parent_and_independent_review_has_one_notification(self):
        generation = self.start()
        self.wait()
        run = self.runs("images")[0]
        self.assertEqual((run["source_id"], run["status"], run["message"]), (generation["id"], "needs_attention", "首张已生成，待确认"))
        self.assertEqual(self.runs("image_review"), [])
        self.assertTrue(any("核对" in log["phase"] for log in self.logs(run)))
        self.recheck(1)
        self.assertEqual(len(self.runs("image_review")), 1)
        self.assertEqual(self.runs("image_review")[0]["status"], "needs_attention")

    def test_parallel_slides_keep_ordered_log_and_package_replay_is_idempotent(self):
        self.complete_group()
        self.approve_all()
        run = self.runs("images")[0]
        logs = self.logs(run)
        self.assertEqual([log["seq"] for log in logs], sorted({log["seq"] for log in logs}))
        for number in (2, 3):
            self.assertTrue(any(f"第 {number} 张" in log["phase"] for log in logs))
        body = self.body()
        saved = self.bench.create_carousel_delivery(self.item["id"], body)
        package = self.runs("package")[0]
        self.bench.create_carousel_delivery(self.item["id"], body)
        self.assertEqual(self.runs("package"), [package])
        self.assertEqual((package["status"], package["result_count"], package["source_id"]),
                         ("completed", 3, saved["delivery"]["id"]))

    def test_saved_generation_failure_is_not_completed_and_errors_are_sanitized(self):
        self.generator.file_mode = "error"
        self.generate()
        run = self.runs("images")[0]
        self.assertEqual(run["status"], "failed")
        self.assertNotIn("模拟生成失败", json.dumps(self.logs(run), ensure_ascii=False))

    def test_worker_start_failure_is_persisted_before_failure_notification(self):
        with patch("workbench_carousel_images.threading.Thread.start", side_effect=RuntimeError("cannot start worker")):
            with self.assertRaises(RuntimeError):
                self.start()
        run = self.runs("images")[0]
        self.assertEqual(run["status"], "failed")
        self.assertEqual(self.current()["generations"][-1]["status"], "failed")
        self.assertEqual(self.bench.activity.feed()["counts"]["active"], 0)


class ImageProgressActivityTests(unittest.TestCase):
    """Exercise progress observation without starting a generator or worker."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="image-progress-activity-")
        self.addCleanup(temporary.cleanup)
        self.bench = CarouselImagesMixin()
        self.bench.lock = threading.RLock()
        self.bench.activity = ActivityStore(Path(temporary.name))
        self.bench._image_tasks()

    def start(self, identifier="IMG-progress", *, kind="generation", phase="第 1 张 · 生成"):
        prefix, activity_kind = ("image_review:", "image_review") if kind == "review" else ("images:", "images")
        run_id = prefix + identifier
        self.bench.activity.record(run_id, kind=activity_kind, owner_id="CAR-progress", source_id=identifier,
                                   product="202H", title="图片进度测试", step="images", status="running",
                                   phase=phase, message="正在准备图片")
        self.bench._image_tasks()[identifier] = {"kind": kind, "carousel_id": "CAR-progress",
                                               "progress": "正在准备图片", "updated_at": "before"}
        self.bench._image_cancel[identifier] = threading.Event()
        return run_id

    def activity_run(self, run_id):
        return next(row for row in self.bench.activity.feed(limit=200)["items"] if row["run_id"] == run_id)

    def test_progress_updates_message_and_timestamp_without_replacing_phase_or_status(self):
        run_id = self.start()
        before = self.activity_run(run_id)
        message = "第 1 张：正在生成图片。"
        with patch("task_activity.stamp", return_value="2026-10-12T07:22:38.000+00:00"):
            self.bench._image_progress("IMG-progress", message)
        run = self.activity_run(run_id)
        self.assertEqual((run["message"], run["phase"], run["status"]), (message, before["phase"], "running"))
        self.assertEqual(run["started_at"], before["started_at"])
        self.assertEqual(run["updated_at"], "2026-10-12T07:22:38.000+00:00")
        self.assertIsNone(run["finished_at"])
        self.assertIsNone(run["event_id"])
        self.assertEqual(self.bench._image_tasks()["IMG-progress"]["progress"], message)

    def test_duplicate_progress_does_not_add_logs_cursor_or_completion_notifications(self):
        run_id = self.start()
        message = "第 1 张：正在整理本次图片文件。"
        self.bench._image_progress("IMG-progress", message)
        before = self.bench.activity.feed()
        logs = self.bench.activity.logs(run_id)["logs"]
        self.bench._image_progress("IMG-progress", message)
        after = self.bench.activity.feed(after=before["cursor"])
        self.assertEqual(after["cursor"], before["cursor"])
        self.assertEqual(self.bench.activity.logs(run_id)["logs"], logs)
        self.assertEqual(after["events"], [])
        self.assertEqual(after["counts"], {"active": 1, "unread": 0})

    def test_parent_generation_review_and_independent_review_use_their_own_runs(self):
        parent = self.start(phase="第 1 张 · 核对")
        independent = self.start("REV-progress", kind="review", phase="独立核对")
        parent_message = "第 1 张核对：Codex 正在处理图片要求。"
        independent_message = "第 2 张核对：正在读取并核对原图与成图。"
        self.bench._image_progress("IMG-progress", parent_message)
        self.bench._image_progress("REV-progress", independent_message)
        rows = {row["run_id"]: row for row in self.bench.activity.feed()["items"]}
        self.assertEqual(set(rows), {parent, independent})
        self.assertEqual((rows[parent]["message"], rows[parent]["phase"]), (parent_message, "第 1 张 · 核对"))
        self.assertEqual((rows[independent]["message"], rows[independent]["phase"]), (independent_message, "独立核对"))
        self.assertEqual(self.bench.activity.feed()["counts"], {"active": 2, "unread": 0})

    def test_cancelled_or_removed_live_run_ignores_late_progress(self):
        run_id = self.start()
        self.bench.activity.record(run_id, status="stopping", phase="正在停止", message="已收到停止请求")
        self.bench._image_tasks()["IMG-progress"]["progress"] = "正在停止后台 Codex…"
        self.bench._image_cancel["IMG-progress"].set()
        before = self.bench.activity.feed()
        live = dict(self.bench._image_tasks()["IMG-progress"])
        self.bench._image_progress("IMG-progress", "迟到的生成进展")
        self.assertEqual(self.bench._image_tasks()["IMG-progress"], live)
        self.assertEqual(self.bench.activity.feed()["cursor"], before["cursor"])
        self.assertEqual((self.activity_run(run_id)["status"], self.activity_run(run_id)["message"]), ("stopping", "已收到停止请求"))
        self.bench.activity.record(run_id, status="cancelled", phase="出图结果已保存", message="出图已停止")
        self.bench._image_tasks().pop("IMG-progress")
        self.bench._image_cancel.pop("IMG-progress")
        finished = self.bench.activity.feed()
        self.bench._image_progress("IMG-progress", "迟到的最终进展")
        self.assertEqual(self.bench.activity.feed()["cursor"], finished["cursor"])
        self.assertEqual((self.activity_run(run_id)["status"], self.activity_run(run_id)["message"]), ("cancelled", "出图已停止"))
        self.assertEqual(self.bench._image_tasks(), {})

    def test_unavailable_activity_does_not_block_live_progress(self):
        run_id = self.start()
        before = self.bench.activity.feed()["cursor"]
        message = "第 1 张：正在生成图片。"
        with patch.object(self.bench.activity, "record", side_effect=OSError("activity unavailable")):
            self.bench._image_progress("IMG-progress", message)
        live = self.bench._image_tasks()["IMG-progress"]
        self.assertEqual(live["progress"], message)
        self.assertNotEqual(live["updated_at"], "before")
        self.assertEqual(self.bench.activity.feed()["cursor"], before)
        self.assertEqual(self.activity_run(run_id)["message"], "正在准备图片")


if __name__ == "__main__":
    unittest.main()
