"""收为案例后的自动下载与拆解：用假下载器和假拆解，在临时案例库里走完整 HTTP 流程。"""
from __future__ import annotations

import copy
import http.client
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


APP_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("workbench_server_auto_breakdown", APP_ROOT / "server.py")
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)
import case_auto_breakdown as AUTO  # noqa: E402  (server.py put APP_ROOT on sys.path)
import case_breakdown_runner as RUNNER  # noqa: E402

VIDEO_ID = "7664986913497009439"


def snapshot():
    return {
        "title": "收为案例测试", "category": "转化正向参考",
        "conclusion": "测试结论。", "kind": "performance",
        "assessments": [{"dimension": "conversion", "polarity": "positive", "reason": "测试"}],
        "metrics": {"video_id": VIDEO_ID, "creator_handle": "@example"},
        "evidence": {"status": "user_reported", "source": "测试", "context": "测试"},
        "assets": {}, "highlights": [], "limitations": [],
    }


def fake_download(url, folder, video_id):
    target = folder / f"{video_id}.mp4"
    target.write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8)
    return target


class WorkbenchCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="auto-breakdown-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.skill, self.runtime = root / "skill", root / "runtime"
        store = SERVER.CaseStore(self.skill)
        case = store.add({"title": "收为案例测试", "product": "SHARPAL 202H",
                          "original": {"text": "原文", "source": "测试", "url": f"https://www.tiktok.com/@example/video/{VIDEO_ID}"},
                          "feedback": {"kind": "editorial", "text": "首次收录"}, "current": snapshot()})
        self.identifier = case["case_id"]
        self.silence = patch.object(SERVER.Handler, "log_message", lambda *args: None)
        self.silence.start()
        self.addCleanup(self.silence.stop)
        self.server = SERVER.make_server(self.skill, self.runtime, port=0, allow_open=False, performance_sources=[])
        self.app = self.server.app
        # Download tests use the waiting analyzer; RunnerTests below drive the real one with a fake model.
        self.app.auto_breakdown.analyzer = AUTO.pending_analyzer
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        _, state = self.request("GET", "/api/state")
        self.token = state["token"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path, body=None):
        headers = {}
        raw = None
        if method == "POST":
            headers["X-Workbench-Token"] = getattr(self, "token", "")
            raw = json.dumps(body or {}).encode("utf-8")
            headers.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def wait(self):
        for _ in range(200):
            status, body = self.request("GET", f"/api/cases/{self.identifier}/auto-breakdown")
            self.assertEqual(status, 200, body)
            if body["job"] and body["job"]["status"] not in AUTO.ACTIVE:
                return body["job"]
            time.sleep(0.05)
        self.fail("自动拆解没有结束")


