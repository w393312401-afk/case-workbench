"""Fault-injection checks for durable production recovery; no network or Resolve mutation."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
CONFIG = json.loads((APP / "config.json").read_text(encoding="utf-8"))
sys.path.insert(0, str(Path(CONFIG["skill_root"]) / "scripts"))

from elevenlabs_client import VoiceError
from production_operations import ProductionOperations, file_lock
from test_production import Bench, FakeEleven
import test_production_resolve as resolve_tests
from workbench_production import meaning_markers


class RecoveringResolve:
    """Same identity contract as the worker; models a response lost after successful build."""
    def __init__(self, lost_response=False):
        self.calls, self.timelines, self.lost_response = [], {}, lost_response

    def run(self, command, payload=None, **options):
        if command != "build":
            raise AssertionError(command)
        payload = dict(payload)
        self.calls.append(payload)
        key = payload["operation_key"]
        if key not in self.timelines:
            self.timelines[key] = {"ok": True, "name": payload["prefix"] + "1", "unique_id": "line-" + key[:12],
                                  "start_frame": 0, "end_frame": 400, "markers": payload["markers"], "saved": True}
            if self.lost_response:
                self.lost_response = False
                raise VoiceError(504, "模拟建线已成功，但响应丢失。")
        return dict(self.timelines[key])


class ProductionRecoveryTests(unittest.TestCase):
    mutate = resolve_tests.BuildTests.mutate
    voiced_job = resolve_tests.BuildTests.voiced_job

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-recovery-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fake = FakeEleven()
        self.bench = Bench(self.root, self.fake)
        self.bench.resolve = RecoveringResolve()
        self.convert = patch("workbench_production.convert_wav", return_value=(False, None))
        self.convert.start()
        self.addCleanup(self.convert.stop)

    def job(self):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": "Candidate.", "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex,
            "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "title": "稿", "text": "One kit.\nOut goes the jig.", "confirmed": True}}, source)["script"]
        return self.bench.create_production_job({"request_id": uuid4().hex,
            "revision": self.bench.production.public()["revision"], "job": {"script_id": script["id"]}})["job"]

    @staticmethod
    def voice_body(**values):
        return {"request_id": uuid4().hex, "voice_id": "VOICE123456789", **values}

    def speech_calls(self):
        return sum(1 for item in self.fake.calls if item[1].endswith("/with-timestamps"))

    def operation(self, kind, job, body):
        return self.bench._production_operations().read(kind, {**body, "job_id": job["id"]})

    def restart(self):
        resolve = self.bench.resolve
        self.bench = Bench(self.root, self.fake)
        self.bench.resolve = resolve

    def error(self, status, call, *args):
        with self.assertRaises(VoiceError) as raised:
            call(*args)
        self.assertEqual(raised.exception.status, status)
        return str(raised.exception)

    def configured_job(self):
        self.mutate("settings", settings={"creators": ["Lab"]})
        return self.voiced_job()[0]

    def test_voice_catalog_write_failure_recovers_result_without_second_speech_after_restart(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.production, "write", side_effect=OSError("catalog disk failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        before = self.operation("take", job, body)
        self.assertEqual(before["state"], "result_ready")
        self.assertEqual(self.bench.production.job(job["id"])["takes"], [])
        self.restart()
        result = self.bench.recover_production_operation(job["id"], {"request_id": body["request_id"], "kind": "take"})
        self.assertEqual(result["take"]["id"], before["take_id"])
        self.assertEqual(self.speech_calls(), 1)
        self.assertEqual(self.bench.production.voice("VOICE123456789")["use_count"], 1)
        self.assertEqual(self.bench.generate_take(job["id"], body), result)

    def test_new_voice_request_is_blocked_while_saved_result_needs_commit(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.production, "write", side_effect=OSError("disk failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        self.error(409, self.bench.generate_take, job["id"], self.voice_body())
        self.assertEqual(self.speech_calls(), 1)
        summary = self.bench.production_job(job["id"])["recovery_operations"]
        self.assertEqual((len(summary), summary[0]["state"]), (1, "result_ready"))

    def test_voice_processing_failure_recovers_raw_response_without_second_speech(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench, "_timing", side_effect=OSError("processing interrupted")):
            with self.assertRaises(OSError):
                self.bench.generate_take(job["id"], body)
        self.assertEqual(self.operation("take", job, body)["state"], "response_saved")
        self.restart()
        result = self.bench.generate_take(job["id"], body)
        self.assertEqual(result["take"]["quality"]["status"], "passed")
        self.assertEqual(self.speech_calls(), 1)

    def test_voice_saved_response_recovers_even_if_state_update_failed(self):
        job, body = self.job(), self.voice_body()
        update = ProductionOperations.update

        def failed_update(operations, record, state, **values):
            if state == "response_saved":
                raise OSError("operation state disk failure")
            return update(operations, record, state, **values)

        with patch.object(ProductionOperations, "update", failed_update):
            self.error(503, self.bench.generate_take, job["id"], body)
        self.assertEqual(self.operation("take", job, body)["state"], "uncertain")
        self.restart()
        self.bench.generate_take(job["id"], body)
        self.assertEqual(self.speech_calls(), 1)

    def test_voice_response_lost_stays_uncertain_and_never_automatically_resends(self):
        job, body = self.job(), self.voice_body()
        transport = self.fake

        def lost(method, url, headers, data, timeout):
            response = transport(method, url, headers, data, timeout)
            if url.split("?", 1)[0].endswith("/with-timestamps"):
                raise TimeoutError("response lost after provider generation")
            return response

        self.bench.voices.transport = lost
        self.error(503, self.bench.generate_take, job["id"], body)
        self.assertEqual(self.operation("take", job, body)["state"], "uncertain")
        self.restart()
        self.assertIn("不会再次发送", self.error(503, self.bench.generate_take, job["id"], body))
        self.error(409, self.bench.generate_take, job["id"], self.voice_body())
        self.assertEqual(self.speech_calls(), 1)

    def test_voice_audio_persistence_failure_never_resends(self):
        job, body = self.job(), self.voice_body()
        with patch.object(ProductionOperations, "save_speech_response", side_effect=OSError("audio disk failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        self.restart()
        self.error(503, self.bench.generate_take, job["id"], body)
        self.assertEqual(self.speech_calls(), 1)

    def uncertain_voice(self):
        job, body = self.job(), self.voice_body()
        with patch.object(ProductionOperations, "save_speech_response", side_effect=OSError("audio disk failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        return job, body

    def test_manual_not_generated_confirmation_allows_original_request_with_recorded_history(self):
        job, body = self.uncertain_voice()
        take_id = self.operation("take", job, body)["take_id"]
        result = self.bench.recover_production_operation(job["id"], {"request_id": body["request_id"], "kind": "take",
            "decision": "not_generated", "confirmed": True})
        self.assertEqual(result["take"]["id"], take_id)
        self.assertEqual(self.speech_calls(), 2)  # The explicit human decision authorizes another provider request.
        operation = self.operation("take", job, body)
        history = operation["manual_resolution_history"]
        self.assertEqual((len(history), history[0]["decision"], history[0]["original_state"]), (1, "not_generated", "uncertain"))
        self.assertTrue(history[0]["original_error"])

    def test_manual_not_generated_requires_exact_confirmation_and_matching_job(self):
        job, body = self.uncertain_voice()
        base = {"request_id": body["request_id"], "kind": "take", "decision": "not_generated"}
        for confirmation in (None, False, 1, "true"):
            self.error(400, self.bench.recover_production_operation, job["id"], {**base, "confirmed": confirmation})
        other = self.job()
        self.error(409, self.bench.recover_production_operation, other["id"], {**base, "confirmed": True})
        self.assertEqual(self.operation("take", job, body)["state"], "uncertain")
        self.assertEqual(self.speech_calls(), 1)

    def test_manual_not_generated_is_rejected_if_audio_or_saved_response_exists(self):
        job, body = self.uncertain_voice()
        record = self.operation("take", job, body)
        folder = self.bench.production.audio / job["id"] / record["take_id"]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "voiceover.mp3").write_bytes(b"ID3existing-audio")
        self.error(409, self.bench.recover_production_operation, job["id"], {"request_id": body["request_id"], "kind": "take",
            "decision": "not_generated", "confirmed": True})
        self.assertEqual(self.operation("take", job, body)["state"], "uncertain")
        self.assertEqual(self.speech_calls(), 1)

        other, other_body = self.job(), self.voice_body()
        update = ProductionOperations.update

        def failed_update(operations, record, state, **values):
            if state == "response_saved":
                raise OSError("operation state disk failure")
            return update(operations, record, state, **values)

        with patch.object(ProductionOperations, "update", failed_update):
            self.error(503, self.bench.generate_take, other["id"], other_body)
        self.error(409, self.bench.recover_production_operation, other["id"], {"request_id": other_body["request_id"], "kind": "take",
            "decision": "not_generated", "confirmed": True})
        self.assertEqual(self.speech_calls(), 2)
        self.bench.recover_production_operation(other["id"], {"request_id": other_body["request_id"], "kind": "take"})
        self.assertEqual(self.speech_calls(), 2)

    def test_definite_provider_refusal_allows_later_retry_without_uncertain_state(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.voices, "speech", side_effect=VoiceError(409, "ElevenLabs 额度不足，本次没有生成。", retry_safe=True)):
            self.assertIn("额度不足", self.error(409, self.bench.generate_take, job["id"], body))
        self.assertEqual(self.operation("take", job, body)["state"], "safe_failed")
        self.restart()
        self.bench.generate_take(job["id"], body)
        self.assertEqual(self.speech_calls(), 1)

    def test_provider_4xx_is_explicitly_safe_but_5xx_is_uncertain(self):
        for status, state in ((402, "safe_failed"), (429, "safe_failed"), (401, "safe_failed"), (500, "uncertain"), (503, "uncertain")):
            with self.subTest(provider_status=status):
                job, body = self.job(), self.voice_body()
                transport = self.fake

                def rejected(method, url, headers, data, timeout, status=status):
                    if url.split("?", 1)[0].endswith("/with-timestamps"):
                        transport(method, url, headers, data, timeout)
                        return status, {}, b'{"detail": "provider failure"}'
                    return transport(method, url, headers, data, timeout)

                self.bench.voices.transport = rejected
                with self.assertRaises(VoiceError):
                    self.bench.generate_take(job["id"], body)
                self.assertEqual(self.operation("take", job, body)["state"], state)
                calls = self.speech_calls()
                self.bench.voices.transport = self.fake
                if state == "uncertain":
                    self.error(503, self.bench.generate_take, job["id"], body)
                    self.assertEqual(self.speech_calls(), calls)
                else:
                    self.bench.generate_take(job["id"], body)
                    self.assertEqual(self.speech_calls(), calls + 1)

    def test_forced_alignment_response_is_durable_before_catalog_commit(self):
        job, body = self.job(), self.voice_body()
        self.fake.alignment = False
        with patch.object(self.bench.production, "write", side_effect=OSError("catalog failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        self.restart()
        result = self.bench.generate_take(job["id"], body)
        self.assertEqual(result["take"]["alignment_source"], "forced")
        self.assertEqual(sum(1 for item in self.fake.calls if item[1] == "/v1/forced-alignment"), 1)

    def test_reprocessing_uses_saved_forced_alignment_without_another_external_call(self):
        job, body = self.job(), self.voice_body()
        self.fake.alignment = False
        update = ProductionOperations.update

        def failed_update(operations, record, state, **values):
            if state == "result_ready":
                raise OSError("result journal failure")
            return update(operations, record, state, **values)

        with patch.object(ProductionOperations, "update", failed_update):
            with self.assertRaises(OSError):
                self.bench.generate_take(job["id"], body)
        self.restart()
        self.bench.generate_take(job["id"], body)
        self.assertEqual(self.speech_calls(), 1)
        self.assertEqual(sum(1 for item in self.fake.calls if item[1] == "/v1/forced-alignment"), 1)

    def test_unknown_forced_alignment_does_not_repeat_when_processing_restarts(self):
        job, body = self.job(), self.voice_body()
        self.fake.alignment = False
        transport = self.fake

        def lost(method, url, headers, data, timeout):
            result = transport(method, url, headers, data, timeout)
            if url.endswith("/v1/forced-alignment"):
                raise TimeoutError("alignment response lost")
            return result

        self.bench.voices.transport = lost
        update = ProductionOperations.update

        def failed_update(operations, record, state, **values):
            if state == "result_ready":
                raise OSError("result journal failure")
            return update(operations, record, state, **values)

        with patch.object(ProductionOperations, "update", failed_update):
            with self.assertRaises(OSError):
                self.bench.generate_take(job["id"], body)
        self.restart()
        result = self.bench.generate_take(job["id"], body)
        self.assertEqual(result["take"]["quality"]["status"], "failed")
        self.assertTrue(any("结果不确定" in warning for warning in result["take"]["warnings"]))
        self.assertEqual(self.speech_calls(), 1)
        self.assertEqual(sum(1 for item in self.fake.calls if item[1] == "/v1/forced-alignment"), 1)

    def test_pre_call_log_failure_does_not_send_speech(self):
        job, body = self.job(), self.voice_body()
        with patch.object(ProductionOperations, "write", side_effect=OSError("operation disk failure")):
            with self.assertRaises(OSError):
                self.bench.generate_take(job["id"], body)
        self.assertEqual(self.speech_calls(), 0)
        self.bench.generate_take(job["id"], body)
        self.assertEqual(self.speech_calls(), 1)

    def test_invalid_request_and_nonfinite_input_are_rejected_before_any_speech(self):
        job = self.job()
        for body in (self.voice_body(request_id="short"), self.voice_body(request_id="../../evil-id"),
                     self.voice_body(unused=float("nan"))):
            self.error(400, self.bench.generate_take, job["id"], body)
        self.assertEqual(self.fake.calls, [])

    def test_pending_request_payload_conflict_is_rejected_before_speech(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.production, "write", side_effect=OSError("disk failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        self.error(409, self.bench.generate_take, job["id"], {**body, "settings": {"stability": 0.6}})
        self.assertEqual(self.speech_calls(), 1)

    def test_same_job_and_same_request_are_locked_across_bench_instances(self):
        job, body = self.job(), self.voice_body()
        entered, release = threading.Event(), threading.Event()
        original = self.bench.voices.speech

        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)

        self.bench.voices.speech = paused
        other = Bench(self.root, self.fake)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(self.bench.generate_take, job["id"], body)
            self.assertTrue(entered.wait(10))
            try:
                self.error(409, other.generate_take, job["id"], body)
                self.error(409, other.generate_take, job["id"], self.voice_body())
            finally:
                release.set()
            result = pending.result(timeout=10)
        self.assertEqual(self.speech_calls(), 1)
        self.assertEqual(other.generate_take(job["id"], body), result)

    def test_file_lock_is_held_across_processes(self):
        target = self.root / "process.lock"
        code = """import sys
