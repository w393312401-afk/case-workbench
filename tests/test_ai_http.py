"""AI HTTP integration uses a fake bridge and temporary case records only."""
import copy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_server import SERVER, snapshot
from test_ai_skills import install_review_fixture
from codex_bridge import BridgeError


class FakeBridge:
    def __init__(self, blocked=False):
        self.blocked = blocked
        self.started = threading.Event()
        self.prompts = []

    def status(self):
        return {"available": True, "label": "临时验证连接"}

    def run(self, prompt, *, cwd, on_progress, cancel_event, **options):
        self.prompts.append(prompt)
        self.started.set()
        on_progress("Codex 已接单，正在处理")
        if self.blocked:
            if cancel_event.wait(4):
                raise RuntimeError("测试任务已取消")
        return "## 候选结果\n\n保持原稿事实，补充具体购买理由。"


class AIHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-ai-http-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        self.runtime = self.root / "runtime"
        self.video = self.root / "fixture.mp4"
        self.video.write_bytes(b"test video")
        self.store = SERVER.CaseStore(self.skill)
        self.case = self.store.add({"title":"临时案例", "product":"SHARPAL 202H",
            "original":{"text":"Keep the actual original."},
            "feedback":{"kind":"editorial","text":"临时验证"}, "current":snapshot(self.video)})
        self.case_id = self.case["case_id"]
        self.bridge = FakeBridge()
        self.silence = patch.object(SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = SERVER.make_server(self.skill, self.runtime, port=0, allow_open=False, ai_bridge=self.bridge)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval":0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.token = self.request("GET", "/api/state")[1]["token"]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, authenticated=True, headers=None):
        supplied = dict(headers or {})
        if authenticated and method == "POST":
            supplied["X-Workbench-Token"] = self.token
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        if data is not None:
            supplied["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, data, supplied)
            response = connection.getresponse()
            raw = response.read()
            content = json.loads(raw) if "application/json" in response.getheader("Content-Type", "") else raw
            return response.status, content
        finally:
            connection.close()

    def body(self, action="review"):
        detail = self.request("GET", f"/api/cases/{self.case_id}")[1]
        return {"request_id":uuid4().hex, "case_id":self.case_id, "revision":detail["revision"],
                "action":action, "instruction":"检查这一版的购买理由，并保存候选建议。"}

    def test_explicit_options_refresh_updates_catalog_without_running_a_task(self):
        source = {"models": [{"id": "claude-opus", "name": "Claude Opus 5.5", "available": True}]}
        cached = copy.deepcopy(source)
        calls = {"models": 0, "refresh": 0}

        def models():
            calls["models"] += 1
            return copy.deepcopy(cached)

        def refresh_models():
            calls["refresh"] += 1
            cached.clear()
            cached.update(copy.deepcopy(source))

        with patch.object(self.bridge, "models", models, create=True), \
                patch.object(self.bridge, "refresh_models", refresh_models, create=True):
            status, first = self.request("GET", "/api/ai/options")
            self.assertEqual(status, 200, first)
            self.assertEqual(first["models"][0]["name"], "Claude Opus 5.5")
            source["models"][0]["name"] = "Claude Opus 6.1"

            status, unchanged = self.request("GET", "/api/ai/options")
            self.assertEqual(status, 200, unchanged)
            self.assertEqual(unchanged["models"][0]["name"], "Claude Opus 5.5")
            self.assertEqual(calls, {"models": 2, "refresh": 0})

            status, refreshed = self.request("GET", "/api/ai/options?refresh=1")
            self.assertEqual(status, 200, refreshed)
            self.assertEqual(refreshed["models"][0]["name"], "Claude Opus 6.1")
            self.assertEqual(calls, {"models": 3, "refresh": 1})
            self.assertEqual(self.bridge.prompts, [])
            self.assertFalse(self.bridge.started.is_set())

    def test_legacy_claude_key_and_sdk_routes_cannot_reenable_api_billing(self):
        for path in ("/api/ai/claude/key", "/api/ai/claude/key/delete", "/api/ai/claude/install"):
            with self.subTest(path=path):
                status, body = self.request("POST", path, {"api_key": "unused-sensitive-placeholder"})
                self.assertEqual(status, 410)
                self.assertIn("客户端额度", body["error"])
                self.assertNotIn("unused-sensitive-placeholder", json.dumps(body))
        self.assertFalse((self.runtime / "secrets" / "anthropic.json").exists())

    def install_claude_login(self, available=False):
        class FakeClaudeLogin:
            def __init__(self):
                self.calls = 0
                self.status_calls = 0
                self.details_calls = 0
                self.callback_calls = []
                self.cancel_calls = []
                self.available = available
                self.attempt_id = None
                self.pending = False
                self.callback_submitted = False
                self.callback_error = None

            def status(self, refresh=False):
                self.status_calls += 1
                return {"provider": "claude", "connection": "local_cli", "available": self.available,
                        "desktop_bundled": True, "login_pending": self.pending,
                        "label": "Claude 订阅已连接" if self.available else "等待完成 Claude 订阅连接"}

            def login_details(self):
                self.details_calls += 1
                return {"attempt_id": self.attempt_id, "pending": self.pending,
                        "url": "https://claude.ai/oauth/authorize?state=private-auth-state" if self.pending else None,
                        "expires_at": "2026-10-09T12:00:00Z" if self.pending else None,
                        "callback_submitted": self.callback_submitted,
                        "callback_error": self.callback_error}

            def start_login(self):
                self.calls += 1
                if not self.available and not self.pending:
                    self.attempt_id = str(uuid4())
                    self.pending = True
                return self.status()

            def submit_login_callback(self, attempt_id, callback_url):
                self.callback_calls.append((attempt_id, callback_url))
                self.callback_submitted = True
                self.callback_error = None
                return self.status()

            def cancel_login(self, attempt_id):
                self.cancel_calls.append(attempt_id)
                self.pending = False
                self.attempt_id = None
                self.callback_submitted = False
                self.callback_error = None
                return self.status()

        claude = FakeClaudeLogin()
        self.bridge.claude = claude
        return claude

    def test_claude_subscription_login_route_returns_client_status(self):
        claude = self.install_claude_login()
        status, body = self.request("POST", "/api/ai/claude/login", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(claude.calls, 1)
        self.assertTrue(body["claude"]["login_pending"])
        self.assertTrue(body["claude"]["desktop_bundled"])
        self.assertTrue(body["login"]["pending"])
        self.assertEqual(body["login"]["attempt_id"], claude.attempt_id)
        self.assertIsInstance(body["message"], str)
        self.assertNotIn("private-auth-state", json.dumps(body["claude"]))
        self.assertFalse((self.runtime / "secrets").exists())

    def test_claude_login_route_preserves_an_available_client_status(self):
        self.install_claude_login(available=True)
        status, body = self.request("POST", "/api/ai/claude/login", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["claude"]["available"])
        self.assertFalse(body["claude"]["login_pending"])
        self.assertFalse(body["login"]["pending"])
        self.assertIsNone(body["login"]["url"])

    def test_claude_pending_login_can_be_recovered_after_page_is_closed(self):
        claude = self.install_claude_login()
        first = self.request("POST", "/api/ai/claude/login", {})[1]
        status, restored = self.request("GET", "/api/ai/claude/login",
                                        headers={"X-Workbench-Token": self.token})
        self.assertEqual(status, 200, restored)
        self.assertEqual(restored["login"], first["login"])
        self.assertEqual(claude.calls, 1)
        second = self.request("POST", "/api/ai/claude/login", {})[1]
        self.assertEqual(second["login"]["attempt_id"], first["login"]["attempt_id"])
        self.assertTrue(second["login"]["pending"])

    def test_claude_login_details_require_token_and_same_origin_before_reading_secrets(self):
        claude = self.install_claude_login()
        self.request("POST", "/api/ai/claude/login", {})
        before = claude.details_calls, claude.status_calls
        for headers in ({}, {"X-Workbench-Token": "wrong"},
                        {"X-Workbench-Token": self.token, "Origin": "https://example.invalid"}):
            with self.subTest(headers=headers):
                status, result = self.request("GET", "/api/ai/claude/login", headers=headers)
                self.assertEqual(status, 403, result)
                self.assertNotIn("private-auth-state", json.dumps(result))
        self.assertEqual((claude.details_calls, claude.status_calls), before)

    def test_claude_callback_and_cancel_forward_only_expected_fields(self):
        claude = self.install_claude_login()
        attempt_id = self.request("POST", "/api/ai/claude/login", {})[1]["login"]["attempt_id"]
        callback = "http://localhost:45678/callback?code=private-auth-code&state=private-auth-state"
        status, result = self.request("POST", "/api/ai/claude/callback",
                                      {"attempt_id": attempt_id, "callback_url": callback})
        self.assertEqual(status, 200, result)
        self.assertEqual(claude.callback_calls, [(attempt_id, callback)])
        self.assertTrue(result["login"]["callback_submitted"])
        self.assertNotIn("private-auth-code", json.dumps(result))
        status, result = self.request("POST", "/api/ai/claude/login/cancel", {"attempt_id": attempt_id})
        self.assertEqual(status, 200, result)
        self.assertEqual(claude.cancel_calls, [attempt_id])
        self.assertFalse(result["login"]["pending"])
        self.assertIsNone(result["login"]["url"])
        next_id = self.request("POST", "/api/ai/claude/login", {})[1]["login"]["attempt_id"]
        self.assertNotEqual(attempt_id, next_id)

    def test_claude_callback_and_cancel_validate_before_touching_login(self):
        claude = self.install_claude_login()
        cases = {
            "/api/ai/claude/callback": [{}, {"attempt_id": "attempt"},
                {"callback_url": "private-auth-code"},
                {"attempt_id": "attempt", "callback_url": "private-auth-code", "url": "extra"},
                {"attempt_id": 123, "callback_url": "private-auth-code"},
                {"attempt_id": "attempt", "callback_url": None}],
            "/api/ai/claude/login/cancel": [{}, {"attempt_id": 123},
                {"attempt_id": "attempt", "force": True}],
        }
        for path, bodies in cases.items():
            valid = {"attempt_id": "attempt"}
            if path.endswith("callback"):
                valid["callback_url"] = "private-auth-code"
            for headers, authenticated in (({}, False), ({"Origin": "https://example.invalid"}, True)):
                status, result = self.request("POST", path, valid, authenticated=authenticated, headers=headers)
                self.assertEqual(status, 403, result)
                self.assertNotIn("private-auth-code", json.dumps(result))
            for body in bodies:
                with self.subTest(path=path, body=body):
                    status, result = self.request("POST", path, body)
                    self.assertEqual(status, 400, result)
                    self.assertNotIn("private-auth-code", json.dumps(result))
        self.assertEqual(claude.callback_calls, [])
        self.assertEqual(claude.cancel_calls, [])

    def test_claude_recovery_routes_are_unavailable_without_login_support(self):
        requests = [("GET", "/api/ai/claude/login", None),
                    ("POST", "/api/ai/claude/callback", {"attempt_id": "attempt", "callback_url": "callback"}),
                    ("POST", "/api/ai/claude/login/cancel", {"attempt_id": "attempt"})]
        for method, path, body in requests:
            with self.subTest(path=path, method=method):
                status, result = self.request(method, path, body, headers={"X-Workbench-Token": self.token})
                self.assertEqual(status, 503, result)
                self.assertIsInstance(result["error"], str)

    def test_claude_callback_maps_safe_validation_conflict_and_client_failures(self):
        claude = self.install_claude_login()
        attempt_id = self.request("POST", "/api/ai/claude/login", {})[1]["login"]["attempt_id"]
        for expected in (400, 409, 503):
            with self.subTest(status=expected):
                error = BridgeError("安全的授权提示")
                error.status = expected
                with patch.object(claude, "submit_login_callback", side_effect=error):
                    status, result = self.request("POST", "/api/ai/claude/callback",
                        {"attempt_id": attempt_id, "callback_url": "private-auth-code"})
                self.assertEqual(status, expected, result)
                self.assertEqual(result["error"], "安全的授权提示")
                self.assertNotIn("private-auth-code", json.dumps(result))

    def test_claude_login_recovery_returns_pending_callback_feedback(self):
        claude = self.install_claude_login()
        attempt_id = self.request("POST", "/api/ai/claude/login", {})[1]["login"]["attempt_id"]
        self.request("POST", "/api/ai/claude/callback",
                     {"attempt_id": attempt_id, "callback_url": "private-auth-code"})
        claude.callback_error = "回调已发送，客户端仍在处理。"
        status, result = self.request("GET", "/api/ai/claude/login", headers={"X-Workbench-Token": self.token})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["login"]["pending"])
        self.assertTrue(result["login"]["callback_submitted"])
        self.assertEqual(result["login"]["callback_error"], claude.callback_error)
        self.assertNotIn("callback_error", result["claude"])
        self.assertNotIn("private-auth-code", json.dumps(result))

    def test_claude_login_requires_token_same_origin_and_strictly_empty_body(self):
        claude = self.install_claude_login()
        path = "/api/ai/claude/login"
        self.assertEqual(self.request("POST", path, {}, authenticated=False)[0], 403)
        self.assertEqual(self.request("POST", path, {}, headers={"Origin": "https://example.invalid"})[0], 403)
        for body in ({"api_key": "unused-sensitive-placeholder"}, {"force": True}, {"url": "https://example.invalid"}):
            with self.subTest(body=body):
                status, result = self.request("POST", path, body)
                self.assertEqual(status, 400, result)
                self.assertNotIn("unused-sensitive-placeholder", json.dumps(result))
        self.assertEqual(claude.calls, 0)

    def test_claude_login_is_unavailable_when_bridge_has_no_login_support(self):
        status, body = self.request("POST", "/api/ai/claude/login", {})
        self.assertEqual(status, 503, body)
        self.assertIsInstance(body["error"], str)


    def create(self, body=None):
        status, value = self.request("POST", "/api/ai/tasks", body or self.body())
        self.assertEqual(status, 200, value)
        return value["task"]

    def wait(self, task_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status, value = self.request("GET", f"/api/ai/tasks/{task_id}")
            self.assertEqual(status, 200, value)
            task = value["task"]
            if task["status"] not in {"queued", "running"} and not self.server.app.ai.active_for_case(self.case_id):
                return task
            time.sleep(0.02)
        self.fail("AI fixture did not finish")

    def records(self):
        return {str(p):p.read_bytes() for p in self.store.folder(self.case_id).rglob("*.json")
                if "collaboration" not in p.parts}

    def test_create_complete_continue_and_preserve_original(self):
        before = self.records()
        task = self.wait(self.create()["task_id"])
        self.assertEqual(task["status"], "completed", task)
        self.assertIn("候选结果", task["rounds"][0]["output"])
        body = {"request_id":uuid4().hex, "task_revision":task["revision"], "instruction":"保留开头，再具体一些。"}
        status, value = self.request("POST", f"/api/ai/tasks/{task['task_id']}/continue", body)
        self.assertEqual(status, 200, value)
        latest = self.wait(task["task_id"])
        self.assertEqual(len(latest["rounds"]), 2)
        self.assertIn("候选结果", self.bridge.prompts[-1])
        self.assertIn("保留开头", self.bridge.prompts[-1])
        self.assertEqual(self.records(), before)
        self.assertTrue(list(self.store.folder(self.case_id).glob("collaboration/*/task.json")))
        status, summary = self.request("GET", "/api/ai/tasks?case_id=" + self.case_id)
        self.assertEqual(status, 200, summary)
        self.assertEqual(len(summary["tasks"]), 1)
        self.assertNotIn("rounds", summary["tasks"][0])

    def test_review_round_carries_the_library_digest_and_tells_the_page(self):
        from test_review_store import build_digest_sources
        build_digest_sources(self.runtime / "reviews" / "sources")  # the temp runtime now has a user-review library
        task = self.wait(self.create()["task_id"])
        self.assertEqual(task["status"], "completed", task)
        prompt = self.bridge.prompts[-1]
        self.assertIn("<untrusted_user_review_digest>", prompt)
        self.assertIn("Will this do swords?", prompt)              # an unanswered question from the library
        self.assertIn("doesn't securely hold short blades", prompt)  # a buyer's limit, cut from a long review
        for name in ("viewer_one", "Jane Doe"):                     # reviewer names are never in the library
            self.assertNotIn(name, prompt)
        context = task["rounds"][0]["review_context"]
        self.assertEqual((context["status"], context["reviews"]), ("attached", 16))
        self.assertNotIn("text", context)
        self.assertIn("synced_at", context)

    def test_no_digest_when_the_runtime_has_no_review_library(self):
        task = self.wait(self.create()["task_id"])
        self.assertEqual(task["status"], "completed", task)
        self.assertNotIn("untrusted_user_review_digest", self.bridge.prompts[-1])
        self.assertNotIn("review_context", task["rounds"][0])
        self.assertFalse((self.runtime / "reviews").exists())  # asking about the library must not create one

    def test_all_three_actions_and_invalid_action(self):
        install_review_fixture(self.skill)
        for action in ("variants", "review", "retrospective"):
            task = self.wait(self.create(self.body(action))["task_id"])
            self.assertEqual((task["action"], task["status"]), (action, "completed"))
        self.assertEqual(self.request("POST", "/api/ai/tasks", self.body("delete_files"))[0], 400)

    def test_stale_case_revision_rejects_before_creating_task(self):
        body = self.body()
        self.store.set_archived(self.case_id, True)
        self.assertEqual(self.request("POST", "/api/ai/tasks", body)[0], 409)
        self.assertEqual(self.request("GET", "/api/ai/tasks")[1]["tasks"], [])

    def test_idempotency_survives_task_progress_and_changed_payload_rejects(self):
        body = self.body()
        task = self.wait(self.create(body)["task_id"])
        self.assertEqual(self.create(body)["task_id"], task["task_id"])
        self.assertEqual(len(self.bridge.prompts), 1)
        changed = {**body, "instruction":"不同要求"}
        self.assertEqual(self.request("POST", "/api/ai/tasks", changed)[0], 409)
        follow = {"request_id":uuid4().hex,"task_revision":task["revision"],"instruction":"继续修改"}
        path = f"/api/ai/tasks/{task['task_id']}/continue"
        self.assertEqual(self.request("POST", path, follow)[0], 200)
        self.wait(task["task_id"])
        self.assertEqual(self.request("POST", path, follow)[0], 200)
        self.assertEqual(len(self.bridge.prompts), 2)

    def test_task_stale_banner_does_not_overwrite_case_or_old_snapshot(self):
        task = self.wait(self.create()["task_id"])
        base = task["base_revision"]
        current = copy.deepcopy(self.case["current"])
        current["conclusion"] = "用户在任务之后更新了判断。"
        self.store.append(self.case_id, {"kind":"editorial","text":"用户新判断","current":current})
        latest = self.request("GET", f"/api/ai/tasks/{task['task_id']}")[1]["task"]
        self.assertTrue(latest["stale"])
        self.assertEqual(latest["base_revision"], base)
        self.assertEqual(self.store.read_case(self.case_id)["current"], current)

    def test_running_task_blocks_delete_then_cancel_allows_full_cleanup(self):
        self.bridge.blocked = True
        task = self.create()
        self.assertTrue(self.bridge.started.wait(2))
        self.store.set_archived(self.case_id, True)
        path = f"/api/cases/{self.case_id}/delete-preview?include_source=false"
        self.assertEqual(self.request("GET", path)[0], 409)
        self.assertEqual(self.request("POST", f"/api/ai/tasks/{task['task_id']}/cancel", {"request_id":uuid4().hex,"round_id":task["rounds"][-1]["round_id"]})[0], 200)
        self.assertEqual(self.wait(task["task_id"])["status"], "cancelled")
        status, preview = self.request("GET", path)
        self.assertEqual(status, 200, preview)
        self.assertTrue(any(Path(f["path"]).name == "task.json" for f in preview["files"]))
        status, deleted = self.request("POST", f"/api/cases/{self.case_id}/delete", {
            "request_id":uuid4().hex,"revision":preview["revision"],"plan_token":preview["plan_token"],
            "confirmation":self.case_id,"include_source":False})
        self.assertEqual(status, 200, deleted)
        self.assertFalse(self.store.folder(self.case_id).exists())
        self.assertEqual(self.request("GET", f"/api/ai/tasks/{task['task_id']}")[0], 404)
        self.assertEqual(self.request("GET", "/api/ai/tasks")[1]["tasks"], [])

    def test_delete_selected_ai_history_through_authenticated_api(self):
        first = self.wait(self.create()["task_id"])
        second = self.wait(self.create()["task_id"])
        before = self.records()
        body = {"tasks": [
            {"task_id": first["task_id"], "revision": first["revision"]},
            {"task_id": second["task_id"], "revision": second["revision"] - 1},
        ]}
        self.assertEqual(self.request("POST", "/api/ai/tasks/delete", body, authenticated=False)[0], 403)
        status, result = self.request("POST", "/api/ai/tasks/delete", body)
        self.assertEqual(status, 200, result)
        self.assertEqual(result["deleted"], [first["task_id"]])
        self.assertEqual([item["task_id"] for item in result["failed"]], [second["task_id"]])
        self.assertEqual(self.request("GET", f"/api/ai/tasks/{first['task_id']}")[0], 404)
        self.assertEqual([item["task_id"] for item in self.request("GET", "/api/ai/tasks")[1]["tasks"]],
                         [second["task_id"]])
        self.assertEqual(self.records(), before)

    def test_cancel_replay_cannot_cancel_a_later_round(self):
        self.bridge.blocked = True
        task = self.create()
        self.assertTrue(self.bridge.started.wait(2))
        path = f"/api/ai/tasks/{task['task_id']}/cancel"
        cancel = {"request_id":uuid4().hex,"round_id":task["rounds"][-1]["round_id"]}
        self.assertEqual(self.request("POST", path, cancel)[0], 200)
        stopped = self.wait(task["task_id"])
        self.bridge.started.clear()
        follow = {"request_id":uuid4().hex,"task_revision":stopped["revision"],"instruction":"新的一轮"}
        self.assertEqual(self.request("POST", f"/api/ai/tasks/{task['task_id']}/continue", follow)[0], 200)
        self.assertTrue(self.bridge.started.wait(2))
        status, replay = self.request("POST", path, cancel)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["task"]["status"], "running")
        self.assertEqual(len(replay["task"]["rounds"]), 2)
        self.assertEqual(self.request("POST", path, {**cancel,"request_id":uuid4().hex})[0], 409)
        self.assertEqual(self.request("POST", path, {"request_id":uuid4().hex})[0], 400)

    def test_ai_mutations_share_existing_request_guards(self):
        for body, authenticated, headers in ((self.body(), False, {}), (self.body(), True, {"Origin":"https://example.invalid"})):
            status, value = self.request("POST", "/api/ai/tasks", body, authenticated, headers)
            self.assertEqual(status, 403, value)
        self.assertEqual(self.request("GET", "/api/ai/status")[1]["available"], True)
        self.assertEqual(self.request("GET", "/api/ai/tasks?case_id=invalid")[0], 400)
        self.assertEqual(self.request("GET", "/api/ai/tasks/AIT-000000000000")[0], 404)


if __name__ == "__main__":
    unittest.main()
