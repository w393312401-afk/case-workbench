"""Editing inherits real saved AI preferences with fake editors and Resolve only."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_production_editing as fixture
from test_production_codex_editing import CodexEditor
from ai_tasks import AITaskError, AITaskManager
from case_store import CaseStore
from elevenlabs_client import VoiceError


class ModelCatalog:
    def __init__(self):
        self.entries = [
            {"id": model, "available": True, "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]}
            for model in ("claude-default", "claude-opus", "claude-sonnet", "claude-haiku", "test-codex")
        ]

    def models(self):
        return {"models": copy.deepcopy(self.entries)}


class EditingGlobalDefaultsTests(unittest.TestCase):
    timeline_job = fixture.EditingTests.timeline_job
    mutate = fixture.EditingTests.mutate
    start = fixture.EditingTests.start
    wait = fixture.EditingTests.wait

    def setUp(self):
        fixture.EditingTests.setUp(self)
        root = self.bench.production.root.parent
        skill = root / "isolated-case-store"
        skill.mkdir()
        self.catalog = ModelCatalog()
        self.bench.ai = AITaskManager(CaseStore(skill), root / "ai-runtime", self.bench.lock, bridge=self.catalog)
        self.codex = CodexEditor()
        self.bench.edit_codex = self.codex
        self.addCleanup(self.bench.ai.shutdown)

    def save(self, model, effort=None):
        return self.bench.ai.save_preferences({"model": model, "reasoning_effort": effort})

    def test_compatible_global_model_and_effort_reach_edit_record_and_client(self):
        self.save("claude-opus", "max")
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["model"], edit["effort"]), ("claude-opus", "max"))
        self.assertEqual((self.editor.calls[0]["model"], self.editor.calls[0]["reasoning_effort"]), ("claude-opus", "max"))

    def test_incompatible_global_defaults_fail_before_touching_resolve_or_starting_editor(self):
        job = self.timeline_job()
        before = len(self.bench.runner.calls)
        for model, effort in (("claude-haiku", None), ("claude-opus", "low")):
            with self.subTest(model=model, effort=effort):
                self.save(model, effort)
                with self.assertRaisesRegex(VoiceError, "不支持 AI 剪辑"):
                    self.start(job)
                self.assertEqual(len(self.bench.runner.calls), before)
                self.assertFalse(self.bench.edit_running())
                self.assertEqual(self.editor.calls, [])
                self.assertEqual(self.codex.calls, [])

    def test_saved_codex_model_and_effort_reach_the_native_editor(self):
        expected = self.save("test-codex", "low")
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["provider"], edit["model"], edit["effort"]), ("codex", "test-codex", "low"))
        self.assertEqual((self.codex.calls[0]["model"], self.codex.calls[0]["reasoning_effort"]), ("test-codex", "low"))
        self.assertEqual(self.bench.ai.preferences(), expected)
        self.assertEqual(self.editor.calls, [])

    def test_saved_client_default_and_explicit_codex_default_use_native_defaults(self):
        self.save(None, None)
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["provider"], edit["model"], edit["effort"]), ("codex", "codex-default", None))
        self.assertEqual((self.codex.calls[0]["model"], self.codex.calls[0]["reasoning_effort"]), (None, None))
        expected = self.save("claude-opus", "max")
        self.start(job, model="codex-default")
        second = self.wait(job["id"])
        self.assertEqual((second["provider"], second["model"], second["effort"]), ("codex", "codex-default", None))
        self.assertEqual((self.codex.calls[1]["model"], self.codex.calls[1]["reasoning_effort"]), (None, None))
        self.assertEqual(self.bench.ai.preferences(), expected)
        self.assertEqual(self.editor.calls, [])

    def test_explicit_null_uses_native_default_without_overwriting_saved_claude(self):
        expected = self.save("claude-opus", "max")
        job = self.timeline_job()
        self.start(job, model=None)
        edit = self.wait(job["id"])
        self.assertEqual((edit["provider"], edit["model"], edit["effort"]), ("codex", "codex-default", None))
        self.assertEqual((self.codex.calls[0]["model"], self.codex.calls[0]["reasoning_effort"]), (None, None))
        self.assertEqual(self.bench.ai.preferences(), expected)
        self.assertEqual(self.editor.calls, [])

    def test_explicit_compatible_override_does_not_change_global_default(self):
        global_default = self.save("test-codex", "high")
        job = self.timeline_job()
        self.start(job, model="claude-sonnet", effort="high")
        edit = self.wait(job["id"])
        self.assertEqual((edit["model"], edit["effort"]), ("claude-sonnet", "high"))
        self.assertEqual(self.bench.ai.preferences(), global_default)

    def test_changed_global_default_applies_to_next_edit_but_retry_keeps_original(self):
        self.save("claude-opus", "max")
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"], "mode": "explain", "materials": ["蓝点折刀成品素材"]}
        original = self.bench.start_edit(job["id"], body)["edit"]
        self.wait(job["id"])
        self.save("test-codex", "high")
        retry = self.bench.start_edit(job["id"], body)["edit"]
        self.assertEqual(retry["id"], original["id"])
        self.assertEqual((retry["model"], retry["effort"]), ("claude-opus", "max"))
        self.assertEqual(len(self.editor.calls), 1)
        self.start(job)
        second = self.wait(job["id"])
        self.assertEqual((second["provider"], second["model"], second["effort"]), ("codex", "test-codex", "high"))
        self.assertEqual(len(self.codex.calls), 1)
        self.assertEqual(self.bench.production.job(job["id"])["edits"][0]["model"], "claude-opus")

    def test_unavailable_or_corrupt_defaults_block_work_without_substitution(self):
        self.save("claude-opus", "high")
        job = self.timeline_job()
        self.catalog.entries[1]["available"] = False
        with self.assertRaises(AITaskError):
            self.start(job)
        (self.bench.ai.runtime / "ai-preferences.json").write_text("broken", encoding="utf-8")
        with self.assertRaisesRegex(AITaskError, "无法读取"):
            self.start(job)
        self.assertEqual(self.editor.calls, [])

    def test_delayed_duplicate_keeps_receipt_when_defaults_change_after_first_replay(self):
        self.save("claude-opus", "max")
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"], "materials": [], "mode": "explain"}
        original = self.bench.start_edit(job["id"], body)["edit"]
        self.wait(job["id"])
        self.save("test-codex", "high")
        replay = self.bench.production.replay
        missed = False

        def delayed_replay(kind, value):
            nonlocal missed
            if kind == "edit_start" and not missed:
                missed = True
                return None
            return replay(kind, value)

        with patch.object(self.bench.production, "replay", side_effect=delayed_replay):
            repeated = self.bench.start_edit(job["id"], body)["edit"]
        self.assertEqual(repeated["id"], original["id"])
        self.assertEqual(len(self.editor.calls), 1)


if __name__ == "__main__":
    unittest.main()