from production_operations import file_lock
from elevenlabs_client import VoiceError
try:
    with file_lock(sys.argv[1], 'busy'):
        print('unlocked')
except VoiceError as error:
    print(error.status)
"""
        with file_lock(target, "busy"):
            completed = subprocess.run([sys.executable, "-c", code, str(target)], cwd=APP,
                                       capture_output=True, text=True, timeout=10)
        self.assertEqual((completed.returncode, completed.stdout.strip()), (0, "409"))

    def test_build_catalog_write_failure_recovers_without_second_build(self):
        job, body = self.configured_job(), {"request_id": uuid4().hex, "creator": "Lab"}
        with patch.object(self.bench.production, "write", side_effect=OSError("catalog disk failure")):
            self.error(503, self.bench.build_timeline, job["id"], body)
        before = self.operation("timeline", job, body)
        self.assertEqual(before["state"], "result_ready")
        self.restart()
        result = self.bench.recover_production_operation(job["id"], {"request_id": body["request_id"], "kind": "timeline"})
        self.assertEqual(result["timeline"]["unique_id"], before["result"]["unique_id"])
        self.assertEqual(len(self.bench.resolve.calls), 1)

    def test_build_response_lost_uses_same_external_identity_after_restart(self):
        job, body = self.configured_job(), {"request_id": uuid4().hex, "creator": "Lab"}
        self.bench.resolve = RecoveringResolve(lost_response=True)
        self.error(504, self.bench.build_timeline, job["id"], body)
        self.error(409, self.bench.build_timeline, job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.assertEqual(self.operation("timeline", job, body)["state"], "uncertain")
        self.restart()
        self.bench.build_timeline(job["id"], body)
        resolve = self.bench.resolve
        self.assertEqual((len(resolve.calls), len(resolve.timelines)), (2, 1))
        self.assertEqual(resolve.calls[0]["operation_key"], resolve.calls[1]["operation_key"])
        self.assertEqual(resolve.calls[0]["recovery_file"], resolve.calls[1]["recovery_file"])

    def test_build_result_journal_write_failure_recovers_original_line(self):
        job, body = self.configured_job(), {"request_id": uuid4().hex, "creator": "Lab"}
        update = ProductionOperations.update

        def failed_update(operations, record, state, **values):
            if state == "result_ready":
                raise OSError("result journal disk failure")
            return update(operations, record, state, **values)

        with patch.object(ProductionOperations, "update", failed_update):
            with self.assertRaises(OSError):
                self.bench.build_timeline(job["id"], body)
        self.restart()
        self.bench.build_timeline(job["id"], body)
        self.assertEqual(len(self.bench.resolve.timelines), 1)

    def test_invalid_build_request_is_rejected_before_worker(self):
        job = self.configured_job()
        for body in ({"request_id": "short", "creator": "Lab"},
                     {"request_id": uuid4().hex, "creator": "Lab", "unused": float("inf")}):
            self.error(400, self.bench.build_timeline, job["id"], body)
        self.assertEqual(self.bench.resolve.calls, [])

    def test_recovery_cannot_move_saved_result_to_another_job_or_kind(self):
        job, body = self.job(), self.voice_body()
        with patch.object(self.bench.production, "write", side_effect=OSError("catalog failure")):
            self.error(503, self.bench.generate_take, job["id"], body)
        other = self.job()
        self.error(409, self.bench.recover_production_operation, other["id"], {"request_id": body["request_id"], "kind": "take"})
        self.error(409, self.bench.recover_production_operation, job["id"], {"request_id": body["request_id"], "kind": "timeline"})
        self.assertEqual(self.speech_calls(), 1)

    def test_incomplete_alignment_is_recorded_but_cannot_be_selected_or_built(self):
        job, body = self.job(), self.voice_body()
        groups = [{"number": 1, "start": 0.0, "end": 1.0, "coverage": 0.59},
                  {"number": 2, "start": 1.0, "end": 2.0, "coverage": 1.0}]
        with patch.object(self.bench, "_timing", return_value=(groups, "forced", ["对齐不足"])):
            take = self.bench.generate_take(job["id"], body)["take"]
        self.assertEqual(take["quality"]["status"], "failed")
        self.error(409, self.bench.select_take, job["id"], {"request_id": uuid4().hex,
            "revision": self.bench.production.public()["revision"], "take_id": take["id"]})
        self.error(409, meaning_markers, job, take, "29.97")
        self.mutate("settings", settings={"creators": ["Lab"]})
        catalog = self.bench.production.read()
        catalog["jobs"][0]["selected_take"] = take["id"]  # Existing catalogs may already contain a bad selection.
        self.bench.production.write(catalog)
        self.error(409, self.bench.build_timeline, job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.assertEqual(self.bench.resolve.calls, [])


if __name__ == "__main__":
    unittest.main()
