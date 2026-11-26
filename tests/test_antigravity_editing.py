"""Antigravity edits use their own API-backed client for planning and placement."""
import copy
import json
from pathlib import Path
import threading
from unittest import TestCase

import test_production_edit_defaults as defaults_fixture
from ai_tasks import AITaskError
from edit_planning import planner_settings
from elevenlabs_client import VoiceError


API_MODEL = "antigravity:claude-sonnet-5-5-medium"


class AntigravityEditor(defaults_fixture.CodexEditor):
    def __init__(self):
        super().__init__()
        self.entries = [{"id": API_MODEL, "provider": "antigravity", "available": True,
                         "reasoning_efforts": []}]
        self.actual_model = "claude-sonnet-5-5-medium"

    def status(self):
        return {"provider": "antigravity", "available": self.available, "usage_source": "antigravity_api",
                "label": "Antigravity API（Claude Code 执行）"}

    def run(self, prompt, *, cwd, on_progress, cancel_event, profile=None, on_event=None, **options):
        if not (profile or {}).get("mcp"):
            self.calls.append({"prompt": prompt, "cwd": cwd, "profile": copy.deepcopy(profile), **options})
            return '本组建议。\n```json\n{"picks": []}\n```'
        return super().run(prompt, cwd=cwd, on_progress=on_progress, cancel_event=cancel_event,
                           profile=profile, on_event=on_event, **options)


class AntigravityEditingTests(TestCase):
    timeline_job = defaults_fixture.EditingGlobalDefaultsTests.timeline_job
    mutate = defaults_fixture.EditingGlobalDefaultsTests.mutate
    start = defaults_fixture.EditingGlobalDefaultsTests.start
    wait = defaults_fixture.EditingGlobalDefaultsTests.wait

    def setUp(self):
        defaults_fixture.EditingGlobalDefaultsTests.setUp(self)
        self.catalog.entries.append({"id": API_MODEL, "provider": "antigravity", "available": True,
                                     "reasoning_efforts": []})
        self.api = AntigravityEditor()
        self.bench.edit_antigravity = self.api
        self.expected = self.bench.ai.save_preferences({"model": API_MODEL, "reasoning_effort": None})

    def test_api_default_uses_api_client_without_changing_preferences(self):
        self.assertEqual(self.bench._edit_settings({}), ("antigravity", API_MODEL, None))
        self.assertEqual(self.bench.ai.preferences(), self.expected)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])

    def test_api_client_status_identifies_the_api_usage_source(self):
        state = self.bench.edit_client_status()["providers"]["antigravity"]
        self.assertTrue(state["available"])
        self.assertEqual(state["usage_source"], "antigravity_api")
        self.assertIn("Antigravity", state["label"])

    def test_api_does_not_require_the_native_subscription_clients(self):
        self.codex.available = False
        self.editor.status = lambda: {"available": False}
        self.assertEqual(self.bench._edit_settings({}), ("antigravity", API_MODEL, None))

    def test_unavailable_api_never_silently_uses_native_clients(self):
        self.api.available = False
        with self.assertRaisesRegex(VoiceError, "Antigravity API") as failure:
            self.bench._edit_settings({})
        self.assertEqual(failure.exception.status, 503)
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])

    def test_api_catalog_and_provider_must_match(self):
        with self.assertRaisesRegex(VoiceError, "不一致"):
            self.bench._edit_settings({"model": API_MODEL, "provider": "codex"})
        self.api.entries = []
        with self.assertRaisesRegex(VoiceError, "当前配置") as failure:
            self.bench._edit_settings({"model": API_MODEL})
        self.assertEqual(failure.exception.status, 400)

    def test_api_model_uses_service_effort_without_an_independent_override(self):
        with self.assertRaises((AITaskError, VoiceError)):
            self.bench._edit_settings({"model": API_MODEL, "effort": "high"})
        self.assertEqual(planner_settings(API_MODEL, "high", "antigravity"), (API_MODEL, None))

    def test_unavailable_model_is_rejected_before_any_editor_run(self):
        self.catalog.entries[-1]["available"] = False
        with self.assertRaises((AITaskError, VoiceError)):
            self.bench._edit_settings({"model": API_MODEL})
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])

    def test_explicit_native_choice_keeps_its_own_effort(self):
        self.assertEqual(self.bench._edit_settings({"model": "claude-opus", "effort": "high"}),
                         ("claude", "claude-opus", "high"))
        self.assertEqual(self.bench.ai.preferences(), self.expected)

    def test_full_edit_uses_api_and_records_api_model_and_usage_source(self):
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual(edit["execution_status"], "completed")
        self.assertEqual((edit["provider"], edit["requested_model"], edit["effort"]),
                         ("antigravity", API_MODEL, None))
        self.assertEqual(edit["effective_model"], "claude-sonnet-5-5-medium")
        self.assertEqual(edit["usage_source"], "antigravity_api")
        self.assertIn("Antigravity", edit["client_label"])
        calls = [call for call in self.api.calls if call["profile"].get("mcp")]
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["model"], calls[0]["reasoning_effort"]), (API_MODEL, None))
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])

    def test_parallel_planning_uses_api_client_and_a_read_only_profile(self):
        edit_id = "EDT-0123456789ab"
        folder = Path(self.bench.production.root.parent) / "planning"
        folder.mkdir()
        (folder / "candidates.json").write_text(json.dumps({"candidates": [
            {"key": "candidate-1", "timeline": "source", "source": [0, 50], "label": "画面", "grid": None}]}), encoding="utf-8")
        self.bench._edits()[edit_id] = {}
        self.bench._edit_cancel[edit_id] = threading.Event()
        result = self.bench._plan_edit(edit_id, folder, "本次交接", API_MODEL, None, "antigravity")
        self.assertEqual((result["workers"], result["completed"], result["failed"]), (1, 1, 0))
        call = self.api.calls[0]
        self.assertEqual((call["model"], call["reasoning_effort"]), (API_MODEL, None))
        self.assertEqual(call["profile"]["mcp"], {})
        self.assertNotIn("Write", call["profile"]["tools"])
        self.assertTrue((folder / "planning.md").is_file())
        self.assertEqual(self.codex.calls, [])
        self.assertEqual(self.editor.calls, [])
