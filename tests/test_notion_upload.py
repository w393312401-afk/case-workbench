"""Notion writes are simulated; all receipts/videos use disposable directories."""
from contextlib import contextmanager
import copy
import json
from pathlib import Path
import tempfile
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

    @contextmanager
    def client(self):
        yield self

    def call(self, action, args):
        self.calls.append((action, copy.deepcopy(args)))
        if action == self.fail:
            raise VoiceError(502, "测试中断")
        if action == "get_tool_access":
            return {"current_tool_access": {"query_data_sources": {"status": "available"}}}
        if action == "fetch":
            if args["id"].startswith("collection://"):
                if notion_id(args["id"]) == notion_id(ACCOUNTS):
                    state = {"schema": {name: {} for name in ("名称", "Username", "Nick Name")}}
                else:
                    state = {"url": "collection://" + DEST, "schema": {name: {} for name in ("名称", "文件和媒体", "日期", "标题＋tags", "发布账号")}}
                    state["schema"]["发布账号"] = {"dataSourceUrl": "collection://" + ACCOUNTS}
                return {"text": "<data-source-state>" + json.dumps(state) + "</data-source-state>"}
            page = self.pages[notion_id(args["id"])]
            props = copy.deepcopy(page["properties"])
            props["发布账号"] = json.loads(props["发布账号"])
            if self.bad_readback:
                props["date:日期:start"] = "2026-09-28"
            return {"url": "https://app.notion.com/p/" + notion_id(args["id"]), "text":
                f'<parent-data-source url="collection://{DEST}"/><properties>{json.dumps(props)}</properties>\n' + ("" if self.bad_body else page["content"])}
        if action == "query_data_sources":
            data = args["data"]
            if "Username" in data["query"]:
                return {"results": [{"url": "https://app.notion.com/p/" + ACCOUNT, "Username": "fixture"}]}
            # Queries must use the canonical table from the fetched schema, including its hyphens.
            assert data["data_source_urls"] == ["collection://" + DEST]
            return {"results": [{"url": "https://app.notion.com/p/" + uid} for uid, page in self.pages.items() if page["properties"]["名称"] == data["params"][0]]}
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
        self.root = Path(temp.name)
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


class NotionUploadHTTPTests(unittest.TestCase):
    def setUp(self):
        import test_server as fixture
        self.fixture = fixture
        fixture.WorkbenchHTTPTests.setUp(self)

    def request(self, *args, **kwargs):
        return self.fixture.WorkbenchHTTPTests.request(self, *args, **kwargs)

    def stop_server(self):
        return self.fixture.WorkbenchHTTPTests.stop_server(self)

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


if __name__ == "__main__":
    unittest.main()