class AutoBreakdownTests(WorkbenchCase):
    def test_download_attaches_video_and_waits_for_analyzer(self):
        self.app.auto_breakdown.downloader = fake_download
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {"video_id": VIDEO_ID})
        self.assertEqual(status, 200, body)
        job = self.wait()
        self.assertEqual(job["status"], "analyzer_pending", job)
        self.assertEqual(job["video_source"], "从 TikTok 下载")
        video = Path(self.app.store.read_case(self.identifier)["current"]["assets"]["video"])
        self.assertTrue(video.is_file())
        self.assertEqual(self.app.video_files.owner(video), self.identifier)
        imports = self.app.video_files.imports
        self.assertEqual([p for p in imports.rglob("*") if p.is_file()], [], "临时下载文件应清理")
        runs = self.app.activity.feed(kind="case")
        self.assertTrue(any(run["case_id"] == self.identifier for run in runs.get("runs", runs.get("items", []))), runs)

    def test_failed_download_reports_reason_then_retry_runs_analyzer(self):
        def broken(url, folder, video_id):
            raise AUTO.DownloadError("原视频没有下载成功。测试原因。")
        self.app.auto_breakdown.downloader = broken
        self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {})
        job = self.wait()
        self.assertEqual(job["status"], "failed")
        self.assertIn("测试原因", job["message"])
        seen = {}
        def analyzer(app, case_id, video_path, job):
            seen["video"] = video_path
            return {"summary": "测试拆解"}
        self.app.auto_breakdown.downloader = fake_download
        self.app.auto_breakdown.analyzer = analyzer
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {"retry": True})
        self.assertEqual(status, 200, body)
        job = self.wait()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["analysis"], {"summary": "测试拆解"})
        self.assertTrue(Path(seen["video"]).is_file())
        # A completed job is not restarted without an explicit retry.
        _, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {})
        self.assertEqual(body["job"]["run_id"], job["run_id"])

    def test_case_without_video_id_is_refused(self):
        case = self.app.store.read_case(self.identifier)
        current = copy.deepcopy(case["current"])
        current["metrics"].pop("video_id")
        self.app.store.append(self.identifier, {"kind": "editorial", "text": "去掉视频 ID", "current": current})
        original = case["original"]
        with patch.object(self.app.store, "read_case", return_value={**self.app.store.read_case(self.identifier), "original": {**original, "url": ""}}):
            status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {})
        self.assertEqual(status, 400, body)

    def test_page_parser_finds_play_address(self):
        data = {"__DEFAULT_SCOPE__": {"webapp.video-detail": {"itemInfo": {"itemStruct": {"video": {"playAddr": "https://v.example/a.mp4"}}}}}}
        page = f'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__" type="application/json">{json.dumps(data)}</script>'
        self.assertEqual(AUTO._find_play_url(page), "https://v.example/a.mp4")

    def test_interrupted_job_is_marked_after_restart(self):
        job = {"case_id": self.identifier, "run_id": "ABD-000000000000", "video_id": VIDEO_ID, "status": "downloading",
               "product": "SHARPAL 202H", "title": "x", "message": ""}
        self.app.auto_breakdown._write(job)
        self.assertEqual(self.app.auto_breakdown.status(self.identifier)["status"], "interrupted")


