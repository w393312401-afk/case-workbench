"""Disconnect endpoint checks use fake credentials and an in-memory HTTP handler."""
from email.message import Message
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from claude_bridge import LoginFlowError
from test_server import SERVER


class ClaudeDisconnectTests(unittest.TestCase):
    def setUp(self):
        self.claude = Mock()
        self.claude.disconnect.return_value = {"available": False, "logged_in": False, "login_pending": False}
        self.claude.login_details.return_value = {"attempt_id": None, "pending": False, "url": None}
        self.codex = Mock()
        self.api = Mock()
        self.app = object.__new__(SERVER.Workbench)
        self.app.lock = threading.RLock()
        self.app.token = "fixture-connection-token"
        self.app.ai = SimpleNamespace(
            bridge=SimpleNamespace(claude=self.claude, codex=self.codex, antigravity=self.api,
                                   _target=lambda model: self.codex),
            list_tasks=Mock(return_value=[]))
        self.app._edit_live = {}

    def handler(self, *, method="POST", body=None, address="127.0.0.1", headers=None):
        handler = object.__new__(SERVER.Handler)
        handler.command, handler.path = method, "/api/ai/claude/logout"
        payload = json.dumps({} if body is None else body).encode("utf-8")
        handler.headers = Message()
        for name, value in {"Host": "127.0.0.1:8767", "X-Workbench-Token": self.app.token,
                            "Content-Length": str(len(payload)), **(headers or {})}.items():
            handler.headers[name] = value
        handler.client_address = (address, 10000)
        handler.server = SimpleNamespace(server_port=8767, task_host=False, app=self.app, data_maintenance=False,
                                         begin_write=Mock(), end_write=Mock())
        handler.connection = Mock()
        handler.rfile = io.BytesIO(payload)
        handler.output, handler.log_error = Mock(), Mock()
        handler.close_connection = False
        return handler

    def test_post_disconnect_returns_verified_status_and_cleared_recovery(self):
        handler = self.handler()
        handler.dispatch()
        status, result = handler.output.call_args.args
        self.assertEqual(status, 200)
        self.assertFalse(result["claude"]["logged_in"])
        self.assertIsNone(result["login"]["url"])
        self.claude.disconnect.assert_called_once_with()
        self.codex.disconnect.assert_not_called()
        self.api.disconnect.assert_not_called()
        handler.server.end_write.assert_called_once_with()

    def test_remote_forwarded_and_missing_token_requests_do_not_disconnect(self):
        cases = [({"X-Forwarded-For": "192.0.2.1"}, "127.0.0.1"),
                 ({"Forwarded": "for=192.0.2.1"}, "127.0.0.1"),
                 ({"CF-Connecting-IP": "192.0.2.1"}, "127.0.0.1"),
                 ({"X-Workbench-Token": ""}, "127.0.0.1"), ({}, "192.0.2.1")]
        for headers, address in cases:
            with self.subTest(headers=headers, address=address):
                handler = self.handler(headers=headers, address=address)
                handler.dispatch()
                self.assertEqual(handler.output.call_args.args[0], 403)
                handler.server.begin_write.assert_not_called()
        self.claude.disconnect.assert_not_called()

    def test_get_and_nonempty_body_do_not_disconnect(self):
        handler = self.handler(method="GET")
        handler.dispatch()
        self.assertEqual(handler.output.call_args.args[0], 404)
        handler = self.handler(body={"unexpected": True})
        handler.dispatch()
        self.assertEqual(handler.output.call_args.args[0], 400)
        self.claude.disconnect.assert_not_called()

    def test_running_or_queued_claude_work_blocks_logout(self):
        for task in ({"status": "queued", "model": "claude-default"},
                     {"status": "running", "model": "claude-sonnet"},
                     {"status": "cancelled", "busy": True, "model": "claude-opus"}):
            with self.subTest(task=task):
                self.app.ai.list_tasks.return_value = [task]
                with self.assertRaises(SERVER.ApiError) as raised:
                    self.app.claude_logout({})
                self.assertEqual(raised.exception.status, 409)
        self.claude.disconnect.assert_not_called()

    def test_automatic_claude_and_preparing_claude_edit_block_logout(self):
        self.app.ai.list_tasks.return_value = [{"status": "queued", "model": None}]
        self.app.ai.bridge._target = lambda model: self.claude
        with self.assertRaises(SERVER.ApiError) as raised:
            self.app.claude_logout({})
        self.assertEqual(raised.exception.status, 409)
        self.app.ai.list_tasks.return_value = []
        self.app._edit_live = {"EDT-fixture": {"provider": "claude", "phase": "preparation"}}
        with self.assertRaises(SERVER.ApiError) as raised:
            self.app.claude_logout({})
        self.assertEqual(raised.exception.status, 409)
        self.claude.disconnect.assert_not_called()

    def test_independent_api_and_codex_work_does_not_block_logout(self):
        self.app.ai.list_tasks.return_value = [{"status": "running", "model": "antigravity:fixture"},
                                              {"status": "queued", "model": "gpt-fixture"},
                                              {"status": "completed", "model": "claude-default"}]
        self.app._edit_live = {"EDT-api": {"provider": "antigravity"}, "EDT-codex": {"provider": "codex"}}
        self.app.claude_logout({})
        self.claude.disconnect.assert_called_once_with()

    def test_bridge_conflict_preserves_status_code_and_safe_message(self):
        self.claude.disconnect.side_effect = LoginFlowError("仍有 Claude 调用正在执行。", 409)
        handler = self.handler()
        handler.dispatch()
        status, result = handler.output.call_args.args
        self.assertEqual(status, 409)
        self.assertEqual(result, {"error": "仍有 Claude 调用正在执行。"})


if __name__ == "__main__":
    unittest.main()
