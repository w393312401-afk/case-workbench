"""Password setup uses isolated runtime files; never the live public account."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import remote_accounts
from remote_accounts import RemoteAccounts, RemoteAccountError
import test_server as fixture


PASSWORD = "fixture-only-password-123"


class RemoteAccountsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="remote-account-test-")
        self.addCleanup(temporary.cleanup)
        self.runtime = Path(temporary.name)
        self.accounts = RemoteAccounts(self.runtime)
        self.accounts.directory.mkdir()
        self.config = {"hostname": "fixture.example.com", "teamDomain": "https://fixture.cloudflareaccess.com",
                       "audience": "a" * 64, "allowedEmails": ["fixture@example.com"], "futureOption": {"keep": True}}
        self.accounts.config.write_text(json.dumps(self.config), encoding="utf-8")
        (self.accounts.directory / "status.json").write_text(json.dumps({"instance_id": "a" * 32}), encoding="utf-8")

    def save(self, **changes):
        return self.accounts.save({"username": "  fixture-admin  ", "password": PASSWORD,
                                   "confirm_password": PASSWORD, **changes})

    def test_hash_private_metadata_and_config_preservation(self):
        result = self.save()
        account = json.loads(self.accounts.account.read_text(encoding="utf-8"))
        self.assertEqual(account["username"], "fixture-admin")
        self.assertEqual(account["scrypt"], {"n": 32768, "r": 8, "p": 3})
        self.assertEqual(len(account["salt"]), 64)
        self.assertEqual(len(account["credential_id"]), 32)
        derived = hashlib.scrypt(PASSWORD.encode(), salt=bytes.fromhex(account["salt"]), **account["scrypt"], dklen=64, maxmem=64*1024*1024)
        self.assertEqual(derived.hex(), account["password_hash"])
        self.assertNotIn(PASSWORD, self.accounts.account.read_text())
        for secret in ("password", "password_hash", "salt", "credential_id"):
            self.assertNotIn(secret, result)
            self.assertNotIn(secret, self.accounts.metadata())
        self.assertEqual(json.loads(self.accounts.config.read_text()), {**self.config, "authMode": "password"})
        reload = json.loads((self.accounts.directory / "reload.request").read_text())
        self.assertEqual(reload["instance_id"], "a"*32)
        self.assertRegex(reload["request_id"], r"^[0-9a-f]{32}$")
        self.assertTrue(result["reload_requested"])
        self.assertIn("Cloudflare 公网入口仍需切换", result["notice"])
        if os.name != "nt":
            self.assertEqual(self.accounts.account.stat().st_mode & 0o777, 0o600)

    def test_replacement_rotates_credential_identifier_and_salt(self):
        self.save()
        original = json.loads(self.accounts.account.read_text())
        self.save(username="other-admin")
        changed = json.loads(self.accounts.account.read_text())
        self.assertNotEqual(changed["credential_id"], original["credential_id"])
        self.assertNotEqual(changed["salt"], original["salt"])
        self.assertEqual(self.accounts.metadata()["username"], "other-admin")

    def test_guest_readiness_uses_verified_deployment_marker_without_exposing_private_fields(self):
        self.accounts.config.write_text(json.dumps({**self.config, "authMode": "password", "guestEnabled": True}))
        deployment = self.accounts.directory / "deployment.json"
        before = self.accounts.metadata()
        self.assertTrue(before["guest_enabled"])
        self.assertFalse(before["public_ready"])
        deployment.write_text(json.dumps({"auth_method": "password_with_guest", "private_fixture": "never-public"}))
        ready = self.accounts.metadata()
        self.assertTrue(ready["public_ready"])
        self.assertFalse(ready["configured"])
        self.assertEqual(ready["username"], "")
        self.assertNotIn("never-public", json.dumps(ready))
        self.assertFalse(self.accounts.account.exists())
        result = self.save()
        self.assertTrue(result["public_ready"])
        self.assertIn("公网账号已更新", result["notice"])
        self.assertIn("访客浏览不受影响", result["notice"])
        self.assertNotIn("Cloudflare", result["notice"])
        self.assertTrue(json.loads(self.accounts.config.read_text())["guestEnabled"])

    def test_guest_flags_fail_closed_for_unknown_or_invalid_metadata(self):
        deployment = self.accounts.directory / "deployment.json"
        for raw in ("invalid json", "null", "[]", '"password_with_guest"', '{}',
                    '{"auth_method":null}', '{"auth_method":true}', '{"auth_method":"cloudflare"}'):
            with self.subTest(raw=raw):
                deployment.write_text(raw)
                self.assertFalse(self.accounts.metadata()["public_ready"])
        for enabled in (None, 1, "true", [], {}):
            with self.subTest(enabled=enabled):
                self.accounts.config.write_text(json.dumps({**self.config, "guestEnabled": enabled}))
                self.assertFalse(self.accounts.metadata()["guest_enabled"])
        deployment.write_text('{"auth_method":"password_with_guest"}')
        self.accounts.config.unlink()
        unavailable = self.accounts.metadata()
        self.assertFalse(unavailable["guest_enabled"])
        self.assertFalse(unavailable["public_ready"])

    def test_invalid_inputs_do_not_write_account_or_enable_password_mode(self):
        for changes in ({"username": "ab"}, {"username": "x"*65}, {"username": "a\nadmin"},
                        {"username": "a\u200bb"}, {"username": 42}, {"password": "short"},
                        {"password": "密码"*65}, {"password": "a"*257}, {"password": "a"*12+"\ud800"},
                        {"password": None}, {"confirm_password": "does-not-match"}):
            with self.subTest(changes=changes), self.assertRaises(RemoteAccountError):
                self.save(**changes)
            self.assertFalse(self.accounts.account.exists())
            self.assertEqual(json.loads(self.accounts.config.read_text()), self.config)

    def test_missing_public_configuration_cannot_create_a_connection(self):
        self.accounts.config.unlink()
        self.assertFalse(self.accounts.metadata()["remote_configured"])
        with self.assertRaises(RemoteAccountError) as raised:
            self.save()
        self.assertEqual(raised.exception.status, 409)
        self.assertFalse(self.accounts.account.exists())
        self.assertFalse(self.accounts.config.exists())

    def test_corrupt_account_has_no_public_secrets(self):
        self.accounts.account.write_text(json.dumps({"username": "unsafe", "password_hash": "secret"}))
        result = self.accounts.metadata()
        self.assertFalse(result["configured"])
        self.assertEqual(result["username"], "")
        self.assertNotIn("secret", json.dumps(result))

    def test_metadata_rejects_version_and_timestamp_that_proxy_would_reject(self):
        self.save()
        account = json.loads(self.accounts.account.read_text())
        for changes in ({"version": True}, {"version": 1.0}, {"version": "1"},
                        {"updated_at": None}, {"updated_at": 123}, {"updated_at": ""},
                        {"updated_at": "x" * 65}):
            with self.subTest(changes=changes):
                self.accounts.account.write_text(json.dumps({**account, **changes}))
                result = self.accounts.metadata()
                self.assertFalse(result["configured"])
                self.assertEqual(result["username"], "")
        del account["updated_at"]
        self.accounts.account.write_text(json.dumps(account))
        self.assertFalse(self.accounts.metadata()["configured"])

    def test_config_write_failure_rolls_back_new_or_existing_credentials(self):
        atomic = remote_accounts.atomic_private_bytes
        for existing in (False, True):
            if existing:
                self.save()
            before_account = self.accounts.account.read_bytes() if existing else None
            before_config = self.accounts.config.read_bytes()
            calls = 0

            def fail_config_once(path, content):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("fixture write failure")
                atomic(path, content)

            with self.subTest(existing=existing), patch.object(remote_accounts, "atomic_private_bytes", side_effect=fail_config_once):
                with self.assertRaises(RemoteAccountError):
                    self.save(username="replacement")
            self.assertEqual(self.accounts.account.read_bytes() if self.accounts.account.exists() else None, before_account)
            self.assertEqual(self.accounts.config.read_bytes(), before_config)

    def test_reload_failure_rolls_back_account_and_config(self):
        atomic = remote_accounts.atomic_private_bytes

        def fail_reload(path, content):
            if path.name == "reload.request":
                raise OSError("fixture reload failure")
            atomic(path, content)

        with patch.object(remote_accounts, "atomic_private_bytes", side_effect=fail_reload):
            with self.assertRaises(RemoteAccountError):
                self.save()
        self.assertFalse(self.accounts.account.exists())
        self.assertEqual(json.loads(self.accounts.config.read_text()), self.config)

    def test_absent_supervisor_does_not_invent_an_instance(self):
        (self.accounts.directory / "status.json").unlink()
        result = self.save()
        self.assertTrue(result["configured"])
        self.assertFalse(result["reload_requested"])
        self.assertFalse((self.accounts.directory / "reload.request").exists())

    def test_permission_failure_leaves_no_partial_file(self):
        with patch.object(remote_accounts, "_private_file", side_effect=OSError("fixture permissions")):
            with self.assertRaises(RemoteAccountError):
                self.save()
        self.assertFalse(self.accounts.account.exists())
        self.assertEqual(list(self.accounts.directory.glob("*.tmp")), [])


class RemoteAccountHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def prepare(self):
        accounts = RemoteAccounts(self.runtime)
        accounts.directory.mkdir()
        accounts.config.write_text(json.dumps({"hostname": "fixture.example.com"}))
        return accounts

    def payload(self):
        return {"username": "fixture-admin", "password": PASSWORD, "confirm_password": PASSWORD}

    def test_cross_site_top_level_landing_navigation_opens_local_settings(self):
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for method in ("GET", "HEAD"):
            for path in ("/", "/index.html", "/?from=settings"):
                with self.subTest(method=method, path=path):
                    status, headers, body = self.request(method, path, headers=navigation)
                    self.assertEqual(status, 200)
                    self.assertIn("text/html", headers["Content-Type"])
                    if method == "GET":
                        self.assertIn(b"settings.js", body)
                    else:
                        self.assertEqual(body, b"")

    def test_landing_navigation_exception_never_bypasses_api_origin_host_or_write_guards(self):
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for method, path, changes in (
                ("GET", "/api/state", {}), ("GET", "/api/remote-account", {}),
                ("POST", "/api/remote-account", {}), ("POST", "/", {}),
                ("GET", "/settings.js", {}), ("GET", "/", {"Sec-Fetch-Dest": "iframe"}),
                ("GET", "/", {"Sec-Fetch-Mode": "cors"}), ("GET", "/", {"Sec-Fetch-Dest": ""}),
                ("GET", "/", {"Origin": "https://outside.example.com"}),
                ("GET", "/", {"Host": "outside.example.com"})):
            with self.subTest(method=method, path=path, changes=changes):
                status, _, _ = self.request(method, path, self.payload() if method == "POST" else None,
                                            headers={**navigation, **changes})
                self.assertEqual(status, 403)

    def test_http_requires_workbench_token_and_rejects_forwarding(self):
        accounts = self.prepare()
        status, _, _ = self.request("POST", "/api/remote-account", self.payload(), authenticated=False)
        self.assertEqual(status, 403)
        for header in ({"Origin": "https://evil.example.com"}, {"X-Forwarded-For": "203.0.113.1"},
                       {"CF-Connecting-IP": "203.0.113.1"}, {"Forwarded": "for=203.0.113.1"}):
            self.assertEqual(self.request("POST", "/api/remote-account", self.payload(), headers=header)[0], 403)
        self.assertFalse(accounts.account.exists())

    def test_http_save_and_get_return_only_safe_metadata(self):
        self.prepare()
        status, headers, before = self.request("GET", "/api/remote-account")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertFalse(before["configured"])
        status, _, result = self.request("POST", "/api/remote-account", self.payload())
        self.assertEqual(status, 200, result)
        self.assertTrue(result["configured"])
        self.assertEqual(result["auth_mode"], "password")
        status, _, after = self.request("GET", "/api/remote-account")
        self.assertEqual(status, 200)
        self.assertEqual(after["username"], "fixture-admin")
        self.assertEqual(set(after), {"configured", "username", "auth_mode", "public_url", "remote_configured", "guest_enabled", "public_ready"})
        self.assertEqual(self.server._active_writes, 0)

    def test_account_write_obeys_existing_maintenance_gate(self):
        accounts = self.prepare()
        self.server.data_maintenance = True
        try:
            status, _, result = self.request("POST", "/api/remote-account", self.payload())
        finally:
            self.server.data_maintenance = False
        self.assertEqual(status, 409, result)
        self.assertFalse(accounts.account.exists())

    def test_http_does_not_create_missing_public_config(self):
        status, _, result = self.request("POST", "/api/remote-account", self.payload())
        self.assertEqual(status, 409, result)
        self.assertFalse((self.runtime / "remote-access").exists())


if __name__ == "__main__":
    unittest.main()
