"""Carousel phase two: AI still search from DaVinci, per-slide base pick and import from existing folders (all faked)."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PIL import Image

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError
from production_store import ProductionStore
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven
from test_production_carousel import CANDIDATE
from variant_parse import carousel_text, parse_carousels
from workbench_carousel import CarouselWorkbenchMixin
from workbench_carousel_stills import CarouselStillsMixin
from workbench_editing import EditingWorkbenchMixin

REPORT = 'Found.\n\n```json\n{"slides": {"1": 1, "2": 1}, "gaps": [{"slide": 3, "need": "收口要看到一排刀"}], "questions": []}\n```'


class Runner:
    def __init__(self, project="202H"):
        self.calls, self.project = [], project

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.calls.append(request)
        if request["command"] == "status":
            body = {"ok": True, "project": self.project, "timelines": ["蓝点折刀成品素材", "Lab261010-202-1"],
                    "current_timeline": getattr(self, "timeline", "Lab261010-202-1"),
                    "current_timeline_uid": getattr(self, "timeline_uid", "uid-own"),
                    "current_timecode": getattr(self, "timecode", "00:00:12:03"),
                    "version": "21.1.0.14", "fps": "29.97", "width": "1080", "height": "1920"}
        elif request["command"] == "restore" and getattr(self, "restore_error", ""):
            body = {"ok": False, "error": self.restore_error}
        else:
            body = {"ok": True, "name": "Lab261010-202-1", "timecode": request.get("timecode", "")}
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class Searcher:
    """Stands in for Claude: exports two stills, lists them plus two bad entries, and reports a gap."""

    def __init__(self, block=False):
        self.calls, self.block = [], block

    def run(self, prompt, *, cwd, on_progress, cancel_event, profile=None, **options):
        self.calls.append({"prompt": prompt, "cwd": cwd, "profile": profile, **options})
        folder = Path(cwd)
        for name in ("s01_c1_ZS161.png", "s02_c1_TJR165.png"):
            Image.new("RGB", (108, 192), "gray").save(folder / "stills" / name)
        (folder / "outside.png").write_bytes(b"x")
        (folder / "candidates.json").write_text(json.dumps([
            {"slide": 1, "file": "stills/s01_c1_ZS161.png", "timeline": "蓝点折刀成品素材", "frame": "120", "marker": "ZS161",
             "reason": "俯拍掀盖，七块磨板可辨", "evidence": False},
            {"slide": 2, "file": "stills/s02_c1_TJR165.png", "timeline": "藤次郎", "frame": 88, "marker": "TJR165", "reason": "夹刀", "evidence": True},
            {"slide": 9, "file": "stills/s01_c1_ZS161.png"},
            {"slide": 3, "file": "outside.png"}], ensure_ascii=False), encoding="utf-8")
        if self.block:
            cancel_event.wait(5)
            raise BridgeCancelled("cancelled")
        return REPORT


class CodexSearcher(Searcher):
    """A native Codex connection; keeps its own defaults and session receipts."""

    def __init__(self, block=False):
        super().__init__(block)
        self.available, self.model_reads = True, 0
        self.actual_model, self.actual_effort = None, None
        self.entered, self.release = threading.Event(), threading.Event()

    def status(self):
        return {"available": self.available, "label": "本机 Codex 测试连接",
                "error": None if self.available else "本机 Codex 暂不可用"}

    def models(self):
        self.model_reads += 1
        return {"available": self.available, "models": [{"id": "gpt-6.1-sol", "name": "Codex 当前模型",
            "is_default": True, "default_reasoning_effort": "medium", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]}]}

    def run(self, prompt, *, cwd, on_progress, cancel_event, profile=None, on_event=None, **options):
        if on_event:
            on_event({"type": "session", "model": self.actual_model or options.get("model") or "gpt-6.1-sol",
                      "reasoning_effort": self.actual_effort or options.get("reasoning_effort") or "medium", "skills": []})
        result = super().run(prompt, cwd=cwd, on_progress=on_progress, cancel_event=cancel_event, profile=profile, **options)
        self.entered.set()
        if getattr(self, "wait_after_export", False):
            if not self.release.wait(5):
                raise RuntimeError("隔离测试等待超时")
        return result


class StillBench(Bench, CarouselWorkbenchMixin, CarouselStillsMixin, EditingWorkbenchMixin):
    def __init__(self, root, editor):
        super().__init__(root, FakeEleven())
        self.ai = SimpleNamespace(resolve_default_settings=lambda body, effort_key: {
            "model": body.get("model", "claude-default"), effort_key: body.get(effort_key)})
        self.editor, self.runner = editor, Runner()
        self.resolve = ResolveBridge(runner=self.runner)
        self.edit_skill_dir = Path(root) / "skills" / "davinci-product-video-editing"
        (self.edit_skill_dir / "references").mkdir(parents=True)
        (self.edit_skill_dir / "references" / "sharpal-202h.md").write_text("标记词", encoding="utf-8")
        photo = Path(root) / "skills" / "tiktok-photo-commerce"
        (photo / "references").mkdir(parents=True)
        (photo / "SKILL.md").write_text("---\nname: tiktok-photo-commerce\n---\n# 图文证据边界", encoding="utf-8")
        (photo / "references" / "production.md").write_text("制作规则", encoding="utf-8")
        self.carousel_image_root = Path(root) / "图文素材"


class StillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="carousel-stills-")
        self.addCleanup(temp.cleanup)
        mcp = Path(temp.name) / "ResolveMCP.exe"
        mcp.write_bytes(b"")
        environment = patch.dict(os.environ, {"RESOLVE_MCP": str(mcp)})
        environment.start()
        self.addCleanup(environment.stop)
        self.searcher = Searcher()
        self.bench = StillBench(temp.name, self.searcher)
        self.item = self.carousel()

    def carousel(self):
        found = parse_carousels(CANDIDATE)[0]
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": CANDIDATE, "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "送礼 · 图文", "text": carousel_text(found), "confirmed": True}},
            source)["script"]
        return self.bench.create_carousel({"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                           "carousel": {"script_id": script["id"]}})["carousel"]

    def start(self, **values):
        return self.bench.start_still_search(self.item["id"], {"request_id": uuid4().hex, "materials": ["蓝点折刀成品素材"], **values})

    def wait(self):
        deadline = time.monotonic() + 5
        while self.bench.edit_running() and time.monotonic() < deadline:
            time.sleep(.02)
        return self.bench.production.carousel(self.item["id"])

    def test_search_collects_valid_candidates_restores_the_timeline_and_reports_gaps(self):
        started = self.start(per_slide=2)["search"]
        self.assertEqual(started["slides"], [1, 2, 3])
        item = self.wait()
        search = item["searches"][-1]
        self.assertEqual((search["status"], search["added"], search["report"]["gaps"][0]["slide"]), ("completed", 2, 3))
        self.assertEqual(len(search["problems"]), 2)
        self.assertEqual((search["error"], search["output"]), ("", REPORT))
        first = item["slides"][0]["candidates"][0]
        self.assertEqual((first["marker"], first["frame"], first["width"], first["height"], first["origin"]), ("ZS161", 120, 108, 192, "davinci"))
        self.assertTrue(self.bench.carousel_image(item["id"], first["id"], "thumb").is_file())
        self.assertTrue(item["slides"][1]["candidates"][0]["evidence"])
        self.assertEqual(item["status"], "picking")
        restore = [call for call in self.bench.runner.calls if call["command"] == "restore"]
        self.assertEqual((restore[0]["unique_id"], restore[0]["timecode"]), ("uid-own", "00:00:12:03"))
        self.assertFalse(self.bench.resolve.busy())
        call = self.searcher.calls[0]
        self.assertFalse(any("unsafe" in tool for tool in call["profile"]["allowed"]))
        self.assertIn("mcp__resolve__run_script_unsafe", call["profile"]["denied"])
        self.assertEqual(call["profile"]["dirs"], [])
        self.assertIn("refs/sharpal-202h.md", call["prompt"])
        self.assertEqual(call["skills"], [{"name": "tiktok-photo-commerce", "path": str(Path(call["cwd"]) / "skill" / "SKILL.md")}])
        self.assertTrue((Path(call["cwd"]) / "skill" / "references" / "production.md").is_file())
        self.assertIn("| 1 | 钩子 |", call["prompt"])

    def test_codex_tool_failure_without_manifest_is_failed_and_preserves_explanation(self):
        fake = self.codex()
        output = ('工具启动失败（`code-mode host is disabled`），无法查看或导出静帧，也无法写入 `candidates.json`。'
                  '第 1–3 张均为 0 个候选，画面尚未核验。\n\n```json\n'
                  '{"slides":{"1":0,"2":0,"3":0},"gaps":[{"slide":1,"need":"待核验完整开箱画面"}]}\n```')
        with patch.object(fake, "run", return_value=output):
            self.start(provider="codex", model="codex-default")
            search = self.wait()["searches"][-1]
        self.assertEqual((search["status"], search["added"], search["output"]), ("failed", 0, output))
        self.assertIn("候选清单", search["error"])
        self.assertTrue(any("没有找到 candidates.json" in problem for problem in search["problems"]))
        self.assertEqual(search["report"]["gaps"][0]["need"], "待核验完整开箱画面")
        self.assertFalse(self.bench.resolve.busy())
        self.assertFalse(self.bench.edit_running())
        self.assertTrue(any(call["command"] == "restore" for call in self.bench.runner.calls))

    def test_invalid_manifest_cannot_complete_even_if_model_reports_success(self):
        for contents in ("{", "null", "true", '"done"', "1", "{}", '{"slides":{"1":0}}',
                         '{"candidates":null}', '{"candidates":{}}'):
            with self.subTest(contents=contents):
                def invalid_manifest(*args, **options):
                    (Path(options["cwd"]) / "candidates.json").write_text(contents, encoding="utf-8")
                    return REPORT

                with patch.object(self.searcher, "run", side_effect=invalid_manifest):
                    self.start()
                    search = self.wait()["searches"][-1]
                self.assertEqual((search["status"], search["added"], search["output"]), ("failed", 0, REPORT))
                self.assertIn("候选清单", search["error"])
                self.assertEqual(len(search["problems"]), 1)
                self.assertIn("candidates.json", search["problems"][0])
                self.assertFalse(self.bench.resolve.busy())
                self.assertFalse(self.bench.edit_running())

    def test_valid_empty_manifest_can_complete_without_candidates(self):
        output = '没有找到合格画面。\n\n```json\n{"slides":{"1":0},"gaps":[{"slide":1,"need":"缺开箱画面"}]}\n```'
        for contents in ("[]", '{"candidates":[]}'):
            with self.subTest(contents=contents):
                def empty_manifest(*args, **options):
                    (Path(options["cwd"]) / "candidates.json").write_text(contents, encoding="utf-8")
                    return output

                with patch.object(self.searcher, "run", side_effect=empty_manifest):
                    self.start(slides=[1])
                    search = self.wait()["searches"][-1]
                self.assertEqual((search["status"], search["added"], search["output"]), ("completed", 0, output))
                self.assertEqual((search["error"], search["problems"]), ("", []))
                self.assertEqual(search["report"]["gaps"][0]["need"], "缺开箱画面")

    def test_missing_manifest_preserves_cancelled_and_failed_status_and_original_error(self):
        for failure, status, error in ((BridgeCancelled("cancelled"), "cancelled", "已按你的要求停止。"),
                                       (RuntimeError("连接启动失败"), "failed", "连接启动失败")):
            with self.subTest(status=status):
                with patch.object(self.searcher, "run", side_effect=failure):
                    self.start()
                    search = self.wait()["searches"][-1]
                self.assertEqual((search["status"], search["error"], search["added"]), (status, error, 0))
                self.assertTrue(any("没有找到 candidates.json" in problem for problem in search["problems"]))
                self.assertFalse(self.bench.resolve.busy())
                self.assertFalse(self.bench.edit_running())

    def test_pick_one_base_per_slide_then_ready(self):
        self.start()
        item = self.wait()
        body = lambda **values: {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values}
        with self.assertRaises(VoiceError):
            self.bench.choose_base(item["id"], body(slide=2, candidate=item["slides"][0]["candidates"][0]["id"]))
        for slide in item["slides"][:2]:
            self.bench.choose_base(item["id"], body(slide=slide["number"], candidate=slide["candidates"][0]["id"]))
        folder = self.bench.carousel_image_root / "202H-S2" / "原图"
        folder.mkdir(parents=True)
        Image.new("RGB", (60, 80), "white").save(folder / "03.jpg")
        (self.bench.carousel_image_root / "202H-S2" / "优化预览").mkdir()
        Image.new("RGB", (60, 80)).save(self.bench.carousel_image_root / "202H-S2" / "优化预览" / "skip.jpg")
        listing = self.bench.folder_images(item["id"])
        self.assertEqual([image["path"] for image in listing["images"]], ["202H-S2/原图/03.jpg"])
        self.assertTrue(self.bench.folder_thumb("202H-S2/原图/03.jpg").is_file())
        with self.assertRaises(VoiceError):
            self.bench.folder_thumb("../../etc/passwd.jpg")
        imported = self.bench.import_folder_image(item["id"], {"request_id": uuid4().hex, "slide": 3, "path": "202H-S2/原图/03.jpg"})["candidate"]
        self.assertEqual((imported["origin"], imported["name"]), ("folder", "03.jpg"))
        ready = self.bench.choose_base(item["id"], body(slide=3, candidate=imported["id"]))["carousel"]
        self.assertEqual((ready["status"], ready["slides"][2]["status"]), ("ready", "picked"))
        cleared = self.bench.choose_base(item["id"], body(slide=3, candidate=None))["carousel"]
        self.assertEqual((cleared["status"], cleared["slides"][2]["base"]), ("picking", None))

    def test_search_only_collects_candidates_for_requested_slides(self):
        self.start(slides=[1], per_slide=1)
        item = self.wait()
        self.assertEqual(item["searches"][-1]["slides"], [1])
        self.assertEqual(item["searches"][-1]["added"], 1)
        self.assertEqual([len(slide.get("candidates", [])) for slide in item["slides"]], [1, 0, 0])

    def test_search_does_not_exceed_requested_candidates_per_slide(self):
        original = self.searcher.run

        def extra_candidates(*args, **options):
            output = original(*args, **options)
            manifest = Path(options["cwd"]) / "candidates.json"
            entries = json.loads(manifest.read_text(encoding="utf-8"))
            entries.extend([dict(entries[0]), dict(entries[0]), {**entries[0], "slide": True}])
            manifest.write_text(json.dumps(entries), encoding="utf-8")
            return output

        with patch.object(self.searcher, "run", side_effect=extra_candidates):
            self.start(per_slide=1)
            item = self.wait()
        self.assertEqual(item["searches"][-1]["added"], 2)
        self.assertEqual([len(slide.get("candidates", [])) for slide in item["slides"]], [1, 1, 0])
        self.assertEqual(sum("超过本次要求" in problem for problem in item["searches"][-1]["problems"]), 2)

    def test_restore_failure_is_recorded_without_losing_candidates_or_holding_resolve(self):
        self.bench.runner.restore_error = "没能恢复播放头位置"
        self.start()
        item = self.wait()
        search = item["searches"][-1]
        self.assertEqual((search["status"], search["added"]), ("completed", 2))
        self.assertIn("没能恢复播放头位置", search["readback_error"])
        self.assertFalse(self.bench.resolve.busy())

    def test_wrong_project_is_checked_in_worker_before_spending_ai_usage(self):
        self.bench.runner.project = "别的项目"
        self.start()
        search = self.wait()["searches"][-1]
        self.assertEqual(search["status"], "failed")
        self.assertIn("别的项目", search["error"])
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(search["restore"], {})
        self.assertFalse(any(call["command"] == "restore" for call in self.bench.runner.calls))
        self.assertFalse(self.bench.resolve.busy())
        with self.assertRaises(VoiceError):
            self.start(slides=[9])

    def hold_resolve(self):
        occupied, release = threading.Event(), threading.Event()

        def edit():
            with self.bench.resolve.lock:
                occupied.set()
                release.wait(5)

        thread = threading.Thread(target=edit, name="fake-active-edit")
        thread.start()
        self.addCleanup(lambda: (release.set(), thread.join(3)))
        self.assertTrue(occupied.wait(3))
        return release, thread

    def wait_for_queue(self, search_id):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            live = self.bench.edit_live(search_id)
            if live and "其他任务完成后自动找底图" in live.get("progress", ""):
                return
            time.sleep(.01)
        self.fail("找底图没有进入达芬奇等待队列")

    def test_busy_edit_accepts_queued_search_and_uses_position_at_admission(self):
        release, thread = self.hold_resolve()
        edit_id = "EDT-0123456789ab"
        self.bench._reserve(edit_id)
        self.addCleanup(lambda: self.bench._forget(edit_id))
        self.assertTrue(self.bench.edit_running())
        started = self.start()["search"]
        self.wait_for_queue(started["id"])
        self.assertEqual(started["status"], "running")
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])
        # The editor has moved since submission; restore must use the position
        # read after it releases Resolve, rather than an earlier snapshot.
        self.bench.runner.timeline, self.bench.runner.timeline_uid = "剪辑完成时间线", "uid-after-edit"
        self.bench.runner.timecode = "00:01:02:03"
        self.bench._forget(edit_id)
        release.set()
        thread.join(3)
        search = self.wait()["searches"][-1]
        self.assertEqual((search["status"], search["added"]), ("completed", 2))
        self.assertEqual(len(self.searcher.calls), 1)
        restore = next(call for call in self.bench.runner.calls if call["command"] == "restore")
        self.assertEqual((restore["unique_id"], restore["timecode"]), ("uid-after-edit", "00:01:02:03"))
        self.assertEqual(search["restore"]["unique_id"], "uid-after-edit")
        self.assertFalse(self.bench.resolve.busy())

    def test_queued_search_cancel_does_not_release_the_edit_lock_or_call_resolve(self):
        release, thread = self.hold_resolve()
        started = self.start()["search"]
        self.wait_for_queue(started["id"])
        self.bench.cancel_still_search(self.item["id"], started["id"])
        search = self.wait()["searches"][-1]
        self.assertEqual((search["status"], search["added"]), ("cancelled", 0))
        self.assertEqual(search["restore"], {})
        self.assertTrue(self.bench.resolve.busy())
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])
        release.set()
        thread.join(3)
        self.assertFalse(self.bench.resolve.busy())

    def test_queued_search_excludes_same_carousel_and_accepts_another_carousel(self):
        release, thread = self.hold_resolve()
        first = self.start()["search"]
        self.wait_for_queue(first["id"])
        with self.assertRaises(VoiceError) as caught:
            self.start()
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("这张图文单", str(caught.exception))
        other = self.carousel()
        second = self.bench.start_still_search(other["id"], {"request_id": uuid4().hex,
            "materials": ["蓝点折刀成品素材"]})["search"]
        self.wait_for_queue(second["id"])
        self.assertEqual(self.searcher.calls, [])
        release.set()
        thread.join(3)
        self.wait()
        self.assertEqual(self.bench.production.carousel(self.item["id"])["searches"][-1]["status"], "completed")
        self.assertEqual(self.bench.production.carousel(other["id"])["searches"][-1]["status"], "completed")
        self.assertEqual(len(self.searcher.calls), 2)

    def test_only_same_carousel_image_work_blocks_still_search(self):
        for method in ("autopilot_running", "_carousel_image_running"):
            with self.subTest(method=method):
                with patch.object(self.bench, method, return_value=True, create=True) as running:
                    with self.assertRaises(VoiceError) as caught:
                        self.start()
                    self.assertEqual(caught.exception.status, 409)
                    running.assert_called_once_with(self.item["id"])
                with patch.object(self.bench, method, side_effect=lambda owner: owner == "CAR-ffffffffffff", create=True):
                    self.start()
                    self.assertEqual(self.wait()["searches"][-1]["status"], "completed")

    def test_queued_search_timeout_is_saved_without_releasing_the_edit_lock(self):
        release, thread = self.hold_resolve()
        with patch("workbench_carousel_stills.SEARCH_TIMEOUT", .1):
            self.start()
            search = self.wait()["searches"][-1]
        self.assertEqual((search["status"], search["added"]), ("failed", 0))
        self.assertTrue(search["error"])
        self.assertTrue(self.bench.resolve.busy())
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])
        release.set()
        thread.join(3)

    def test_preparation_or_start_failure_never_releases_another_tasks_lock(self):
        release, thread = self.hold_resolve()
        with patch("workbench_carousel_stills.shutil.copytree", side_effect=OSError("资料复制失败")):
            with self.assertRaises(OSError):
                self.start()
        self.assertFalse(self.bench.edit_running())
        self.assertEqual(self.bench.production.carousel(self.item["id"])["searches"], [])
        with patch("workbench_carousel_stills.threading.Thread.start", side_effect=RuntimeError("线程未启动")):
            with self.assertRaises(VoiceError):
                self.start()
        self.assertFalse(self.bench.edit_running())
        self.assertEqual(self.bench.production.carousel(self.item["id"])["searches"][-1]["status"], "failed")
        self.assertTrue(self.bench.resolve.busy())
        self.assertTrue(thread.is_alive())
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])
        release.set()
        thread.join(3)

    def test_stop_and_restart_are_recorded(self):
        self.searcher.block = True
        started = self.start()["search"]
        deadline = time.monotonic() + 5
        while not self.searcher.calls and time.monotonic() < deadline:
            time.sleep(.02)
        running = self.bench.carousel_detail(self.item["id"])["carousel"]["searches"][-1]
        self.assertEqual(running["status"], "running")
        self.assertIn("progress", running["live"])
        self.bench.cancel_still_search(self.item["id"], started["id"])
        item = self.wait()
        self.assertEqual(item["searches"][-1]["status"], "cancelled")
        self.assertFalse(self.bench.resolve.busy())
        self.assertTrue(any(call["command"] == "restore" for call in self.bench.runner.calls))
        data = self.bench.production.read()
        data["carousels"][0]["searches"][-1]["status"] = "running"
        self.bench.production.write(data)
        restarted = ProductionStore(self.bench.production.root.parent)
        restarted.interrupt_running()
        self.assertEqual(restarted.carousel(self.item["id"])["searches"][-1]["status"], "interrupted")

    def codex(self, **values):
        fake = CodexSearcher(**values)
        self.bench.still_codex = fake
        self.addCleanup(fake.release.set)
        self.addCleanup(self.bench.stop_edits)
        return fake

    def test_codex_default_routes_to_native_bridge_and_keeps_client_model_effort(self):
        fake = self.codex()
        started = self.start(provider="codex", model="codex-default")["search"]
        item = self.wait()
        record = item["searches"][-1]
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.searcher.calls, [])
        call = fake.calls[0]
        self.assertIsNone(call["model"])
        self.assertIsNone(call["reasoning_effort"])
        self.assertEqual(record["provider"], "codex")
        self.assertEqual(record["requested_model"], "codex-default")
        self.assertEqual((record["effective_model"], record["effective_effort"]), ("gpt-6.1-sol", "medium"))
        self.assertIn("Codex", record["client_label"])
        self.assertTrue(record["usage_source"])
        self.assertEqual((record["status"], record["added"]), ("completed", 2))
        self.assertEqual(record["id"], started["id"])
        self.assertTrue(any(entry["command"] == "restore" for entry in self.bench.runner.calls))
        self.assertIn("mcp__resolve__run_script_unsafe", call["profile"]["denied"])
        self.assertEqual(call["profile"]["dirs"], [])
        self.assertFalse(any("unsafe" in name for name in call["profile"]["allowed"]))
        for slide in item["slides"][:2]:
            candidate = slide["candidates"][0]
            self.assertEqual(candidate["search_id"], record["id"])
            self.assertTrue(self.bench.carousel_image(item["id"], candidate["id"], "full").is_file())

    def test_provider_status_and_explicit_claude_still_use_original_client_profile(self):
        fake = self.codex()
        state = self.bench.still_search_status()
        self.assertTrue(state["providers"]["codex"]["available"])
        self.assertTrue(state["providers"]["claude"]["available"])
        self.assertFalse(state["busy"])
        self.assertEqual(fake.calls, [])
        self.start(model="claude-opus", effort="max")
        record = self.wait()["searches"][-1]
        self.assertEqual(record["provider"], "claude")
        self.assertEqual(self.searcher.calls[0]["model"], "claude-opus")
        self.assertEqual(self.searcher.calls[0]["reasoning_effort"], "max")
        self.assertEqual(self.searcher.calls[0]["profile"]["mcp"].keys(), {"resolve"})
        self.assertEqual(fake.calls, [])
        # A bridge that supplied no session receipt must not invent the actual model.
        self.assertIsNone(record["effective_model"])
        self.assertIsNone(record["effective_effort"])

    def test_codex_dynamic_model_supports_low_effort_and_records_actual_session(self):
        fake = self.codex()
        fake.actual_effort = "medium"
        self.start(provider="codex", model="gpt-6.1-sol", effort="low", slides=[1])
        record = self.wait()["searches"][-1]
        self.assertEqual(fake.calls[0]["model"], "gpt-6.1-sol")
        self.assertEqual(fake.calls[0]["reasoning_effort"], "low")
        self.assertEqual(record["requested_model"], "gpt-6.1-sol")
        self.assertEqual(record["effective_effort"], "medium")
        self.assertEqual(record["added"], 1)
        self.assertGreater(fake.model_reads, 0)
        self.assertEqual(self.searcher.calls, [])

    def test_global_default_is_applied_to_still_search_and_saved_with_its_effort(self):
        fake = self.codex()
        self.bench.ai.resolve_default_settings = lambda body, effort_key: {"model": "gpt-6.1-sol", effort_key: "high"}
        self.start(slides=[1])
        record = self.wait()["searches"][-1]
        self.assertEqual((record["provider"], record["model"], record["effort"]), ("codex", "gpt-6.1-sol", "high"))
        self.assertEqual((fake.calls[0]["model"], fake.calls[0]["reasoning_effort"]), ("gpt-6.1-sol", "high"))
        self.assertEqual(self.searcher.calls, [])

    def test_incompatible_global_default_is_rejected_before_resolve_or_ai_work(self):
        self.codex()
        for model, effort in (("claude-haiku", None), ("claude-opus", "low"), ("gpt-6.1-sol", "ultra")):
            with self.subTest(model=model, effort=effort):
                self.bench.ai.resolve_default_settings = lambda body, effort_key: {"model": model, effort_key: effort}
                with self.assertRaises(VoiceError):
                    self.start()
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.still_codex.calls, [])
        self.assertEqual(self.bench.runner.calls, [])

    def test_codex_bad_model_or_effort_is_rejected_without_fallback_or_resolve_work(self):
        fake = self.codex()
        for body in ({"model": "gpt-missing"}, {"model": "gpt-6.1-sol", "effort": "ultra"},
                     {"model": "codex-default", "effort": "high"}):
            with self.subTest(body=body), self.assertRaises(VoiceError) as caught:
                self.start(provider="codex", **body)
            self.assertEqual(caught.exception.status, 400)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])
        self.assertEqual(self.bench.production.carousel(self.item["id"])["searches"], [])

    def test_codex_unavailable_does_not_switch_to_available_claude(self):
        fake = self.codex()
        fake.available = False
        with self.assertRaises(VoiceError) as caught:
            self.start(provider="codex", model="codex-default")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(self.bench.runner.calls, [])

    def test_codex_default_survives_missing_model_catalog_and_unavailable_claude(self):
        fake = self.codex()
        self.bench.editor = None
        with patch.object(fake, "models", return_value={"available": False, "models": [], "error": "当前模型列表暂不可用"}) as models:
            self.start(provider="codex", model="codex-default")
            record = self.wait()["searches"][-1]
            self.assertEqual(record["status"], "completed")
            self.assertEqual(models.call_count, 0)
            before_resolve = len(self.bench.runner.calls)
            with self.assertRaises(VoiceError) as caught:
                self.start(provider="codex", model="gpt-6.1-sol")
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(len(self.bench.runner.calls), before_resolve)
            self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.searcher.calls, [])
        self.assertEqual(len(self.bench.production.carousel(self.item["id"])["searches"]), 1)

    def test_thread_start_failure_preserves_failed_record_without_holding_resolve(self):
        fake = self.codex()
        # Limit the start failure to this request; fixture construction is complete.
        with patch("workbench_carousel_stills.threading.Thread.start", side_effect=RuntimeError("隔离测试启动失败")):
            with self.assertRaises(VoiceError) as caught:
                self.start(provider="codex", model="codex-default")
        self.assertEqual(caught.exception.status, 503)
        record = self.bench.production.carousel(self.item["id"])["searches"][-1]
        self.assertEqual(record["status"], "failed")
        self.assertTrue(record["finished_at"])
        self.assertEqual(record["added"], 0)
        self.assertIn("未能启动", record["error"])
        self.assertFalse(self.bench.resolve.busy())
        self.assertFalse(self.bench.edit_running())
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.searcher.calls, [])

    def test_codex_cancel_preserves_exported_candidates_and_restores_own_timeline(self):
        fake = self.codex(block=True)
        started = self.start(provider="codex", model="codex-default")["search"]
        deadline = time.monotonic() + 3
        while not fake.calls and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(fake.calls)
        running = self.bench.carousel_detail(self.item["id"])["carousel"]["searches"][-1]
        self.assertEqual(running["provider"], "codex")
        self.bench.cancel_still_search(self.item["id"], started["id"])
        record = self.wait()["searches"][-1]
        self.assertEqual(record["status"], "cancelled")
        self.assertEqual(record["added"], 2)
        self.assertFalse(self.bench.resolve.busy())
        self.assertTrue(any(entry["command"] == "restore" for entry in self.bench.runner.calls))
        self.assertEqual(self.searcher.calls, [])

    def test_cancel_still_search_cannot_cancel_another_carousel_task(self):
        fake = self.codex(block=True)
        started = self.start(provider="codex", model="codex-default")["search"]
        deadline = time.monotonic() + 3
        while not fake.calls and time.monotonic() < deadline:
            time.sleep(.01)
        with self.assertRaises(VoiceError) as caught:
            self.bench.cancel_still_search("CAR-ffffffffffff", started["id"])
        self.assertEqual(caught.exception.status, 404)
        self.assertFalse(self.bench._edit_cancel[started["id"]].is_set())
        self.bench.cancel_still_search(self.item["id"], started["id"])
        self.wait()

    def test_codex_restart_late_completion_cannot_resurrect_or_collect_old_task(self):
        fake = self.codex()
        fake.wait_after_export = True
        started = self.start(provider="codex", model="codex-default")["search"]
        self.assertTrue(fake.entered.wait(3))
        restarted = ProductionStore(self.bench.production.root.parent, self.bench.lock)
        restarted.interrupt_running()
        interrupted = restarted.carousel(self.item["id"])["searches"][-1]
        self.assertEqual(interrupted["status"], "interrupted")
        fake.release.set()
        item = self.wait()
        self.assertEqual(item["searches"][-1], interrupted)
        self.assertEqual([len(slide["candidates"]) for slide in item["slides"]], [0, 0, 0])
        self.assertFalse(self.bench.resolve.busy())


if __name__ == "__main__":
    unittest.main()
