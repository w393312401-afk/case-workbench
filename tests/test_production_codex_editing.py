"""Native Codex edits use isolated production files and a fake Resolve worker."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_editing as fixture
from elevenlabs_client import VoiceError


class CodexEditor(fixture.Editor):
    """The native bridge has its own availability, catalog and session receipt."""

    def __init__(self, **options):
        super().__init__(**options)
        self.available, self.model_reads = True, 0
        self.actual_model, self.actual_effort = None, None
        self.emit_session = True
        self.entries = [{"id": model, "name": "Codex test model", "available": True,
                         "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]}
                        for model in ("gpt-6.1-sol", "test-codex")]

    def status(self):
        return {"available": self.available, "label": "本机 Codex 测试连接",
                "error": None if self.available else "本机 Codex 暂不可用"}

    def models(self):
        self.model_reads += 1
        return {"available": self.available, "models": copy.deepcopy(self.entries)}

    def run(self, prompt, *, cwd, on_progress, cancel_event, profile=None, on_event=None, **options):
        if self.emit_session and on_event:
            on_event({"type": "session", "model": self.actual_model or options.get("model") or "gpt-6.1-sol",
                      "reasoning_effort": self.actual_effort or options.get("reasoning_effort") or "medium",
                      "skills": []})
        return super().run(prompt, cwd=cwd, on_progress=on_progress, cancel_event=cancel_event,
                           profile=profile, on_event=on_event, **options)


class CodexEditingTests(unittest.TestCase):
    timeline_job = fixture.EditingTests.timeline_job
    mutate = fixture.EditingTests.mutate
    start = fixture.EditingTests.start
    wait = fixture.EditingTests.wait

    def setUp(self):
        fixture.EditingTests.setUp(self)
        self.codex = CodexEditor()
        self.bench.edit_codex = self.codex
        self.addCleanup(self.bench.stop_edits)

    def test_native_default_keeps_client_settings_and_runs_the_editing_skill(self):
        job = self.timeline_job()
        started = self.start(job, provider="codex", model="codex-default")["edit"]
        edit = self.wait(job["id"])
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(self.editor.calls, [])
        call = self.codex.calls[0]
        self.assertIsNone(call["model"])
        self.assertIsNone(call["reasoning_effort"])
        self.assertEqual((edit["provider"], edit["requested_model"], edit["model"]),
                         ("codex", "codex-default", "codex-default"))
        self.assertEqual((edit["effective_model"], edit["effective_effort"]), ("gpt-6.1-sol", "medium"))
        self.assertIn("Codex", edit["client_label"])
        self.assertTrue(edit["usage_source"])
        self.assertEqual((edit["id"], edit["execution_status"]), (started["id"], "completed"))
        folder = Path(call["cwd"])
        self.assertEqual(folder.name, edit["id"])
        self.assertTrue((folder / "skill" / "SKILL.md").is_file())
        self.assertTrue((folder / "skill" / "references" / "source-ledger.md").is_file())
        self.assertFalse((folder / "skill" / "agents").exists())
        self.assertIn((folder / "handoff.md").read_text(encoding="utf-8"), call["prompt"])
        self.assertIn("davinci-product-video-editing", call["profile"]["rules"])
        self.assertEqual(call["profile"]["dirs"], [str(self.bench.ledger_dir)])
        self.assertEqual(call["profile"]["mcp"].keys(), {"resolve"})
        self.assertIn("mcp__resolve__run_script", call["profile"]["allowed"])
        self.assertIn("mcp__resolve__run_script_unsafe", call["profile"]["denied"])
        self.assertFalse(any("unsafe" in name for name in call["profile"]["allowed"]))
        self.assertFalse(self.bench.resolve.busy())

    def test_selected_model_and_low_effort_use_native_catalog_and_actual_receipt(self):
        self.codex.actual_model, self.codex.actual_effort = "gpt-6.1-sol-resolved", "medium"
        job = self.timeline_job()
        self.start(job, model="gpt-6.1-sol", effort="low")
        edit = self.wait(job["id"])
        self.assertEqual((self.codex.calls[0]["model"], self.codex.calls[0]["reasoning_effort"]), ("gpt-6.1-sol", "low"))
        self.assertEqual((edit["requested_model"], edit["effort"]), ("gpt-6.1-sol", "low"))
        self.assertEqual((edit["effective_model"], edit["effective_effort"]), ("gpt-6.1-sol-resolved", "medium"))
        self.assertGreater(self.codex.model_reads, 0)
        self.assertEqual(self.editor.calls, [])

    def test_status_reports_both_clients_and_claude_retains_its_profile(self):
        state = self.bench.edit_client_status()
        self.assertTrue(state["providers"]["codex"]["available"])
        self.assertTrue(state["providers"]["claude"]["available"])
        self.assertFalse(state["busy"])
        job = self.timeline_job()
        self.start(job, model="claude-opus", effort="max")
        edit = self.wait(job["id"])
        self.assertEqual(edit["provider"], "claude")
        self.assertEqual(self.codex.calls, [])
        self.assertEqual((self.editor.calls[0]["model"], self.editor.calls[0]["reasoning_effort"]), ("claude-opus", "max"))
        self.assertEqual(self.editor.calls[0]["skills"][0]["name"], "davinci-product-video-editing")
        self.assertIsNone(edit["effective_model"])
        self.assertIsNone(edit["effective_effort"])

    def test_default_can_run_without_claude_or_the_model_catalog(self):
        job = self.timeline_job()
        self.bench.editor = None
        with patch.object(self.codex, "models", side_effect=RuntimeError("模型列表暂不可用")) as models:
            self.start(job, model="codex-default")
            edit = self.wait(job["id"])
            self.assertEqual(edit["execution_status"], "completed")
            self.assertEqual(models.call_count, 0)
            before = len(self.bench.runner.calls)
            with self.assertRaises(VoiceError) as caught:
                self.start(job, model="gpt-6.1-sol")
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(len(self.bench.runner.calls), before)
        self.assertEqual(len(self.codex.calls), 1)

    def test_no_client_substitution_when_either_requested_provider_is_unavailable(self):
        job = self.timeline_job()
        before = len(self.bench.runner.calls)
        self.codex.available = False
        with self.assertRaises(VoiceError) as caught:
            self.start(job, model="codex-default")
        self.assertEqual(caught.exception.status, 503)
        self.codex.available = True
        self.bench.editor = None
        with self.assertRaises(VoiceError) as caught:
            self.start(job, model="claude-opus", effort="high")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])
        self.assertEqual(len(self.bench.runner.calls), before)
        self.assertEqual(self.bench.production.job(job["id"])["edits"], [])

    def test_invalid_native_model_effort_and_provider_fail_before_resolve(self):
        job = self.timeline_job()
        before = len(self.bench.runner.calls)
        for values in ({"model": "gpt-missing"}, {"model": "gpt-6.1-sol", "effort": "ultra"},
                       {"model": "codex-default", "effort": "high"},
                       {"provider": "claude", "model": "gpt-6.1-sol"},
                       {"provider": "codex", "model": "claude-opus"}):
            with self.subTest(values=values), self.assertRaises(VoiceError) as caught:
                self.start(job, **values)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])
        self.assertEqual(len(self.bench.runner.calls), before)
        self.assertEqual(self.bench.production.job(job["id"])["edits"], [])

    def test_disabled_native_catalog_entry_does_not_start_ai_or_resolve(self):
        job = self.timeline_job()
        before = len(self.bench.runner.calls)
        self.codex.entries[0]["available"] = False
        with self.assertRaises(VoiceError) as caught:
            self.start(job, model="gpt-6.1-sol")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])
        self.assertEqual(len(self.bench.runner.calls), before)

    def test_no_session_receipt_does_not_invent_effective_settings(self):
        self.codex.emit_session = False
        job = self.timeline_job()
        self.start(job, model="gpt-6.1-sol", effort="high")
        edit = self.wait(job["id"])
        self.assertEqual((edit["model"], edit["effort"]), ("gpt-6.1-sol", "high"))
        self.assertIsNone(edit["effective_model"])
        self.assertIsNone(edit["effective_effort"])

    def test_codex_result_still_requires_independent_timeline_validation(self):
        for expected, reply in (("awaiting_review", fixture.inspect_reply(shots=((0, 200), (200, 400)))),
                                ("needs_material", fixture.inspect_reply(gaps=((200, 400),))),
                                ("needs_repair", fixture.inspect_reply())):
            with self.subTest(status=expected):
                job = self.timeline_job()
                self.bench.runner.inspect = reply
                self.start(job, model="codex-default")
                edit = self.wait(job["id"])
                self.assertEqual((edit["status"], edit["validation"]["status"]), (expected, expected))
                self.assertEqual(edit["execution_status"], "completed")
                self.assertIsNotNone(edit["baseline"])
                self.assertTrue(self.bench.runner.calls[-1]["save"])
                self.assertFalse(self.bench.resolve.busy())
        self.assertEqual(self.editor.calls, [])

    def test_cancel_reports_native_progress_then_reads_back_and_unlocks_resolve(self):
        self.codex.block = True
        job = self.timeline_job()
        started = self.start(job, model="codex-default")["edit"]
        self.assertTrue(self.codex.started.wait(5))
        state = self.bench.edit_client_status()
        self.assertTrue(state["busy"])
        self.assertEqual(self.bench.edit_live(started["id"])["latest"], "选第 1 个意群的画面。")
        with self.assertRaises(VoiceError) as caught:
            self.start(job, model="claude-opus")
        self.assertEqual(caught.exception.status, 409)
        self.bench.cancel_edit(job["id"], started["id"])
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"]), ("cancelled", "cancelled"))
        self.assertIsNotNone(edit["summary"])
        self.assertTrue(self.bench.runner.calls[-1]["save"])
        self.assertFalse(self.bench.resolve.busy())
        self.assertFalse(self.bench.edit_running())
        self.assertEqual(self.editor.calls, [])

    def test_native_failure_preserves_error_receipt_and_does_not_fallback(self):
        self.codex.error = "Codex 测试连接中断"
        job = self.timeline_job()
        self.start(job, model="codex-default")
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"], edit["error"]),
                         ("failed", "failed", "Codex 测试连接中断"))
        self.assertEqual(edit["effective_model"], "gpt-6.1-sol")
        self.assertIsNotNone(edit["summary"])
        self.assertFalse(self.bench.resolve.busy())
        self.assertFalse(self.bench.edit_running())
        self.assertEqual(self.editor.calls, [])

    def test_duplicate_request_keeps_original_receipt_after_client_becomes_unavailable(self):
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"],
                "model": "codex-default", "materials": [], "mode": "explain"}
        original = self.bench.start_edit(job["id"], body)["edit"]
        self.wait(job["id"])
        before = len(self.bench.runner.calls)
        self.codex.available = False
        repeated = self.bench.start_edit(job["id"], body)["edit"]
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(repeated["requested_model"], "codex-default")
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(len(self.bench.runner.calls), before)
        self.assertEqual(self.editor.calls, [])

    def test_malformed_readback_records_verification_failure_and_releases_locks(self):
        job = self.timeline_job()
        self.bench.runner.final_inspect = {"ok": True, "tracks": []}
        self.start(job, model="codex-default")
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"]), ("verification_failed", "completed"))
        self.assertEqual(edit["validation"]["issues"][0]["code"], "readback_invalid")
        self.assertFalse(self.bench.resolve.busy())
        self.assertFalse(self.bench.edit_running())


if __name__ == "__main__":
    unittest.main()
