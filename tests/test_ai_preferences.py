"""Global AI defaults: temporary runtime, fake model catalog, no real AI calls."""
import copy
import json
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_ai_http as http_fixture
from ai_tasks import AITaskManager


class AIPreferencesTests(unittest.TestCase):
    # Reuse only the disposable HTTP fixture, not its unrelated test methods.
    close = http_fixture.AIHTTPTests.close
    request = http_fixture.AIHTTPTests.request
    body = http_fixture.AIHTTPTests.body
    create = http_fixture.AIHTTPTests.create
    wait = http_fixture.AIHTTPTests.wait

    def setUp(self):
        http_fixture.AIHTTPTests.setUp(self)
        self.catalog = {"models": [
            {"id": "test-a", "name": "Test A", "available": True, "reasoning_efforts": ["low", "high"]},
            {"id": "test-b", "name": "Test B", "available": True, "reasoning_efforts": ["medium"]},
            {"id": "claude-default", "name": "Claude 默认", "available": False, "reasoning_efforts": ["high"]},
        ]}
        patcher = patch.object(self.bridge, "models", side_effect=lambda: copy.deepcopy(self.catalog), create=True)
        self.models = patcher.start()
        self.addCleanup(patcher.stop)
        self.path = self.runtime / "ai-preferences.json"

    def save(self, model, effort):
        value = {"model": model, "reasoning_effort": effort}
        status, result = self.request("POST", "/api/ai/preferences", value)
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {"preferences": value})
        return value

    def test_initial_defaults_preserve_ready_claude_preference_and_options_reuse_catalog(self):
        status, result = self.request("GET", "/api/ai/preferences")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["preferences"], {"model": None, "reasoning_effort": None})
        self.catalog["models"][-1]["available"] = True
        before = self.models.call_count
        status, result = self.request("GET", "/api/ai/options")
        self.assertEqual(status, 200, result)
        self.assertEqual(result["default_settings"], {"model": "claude-default", "reasoning_effort": None})
        self.assertFalse(result["defaults_configured"])
        self.assertIsNone(result["defaults_error"])
        self.assertEqual(self.models.call_count, before + 1)
        self.assertEqual(self.bridge.prompts, [])
        self.assertFalse(self.path.exists())

    def test_saved_defaults_persist_across_managers_and_are_exposed_in_options(self):
        selected = self.save("test-a", "high")
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), selected)
        restarted = AITaskManager(self.store, self.runtime, threading.RLock(), bridge=self.bridge)
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.preferences(), selected)
        self.assertEqual(restarted.options()["default_settings"], selected)
        self.assertTrue(restarted.options()["defaults_configured"])
        self.assertEqual(self.request("GET", "/api/ai/preferences")[1]["preferences"], selected)
        self.assertEqual(self.bridge.prompts, [])

    def test_saved_client_default_is_not_replaced_by_later_available_claude(self):
        selected = self.save(None, None)
        self.catalog["models"][-1]["available"] = True
        self.assertEqual(self.request("GET", "/api/ai/preferences")[1]["preferences"], selected)
        self.assertEqual(self.request("GET", "/api/ai/options")[1]["default_settings"], selected)

    def test_invalid_preferences_leave_saved_defaults_unchanged(self):
        self.save("test-a", "low")
        before = self.path.read_bytes()
        for payload in ({}, {"model": "test-a"}, {"model": "test-a", "reasoning_effort": "low", "other": 1},
                        {"model": [], "reasoning_effort": None}, {"model": "", "reasoning_effort": None},
                        {"model": "test-a", "reasoning_effort": []}, {"model": None, "reasoning_effort": "high"},
                        {"model": "missing", "reasoning_effort": None},
                        {"model": "test-b", "reasoning_effort": "high"}):
            with self.subTest(payload=payload):
                status, result = self.request("POST", "/api/ai/preferences", payload)
                self.assertEqual(status, 400, result)
                self.assertEqual(self.path.read_bytes(), before)
        status, result = self.request("POST", "/api/ai/preferences", {"model": "claude-default", "reasoning_effort": None})
        self.assertEqual(status, 503, result)
        self.assertEqual(self.path.read_bytes(), before)

    def test_preferences_write_requires_token_and_same_origin(self):
        payload = {"model": "test-a", "reasoning_effort": "high"}
        for authenticated, headers in ((False, {}), (False, {"X-Workbench-Token": "incorrect"}),
                                       (True, {"Origin": "https://example.invalid"})):
            with self.subTest(authenticated=authenticated, headers=headers):
                status, result = self.request("POST", "/api/ai/preferences", payload,
                                              authenticated=authenticated, headers=headers)
                self.assertEqual(status, 403, result)
                self.assertFalse(self.path.exists())
        self.assertEqual(self.bridge.prompts, [])

    def test_new_and_continued_rounds_inherit_current_defaults_preserving_history_and_skill(self):
        self.save("test-a", "high")
        body = {**self.body(), "skill_selection": "none"}
        task = self.wait(self.create(body)["task_id"])
        original = copy.deepcopy(task["rounds"][0])
        self.assertEqual((original["requested_model"], original["requested_reasoning_effort"]), ("test-a", "high"))
        self.save("test-b", "medium")
        continued = {"request_id": uuid4().hex, "task_revision": task["revision"], "instruction": "继续这轮审查。"}
        status, result = self.request("POST", f"/api/ai/tasks/{task['task_id']}/continue", continued)
        self.assertEqual(status, 200, result)
        latest = self.wait(task["task_id"])
        self.assertEqual(latest["rounds"][0], original)
        turn = latest["rounds"][1]
        self.assertEqual((turn["requested_model"], turn["requested_reasoning_effort"], turn["skill_selection"]),
                         ("test-b", "medium", "none"))
        new_task = self.wait(self.create({**self.body(), "skill_selection": "none"})["task_id"])
        self.assertEqual(new_task["rounds"][0]["requested_model"], "test-b")

    def test_explicit_model_and_null_override_global_defaults(self):
        self.save("test-a", "high")
        for model in (None, "test-b"):
            with self.subTest(model=model):
                task = self.wait(self.create({**self.body(), "model": model, "skill_selection": "none"})["task_id"])
                turn = task["rounds"][0]
                self.assertEqual(turn["requested_model"], model)
                self.assertIsNone(turn["requested_reasoning_effort"])
        self.assertEqual(self.server.app.ai.preferences(), {"model": "test-a", "reasoning_effort": "high"})

    def test_retry_keeps_original_frozen_defaults_after_preference_change(self):
        self.save("test-a", "high")
        body = {**self.body(), "skill_selection": "none"}
        original = self.wait(self.create(body)["task_id"])
        calls = len(self.bridge.prompts)
        self.save("test-b", "medium")
        retry = self.create(body)
        self.assertEqual(retry["task_id"], original["task_id"])
        self.assertEqual(retry["rounds"], original["rounds"])
        self.assertEqual(len(self.bridge.prompts), calls)

    def test_saved_unavailable_model_remains_visible_but_blocks_new_work(self):
        selected = self.save("test-a", "high")
        self.catalog["models"][0]["available"] = False
        self.assertEqual(self.request("GET", "/api/ai/preferences")[1]["preferences"], selected)
        status, result = self.request("POST", "/api/ai/tasks", {**self.body(), "skill_selection": "none"})
        self.assertEqual(status, 503, result)
        self.assertEqual(self.bridge.prompts, [])

    def test_custom_effort_key_resolves_for_production_and_corrupt_settings_can_be_repaired(self):
        self.save("test-a", "high")
        resolved = self.server.app.ai.resolve_default_settings({}, effort_key="effort")
        self.assertEqual(resolved, {"model": "test-a", "effort": "high"})
        explicit = self.server.app.ai.resolve_default_settings({"model": None}, effort_key="effort")
        self.assertEqual(explicit, {"model": None, "effort": None})
        self.path.write_text("invalid json", encoding="utf-8")
        status, result = self.request("GET", "/api/ai/preferences")
        self.assertEqual(status, 500, result)
        status, options = self.request("GET", "/api/ai/options")
        self.assertEqual(status, 200, options)
        self.assertEqual(options["models"], self.catalog["models"])
        self.assertTrue(options["defaults_configured"])
        self.assertIn("重新保存", options["defaults_error"])
        self.assertEqual(options["default_settings"], {"model": None, "reasoning_effort": None})
        status, result = self.request("POST", "/api/ai/tasks", {**self.body(), "skill_selection": "none"})
        self.assertEqual(status, 500, result)
        self.assertEqual(self.bridge.prompts, [])
        self.save(None, None)
        self.assertEqual(self.request("GET", "/api/ai/preferences")[0], 200)
        options = self.request("GET", "/api/ai/options")[1]
        self.assertTrue(options["defaults_configured"])
        self.assertIsNone(options["defaults_error"])


if __name__ == "__main__":
    unittest.main()
