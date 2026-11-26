"""Notion writes are simulated; all receipts/videos use disposable directories."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from elevenlabs_client import VoiceError
from notion_bridge import NotionBridge, notion_id, send_video
from workbench_notion import NotionUploads, body_markdown
from test_video_publication import Bench, JOB, SCRIPT, TAKE, VIDEO


DEST = "11111111-1111-4111-8111-111111111111"
ACCOUNTS = "22222222-2222-4222-8222-222222222222"
PAGE = "33333333-3333-4333-8333-333333333333"
ACCOUNT = "44444444-4444-4444-8444-444444444444"
FILE = "55555555-5555-4555-8555-555555555555"


class FakeNotion:
    def __init__(self):
        self.calls, self.pages = [], {}
        self.fail = ""
        self.bad_readback = False
        self.bad_body = False
        self.account_field = "发布账号"
        self.query_access = "available"

    @contextmanager
    def client(self):
        yield self

    def call(self, action, args):
        self.calls.append((action, copy.deepcopy(args)))
        if action == self.fail:
            raise VoiceError(502, "测试中断")
        if action == "get_tool_access":
            return {"current_tool_access": {"query_data_sources": {"status": self.query_access}}}
        if action == "fetch":
            if args["id"].startswith("collection://"):
                if notion_id(args["id"]) == notion_id(ACCOUNTS):
                    state = {"schema": {name: {} for name in ("名称", "Username", "Nick Name")}}
                else:
                    state = {"url": "collection://" + DEST, "schema": {name: {} for name in ("名称", "文件和媒体", "日期", "标题＋tags", "发布账号", "视频状态")}}
                    state["schema"][self.account_field] = {"dataSourceUrl": "collection://" + ACCOUNTS}
                return {"text": "<data-source-state>" + json.dumps(state) + "</data-source-state>"}
            page = self.pages[notion_id(args["id"])]
            props = copy.deepcopy(page["properties"])
            props[self.account_field] = json.loads(props[self.account_field])
            if self.bad_readback:
                props["date:日期:start"] = "2026-09-28"
            return {"url": "https://app.notion.com/p/" + notion_id(args["id"]), "text":
                f'<parent-data-source url="collection://{DEST}"/><properties>{json.dumps(props)}</properties>\n' + ("" if self.bad_body else page["content"])}
        if action == "query_data_sources":
            data = args["data"]
            if "Username" in data["query"]:
                return {"results": [{"url": "https://app.notion.com/p/" + ACCOUNT, "Username": "fixture"}] if "fixture" in data["params"] else []}
            # Queries must use the canonical table from the fetched schema, including its hyphens.
            assert data["data_source_urls"] == ["collection://" + DEST]
            return {"results": [{"url": "https://app.notion.com/p/" + uid} for uid, page in self.pages.items() if page["properties"]["名称"] in data["params"]]}
        if action == "create_pages":
            self.pages[notion_id(PAGE)] = copy.deepcopy(args["pages"][0])
            return {"pages": [{"id": PAGE, "url": "https://app.notion.com/p/" + PAGE}]}
        if action == "create_file_upload":
            return {"file_upload_id": FILE, "filename": args["filename"], "content_type": args["content_type"]}
        if action == "update_page":
            page = self.pages[notion_id(args["page_id"])]
            if args["command"] == "update_properties":
                page["properties"].update(args["properties"])
            else:
                page["content"] = args["new_str"]
            return {"page_id": args["page_id"]}
        raise AssertionError(action)


class NotionUploadTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.app = Bench(self.root)
        self.app.calendar = {}
        self.app.content_calendar = lambda query: self.app.calendar.get(query["id"][0], {})
        self.app.set_content_calendar = lambda body: self.app.calendar.update({body["id"]: body})
        data = self.app.production.empty()
        data["jobs"] = [{"id": JOB, "product": "202H", "script_id": SCRIPT, "script_version": 1,
            "selected_take": TAKE, "takes": [{"id": TAKE}], "lines": [{"spoken": "Use this angle.", "zh": "用这个角度。"}],
            "supplement": {"captions": ["🔴Lock the angle#SHARPAL202H"], "hook_text": "LOCK THE ANGLE", "hook_text_zh": "锁住角度"},
            "timelines": [{"unique_id": "timeline-1", "name": "Fixture261103-202-1", "creator": "Fixture", "take_id": TAKE, "saved": True}],
            "edits": [], "archived": False, "history": []}]
        self.app.production.write(data)
        data = self.app.creation.empty()
        data["scripts"] = [{"id": SCRIPT, "text": "Wrong stale script.", "version": 1, "source": {}}]
        self.app.creation.write(data)
        self.video = self.root / "final.mp4"
        self.video.write_bytes(VIDEO)
        self.bridge = FakeNotion()
        self.manager = NotionUploads(self.app, {"data_source_id": DEST, "accounts": {"Fixture": "fixture"}}, self.bridge)
        self.addCleanup(patch.stopall)
        patch("workbench_notion.threading.Thread").start()
        patch("workbench_notion.send_video", return_value={"status": "uploaded", "file_upload_id": FILE}).start()

    def body(self, **extra):
        return {"request_id": uuid4().hex, "timeline_uid": "timeline-1", "file_path": str(self.video),
            "published_on": "2026-11-03", "account": "fixture", "title": "🔴Lock the angle#SHARPAL202H",
            "screen_en": "LOCK THE ANGLE", "screen_zh": "锁住角度", **extra}

    def run_upload(self, body=None):
        started = self.manager.start(JOB, body or self.body())
        self.manager._run(started["id"])
        return self.manager.store.read()["uploads"][started["id"]]

    def test_upload_attaches_file_and_sets_real_date_account_title_and_bilingual_body(self):
        row = self.run_upload()
        self.assertEqual("completed", row["status"])
        page = self.bridge.pages[notion_id(PAGE)]
        self.assertEqual("2026-11-03", page["properties"]["date:日期:start"])
        self.assertEqual("🔴Lock the angle#SHARPAL202H", page["properties"]["标题＋tags"])
        self.assertTrue(page["properties"]["文件和媒体"])
        self.assertIn("Use this angle.", page["content"])
        self.assertIn("用这个角度。", page["content"])
        self.assertIn("LOCK THE ANGLE", page["content"])
        self.assertNotIn("Wrong stale script.", page["content"])
        self.assertNotIn("<video", page["content"])
        self.assertEqual(VIDEO, self.manager.store.managed_path(row["file"]).read_bytes())
        self.assertTrue(self.app.calendar["video:" + JOB]["calendar_uploaded"])
        self.assertFalse(self.manager.busy())

    def test_page_name_follows_the_release_naming_and_market(self):
        # 成品统一命名（2026-11-24）：国旗 形式 产品-达人-日期-序号。
        self.assertEqual(("🇺🇸 ▶️ 202-Fixture-1103-1", "US-202-Fixture-1103-1"),
                         (lambda d: (d["name"], d["file_name"]))(self.manager.detail(JOB, "timeline-1")))
        data = self.app.production.read()
        data["jobs"][0]["market"] = "de"
        self.app.production.write(data)
        self.run_upload()
        self.assertEqual("🇩🇪 ▶️ 202-Fixture-1103-1", self.bridge.pages[notion_id(PAGE)]["properties"]["名称"])

    def test_a_page_made_under_the_old_name_is_found_and_renamed(self):
        # An interrupted upload from before the new names left a page named after the timeline and no page id.
        self.bridge.pages[notion_id(PAGE)] = {"properties": {"名称": "Fixture261103-202-1", "标题＋tags": "", "发布账号": "[]"}, "content": ""}
        self.run_upload()
        self.assertEqual(1, len(self.bridge.pages))
        self.assertEqual("🇺🇸 ▶️ 202-Fixture-1103-1", self.bridge.pages[notion_id(PAGE)]["properties"]["名称"])

    def test_connection_is_remembered_and_tools_are_awaited(self):
        import notion_bridge
        from notion_bridge import NotionBridge
        cache = self.root / "connection.json"
        bridge = NotionBridge(cache_path=cache)
        states = iter([{"authStatus": "oAuth"}, {"authStatus": "oAuth", "tools": {"fetch": {"name": "fetch"}}}])
        starts = []

        @contextmanager
        def connection(**_):
            starts.append(1)
            if len(starts) == 1:
                raise VoiceError(503, "startup failed")
            yield object(), "notion"
        with patch.object(bridge, "connection", connection), patch.object(bridge, "_state", lambda *_: next(states)), \
                patch("notion_bridge.time.sleep"):
            with bridge.client() as client:
                self.assertIn("fetch", client.tools)
        self.assertEqual(2, len(starts))  # one failed start is retried
        # A fresh bridge (as after a restart) answers from the file without starting Codex.
        again = NotionBridge(cache_path=cache)
        with patch.object(again, "connection", side_effect=AssertionError("no live check")):
            self.assertEqual("connected", again.status()["status"])
        # Losing authorisation clears it.
        with patch.object(again, "connection", lambda **_: contextmanager(lambda: (yield object(), "notion"))()), \
                patch.object(again, "_state", lambda *_: {"authStatus": "notLoggedIn"}):
            with self.assertRaises(VoiceError):
                with again.client():
                    pass
        self.assertIsNone(NotionBridge(cache_path=cache)._cached())
        self.assertTrue(notion_bridge.CONNECTED_FOR > 0)

    def test_new_page_is_ready_and_a_set_status_is_kept(self):
        self.run_upload()
        page = self.bridge.pages[notion_id(PAGE)]
        self.assertEqual("就绪", page["properties"]["视频状态"])
        page["properties"]["视频状态"] = "已发布"
        self.assertEqual("completed", self.run_upload(self.body(title="🔴Another title#tag"))["status"])
        self.assertEqual("已发布", page["properties"]["视频状态"])

    def sql_calls(self):
        return [args for action, args in self.bridge.calls if action == "query_data_sources"]

    def test_account_is_looked_up_once_then_reused(self):
        self.run_upload()
        first = [call for call in self.sql_calls() if "Username" in call["data"]["query"]]
        self.assertEqual(1, len(first))
        self.assertTrue((self.manager.store.root / "accounts.json").is_file())
        before = len(self.sql_calls())
        # The second upload updates the known page with the remembered account: no SQL at all.
        row = self.run_upload(self.body(title="🔴Another title#tag"))
        self.assertEqual("completed", row["status"])
        self.assertEqual(before, len(self.sql_calls()))
        page = self.bridge.pages[notion_id(PAGE)]
        self.assertIn(ACCOUNT.replace("-", ""), page["properties"]["发布账号"].replace("-", ""))

    def test_failed_readback_forgets_the_remembered_account(self):
        self.run_upload()
        self.bridge.bad_readback = True
        self.assertEqual("failed", self.run_upload(self.body(title="🔴Another#tag"))["status"])
        self.assertEqual({}, json.loads((self.manager.store.root / "accounts.json").read_text(encoding="utf-8"))[notion_id("collection://" + ACCOUNTS)])

    def test_exhausted_query_precheck_says_quota_and_keeps_the_remembered_account(self):
        key = notion_id("collection://" + ACCOUNTS)
        self.manager._remember_account(key, "fixture", "https://app.notion.com/p/" + ACCOUNT)
        remembered = json.loads((self.manager.store.root / "accounts.json").read_text(encoding="utf-8"))[key]
        # A new page still needs one lookup; the hourly allowance is used up at that moment.
        self.bridge.query_access = "usage_limit_reached"
        with patch("sys.stderr"):
            row = self.run_upload()
        self.assertEqual("failed", row["status"])
        self.assertIn("额度", row["error"])
        self.assertNotIn("权限", row["error"])
        self.assertEqual(remembered, json.loads((self.manager.store.root / "accounts.json").read_text(encoding="utf-8"))[key])

    def test_other_query_precheck_status_is_shown(self):
        self.bridge.query_access = "unavailable"
        with patch("sys.stderr"):
            row = self.run_upload()
        self.assertEqual("failed", row["status"])
        self.assertIn("unavailable", row["error"])

    def test_query_quota_error_is_named_plainly(self):
        from notion_bridge import unpack
        limit = {"isError": True, "content": [{"type": "text", "text": 'used up its Query Data Source allowance {"error":{"code":"usage_limit_reached"}}'}]}
        with self.assertRaises(VoiceError) as caught:
            unpack(limit)
        self.assertIn("额度", str(caught.exception))
        # Other errors name the step and keep what Notion said.
        with self.assertRaises(VoiceError) as caught:
            unpack({"isError": True, "content": [{"type": "text", "text": "Could not find page  with ID abc"}]}, "update_page")
        self.assertIn("更新排期页面", str(caught.exception))
        self.assertIn("Could not find page with ID abc", str(caught.exception))
        with self.assertRaises(VoiceError) as caught:
            unpack({"isError": True, "content": []})
        self.assertIn("权限", str(caught.exception))

    def test_a_creators_last_successful_account_is_offered_next_time(self):
        self.manager.settings["accounts"] = {}
        self.assertEqual(self.manager.detail(JOB, "timeline-1")["account"], "Fixture")
        failed = self.run_upload(self.body(account="lab"))
        self.assertIn("发布账号", failed["error"])
        # The account Notion could not find is not offered again.
        self.assertEqual(self.manager.detail(JOB, "timeline-1")["account"], "Fixture")
        self.assertEqual(self.run_upload(self.body(account="fixture"))["status"], "completed")
        data = self.app.production.read()
        data["jobs"][0]["timelines"].append({"unique_id": "timeline-2", "name": "Fixture261104-202-1", "creator": "Fixture", "take_id": TAKE, "saved": True})
        self.app.production.write(data)
        self.assertEqual(self.manager.detail(JOB, "timeline-2")["account"], "fixture")

    def test_new_account_relation_is_preferred_over_old_one(self):
        # The calendar keeps its old 「发布账号」 column (no relation now) next to the new 「账号」 relation.
        self.bridge.account_field = "账号"
        row = self.run_upload()
        self.assertEqual("completed", row["status"])
        page = self.bridge.pages[notion_id(PAGE)]
        self.assertIn(ACCOUNT.replace("-", ""), page["properties"]["账号"].replace("-", ""))
        self.assertNotIn("发布账号", page["properties"])

    def test_detail_prefills_newest_export_and_title_chinese(self):
        import os
        exports = self.app.production.root / "exports"
        exports.mkdir(parents=True, exist_ok=True)
        old = exports / "Fixture261103-202-1.mp4"
        old.write_bytes(VIDEO)
        os.utime(old, (1_000_000, 1_000_000))
        newest = self.root / "preview-hd.mp4"
        newest.write_bytes(VIDEO)
        self.app.video_previews = type("Previews", (), {"export_file": lambda _, job, uid: newest})()
        self.app._title_translations = lambda: {"Lock the angle": "锁住角度"}
        detail = self.manager.detail(JOB, "timeline-1")
        self.assertEqual(str(newest), detail["file_path"])
        self.assertEqual(["锁住角度"], detail["copy"]["titles_zh"])
        os.utime(newest, (500_000, 500_000))
        self.assertEqual(str(old), self.manager.detail(JOB, "timeline-1")["file_path"])

    def test_same_request_replays_and_new_upload_updates_original_page(self):
        body = self.body()
        first = self.run_upload(body)
        calls = len(self.bridge.calls)
        self.assertEqual(first["id"], self.manager.start(JOB, body)["id"])
        self.assertEqual(calls, len(self.bridge.calls))
        second = self.run_upload(self.body(title="🔴Another title#tag"))
        self.assertEqual(first["page_id"], second["page_id"])
        self.assertEqual(1, sum(action == "create_pages" for action, _ in self.bridge.calls))

    def test_failure_after_page_creation_resumes_same_page_without_false_upload_mark(self):
        self.bridge.fail = "create_file_upload"
        body = self.body()
        failed = self.run_upload(body)
        self.assertEqual("failed", failed["status"])
        self.assertTrue(failed["page_id"])
        self.assertEqual({}, self.app.calendar)
        self.bridge.fail = ""
        saved = self.run_upload(body)
        self.assertEqual("completed", saved["status"])
        self.assertEqual(1, sum(action == "create_pages" for action, _ in self.bridge.calls))

    def test_wrong_date_readback_is_not_marked_uploaded(self):
        self.bridge.bad_readback = True
        row = self.run_upload()
        self.assertEqual("failed", row["status"])
        self.assertIn("日期", row["error"])
        self.assertEqual({}, self.app.calendar)

    def test_missing_body_is_not_reported_as_completed(self):
        self.bridge.bad_body = True
        row = self.run_upload()
        self.assertEqual("failed", row["status"])
        self.assertEqual({}, self.app.calendar)

    def test_one_running_upload_per_timeline_and_conflicting_request_is_refused(self):
        body = self.body()
        self.manager.start(JOB, body)
        for other in (self.body(), {**body, "title": "different"}):
            with self.assertRaises(VoiceError) as caught:
                self.manager.start(JOB, other)
            self.assertEqual(409, caught.exception.status)

    def test_other_timeline_and_missing_fields_never_start_upload(self):
        for extra in ({"timeline_uid": "unrelated"}, {"published_on": "2026-02-30"}, {"account": ""}, {"title": ""}):
            with self.assertRaises(VoiceError):
                self.manager.start(JOB, self.body(**extra))
        self.assertFalse(self.manager.busy())
        self.assertEqual([], self.bridge.calls)

    def test_video_sender_refuses_remote_urls_without_sending_bytes(self):
        with patch("notion_bridge.build_opener") as opener:
            with self.assertRaises(VoiceError):
                send_video({"file_upload_id": FILE, "upload_url": f"https://example.com/v1/mcp/file_uploads/{FILE}/send"}, self.video)
            opener.assert_not_called()

    def test_markdown_fences_preserve_literal_script_text(self):
        result = body_markdown({"en": "literal ``` text", "zh": "中文", "screen_en": "HOOK"})
        self.assertIn("````text\nliteral ``` text\n````", result)

    def test_connector_uses_direct_mcp_calls_without_starting_a_model_turn(self):
        opened, rpc = [], []
        class Session:
            thread_id = None
            def request(self, method, params, **kwargs):
                rpc.append((method, params))
                if method == "config/read":
                    return {"config": {"mcp_servers": {"notion": {"url": "https://mcp.notion.com/mcp"}, "unrelated": {"command": "other"}}}}
                if method == "thread/start":
                    return {"thread": {"id": "temporary-notion"}}
                if method == "mcpServerStatus/list":
                    return {"data": [{"name": "notion", "tools": {"mcp__notion__notion_fetch": {"name": "notion-fetch"}}}]}
                if method == "mcpServer/tool/call":
                    return {"content": [{"type": "text", "text": '{"title":"Fixture"}'}]}
                return {}
            def send(self, value):
                pass
            def close(self):
                pass
        def start(directory, cancel, progress, event, overrides, guards, timeout):
            opened.append(overrides)
            return Session()
        with patch("notion_bridge.CodexStillsBridge._restricted_session", side_effect=start):
            with NotionBridge().client() as client:
                self.assertEqual({"title": "Fixture"}, client.call("fetch", {"id": PAGE}))
        self.assertFalse(any(method == "turn/start" for method, _ in rpc))
        self.assertEqual({"enabled": False}, opened[-1]["mcp_servers"]["unrelated"])
        tool = next(params for method, params in rpc if method == "mcpServer/tool/call")
        self.assertEqual("notion", tool["server"])
        self.assertEqual("temporary-notion", tool["threadId"])


class NotionConnectionErrorTests(unittest.TestCase):
    """The page must say which side failed instead of one generic 授权未完成 line."""

    class Session:
        thread_id = None
        def __init__(self, outcome):
            self.outcome = outcome
        def request(self, method, params, **kwargs):
            if method == "config/read":
                return {"config": {"mcp_servers": {"notion": {"url": "https://mcp.notion.com/mcp"}}}}
            if method == "mcpServer/oauth/login":
                return {"authorizationUrl": "https://mcp.notion.com/authorize?secret-state=do-not-show"}
            return {}
        def notification(self, method, params):
            pass
        def pump(self, deadline=None):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            self.notification("mcpServer/oauthLogin/completed", {"name": "notion", **self.outcome})
        def close(self):
            pass

    def bridge(self, start):
        patcher = patch("notion_bridge.CodexStillsBridge._restricted_session", side_effect=start)
        patcher.start()
        self.addCleanup(patcher.stop)
        return NotionBridge()

    def finished_login(self, outcome):
        bridge = self.bridge(lambda *args: self.Session(outcome))
        first = bridge.connect()
        for _ in range(200):
            with bridge.lock:
                if bridge.login.get("status") not in {"waiting", "preparing"}:
                    return first, dict(bridge.login)
            threading.Event().wait(0.01)
        self.fail("login did not finish")

    def test_codex_refusing_its_config_names_the_field_on_the_page(self):
        from codex_bridge import BridgeError
        def start(*args):
            raise BridgeError("本机 Codex 配置里有当前版本不认识的设置「computer_use.windows.always_allowed_app_ids」，未启动。", code="CODEX_CONFIG")
        state = self.bridge(start).status(refresh=True)
        self.assertEqual("unavailable", state["status"])
        self.assertTrue(state["error"].startswith("本机 Codex 启动失败"))
        self.assertIn("computer_use.windows.always_allowed_app_ids", state["error"])
        login = self.bridge(start).connect()
        self.assertEqual("disconnected", login["status"])
        self.assertIn("本机 Codex 启动失败", login["error"])

    def test_codex_login_problem_is_named(self):
        from codex_bridge import BridgeError
        def start(*args):
            raise BridgeError("本机 Codex 登录暂不可用。", code="CODEX_AUTH")
        self.assertIn("Codex 检查登录", self.bridge(start).status(refresh=True)["error"])

    def test_denied_timed_out_and_failed_authorizations_are_told_apart(self):
        from codex_bridge import BridgeError
        cases = [({"success": False, "error": "OAuth error: access_denied"}, "没有同意"),
                 ({"success": False, "error": "timed out waiting for callback"}, "授权超时"),
                 (BridgeError("Codex 响应超时。", code="CODEX_TIMEOUT"), "授权超时"),
                 ({"success": False, "error": "token exchange failed: https://example.invalid/?code=secret"}, "Codex 报告授权失败")]
        for outcome, expected in cases:
            with self.subTest(expected=expected):
                _, final = self.finished_login(outcome)
                self.assertEqual("disconnected", final["status"])
                self.assertIn(expected, final["error"])
                self.assertNotIn("secret", final["error"])

    def test_successful_authorization_has_no_error(self):
        _, final = self.finished_login({"success": True})
        self.assertEqual({"status": "connected"}, final)


class PhotoNotionUploadTests(unittest.TestCase):
    """挂车图文: the exported 发布 JPGs and frozen copy go to the same calendar, recorded apart from video uploads."""
    SET = "PHS-aaaaaaaaaaaa"

    def setUp(self):
        NotionUploadTests.setUp(self)  # same fixture order, Notion fake and settings as the video tests
        self.exports = []
        self.data = {"id": self.SET, "job_id": JOB, "timeline_uid": "timeline-1", "timeline_name": "Fixture261103-202-1",
                     "current": "A", "works": [{"key": "A", "name_zh": "达人自述", "exports": self.exports}]}
        self.app.photo_sets = type("Sets", (), {"_read": lambda _, set_id: copy.deepcopy(self.data), "ensure_open": lambda _, set_id: None})()

    def export(self, name, slides=2):
        folder = self.root / "图文成品" / name
        (folder / "发布JPG").mkdir(parents=True)
        for number in range(1, slides + 1):
            (folder / "发布JPG" / f"{number:02d}.jpg").write_bytes(bytes([255, 216]) + bytes(40) + bytes([number]))
        (folder / "copy.json").write_text(json.dumps({
            "titles": ["🔴I found the next level#SHARPAL202H", "🔴Second title#tag"], "titles_zh": ["我找到了下一级", "第二个"],
            "caption_en": "Seven plates in one case.", "caption_zh": "一个盒子装七块磨板。",
            "slides": [{"number": n, "title_lines": [f"SLIDE {n} TITLE"], "body": f"Slide {n} line.", "title_zh": f"第 {n} 张", "body_zh": ""}
                       for n in range(1, slides + 1)]}, ensure_ascii=False), encoding="utf-8")
        self.exports.append({"folder": str(folder), "name": name, "at": "2026-11-08T00:00:00+00:00", "style": "bar"})
        return folder

    def photo_body(self, **extra):
        return {"request_id": uuid4().hex, "work": "A", "published_on": "2026-11-09", "account": "fixture",
                "title": "🔴I found the next level#SHARPAL202H", **extra}

    def run_photo(self, body=None):
        started = self.manager.photo_start(self.SET, body or self.photo_body())
        self.manager._photo_run(started["id"])
        return self.manager.photo_store.read()["uploads"][started["id"]]

    def test_every_picture_goes_into_files_and_the_body_holds_copy_and_slide_text(self):
        self.export("C1-20261108-A达人自述", slides=3)
        detail = self.manager.photo_detail(self.SET, "A")
        self.assertEqual(("", 3, "🇺🇸 🧩 202-Fixture-1103-1A", "fixture"), (detail["blocked_reason"], detail["pictures"], detail["name"], detail["account"]))
        self.assertEqual("🔴I found the next level#SHARPAL202H", detail["title"])
        row = self.run_photo()
        self.assertEqual("completed", row["status"])
        uploads = [args for action, args in self.bridge.calls if action == "create_file_upload"]
        self.assertEqual(["image/jpeg"] * 3, [args["content_type"] for args in uploads])
        self.assertEqual("US-202-Fixture-1103-1A-01.jpg", uploads[0]["filename"])
        page = self.bridge.pages[notion_id(PAGE)]
        self.assertEqual(3, len(page["properties"]["文件和媒体"]))
        self.assertEqual(("2026-11-09", "🇺🇸 🧩 202-Fixture-1103-1A", "就绪"),
                         (page["properties"]["date:日期:start"], page["properties"]["名称"], page["properties"]["视频状态"]))
        for text in ("Seven plates in one case.", "一个盒子装七块磨板。", "SLIDE 2 TITLE", "Slide 3 line.", "第 1 张"):
            self.assertIn(text, page["content"])
        # Kept apart from video receipts: the video's own state is untouched.
        self.assertEqual({}, self.manager.latest(JOB, "timeline-1"))
        self.assertEqual({"A": "completed"}, self.manager.photo_summary()[self.SET])
        self.assertNotIn("video:" + JOB, self.app.calendar)
        self.assertFalse(self.manager.busy())

    def test_an_export_made_before_titles_offers_the_caption_line(self):
        folder = self.export("C1-20261105-A")
        frozen = json.loads((folder / "copy.json").read_text(encoding="utf-8"))
        frozen.pop("titles")
        frozen["caption_en"] = "Stop guessing the angle. It locks in. #knife #sharpal"
        (folder / "copy.json").write_text(json.dumps(frozen), encoding="utf-8")
        self.assertEqual("🔴Stop guessing the angle.#knife #sharpal", self.manager.photo_detail(self.SET, "A")["title"])

    def test_forgetting_a_deleted_carousel_only_drops_its_own_receipts(self):
        rows = {"NPH-000000000001": {"id": "NPH-000000000001", "job_id": "PRD-000000000001", "set_id": "PHS-000000000001", "page_id": "p1"},
                "NPH-000000000002": {"id": "NPH-000000000002", "job_id": "PRD-000000000001", "set_id": "PHS-000000000002", "page_id": "p2"}}
        self.manager.photo_store.write({"schema_version": 1, "uploads": rows})
        self.assertEqual([row["page_id"] for row in self.manager.photo_receipts({"PHS-000000000001"})], ["p1"])
        self.manager.running.add(("PRD-000000000001", "photo:PHS-000000000001:A"))
        self.assertTrue(self.manager.photo_busy({"PHS-000000000001"}))
        self.assertFalse(self.manager.photo_busy({"PHS-000000000002"}))
        self.manager.running.clear()
        self.manager.forget_photo({"PHS-000000000001"})
        self.assertEqual(list(self.manager.photo_store.read()["uploads"]), ["NPH-000000000002"])

    def test_not_exported_yet_cannot_upload(self):
        self.assertIn("先定稿导出", self.manager.photo_detail(self.SET, "A")["blocked_reason"])
        with self.assertRaises(VoiceError) as caught:
            self.manager.photo_start(self.SET, self.photo_body())
        self.assertEqual(409, caught.exception.status)
        self.assertEqual([], self.bridge.calls)

    def test_a_newer_export_is_flagged_and_updates_the_same_page(self):
        self.export("C1-20261108-A达人自述")
        self.run_photo()
        self.export("C1-20261108-A达人自述-v2", slides=4)
        detail = self.manager.photo_detail(self.SET, "A")
        self.assertEqual(("C1-20261108-A达人自述", "C1-20261108-A达人自述-v2"), (detail["receipt"]["export_name"], detail["export_name"]))
        self.assertEqual("completed", self.run_photo()["status"])
        self.assertEqual(1, len(self.bridge.pages))
        self.assertEqual(4, len(self.bridge.pages[notion_id(PAGE)]["properties"]["文件和媒体"]))

    def test_failed_readback_is_not_marked_uploaded_and_a_restart_reports_interruption(self):
        self.export("C1-20261108-A达人自述")
        self.bridge.bad_body = True
        self.assertEqual("failed", self.run_photo()["status"])
        started = self.manager.photo_start(self.SET, self.photo_body(account="fixture "))
        self.assertTrue(self.manager.busy(JOB))
        with self.assertRaises(VoiceError):
            self.manager.photo_start(self.SET, self.photo_body())
        again = NotionUploads(self.app, {"data_source_id": DEST}, self.bridge)
        self.assertEqual("interrupted", again.photo_store.read()["uploads"][started["id"]]["status"])

    def test_wrong_version_and_extra_fields_are_refused(self):
        self.export("C1-20261108-A达人自述")
        for body in (self.photo_body(work="Z"), self.photo_body(work="AB"), self.photo_body(file_path="C:/x.jpg"), self.photo_body(title=" ")):
            with self.assertRaises(VoiceError):
                self.manager.photo_start(self.SET, body)


class NotionUploadHTTPTests(unittest.TestCase):
    def setUp(self):
        import test_server as fixture
        self.fixture = fixture
        fixture.WorkbenchHTTPTests.setUp(self)

    def request(self, *args, **kwargs):
        return self.fixture.WorkbenchHTTPTests.request(self, *args, **kwargs)

    def stop_server(self):
        return self.fixture.WorkbenchHTTPTests.stop_server(self)

    def test_index_serves_notion_upload_and_all_its_script_sources(self):
        status, _, index = self.request("GET", "/")
        self.assertEqual(200, status)
        sources = re.findall(r'<script\b[^>]*\bsrc="([^"]+)"', index.decode("utf-8"))
        paths = []
        for source in sources:
            with self.subTest(source=source):
                status, headers, content = self.request("GET", source)
                self.assertEqual(200, status)
                self.assertIn("javascript", headers["Content-Type"])
                self.assertTrue(content)
                # The program bundle retains the source name for each script it executes.
                units = re.findall(r'//# sourceURL=(/[\w.-]+\.js)"', content.decode("utf-8"))
                paths.extend(units or [source.split("?", 1)[0]])
        self.assertIn("/notion-upload.js", paths)
        self.assertIn("/video-publication.js", paths)
        self.assertLess(paths.index("/notion-upload.js"), paths.index("/video-publication.js"))

    def test_routes_pass_the_chosen_timeline_and_do_not_register_a_post(self):
        manager = self.server.app.notion_uploads
        with patch.object(manager, "detail", return_value={"timeline_uid": "one"}) as detail:
            status, _, data = self.request("GET", f"/api/production/jobs/{JOB}/notion?timeline=one")
            self.assertEqual(200, status)
            self.assertEqual("one", data["timeline_uid"])
            detail.assert_called_once_with(JOB, "one")
        with patch.object(manager, "start", return_value={"status": "running"}) as start:
            body = {"timeline_uid": "one", "request_id": uuid4().hex}
            status, _, data = self.request("POST", f"/api/production/jobs/{JOB}/notion", body)
            self.assertEqual(202, status)
            start.assert_called_once_with(JOB, body)
        self.assertEqual({}, self.server.app._video_publications().read()["jobs"])

    def test_connection_is_csrf_protected_and_uses_the_connection_manager(self):
        with patch.object(self.server.app.notion_uploads.bridge, "connect", return_value={"status": "waiting"}) as connect:
            status, _, _ = self.request("POST", "/api/notion/connect", {}, authenticated=False)
            self.assertEqual(403, status)
            connect.assert_not_called()
            status, _, result = self.request("POST", "/api/notion/connect", {})
            self.assertEqual(200, status)
            self.assertEqual("waiting", result["status"])

    def test_missing_or_multiple_timeline_queries_are_rejected(self):
        for suffix in ("", "?timeline=one&timeline=two", "?timeline=one&extra=value"):
            status, _, _ = self.request("GET", f"/api/production/jobs/{JOB}/notion{suffix}")
            self.assertEqual(400, status)


class ReceiptCacheTests(unittest.TestCase):
    """Receipts are validated once per file version; callers get private copies."""

    def test_unchanged_file_is_not_revalidated_and_a_rewrite_is_read_again(self):
        from workbench_notion import NotionUploadStore
        with tempfile.TemporaryDirectory() as folder:
            store = NotionUploadStore(folder)
            self.assertEqual({}, store.read()["uploads"])
            store.root.mkdir(parents=True)
            row = {"id": "NUP-" + "a" * 12, "job_id": "PRD-" + "b" * 12, "timeline_uid": "t1", "status": "completed"}
            store.path.write_text(json.dumps({"schema_version": 1, "uploads": {row["id"]: row}}), encoding="utf-8")
            first = store.read()
            first["uploads"][row["id"]]["status"] = "changed by caller"
            with patch.object(NotionUploadStore, "_read_validated", side_effect=AssertionError("re-validated")):
                self.assertEqual("completed", store.read()["uploads"][row["id"]]["status"])
            store.path.write_text(json.dumps({"schema_version": 1, "uploads": {}}) + "\n", encoding="utf-8")
            self.assertEqual({}, store.read()["uploads"])
            store.path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(VoiceError):
                store.read()


if __name__ == "__main__":
    unittest.main()
