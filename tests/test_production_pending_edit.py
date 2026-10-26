"""A queued build can hand off to editing once, using only its own committed identity."""
import copy
import json
import subprocess
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_editing as fixture
from elevenlabs_client import VoiceError


class PendingEditTests(unittest.TestCase):
    mutate = fixture.EditingTests.mutate
    timeline_job = fixture.EditingTests.timeline_job

    def setUp(self):
        fixture.EditingTests.setUp(self)
        self.job = self.timeline_job()
        self.addCleanup(self.finish_workers)
        self.built = 1
        original_runner = self.bench.resolve.runner

        def runner(args, input, **options):
            completed = original_runner(args, input=input, **options)
            request = json.loads(input.decode("utf-8"))
            if request["command"] == "build":
                self.built += 1
                value = json.loads(completed.stdout.decode("utf-8"))
                value["unique_id"] = "uid-" + str(self.built)
                return subprocess.CompletedProcess(args, 0, json.dumps(value).encode("utf-8"), b"")
            return completed

        self.bench.resolve.runner = runner

    def finish_workers(self):
        self.bench.stop_edits()
        for identifier, live in list(self.bench._timeline_live().items()):
            if live["stage"] == "waiting":
                self.bench.cancel_timeline(identifier, {"request_id": live["request_id"]})
        self.until(lambda: not self.bench.edit_running() and not self.bench._timeline_live())

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.02)
        self.fail("worker did not finish")

    def queue_build(self, **values):
        body = {"request_id": uuid4().hex, "creator": "Lab", **values}
        with patch("workbench_production.QUEUE_GRACE", 0):
            self.assertTrue(self.bench.build_timeline(self.job["id"], body)["queued"])
        return body

    def start_pending(self, build, **values):
        body = {"request_id": uuid4().hex, "timeline_request_id": build["request_id"], "mode": "sync",
                "materials": ["原素材时间线"], "instruction": "原来的剪辑要求", **values}
        return body, self.bench.start_edit(self.job["id"], body)

    def edit(self, edit_id):
        return next(row for row in self.bench.production.job(self.job["id"])["edits"] if row["id"] == edit_id)

    def operation(self, build):
        return self.bench._production_operations().read("timeline", {**build, "job_id": self.job["id"]})

    def test_waiting_build_binds_original_take_settings_and_frozen_model_then_edits_once(self):
        self.bench.resolve.lock.acquire()
        try:
            build = self.queue_build(options={"fps": "25", "width": 1280, "height": 720})
            original = self.operation(build)
            body, result = self.start_pending(build, model="claude-sonnet", effort="high")
            record = result["edit"]
            self.assertEqual(record["timeline_uid"], "pending:" + build["request_id"])
            self.assertEqual(record["timeline_operation_key"], original["operation_key"])
            self.assertEqual(record["take_id"], original["snapshot"]["take_id"])
            self.assertTrue(record["waiting_timeline"])
            self.assertEqual(result["job"]["status"], "editing")
            self.assertEqual(self.bench.edit_live(record["id"])["phase"], "waiting_timeline")
            self.assertEqual(self.editor.calls, [])
            # Changes made while waiting cannot redirect this edit to another voice or project.
            with self.bench.lock:
                data = self.bench.production.read()
                job = next(row for row in data["jobs"] if row["id"] == self.job["id"])
                later_take = copy.deepcopy(job["takes"][0])
                later_take["id"] = "TAK-" + uuid4().hex[:12]
                later_take["model_id"] = "different-voice-model"
                job["takes"].append(later_take)
                job["selected_take"] = later_take["id"]
                data["settings"]["products"][job["product"]]["project"] = "另一个项目"
                self.bench.production._write(data)
            self.bench.ai.resolve_default_settings = lambda *args, **kwargs: {"model": "claude-opus", "effort": None}
        finally:
            self.bench.resolve.lock.release()
        self.until(lambda: not self.bench.edit_running())
        record = self.edit(record["id"])
        self.assertEqual(record["status"], "completed")
        self.assertFalse(record["waiting_timeline"])
        self.assertEqual(record["timeline_uid"], self.operation(build)["result"]["unique_id"])
        self.assertEqual(record["take_id"], original["snapshot"]["take_id"])
        self.assertEqual(len(self.editor.calls), 1)
        call = self.editor.calls[0]
        self.assertEqual((call["model"], call["reasoning_effort"]), ("claude-sonnet", "high"))
        self.assertIn("25 fps", call["prompt"])
        self.assertIn("1280×720", call["prompt"])
        self.assertIn("项目：202H", call["prompt"])
        self.assertNotIn("different-voice-model", call["prompt"])
        self.assertIn("原来的剪辑要求", call["prompt"])
        self.assertEqual(self.bench.start_edit(self.job["id"], body)["edit"]["id"], record["id"])

    def test_pending_request_replays_rejects_duplicate_target_and_cancels_without_external_calls(self):
        self.bench.resolve.lock.acquire()
        try:
            build = self.queue_build()
            with patch.object(self.bench, "_forget", wraps=self.bench._forget) as forget:
                body, first = self.start_pending(build)
                edit_id = first["edit"]["id"]
                self.assertEqual(self.bench.start_edit(self.job["id"], body)["edit"]["id"], edit_id)
                with self.assertRaisesRegex(VoiceError, "目标时间线"):
                    self.start_pending(build)
                self.bench.cancel_edit(self.job["id"], edit_id)
                self.until(lambda: not self.bench.edit_running())
                self.assertEqual(self.edit(edit_id)["status"], "cancelled")
                self.assertEqual(forget.call_count, 1)
            self.assertEqual(self.editor.calls, [])
            self.assertEqual(sum(call["command"] == "build" for call in self.bench.runner.calls), 1)
            self.bench.cancel_timeline(self.job["id"], {"request_id": build["request_id"]})
            self.until(lambda: not self.bench._timeline_live())
        finally:
            self.bench.resolve.lock.release()

    def test_build_cancel_or_uncertain_result_never_starts_editor(self):
        for outcome in ("cancelled", "uncertain", "failed"):
            with self.subTest(outcome=outcome):
                self.bench.resolve.lock.acquire()
                try:
                    build = self.queue_build()
                    _, result = self.start_pending(build)
                    if outcome == "cancelled":
                        self.bench.cancel_timeline(self.job["id"], {"request_id": build["request_id"]})
                    else:
                        self.bench._production_operations().update(self.operation(build),
                            "uncertain" if outcome == "uncertain" else "safe_failed", error="test build failure")
                    self.until(lambda: not self.bench.edit_running())
                    record = self.edit(result["edit"]["id"])
                    self.assertEqual(record["status"], "cancelled" if outcome == "cancelled" else "failed")
                    self.assertIn("AI 剪辑没有开始", record["error"])
                    self.assertEqual(self.editor.calls, [])
                    if outcome != "cancelled":
                        self.bench.cancel_timeline(self.job["id"], {"request_id": build["request_id"]})
                    self.until(lambda: not self.bench._timeline_live())
                finally:
                    self.bench.resolve.lock.release()

    def test_already_committed_build_uses_exact_timeline_even_after_newer_build(self):
        first = self.bench._production_operations().latest(self.job["id"], "timeline")
        second = {"request_id": uuid4().hex, "creator": "Lab"}
        self.bench.build_timeline(self.job["id"], second)
        self.assertNotEqual(first["result"]["unique_id"], self.operation(second)["result"]["unique_id"])
        _, started = self.start_pending({"request_id": first["request_id"]})
        self.until(lambda: not self.bench.edit_running())
        record = self.edit(started["edit"]["id"])
        self.assertEqual((record["status"], record["timeline_uid"]), ("completed", first["result"]["unique_id"]))
        self.assertIn("UniqueId " + first["result"]["unique_id"], self.editor.calls[0]["prompt"])
        self.assertNotIn("UniqueId " + self.operation(second)["result"]["unique_id"], self.editor.calls[0]["prompt"])

    def test_other_job_unknown_or_abandoned_request_is_rejected(self):
        with self.assertRaises(VoiceError):
            self.start_pending({"request_id": uuid4().hex})
        original = self.bench._production_operations().latest(self.job["id"], "timeline")
        abandoned = {"request_id": uuid4().hex, "job_id": self.job["id"], "creator": "Lab"}
        self.bench._production_operations().prepare("timeline", abandoned, original["snapshot"])
        with self.assertRaisesRegex(VoiceError, "不在排队"):
            self.start_pending(abandoned)
        other = self.timeline_job()
        with self.assertRaises(VoiceError):
            self.bench.start_edit(other["id"], {"request_id": uuid4().hex, "timeline_request_id": original["request_id"]})
        self.assertEqual(self.editor.calls, [])

    def test_abandoned_live_build_ends_the_wait_without_external_work(self):
        original = self.bench._production_operations().latest(self.job["id"], "timeline")
        build = {"request_id": uuid4().hex, "job_id": self.job["id"], "creator": "Lab"}
        self.bench._production_operations().prepare("timeline", build, original["snapshot"])
        with self.bench.lock:
            self.bench._timeline_live()[self.job["id"]] = {"request_id": build["request_id"], "cancel": threading.Event(), "stage": "waiting"}
            _, started = self.start_pending(build)
            self.bench._timeline_live().pop(self.job["id"])
        self.until(lambda: not self.bench.edit_running())
        record = self.edit(started["edit"]["id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("不在运行", record["error"])
        self.assertEqual(self.editor.calls, [])

    def test_waits_for_slow_catalog_commit_after_build_releases_resolve(self):
        entered, finish = threading.Event(), threading.Event()
        original_commit = self.bench._commit_production_operation

        def commit(operations, record):
            entered.set()
            if not finish.wait(5):
                raise RuntimeError("commit test timed out")
            return original_commit(operations, record)

        with patch.object(self.bench, "_commit_production_operation", side_effect=commit):
            self.bench.resolve.lock.acquire()
            try:
                build = self.queue_build()
                _, started = self.start_pending(build)
            finally:
                self.bench.resolve.lock.release()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(self.bench._timeline_live())
                self.assertFalse(self.editor.started.wait(2.2))
                self.assertEqual(self.edit(started["edit"]["id"])["status"], "running")
            finally:
                finish.set()
            self.until(lambda: not self.bench.edit_running())
        self.assertEqual(self.edit(started["edit"]["id"])["status"], "completed")
        self.assertEqual(len(self.editor.calls), 1)

    def test_direct_and_pending_submissions_cannot_admit_the_same_timeline(self):
        build = self.bench._production_operations().latest(self.job["id"], "timeline")
        self.bench.resolve.lock.acquire()
        try:
            direct = fixture.EditingTests.start(self, self.job)["edit"]
            with self.assertRaisesRegex(VoiceError, "目标时间线"):
                self.start_pending(build)
            self.bench.cancel_edit(self.job["id"], direct["id"])
            self.until(lambda: not self.bench.edit_running())
            with self.bench.lock:
                _, pending = self.start_pending(build)
                with self.assertRaisesRegex(VoiceError, "目标时间线"):
                    fixture.EditingTests.start(self, self.job)
                self.bench.cancel_edit(self.job["id"], pending["edit"]["id"])
            self.until(lambda: not self.bench.edit_running())
            self.assertEqual(self.editor.calls, [])
        finally:
            self.bench.resolve.lock.release()

    def test_interrupted_direct_edit_cannot_bypass_required_review_via_its_build_request(self):
        build = self.bench._production_operations().latest(self.job["id"], "timeline")
        self.bench.resolve.lock.acquire()
        try:
            direct = fixture.EditingTests.start(self, self.job)["edit"]
            self.bench.cancel_edit(self.job["id"], direct["id"])
            self.until(lambda: not self.bench.edit_running())
            with self.bench.lock:
                data = self.bench.production.read()
                job = next(row for row in data["jobs"] if row["id"] == self.job["id"])
                job["edits"][-1]["status"] = "interrupted"
                self.bench.production._write(data)
            with self.assertRaisesRegex(VoiceError, "尚未确认"):
                self.start_pending(build)
            self.assertEqual(self.editor.calls, [])
        finally:
            self.bench.resolve.lock.release()

    def test_restart_retains_interrupted_pending_record_without_running_ai(self):
        self.bench.resolve.lock.acquire()
        try:
            build = self.queue_build()
            _, result = self.start_pending(build)
            self.bench.production.interrupt_running()
            record = self.edit(result["edit"]["id"])
            self.assertEqual(record["status"], "interrupted")
            self.assertTrue(record["waiting_timeline"])
            self.assertIn("AI 剪辑没有开始", record["error"])
            self.assertEqual(record["timeline_request_id"], build["request_id"])
            self.bench.cancel_edit(self.job["id"], record["id"])
            self.bench.cancel_timeline(self.job["id"], {"request_id": build["request_id"]})
            self.until(lambda: not self.bench.edit_running() and not self.bench._timeline_live())
            self.assertEqual(self.editor.calls, [])
        finally:
            self.bench.resolve.lock.release()


if __name__ == "__main__":
    unittest.main()
