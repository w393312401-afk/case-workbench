"""Production phase three: the unattended AI editing task, with a fake Claude bridge and a fake Resolve worker."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError
from production_store import ProductionStore
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven, CANDIDATE
from workbench_editing import EditingWorkbenchMixin, edit_summary, final_report, handoff_markdown

REPORT = 'Done.\n\n```json\n{"timeline": "Lab261010-202-1", "shots": 3, "gaps": [], "questions": ["开头要不要换切纸？"], "saved": true}\n```'


def inspect_reply(gaps=(), voice_end=400, shots=((0, 25), (25, 50), (50, 200))):
    markers = [{"frame": f, "color": "Blue", "name": f"#{n}", "note": "", "duration": 10, "custom": f"workbench-group:{{job}}:{n}"}
               for n, f in ((1, 0), (2, 120), (3, 260))]
    markers += [{"frame": a, "color": "Red", "name": "缺素材｜切纸结果", "note": "需补实拍", "duration": b - a, "custom": "missing"} for a, b in gaps]
    return {"ok": True, "name": "Lab261010-202-1", "start_frame": 0, "end_frame": voice_end, "fps": "29.97",
            "tracks": {"video": [{"index": 1, "name": "视频 1", "items": [{"start": a, "end": b, "name": "素材.mp4"} for a, b in shots]}],
                       "audio": [{"index": 1, "name": "音频 1", "items": []},
                                 {"index": 2, "name": "配音", "items": [{"start": 0, "end": voice_end, "name": "x_配音"}]}]},
            "markers": markers}


class Runner:
    def __init__(self):
        self.calls, self.inspect = [], inspect_reply()

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.calls.append(request)
        if request["command"] == "inspect":
            body = json.loads(json.dumps(self.inspect).replace("{job}", self.job))
        elif request["command"] == "build":
            body = {"ok": True, "name": request["prefix"] + "1", "unique_id": "uid-1", "start_frame": 0, "end_frame": 400,
                    "audio_items": [{"start": 0, "end": 400}], "markers": [{"frame": m["frame"]} for m in request["markers"]]}
        else:
            body = {"ok": True, "project": "202H", "timelines": ["蓝点折刀成品素材", "Lab261010-202-1"]}
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class Editor:
    """Stands in for ClaudeBridge.run with a tool profile."""

    def __init__(self, block=False, error=None):
        self.calls, self.block, self.error, self.started = [], block, error, threading.Event()

    def run(self, prompt, *, cwd, on_progress, cancel_event, profile=None, skills=None, on_event=None, **options):
        self.calls.append({"prompt": prompt, "cwd": cwd, "profile": profile, "skills": skills, **options})
        on_progress("正在调用 mcp__resolve__run_script。")
        on_event({"type": "message", "text": "选第 1 个意群的画面。"})
        Path(cwd, "notes.txt").write_text("ok", encoding="utf-8")
        self.started.set()
        if self.block:
            cancel_event.wait(5)
            raise BridgeCancelled("cancelled")
        if self.error:
            raise RuntimeError(self.error)
        return REPORT


class EditBench(Bench, EditingWorkbenchMixin):
    def __init__(self, root, editor):
        super().__init__(root, FakeEleven())
        self.editor, self.runner = editor, Runner()
        self.resolve = ResolveBridge(runner=self.runner)
        self.ledger_dir = Path(root) / "davinci-ledger"
        self.edit_skill_dir = Path(root) / "skill-src"
        (self.edit_skill_dir / "references").mkdir(parents=True)
        (self.edit_skill_dir / "agents").mkdir()
        (self.edit_skill_dir / "SKILL.md").write_text("---\nname: davinci-product-video-editing\n---\n# 剪辑", encoding="utf-8")
        (self.edit_skill_dir / "references" / "source-ledger.md").write_text("账本", encoding="utf-8")


class EditingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-edit-")
        self.addCleanup(temp.cleanup)
        mcp = Path(temp.name) / "ResolveMCP.exe"
        mcp.write_bytes(b"")
        environment = patch.dict(os.environ, {"RESOLVE_MCP": str(mcp)})
        environment.start()
        self.addCleanup(environment.stop)
        self.editor = Editor()
        self.bench = EditBench(temp.name, self.editor)

    def mutate(self, kind, **values):
        return self.bench.production.mutate(kind, {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values})

    def timeline_job(self):
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": CANDIDATE, "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "title": "稿", "text": "One kit, replacing this jig.\nOut goes the jig.\nFull 202H kit's below.",
                       "confirmed": True}}, source)["script"]
        job = self.bench.create_production_job({"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                "job": {"script_id": script["id"]}})["job"]
        take = self.bench.generate_take(job["id"], {"request_id": uuid4().hex, "voice_id": "VOICE123456789"})["take"]
        self.mutate("select_take", job_id=job["id"], take_id=take["id"])
        self.mutate("settings", settings={"creators": ["Lab"]})
        self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})
        self.bench.runner.job = job["id"]
        return self.bench.production.job(job["id"])

    def start(self, job, **values):
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"], "materials": ["蓝点折刀成品素材"],
                "mode": "explain", "instruction": "开头用切纸结果", **values}
        return self.bench.start_edit(job["id"], body)

    def wait(self, job_id):
        deadline = time.monotonic() + 5
        while self.bench.edit_running() and time.monotonic() < deadline:
            time.sleep(.02)
        return self.bench.production.job(job_id)["edits"][-1]

    def test_unattended_edit_runs_restricted_and_reads_the_timeline_back(self):
        job = self.timeline_job()
        started = self.start(job)["edit"]
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["report"]["questions"]), ("completed", ["开头要不要换切纸？"]))
        self.assertEqual((edit["summary"]["shots"], edit["summary"]["group_markers"], edit["summary"]["voice_changed"]), (3, 3, False))
        self.assertEqual(edit["summary"]["uncovered"], [{"start": 200, "end": 400, "seconds": 6.67}])
        call = self.editor.calls[0]
        profile = call["profile"]
        self.assertEqual(profile["tools"], ["Read", "Write", "Edit", "Glob", "Grep"])
        self.assertIn("mcp__resolve__run_script", profile["allowed"])
        self.assertFalse(any("unsafe" in tool for tool in profile["allowed"]))
        self.assertEqual(profile["dirs"], [str(self.bench.ledger_dir)])
        folder = Path(call["cwd"])
        self.assertEqual(folder.name, started["id"])
        self.assertTrue((folder / "skill" / "references" / "source-ledger.md").is_file())
        self.assertFalse((folder / "skill" / "agents").exists())
        self.assertEqual(call["skills"][0]["path"], str(folder / "skill" / "SKILL.md"))
        handoff = (folder / "handoff.md").read_text(encoding="utf-8")
        for text in ("Lab", "蓝点折刀成品素材", "开头用切纸结果", "| 3 | Full 202H kit's below. |", str(self.bench.ledger_dir), "workbench-group:" + job["id"]):
            self.assertIn(text, handoff)
        self.assertIn(handoff, call["prompt"])
        self.assertFalse(self.bench.resolve.busy())
        self.assertEqual(self.bench.production.public("202H")["product_settings"]["material_timelines"], ["蓝点折刀成品素材"])

    def test_stop_cancels_and_a_second_run_is_refused_while_one_runs(self):
        self.editor.block = True
        job = self.timeline_job()
        self.start(job)
        self.assertTrue(self.editor.started.wait(5))
        with self.assertRaises(VoiceError) as caught:
            self.start(job)
        self.assertEqual(caught.exception.status, 409)
        detail = self.bench.production_job(job["id"])
        running = detail["job"]["edits"][-1]
        self.assertEqual((running["status"], running["live"]["latest"]), ("running", "选第 1 个意群的画面。"))
        self.bench.cancel_edit(job["id"], running["id"])
        edit = self.wait(job["id"])
        self.assertEqual(edit["status"], "cancelled")
        self.assertIsNotNone(edit["summary"])
        with self.assertRaises(VoiceError):
            self.bench.cancel_edit(job["id"], running["id"])

    def test_failures_are_recorded_and_restart_marks_interrupted(self):
        self.editor.error = "Claude 响应超时"
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["error"]), ("failed", "Claude 响应超时"))
        data = self.bench.production.read()
        data["jobs"][0]["edits"][-1]["status"] = "running"
        self.bench.production.write(data)
        self.assertEqual(self.bench.production_job(job["id"])["job"]["edits"][-1]["status"], "interrupted")
        restarted = ProductionStore(self.bench.production.root.parent)
        restarted.interrupt_running()
        self.assertEqual(restarted.job(job["id"])["edits"][-1]["status"], "interrupted")

    def test_preflight_failure_spends_nothing_and_handoff_can_be_copied(self):
        job = self.timeline_job()
        self.bench.resolve = ResolveBridge(runner=lambda args, input, **o: subprocess.CompletedProcess(
            args, 0, json.dumps({"ok": False, "error": "达芬奇当前打开的是项目「X」"}, ensure_ascii=False).encode(), b""))
        with self.assertRaises(VoiceError):
            self.start(job)
        self.assertEqual(self.editor.calls, [])
        self.assertEqual(self.bench.production.job(job["id"])["edits"], [])
        with self.assertRaises(VoiceError):
            self.start(job, mode="nope")
        result = self.bench.edit_handoff(job["id"], {"timeline": job["timelines"][-1]["unique_id"], "materials": [], "mode": "sync"})
        self.assertTrue(Path(result["path"]).is_file())
        self.assertIn("$davinci-product-video-editing", result["prompt"])
        self.assertIn("音画同步", result["prompt"])


class SummaryTests(unittest.TestCase):
    def test_gaps_long_shots_and_changed_voiceover(self):
        job = {"id": "PRD-0123456789ab", "lines": [{}, {}, {}]}
        reply = json.loads(json.dumps(inspect_reply(gaps=[(200, 300)], shots=((0, 40), (40, 200), (300, 380)))).replace("{job}", job["id"]))
        summary = edit_summary(reply, job, {"frames": 390}, "29.97")
        self.assertEqual((summary["shots"], summary["long_shots"], summary["max_shot"], summary["frame_limit"]), (3, 3, 160, 29))
        self.assertEqual([gap["seconds"] for gap in summary["gaps"]], [6.67])
        self.assertEqual(summary["uncovered"], [{"start": 380, "end": 400, "seconds": 12.68}])
        self.assertTrue(summary["voice_changed"])
        self.assertIsNone(final_report("no block"))
        self.assertEqual(final_report(REPORT)["shots"], 3)

    def test_handoff_keeps_table_cells_on_one_line(self):
        job = {"id": "PRD-0123456789ab", "title": "稿", "script_id": "SCR-0123456789ab", "script_version": 1, "direction_id": "",
               "variant_label": "", "lines": [{"number": 1, "spoken": "A | B", "zh": "甲\n乙", "proof": "", "tags": "", "evidence": ""}]}
        take = {"model_id": "eleven_v4", "duration": 2.0, "groups": [{"start": 0.1, "end": 1.8}]}
        config = {"project": "202H", "fps": "29.97", "width": 1080, "height": 1920}
        text = handoff_markdown(job, take, {"name": "Lab261010-202-1", "unique_id": "u", "frames": 59}, config, mode="explain",
                                instruction="", materials=[], ledger="L", notes={}, voice_label="Zack")
        self.assertIn("| 1 | A ／ B | 甲 乙 | 待补 |", text)
        self.assertIn("未指定", text)


if __name__ == "__main__":
    unittest.main()
