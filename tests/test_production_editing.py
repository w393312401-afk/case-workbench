"""Production phase three: the unattended AI editing task, with a fake Claude bridge and a fake Resolve worker."""
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

from codex_bridge import BridgeCancelled
from elevenlabs_client import VoiceError
from production_store import ProductionStore, job_status
from production_validation import snapshot
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven, CANDIDATE
from workbench_editing import EditingWorkbenchMixin, edit_summary, final_report, handoff_markdown

REPORT = 'Done.\n\n```json\n{"timeline": "Lab261010-202-1", "shots": 3, "gaps": [], "questions": ["开头要不要换切纸？"], "saved": true}\n```'


def inspect_reply(gaps=(), voice_end=400, shots=((0, 25), (25, 50), (50, 200))):
    markers = [{"frame": f, "color": "Blue", "name": f"#{n}", "note": "", "duration": 10, "custom": f"workbench-group:{{job}}:{n}"}
               for n, f in ((1, 0), (2, 120), (3, 260))]
    markers += [{"frame": a, "color": "Red", "name": "缺素材｜切纸结果", "note": "需补实拍", "duration": b - a, "custom": "missing"} for a, b in gaps]
    videos = [{"start": a, "end": b, "name": "素材.mp4", "uid": f"v-{i}", "media_id": "source-1", "media_path": "C:/media/source.mp4",
               "source_start": a, "source_end": b, "enabled": True, "properties": {}, "has_audio": True, "linked_ids": [f"a-{i}"]}
              for i, (a, b) in enumerate(shots)]
    originals = [{**item, "uid": f"a-{i}", "linked_ids": [f"v-{i}"]} for i, item in enumerate(videos)]
    return {"ok": True, "name": "Lab261010-202-1", "unique_id": "uid-1", "start_frame": 0, "end_frame": voice_end,
            "fps": "29.97", "width": "1080", "height": "1920", "saved": True, "other_shots": [], "dedup_complete": True,
            "tracks": {"video": [{"index": 1, "name": "视频 1", "locked": False, "enabled": True, "items": videos}],
                       "audio": [{"index": 1, "name": "音频 1", "locked": False, "enabled": True, "items": originals},
                                 {"index": 2, "name": "配音", "locked": False, "enabled": True,
                                  "items": [{"start": 0, "end": voice_end, "name": "x_配音", "uid": "voice-1", "media_id": "voice-media",
                                             "media_path": "C:/media/voice.wav", "source_start": 0, "source_end": voice_end,
                                             "enabled": True, "properties": {"AudioVolume": -3}, "linked_ids": []}]}]},
            "markers": markers}


class Runner:
    def __init__(self):
        self.calls, self.inspect, self.final_inspect = [], inspect_reply(), None

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.calls.append(request)
        if request["command"] == "inspect":
            reply = self.final_inspect if request.get("save") and self.final_inspect is not None else self.inspect
            if isinstance(reply, Exception):
                raise reply
            body = json.loads(json.dumps(reply).replace("{job}", self.job))
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
        self.output = REPORT

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
        return self.output


