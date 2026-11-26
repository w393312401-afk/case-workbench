"""Restricted native tools use an isolated API transport, never a real editor."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from antigravity_bridge import AntigravityBridge, DEFAULT_MODEL, MODEL_PREFIX
from antigravity_edit_bridge import AntigravityEditBridge
from claude_bridge import ClaudeBridge
from codex_bridge import BridgeCancelled, BridgeError
from test_claude_bridge import FakeProcess, stream_block


KEY = "sk-fixture-antigravity-edit-private"
OPUS_MODEL = "claude-opus-5-5-medium"


def complete(process, model=DEFAULT_MODEL):
    process.emit({"type": "system", "subtype": "init", "model": model})
    stream_block(process, 0, "text", '{"saved": true}')
    process.emit({"type": "result", "subtype": "success", "is_error": False,
                  "result": '{"saved": true}', "usage": {"input_tokens": 11, "output_tokens": 7},
                  "modelUsage": {model: {"inputTokens": 11, "outputTokens": 7}}})
    process.exit()


class AntigravityEditBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.executable = self.root / "claude.exe"
        self.executable.write_bytes(b"fake CLI")
        self.settings = {"enabled": True, "base_url": "http://127.0.0.1:8046/v1", "model": DEFAULT_MODEL}
        self.api = AntigravityBridge(settings_reader=lambda: dict(self.settings), key_reader=lambda: KEY)
        self.launches, self.processes, self.events, self.progress = [], [], [], []
        self.command_runner = Mock(side_effect=AssertionError("subscription auth must not run"))
        self.profile = {"rules": "Only edit the authorized timeline.", "tools": ["Read", "Write", "Edit"],
                        "allowed": ["Read", "Write", "Edit", "mcp__resolve__run_script"],
                        "denied": ["Bash", "mcp__resolve__run_script_unsafe"],
                        "mcp": {"resolve": {"command": "fake-resolve.exe", "args": ["--safe"]}},
                        "dirs": [], "timeout": 5}

    def factory(self, scenario=complete):
        def launch(arguments, **options):
            settings_file = Path(arguments[arguments.index("--settings") + 1])
            rules_file = Path(arguments[arguments.index("--system-prompt-file") + 1])
            config_dir = Path(options["env"]["CLAUDE_CONFIG_DIR"])
            self.assertTrue(config_dir.is_dir())
            config_dir.joinpath("client-side-cache.json").write_text("{}", encoding="utf-8")
            self.launches.append({"arguments": arguments, "options": options,
                                  "settings": settings_file.read_text(encoding="utf-8"),
                                  "rules": rules_file.read_text(encoding="utf-8"), "config_dir": config_dir})
            process = FakeProcess(scenario, binary=True)
            self.processes.append(process)
            return process
        return launch

    def bridge(self, scenario=complete, api=None, **options):
        bridge = AntigravityEditBridge(self.root, api_bridge=api or self.api, executable=str(self.executable),
                                       command_runner=self.command_runner, process_factory=self.factory(scenario), **options)
        self.addCleanup(bridge.close)
        return bridge

    def run_bridge(self, bridge=None, **options):
        return (bridge or self.bridge()).run("Edit this one timeline.", cwd=self.root,
                                             cancel_event=options.pop("cancel_event", threading.Event()),
                                             on_progress=self.progress.append, on_event=self.events.append,
                                             profile=self.profile, **options)

    def test_cli_tools_use_api_credentials_and_preserve_restricted_profile(self):
        original = {"ANTHROPIC_BASE_URL": "https://old.example.invalid", "ANTHROPIC_AUTH_TOKEN": "old-secret",
                    "ANTHROPIC_API_KEY": "old-key", "CLAUDE_CODE_OAUTH_TOKEN": "old-oauth",
                    "CLAUDE_CODE_EFFORT_LEVEL": "max", "MAX_THINKING_TOKENS": "9999",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "other-model", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1",
                    "HTTP_PROXY": "http://proxy.invalid:8080", "HTTPS_PROXY": "http://proxy.invalid:8080",
                    "NO_PROXY": "example.invalid,internal.invalid", "no_proxy": "example.invalid,internal.invalid"}
        with patch.dict(os.environ, original), \
                patch.object(ClaudeBridge, "_client_defaults", side_effect=AssertionError("do not inherit client defaults")):
            self.assertEqual(self.run_bridge(model=MODEL_PREFIX + DEFAULT_MODEL), '{"saved": true}')
            for name, value in original.items():
                self.assertEqual(os.environ[name], value)
        launch = self.launches[0]
        arguments, environment = launch["arguments"], launch["options"]["env"]
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8046")
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], KEY)
        self.assertEqual(environment["ANTHROPIC_MODEL"], DEFAULT_MODEL)
        for name in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL",
                     "CLAUDE_CODE_SUBAGENT_MODEL", "MAX_THINKING_TOKENS", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"):
            self.assertNotIn(name, environment)
        self.assertEqual(set(environment["NO_PROXY"].split(",")),
                         {"example.invalid", "internal.invalid", "localhost", "127.0.0.1", "::1"})
        self.assertEqual(environment["NO_PROXY"], environment["no_proxy"])
        self.assertEqual(environment["HTTP_PROXY"], original["HTTP_PROXY"])
        self.assertEqual(arguments[arguments.index("--model") + 1], DEFAULT_MODEL)
        self.assertNotIn("--effort", arguments)
        self.assertIn("--restricted", arguments)
        self.assertNotIn("--safe-mode", arguments)
        self.assertIn("--strict-mcp-config", arguments)
        self.assertEqual(json.loads(arguments[arguments.index("--mcp-config") + 1]), {"mcpServers": self.profile["mcp"]})
        self.assertEqual(arguments[arguments.index("--tools") + 1], "Read,Write,Edit")
        self.assertEqual(arguments[arguments.index("--disallowedTools") + 1], ",".join(self.profile["denied"]))
        self.assertEqual(launch["options"]["cwd"], str(self.root))
        self.assertEqual(json.loads(launch["settings"]), {"model": DEFAULT_MODEL, "disableAllHooks": True,
                                                        "permissions": {"allow": self.profile["allowed"]}})
        self.assertFalse(launch["config_dir"].exists())
        self.assertNotIn(KEY, repr(arguments) + launch["settings"] + launch["rules"] + repr(self.events) + repr(self.progress))
        self.assertFalse(any("客户端额度" in message for message in self.progress))
        self.command_runner.assert_not_called()

    def test_status_and_models_require_cli_and_api_but_no_subscription_auth(self):
        bridge = self.bridge()
        state = bridge.status()
        self.assertTrue(state["available"])
        self.assertEqual(state["provider"], "antigravity")
        self.assertEqual(state["usage_source"], "antigravity_api")
        self.assertTrue(bridge.models()["models"][0]["supports_tools"])
        self.assertNotIn(KEY, repr(state) + repr(bridge.models()))
        self.executable.unlink()
        self.assertFalse(bridge.status()["available"])
        self.assertFalse(bridge.models()["models"][0]["available"])
        self.command_runner.assert_not_called()

    def test_disabled_api_cannot_launch_or_fall_back_to_subscription(self):
        self.settings["enabled"] = False
        bridge = self.bridge()
        self.assertFalse(bridge.status()["available"])
        with self.assertRaises(BridgeError):
            self.run_bridge(bridge)
        self.assertEqual(self.launches, [])
        self.command_runner.assert_not_called()

    def test_session_and_usage_stay_in_api_namespace(self):
        self.run_bridge()
        sessions = [event for event in self.events if event["type"] == "session"]
        self.assertTrue(sessions)
        self.assertTrue(all(event["model"] == MODEL_PREFIX + DEFAULT_MODEL and event["reasoning_effort"] is None
                            and event["provider"] == "antigravity" for event in sessions))
        usage = next(event for event in self.events if event["type"] == "usage")
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["provider"]), (11, 7, "antigravity"))

    def test_explicit_opus_runs_opus_instead_of_the_sonnet_service_default(self):
        self.settings["models"] = [OPUS_MODEL]
        bridge = self.bridge(scenario=lambda process: complete(process, OPUS_MODEL))
        self.assertEqual(self.run_bridge(bridge, model=MODEL_PREFIX + OPUS_MODEL), '{"saved": true}')
        launch = self.launches[0]
        arguments, environment = launch["arguments"], launch["options"]["env"]
        self.assertEqual(arguments[arguments.index("--model") + 1], OPUS_MODEL)
        self.assertEqual(json.loads(launch["settings"])["model"], OPUS_MODEL)
        for name in ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                     "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
            self.assertEqual(environment[name], OPUS_MODEL)
        sessions = [event for event in self.events if event["type"] == "session"]
        self.assertTrue(all(event["model"] == MODEL_PREFIX + OPUS_MODEL for event in sessions))
        self.assertEqual(next(event for event in self.events if event["type"] == "usage")["models"], [OPUS_MODEL])
        self.assertEqual(self.settings["model"], DEFAULT_MODEL)
        self.assertEqual(bridge.status()["default_model"], MODEL_PREFIX + DEFAULT_MODEL)
        self.assertEqual([row["id"] for row in bridge.models()["models"]],
                         [MODEL_PREFIX + DEFAULT_MODEL, MODEL_PREFIX + OPUS_MODEL])

    def test_none_keeps_the_service_default_when_an_alternative_is_configured(self):
        self.settings["models"] = [OPUS_MODEL]
        self.run_bridge(model=None)
        launch = self.launches[0]
        self.assertEqual(launch["arguments"][launch["arguments"].index("--model") + 1], DEFAULT_MODEL)
        self.assertEqual(launch["options"]["env"]["ANTHROPIC_MODEL"], DEFAULT_MODEL)

    def test_unconfigured_model_is_rejected_even_when_an_alternative_is_available(self):
        self.settings["models"] = [OPUS_MODEL]
        for model in ("antigravity:unknown-model", OPUS_MODEL, ""):
            with self.subTest(model=model), self.assertRaises(BridgeError):
                self.run_bridge(model=model)
        self.assertEqual(self.launches, [])

    def test_user_client_configuration_is_not_read_or_written(self):
        user_config = self.root / "user-client"
        user_config.mkdir()
        original = '{"model":"claude-opus","effortLevel":"max"}'
        path = user_config / "settings.json"
        path.write_text(original, encoding="utf-8")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(user_config)}):
            self.run_bridge()
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual(list(user_config.iterdir()), [path])
        self.assertNotEqual(self.launches[0]["config_dir"], user_config)

    def test_base_url_keeps_proxy_prefix_without_duplicate_v1(self):
        self.settings["base_url"] = "https://example.invalid/relay/v1/"
        self.run_bridge()
        self.assertEqual(self.launches[0]["options"]["env"]["ANTHROPIC_BASE_URL"], "https://example.invalid/relay")

    def test_explicit_other_model_and_independent_effort_are_rejected(self):
        for options in ({"model": "claude-default"}, {"model": MODEL_PREFIX + "another-model"}, {"reasoning_effort": "medium"}):
            with self.subTest(options=options), self.assertRaises(BridgeError):
                self.run_bridge(**options)
        self.assertEqual(self.launches, [])

    def test_cancellation_terminates_only_the_owned_cli(self):
        bridge = self.bridge(scenario=lambda process: None)
        cancel = threading.Event()
        timer = threading.Timer(.05, cancel.set)
        timer.start()
        self.addCleanup(timer.cancel)
        with self.assertRaises(BridgeCancelled):
            self.run_bridge(bridge, cancel_event=cancel)
        self.assertTrue(self.processes[0].terminated)
        self.assertFalse(self.launches[0]["config_dir"].exists())

    def test_api_auth_failure_is_safe_and_does_not_request_subscription_login(self):
        def failure(process):
            process.emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
                          "errors": ["401 unauthorized " + KEY]})
            process.exit()
        with self.assertRaises(BridgeError) as raised:
            self.run_bridge(self.bridge(scenario=failure))
        message = str(raised.exception)
        self.assertIn("Antigravity API", message)
        self.assertNotIn("订阅", message)
        self.assertNotIn(KEY, message)
        self.command_runner.assert_not_called()

    def test_configuration_switch_cannot_mix_model_endpoint_and_key(self):
        snapshots = [
            {"antigravity": dict(self.settings), "antigravity_api_key": "sk-first-key"},
            {"antigravity": {**self.settings, "base_url": "http://127.0.0.1:8047/v1", "model": "claude-sonnet-6"},
             "antigravity_api_key": "sk-second-key"},
        ]
        api = AntigravityBridge()
        with patch("antigravity_bridge.load_config", side_effect=snapshots):
            self.run_bridge(self.bridge(api=api), model=MODEL_PREFIX + DEFAULT_MODEL)
        environment = self.launches[0]["options"]["env"]
        self.assertEqual(environment["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8046")
        self.assertEqual(environment["ANTHROPIC_AUTH_TOKEN"], "sk-first-key")
        self.assertEqual(environment["ANTHROPIC_MODEL"], DEFAULT_MODEL)

    def test_concurrent_jobs_have_separate_api_environments_and_config_directories(self):
        local, barrier = threading.local(), threading.Barrier(2)
        api = AntigravityBridge(settings_reader=lambda: local.settings, key_reader=lambda: local.key)

        def scenario(process):
            barrier.wait(timeout=2)
            complete(process, DEFAULT_MODEL)

        bridge = self.bridge(scenario=scenario, api=api)
        failures = []

        def run(port, key):
            local.settings = {**self.settings, "base_url": f"http://127.0.0.1:{port}/v1"}
            local.key = key
            try:
                self.run_bridge(bridge)
            except Exception as error:
                failures.append(error)

        threads = [threading.Thread(target=run, args=(8046, "sk-first-key")),
                   threading.Thread(target=run, args=(8047, "sk-second-key"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        pairs = {(launch["options"]["env"]["ANTHROPIC_BASE_URL"], launch["options"]["env"]["ANTHROPIC_AUTH_TOKEN"])
                 for launch in self.launches}
        self.assertEqual(pairs, {("http://127.0.0.1:8046", "sk-first-key"), ("http://127.0.0.1:8047", "sk-second-key")})
        self.assertEqual(len({launch["config_dir"] for launch in self.launches}), 2)
        self.assertTrue(all(not launch["config_dir"].exists() for launch in self.launches))


if __name__ == "__main__":
    unittest.main()
