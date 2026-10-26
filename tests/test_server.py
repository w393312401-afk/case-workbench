"""HTTP integration checks; every write stays inside a temporary case library."""
from __future__ import annotations

import copy
import http.client
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
from uuid import uuid4


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workbench_server_under_test", APP_ROOT / "server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)

# Chromium rejects these ports even for localhost. Windows can include them in
# its ephemeral range; 2049 caused ERR_UNSAFE_PORT in the browser suite.
# Source: chromium/net/base/port_util.cc (kRestrictedPorts).
CHROMIUM_BLOCKED_PORTS = {
    0, 1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53, 69,
    77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117, 119,
    123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514, 515,
    526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989, 990,
    993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000, 6566,
    6665, 6666, 6667, 6668, 6669, 6697, 10080,
}


def make_test_server(skill, runtime):
    for _ in range(32):
        server = SERVER.make_server(skill, runtime, port=0, allow_open=False)
        if server.server_port not in CHROMIUM_BLOCKED_PORTS:
            return server
        server.server_close()
    raise RuntimeError("Could not bind a browser-safe ephemeral test port")


class BrowserPortFixtureTests(unittest.TestCase):
    def test_blocked_listener_is_closed_before_browser_safe_port_is_used(self):
        blocked = Mock(server_port=2049)
        accepted = Mock(server_port=49152)
        with patch.object(SERVER, "make_server", side_effect=[blocked, accepted]) as make:
            self.assertIs(make_test_server("fixture-skill", "fixture-runtime"), accepted)
        blocked.server_close.assert_called_once_with()
        accepted.server_close.assert_not_called()
        self.assertEqual(make.call_count, 2)
        for call in make.call_args_list:
            self.assertEqual(call.kwargs, {"port": 0, "allow_open": False})


def snapshot(video):
    return {
        "title": "完整系统价值", "category": "转化正向参考",
        "conclusion": "用户结合客单价认可本案例；不作为通用阈值。", "kind": "performance",
        "assessments": [{"dimension": "conversion", "polarity": "positive", "reason": "用户认可"}],
        "metrics": {"video_id": "7664986913497009439", "creator_handle": "@example",
                    "published_title": "Complete kit", "views_display": "48.56K",
                    "product_impressions_display": "50.39K", "product_clicks_display": "1.28K",
                    "items_sold": 12, "ctor_display": "0.94%", "ticket_value": "179.9"},
        "evidence": {"status": "user_reported", "source": "用户截图", "context": "统计窗口未知"},
        "transcript": {"text": "The whole setup in one case.\nKeep the original words.",
                       "status": "checked", "source": "用户核对"},
        "assets": {"video": str(video)},
        "highlights": ["清晰说明完整价值"], "limitations": ["不能据此推断因果"],
        "history_note": "旧稿不能继续关联当前数据。",
    }


def new_case_snapshot(video):
    """A second test case may share video bytes, but needs its own TikTok ID."""
    current = snapshot(video)
    current["metrics"]["video_id"] = "7664986913497009440"
    return current


class WorkbenchHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="case-workbench-test-")
        self.addCleanup(self.temp.cleanup)
        # Hosted Windows runners may expose TEMP through an 8.3 alias (RUNNER~1).
        # The application stores canonical paths, so comparisons need the same form.
        self.root = Path(self.temp.name).resolve()
        self.skill = self.root / "skill"
        self.runtime = self.root / "runtime"
        self.video = self.root / "source video.mp4"
        self.video_bytes = bytes(range(256)) * 8
        self.video.write_bytes(self.video_bytes)
        self.store = SERVER.CaseStore(self.skill)
        case = self.store.add({
            "title": "收录原始标题", "product": "SHARPAL 202H",
            "original": {"text": "original script remains immutable", "source": "测试原件"},
            "feedback": {"kind": "editorial", "text": "首次收录"},
            "current": snapshot(self.video),
        })
        self.identifier = case["case_id"]
        current = copy.deepcopy(case["current"])
        current["source_event_ids"] = [case["events"][0]["event_id"]]
        self.store.append(self.identifier, {"kind": "fact", "text": "确认来源", "current": current})
        self.silence = patch.object(SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = make_test_server(self.skill, self.runtime)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200, state)
        self.token = state["token"]

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None, headers=None, authenticated=True, raw=None):
        supplied = dict(headers or {})
        if method == "POST" and authenticated:
            supplied.setdefault("X-Workbench-Token", getattr(self, "token", ""))
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            supplied.setdefault("Content-Type", "application/json")
        if raw is not None:
            supplied.setdefault("Content-Length", str(len(raw)))
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=supplied)
            response = connection.getresponse()
            content = response.read()
            response_headers = dict(response.getheaders())
            if "application/json" in response_headers.get("Content-Type", ""):
                content = json.loads(content)
            return response.status, response_headers, content
        finally:
            connection.close()

    def detail(self):
        status, _, value = self.request("GET", f"/api/cases/{self.identifier}")
        self.assertEqual(status, 200, value)
        return value

    def body(self, detail=None):
        detail = detail or self.detail()
        current = copy.deepcopy(detail["current"])
        current["conclusion"] += " 已补充验证说明。"
        return {"request_id": uuid4().hex, "revision": detail["revision"],
                "current": current, "note": "补充当前结论，保存历史。"}

    def save(self, body):
        return self.request("POST", f"/api/cases/{self.identifier}", body)

    def record_bytes(self):
        return {str(path.relative_to(self.skill)): path.read_bytes()
                for path in self.store.folder(self.identifier).rglob("*.json")}

    def assert_old_records_unchanged(self, before):
        for relative, content in before.items():
            self.assertEqual((self.skill / relative).read_bytes(), content, relative)

    def assert_owned_video(self, detail, content, source=None):
        path = Path(detail["current"]["assets"]["video"])
        self.assertTrue(path.is_relative_to(self.runtime / "evidence" / "cases" / detail["case_id"] / "videos"))
        self.assertEqual(path.read_bytes(), content)
        if source is not None:
            self.assertNotEqual(path, Path(source))
        self.assertTrue(detail["assets"]["video"]["managed"])
        self.assertTrue(detail["assets"]["video"]["owned"])
        return path

    def test_state_polling_returns_small_reply_only_when_digest_matches(self):
        state = self.request("GET", "/api/state")[2]
        self.assertTrue(state["digest"])
        status, _, same = self.request("GET", "/api/state?since=" + state["digest"])
        self.assertEqual(status, 200)
        self.assertIs(same["unchanged"], True)
        self.assertNotIn("cases", same)
        self.assertEqual(same["token"], state["token"])
        stale = self.request("GET", "/api/state?since=stale")[2]
        self.assertNotIn("unchanged", stale)
        self.assertEqual(len(stale["cases"]), len(state["cases"]))

    def test_cached_state_still_sees_outside_changes(self):
        first = self.request("GET", "/api/state")[2]
        self.assertIs(self.request("GET", "/api/state?since=" + first["digest"])[2]["unchanged"], True)
        # An intake tool appends a revision outside the workbench.
        current = copy.deepcopy(self.store.read_case(self.identifier)["current"])
        current["conclusion"] = "外部收录流程更新的结论。"
        self.store.append(self.identifier, {"kind": "editorial", "text": "外部更新", "current": current})
        changed = self.request("GET", "/api/state?since=" + first["digest"])[2]
        self.assertNotIn("unchanged", changed)
        self.assertEqual(changed["cases"][0]["current"]["conclusion"], "外部收录流程更新的结论。")
        # The linked video file is moved away.
        self.video.rename(self.video.with_suffix(".moved"))
        moved = self.request("GET", "/api/state?since=" + changed["digest"])[2]
        self.assertFalse(moved["cases"][0]["assets"]["video"]["exists"])
        # Column assignments are edited by another workbench page.
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "navigation-assignments.json").write_text(
            json.dumps({self.identifier: "column:native"}), encoding="utf-8")
        self.assertEqual(self.request("GET", "/api/state")[2]["navigation"], {self.identifier: "column:native"})
        # A broken index is still reported instead of being served from the cache.
        (self.skill / "cases" / "index.json").write_text("{}", encoding="utf-8")
        self.assertNotEqual(self.request("GET", "/api/state")[0], 200)

    def test_structure_check_reports_template_errors_and_is_cached(self):
        holder = type("Holder", (), {"structure_cache": {}})()
        bad = SERVER.Workbench.structure_check(holder, "没有 frontmatter 的正文", "digest-bad")
        self.assertFalse(bad["ok"])
        self.assertGreater(bad["count"], 0)
        self.assertLessEqual(len(bad["errors"]), 8)
        again = SERVER.Workbench.structure_check(holder, "内容已变但摘要相同", "digest-bad")
        self.assertIs(again, bad)

    def test_state_and_detail_use_current_version_and_preserve_unknowns(self):
        status, _, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(len(state["cases"]), 1)
        entry = state["cases"][0]
        detail = self.detail()
        self.assertEqual(entry["title"], "完整系统价值")
        self.assertEqual(entry["revision"], detail["revision"])
        self.assertEqual(detail["current"]["metrics"]["ctor_display"], "0.94%")
        self.assertNotIn("currency", detail["current"]["metrics"])
        self.assertTrue(detail["assets"]["video"]["exists"])
        self.assertEqual(detail["assets"]["video"]["bytes"], len(self.video_bytes))

    def test_video_ranges_and_head(self):
        path = f"/media/{self.identifier}/video"
        for header, expected, content_range in (
            ("bytes=12-29", self.video_bytes[12:30], "bytes 12-29/2048"),
            ("bytes=-9", self.video_bytes[-9:], "bytes 2039-2047/2048"),
            ("bytes=2044-", self.video_bytes[2044:], "bytes 2044-2047/2048"),
        ):
            with self.subTest(header=header):
                status, headers, content = self.request("GET", path, headers={"Range": header})
                self.assertEqual(status, 206)
                self.assertEqual(content, expected)
                self.assertEqual(headers["Content-Range"], content_range)
        status, headers, content = self.request("HEAD", path)
        self.assertEqual((status, content), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(self.video_bytes))
        status, headers, _ = self.request("GET", path, headers={"Range": "bytes=99999-"})
        self.assertEqual(status, 416)
        self.assertEqual(headers["Content-Range"], "bytes */2048")

    def test_thumbnail_is_cached_in_runtime_and_refreshes_with_source(self):
        endpoint = f"/media/{self.identifier}/thumbnail"
        jpeg = b"\xff\xd8\xfffake-jpeg\xff\xd9"
        before = self.record_bytes()

        def extract(command, **_kwargs):
            Path(command[-1]).write_bytes(jpeg)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(SERVER.subprocess, "run", side_effect=extract) as run:
            status, headers, content = self.request("GET", endpoint)
            self.assertEqual((status, content), (200, jpeg))
            self.assertEqual(headers["Content-Type"], "image/jpeg")
            self.assertEqual(run.call_count, 1)
            self.assertIn("-frames:v", run.call_args.args[0])

            self.assertEqual(self.request("GET", endpoint)[2], jpeg)
            self.assertEqual(self.request("HEAD", endpoint)[0], 200)
            self.assertEqual(run.call_count, 1)

            self.video.write_bytes(self.video_bytes + b"changed")
            self.assertEqual(self.request("GET", endpoint)[2], jpeg)
            self.assertEqual(run.call_count, 2)

        cache = self.runtime / "thumbnail-cache" / self.identifier
        self.assertEqual(len(list(cache.glob("*.jpg"))), 2)
        self.assert_old_records_unchanged(before)

    def test_thumbnail_missing_or_failed_video_has_safe_response(self):
        endpoint = f"/media/{self.identifier}/thumbnail"
        before = self.record_bytes()
        with patch.object(SERVER.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as run:
            status, _, body = self.request("GET", endpoint)
            self.assertEqual(status, 404, body)
            self.assertEqual(run.call_count, 2)
            self.assertFalse(list((self.runtime / "thumbnail-cache" / self.identifier).glob("*.jpg")))
        self.assertEqual(self.request("GET", endpoint + "?event=unknown")[0], 404)
        self.assertEqual(self.request("GET", endpoint + "?event=one&event=two")[0], 400)
        self.video.unlink()
        with patch.object(SERVER.subprocess, "run") as run:
            self.assertEqual(self.request("GET", endpoint)[0], 404)
            run.assert_not_called()
        self.assert_old_records_unchanged(before)

    def add_source_analysis(self):
        source = self.root / "历史完整拆解.md"
        source.write_text("# 完整反推拆解\n\n| 镜头 | 作用 |\n| --- | --- |\n| 开场 | 结果先行 |\n", encoding="utf-8-sig")
        current = copy.deepcopy(self.detail()["current"])
        current["source_case"] = {
            "library_id": "VSL-T3-001", "file_path": str(source),
            "tier": "T3", "transcript_status": "verified", "case_type": "video_analysis",
        }
        self.store.append(self.identifier, {"kind": "editorial", "text": "关联历史拆解", "current": current})
        return source

    def test_source_analysis_stays_separate_and_survives_edit_archive_restore(self):
        source = self.add_source_analysis()
        before = source.read_bytes()
        detail = self.detail()
        self.assertTrue(detail["source_analysis"]["exists"])
        self.assertIn("| 开场 | 结果先行 |", detail["source_analysis"]["text"])
        self.assertNotIn("完整反推拆解", detail["current"]["transcript"]["text"])
        body = self.body(detail)
        status, _, saved = self.save(body)
        self.assertEqual(status, 200, saved)
        for archived in (True, False):
            status, _, saved = self.archive(self.archive_body(archived))
            self.assertEqual(status, 200, saved)
            self.assertEqual(saved["case"]["current"]["source_case"], detail["current"]["source_case"])
            self.assertEqual(saved["case"]["source_analysis"]["text"], detail["source_analysis"]["text"])
        self.assertEqual(source.read_bytes(), before)

    def test_source_change_refreshes_signature_and_missing_source_keeps_case_available(self):
        source = self.add_source_analysis()
        first = self.request("GET", "/api/state")[2]
        self.assertNotIn("source_analysis", first["cases"][0])
        source.write_text("# 已补充的完整拆解", encoding="utf-8")
        second = self.request("GET", "/api/state")[2]
        self.assertNotEqual(first["signature"], second["signature"])
        self.assertEqual(self.detail()["source_analysis"]["text"], "# 已补充的完整拆解")
        source.unlink()
        missing = self.detail()
        self.assertFalse(missing["source_analysis"]["exists"])
        self.assertEqual(missing["original"]["text"], "original script remains immutable")
        self.assertEqual(self.request("GET", "/api/state")[0], 200)

    def test_source_analysis_read_failure_does_not_hide_case(self):
        source = self.add_source_analysis()
        source.write_bytes(b"\xff\xfe\x00invalid utf8")
        detail = self.detail()
        self.assertFalse(detail["source_analysis"]["exists"])
        self.assertIn("error", detail["source_analysis"])
        self.assertTrue(detail["current"])

    def deletion_fixture(self):
        owned = self.runtime / "evidence" / "workbench-imports" / "delete-fixture" / "video.mp4"
        owned.parent.mkdir(parents=True)
        owned.write_bytes(b"owned test video")
        saved_body = self.body()
        saved_body["current"]["assets"]["video"] = str(owned)
        status, _, saved = self.save(saved_body)
        self.assertEqual(status, 200, saved)
        owned = Path(saved["case"]["current"]["assets"]["video"])
        self.assertEqual(self.archive(self.archive_body(True))[0], 200)
        status, _, plan = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")
        self.assertEqual(status, 200, plan)
        return owned, saved_body, plan

    def deletion_body(self, plan):
        return {"request_id": uuid4().hex, "revision": plan["revision"], "plan_token": plan["plan_token"],
                "confirmation": self.identifier, "include_source": False}

    def delete(self, body, **kwargs):
        return self.request("POST", f"/api/cases/{self.identifier}/delete", body, **kwargs)

    def test_delete_preview_is_read_only_and_active_case_cannot_be_deleted(self):
        before = self.record_bytes()
        self.assertEqual(self.request("GET", f"/api/cases/{self.identifier}/delete-preview")[0], 400)
        self.assertEqual(self.delete({"request_id": uuid4().hex, "confirmation": self.identifier})[0], 400)
        self.assertEqual(self.record_bytes(), before)
        owned, _, plan = self.deletion_fixture()
        before = self.record_bytes()
        self.assertIn(str(owned), [item["path"] for item in plan["files"]])
        self.assertIn(str(self.video), [item["path"] for item in plan["retained"]])
        self.assertEqual(self.request("GET", f"/api/cases/{self.identifier}/delete-preview")[0], 200)
        self.assertEqual(self.record_bytes(), before)
        self.assertTrue(owned.is_file())

    def test_delete_removes_owned_local_data_and_clears_old_response_caches(self):
        owned, saved_body, plan = self.deletion_fixture()
        jpeg = b"\xff\xd8\xfffake-jpeg\xff\xd9"

        def extract(command, **_kwargs):
            Path(command[-1]).write_bytes(jpeg)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(SERVER.subprocess, "run", side_effect=extract):
            self.assertEqual(self.request("GET", f"/media/{self.identifier}/thumbnail")[2], jpeg)
        thumbnail_cache = self.runtime / "thumbnail-cache" / self.identifier
        self.assertEqual(len(list(thumbnail_cache.glob("*.jpg"))), 1)
        body = self.deletion_body(plan)
        status, _, result = self.delete(body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["deleted"])
        self.assertFalse(self.store.folder(self.identifier).exists())
        self.assertFalse((self.runtime / "library" / self.identifier).exists())
        self.assertFalse(thumbnail_cache.exists())
        self.assertFalse(owned.exists())
        self.assertEqual(self.video.read_bytes(), self.video_bytes)
        state = self.request("GET", "/api/state")
        self.assertEqual(state[0], 200, state)
        self.assertEqual(state[2]["cases"], [])
        self.assertEqual(self.delete(body)[2], result)
        self.assertNotEqual(self.save(saved_body)[0], 200)
        self.assertNotIn(self.identifier, (self.runtime / "library" / "ARCHIVE.md").read_text(encoding="utf-8"))

    def test_delete_confirmation_token_and_request_authentication_are_required(self):
        owned, _, plan = self.deletion_fixture()
        original = self.record_bytes()
        for value, expected in (({"confirmation": "yes"}, 400), ({"plan_token": "wrong"}, 409), ({"include_source": "false"}, 400)):
            body = self.deletion_body(plan) | value
            self.assertEqual(self.delete(body)[0], expected)
        self.assertEqual(self.delete(self.deletion_body(plan), authenticated=False)[0], 403)
        self.assertEqual(self.delete(self.deletion_body(plan), headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.record_bytes(), original)
        self.assertTrue(owned.is_file())

    def test_delete_rechecks_state_and_files_after_preview(self):
        owned, _, plan = self.deletion_fixture()
        owned.write_bytes(b"new file content after user saw the plan")
        self.assertEqual(self.delete(self.deletion_body(plan))[0], 409)
        status, _, fresh = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")
        self.assertEqual(status, 200, fresh)
        self.assertEqual(self.archive(self.archive_body(False))[0], 200)
        self.assertIn(self.delete(self.deletion_body(fresh))[0], (400, 409))
        self.assertTrue(owned.exists())
        self.assertFalse(self.detail()["archived"])

    def test_delete_preview_query_validation_and_unknown_case(self):
        self.assertEqual(self.request("GET", f"/api/cases/{self.identifier}/delete-preview?include_source=yes")[0], 400)
        self.assertEqual(self.request("GET", "/api/cases/PVR-ffffffffffff/delete-preview")[0], 404)

    def test_delete_response_failure_cannot_replay_cached_case_contents(self):
        _, saved_body, plan = self.deletion_fixture()
        actual_delete = self.server.app.deletion.delete
        def committed_but_no_response(*args, **kwargs):
            actual_delete(*args, **kwargs)
            raise OSError("simulated response failure after physical deletion")
        with patch.object(self.server.app.deletion, "delete", side_effect=committed_but_no_response):
            self.assertEqual(self.delete(self.deletion_body(plan))[0], 500)
        self.assertEqual(self.request("GET", "/api/state")[2]["cases"], [])
        self.assertNotEqual(self.save(saved_body)[0], 200)
        self.assertFalse(any(value[1].get("case", {}).get("case_id") == self.identifier for value in self.server.app.requests.values()))

    def test_event_pinned_media_remains_stable_after_current_video_is_replaced(self):
        before = self.detail()
        old_event_id = before["current_event_id"]
        media_path = f"/media/{self.identifier}/video"
        pinned_path = media_path + "?" + urlencode({"event": old_event_id})
        status, old_headers, old_range = self.request("GET", pinned_path, headers={"Range": "bytes=12-29"})
        self.assertEqual(status, 206)
        self.assertEqual(old_range, self.video_bytes[12:30])
        self.assertEqual(old_headers["Content-Range"], "bytes 12-29/2048")
        old_state_case = self.request("GET", "/api/state")[2]["cases"][0]
        self.assertEqual(old_state_case["current_event_id"], old_event_id)

        replacement = self.root / "replacement video.mp4"
        replacement_bytes = b"different-video-content-" * 128
        replacement.write_bytes(replacement_bytes)
        body = self.body(before)
        body["current"]["assets"]["video"] = str(replacement)
        status, _, saved = self.save(body)
        self.assertEqual(status, 200, saved)
        new_event_id = saved["case"]["current_event_id"]
        self.assertNotEqual(new_event_id, old_event_id)

        status, _, default_bytes = self.request("GET", media_path)
        self.assertEqual((status, default_bytes), (200, replacement_bytes))
        self.assertNotEqual(default_bytes, self.video_bytes)
        status, _, pinned_bytes = self.request("GET", pinned_path)
        self.assertEqual((status, pinned_bytes), (200, self.video_bytes))
        status, pinned_headers, pinned_range = self.request("GET", pinned_path, headers={"Range": "bytes=12-29"})
        self.assertEqual(status, 206)
        self.assertEqual(pinned_range, old_range)
        self.assertEqual(pinned_headers["Content-Range"], old_headers["Content-Range"])
        self.assertEqual(pinned_headers["Content-Length"], old_headers["Content-Length"])
        latest_state_case = self.request("GET", "/api/state")[2]["cases"][0]
        self.assertEqual(latest_state_case["current_event_id"], new_event_id)
        self.assertEqual(latest_state_case["assets"]["video"]["path"], saved["case"]["current"]["assets"]["video"])
        self.assert_owned_video(saved["case"], replacement_bytes, replacement)

    def test_save_appends_full_snapshot_without_modifying_original_or_prior_events(self):
        before = self.record_bytes()
        old_detail = self.detail()
        body = self.body(old_detail)
        status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["warning"])
        expected = copy.deepcopy(body["current"])
        expected["assets"]["video"] = result["case"]["current"]["assets"]["video"]
        self.assertEqual(result["case"]["current"], expected)
        self.assert_owned_video(result["case"], self.video_bytes, self.video)
        self.assertEqual(len(result["case"]["events"]), len(old_detail["events"]) + 1)
        self.assertNotEqual(result["case"]["revision"], old_detail["revision"])
        self.assert_old_records_unchanged(before)
        for name in ("INDEX.md", f"{self.identifier}/case.md", f"{self.identifier}/history.md"):
            self.assertTrue((self.runtime / "library" / name).is_file(), name)

    def test_stale_revision_conflicts_and_does_not_write(self):
        first = self.body()
        stale = copy.deepcopy(first)
        stale["request_id"] = uuid4().hex
        stale["current"]["title"] = "过期编辑不能覆盖"
        self.assertEqual(self.save(first)[0], 200)
        committed = self.record_bytes()
        status, _, result = self.save(stale)
        self.assertEqual(status, 409, result)
        self.assertEqual(committed, self.record_bytes())
        self.assertNotEqual(self.detail()["current"]["title"], "过期编辑不能覆盖")

    def test_feedback_only_external_append_also_invalidates_revision(self):
        pending = self.body()
        self.store.append(self.identifier, {"kind": "fact", "text": "外部工具只追加反馈，未改当前视图"})
        committed = self.record_bytes()
        self.assertEqual(self.save(pending)[0], 409)
        self.assertEqual(committed, self.record_bytes())

    def test_duplicate_request_id_is_idempotent_but_changed_payload_conflicts(self):
        body = self.body()
        status, _, first = self.save(body)
        self.assertEqual(status, 200, first)
        committed = self.record_bytes()
        status, _, second = self.save(body)
        self.assertEqual(status, 200, second)
        self.assertEqual(first, second)
        self.assertEqual(committed, self.record_bytes())
        body["note"] = "同一保存标识携带不同请求"
        self.assertEqual(self.save(body)[0], 409)
        self.assertEqual(committed, self.record_bytes())

    def archive_body(self, archived=True, detail=None):
        detail = detail or self.detail()
        return {"request_id": uuid4().hex, "revision": detail["revision"],
                "archived": archived, "note": "暂时归档" if archived else "恢复案例"}

    def archive(self, body, **kwargs):
        return self.request("POST", f"/api/cases/{self.identifier}/archive", body, **kwargs)

    def test_archive_and_restore_preserve_content_media_and_history(self):
        old = self.detail()
        before = self.record_bytes()
        status, _, result = self.archive(self.archive_body(detail=old))
        self.assertEqual(status, 200, result)
        archived = result["case"]
        self.assertTrue(archived["archived"])
        self.assertEqual(archived["current"], old["current"])
        self.assertEqual(archived["current_event_id"], old["current_event_id"])
        self.assertEqual(archived["original"], old["original"])
        self.assertEqual(archived["archive_event_id"], archived["events"][-1]["event_id"])
        self.assertEqual(archived["events"][-1]["lifecycle"], {"archived": True})
        self.assert_old_records_unchanged(before)
        self.assertEqual(self.video.read_bytes(), self.video_bytes)
        self.assertEqual(self.request("GET", f"/media/{self.identifier}/video")[2], self.video_bytes)
        summary = self.request("GET", "/api/state")[2]["cases"][0]
        self.assertTrue(summary["archived"])
        self.assertEqual(summary["current_event_id"], old["current_event_id"])
        index = (self.runtime / "library/INDEX.md").read_text(encoding="utf-8")
        archive = (self.runtime / "library/ARCHIVE.md").read_text(encoding="utf-8")
        self.assertNotIn(self.identifier, index)
        self.assertIn(self.identifier, archive)
        status, _, result = self.archive(self.archive_body(False))
        self.assertEqual(status, 200, result)
        self.assertFalse(result["case"]["archived"])
        self.assertEqual(result["case"]["current"], old["current"])
        self.assertIn(self.identifier, (self.runtime / "library/INDEX.md").read_text(encoding="utf-8"))
        self.assertNotIn(self.identifier, (self.runtime / "library/ARCHIVE.md").read_text(encoding="utf-8"))
        self.assert_old_records_unchanged(before)

    def test_archive_request_retry_and_same_state_are_idempotent(self):
        body = self.archive_body()
        status, _, first = self.archive(body)
        self.assertEqual(status, 200, first)
        committed = self.record_bytes()
        status, _, second = self.archive(body)
        self.assertEqual((status, second), (200, first))
        status, _, no_change = self.archive(self.archive_body())
        self.assertEqual(status, 200, no_change)
        self.assertTrue(no_change["unchanged"])
        self.assertEqual(committed, self.record_bytes())
        changed = dict(body, archived=False)
        self.assertEqual(self.archive(changed)[0], 409)
        self.assertEqual(committed, self.record_bytes())

    def test_archive_rejects_stale_invalid_and_unauthenticated_writes(self):
        before = self.record_bytes()
        good = self.archive_body()
        for bad in (dict(good, archived="false"), dict(good, archived=1),
                    dict(good, archived=None), dict(good, note=7),
                    dict(good, revision="outdated")):
            with self.subTest(body=bad):
                self.assertIn(self.archive(bad)[0], (400, 409))
        self.assertEqual(self.archive(good, authenticated=False)[0], 403)
        self.assertEqual(before, self.record_bytes())
        stale = self.archive_body()
        self.assertEqual(self.save(self.body())[0], 200)
        committed = self.record_bytes()
        self.assertEqual(self.archive(stale)[0], 409)
        self.assertEqual(committed, self.record_bytes())

    def test_editing_archived_case_does_not_restore_it_and_invalidates_old_edit(self):
        pending_edit = self.body()
        self.assertEqual(self.archive(self.archive_body())[0], 200)
        self.assertEqual(self.save(pending_edit)[0], 409)
        fresh_edit = self.body()
        status, _, saved = self.save(fresh_edit)
        self.assertEqual(status, 200, saved)
        self.assertTrue(saved["case"]["archived"])
        expected = copy.deepcopy(fresh_edit["current"])
        expected["assets"]["video"] = saved["case"]["current"]["assets"]["video"]
        self.assertEqual(saved["case"]["current"], expected)

    def test_archive_render_failure_keeps_commit_and_retry_does_not_duplicate(self):
        body = self.archive_body()
        with patch.object(SERVER, "render_library", side_effect=OSError("simulated render failure")):
            status, _, result = self.archive(body)
        self.assertEqual(status, 200, result)
        self.assertIn("已保存", result["warning"])
        self.assertTrue(self.detail()["archived"])
        committed = self.record_bytes()
        self.assertEqual(self.archive(body)[2], result)
        self.assertEqual(committed, self.record_bytes())
        self.assertEqual(self.request("POST", "/api/sync", {})[0], 200)
        self.assertIn(self.identifier, (self.runtime / "library/ARCHIVE.md").read_text(encoding="utf-8"))

    def test_archive_index_failure_reports_committed_state_and_can_sync(self):
        body = self.archive_body()
        with patch.object(self.server.app.store, "rebuild_index", side_effect=OSError("simulated index failure")):
            status, _, result = self.archive(body)
        self.assertEqual(status, 200, result)
        self.assertTrue(result["case"]["archived"])
        self.assertIn("已保存", result["warning"])
        committed = self.record_bytes()
        self.assertEqual(self.archive(body)[2], result)
        self.assertEqual(committed, self.record_bytes())
        self.assertEqual(self.request("POST", "/api/sync", {})[0], 200)
        self.assertTrue(self.request("GET", "/api/state")[2]["cases"][0]["archived"])

    def test_only_registered_attachments_can_be_downloaded(self):
        secret = self.root / "unregistered.txt"
        secret.write_text("not a registered attachment", encoding="utf-8")
        for path in (
            f"/media/{self.identifier}/transcript", f"/media/{self.identifier}/unknown",
            "/media/../../unregistered.txt", "/" + str(secret).replace("\\", "/"),
            f"/media/{self.identifier}/video?event=unregistered-event",
        ):
            with self.subTest(path=path):
                status, _, result = self.request("GET", path)
                self.assertIn(status, (400, 404), result)
        query = urlencode({"path": str(secret)})
        status, _, result = self.request("GET", f"/media/{self.identifier}/video?{query}")
        self.assertEqual((status, result), (200, self.video_bytes))

    def test_missing_token_bad_origin_bad_host_and_cross_site_are_rejected(self):
        body = self.body()
        before = self.record_bytes()
        for headers, authenticated in (
            ({}, False), ({"Origin": "https://malicious.example"}, True),
            ({"Host": "malicious.example"}, True), ({"Sec-Fetch-Site": "cross-site"}, True),
            ({"X-Workbench-Token": "wrong-token"}, True),
        ):
            with self.subTest(headers=headers, authenticated=authenticated):
                status, _, result = self.request("POST", f"/api/cases/{self.identifier}", body,
                                                 headers=headers, authenticated=authenticated)
                self.assertEqual(status, 403, result)
        for headers in ({"Origin": "https://malicious.example"}, {"Host": "malicious.example"}):
            self.assertEqual(self.request("GET", "/api/state", headers=headers)[0], 403)
        self.assertEqual(before, self.record_bytes())

    def test_upload_is_local_then_can_be_associated_with_new_case(self):
        payload = b"test-uploaded-mp4-bytes"
        query = urlencode({"kind": "video", "name": "../../新案例.mp4"})
        status, _, result = self.request("POST", "/api/upload?" + query, raw=payload)
        self.assertEqual(status, 200, result)
        path = Path(result["path"])
        self.assertTrue(path.resolve().is_relative_to((self.runtime / "evidence" / "workbench-imports").resolve()))
        self.assertEqual(path.read_bytes(), payload)
        current = new_case_snapshot(path)
        current["title"] = "上传并收录的新案例"
        body = {"request_id": uuid4().hex, "current": current, "product": "另一产品", "note": "收录上传文件"}
        status, _, result = self.request("POST", "/api/cases", body)
        self.assertEqual(status, 200, result)
        new_id = result["case"]["case_id"]
        self.assertNotEqual(new_id, self.identifier)
        self.assert_owned_video(result["case"], payload, path)
        self.assertFalse(path.exists(), "Unreferenced upload staging file should be cleaned after commit")
        self.assertEqual(self.request("GET", f"/media/{new_id}/video")[2], payload)
        self.assertEqual(len(self.request("GET", "/api/state")[2]["cases"]), 2)

    def test_upload_invalid_kind_and_empty_file_are_rejected(self):
        for query, payload in (({"kind": "video", "name": "script.exe"}, b"x"),
                               ({"kind": "unknown", "name": "clip.mp4"}, b"x"),
                               ({"kind": "video", "name": "empty.mp4"}, b"")):
            with self.subTest(query=query):
                self.assertEqual(self.request("POST", "/api/upload?" + urlencode(query), raw=payload)[0], 400)

    def test_saved_video_survives_original_source_removal_and_is_not_copied_on_noop(self):
        status, _, saved = self.save(self.body())
        self.assertEqual(status, 200, saved)
        owned = self.assert_owned_video(saved["case"], self.video_bytes, self.video)
        self.video.unlink()  # Temporary test source only.
        self.assertEqual(self.request("GET", f"/media/{self.identifier}/video")[2], self.video_bytes)
        before = list((self.runtime / "evidence" / "cases").rglob("*.mp4"))
        current = self.detail()
        body = {"request_id": uuid4().hex, "revision": current["revision"], "current": current["current"], "note": "未修改"}
        status, _, unchanged = self.save(body)
        self.assertEqual(status, 200, unchanged)
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(list((self.runtime / "evidence" / "cases").rglob("*.mp4")), before)
        self.assertTrue(owned.exists())

    def test_save_existing_external_video_without_text_edits_creates_a_managed_copy(self):
        detail = self.detail()
        body = {"request_id": uuid4().hex, "revision": detail["revision"], "current": detail["current"], "note": "为原视频保存独立副本"}
        status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        self.assertFalse(result.get("unchanged", False))
        self.assert_owned_video(result["case"], self.video_bytes, self.video)
        self.assertEqual(len(result["case"]["events"]), len(detail["events"]) + 1)
        self.assertEqual(self.video.read_bytes(), self.video_bytes)

    def test_video_without_transcript_or_url_can_be_registered_as_a_new_case(self):
        current = new_case_snapshot(self.video)
        current.pop("transcript")
        body = {"request_id": uuid4().hex, "current": current, "product": "视频导入测试", "note": "仅导入视频"}
        status, _, result = self.request("POST", "/api/cases", body)
        self.assertEqual(status, 200, result)
        self.assert_owned_video(result["case"], self.video_bytes, self.video)
        self.assertNotIn("transcript", result["case"]["current"])
        self.assertTrue(result["case"]["original"])
        self.assertEqual(self.request("POST", "/api/cases", body)[2], result)

    def test_each_case_owns_its_video_even_when_importing_another_case_copy(self):
        first = self.save(self.body())[2]["case"]
        first_path = Path(first["current"]["assets"]["video"])
        current = new_case_snapshot(first_path)
        body = {"request_id": uuid4().hex, "current": current, "product": "独立视频测试", "note": "复制另一个案例的视频"}
        status, _, second = self.request("POST", "/api/cases", body)
        self.assertEqual(status, 200, second)
        second_path = self.assert_owned_video(second["case"], self.video_bytes, first_path)
        self.archive(self.archive_body(True))
        plan = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")[2]
        self.assertEqual(self.delete(self.deletion_body(plan))[0], 200)
        self.assertFalse(first_path.exists())
        self.assertTrue(second_path.exists())
        self.assertEqual(self.video.read_bytes(), self.video_bytes)
        self.assertEqual(self.request("GET", f"/media/{second['case']['case_id']}/video")[2], self.video_bytes)

    def test_saved_video_revisions_stay_available_until_the_case_is_deleted(self):
        first = self.save(self.body())[2]["case"]
        old_owned = Path(first["current"]["assets"]["video"])
        replacement = self.root / "replacement-owned-test.mp4"
        replacement.write_bytes(b"replacement video bytes")
        body = self.body()
        body["current"]["assets"]["video"] = str(replacement)
        status, _, saved = self.save(body)
        self.assertEqual(status, 200, saved)
        new_owned = self.assert_owned_video(saved["case"], replacement.read_bytes(), replacement)
        self.assertTrue(old_owned.exists())
        old_url = f"/media/{self.identifier}/video?" + urlencode({"event": first["current_event_id"]})
        self.assertEqual(self.request("GET", old_url)[2], self.video_bytes)
        self.archive(self.archive_body(True))
        plan = self.request("GET", f"/api/cases/{self.identifier}/delete-preview")[2]
        self.assertEqual(self.delete(self.deletion_body(plan))[0], 200)
        self.assertFalse(old_owned.exists())
        self.assertFalse(new_owned.exists())
        self.assertTrue(replacement.exists())

    def test_stale_revision_rejects_before_copying_any_video(self):
        body = self.body()
        self.store.set_archived(self.identifier, True)
        before = list((self.runtime / "evidence").rglob("*"))
        self.assertEqual(self.save(body)[0], 409)
        self.assertEqual(list((self.runtime / "evidence").rglob("*")), before)
        self.assertEqual(self.video.read_bytes(), self.video_bytes)

    def test_external_case_updates_and_new_cases_appear_without_restart(self):
        previous = self.request("GET", "/api/state")[2]
        current = copy.deepcopy(self.detail()["current"])
        current["title"] = "外部收录工具修改后的标题"
        self.store.append(self.identifier, {"kind": "fact", "text": "来自外部工具", "current": current})
        self.store.add({"title": "外部新增未整理案例", "original": {"text": "External new case."},
                        "feedback": {"kind": "editorial", "text": "首次反馈"}})
        status, _, latest = self.request("GET", "/api/state")
        self.assertEqual(status, 200, latest)
        self.assertNotEqual(previous["signature"], latest["signature"])
        self.assertEqual(len(latest["cases"]), 2)
        matching = next(case for case in latest["cases"] if case["case_id"] == self.identifier)
        self.assertEqual(matching["current"]["title"], current["title"])
        self.assertEqual(self.detail()["current"]["title"], current["title"])

    def test_render_failure_reports_committed_save_and_retry_does_not_append_again(self):
        body = self.body()
        count = len(self.detail()["events"])
        with patch.object(SERVER, "render_library", side_effect=OSError("simulated output failure")):
            status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        self.assertIn("已保存", result["warning"])
        self.assertEqual(len(self.detail()["events"]), count + 1)
        committed = self.record_bytes()
        status, _, retry = self.save(body)
        self.assertEqual(status, 200, retry)
        self.assertEqual(retry, result)
        self.assertEqual(committed, self.record_bytes())
        self.assertEqual(self.request("POST", "/api/sync", {})[0], 200)
        self.assertEqual(committed, self.record_bytes())
        self.assertTrue((self.runtime / "library" / "INDEX.md").is_file())

    def test_video_copy_survives_index_failure_and_retry(self):
        body = self.body()
        with patch.object(self.server.app.store, "rebuild_index", side_effect=OSError("index unavailable")):
            status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        self.assertIn("已保存", result["warning"])
        owned = self.assert_owned_video(result["case"], self.video_bytes, self.video)
        records = self.record_bytes()
        self.assertEqual(self.save(body)[2], result)
        self.assertEqual(self.record_bytes(), records)
        self.assertEqual(list((self.runtime / "evidence" / "cases").rglob("*.mp4")), [owned])
        self.assertEqual(self.request("POST", "/api/sync", {})[0], 200)
        self.assertTrue(owned.exists())

    def test_failed_commit_rolls_back_only_new_video_copy(self):
        records = self.record_bytes()
        with patch.object(self.server.app.store, "append", side_effect=OSError("event write failed")):
            status, _, result = self.save(self.body())
        self.assertEqual(status, 500, result)
        self.assertEqual(self.record_bytes(), records)
        self.assertEqual(self.video.read_bytes(), self.video_bytes)
        self.assertEqual(list((self.runtime / "evidence" / "cases").rglob("*.mp4")), [])

    def test_stage_cleanup_failure_does_not_fail_committed_save(self):
        query = urlencode({"kind": "video", "name": "cleanup.mp4"})
        status, _, upload = self.request("POST", "/api/upload?" + query, raw=self.video_bytes)
        self.assertEqual(status, 200, upload)
        stage = Path(upload["path"])
        body = self.body()
        body["current"]["assets"]["video"] = str(stage)
        with patch.object(self.server.app.video_files, "cleanup_stage", side_effect=OSError("file locked")):
            status, _, result = self.save(body)
        self.assertEqual(status, 200, result)
        self.assertIsNone(result["warning"])
        self.assert_owned_video(result["case"], self.video_bytes, stage)
        self.assertTrue(stage.exists())
        self.assertEqual(self.save(body)[2], result)

    def test_new_video_case_postcommit_failure_returns_saved_warning_and_retries_once(self):
        body = {"request_id": uuid4().hex, "current": new_case_snapshot(self.video),
                "product": "测试产品", "note": "导入视频"}
        original_add = self.server.app.store.add

        def add_then_fail(value, **kwargs):
            original_add(value, **kwargs)
            raise OSError("simulated failure after complete commit")

        with patch.object(self.server.app.store, "add", side_effect=add_then_fail):
            status, _, result = self.request("POST", "/api/cases", body)
        self.assertEqual(status, 200, result)
        self.assertIn("已保存", result["warning"])
        owned = self.assert_owned_video(result["case"], self.video_bytes, self.video)
        self.assertEqual(self.request("POST", "/api/cases", body)[2], result)
        self.assertEqual(len(self.store.read_index()["cases"]), 2)
        self.assertEqual(list((self.runtime / "evidence" / "cases").rglob("*.mp4")), [owned])

    def test_missing_existing_asset_stays_readable_and_other_fields_can_be_saved(self):
        self.video.unlink()
        detail = self.detail()
        self.assertFalse(detail["assets"]["video"]["exists"])
        self.assertIsNone(detail["assets"]["video"]["bytes"])
        self.assertEqual(self.request("GET", "/api/state")[0], 200)
        self.assertEqual(self.request("GET", f"/media/{self.identifier}/video")[0], 404)
        status, _, result = self.save(self.body(detail))
        self.assertEqual(status, 200, result)
        self.assertEqual(result["case"]["current"]["assets"]["video"], str(self.video))
        missing = self.body()
        missing["current"]["assets"]["video"] = str(self.root / "another-missing.mp4")
        self.assertEqual(self.save(missing)[0], 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
