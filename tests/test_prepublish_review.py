"""Pre-publish review: result parsing, measured findings, the publication gate and the retrospective hand-off.

The model and the heavy preparation are faked; one test runs the real preparation on a generated clip when
ffmpeg is available."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from creation_store import CreationStore
from elevenlabs_client import VoiceError
from prepublish_review import (DRAFT_FRAMEWORK, FRAMEWORK, MATERIAL_RULES, REVIEW_MODEL, _sheet, _shot_list, draft_mode, draft_prompt, label_shots, parse,
                               parse_card, prepare)
from production_store import ProductionStore
import workbench_prepublish
from workbench_prepublish import PrepublishWorkbenchMixin
from workbench_video_publish import VideoPublishMixin

JOB = "PRD-222222222222"
TAKE = "TAK-222222222222"
SCRIPT = "SCR-222222222222"
POST = "7999999999999999911"
VIDEO = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2" + b"test-video-bytes" * 100
REPLY = """分析略。
```json
{"findings": [
 {"severity": "must_fix", "confidence": "high", "layer": "会不会被封", "at": "21–25", "problem": "提到替换磨板单卖，容易买错款", "consequence": "被投诉", "fix": "删掉这句"},
 {"severity": "suggest", "confidence": "medium", "layer": "开头3秒", "at": "0–3", "problem": "钩子没兑现", "consequence": "没人看", "fix": "补对比"}
], "premortem": "前 3 秒没有理由留下。", "ab_tests": ["开头：切纸结果 vs 让步句"], "summary": "合规风险需先处理。"}
```"""


class FakeBridge:
    def __init__(self, reply=REPLY):
        self.reply, self.calls = reply, []

    def run(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        return self.reply


class FakeAI:
    def __init__(self):
        self.bridge = type("Bridges", (), {"codex": None})()

    @staticmethod
    def resolve_default_settings(body, effort_key="reasoning_effort"):
        return {"model": body.get("model", "claude-default"), effort_key: body.get(effort_key)}

    @staticmethod
    def _review_context(snapshot, action):
        return None

    @staticmethod
    def _review_block(context):
        return ""


def fake_prepare(video, spoken, folder, cancel=None, on_step=None):
    folder = Path(folder)
    (folder / "frames").mkdir(parents=True, exist_ok=True)
    (folder / "opening.jpg").write_bytes(b"jpg")
    (folder / "shots1.jpg").write_bytes(b"jpg")
    return {"duration": 30.0, "shots": [], "sheets": [{"file": "opening.jpg", "label": "开头"}, {"file": "shots1.jpg", "label": "镜头"}],
            "transcript": "[0.0 秒] I sharpened every EDC knife", "lines": [{"line": n + 1, "start": 0.0, "end": 1.0} for n in range(len(spoken))]}


class Bench(VideoPublishMixin, PrepublishWorkbenchMixin):
    def __init__(self, root, bridge):
        self.lock = threading.RLock()
        self.production = ProductionStore(root, self.lock)
        self.creation = CreationStore(root, self.lock)
        self.state_cache = {}
        self.ai, self.editor = FakeAI(), bridge

    def save_script_status(self, identifier, body, *, video_job_id=None):
        body = copy.deepcopy(body)
        body["script_status"]["script_id"] = identifier
        return self.creation.mutate("script_status", body)


def wait(review_id):
    for thread in threading.enumerate():
        if thread.name == "prepublish-" + review_id:
            thread.join(10)


class ParsingTests(unittest.TestCase):
    def test_reply_is_normalised_and_capped(self):
        many = {"findings": [{"severity": "fatal", "confidence": "sure", "layer": "x", "problem": f"p{n}"} for n in range(8)],
                "premortem": "  前   3 秒  ", "summary": "s"}
        result = parse("```json\n" + json.dumps(many, ensure_ascii=False) + "\n```")
        self.assertEqual(5, len(result["findings"]))
        self.assertEqual({"suggest"}, {row["severity"] for row in result["findings"]})
        self.assertEqual("medium", result["findings"][0]["confidence"])
        self.assertEqual("前 3 秒", result["premortem"])
        self.assertEqual({"为什么要买"}, {row["layer"] for row in result["findings"]})
        guess = parse('```json\n{"findings": [{"severity": "must_fix", "confidence": "low", "problem": "疑似 AI 配音"}]}\n```')
        self.assertEqual("suggest", guess["findings"][0]["severity"])

    def test_missing_or_broken_block_fails(self):
        with self.assertRaises(ValueError):
            parse("没有结果块")
        with self.assertRaises(ValueError):
            parse("```json\n{broken}\n```")

    def test_draft_mode_follows_the_data(self):
        self.assertEqual("sold", draft_mode(29, 157326))
        self.assertEqual("sold", draft_mode(1, 120))
        self.assertEqual("unsold", draft_mode(0, 4800))
        self.assertEqual("thin", draft_mode(0, 300))
        self.assertEqual("thin", draft_mode(None, None))
        report = {"duration": 31.0, "shots": [{}], "lines": [], "transcript": "x"}
        unsold = draft_prompt([], report, source="s", review_text="", figures={}, mode="unsold")
        self.assertIn(DRAFT_FRAMEWORK, unsold)
        # A video that did not sell gets no compliance pass and no kill list, only the funnel step it lost people at.
        for gone in ("会不会被封", "合规层面", "被限流", "## 杀手清单", '列一张"杀手清单"'):
            self.assertNotIn(gone, unsold)
        self.assertIn("```retro-card", unsold)
        self.assertIn("## 一句话：这条卡在哪一步", unsold)
        self.assertIn("数据不支持的（那一步并不比同类差）不要写", unsold)
        self.assertIn("## 值得保留的", unsold)
        self.assertNotIn("为什么出单", unsold)
        thin = draft_prompt([], report, source="s", review_text="", figures={}, mode="thin")
        self.assertIn("播放很少", thin)
        self.assertIn("不输出建议动作行", thin)
        self.assertIn("加码、换执行、停测三选一", thin)
        self.assertNotIn("继续观察", thin)
        self.assertNotIn("本轮暂无发现", thin)
        self.assertIn("不能从单条结果认定整个方向不成立", thin)
        self.assertNotIn("停测＝方向不成立", thin)
        self.assertNotIn("会不会被封", thin)
        self.assertNotIn("## 杀手清单", thin)

    def test_card_block_is_trimmed_and_optional(self):
        output = ("## 正文\n```retro-card\n" + json.dumps({
            "verdict": "卡在点击", "reasons": [{"at": "21–24 秒", "point": "没有价格锚点", "fix": "加一行价格"}, {"point": ""}, "x",
                                               {"point": "二"}, {"point": "三"}, {"point": "四"}],
            "keep": ["开头", 3, "节奏"], "next": {"change": "只改 CTA", "hold": "", "other": "x"}}, ensure_ascii=False) + "\n```\n结论：x")
        card = parse_card(output)
        self.assertEqual("卡在点击", card["verdict"])
        self.assertEqual(["没有价格锚点", "二", "三"], [row["point"] for row in card["reasons"]])
        self.assertEqual(["开头", "节奏"], card["keep"])
        self.assertEqual({"change": "只改 CTA"}, card["next"])
        self.assertIsNone(parse_card("## 旧格式的复盘\n结论：x"))
        self.assertIsNone(parse_card("```retro-card\n{broken}\n```"))

    def test_draft_prompt_explains_the_result_with_figures(self):
        figures = {"每周（完播率 finish_rate_pct、点击率 ctr_pct、点击后转化 ctor_pct）": [{"finish_rate_pct": 5.19, "ctr_pct": 3.0}]}
        report = {"duration": 31.0, "shots": [{}], "lines": [], "transcript": "[0.0 秒] I sharpened every EDC knife"}
        text = draft_prompt([], report, source="从 TikTok 下载", review_text="", figures=figures)
        self.assertNotIn(FRAMEWORK, text)
        self.assertNotIn("只许挑毛病", text)
        self.assertIn("5.19", text)
        self.assertIn("[0.0 秒] I sharpened", text)
        for heading in ("## 一句话：这条为什么出单", "## 1. 为什么停下来看", "## 2. 为什么点商品", "## 3. 为什么下单",
                        "## 4. 可以复制的做法", "## 5. 拖后腿的地方", "## 下一轮", "建议动作："):
            self.assertIn(heading, text)
        self.assertIn("不要读取或引用其他文件和案例", text)
        self.assertIn("看不清不等于没有", text)
        blind = draft_prompt([{"number": 1, "spoken": "Try this."}], None, source="没能取得成片（下载失败）", review_text="", figures={})
        self.assertIn("没看到成片，判断不了", blind)
        self.assertIn("L1（时间未对齐）Try this.", blind)


class GateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bridge = FakeBridge()
        self.bench = Bench(self.root, self.bridge)
        self.video = self.root / "Lab261022-202-3.mp4"
        self.video.write_bytes(VIDEO)
        job = {"id": JOB, "product": "202H", "script_id": SCRIPT, "script_version": 1, "script_family": SCRIPT, "title": "Test",
               "direction_id": "202H-S2", "variant_label": "A", "text_sha256": "frozen",
               "lines": [{"number": 1, "spoken": "One stone may already get your knife sharp.", "proof": "切纸", "zh": "一"}],
               "selected_take": TAKE, "takes": [{"id": TAKE, "model_id": "model", "voice_id": "voice", "text": "x"}],
               "timelines": [{"unique_id": "timeline-1", "name": "Lab261022-202-3", "creator": "Lab", "folder": "A本土店渠道号-Lab",
                              "take_id": TAKE, "saved": True}],
               "edits": [], "manual_completion": {"at": "2026-10-01", "take_id": TAKE, "timeline_uid": "timeline-1", "basis": "human_review"},
               "archived": False, "history": []}
        data = self.bench.production.empty()
        data["jobs"] = [job]
        self.bench.production.write(data)
        data = self.bench.creation.empty()
        data["scripts"] = [{"id": SCRIPT, "version": 1, "product": "202H", "status": "adopted", "video_id": "",
                            "published_on": "", "case_id": "", "history": []}]
        self.bench.creation.write(data)

    def review(self, **body):
        with patch.object(workbench_prepublish, "prepare", fake_prepare):
            started = self.bench.start_prepublish_review(JOB, {"request_id": uuid4().hex, "file_path": str(self.video), **body})
            wait(started["review"]["id"])
        return self.bench.prepublish_reviews(JOB)

    def publish(self):
        detail = self.bench.video_publication_detail(JOB)
        return self.bench.save_video_publication(JOB, {"revision": detail["revision"], "source_signature": detail["source_signature"],
                                                       "request_id": uuid4().hex, "post_id": POST, "published_on": "2026-10-01",
                                                       "account": "fixture", "file_path": str(self.video), "file_confirmed": True})

    def test_must_fix_blocks_publication_until_each_is_resolved(self):
        data = self.review()
        review = data["reviews"][0]
        self.assertEqual("completed", review["status"])
        self.assertEqual(["会不会被封", "开头3秒"], [row["layer"] for row in review["findings"]])
        self.assertEqual(["开头：切纸结果 vs 让步句"], review["ab_tests"])
        self.assertEqual(1, data["blocking"])
        call = self.bridge.calls[0]
        self.assertEqual(["opening.jpg", "shots1.jpg"], [Path(path).name for _, path in call["images"]])
        self.assertIn(FRAMEWORK, call["prompt"])
        self.assertIn("L1（0.0–1.0 秒）One stone may already get your knife sharp.", call["prompt"])
        self.assertNotIn("画面证据要求", call["prompt"])
        self.assertNotIn("window_views", call["prompt"])
        with self.assertRaises(VoiceError) as caught:
            self.publish()
        self.assertEqual(409, caught.exception.status)
        result = self.bench.resolve_prepublish_finding(JOB, review["id"], {"finding_id": "F1", "resolution": "accepted", "note": "已核实"})
        self.assertEqual(0, result["blocking"])
        self.assertEqual(POST, self.publish()["publication"]["post_id"])
        handed = self.bench.prepublish_for_video(POST)
        self.assertEqual("前 3 秒没有理由留下。", handed["premortem"])
        self.assertEqual("接受风险", handed["findings"][0]["resolution"])

    def test_same_request_replays_and_bad_reply_fails_without_findings(self):
        body = {"request_id": uuid4().hex, "file_path": str(self.video)}
        self.bench.editor = FakeBridge("没有约定的结果块")
        with patch.object(workbench_prepublish, "prepare", fake_prepare):
            first = self.bench.start_prepublish_review(JOB, body)
            wait(first["review"]["id"])
            again = self.bench.start_prepublish_review(JOB, body)
        self.assertEqual(first["review"]["id"], again["review"]["id"])
        latest = self.bench.prepublish_reviews(JOB)
        self.assertEqual("failed", latest["reviews"][0]["status"])
        self.assertIn("格式不符", latest["reviews"][0]["error"])
        self.assertEqual(0, latest["blocking"])
        self.assertEqual(POST, self.publish()["publication"]["post_id"])

    def test_unfinished_job_and_restart_interruption(self):
        data = self.bench.production.read()
        data["jobs"][0]["manual_completion"] = None
        self.bench.production.write(data)
        with self.assertRaises(VoiceError) as caught:
            self.bench.start_prepublish_review(JOB, {"request_id": uuid4().hex, "file_path": str(self.video)})
        self.assertEqual(409, caught.exception.status)
        folder = self.bench._prepublish_root(JOB) / "PPR-333333333333"
        folder.mkdir(parents=True)
        (folder / "review.json").write_text(json.dumps({"id": "PPR-333333333333", "job_id": JOB, "status": "running",
                                                        "created_at": "2026-10-24", "findings": []}), encoding="utf-8")
        self.assertEqual("interrupted", self.bench.prepublish_reviews(JOB)["reviews"][0]["status"])


class FakeResolveRunner:
    """Stands in for resolve_worker.py: records each command and renders a file when the job finishes."""

    def __init__(self, final="Complete"):
        self.commands, self.final, self.polls, self.refuse = [], final, 0, False

    def __call__(self, args, input=None, **kwargs):
        request = json.loads(input.decode("utf-8"))
        self.commands.append(request)
        command, reply = request["command"], {"ok": True}
        if command == "status":
            reply.update(project="Lab-202H", current_timeline="其他时间线", current_timeline_uid="other-uid", current_timecode="01:00:00:00")
        elif command == "render_start":
            reply = {"ok": False, "error": "达芬奇没能开始渲染"} if self.refuse else {
                "ok": True, "job_id": "job-1", "previous_format": "mov", "previous_codec": "ProRes"}
        elif command == "render_poll":
            self.polls += 1
            reply.update(state="Rendering" if self.polls < 2 else self.final, percent=50 * self.polls, error="Disk full")
        elif command == "render_finish":
            folder = Path(request["render_dir"])
            if self.final == "Complete":
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "timeline.mp4").write_bytes(VIDEO)
            reply["files"] = [str(path) for path in folder.glob("timeline.*")]
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(reply).encode("utf-8"), stderr=b"")


class TimelineReviewTests(unittest.TestCase):
    def setUp(self):
        GateTests.setUp(self)
        from resolve_bridge import ResolveBridge
        self.runner = FakeResolveRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.seen = []

        def prepare_and_note(video, spoken, folder, cancel=None, on_step=None):
            self.seen.append((Path(video).name, Path(video).is_file()))
            return fake_prepare(video, spoken, folder, cancel, on_step)

        patcher = patch.object(workbench_prepublish, "prepare", prepare_and_note)
        patcher.start()
        self.addCleanup(patcher.stop)
        poll = patch.object(workbench_prepublish, "RENDER_POLL", 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    def timeline_review(self, **body):
        started = self.bench.start_prepublish_review(JOB, {"request_id": uuid4().hex, **body})
        wait(started["review"]["id"])
        return self.bench.prepublish_reviews(JOB)["reviews"][0]

    def test_reviews_the_timeline_by_rendering_it_in_davinci(self):
        review = self.timeline_review()
        self.assertEqual("completed", review["status"], review["error"])
        self.assertEqual("达芬奇时间线「Lab261022-202-3」", review["file"]["name"])
        self.assertEqual([("timeline.mp4", True)], self.seen)
        self.assertEqual(["status", "render_start", "render_poll", "render_poll", "render_finish", "restore"],
                         [row["command"] for row in self.runner.commands])
        start = self.runner.commands[1]
        self.assertEqual(("202H", "timeline-1", "A本土店渠道号-Lab"), (start["project"], start["unique_id"], start["folder"]))
        finish, restore = self.runner.commands[-2:]
        self.assertEqual(("mov", "ProRes"), (finish["previous_format"], finish["previous_codec"]))
        self.assertEqual("other-uid", restore["unique_id"])
        self.assertFalse((self.bench._prepublish_root(JOB) / review["id"] / "render").exists())
        self.assertEqual(1, self.bench.prepublish_reviews(JOB)["blocking"])

    def test_a_failed_render_is_reported_and_davinci_is_put_back(self):
        self.runner.final = "Failed"
        review = self.timeline_review()
        self.assertEqual("failed", review["status"])
        self.assertIn("Disk full", review["error"])
        self.assertEqual([], self.seen)
        self.assertEqual(["render_finish", "restore"], [row["command"] for row in self.runner.commands[-2:]])

    def test_a_refused_render_is_not_called_a_format_error(self):
        self.runner.refuse = True
        review = self.timeline_review()
        self.assertEqual("failed", review["status"])
        self.assertEqual("达芬奇没能开始渲染", review["error"])
        self.assertEqual("restore", self.runner.commands[-1]["command"])

    def test_typed_path_still_wins_and_never_touches_davinci(self):
        review = self.timeline_review(file_path=str(self.video))
        self.assertEqual("completed", review["status"])
        self.assertEqual(self.video.name, review["file"]["name"])
        self.assertEqual([], self.runner.commands)

    def test_timeline_choice_and_missing_timeline(self):
        pick = PrepublishWorkbenchMixin._review_timeline
        job = {"selected_take": TAKE, "timelines": [{"unique_id": "a", "take_id": TAKE}, {"unique_id": "b", "take_id": "other"},
                                                    {"unique_id": "c", "take_id": "other", "archived": True}]}
        self.assertEqual("a", pick(job, "")["unique_id"])
        self.assertEqual("b", pick(job, "b")["unique_id"])
        job["selected_take"] = ""
        self.assertEqual("b", pick(job, "")["unique_id"])
        with self.assertRaises(VoiceError) as unknown:
            pick(job, "zzz")
        self.assertEqual(400, unknown.exception.status)
        with self.assertRaises(VoiceError) as missing:
            pick({"timelines": []}, "")
        self.assertEqual(409, missing.exception.status)
        self.assertIn("路径", str(missing.exception))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class PrepareTests(unittest.TestCase):
    def test_real_preparation_on_generated_clip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            clip = root / "clip.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=4",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=4", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
            with patch("prepublish_review._whisper") as model:
                model.return_value.transcribe.return_value = ([], None)
                report = prepare(clip, ["Hello there."], root / "review")
            self.assertTrue((root / "review" / "opening.jpg").is_file())
            self.assertEqual("opening.jpg", report["sheets"][0]["file"])
            self.assertGreaterEqual(len(report["sheets"]), 2)
            self.assertEqual([{"line": 1, "start": None, "end": None}], report["lines"])
            self.assertEqual("", report["transcript"])
            # Full-resolution opening frames go out on their own, beside the small-tile sheets.
            self.assertIn("detail01.jpg", [sheet["file"] for sheet in report["sheets"]])

    def test_a_sheet_page_with_one_leftover_tile_still_renders(self):
        # 8n+1 shot tiles leave one tile on the last page; ffmpeg's xstack refuses a single input.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            frame = root / "frame.jpg"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=270x480", "-frames:v", "1", str(frame)], check=True)
            for count in (1, 2):
                _sheet([(frame, f"#{n} 0.5s") for n in range(count)], root / f"sheet{count}.jpg")
                self.assertTrue((root / f"sheet{count}.jpg").is_file())

    def test_labels_stand_only_where_the_export_still_shows_that_footage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            material, other, export = root / "material.mp4", root / "other.mp4", root / "export.mp4"
            for path, source in ((material, "testsrc2=size=270x480:rate=30:duration=6"), (other, "mandelbrot=size=270x480:rate=30")):
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", source, "-t", "6", "-pix_fmt", "yuv420p", str(path)], check=True)
            # The export plays material 0–2 s, then a shot a human swapped in for material 3–5 s.
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(material), "-i", str(other), "-filter_complex",
                            "[0:v]trim=0:2,setpts=PTS-STARTPTS[a];[1:v]trim=0:2,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1[v]",
                            "-map", "[v]", "-pix_fmt", "yuv420p", str(export)], check=True)
            item = {"media_id": "M1", "media_path": str(material), "name": "material.mp4", "enabled": True}
            timeline = {"tracks": {"video": [{"index": 1, "enabled": True, "items": [
                {**item, "start": 0, "end": 60, "source_start": 0, "source_end": 60, "source_start_time": 0.0},
                {**item, "start": 60, "end": 120, "source_start": 90, "source_end": 150, "source_start_time": 3.0}]}]}}
            candidates = [{"media_id": "M1", "source": [0, 70], "label": "镜面刃特写 X1", "label_status": "verified"},
                          {"media_id": "M1", "source": [80, 160], "label": "切纸测试 X2", "label_status": "verified"}]
            rows = label_shots(export, timeline, candidates, 30.0)
            self.assertEqual(["镜面刃特写 X1", "切纸测试 X2"], [row["label"] for row in rows])
            self.assertEqual([True, False], [row["match"] for row in rows])
            listing = _shot_list(rows)
            self.assertIn("0.0–2.0 秒：镜面刃特写 X1（人工核过的标签） · 已比对一致", listing)
            self.assertIn("2.0–4.0 秒：切纸测试 X2（人工核过的标签） · 成片画面与剪辑记录不一致", listing)


if __name__ == "__main__":
    unittest.main()