def peers(n=20):
    """Synthetic 202H sales rows: views 5k..100k, 1% CTR, ~1% CTOR."""
    rows = []
    for i in range(n):
        views = 5000 + i * 5000
        clicks = views // 100
        rows.append({"video_id": f"76{i:017d}", "product": "202H", "products": "SHARPAL 202H", "views": views, "clicks": clicks,
                     "orders": max(0, clicks // 100), "headline": f"peer {i}", "posted_on": "2026-08-01", "account": "a"})
    return rows


class InsightUnitTests(unittest.TestCase):
    def test_funnel_tells_traffic_from_conversion(self):
        import case_insights as CI
        base = peers()
        conversion = {"video_id": "1" * 19, "product": "202H", "views": 20000, "clicks": 400, "orders": 20}
        traffic = {"video_id": "2" * 19, "product": "202H", "views": 900000, "clicks": 9000, "orders": 9}
        self.assertEqual(CI.funnel(conversion, base + [conversion])["key"], "conversion")
        self.assertEqual(CI.funnel(traffic, base + [traffic])["key"], "traffic")
        silent = {"video_id": "3" * 19, "product": "202H", "views": 900000, "clicks": 9000, "orders": 0}
        self.assertEqual(CI.funnel(silent, base + [silent])["key"], "views_no_orders")

    def test_contrasts_pick_similar_reach_with_far_fewer_orders(self):
        import case_insights as CI
        base = peers()
        winner = {"video_id": "4" * 19, "product": "202H", "views": 50000, "clicks": 1500, "orders": 30, "posted_on": "2026-08-05"}
        picks = CI.contrasts(winner, base + [winner])
        self.assertTrue(picks)
        for pick in picks:
            self.assertLessEqual(pick["orders"], 7)
            self.assertTrue(25000 <= pick["views"] <= 100000)

    def test_frontmatter_and_transcript_gate(self):
        import case_insights as CI
        with tempfile.TemporaryDirectory() as folder:
            doc = Path(folder) / "sources" / "video-cases" / "own_products" / "x.md"
            doc.parent.mkdir(parents=True)
            doc.write_text("---\nskeleton_id: S12\nhook_types: [ResultHook, IdentityHook]\nduration_sec: 21.5\n---\n# t\n", encoding="utf-8")
            case = {"current": {"source_case": {"file_path": "C:\\Users\\x\\skill\\sources\\video-cases\\own_products\\x.md"},
                                "test_direction": {"direction_id": "202H-S4"}, "transcript": {"text": "hi", "status": "机器转写，未人工逐字听辨", "source": "asr"}}}
            tags = CI.CaseTags(folder).derive(case, folder)
        self.assertEqual(tags["skeleton_id"], "S12")
        self.assertEqual(tags["hook_types"], ["ResultHook", "IdentityHook"])
        self.assertEqual(tags["lead_code"], "S4")
        self.assertEqual(tags["carrier"], "口播")
        self.assertFalse(CI.transcript_gate(case)["verbatim_ok"])

    def test_analysis_tags_win_over_frontmatter(self):
        import case_insights as CI
        with tempfile.TemporaryDirectory() as folder:
            tags = CI.CaseTags(folder)
            tags.update("PVR-000000000001", {"skeleton_id": "S3"}, "analysis")
            tags.update("PVR-000000000001", {"skeleton_id": "S12", "hook_types": ["ResultHook"]}, "frontmatter")
            entry = tags.get("PVR-000000000001")
        self.assertEqual(entry["skeleton_id"], "S3")
        self.assertEqual(entry["hook_types"], ["ResultHook"])


class BatchCollectTests(AutoBreakdownTests):
    def setUp(self):
        super().setUp()
        self.rows = peers() + [{"video_id": "7680000000000000001", "product": "202H", "products": "SHARPAL 202H", "views": 40000,
                                "clicks": 900, "orders": 25, "headline": "batch winner", "title": "Batch winner #fyp",
                                "posted_on": "2026-08-03", "account": "shop"}]
        self.videos = patch.object(self.app.performance, "videos", side_effect=lambda links=None: [dict(r, case_id=(links or {}).get(r["video_id"])) for r in self.rows])
        self.videos.start()
        self.addCleanup(self.videos.stop)
        self.app.auto_breakdown.downloader = fake_download

    def test_batch_creates_cases_and_feeds_breakdown_inputs(self):
        status, body = self.request("POST", "/api/performance/batch-collect", {"video_ids": ["7680000000000000001", "7680000000000000001", "123"]})
        self.assertEqual(status, 200, body)
        results = body["results"]
        self.assertEqual(results[0]["status"], "queued", results)
        self.assertEqual(results[1]["status"], "skipped")
        self.assertEqual(results[2]["status"], "skipped")
        self.identifier = results[0]["case_id"]
        job = self.wait()
        self.assertEqual(job["status"], "analyzer_pending", job)
        self.assertEqual(job["inputs"]["funnel"]["key"], "conversion")
        self.assertTrue(job["inputs"]["contrasts"])
        case = self.app.store.read_case(self.identifier)
        self.assertEqual(case["current"]["metrics"]["video_id"], "7680000000000000001")
        status, body = self.request("GET", f"/api/cases/{self.identifier}/insights")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["insights"]["tags"]["funnel"], "conversion")
        status, body = self.request("GET", "/api/case-tags/stats?product=202H")
        self.assertEqual(status, 200, body)
        self.assertIn("conversion", body["stats"]["funnel"])

    def test_batch_downloads_run_four_at_a_time(self):
        import threading as th
        state = {"now": 0, "peak": 0}
        guard = th.Lock()
        def slow(url, folder, video_id):
            with guard:
                state["now"] += 1; state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.3)
            with guard:
                state["now"] -= 1
            return fake_download(url, folder, video_id)
        self.app.auto_breakdown.downloader = slow
        ids = [r["video_id"] for r in self.rows[:10]]
        status, body = self.request("POST", "/api/performance/batch-collect", {"video_ids": ids})
        self.assertEqual(status, 200, body)
        cases = [r["case_id"] for r in body["results"] if r["status"] == "queued"]
        self.assertEqual(len(cases), 10, body)
        for case_id in cases:
            self.identifier = case_id
            self.assertEqual(self.wait()["status"], "analyzer_pending")
        self.assertEqual(state["peak"], AUTO.PARALLEL_DOWNLOADS)

    def test_batch_limit(self):
        status, body = self.request("POST", "/api/performance/batch-collect", {"video_ids": [str(7680000000000000000 + i) for i in range(11)]})
        self.assertEqual(status, 400, body)


DOC = """---
library_id: PENDING
case_structure_version: 1
tier: T2
title: 测试拆解
short_name: 测试短名
hook_types: [IdentityHook, ResultFirst]
skeleton_id: S13
duration_sec: 21.5
transcript_status: none
---
# 测试拆解
STRUCTURE-OK
"""


class FakeBridge:
    accepts_video = True

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def run(self, prompt, **options):
        self.calls.append({"prompt": prompt, **options})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeRuntime:
    def __init__(self, root):
        pass

    def prepare(self, action, selection):
        return {"ready": True, "skills": [{"name": "product-video-case-analysis", "path": "skill"}],
                "references": [{"label": "模板", "path": "sources/video-cases/case_template.md", "text": "TEMPLATE-COPY"}],
                "instructions": "SKILL-INSTRUCTIONS", "unavailable": []}


class RunnerTests(WorkbenchCase):
    def setUp(self):
        super().setUp()
        template = self.skill / "sources" / "video-cases" / "case_template.md"
        template.parent.mkdir(parents=True, exist_ok=True)
        template.write_text("---\nlibrary_id: VSL-T3-000\n---\n# 模板\n", encoding="utf-8")
        runtime = patch.object(RUNNER, "SkillRuntime", FakeRuntime)
        runtime.start()
        self.addCleanup(runtime.stop)
        self.validated = []
        def validator(text):
            self.validated.append(text)
            return [] if "STRUCTURE-OK" in text else ["缺少第 3 节固定 H3 块"]
        self.app.auto_breakdown.analyzer = RUNNER.CaseBreakdownRunner(validator=validator)
        self.app.auto_breakdown.downloader = fake_download
        self.app.ai.preferences = lambda **kw: {"model": "gpt-test", "reasoning_effort": "high"}

    def use(self, *replies):
        self.bridge = FakeBridge(replies)
        self.app.ai.bridge = self.bridge
        return self.bridge

    def run_job(self):
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {"video_id": VIDEO_ID, "retry": True})
        self.assertEqual(status, 200, body)
        return self.wait()

    def test_breakdown_is_saved_as_source_document(self):
        bridge = self.use(DOC)
        job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["analysis"]["library_id"], "VSL-T2-001")
        call = bridge.calls[0]
        self.assertTrue(call["require_image_view"])
        self.assertTrue(Path(call["video_path"]).is_file())
        self.assertEqual(call["model"], "gpt-test")
        self.assertIn("只分析、不入库", call["prompt"])
        self.assertIn("202H_镜面进阶", call["prompt"], "要告诉模型旧示例不是严格结构")
        self.assertEqual(call["prompt"].count("TEMPLATE-COPY"), 0, "模板只放一次（用实时文件）")
        source = self.app.store.read_case(self.identifier)["current"]["source_case"]
        self.assertEqual(source["library_id"], "VSL-T2-001")
        self.assertEqual(source["transcript_status"], "none")
        text = Path(source["file_path"]).read_text(encoding="utf-8")
        self.assertIn("library_id: VSL-T2-001", text)
        self.assertTrue(Path(source["file_path"]).name.startswith("202H_测试短名"))
        tags = self.app.auto_breakdown.tags.get(self.identifier)
        self.assertEqual(tags["hook_types"], ["IdentityHook", "ResultFirst"])
        self.assertEqual(tags["sources"]["skeleton_id"], "analysis")
        # A second breakdown of another case takes the next number.
        self.use(DOC)
        other = self.app.store.add({"title": "第二条", "product": "SHARPAL 202H", "original": {"text": "x", "source": "测试",
                                    "url": "https://www.tiktok.com/@e/video/7664986913497009440"},
                                    "feedback": {"kind": "editorial", "text": "收录"}, "current": {**snapshot(), "metrics": {"video_id": "7664986913497009440"}}})
        self.identifier = other["case_id"]
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {})
        self.assertEqual(self.wait()["analysis"]["library_id"], "VSL-T2-002")

    def test_structure_errors_get_one_repair_round(self):
        bridge = self.use("```markdown\n" + DOC.replace("STRUCTURE-OK", "缺东西") + "\n```", DOC)
        job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(len(bridge.calls), 2)
        self.assertIn("缺少第 3 节固定 H3 块", bridge.calls[1]["prompt"])

    def test_two_failed_validations_keep_a_draft_and_save_nothing(self):
        self.use("不是文档", DOC.replace("STRUCTURE-OK", "还是缺"))
        job = self.run_job()
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("严格校验", job["message"])
        self.assertNotIn("处理失败", job["message"])
        self.assertTrue((self.runtime / "evidence" / "auto-breakdown" / f"{self.identifier}-draft.md").is_file())
        self.assertNotIn("source_case", self.app.store.read_case(self.identifier)["current"])

    def test_expired_codex_login_falls_back_to_claude(self):
        bridge = self.use(RuntimeError("Codex 未登录，请重新登录"), DOC)
        job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual([c["model"] for c in bridge.calls], ["gpt-test", RUNNER.FALLBACK_MODEL])
        self.assertEqual(job["analysis"]["model"], RUNNER.FALLBACK_MODEL)

    def test_manual_run_uses_the_picked_model_without_fallback(self):
        bridge = self.use(DOC)
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown",
                                    {"retry": True, "model": "claude-sonnet-5-5", "reasoning_effort": "max"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["job"]["model"], "claude-sonnet-5-5")
        job = self.wait()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual((bridge.calls[0]["model"], bridge.calls[0]["reasoning_effort"]), ("claude-sonnet-5-5", "max"))
        self.use(RuntimeError("Codex 未登录"))
        self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {"retry": True, "model": "gpt-6-astra"})
        job = self.wait()
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(len(self.bridge.calls), 1, "选定的模型出错时不应自动换模型")
        status, body = self.request("POST", f"/api/cases/{self.identifier}/auto-breakdown", {"retry": True, "reasoning_effort": "turbo"})
        self.assertEqual(status, 400, body)

    def test_reply_streams_into_the_job_while_writing(self):
        seen = {}
        test = self
        class Streaming(FakeBridge):
            def run(self, prompt, **options):
                options["on_progress"]("正在从原片抽取画面。")
                options["on_event"]({"type": "message", "item_id": "a", "phase": "final_answer", "text": "## 1. 案例基本信息", "complete": False})
                seen["mid"] = test.app.auto_breakdown._read(test.identifier).get("live")
                options["on_event"]({"type": "reasoning", "text": "不该显示"})
                return super().run(prompt, **options)
        self.app.ai.bridge = Streaming([DOC])
        job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(seen["mid"]["text"], "## 1. 案例基本信息")
        self.assertEqual(seen["mid"]["stage"], "写拆解")
        self.assertIn("抽取画面", seen["mid"]["note"])
        self.assertIsNone(job.get("live"))

    def test_soundtrack_is_transcribed_before_the_breakdown(self):
        words = [{"text": "I", "start": 0.0, "end": 0.2, "type": "word"}, {"text": " ", "type": "spacing"},
                 {"text": "tested", "start": 0.25, "end": 0.6, "type": "word"}, {"text": " ", "type": "spacing"},
                 {"text": "it.", "start": 0.62, "end": 0.9, "type": "word"},
                 {"text": "Cuts", "start": 2.5, "end": 2.8, "type": "word"}, {"text": " ", "type": "spacing"},
                 {"text": "clean", "start": 2.85, "end": 3.2, "type": "word"}]
        class Voices:
            calls = []
            def transcribe(self, audio, filename):
                self.calls.append(filename)
                return {"words": words, "language_code": "eng", "model_id": "scribe_v1"}
        self.app.voices = Voices()
        def audio(video_path, folder):
            target = Path(folder) / "audio.mp3"
            target.write_bytes(b"0" * 2000)
            return target
        with patch.object(RUNNER, "_audio", audio):
            bridge = self.use("1. 我测过了。\n切得很干净", DOC)
            job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        current = self.app.store.read_case(self.identifier)["current"]
        transcript = current["transcript"]
        self.assertEqual(current["translation_zh"], {"channel": "transcript", "source_text": transcript["text"], "text": "我测过了。\n切得很干净"})
        bridge.calls.pop(0)  # the translation call
        self.assertEqual(transcript["text"], "[0.00s - 0.90s] I tested it.\n[2.50s - 3.20s] Cuts clean")
        self.assertEqual(transcript["status"], RUNNER.ASR_STATUS)
        self.assertIn("[0.00s - 0.90s] I tested it.", bridge.calls[0]["prompt"])
        self.assertIn('"real_audio_asr_with_timestamps": true', bridge.calls[0]["prompt"])
        # A second run keeps the saved transcript instead of paying for it again.
        self.use(DOC)
        with patch.object(RUNNER, "_audio", audio):
            self.run_job()
        self.assertEqual(len(Voices.calls), 1)

    def test_failed_transcription_does_not_stop_the_breakdown(self):
        class Voices:
            def transcribe(self, audio, filename):
                raise ValueError("ElevenLabs 额度不足")
        self.app.voices = Voices()
        def audio(video_path, folder):
            target = Path(folder) / "audio.mp3"; target.write_bytes(b"0" * 2000); return target
        with patch.object(RUNNER, "_audio", audio):
            bridge = self.use(DOC)
            job = self.run_job()
        self.assertEqual(job["status"], "completed", job)
        self.assertIn("额度不足", bridge.calls[0]["prompt"])
        self.assertNotIn("transcript", self.app.store.read_case(self.identifier)["current"])

    def test_fixed_field_labels_lose_their_notes(self):
        doc = "| 槽位 | 本案例的取舍 |\n|---|---|\n| 受众切入（推断） | 磨刀新手 |\n| 核心矛盾 | 角度难稳 |"
        self.assertEqual(RUNNER.tidy_fixed_labels(doc).splitlines()[2], "| 受众切入 | （推断）磨刀新手 |")
        self.assertEqual(RUNNER.tidy_fixed_labels(doc).splitlines()[3], "| 核心矛盾 | 角度难稳 |")

    def test_transcribe_button_endpoint(self):
        status, body = self.request("POST", f"/api/cases/{self.identifier}/transcribe", {})
        self.assertEqual(status, 400, body)
        self.assertIn("还没有可用的视频", body.get("error", json.dumps(body, ensure_ascii=False)))
        self.app.auto_breakdown.analyzer = AUTO.pending_analyzer
        self.run_job()  # downloads and attaches the video
        class Voices:
            def transcribe(self, audio, filename):
                return {"words": [{"text": "Sharp.", "start": 0.1, "end": 0.5, "type": "word"}], "model_id": "scribe_v1"}
        self.app.voices = Voices()
        def audio(video_path, folder):
            target = Path(folder) / "audio.mp3"; target.write_bytes(b"0" * 2000); return target
        with patch.object(RUNNER, "_audio", audio):
            self.use(RuntimeError("AI 不可用"))
            status, body = self.request("POST", f"/api/cases/{self.identifier}/transcribe", {})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["transcript"]["text"], "[0.10s - 0.50s] Sharp.")
            self.assertIn("中文对照没生成成功", body["note"])
            self.use("锋利。")
            status, body = self.request("POST", f"/api/cases/{self.identifier}/transcribe", {"translate_only": True})
            self.assertEqual(status, 200, body)
            self.assertEqual(body["translation_zh"]["text"], "锋利。")
            self.use("锋利。")
            status, body = self.request("POST", f"/api/cases/{self.identifier}/transcribe", {})
            self.assertEqual(status, 400, "已有文案时不确认就不覆盖")
            status, body = self.request("POST", f"/api/cases/{self.identifier}/transcribe", {"replace": True})
            self.assertEqual(status, 200, body)

    def test_structure_failures_get_one_automatic_rerun(self):
        self.use("不是文档", "还不是")
        self.assertEqual(self.run_job()["status"], "failed")
        self.use(DOC)
        self.assertEqual(self.app.auto_breakdown.resume_pending(), [self.identifier])
        self.assertEqual(self.wait()["status"], "completed")
        self.use("不是文档", "还不是")
        self.run_job()
        job = self.app.auto_breakdown._read(self.identifier)
        job["resumed_after_structure"] = True
        self.app.auto_breakdown._write(job)
        self.assertEqual(self.app.auto_breakdown.resume_pending(), [])

    def test_pending_cases_are_rerun_once_the_analyzer_is_wired(self):
        runner = self.app.auto_breakdown.analyzer
        self.app.auto_breakdown.analyzer = AUTO.pending_analyzer
        self.assertEqual(self.run_job()["status"], "analyzer_pending")
        self.assertEqual(self.app.auto_breakdown.resume_pending(), [], "等待中的拆解器不应补跑")
        self.app.auto_breakdown.analyzer = runner
        self.use(DOC)
        self.assertEqual(self.app.auto_breakdown.resume_pending(), [self.identifier])
        job = self.wait()
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["video_source"], "案例已有视频")


if __name__ == "__main__":
    unittest.main()
