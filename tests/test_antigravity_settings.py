"""Service settings preserve local configuration and never expose credentials."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import api_keys
from test_server import SERVER


class AntigravitySettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "config.local.json"
        self.original = {"port": 9876, "runtime_root": "private/runtime"}
        self.path.write_text(json.dumps(self.original), encoding="utf-8")
        self.registry = {}
        self.bridge = Mock()
        self.bridge.status.return_value = {"available": True, "label": "Antigravity 已配置"}
        self.bridge.check_connection.return_value = {"ok": True, "detail": "模型已返回正文"}
        self.app = object.__new__(SERVER.Workbench)
        self.app.ai = SimpleNamespace(bridge=SimpleNamespace(antigravity=self.bridge))
        patches = [patch.object(SERVER, "APP_ROOT", self.root),
                   patch.object(api_keys, "KEY_CONFIG_ROOT", self.root),
                   patch.object(api_keys, "_registry", side_effect=self.registry.get),
                   patch.object(api_keys, "_broadcast"), patch.dict(os.environ, {}, clear=False)]
        for pending in patches:
            pending.start()
            self.addCleanup(pending.stop)
        os.environ.pop("ANTIGRAVITY_API_KEY", None)
        self.body = {"enabled": True, "base_url": "http://127.0.0.1:8046/v1",
                     "model": "claude-sonnet-5-5-medium", "vision": False}

    def test_save_preserves_paths_and_only_returns_the_key_tail(self):
        key = "sk-fixture-private-credential-WXYZ"
        result = self.app.antigravity_settings({**self.body, "api_key": key})
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {**self.original, "antigravity": self.body, "antigravity_api_key": key})
        self.assertEqual(api_keys.read("ANTIGRAVITY_API_KEY"), key)
        self.assertTrue(result["key_set"])
        self.assertEqual(result["key_tail"], "WXYZ")
        self.assertNotIn(key, json.dumps(result))
        self.bridge.refresh_models.assert_called_once()

    def test_blank_key_keeps_existing_credential_and_does_not_run_model(self):
        self.registry["ANTIGRAVITY_API_KEY"] = "sk-fixture-existing-credential-ABCD"
        self.app.antigravity_settings({**self.body, "api_key": "   "})
        self.assertEqual(self.registry["ANTIGRAVITY_API_KEY"], "sk-fixture-existing-credential-ABCD")
        self.bridge.check_connection.assert_not_called()
        self.assertEqual(self.app.antigravity_settings()["key_tail"], "ABCD")

    def test_saving_existing_form_keeps_additional_models_for_the_same_service(self):
        models = ["claude-sonnet-5-5-medium", "claude-opus-5-5-medium"]
        previous = {**self.original, "antigravity": {**self.body, "models": models}}
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        result = self.app.antigravity_settings({**self.body, "vision": True})
        self.assertEqual(result["settings"]["models"], models)
        self.assertEqual(result["settings"]["model"], self.body["model"])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["antigravity"],
                         {**self.body, "vision": True, "models": models})

    def test_explicit_model_list_can_be_replaced_or_cleared(self):
        for models in (["claude-opus-5-5-medium"], []):
            result = self.app.antigravity_settings({**self.body, "models": models})
            self.assertEqual(result["settings"]["models"], models)
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["antigravity"]["models"], models)

    def test_switching_service_does_not_copy_the_previous_model_catalog(self):
        previous = {**self.original, "antigravity": {**self.body, "models": ["claude-opus-5-5-medium"]}}
        self.path.write_text(json.dumps(previous), encoding="utf-8")
        result = self.app.antigravity_settings({**self.body, "base_url": "http://127.0.0.1:9999/v1"})
        self.assertNotIn("models", result["settings"])
        self.assertNotIn("models", json.loads(self.path.read_text(encoding="utf-8"))["antigravity"])

    def test_failed_save_keeps_both_previous_endpoint_and_credential(self):
        prior = {**self.original, "antigravity": self.body, "antigravity_api_key": "sk-old-private-key-ABCD"}
        self.path.write_text(json.dumps(prior), encoding="utf-8")
        with patch("app_config.os.replace", side_effect=OSError("fixture disk error")):
            with self.assertRaises(SERVER.ApiError):
                self.app.antigravity_settings({**self.body, "base_url": "http://127.0.0.1:9999/v1",
                                              "api_key": "sk-new-private-key-WXYZ"})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), prior)

    def test_invalid_and_incomplete_configuration_does_not_change_file(self):
        previous = self.path.read_bytes()
        for changes in ({"base_url": "https://example.com/v1?token=secret"}, {"vision": "false"},
                        {"unknown": True}, {"api_key": []}, {"models": "claude-opus-5-5-medium"},
                        {"models": ["antigravity:claude-opus-5-5-medium"]}):
            with self.subTest(changes=changes):
                with self.assertRaises(SERVER.ApiError) as raised:
                    self.app.antigravity_settings({**self.body, **changes})
                self.assertEqual(raised.exception.status, 400)
                self.assertEqual(self.path.read_bytes(), previous)
        with self.assertRaises(SERVER.ApiError) as raised:
            self.app.antigravity_settings({"enabled": True, "api_key": ""})
        self.assertEqual(raised.exception.status, 400)

    def test_invalid_key_keeps_the_validation_error(self):
        with self.assertRaises(api_keys.KeyError_) as raised:
            self.app.antigravity_settings({**self.body, "api_key": "wrong-prefix"})
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), self.original)

    def test_explicit_check_uses_the_api_bridge_once(self):
        self.assertEqual(self.app.check_antigravity({}), {"ok": True, "detail": "模型已返回正文"})
        self.bridge.check_connection.assert_called_once_with()
        with self.assertRaises(SERVER.ApiError):
            self.app.check_antigravity({"unexpected": True})

    def test_remote_forwarded_requests_cannot_read_or_change_service_settings(self):
        for method in ("GET", "POST"):
            for headers, address in (({"X-Forwarded-For": "192.0.2.1"}, "127.0.0.1"), ({}, "192.0.2.1")):
                with self.subTest(method=method, headers=headers, address=address):
                    handler = object.__new__(SERVER.Handler)
                    handler.command, handler.path = method, "/api/ai/antigravity"
                    handler.headers, handler.client_address = headers, (address, 10000)
                    handler.server = Mock(app=Mock())
                    handler.guard = Mock()
                    handler.output = Mock()
                    handler.dispatch()
                    self.assertEqual(handler.output.call_args.args[0], 403)
                    handler.server.app.antigravity_settings.assert_not_called()
                    handler.server.begin_write.assert_not_called()


class AntigravityLocalKeyTests(unittest.TestCase):
    def test_local_credential_updates_and_removal_override_stale_environment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            key = "sk-fixture-local-credential-WXYZ"
            with patch.object(api_keys, "KEY_CONFIG_ROOT", root), \
                    patch.object(api_keys, "_registry", return_value="sk-stale-registry-value-ABCD"), \
                    patch.dict(os.environ, {"ANTIGRAVITY_API_KEY": "sk-stale-process-value-ABCD"}):
                result = api_keys.save({"name": "ANTIGRAVITY_API_KEY", "value": key})
                self.assertEqual(api_keys.read("ANTIGRAVITY_API_KEY"), key)
                self.assertEqual(json.loads((root / "config.local.json").read_text(encoding="utf-8")),
                                 {"antigravity_api_key": key})
                self.assertNotIn(key, json.dumps(result))
                removed = api_keys.remove({"name": "ANTIGRAVITY_API_KEY"})
                self.assertEqual(api_keys.read("ANTIGRAVITY_API_KEY"), "")
                row = next(row for row in removed["keys"] if row["name"] == "ANTIGRAVITY_API_KEY")
                self.assertFalse(row["set"])

    def test_missing_local_credential_preserves_manual_environment_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(api_keys, "KEY_CONFIG_ROOT", Path(folder)), \
                    patch.object(api_keys, "_registry", return_value=None), \
                    patch.dict(os.environ, {"ANTIGRAVITY_API_KEY": "sk-existing-environment-value"}):
                self.assertEqual(api_keys.read("ANTIGRAVITY_API_KEY"), "sk-existing-environment-value")


if __name__ == "__main__":
    unittest.main()
