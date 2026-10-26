"""Isolated ownership, fail-closed configuration and restart checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

spec = importlib.util.spec_from_file_location("remote_supervisor_under_test", Path(__file__).with_name("remote_supervisor.py"))
supervisor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = supervisor
spec.loader.exec_module(supervisor)


class FakeOutput:
    def __init__(self):
        self.lines = queue.Queue()
        self.lines.put(b"Protected workbench proxy listening on 127.0.0.1:8768 for workbench.example.com\n")

    def readline(self, limit):
        return self.lines.get()

    def close(self):
        self.lines.put(b"")


class FakeProcess:
    sequence = 100

    def __init__(self):
        type(self).sequence += 1
        self.pid = self.sequence
        self.returncode = None
        self.terminated = False
        self.stdout = FakeOutput()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -1
        self.stdout.close()

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return self.returncode


class RemoteSupervisorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workbench-remote-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime" / "remote-access"
        self.runtime.mkdir(parents=True)
        (self.root / "deployment").mkdir()
        (self.root / "deployment" / "remote_proxy.mjs").write_text("// fixture", encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps({"runtime_root": str(self.runtime.parent)}), encoding="utf-8")
        self.config = {"hostname": "workbench.example.com", "teamDomain": "https://fixture.cloudflareaccess.com",
                       "audience": "a" * 64, "allowedEmails": ["fixture@example.com"],
                       "nodePath": sys.executable, "cloudflaredPath": sys.executable,
                       "httpsProxy": "http://127.0.0.1:7897"}
        (self.runtime / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        (self.runtime / "tunnel-token.txt").write_text("fixture-token", encoding="utf-8")
        self.settings = supervisor.load_settings(self.root)

    def files(self):
        return {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def password_config(self):
        self.config["authMode"] = "password"
        for key in ("teamDomain", "audience", "allowedEmails"):
            self.config.pop(key, None)
        account = {"version": 1, "username": "fixture-owner", "salt": "01" * 32,
                   "password_hash": "02" * 64, "scrypt": {"n": 32768, "r": 8, "p": 3},
                   "credential_id": "03" * 16, "updated_at": "2026-10-16T00:00:00Z"}
        supervisor.atomic_json(self.runtime / "account.json", account)
        supervisor.atomic_json(self.runtime / "config.json", self.config)
        return account

    def guest_config(self, *, account=False):
        value = self.password_config()
        if not account:
            (self.runtime / "account.json").unlink()
        (self.root / "deployment" / "guest_snapshot.py").write_text("# fixture", encoding="utf-8")
        self.config["guestEnabled"] = True
        supervisor.atomic_json(self.runtime / "config.json", self.config)
        return value

    def test_guest_requires_explicit_boolean_and_password_mode(self):
        self.assertFalse(self.settings.guest_enabled)
        self.assertNotIn("guest_snapshot", self.settings.commands())
        for value in (None, "true", 1, 0, [], {}):
            supervisor.atomic_json(self.runtime / "config.json", {**self.config, "guestEnabled": value})
            with self.assertRaises(supervisor.ConfigurationError):
                supervisor.load_settings(self.root)
        supervisor.atomic_json(self.runtime / "config.json", {**self.config, "guestEnabled": True})
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)

    def test_guest_can_start_without_account_but_existing_bad_account_remains_invalid(self):
        account = self.guest_config()
        before = self.files()
        settings = supervisor.load_settings(self.root)
        self.assertTrue(settings.guest_enabled)
        self.assertEqual(settings.auth_mode, "password")
        self.assertEqual(before, self.files())
        for raw in ("", "{invalid", json.dumps({**account, "scrypt": {"n": 2, "r": 8, "p": 3}})):
            (self.runtime / "account.json").write_text(raw, encoding="utf-8")
            with self.assertRaises(supervisor.ConfigurationError):
                supervisor.load_settings(self.root)
        (self.runtime / "account.json").unlink()
        (self.runtime / "account.json").mkdir()
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)
        (self.runtime / "account.json").rmdir()
        (self.root / "deployment" / "guest_snapshot.py").unlink()
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)

    def test_guest_command_uses_only_fixed_interpreter_and_project_script(self):
        self.guest_config()
        settings = supervisor.load_settings(self.root)
        command = settings.commands()["guest_snapshot"]
        expected = Path(sys.executable)
        if os.name == "nt" and expected.with_name("pythonw.exe").is_file():
            expected = expected.with_name("pythonw.exe")
        self.assertEqual(command, [str(expected), "-B", str(self.root / "deployment" / "guest_snapshot.py")])
        self.assertEqual(settings.child_kinds(), ("guest_snapshot", "proxy", "tunnel"))
        self.assertNotIn("--once", command, "the owned child must keep refreshing snapshots")
        self.assertNotIn("fixture-token", command)
        self.assertNotIn(str(settings.token), command)

    def test_guest_missing_account_run_starts_three_owned_children_and_joins_job(self):
        self.guest_config()
        settings = supervisor.load_settings(self.root)
        created, joined, commands = [], [], []
        unrelated = FakeProcess()

        class Job:
            def add(self, process):
                joined.append(process)

            def close(self):
                pass

        def spawn(command, **kwargs):
            process = FakeProcess()
            created.append(process)
            commands.append(command)
            if command == settings.commands()["guest_snapshot"]:
                self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["cwd"], self.root)
            return process

        service = supervisor.Supervisor(settings, spawn=spawn, job=Job())
        with patch.object(service.wakeup, "wait", side_effect=lambda _timeout: service.stop_event.set()):
            service.run()
        self.assertEqual(commands, [settings.commands()[kind] for kind in settings.child_kinds()])
        self.assertEqual(joined, created)
        self.assertEqual(len(created), 3)
        self.assertTrue(all(process.terminated for process in created))
        self.assertFalse(unrelated.terminated)
        self.assertEqual(service.state, "stopped")
        self.assertTrue(service.status()["guest_enabled"])

    def test_guest_restart_backoff_preserves_authenticated_proxy_and_tunnel(self):
        self.guest_config()
        settings = supervisor.load_settings(self.root)
        now, created = [0.0], []

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(settings, clock=lambda: now[0], spawn=spawn)
        self.addCleanup(service.stop_children)
        service.tick()
        guest, proxy, tunnel = created
        guest.returncode = 9
        service.tick()
        self.assertEqual(service.children["guest_snapshot"]["retry_at"], 1)
        self.assertEqual(service.state, "retry_wait")
        now[0] = 0.5
        service.tick()
        self.assertEqual(len(created), 3)
        now[0] = 1
        service.tick()
        self.assertEqual(len(created), 4)
        self.assertIs(service.children["proxy"]["process"], proxy)
        self.assertIs(service.children["tunnel"]["process"], tunnel)
        self.assertFalse(proxy.terminated)
        self.assertFalse(tunnel.terminated)
        self.assertTrue(service.children["proxy"]["readiness"]["ready"].is_set())
        self.assertEqual(service.state, "children_running")

    def test_guest_running_cannot_bypass_proxy_readiness_to_start_tunnel(self):
        self.guest_config()
        settings = supervisor.load_settings(self.root)
        launched = []

        def spawn(command, **kwargs):
            self.assertNotEqual(command, settings.commands()["tunnel"])
            process = FakeProcess()
            launched.append(command)
            if command == settings.commands()["proxy"]:
                process.returncode = 98
                process.stdout.lines.get_nowait()
                process.stdout.close()
            return process

        service = supervisor.Supervisor(settings, spawn=spawn)
        self.addCleanup(service.stop_children)
        service.tick()
        self.assertEqual(launched, [settings.commands()["guest_snapshot"], settings.commands()["proxy"]])
        self.assertIsNotNone(service.children["guest_snapshot"]["process"])
        self.assertIsNone(service.children["proxy"]["process"])
        self.assertIsNone(service.children["tunnel"]["process"])

    def test_reload_can_add_and_remove_owned_guest_child(self):
        created = []
        unrelated = FakeProcess()

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(self.settings, spawn=spawn)
        self.addCleanup(service.stop_children)
        service.tick()
        initial = list(created)
        self.guest_config(account=True)
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": uuid4().hex})
        self.assertTrue(service.reload_if_requested())
        self.assertTrue(all(process.terminated for process in initial))
        service.tick()
        self.assertEqual(len(created), 5)
        guest_generation = created[2:]
        self.assertEqual(set(service.children), {"guest_snapshot", "proxy", "tunnel"})
        self.assertTrue(service.status()["guest_enabled"])
        self.config["guestEnabled"] = False
        supervisor.atomic_json(self.runtime / "config.json", self.config)
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": uuid4().hex})
        self.assertTrue(service.reload_if_requested())
        self.assertTrue(all(process.terminated for process in guest_generation))
        self.assertEqual(set(service.children), {"proxy", "tunnel"})
        service.tick()
        self.assertEqual(len(created), 7)
        self.assertFalse(service.status()["guest_enabled"])
        self.assertFalse(unrelated.terminated)

    def test_password_mode_requires_fixed_account_but_no_cloudflare_identity(self):
        account = self.password_config()
        before = self.files()
        settings = supervisor.load_settings(self.root)
        self.assertEqual(settings.auth_mode, "password")
        self.assertEqual(before, self.files(), "configuration validation is read-only")
        for changes in ({"version": True}, {"username": "\r\n"}, {"salt": "short"},
                        {"password_hash": "invalid"}, {"credential_id": "invalid"},
                        {"scrypt": {"n": 2, "r": 8, "p": 3}}, {"updated_at": None}):
            with self.subTest(changes=changes):
                supervisor.atomic_json(self.runtime / "account.json", {**account, **changes})
                with self.assertRaises(supervisor.ConfigurationError):
                    supervisor.load_settings(self.root)
        for raw in ("{invalid", "x" * 8193):
            (self.runtime / "account.json").write_text(raw, encoding="utf-8")
            with self.assertRaises(supervisor.ConfigurationError):
                supervisor.load_settings(self.root)
        (self.runtime / "account.json").unlink()
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)

    def test_auth_mode_defaults_to_cloudflare_and_rejects_unknown_modes(self):
        self.assertEqual(self.settings.auth_mode, "cloudflare")
        for value in ("", "none", None, []):
            supervisor.atomic_json(self.runtime / "config.json", {**self.config, "authMode": value})
            with self.assertRaises(supervisor.ConfigurationError):
                supervisor.load_settings(self.root)

    def test_reload_applies_password_mode_once_and_only_restarts_owned_children(self):
        created = []
        unrelated = FakeProcess()

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(self.settings, spawn=spawn)
        self.addCleanup(service.stop_children)
        service.tick()
        self.password_config()
        request_id = uuid4().hex
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": request_id})
        for child in service.children.values():
            child.update(failures=6, retry_at=999999999)
        self.assertTrue(service.reload_if_requested())
        self.assertEqual(service.settings.auth_mode, "password")
        self.assertTrue(all(process.terminated for process in created))
        self.assertFalse(unrelated.terminated)
        self.assertTrue(all(child["failures"] == 0 and child["retry_at"] == 0 for child in service.children.values()))
        service.tick()
        self.assertEqual(len(created), 4)
        self.assertEqual(service.status()["auth_mode"], "password")
        self.assertEqual(service.status()["last_reload_request_id"], request_id)
        self.assertIsNone(service.reload_if_requested())
        self.assertTrue(all(not process.terminated for process in created[2:]))
        second_id = uuid4().hex
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": second_id})
        self.assertTrue(service.reload_if_requested())
        service.tick()
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": request_id})
        self.assertIsNone(service.reload_if_requested(), "replaying an older completed request cannot restart the service")
        self.assertEqual(len(created), 6)

    def test_reload_ignores_stale_instance_and_malformed_requests(self):
        service = supervisor.Supervisor(self.settings, spawn=lambda *args, **kwargs: FakeProcess())
        self.addCleanup(service.stop_children)
        service.tick()
        owned = [child["process"] for child in service.children.values()]
        for request in ({"instance_id": uuid4().hex, "request_id": uuid4().hex},
                        {"instance_id": service.instance, "request_id": "not-a-request-id"},
                        {"instance_id": service.instance}, [service.instance]):
            supervisor.atomic_json(self.runtime / "reload.request", request)
            self.assertIsNone(service.reload_if_requested())
        for raw in ("{invalid", "x" * 2049):
            (self.runtime / "reload.request").write_text(raw, encoding="utf-8")
            self.assertIsNone(service.reload_if_requested())
        self.assertTrue(all(not process.terminated for process in owned))
        self.assertIsNone(service.last_reload_request_id)

    def test_invalid_reload_stops_owned_children_and_logs_no_credential_values(self):
        service = supervisor.Supervisor(self.settings, spawn=lambda *args, **kwargs: FakeProcess())
        service.tick()
        owned = [child["process"] for child in service.children.values()]
        unrelated = FakeProcess()
        self.password_config()
        (self.runtime / "account.json").write_text("private-fixture-password", encoding="utf-8")
        supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": uuid4().hex})
        with self.assertLogs(service.logger, level="ERROR") as captured:
            self.assertFalse(service.reload_if_requested())
        self.assertTrue(all(process.terminated for process in owned))
        self.assertFalse(unrelated.terminated)
        self.assertEqual(service.state, "configuration_error")
        self.assertTrue(service.should_stop())
        self.assertNotIn("private-fixture-password", "\n".join(captured.output))
        self.assertIn("reload_configuration_invalid", "\n".join(captured.output))

    def test_run_loop_applies_reload_and_continues_with_new_owned_children(self):
        created = []

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(self.settings, spawn=spawn)

        def wait(_timeout):
            if len(created) == 2:
                self.password_config()
                supervisor.atomic_json(self.runtime / "reload.request", {"instance_id": service.instance, "request_id": uuid4().hex})
            else:
                service.stop_event.set()

        with patch.object(service.wakeup, "wait", side_effect=wait):
            service.run()
        self.assertEqual(len(created), 4)
        self.assertEqual(service.settings.auth_mode, "password")
        self.assertEqual(service.state, "stopped")
        self.assertTrue(all(process.terminated for process in created))

    def test_missing_password_account_stops_before_spawning_any_child(self):
        self.password_config()
        settings = supervisor.load_settings(self.root)
        (self.runtime / "account.json").unlink()
        with patch.object(supervisor.subprocess, "Popen") as spawn:
            service = supervisor.Supervisor(settings, spawn=spawn)
            service.run()
        spawn.assert_not_called()
        self.assertEqual(service.state, "configuration_error")

    def test_validation_is_read_only_and_rejects_missing_token_and_untrusted_proxy(self):
        before = self.files()
        supervisor.load_settings(self.root)
        self.assertEqual(before, self.files())
        self.config["httpsProxy"] = "http://user:password@outside.example:7897"
        (self.runtime / "config.json").write_text(json.dumps(self.config), encoding="utf-8")
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)
        (self.runtime / "tunnel-token.txt").unlink()
        with self.assertRaises(supervisor.ConfigurationError):
            supervisor.load_settings(self.root)

    def test_connector_has_own_config_and_token_file_and_no_inherited_tunnel_identity(self):
        command = self.settings.commands()["tunnel"]
        self.assertIn("--no-autoupdate", command)
        self.assertEqual(command[command.index("--protocol") + 1], "http2")
        self.assertEqual(command[command.index("--token-file") + 1], str(self.settings.token))
        self.assertEqual(command[command.index("--config") + 1], str(self.runtime / "cloudflared.yml"))
        self.assertNotIn("fixture-token", command)
        with patch.dict(os.environ, {"TUNNEL_TOKEN": "another-app-token", "TUNNEL_CONFIG": "another-app.yml",
                                     "ALL_PROXY": "http://outside.example:1", "HTTPS_PROXY": "http://outside.example:2"}):
            connector = self.settings.environment("tunnel")
            proxy = self.settings.environment("proxy")
        self.assertNotIn("TUNNEL_TOKEN", connector)
        self.assertNotIn("TUNNEL_CONFIG", connector)
        self.assertNotIn("HTTPS_PROXY", connector)
        self.assertNotIn("ALL_PROXY", connector)
        self.assertEqual(proxy["HTTPS_PROXY"], self.config["httpsProxy"])
        self.assertEqual(proxy["NODE_USE_ENV_PROXY"], "1")
        self.assertIn("127.0.0.1", proxy["NO_PROXY"])

    def test_restart_backoff_and_stop_touch_only_owned_children(self):
        now, created = [0.0], []
        unrelated = FakeProcess()

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(self.settings, clock=lambda: now[0], spawn=spawn)
        service.tick()
        self.assertEqual(len(created), 2)
        service.children["tunnel"]["process"].returncode = 9
        service.tick()
        self.assertEqual(service.children["tunnel"]["retry_at"], 1)
        now[0] = 0.5
        service.tick()
        self.assertEqual(len(created), 2)
        now[0] = 1
        service.tick()
        self.assertEqual(len(created), 3)
        service.children["tunnel"]["process"].returncode = 9
        service.tick()
        self.assertEqual(service.children["tunnel"]["retry_at"], 3)
        service.stop_children()
        self.assertTrue(created[0].terminated)
        self.assertFalse(unrelated.terminated)
        self.assertTrue(all(child["process"] is None for child in service.children.values()))

    def test_instance_stop_request_and_process_lock_prevent_cross_instance_stop(self):
        service = supervisor.Supervisor(self.settings)
        supervisor.atomic_json(self.runtime / "stop.request", {"instance_id": "previous-instance"})
        self.assertFalse(service.should_stop())
        supervisor.atomic_json(self.runtime / "stop.request", {"instance_id": service.instance})
        self.assertTrue(service.should_stop())
        lock = supervisor.ProcessLock(self.runtime / "supervisor.lock").acquire()
        try:
            with self.assertRaises(supervisor.AlreadyRunning):
                supervisor.ProcessLock(lock.path).acquire()
        finally:
            lock.close()
        second = supervisor.ProcessLock(lock.path).acquire()
        second.close()

    def test_transient_status_write_failure_preserves_children_and_recovers(self):
        created = []

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        service = supervisor.Supervisor(self.settings, spawn=spawn)
        self.addCleanup(service.stop_children)
        service.tick()
        with self.assertLogs(service.logger, level="INFO") as captured:
            with patch.object(supervisor, "atomic_json", side_effect=PermissionError("fixture-secret")):
                service.tick()
                service.tick()
            self.assertEqual(len(created), 2)
            self.assertTrue(all(process.poll() is None for process in created))
            self.assertEqual(service.state, "children_running")
            service.tick()
        messages = "\n".join(captured.output)
        self.assertEqual(messages.count("status_write_failed"), 1)
        self.assertIn("exception=PermissionError", messages)
        self.assertIn("status_write_recovered", messages)
        self.assertNotIn("fixture-secret", messages)
        status = json.loads((self.runtime / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["children"]["tunnel"]["pid"], created[1].pid)

    def test_failed_run_reclaims_children_even_when_final_status_cannot_be_written(self):
        service = supervisor.Supervisor(self.settings, spawn=lambda *args, **kwargs: FakeProcess())
        service.tick()
        owned = [child["process"] for child in service.children.values()]
        with self.assertLogs(service.logger, level="INFO") as captured:
            with patch.object(service, "tick", side_effect=RuntimeError("private-fixture-value")), \
                    patch.object(supervisor, "atomic_json", side_effect=PermissionError("secret-path")):
                with self.assertRaisesRegex(RuntimeError, "private-fixture-value"):
                    service.run()
        self.assertTrue(all(process.terminated for process in owned))
        self.assertEqual(service.state, "failed")
        messages = "\n".join(captured.output)
        self.assertIn("supervisor_failed exception=RuntimeError", messages)
        self.assertIn("supervisor_stopped state=failed reason=exception", messages)
        self.assertNotIn("private-fixture-value", messages)
        self.assertNotIn("secret-path", messages)

    def test_output_close_failure_does_not_prevent_child_restart(self):
        now = [0.0]
        service = supervisor.Supervisor(self.settings, clock=lambda: now[0],
                                        spawn=lambda *args, **kwargs: FakeProcess())
        self.addCleanup(service.stop_children)
        service.tick()
        proxy = service.children["proxy"]["process"]
        tunnel = service.children["tunnel"]["process"]
        tunnel.returncode = 9
        with patch.object(tunnel.stdout, "close", side_effect=OSError("private-fixture-value")):
            with self.assertLogs(service.logger, level="WARNING") as captured:
                service.tick()
        now[0] = 1.0
        service.tick()
        self.assertIs(service.children["proxy"]["process"], proxy)
        self.assertIsNot(service.children["tunnel"]["process"], tunnel)
        self.assertEqual(service.state, "children_running")
        self.assertIn("tunnel_output_close_failed exception=OSError", "\n".join(captured.output))
        self.assertNotIn("private-fixture-value", "\n".join(captured.output))

    def test_delayed_old_proxy_observer_cannot_stop_replacement_tunnel(self):
        now = [0.0]
        service = supervisor.Supervisor(self.settings, clock=lambda: now[0],
                                        spawn=lambda *args, **kwargs: FakeProcess())
        self.addCleanup(service.stop_children)
        service.tick()
        old_proxy = service.children["proxy"]["process"]
        old_ready = service.children["proxy"]["readiness"]["ready"]
        observed_eof = threading.Event()
        clear = old_ready.clear

        def clear_and_notify():
            clear()
            observed_eof.set()

        with patch.object(old_ready, "clear", side_effect=clear_and_notify):
            with service.lifecycle_lock:
                old_proxy.returncode = 0
                old_proxy.stdout.close()
                self.assertTrue(observed_eof.wait(timeout=2))
                # Keep the old observer blocked while a new generation starts.
                service.tick()
                now[0] = 1.0
                service.tick()
                replacement = service.children["tunnel"]["process"]
                self.assertIsNotNone(replacement)
            self.assertTrue(service.wakeup.wait(timeout=2))
        self.assertIs(service.children["tunnel"]["process"], replacement)
        self.assertFalse(replacement.terminated)

    def test_proxy_exit_immediately_stops_only_owned_tunnel_before_retry(self):
        created = []

        def spawn(*args, **kwargs):
            process = FakeProcess()
            created.append(process)
            return process

        unrelated = FakeProcess()
        service = supervisor.Supervisor(self.settings, spawn=spawn)
        service.tick()
        self.addCleanup(service.stop_children)
        proxy, tunnel = created
        proxy.returncode = 98
        proxy.stdout.close()
        self.assertTrue(service.wakeup.wait(timeout=2))
        self.assertTrue(tunnel.terminated)
        self.assertIsNone(service.children["tunnel"]["process"])
        self.assertFalse(unrelated.terminated)
        service.tick()
        self.assertEqual(len(created), 2)

    def test_bind_failure_without_ready_stdout_never_launches_tunnel(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        script = f"import socket; s=socket.socket(); s.bind(('127.0.0.1', {port})); print('unexpected', flush=True)"
        commands = {"proxy": [sys.executable, "-B", "-c", script], "tunnel": ["must-never-launch"]}
        launched = []

        def spawn(command, **kwargs):
            self.assertNotEqual(command, commands["tunnel"])
            launched.append(command)
            return subprocess.Popen(command, **kwargs)

        with patch.object(supervisor.Settings, "commands", return_value=commands):
            service = supervisor.Supervisor(self.settings, spawn=spawn)
            service.tick()
            self.addCleanup(service.stop_children)
        self.assertEqual(launched, [commands["proxy"]])
        self.assertIsNone(service.children["proxy"]["process"])
        self.assertIsNone(service.children["tunnel"]["process"])
        self.assertEqual(service.state, "retry_wait")

    def test_missing_token_stops_before_spawning_any_child(self):
        (self.runtime / "tunnel-token.txt").unlink()
        with patch.object(supervisor.subprocess, "Popen") as spawn:
            service = supervisor.Supervisor(self.settings, spawn=spawn)
            service.run()
        spawn.assert_not_called()
        self.assertEqual(service.state, "configuration_error")

    @unittest.skipUnless(os.name == "nt", "Windows child ownership")
    def test_windows_job_exit_kills_owned_child_and_preserves_unrelated_process(self):
        command = [sys.executable, "-B", "-c", "import time; time.sleep(60)"]
        owned = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        unrelated = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        job = supervisor.ChildJob()
        try:
            job.add(owned)
            job.close()
            owned.wait(timeout=5)
            self.assertIsNotNone(owned.returncode)
            self.assertIsNone(unrelated.poll())
        finally:
            job.close()
            for process in (owned, unrelated):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