class EditBench(Bench, EditingWorkbenchMixin):
    def __init__(self, root, editor):
        super().__init__(root, FakeEleven())
        self.ai = SimpleNamespace(resolve_default_settings=lambda body, effort_key: {
            "model": body.get("model", "claude-default"), effort_key: body.get(effort_key)})
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

    def test_unattended_edit_follows_current_skill_without_automatic_readback(self):
        job = self.timeline_job()
        started = self.start(job)["edit"]
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"], edit["report"]["questions"]), ("completed", "completed", ["开头要不要换切纸？"]))
        self.assertIsNone(edit["summary"])
        self.assertIsNone(edit["validation"])
        self.assertEqual(edit["completion_policy"], "skill_delivery")
        call = self.editor.calls[0]
        profile = call["profile"]
        self.assertEqual(profile["tools"], ["Read", "Write", "Edit", "Glob", "Grep"])
        self.assertIn("mcp__resolve__run_script", profile["allowed"])
        self.assertFalse(any("unsafe" in tool for tool in profile["allowed"]))
        self.assertIn("mcp__resolve__run_script_unsafe", profile["denied"])
        self.assertEqual(profile["dirs"], [])
        folder = Path(call["cwd"])
        self.assertEqual(folder.name, started["id"])
        self.assertTrue((folder / "skill" / "references" / "source-ledger.md").is_file())
        self.assertFalse((folder / "skill" / "agents").exists())
        self.assertEqual(call["skills"][0]["path"], str(folder / "skill" / "SKILL.md"))
        handoff = (folder / "handoff.md").read_text(encoding="utf-8")
        for text in ("Lab", "蓝点折刀成品素材", "开头用切纸结果", "| 3 | Full 202H kit's below. |", "workbench-group:" + job["id"]):
            self.assertIn(text, handoff)
        self.assertNotIn(str(self.bench.ledger_dir), call["prompt"])
        self.assertNotIn("账本", call["prompt"])
        self.assertNotIn("核验", call["prompt"])
        self.assertIn(handoff, call["prompt"])
        self.assertFalse(self.bench.resolve.busy())
        self.assertIsNone(edit["baseline"])
        self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))
        self.assertEqual(self.bench.production.public("202H")["product_settings"]["material_timelines"], ["蓝点折刀成品素材"])

    def test_each_edit_copies_the_current_personal_skill(self):
        job = self.timeline_job()
        source = self.bench.edit_skill_dir / "SKILL.md"
        source.write_text("当次规则：剪好保存后结束。", encoding="utf-8")
        self.start(job)
        self.wait(job["id"])
        first_copy = Path(self.editor.calls[-1]["cwd"]) / "skill" / "SKILL.md"
        self.assertEqual(first_copy.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        source.write_text("更新规则：完全按当前技能剪辑。", encoding="utf-8")
        self.start(job)
        self.wait(job["id"])
        second_copy = Path(self.editor.calls[-1]["cwd"]) / "skill" / "SKILL.md"
        self.assertEqual(second_copy.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
        self.assertNotEqual(first_copy.read_text(encoding="utf-8"), second_copy.read_text(encoding="utf-8"))

    def test_explicit_unsaved_delivery_is_retained_without_engineering_readback(self):
        job = self.timeline_job()
        self.editor.output = REPORT.replace('"saved": true', '"saved": false')
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"]), ("needs_repair", "completed"))
        self.assertIn("尚未保存", edit["error"])
        self.assertIsNone(edit["validation"])
        self.assertIs(edit["report"]["saved"], False)
        self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))

    def test_delivery_requires_explicit_saved_true_without_automatic_readback(self):
        for output in ("剪辑执行已结束。", "```json\n{broken}\n```", "```json\n{\"shots\": 3}\n```",
                       "```json\n{\"saved\": 1}\n```", "```json\n{\"saved\": \"true\"}\n```"):
            with self.subTest(output=output):
                job = self.timeline_job()
                self.editor.output = output
                self.start(job)
                edit = self.wait(job["id"])
                self.assertEqual((edit["status"], edit["execution_status"]), ("needs_repair", "completed"))
                self.assertIn("未说明工程保存状态", edit["error"])
                self.assertEqual(edit["output"], output)
                self.assertIsNone(edit["validation"])
                self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))

    def test_reported_material_gap_is_delivered_without_automatic_gap_validation(self):
        job = self.timeline_job()
        report = {"timeline": "Lab261010-202-1", "shots": 3, "gaps": [{"start": 0, "end": 40, "need": "缺少动作实拍"}], "saved": True}
        self.editor.output = "已保存，缺素材。\n```json\n" + json.dumps(report, ensure_ascii=False) + "\n```"
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual((edit["status"], edit["execution_status"]), ("needs_material", "completed"))
        self.assertEqual(edit["report"], report)
        self.assertIsNone(edit["validation"])
        self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))

    def test_delete_edit_removes_record_files_and_recomputes_status(self):
        job = self.timeline_job()
        edit_id = self.start(job)["edit"]["id"]
        self.wait(job["id"])
        folder = self.bench.production.root / "edits" / edit_id
        self.assertTrue(folder.is_dir())
        (folder / "stills").mkdir(exist_ok=True)
        (folder / "stills" / "grid.jpg").write_bytes(b"x" * 2048)
        preview = self.bench.delete_edit(job["id"], edit_id, {"preview": True})
        self.assertGreaterEqual(preview["bytes"], 2048)
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["id"], edit_id)
        result = self.bench.delete_edit(job["id"], edit_id, {"request_id": uuid4().hex})
        self.assertEqual((result["deleted_edit"], result["warnings"]), (edit_id, []))
        after = self.bench.production.job(job["id"])
        self.assertEqual(after["edits"], [])
        self.assertEqual(after["status"], job_status(after))
        self.assertNotEqual(after["status"], "needs_repair")
        self.assertIn("删除 AI 剪辑", after["history"][-1]["note"])
        self.assertFalse(folder.exists())
        with self.assertRaises(VoiceError) as caught:
            self.bench.delete_edit(job["id"], edit_id, {"request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 404)

    def test_delete_edit_refuses_a_running_edit_and_foreign_ids(self):
        job = self.timeline_job()
        self.editor.block = True
        edit_id = self.start(job)["edit"]["id"]
        self.assertTrue(self.editor.started.wait(5))
        with self.assertRaises(VoiceError) as caught:
            self.bench.delete_edit(job["id"], edit_id, {"request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 409)
        self.assertTrue((self.bench.production.root / "edits" / edit_id).is_dir())
        with self.assertRaises(VoiceError):
            self.bench.delete_edit(job["id"], "../../etc", {"request_id": uuid4().hex})
        self.bench.cancel_edit(job["id"], edit_id)
        self.wait(job["id"])

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
        self.assertIsNone(edit["summary"])
        self.assertIsNone(edit["validation"])
        self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))
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

    def test_task_does_not_add_engineering_preflight_and_handoff_can_be_copied(self):
        job = self.timeline_job()
        self.bench.resolve = ResolveBridge(runner=lambda args, input, **o: subprocess.CompletedProcess(
            args, 0, json.dumps({"ok": False, "error": "达芬奇当前打开的是项目「X」"}, ensure_ascii=False).encode(), b""))
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual(edit["status"], "completed")
        self.assertEqual(len(self.editor.calls), 1)
        with self.assertRaises(VoiceError):
            self.start(job, mode="nope")
        result = self.bench.edit_handoff(job["id"], {"timeline": job["timelines"][-1]["unique_id"], "materials": [], "mode": "sync"})
        self.assertTrue(Path(result["path"]).is_file())
        self.assertIn("$davinci-product-video-editing", result["prompt"])
        self.assertIn("音画同步", result["prompt"])
        self.assertNotIn("核验", result["prompt"])
        self.assertNotIn("账本", Path(result["path"]).read_text(encoding="utf-8"))

    def test_delivery_status_comes_from_execution_not_automatic_timeline_inspection(self):
        for reply in (inspect_reply(shots=((0, 200), (200, 400))), inspect_reply(gaps=((200, 400),))):
            with self.subTest(reply=reply):
                job = self.timeline_job()
                self.bench.runner.inspect = reply
                self.bench.runner.final_inspect = None
                self.start(job)
                edit = self.wait(job["id"])
                self.assertEqual(edit["status"], "completed")
                self.assertIsNone(edit["validation"])
                self.assertEqual(edit["execution_status"], "completed")
                self.assertFalse(any(call["command"] == "inspect" for call in self.bench.runner.calls))

    def test_unused_readback_failure_does_not_block_skill_delivery_or_leave_locks(self):
        for reply in ({"ok": True, "tracks": []}, RuntimeError("worker 返回损坏")):
            with self.subTest(reply=reply):
                job = self.timeline_job()
                self.bench.runner.final_inspect = reply
                self.start(job)
                edit = self.wait(job["id"])
                self.assertEqual((edit["status"], edit["execution_status"]), ("completed", "completed"))
                self.assertIsNone(edit["validation"])
                self.assertFalse(self.bench.resolve.busy())
                self.assertFalse(self.bench.edit_running())

    def test_catalog_write_failure_is_recovered_without_reediting(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        original = self.bench.production.mutate
        def mutate(kind, body, extra=None):
            if kind == "edit_finish":
                raise OSError("磁盘暂不可写")
            return original(kind, body, extra)
        with patch.object(self.bench.production, "mutate", side_effect=mutate):
            started = self.start(job)["edit"]
            self.wait(job["id"])
        journal = self.bench.production.root / "edits" / started["id"] / "result.json"
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["result"]["status"], "completed")
        self.assertFalse(self.bench.resolve.busy())
        recovered = self.bench.recover_edit_results()
        self.assertEqual(recovered["recovered"], [started["id"]])
        edit = self.bench.production.job(job["id"])["edits"][-1]
        self.assertEqual(edit["status"], "completed")
        self.assertEqual(len(self.editor.calls), 1)
        self.assertEqual(self.bench.recover_edit_results()["recovered"], [])

    def test_explicit_result_recovery_does_not_read_back_or_reedit_the_timeline(self):
        job = self.timeline_job()
        original = self.bench.production.mutate
        def mutate(kind, body, extra=None):
            if kind == "edit_finish":
                raise OSError("磁盘暂不可写")
            return original(kind, body, extra)
        with patch.object(self.bench.production, "mutate", side_effect=mutate):
            started = self.start(job)["edit"]
            self.wait(job["id"])
        before = len(self.bench.runner.calls)
        result = self.bench.recheck_edit(job["id"], started["id"], {"request_id": uuid4().hex, "recover_result": True})
        self.assertIs(result["recovered_result"], True)
        self.assertEqual(result["edit"]["status"], "completed")
        self.assertIsNone(result["edit"]["validation"])
        self.assertEqual(result["recovered"], [started["id"]])
        self.assertEqual(len(self.bench.runner.calls), before)
        self.assertEqual(len(self.editor.calls), 1)

    def test_recheck_allows_manual_sound_adjustment_but_preserves_failed_execution(self):
        self.editor.error = "Claude 超时"
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        self.start(job)
        edit = self.wait(job["id"])
        data = self.bench.production.read()
        record = next(item for item in data["jobs"] if item["id"] == job["id"])["edits"][-1]
        record["baseline"] = snapshot(json.loads(json.dumps(self.bench.runner.inspect).replace("{job}", job["id"])))
        self.bench.production.write(data)
        self.bench.runner.inspect["tracks"]["audio"][1]["items"][0]["properties"]["AudioVolume"] = -10
        self.bench.runner.inspect["tracks"]["audio"][1]["enabled"] = False
        result = self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})["edit"]
        self.assertEqual((result["status"], result["execution_status"]), ("awaiting_review", "failed"))
        self.assertEqual(len(self.editor.calls), 1)
        self.assertTrue(self.bench.runner.calls[-1]["save"])

    def test_old_completed_without_baseline_cannot_be_declared_verified(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        self.start(job)
        edit = self.wait(job["id"])
        data = self.bench.production.read()
        old = next(item for item in data["jobs"] if item["id"] == job["id"])["edits"][-1]
        old.pop("baseline")
        old["status"] = "completed"
        self.bench.production.write(data)
        result = self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})["edit"]
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("baseline_missing", [item["code"] for item in result["validation"]["issues"]])

    def test_invalid_recheck_request_has_no_resolve_effect(self):
        job = self.timeline_job()
        self.start(job)
        edit = self.wait(job["id"])
        calls = len(self.bench.runner.calls)
        with self.assertRaises(VoiceError) as caught:
            self.bench.recheck_edit(job["id"], edit["id"], {"request_id": "bad"})
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(len(self.bench.runner.calls), calls)

    def test_malformed_unused_preflight_does_not_block_editing(self):
        job = self.timeline_job()
        self.bench.runner.inspect = {"ok": True, "tracks": {"video": [], "audio": []}}
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual(edit["status"], "completed")
        self.assertFalse(self.bench.resolve.busy())
        self.assertEqual(len(self.editor.calls), 1)

    def test_first_result_file_failure_retains_memory_and_does_not_repeat_ai(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        with patch.object(self.bench, "_write_edit_journal", side_effect=OSError("result file unavailable")):
            started = self.start(job)["edit"]
            edit = self.wait(job["id"])
            self.assertEqual((edit["status"], edit["execution_status"]), ("needs_repair", "completed"))
            self.assertIsNone(edit["validation"])
            self.assertIn("结果登记失败", edit["error"])
            self.assertTrue(self.bench.edit_pending(started["id"])["result_pending"])
            for action in (lambda: self.start(job), lambda: self.bench.recheck_edit(job["id"], started["id"], {"request_id": uuid4().hex})):
                with self.assertRaises(VoiceError) as caught:
                    action()
                self.assertEqual(caught.exception.status, 503)
            self.assertEqual(len(self.editor.calls), 1)
        recovered = self.bench.recover_edit_results()
        self.assertEqual(recovered["recovered"], [started["id"]])
        self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["status"], "completed")
        self.assertIsNone(self.bench.edit_pending(started["id"]))
        self.assertFalse(self.bench.resolve.busy())

    def test_both_persistence_destinations_failed_keep_the_complete_memory_result(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        original = self.bench.production.mutate
        def cannot_finish(kind, body, extra=None):
            if kind == "edit_finish":
                raise OSError("catalog unavailable")
            return original(kind, body, extra)
        with patch.object(self.bench, "_write_edit_journal", side_effect=OSError("journal unavailable")), \
             patch.object(self.bench.production, "mutate", side_effect=cannot_finish):
            started = self.start(job)["edit"]
            self.wait(job["id"])
            self.assertFalse(self.bench.edit_running())
            self.assertFalse(self.bench.resolve.busy())
            pending = self.bench.edit_pending(started["id"])
            self.assertEqual(pending["execution_status"], "completed")
            self.assertIsNone(self.bench._edit_pending[started["id"]]["result"]["validation"])
            self.assertEqual(self.bench._edit_pending[started["id"]]["result"]["status"], "completed")
            with self.assertRaises(VoiceError) as caught:
                self.start(job)
            self.assertEqual(caught.exception.status, 503)
            self.assertEqual(len(self.editor.calls), 1)
        self.assertEqual(self.bench.recover_edit_results()["recovered"], [started["id"]])
        self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["status"], "completed")

    def test_restart_without_a_result_file_can_request_manual_engineering_readback(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        original = self.bench.production.mutate
        def cannot_finish(kind, body, extra=None):
            if kind == "edit_finish":
                raise OSError("catalog unavailable")
            return original(kind, body, extra)
        with patch.object(self.bench, "_write_edit_journal", side_effect=OSError("journal unavailable")), \
             patch.object(self.bench.production, "mutate", side_effect=cannot_finish):
            started = self.start(job)["edit"]
            self.wait(job["id"])
        self.bench._edit_pending.clear()  # Memory is gone after a process restart.
        self.bench.production.interrupt_running()
        self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["status"], "interrupted")
        with self.assertRaises(VoiceError) as caught:
            self.start(job)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.editor.calls), 1)
        result = self.bench.recheck_edit(job["id"], started["id"], {"request_id": uuid4().hex})["edit"]
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("baseline_missing", [issue["code"] for issue in result["validation"]["issues"]])
        self.assertEqual(len(self.editor.calls), 1)

    def test_busy_recheck_refuses_without_changing_existing_validation(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        self.start(job)
        edit = self.wait(job["id"])
        before = self.bench.production.read()
        calls = len(self.bench.runner.calls)
        self.bench.resolve.lock.acquire()
        try:
            with self.assertRaises(VoiceError) as caught:
                self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(self.bench.production.read(), before)
            self.assertEqual(len(self.bench.runner.calls), calls)
        finally:
            self.bench.resolve.lock.release()

    def test_recheck_holds_resolve_until_catalog_commit_to_prevent_late_old_pass(self):
        job = self.timeline_job()
        self.bench.runner.inspect = inspect_reply(shots=((0, 200), (200, 400)))
        self.start(job)
        edit = self.wait(job["id"])
        data = self.bench.production.read()
        record = next(item for item in data["jobs"] if item["id"] == job["id"])["edits"][-1]
        record["baseline"] = snapshot(json.loads(json.dumps(self.bench.runner.inspect).replace("{job}", job["id"])))
        self.bench.production.write(data)
        entered, release = threading.Event(), threading.Event()
        results, errors = [], []
        original = self.bench.production.mutate
        def mutate(kind, body, extra=None):
            if kind == "edit_recheck" and threading.current_thread().name == "old-recheck":
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
            return original(kind, body, extra)
        def old_check():
            try:
                results.append(self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex}))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.bench.production, "mutate", side_effect=mutate):
            old = threading.Thread(target=old_check, name="old-recheck")
            old.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertTrue(self.bench.resolve.busy())
                self.bench.runner.final_inspect = {**self.bench.runner.inspect, "saved": False}
                calls = len(self.bench.runner.calls)
                with self.assertRaises(VoiceError) as caught:
                    self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})
                self.assertEqual(caught.exception.status, 409)
                self.assertEqual(len(self.bench.runner.calls), calls)
            finally:
                release.set()
                old.join(5)
        self.assertFalse(old.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["edit"]["status"], "awaiting_review")
        latest = self.bench.recheck_edit(job["id"], edit["id"], {"request_id": uuid4().hex})["edit"]
        self.assertEqual(latest["status"], "verification_failed")
        self.assertEqual(self.bench.production.job(job["id"])["edits"][-1]["validation"], latest["validation"])
        self.assertFalse(self.bench.resolve.busy())

    def test_delayed_same_request_checks_replay_again_under_resolve_lock(self):
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"], "materials": [], "mode": "explain"}
        entered, release = threading.Event(), threading.Event()
        original = self.bench.production.replay
        delayed_calls, results, errors = [], [], []
        def replay(kind, request):
            result = original(kind, request)
            if kind == "edit_start" and threading.current_thread().name == "delayed-start" and not delayed_calls:
                delayed_calls.append(True)
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test timed out")
            return result
        def delayed_start():
            try:
                results.append(self.bench.start_edit(job["id"], body))
            except Exception as exc:
                errors.append(exc)
        with patch.object(self.bench.production, "replay", side_effect=replay):
            delayed = threading.Thread(target=delayed_start, name="delayed-start")
            delayed.start()
            try:
                self.assertTrue(entered.wait(5))
                first = self.bench.start_edit(job["id"], body)
                self.wait(job["id"])
            finally:
                release.set()
                delayed.join(5)
        self.assertFalse(delayed.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["edit"]["id"], first["edit"]["id"])
        self.assertEqual(len(self.editor.calls), 1)
        self.assertEqual(len(self.bench.production.job(job["id"])["edits"]), 1)
        self.assertFalse(self.bench.resolve.busy())

    def test_cached_mutate_result_does_not_launch_new_edit_identity(self):
        job = self.timeline_job()
        body = {"request_id": uuid4().hex, "timeline": job["timelines"][-1]["unique_id"], "materials": [], "mode": "explain"}
        first = self.bench.start_edit(job["id"], body)
        self.wait(job["id"])
        original = self.bench.production.replay
        with patch.object(self.bench.production, "replay", side_effect=lambda kind, value: None if kind == "edit_start" else original(kind, value)):
            repeated = self.bench.start_edit(job["id"], body)
        self.assertEqual(repeated["edit"]["id"], first["edit"]["id"])
        self.assertEqual(len(self.editor.calls), 1)
        self.assertFalse(self.bench.edit_running())
        self.assertFalse(self.bench.resolve.busy())


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
        take = {"model_id": "eleven_v4", "duration": 2.0, "groups": [{"number": 1, "start": 0.1, "end": 1.8, "coverage": 1.0}]}
        config = {"project": "202H", "fps": "29.97", "width": 1080, "height": 1920}
        text = handoff_markdown(job, take, {"name": "Lab261010-202-1", "unique_id": "u", "frames": 59}, config, mode="explain",
                                instruction="", materials=[], ledger="L", notes={}, voice_label="Zack")
        self.assertIn("| 1 | A ／ B | 甲 乙 | 待补 |", text)
        self.assertIn("未指定", text)


if __name__ == "__main__":
    unittest.main()
