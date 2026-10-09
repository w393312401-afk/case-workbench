"""Local Claude Code subprocess tests: no API key, network, or real model calls.

Run with: python -m unittest discover -s tests -p test_claude_bridge.py -v
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claude_bridge import ClaudeBridge, ModelRouter  # noqa: E402
from codex_bridge import BridgeCancelled, BridgeError, BridgeUnavailable  # noqa: E402

SECRET = "sk-ant-test-secret-that-must-never-appear"
AUTHENTICATED = {
    "loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max",
    "email": "private@example.invalid", "accessToken": SECRET, "refreshToken": SECRET,
}
LOGIN_STATE = "private-auth-state-that-must-never-be-in-status"
LOGIN_CODE = "private-auth-code-that-must-never-be-in-status"
LOGIN_REDIRECT = "https://platform.claude.com/oauth/code/callback"
LOGIN_LOCAL_REDIRECT = "http://localhost:45678/callback"
LOGIN_URL = "https://claude.com/cai/oauth/authorize?" + urlencode({
    "code": "true", "client_id": "fake-public-client", "response_type": "code",
    "redirect_uri": LOGIN_REDIRECT, "scope": "user:profile", "code_challenge": "fake-public-challenge",
    "code_challenge_method": "S256", "state": LOGIN_STATE,
})


class FakeReader:
    """A pipe that supports the bridge's asynchronous readline loops."""

    def __init__(self, binary=False):
        self.binary = binary
        self.queue = queue.Queue()
        self.closed = False

    def put(self, text):
        self.queue.put(text.encode("utf-8") if self.binary else text)

    def readline(self, limit=-1):
        return self.queue.get(timeout=5)

    read = readline

    def __iter__(self):
        return self

    def __next__(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line

    def close(self):
        if not self.closed:
            self.closed = True
            self.put("")


class FakeWriter:
    def __init__(self, process, parse_json=True):
        self.process = process
        self.parse_json = parse_json
        self.text = ""
        self.pending = ""
        self.closed = False

    def write(self, value):
        text = bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray, memoryview)) else value
        self.text += text
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            if line.strip():
                self.process.receive(json.loads(line) if self.parse_json else line)
        return len(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if self.pending.strip():
            self.process.receive(json.loads(self.pending) if self.parse_json else self.pending)
            self.pending = ""


class FakeProcess:
    def __init__(self, scenario, *, binary=False, login=False):
        self.stdout, self.stderr = FakeReader(binary), FakeReader(binary)
        self.stdin = FakeWriter(self, parse_json=not login)
        self.scenario = scenario
        self.received = []
        self.started = threading.Event()
        self.exit_event = threading.Event()
        self.returncode = None
        self.terminated = False
        self.killed = False

    def receive(self, message):
        self.received.append(message)
        self.started.set()
        self.scenario(self)

    def emit(self, message):
        self.stdout.put(json.dumps(message, ensure_ascii=False) + "\n")

    def exit(self, code=0):
        if self.returncode is None:
            self.returncode = code
            self.exit_event.set()
            self.stdout.close()
            self.stderr.close()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self.exit_event.wait(timeout):
            raise subprocess.TimeoutExpired("fake-claude", timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.exit(-1)

    def kill(self):
        self.killed = True
        self.exit(-9)


def stream_block(process, index, kind, value):
    process.emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": index,
        "content_block": {"type": kind, kind: ""},
    }})
    for piece in (value[:len(value) // 2], value[len(value) // 2:]):
        process.emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": index,
            "delta": {"type": kind + "_delta", kind: piece},
        }})
    process.emit({"type": "stream_event", "event": {"type": "content_block_stop", "index": index}})


def success(process):
    process.emit({"type": "system", "subtype": "init", "session_id": "fake-session", "model": "claude-client-default-model"})
    stream_block(process, 0, "thinking", "先看方向和评论。")
    stream_block(process, 1, "text", "🔴Stop guessing the angle#knifetok")
    process.emit({"type": "assistant", "message": {"id": "msg-1", "content": [
        {"type": "thinking", "thinking": "先看方向和评论。"},
        {"type": "text", "text": "🔴Stop guessing the angle#knifetok"},
    ]}})
    process.emit({"type": "result", "subtype": "success", "is_error": False,
                  "result": "🔴Stop guessing the angle#knifetok", "session_id": "fake-session"})
    process.exit()


class ClaudeBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-bridge-tests-")
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name)
        self.executable = self.runtime / "claude.exe"
        self.executable.write_bytes(b"fake executable")
        self.auth = dict(AUTHENTICATED)
        self.auth_calls = []
        self.launches = []
        self.processes = []
        self.events = []
        self.progress = []
        self.cancel = threading.Event()

    def auth_runner(self, arguments, **options):
        self.auth_calls.append((arguments, options))
        return subprocess.CompletedProcess(arguments, 0, json.dumps(self.auth), "")

    def bridge(self, scenario=success, **options):
        def factory(arguments, **kwargs):
            launch = {"arguments": list(arguments), "options": kwargs, "files": {}}
            for flag in ("--settings", "--append-system-prompt-file", "--system-prompt-file", "--mcp-config"):
                if flag in arguments:
                    value = arguments[arguments.index(flag) + 1]
                    path = Path(value)
                    if path.is_file():
                        launch["files"][flag] = {"path": path, "text": path.read_text(encoding="utf-8")}
            self.launches.append(launch)
            process = FakeProcess(scenario, binary=not (kwargs.get("text") or kwargs.get("universal_newlines") or kwargs.get("encoding")),
                                  login="auth" in arguments and "login" in arguments)
            self.processes.append(process)
            return process

        return ClaudeBridge(self.runtime, executable=str(self.executable),
                            command_runner=self.auth_runner, process_factory=factory, **options)

    def run_bridge(self, bridge=None, prompt="写 3 条变体", **options):
        return (bridge or self.bridge()).run(prompt, cwd=str(self.runtime), on_progress=self.progress.append,
                                             cancel_event=self.cancel, on_event=self.events.append, **options)

    def test_subscription_client_is_available_without_api_key_or_sdk(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": ""}):
            bridge = self.bridge()
            state = bridge.status()
            self.assertTrue(state["available"])
            self.assertEqual(state["provider"], "claude")
            self.assertEqual(state["default_model"], "claude-default")
            self.assertIn("auth", self.auth_calls[-1][0])
            self.assertIn("status", self.auth_calls[-1][0])
            self.assertIn("--json", self.auth_calls[-1][0])
            models = bridge.models()["models"]
            default = next(model for model in models if model["is_default"])
            self.assertEqual(default["id"], "claude-default")
            self.assertEqual(self.run_bridge(bridge), "🔴Stop guessing the angle#knifetok")

    def test_auth_status_exposes_only_safe_account_fields(self):
        state = self.bridge().status()
        serialized = json.dumps(state)
        self.assertNotIn(SECRET, serialized)
        self.assertNotIn("private@example.invalid", serialized)
        self.assertNotIn("accessToken", serialized)
        self.assertNotIn("refreshToken", serialized)
        self.assertNotIn("key_hint", state)
        self.assertNotIn("api_key", state)

    def wait_for_login(self, bridge, condition=lambda state: True):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = bridge.status()
            if not state.get("login_pending") and condition(state):
                return state
            time.sleep(0.01)
        self.fail("The fake Claude login process did not finish")

    def wait_for_login_details(self, bridge, condition=lambda details: bool(details.get("url"))):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            details = bridge.login_details()
            if condition(details):
                return details
            time.sleep(0.01)
        self.fail("The fake Claude login details did not reach the expected state")

    def start_pending_login(self, bridge, stream="stdout", url=LOGIN_URL):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        self.assertTrue(bridge.start_login()["login_pending"])
        getattr(self.processes[-1], stream).put("Browser didn't open? Use this URL:\n" + url + "\n")
        return self.wait_for_login_details(bridge)

    def assert_login_secrets_absent(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        for secret in (SECRET, LOGIN_CODE, LOGIN_STATE):
            self.assertNotIn(secret, serialized)

    def login_callback(self, **parameters):
        return LOGIN_REDIRECT + "?" + urlencode({"code": LOGIN_CODE, "state": LOGIN_STATE, **parameters})

    def test_subscription_login_uses_official_client_without_exposing_credentials(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge()
        routing = {name: SECRET for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                                             "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN")}
        try:
            with patch.dict(os.environ, routing), patch.object(Path, "read_text", side_effect=AssertionError("private credentials must not be read")):
                state = bridge.start_login()
            self.assertTrue(state["login_pending"])
            self.assertFalse(state["available"])
            launch = self.launches[-1]
            self.assertEqual(launch["arguments"], [str(self.executable), "--safe-mode", "--restricted",
                                                  "auth", "login", "--claudeai"])
            options = launch["options"]
            self.assertFalse(options.get("shell", False))
            self.assertTrue(Path(options["cwd"]).is_dir())
            for stream in ("stdin", "stdout", "stderr"):
                self.assertEqual(options[stream], subprocess.PIPE)
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            for name in routing:
                self.assertNotIn(name, options["env"])
            self.assertNotIn(SECRET, json.dumps(state))
            self.assertNotIn("private@example.invalid", json.dumps(state))
            self.assertFalse((self.runtime / "secrets").exists())
        finally:
            if self.processes:
                self.processes[-1].exit()
                self.wait_for_login(bridge)

    def test_repeated_login_reuses_pending_official_client_process(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge()
        try:
            self.assertTrue(bridge.start_login()["login_pending"])
            self.assertTrue(bridge.start_login()["login_pending"])
            self.assertTrue(bridge.status(refresh=True)["login_pending"])
            self.assertEqual(len(self.launches), 1)
        finally:
            if self.processes:
                self.processes[-1].exit()
                self.wait_for_login(bridge)

    def test_official_login_url_is_recoverable_in_memory_without_entering_normal_status(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                bridge = self.bridge()
                try:
                    details = self.start_pending_login(bridge, stream=stream)
                    self.assertEqual(details["url"], LOGIN_URL)
                    self.assertTrue(details["pending"])
                    self.assertTrue(details["attempt_id"])
                    self.assertTrue(details["expires_at"])
                    self.assertFalse(details["callback_submitted"])
                    self.assert_login_secrets_absent(bridge.status())
                    self.assert_login_secrets_absent(bridge.models())
                    self.assertTrue(bridge.start_login()["login_pending"])
                    self.assertEqual(bridge.login_details()["attempt_id"], details["attempt_id"])
                    self.assertEqual(bridge.login_details()["url"], LOGIN_URL)
                    for file in self.runtime.rglob("*"):
                        if file.is_file():
                            contents = file.read_bytes()
                            for secret in (LOGIN_CODE, LOGIN_STATE):
                                self.assertNotIn(secret.encode(), contents)
                finally:
                    bridge.close()
                    self.wait_for_login(bridge)

    def test_untrusted_or_malformed_authorization_urls_are_not_exposed(self):
        bridge = self.bridge()
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge.start_login()
        try:
            for url in (LOGIN_URL.replace("https://claude.com/", "https://example.invalid/"),
                        LOGIN_URL.replace("https://claude.com/", "https://claude.com.example.invalid/"),
                        LOGIN_URL.replace("https://", "http://"),
                        "https://claude.ai/help?state=" + LOGIN_STATE):
                self.processes[-1].stdout.put(url + "\n")
            self.processes[-1].stdout.put(LOGIN_URL + "\n")
            details = self.wait_for_login_details(bridge)
            self.assertEqual(details["url"], LOGIN_URL)
            self.assert_login_secrets_absent(bridge.status())
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_cancel_pending_login_clears_link_and_allows_a_new_attempt(self):
        bridge = self.bridge()
        first = self.start_pending_login(bridge)
        first_process = self.processes[-1]
        state = bridge.cancel_login(first["attempt_id"])
        self.assertFalse(state["login_pending"])
        self.assertTrue(first_process.terminated or first_process.killed)
        self.assert_login_secrets_absent(state)
        self.assertIsNone(bridge.login_details()["url"])
        self.assertFalse(bridge.login_details()["pending"])
        try:
            second = self.start_pending_login(bridge)
            self.assertNotEqual(second["attempt_id"], first["attempt_id"])
            with self.assertRaises(BridgeError) as caught:
                bridge.cancel_login(first["attempt_id"])
            self.assert_login_secrets_absent(str(caught.exception))
            self.assertTrue(bridge.login_details()["pending"])
            self.assertEqual(bridge.login_details()["attempt_id"], second["attempt_id"])
            self.assertIsNone(self.processes[-1].poll())
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_full_callback_is_forwarded_to_the_existing_official_client_once(self):
        bridge = self.bridge(scenario=lambda process: None)
        details = self.start_pending_login(bridge)
        process = self.processes[-1]
        try:
            state = bridge.submit_login_callback(details["attempt_id"], self.login_callback())
            self.assertTrue(state["login_pending"])
            self.assert_login_secrets_absent(state)
            self.assertEqual(process.stdin.text, LOGIN_CODE + "#" + LOGIN_STATE + "\n")
            self.assertFalse(process.stdin.closed)
            self.assertTrue(bridge.login_details()["callback_submitted"])
            with self.assertRaises(BridgeError) as caught:
                bridge.submit_login_callback(details["attempt_id"], self.login_callback())
            self.assertEqual(getattr(caught.exception, "status", None), 409)
            self.assert_login_secrets_absent(str(caught.exception))
            self.assertEqual(process.stdin.text, LOGIN_CODE + "#" + LOGIN_STATE + "\n")
            self.auth = dict(AUTHENTICATED)
            process.exit()
            final = self.wait_for_login(bridge, lambda state: state["available"])
            self.assertTrue(final["available"])
            self.assert_login_secrets_absent(final)
            self.assertIsNone(bridge.login_details()["url"])
        finally:
            bridge.close()

    def test_manual_code_state_and_explicit_https_port_are_supported(self):
        values = [LOGIN_CODE + "#" + LOGIN_STATE,
                  self.login_callback().replace("platform.claude.com/", "platform.claude.com:443/")]
        for callback in values:
            with self.subTest(callback_type="code" if "://" not in callback else "https"):
                bridge = self.bridge(scenario=lambda process: None)
                try:
                    details = self.start_pending_login(bridge)
                    bridge.submit_login_callback(details["attempt_id"], callback)
                    self.assertEqual(self.processes[-1].stdin.text, LOGIN_CODE + "#" + LOGIN_STATE + "\n")
                    self.assertTrue(bridge.login_details()["callback_submitted"])
                finally:
                    bridge.close()
                    self.wait_for_login(bridge)

    def test_callback_requires_the_current_attempt_and_its_exact_state_and_redirect(self):
        bridge = self.bridge(scenario=lambda process: None)
        details = self.start_pending_login(bridge)
        process = self.processes[-1]
        invalid = [self.login_callback(state="wrong-state"), self.login_callback(code=""),
                   self.login_callback().replace("platform.claude.com", "example.invalid"),
                   self.login_callback().replace("platform.claude.com/", "platform.claude.com:444/"),
                   self.login_callback().replace("/callback?", "/other-path?"),
                   self.login_callback().replace("https://platform", "https://user:password@platform"),
                   self.login_callback().replace("https://", "http://"),
                   self.login_callback() + "&state=wrong-state", LOGIN_CODE + "#wrong-state",
                   LOGIN_CODE + "\n" + LOGIN_STATE]
        try:
            for callback in invalid:
                with self.subTest(callback_type=invalid.index(callback)):
                    with self.assertRaises(BridgeError) as caught:
                        bridge.submit_login_callback(details["attempt_id"], callback)
                    self.assertEqual(getattr(caught.exception, "status", None), 400)
                    self.assert_login_secrets_absent(str(caught.exception))
                    self.assertEqual(process.stdin.text, "")
                    self.assertFalse(bridge.login_details()["callback_submitted"])
            with self.assertRaises(BridgeError) as caught:
                bridge.submit_login_callback("stale-attempt", self.login_callback())
            self.assertEqual(getattr(caught.exception, "status", None), 409)
            self.assert_login_secrets_absent(str(caught.exception))
            self.assertEqual(process.stdin.text, "")
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_loopback_callback_goes_only_to_listener_owned_by_current_client(self):
        for hostname in ("localhost", "127.0.0.1"):
            with self.subTest(hostname=hostname):
                checked, sent = [], []

                def owner(process, port):
                    checked.append((process, port))
                    return port == 45678

                bridge = self.bridge(scenario=lambda process: None, callback_port_checker=owner,
                                     callback_sender=sent.append)
                try:
                    details = self.start_pending_login(bridge)
                    process = self.processes[-1]
                    local_callback = LOGIN_LOCAL_REDIRECT.replace("localhost", hostname) + "?" + urlencode({
                        "code": LOGIN_CODE, "state": LOGIN_STATE})
                    bridge.submit_login_callback(details["attempt_id"], local_callback)
                    self.wait_for_login_details(bridge, lambda value: bool(sent) and value["callback_submitted"])
                    self.assertEqual(checked, [(process, 45678)])
                    self.assertEqual(sent, [LOGIN_LOCAL_REDIRECT.replace("localhost", "127.0.0.1") + "?" + urlencode({
                        "code": LOGIN_CODE, "state": LOGIN_STATE})])
                    self.assertEqual(process.stdin.text, "")
                    self.assertTrue(bridge.login_details()["callback_submitted"])
                finally:
                    bridge.close()
                    self.wait_for_login(bridge)

    def test_arbitrary_local_port_and_wrong_state_never_receive_callback_secrets(self):
        checked, sent = [], []

        def owner(process, port):
            checked.append((process, port))
            return port == 45678

        bridge = self.bridge(scenario=lambda process: None, callback_port_checker=owner,
                             callback_sender=sent.append)
        details = self.start_pending_login(bridge)
        try:
            values = [LOGIN_LOCAL_REDIRECT.replace(":45678/", ":45679/") + "?" + urlencode({
                        "code": LOGIN_CODE, "state": LOGIN_STATE}),
                      LOGIN_LOCAL_REDIRECT + "?" + urlencode({"code": LOGIN_CODE, "state": "wrong-state"})]
            for value in values:
                with self.assertRaises(BridgeError) as caught:
                    bridge.submit_login_callback(details["attempt_id"], value)
                self.assertEqual(getattr(caught.exception, "status", None), 400)
                self.assert_login_secrets_absent(str(caught.exception))
            self.assertEqual(sent, [])
            self.assertEqual(self.processes[-1].stdin.text, "")
            self.assertFalse(bridge.login_details()["callback_submitted"])
            self.assertEqual([port for _, port in checked], [45679, 45678])
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_local_callback_delivery_failure_can_retry_without_exposing_secrets(self):
        sent = []
        bridge = self.bridge(scenario=lambda process: None, callback_port_checker=lambda process, port: port == 45678,
                             callback_sender=lambda url: (_ for _ in ()).throw(ConnectionRefusedError(SECRET + " " + LOGIN_CODE)))
        details = self.start_pending_login(bridge)
        local_callback = LOGIN_LOCAL_REDIRECT + "?" + urlencode({"code": LOGIN_CODE, "state": LOGIN_STATE})
        try:
            state = bridge.submit_login_callback(details["attempt_id"], local_callback)
            self.assertTrue(state["login_pending"])
            failure = self.wait_for_login_details(bridge, lambda value: bool(value.get("callback_error")))
            self.assert_login_secrets_absent(failure["callback_error"])
            self.assert_login_secrets_absent(state)
            self.assertFalse(failure["callback_submitted"])
            bridge.callback_sender = sent.append
            bridge.submit_login_callback(details["attempt_id"], local_callback)
            recovered = self.wait_for_login_details(bridge, lambda value: bool(sent) and value["callback_submitted"])
            self.assertEqual(len(sent), 1)
            self.assertIsNone(recovered.get("callback_error"))
            self.assertEqual(self.processes[-1].stdin.text, "")
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_slow_local_callback_keeps_status_and_cancel_available(self):
        sender_started, release_sender, sender_finished, submit_finished = (threading.Event() for _ in range(4))
        result = {}

        def slow_sender(url):
            sender_started.set()
            try:
                release_sender.wait(4)
            finally:
                sender_finished.set()

        bridge = self.bridge(scenario=lambda process: None, callback_port_checker=lambda process, port: port == 45678,
                             callback_sender=slow_sender)
        details = self.start_pending_login(bridge)
        local_callback = LOGIN_LOCAL_REDIRECT + "?" + urlencode({"code": LOGIN_CODE, "state": LOGIN_STATE})

        def submit():
            try:
                result["state"] = bridge.submit_login_callback(details["attempt_id"], local_callback)
            except Exception as error:
                result["error"] = error
            finally:
                submit_finished.set()

        worker = threading.Thread(target=submit, daemon=True)
        worker.start()
        try:
            self.assertTrue(sender_started.wait(1), "The local callback did not start")
            self.assertTrue(submit_finished.wait(1), "The callback request blocked on local delivery")
            self.assertNotIn("error", result)
            self.assertTrue(result["state"]["login_pending"])
            self.assertFalse(sender_finished.is_set())
            self.assertTrue(bridge.status()["login_pending"])
            self.assertTrue(bridge.login_details()["callback_submitted"])
            cancelled = bridge.cancel_login(details["attempt_id"])
            self.assertFalse(cancelled["login_pending"])
            self.assertFalse(bridge.login_details()["pending"])
            self.assertIsNone(bridge.login_details()["url"])
            self.assertIsNone(bridge.login_details().get("callback_error"))
            self.assertFalse(sender_finished.is_set())
        finally:
            release_sender.set()
            sender_finished.wait(1)
            worker.join(timeout=1)
            bridge.close()
            self.wait_for_login(bridge)

    def test_uncertain_local_callback_timeout_remains_reserved_and_cannot_be_replayed(self):
        for error in (TimeoutError(SECRET + " " + LOGIN_CODE),
                      urllib.error.URLError(TimeoutError(SECRET + " " + LOGIN_CODE))):
            with self.subTest(error_type=type(error).__name__):
                delivered = []

                def sender(url):
                    delivered.append(url)
                    raise error

                bridge = self.bridge(scenario=lambda process: None,
                    callback_port_checker=lambda process, port: port == 45678, callback_sender=sender)
                details = self.start_pending_login(bridge)
                local_callback = LOGIN_LOCAL_REDIRECT + "?" + urlencode({"code": LOGIN_CODE, "state": LOGIN_STATE})
                try:
                    state = bridge.submit_login_callback(details["attempt_id"], local_callback)
                    self.assert_login_secrets_absent(state)
                    pending = self.wait_for_login_details(bridge, lambda value: bool(value.get("callback_error")))
                    self.assertTrue(pending["callback_submitted"])
                    self.assertTrue(pending["pending"])
                    self.assert_login_secrets_absent(pending["callback_error"])
                    self.assert_login_secrets_absent(bridge.status())
                    with self.assertRaises(BridgeError) as caught:
                        bridge.submit_login_callback(details["attempt_id"], local_callback)
                    self.assertEqual(getattr(caught.exception, "status", None), 409)
                    self.assert_login_secrets_absent(str(caught.exception))
                    self.assertEqual(len(delivered), 1)
                    self.assertEqual(self.processes[-1].stdin.text, "")
                finally:
                    bridge.close()
                    self.wait_for_login(bridge)

    def test_callback_cannot_be_sent_before_the_current_authorization_url_is_ready(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge(scenario=lambda process: None)
        bridge.start_login()
        try:
            details = bridge.login_details()
            with self.assertRaises(BridgeError) as caught:
                bridge.submit_login_callback(details["attempt_id"], self.login_callback())
            self.assert_login_secrets_absent(str(caught.exception))
            self.assertEqual(self.processes[-1].stdin.text, "")
            self.assertFalse(bridge.login_details()["callback_submitted"])
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_callback_pipe_failure_is_safe_and_allows_retry_on_same_active_attempt(self):
        bridge = self.bridge(scenario=lambda process: None)
        details = self.start_pending_login(bridge)
        process = self.processes[-1]
        try:
            with patch.object(process.stdin, "write", side_effect=OSError(SECRET + " " + LOGIN_CODE)):
                with self.assertRaises(BridgeError) as caught:
                    bridge.submit_login_callback(details["attempt_id"], self.login_callback())
            self.assertEqual(getattr(caught.exception, "status", None), 503)
            self.assert_login_secrets_absent(str(caught.exception))
            self.assertFalse(bridge.login_details()["callback_submitted"])
            self.assertEqual(bridge.login_details()["attempt_id"], details["attempt_id"])
            bridge.submit_login_callback(details["attempt_id"], self.login_callback())
            self.assertTrue(bridge.login_details()["callback_submitted"])
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_login_completion_refreshes_cached_authentication_status(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge()
        self.assertFalse(bridge.status()["available"])
        self.assertTrue(bridge.start_login()["login_pending"])
        before = len(self.auth_calls)
        self.auth = dict(AUTHENTICATED)
        self.processes[-1].exit()
        state = self.wait_for_login(bridge, lambda value: value["available"])
        self.assertTrue(state["available"])
        self.assertGreater(len(self.auth_calls), before)
        self.assertNotIn(SECRET, json.dumps(state))

    def test_already_authenticated_client_does_not_open_another_login(self):
        state = self.bridge().start_login()
        self.assertTrue(state["available"])
        self.assertFalse(state["login_pending"])
        self.assertEqual(self.launches, [])

    def test_missing_client_and_launch_error_are_safe_login_states(self):
        bridge = self.bridge()
        self.executable.unlink()
        with self.assertRaises(BridgeUnavailable) as caught:
            bridge.start_login()
        self.assertNotIn(SECRET, str(caught.exception))
        state = bridge.status()
        self.assertFalse(state["available"])
        self.assertFalse(state["login_pending"])
        self.assertEqual(self.launches, [])
        self.executable.write_bytes(b"fake")
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge.process_factory = lambda *args, **kwargs: (_ for _ in ()).throw(OSError(SECRET))
        with self.assertRaises(BridgeUnavailable) as caught:
            bridge.start_login()
        self.assertNotIn(SECRET, str(caught.exception))
        state = bridge.status()
        self.assertFalse(state["login_pending"])
        self.assertNotIn(SECRET, json.dumps(state))

    def test_status_distinguishes_bundled_client_authentication_from_desktop_chat(self):
        bundled = self.runtime / "Claude" / "claude-code" / "version" / "platform" / "claude.exe"
        bundled.parent.mkdir(parents=True)
        bundled.write_bytes(b"fake")
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge()
        bridge.executable = str(bundled)
        with patch.object(Path, "read_text", side_effect=AssertionError("private credentials must not be read")):
            state = bridge.status()
        self.assertTrue(state["desktop_bundled"])
        self.assertFalse(state["available"])
        self.assertNotIn("桌面版尚未登录", state["error"])
        self.assertNotIn("桌面版未登录", state["error"])
        self.assertFalse(state["login_pending"])

    def test_login_timeout_stops_process_and_reports_a_safe_retriable_error(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge(login_timeout=0.03)
        self.assertTrue(bridge.start_login()["login_pending"])
        state = self.wait_for_login(bridge, lambda value: "超时" in value.get("error", ""))
        self.assertFalse(state["available"])
        self.assertIn("超时", state["error"])
        self.assertTrue(self.processes[-1].terminated or self.processes[-1].killed)
        self.assertNotIn(SECRET, json.dumps(state))
        self.assertFalse(bridge.login_details()["pending"])
        self.assertIsNone(bridge.login_details()["url"])

    def test_login_failure_discards_captured_link_and_redacts_process_diagnostics(self):
        bridge = self.bridge()
        self.start_pending_login(bridge)
        self.processes[-1].stderr.put("OAuth error " + LOGIN_STATE + " " + LOGIN_CODE + " " + SECRET + "\n")
        self.processes[-1].exit(1)
        state = self.wait_for_login(bridge)
        self.assertFalse(state["available"])
        self.assertFalse(state["login_pending"])
        self.assert_login_secrets_absent(state)
        self.assertIsNone(bridge.login_details()["url"])

    def test_expired_attempt_cannot_accept_a_callback_and_new_login_gets_a_new_id(self):
        bridge = self.bridge(scenario=lambda process: None, login_timeout=0.1)
        details = self.start_pending_login(bridge)
        self.wait_for_login(bridge, lambda state: "超时" in state.get("error", ""))
        self.assertFalse(bridge.login_details()["pending"])
        self.assertIsNone(bridge.login_details()["url"])
        with self.assertRaises(BridgeError) as caught:
            bridge.submit_login_callback(details["attempt_id"], self.login_callback())
        self.assertEqual(getattr(caught.exception, "status", None), 409)
        self.assert_login_secrets_absent(str(caught.exception))
        self.assertEqual(self.processes[-1].stdin.text, "")
        try:
            retried = self.start_pending_login(bridge)
            self.assertNotEqual(retried["attempt_id"], details["attempt_id"])
        finally:
            bridge.close()
            self.wait_for_login(bridge)

    def test_closing_bridge_stops_only_its_pending_login_process(self):
        self.auth = {"loggedIn": False, "authMethod": "none"}
        bridge = self.bridge()
        self.assertTrue(bridge.start_login()["login_pending"])
        bridge.close()
        self.assertTrue(self.processes[-1].terminated or self.processes[-1].killed)
        self.assertFalse(self.wait_for_login(bridge)["login_pending"])

    def test_signed_out_or_api_authenticated_client_cannot_start_writing(self):
        for auth in ({"loggedIn": False}, {"loggedIn": True, "authMethod": "api_key"},
                     {"loggedIn": True, "authMethod": "bedrock"}):
            with self.subTest(auth=auth):
                self.auth = auth
                bridge = self.bridge()
                self.assertFalse(bridge.status()["available"])
                with self.assertRaises(BridgeUnavailable):
                    self.run_bridge(bridge)
                self.assertEqual(self.launches, [])

    def test_missing_or_failed_client_check_is_a_safe_unavailable_state(self):
        bridge = self.bridge()
        self.executable.unlink()
        self.assertFalse(bridge.status(refresh=True)["available"])
        self.executable.write_bytes(b"fake")
        bridge.command_runner = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, SECRET, SECRET)
        state = bridge.status(refresh=True)
        self.assertFalse(state["available"])
        self.assertNotIn(SECRET, json.dumps(state))

    def test_streaming_progress_and_answer_finish_only_after_success(self):
        output = self.run_bridge()
        self.assertEqual(output, "🔴Stop guessing the angle#knifetok")
        messages = [event for event in self.events if event["type"] == "message"]
        self.assertTrue(any(event["phase"] == "commentary" and event["text"] == "先看方向和评论。" for event in messages))
        self.assertTrue(any(event["phase"] == "final_answer" and not event.get("complete") for event in messages))
        final = [event for event in messages if event["phase"] == "final_answer"][-1]
        self.assertEqual(final["text"], output)
        self.assertTrue(final["complete"])
        self.assertTrue(any(event["type"] == "session" and event.get("model") == "claude-client-default-model" for event in self.events))
        self.assertTrue(any("Claude" in text for text in self.progress))

    def test_default_preserves_client_model_and_large_prompt_uses_stdin(self):
        prompt = "资料很长。" * 30000
        self.run_bridge(prompt=prompt)
        launch = self.launches[-1]
        argv = launch["arguments"]
        self.assertNotIn("--model", argv)
        self.assertNotIn("--effort", argv)
        self.assertNotIn(prompt, argv)
        self.assertLess(sum(len(argument) for argument in argv), 20000)
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--include-partial-messages", argv)
        message = self.processes[-1].received[0]
        self.assertEqual(message["type"], "user")
        self.assertIn(prompt, json.dumps(message, ensure_ascii=False))

    def test_model_aliases_full_ids_and_effort_are_forwarded_without_fallback(self):
        for requested, expected in (("claude-default", None), ("claude-opus", "opus"),
                                    ("claude-sonnet", "sonnet"), ("claude-haiku", "haiku"),
                                    ("claude-opus-5-5", "claude-opus-5-5")):
            with self.subTest(model=requested):
                self.run_bridge(model=requested, reasoning_effort="low")
                argv = self.launches[-1]["arguments"]
                if expected is None:
                    self.assertNotIn("--model", argv)
                else:
                    self.assertEqual(argv[argv.index("--model") + 1], expected)
                self.assertEqual(argv[argv.index("--effort") + 1], "low")
                self.assertNotIn("--fallback-model", argv)

    def test_keys_tokens_and_provider_routing_are_removed_only_in_child_environment(self):
        routing = {name: SECRET for name in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        )}
        legacy_key = self.runtime / "secrets" / "anthropic.json"
        legacy_key.parent.mkdir()
        legacy_key.write_text(json.dumps({"api_key": SECRET}), encoding="utf-8")
        with patch.dict(os.environ, routing):
            self.run_bridge()
            for _, options in self.auth_calls:
                for name in routing:
                    self.assertNotIn(name, options["env"])
            for name in routing:
                self.assertNotIn(name, self.launches[-1]["options"]["env"])
                self.assertEqual(os.environ[name], SECRET)
        self.assertNotIn(SECRET, json.dumps(self.events, ensure_ascii=False))
        self.assertNotIn(SECRET, "".join(self.progress))

    def test_isolated_read_only_client_launch_and_temporary_files_are_cleaned_up(self):
        self.run_bridge()
        launch = self.launches[-1]
        argv, options = launch["arguments"], launch["options"]
        self.assertFalse(options.get("shell", False))
        self.assertNotEqual(Path(options["cwd"]), self.runtime)
        self.assertFalse(Path(options["cwd"]).exists())
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        self.assertIn("--no-session-persistence", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--safe-mode", argv)
        self.assertIn("--restricted", argv)
        self.assertNotIn("--bare", argv)
        self.assertIn("--settings", launch["files"])
        settings = json.loads(launch["files"]["--settings"]["text"])
        self.assertTrue(settings.get("disableAllHooks"))
        for file in launch["files"].values():
            self.assertFalse(file["path"].exists())

    def test_client_defaults_copy_model_and_effort_but_exclude_hooks_and_provider_env(self):
        client = self.runtime / "client-settings"
        client.mkdir()
        (client / "settings.json").write_text(json.dumps({
            "model": "claude-sonnet-5-5", "effortLevel": "low",
            "modelSettings": {"claude-client-default-model": {"effortLevel": "medium", "token": SECRET}},
            "hooks": {"PreToolUse": SECRET}, "env": {"ANTHROPIC_API_KEY": SECRET},
        }), encoding="utf-8")
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(client)}):
            self.run_bridge()
        settings_text = self.launches[-1]["files"]["--settings"]["text"]
        settings = json.loads(settings_text)
        self.assertEqual(settings["model"], "claude-sonnet-5-5")
        self.assertEqual(settings["effortLevel"], "low")
        self.assertEqual(settings["modelSettings"], {"claude-client-default-model": {"effortLevel": "medium"}})
        self.assertNotIn("hooks", settings)
        self.assertNotIn("env", settings)
        self.assertNotIn(SECRET, settings_text)
        self.assertTrue(any(event.get("reasoning_effort") == "medium" for event in self.events if event["type"] == "session"))

    def test_skill_rules_and_original_video_frames_are_delivered(self):
        skill = self.runtime / "skill" / "SKILL.md"
        skill.parent.mkdir()
        skill.write_text("---\nname: script-writing\n---\n每个脚本 3 组标题行。", encoding="utf-8")
        selected = [{"name": "script-writing", "path": str(skill)}]
        bridge = self.bridge(frame_extractor=lambda path, cancel: [("画面 1", "aGVsbG8=")])
        self.run_bridge(bridge, skills=selected, require_image_view=True, video_path="original.mp4")
        system_text = "\n".join(file["text"] for flag, file in self.launches[-1]["files"].items() if "system-prompt" in flag)
        self.assertIn("每个脚本 3 组标题行", system_text)
        message = self.processes[-1].received[0]["message"]
        images = [block for block in message["content"] if block["type"] == "image"]
        self.assertEqual(images[0]["source"], {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="})
        self.assertIn({"type": "skills_submitted", "skills": selected}, self.events)

    def test_missing_skill_or_video_frames_stop_before_launch(self):
        with self.assertRaises(BridgeError):
            self.run_bridge(skills=[{"name": "missing", "path": str(self.runtime / "missing" / "SKILL.md")}])
        with self.assertRaisesRegex(BridgeError, "画面"):
            self.run_bridge(self.bridge(frame_extractor=lambda path, cancel: []),
                            require_image_view=True, video_path="missing.mp4")
        self.assertEqual(self.launches, [])

    def test_assistant_and_result_do_not_duplicate_streamed_answer(self):
        def scenario(process):
            stream_block(process, 0, "text", "partial draft")
            process.emit({"type": "assistant", "message": {"id": "msg", "content": [{"type": "text", "text": "Complete final text"}]}})
            process.emit({"type": "result", "subtype": "success", "is_error": False, "result": "Complete final text"})
            process.exit()
        self.assertEqual(self.run_bridge(self.bridge(scenario)), "Complete final text")

    def test_cli_emits_each_assistant_block_before_its_stream_stop(self):
        thinking, answer = "先核对案例里的真实需求。", "One complete script."

        def scenario(process):
            def stream(event):
                process.emit({"type": "stream_event", "event": event})

            stream({"type": "message_start", "message": {"id": "msg-blocks", "content": []}})
            stream({"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}})
            stream({"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": thinking}})
            process.emit({"type": "assistant", "message": {"id": "msg-blocks", "content": [{"type": "thinking", "thinking": thinking}]}})
            stream({"type": "content_block_stop", "index": 0})
            stream({"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}})
            stream({"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": answer}})
            process.emit({"type": "assistant", "message": {"id": "msg-blocks", "content": [{"type": "text", "text": answer}]}})
            stream({"type": "content_block_stop", "index": 1})
            stream({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
            process.emit({"type": "result", "subtype": "success", "is_error": False, "result": answer})
            process.exit()

        self.assertEqual(self.run_bridge(self.bridge(scenario)), answer)
        # The workbench updates messages by item_id; inspect their final state.
        messages = {event["item_id"]: event for event in self.events if event["type"] == "message"}
        commentary = [event["text"] for event in messages.values() if event["phase"] == "commentary"]
        answers = [event["text"] for event in messages.values() if event["phase"] == "final_answer"]
        self.assertEqual(commentary, [thinking])
        self.assertEqual(answers, [answer])

    def test_stream_stop_reason_rejects_truncation_or_refusal_even_with_success_result(self):
        def scenario_for(reason):
            def scenario(process):
                stream_block(process, 0, "text", "unfinished draft")
                process.emit({"type": "stream_event", "event": {
                    "type": "message_delta", "delta": {"stop_reason": reason},
                }})
                process.emit({"type": "result", "subtype": "success", "is_error": False, "result": "unfinished draft"})
                process.exit()
            return scenario

        for reason in ("max_tokens", "refusal"):
            with self.subTest(reason=reason):
                self.events.clear()
                self.progress.clear()
                with self.assertRaises(BridgeError):
                    self.run_bridge(self.bridge(scenario_for(reason)))
                self.assertNotIn("Claude 已完成回答。", self.progress)

    def test_explicit_effort_overrides_child_env_and_default_inherits_env_before_model_settings(self):
        client = self.runtime / "client-effort-settings"
        client.mkdir()
        (client / "settings.json").write_text(json.dumps({
            "model": "claude-client-default-model", "effortLevel": "low",
            "modelSettings": {"claude-client-default-model": {"effortLevel": "medium"}},
        }), encoding="utf-8")

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(client), "CLAUDE_CODE_EFFORT_LEVEL": "high"}):
            self.run_bridge(reasoning_effort="low")
            self.assertEqual(self.launches[-1]["options"]["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "low")
            sessions = [event for event in self.events if event["type"] == "session"]
            self.assertTrue(sessions)
            self.assertTrue(all(event["reasoning_effort"] == "low" for event in sessions))
            self.assertEqual(os.environ["CLAUDE_CODE_EFFORT_LEVEL"], "high")

            self.events.clear()
            self.run_bridge()
            self.assertEqual(self.launches[-1]["options"]["env"]["CLAUDE_CODE_EFFORT_LEVEL"], "high")
            self.assertNotIn("--effort", self.launches[-1]["arguments"])
            sessions = [event for event in self.events if event["type"] == "session"]
            self.assertTrue(sessions)
            self.assertTrue(all(event["reasoning_effort"] == "high" for event in sessions))
            self.assertEqual(os.environ["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_error_missing_result_or_nonzero_exit_never_complete_a_candidate(self):
        def scenario_for(mode):
            def scenario(process):
                stream_block(process, 0, "text", "unfinished draft")
                process.emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "unfinished draft"}]}})
                if mode == "error":
                    process.emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "errors": [SECRET]})
                elif mode == "nonzero":
                    process.emit({"type": "result", "subtype": "success", "is_error": False, "result": "unfinished draft"})
                process.stderr.put(SECRET + "\n")
                process.exit(1 if mode == "nonzero" else 0)
            return scenario
        for mode in ("error", "missing", "nonzero"):
            with self.subTest(mode=mode):
                self.events.clear()
                with self.assertRaises(BridgeError) as caught:
                    self.run_bridge(self.bridge(scenario_for(mode)))
                self.assertNotIn(SECRET, str(caught.exception))
                self.assertNotIn("Claude 已完成回答。", self.progress)

    def test_cancellation_before_launch_and_during_a_silent_process(self):
        self.cancel.set()
        with self.assertRaises(BridgeCancelled):
            self.run_bridge()
        self.assertEqual(self.launches, [])
        self.cancel.clear()

        def silent(process):
            self.cancel.set()

        with self.assertRaises(BridgeCancelled):
            self.run_bridge(self.bridge(silent))
        self.assertTrue(self.processes[-1].terminated or self.processes[-1].killed)
        self.assertFalse(any(event.get("complete") for event in self.events))

    def test_timeout_stops_a_silent_process_without_saving_partial_output(self):
        def silent(process):
            stream_block(process, 0, "text", "partial")
        with self.assertRaisesRegex(BridgeError, "超时"):
            self.run_bridge(self.bridge(silent, timeout=0.05, startup_timeout=0.05))
        self.assertTrue(self.processes[-1].terminated or self.processes[-1].killed)
        self.assertNotIn("Claude 已完成回答。", self.progress)


class ModelRouterTests(unittest.TestCase):
    def test_router_preserves_dispatch_default_fallback_and_video_contract(self):
        calls = []

        class Fake:
            def __init__(self, name, available=True):
                self.name, self.available = name, available

            def status(self):
                return {"available": self.available, "label": self.name}

            def models(self):
                return {"models": [{"id": self.name + "-m"}]}

            def run(self, prompt, **options):
                calls.append((self.name, options))
                return self.name

        router = ModelRouter(Fake("codex"), Fake("claude"))
        for model in ("claude-default", "claude-opus", "claude-opus-5-5"):
            self.assertEqual(router.run("p", model=model, video_path="v"), "claude")
            self.assertEqual(calls[-1][1]["video_path"], "v")
        self.assertEqual(router.run("p", model="gpt-5", video_path="v"), "codex")
        self.assertNotIn("video_path", calls[-1][1])
        self.assertEqual(router.run("p", model=None), "codex")
        router.codex.available = False
        self.assertEqual(router.run("p", model=None), "claude")
        self.assertEqual(router.models()["models"][0]["provider"], "codex")
        self.assertTrue(router.status()["available"])


if __name__ == "__main__":
    unittest.main()
