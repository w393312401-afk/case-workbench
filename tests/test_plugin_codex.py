from __future__ import annotations

import socket
import threading
import unittest

from codex_bridge import BridgeCancelled, BridgeError, BridgeUnavailable
from plugin_codex import PluginCodex, PluginCodexError
import test_server as fixture


def body():
    return {"model": "codex-default", "stream": False, "max_tokens": 8192,
            "messages": [{"role": "system", "content": "只返回 id 和 translation 的 JSON 数组。"},
                         {"role": "user", "content": '[{"id":"cue-1","text":"Keep the angle steady."}]'}]}


class FakeBridge:
    def __init__(self):
        self.calls = []
        self.error = None

    def status(self):
        return {"available": True}

    def run(self, prompt, **options):
        self.calls.append((prompt, options))
        if self.error:
            raise self.error
        options["on_event"]({"type": "session", "model": "client-default"})
        return '[{"id":"cue-1","translation":"保持角度稳定。"}]'


class PluginCodexTests(unittest.TestCase):
    def test_translation_uses_local_default_without_tokens_or_stored_threads(self):
        bridge = FakeBridge()
        result = PluginCodex(bridge=bridge).chat(body())
        self.assertEqual(result["model"], "client-default")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        prompt, options = bridge.calls[0]
        self.assertIn("不调用工具", prompt)
        self.assertIn("cue-1", prompt)
        self.assertNotIn("model", options)
        self.assertNotIn("skills", options)

    def test_invalid_payload_cannot_start_codex(self):
        bridge = FakeBridge()
        service = PluginCodex(bridge=bridge)
        invalid = [{**body(), "tools": []}, {**body(), "stream": True}, {**body(), "model": "other"},
                   {**body(), "max_tokens": True}, {**body(), "messages": []}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(PluginCodexError):
                service.chat(value)
        self.assertFalse(bridge.calls)

    def test_failure_releases_concurrency_slot_and_does_not_retry(self):
        bridge = FakeBridge()
        bridge.error = BridgeError("本机 Codex 登录暂不可用。")
        service = PluginCodex(bridge=bridge)
        with self.assertRaisesRegex(PluginCodexError, "登录"):
            service.chat(body())
        self.assertEqual(len(bridge.calls), 1)
        bridge.error = None
        self.assertTrue(service.chat(body())["choices"])

    def test_translation_allows_longer_generation_without_changing_the_default_model(self):
        service = PluginCodex()
        self.assertEqual(service.bridge.timeout, 180)
        self.assertEqual(service.bridge.startup_timeout, 45)

    def test_failure_codes_separate_retryable_interruptions_from_login_and_setup(self):
        cases = [
            (BridgeError("temporary", code="CODEX_CONNECTION"), 503, "CODEX_CONNECTION", True),
            (BridgeError("timeout", code="CODEX_TIMEOUT"), 504, "CODEX_TIMEOUT", True),
            (BridgeError("busy", code="CODEX_BUSY"), 429, "CODEX_BUSY", True),
            (BridgeError("login", code="CODEX_AUTH"), 401, "CODEX_AUTH", False),
            (BridgeError("quota", code="CODEX_QUOTA"), 429, "CODEX_QUOTA", False),
            (BridgeUnavailable("missing"), 503, "CODEX_SETUP", False),
            (BridgeCancelled("cancelled"), 499, "CODEX_CANCELLED", False),
        ]
        for failure, status, code, retryable in cases:
            with self.subTest(code=code):
                bridge = FakeBridge()
                bridge.error = failure
                service = PluginCodex(bridge=bridge)
                with self.assertRaises(PluginCodexError) as caught:
                    service.chat(body())
                self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.retryable),
                                 (status, code, retryable))
                self.assertEqual(len(bridge.calls), 1, "Retries belong to the subtitle client, not both layers")
                bridge.error = None
                self.assertTrue(service.chat(body())["choices"])

    def test_closed_client_cancels_local_codex_turn(self):
        started = threading.Event()

        class WaitingBridge(FakeBridge):
            def run(self, prompt, **options):
                started.set()
                if not options["cancel_event"].wait(3):
                    raise AssertionError("Client disconnect did not cancel the turn")
                raise BridgeError("已取消")

        local, remote = socket.socketpair()
        self.addCleanup(local.close)
        self.addCleanup(remote.close)
        service = PluginCodex(bridge=WaitingBridge())
        errors = []

        def work():
            try:
                service.chat(body(), connection=local)
            except PluginCodexError as error:
                errors.append(error)

        worker = threading.Thread(target=work)
        worker.start()
        self.assertTrue(started.wait(1))
        remote.close()
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)


class PluginCodexHTTPTests(unittest.TestCase):
    setUp = fixture.WorkbenchHTTPTests.setUp
    stop_server = fixture.WorkbenchHTTPTests.stop_server
    request = fixture.WorkbenchHTTPTests.request

    def test_failure_returns_a_safe_code_and_retryability_without_raw_diagnostics(self):
        bridge = FakeBridge()
        bridge.error = BridgeError("Codex 服务连接暂时中断，请稍后重试。", code="CODEX_CONNECTION")
        self.server.app.plugin_codex = PluginCodex(bridge=bridge)
        key = self.request("GET", "/api/public-stats/link")[2]["key"]
        status, _, result = self.request("POST", "/api/public-stats/codex/chat/completions", body(),
            headers={"Origin": "chrome-extension://abcdefghijklmnop", "X-TKL-Key": key}, authenticated=False)
        self.assertEqual(status, 503, result)
        self.assertEqual(result["code"], "CODEX_CONNECTION")
        self.assertTrue(result["retryable"])
        self.assertNotIn(key, str(result))

    def test_pairing_and_extension_origin_are_required_and_data_is_unchanged(self):
        bridge = FakeBridge()
        self.server.app.plugin_codex = PluginCodex(bridge=bridge)
        key = self.request("GET", "/api/public-stats/link")[2]["key"]
        before = self.request("GET", "/api/state")[2]
        headers = {"Origin": "chrome-extension://abcdefghijklmnop", "Authorization": "Bearer " + key}
        path = "/api/public-stats/codex/chat/completions"
        self.assertEqual(self.request("POST", path, body(), authenticated=False)[0], 403)
        wrong = {**headers, "Origin": "https://www.tiktok.com"}
        self.assertEqual(self.request("POST", path, body(), headers=wrong, authenticated=False)[0], 403)
        forwarded = {**headers, "X-Forwarded-For": "192.0.2.1"}
        self.assertEqual(self.request("POST", path, body(), headers=forwarded, authenticated=False)[0], 403)
        status, _, result = self.request("POST", path, body(), headers=headers, authenticated=False)
        self.assertEqual(status, 200, result)
        self.assertIn("保持角度稳定", result["choices"][0]["message"]["content"])
        self.assertEqual(len(bridge.calls), 1)
        after = self.request("GET", "/api/state")[2]
        self.assertEqual(before["digest"], after["digest"])
        ping = self.request("POST", "/api/public-stats/codex/status", {}, headers={"Origin": headers["Origin"], "X-TKL-Key": key}, authenticated=False)
        self.assertEqual(ping[0], 200, ping[2])
        self.assertEqual(ping[2]["provider"], "local-codex")
        self.assertEqual(len(bridge.calls), 1)


if __name__ == "__main__":
    unittest.main()
