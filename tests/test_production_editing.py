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
from workbench_editing import EditingWorkbenchMixin, edit_summary, final_report, handoff_markdown, project_key, with_location
from workbench_production import meaning_markers

REPORT = 'Done.\n\n```json\n{"timeline": "Lab261010-202-1", "shots": 3, "gaps": [], "questions": ["开头要不要换切纸？"], "saved": true}\n```'


def judging_inspects(calls):
    """Readbacks that could judge or save the timeline; the record-only delivery snapshot is not one of them."""
    return [call for call in calls if call["command"] == "inspect" and not call.get("current_only")]


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

    def current_readback(self, job, shots=((0, 200), (200, 400))):
        job = self.bench.production.job(job["id"])
        timeline, take = job["timelines"][-1], job["takes"][-1]
        reply = inspect_reply(shots=shots)
        reply["tracks"]["audio"][1]["items"][0]["media_path"] = str(self.bench.production.audio / (take.get("wav") or take["audio"]))
        reply["markers"] = [{**marker, "color": "Blue"} for marker in meaning_markers(job, take, timeline["fps"],
                            lead_frames=timeline.get("lead_frames", 0))]
        self.bench.runner.inspect = reply
        return reply

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
        self.assertEqual(judging_inspects(self.bench.runner.calls), [])
        self.assertEqual(self.bench.production.public("202H")["product_settings"]["material_timelines"], ["蓝点折刀成品素材"])

    def test_archived_timeline_cannot_start_ai_until_restored(self):
        job = self.timeline_job()
        timeline_uid = job["timelines"][-1]["unique_id"]
        before_calls = len(self.bench.runner.calls)
        self.mutate("timeline_archive", job_id=job["id"], timeline_uid=timeline_uid, archived=True)
        with self.assertRaises(VoiceError) as caught:
            self.start(job)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.bench.runner.calls), before_calls)
        self.assertEqual(self.editor.calls, [])
        self.mutate("timeline_archive", job_id=job["id"], timeline_uid=timeline_uid, archived=False)
        self.start(job)
        self.assertEqual(self.wait(job["id"])["status"], "completed")

    def test_saved_delivery_keeps_a_record_only_timeline_snapshot(self):
        job = self.timeline_job()
        started = self.start(job)["edit"]
        edit = self.wait(job["id"])
        # The snapshot changes nothing: status and validation come from the delivery alone.
        self.assertEqual((edit["status"], edit["summary"], edit["validation"]), ("completed", None, None))
        commands = [call["command"] for call in self.bench.runner.calls]
        snapshots = [call for call in self.bench.runner.calls if call["command"] == "inspect"]
        self.assertEqual(len(snapshots), 1)
        request = snapshots[0]
        self.assertIs(request["current_only"], True)
        self.assertNotIn("save", request)
        self.assertEqual((request["unique_id"], request["name"]), ("uid-1", job["timelines"][-1]["name"]))
        # Taken right after the client's delivery, before usage marking rewrites the material timelines.
        self.assertLess(commands.index("inspect"), commands.index("mark_usage"))
        record = json.loads((self.bench.production.root / "edits" / started["id"] / "delivered-timeline.json").read_text(encoding="utf-8"))
        self.assertEqual((record["schema_version"], record["edit_id"], record["purpose"]), (1, started["id"], "record_only"))
        self.assertNotIn("ok", record["timeline"])
        self.assertEqual([item["uid"] for item in record["timeline"]["tracks"]["video"][0]["items"]], ["v-0", "v-1", "v-2"])
        self.assertEqual(edit["delivery_snapshot"], {"file": "delivered-timeline.json", "captured_at": record["captured_at"],
                                                     "timeline": "Lab261010-202-1", "shots": 3})

    def listing(self, used=()):
        clip = {"uid": "mat-1", "start": 0, "end": 50, "name": "a.mov", "media_id": "m1", "media_path": "D:/a.mov",
                "source_start": 0, "source_end": 49, "has_audio": True, "enabled": True, "online": "Online", "track": 1,
                "labels": ["推磨 DG001"], "verified_labels": ["推磨 DG001"]}
        return {"ok": True, "materials": [{"name": "蓝点折刀成品素材", "unique_id": "mat-tl", "start_frame": 0, "fps": "29.97",
                                           "drop": False, "clips": [clip]}],
                "missing": [], "used": list(used), "used_complete": True, "dedup_enabled": False, "usage_counted": True}

    def run_with_listings(self, *listings, job=None, before_recount=None, plan=None):
        """Run one edit whose snapshot and pre-edit recount return these listings in turn (an Exception raises).

        before_recount runs when the second listing is asked for; plan stands in for the planners."""
        calls, original, self.commands = [], Runner.__call__, []
        replies = list(listings)

        def runner(runner_self, args, input, **options):
            request = json.loads(input.decode("utf-8"))
            self.commands.append(request["command"])
            if request["command"] == "candidates":
                calls.append({**request, "editor_calls": len(self.editor.calls), "resolve_held": self.bench.resolve.busy()})
                if len(calls) == 2 and before_recount:
                    before_recount()
                reply = replies.pop(0) if len(replies) > 1 else replies[0]
                if isinstance(reply, Exception):
                    raise reply
                return subprocess.CompletedProcess(args, 0, json.dumps(reply, ensure_ascii=False).encode("utf-8"), b"")
            return original(runner_self, args, input, **options)

        job = job or self.timeline_job()
        planner = patch.object(self.bench, "_plan_edit", side_effect=plan) if plan else \
            patch.object(self.bench, "_plan_edit", return_value={"workers": 0, "completed": 0, "failed": 0})
        with patch.object(Runner, "__call__", runner), planner:
            started = self.start(job)["edit"]
            edit = self.wait(job["id"])
        return edit, self.bench.production.root / "edits" / started["id"], calls

    def test_use_counts_are_read_again_right_before_the_ai_places_shots(self):
        placed = {"timeline_name": "Lab261010-202-9", "timeline_uid": "other", "uid": "shot-1", "media_id": "m1",
                  "media_path": "D:/a.mov", "source_start": 10, "source_end": 29, "start": 100, "end": 120}
        edit, folder, calls = self.run_with_listings(self.listing(), self.listing([placed]))
        # The snapshot and then the recount, which never opens another project.
        self.assertEqual(len(calls), 2)
        self.assertIs(calls[1]["current_only"], True)
        self.assertEqual(calls[1]["target_uid"], calls[0]["target_uid"])
        self.assertEqual(edit["status"], "completed")
        self.assertEqual({key: edit["usage_refresh"][key] for key in ("changed", "unused")}, {"changed": 1, "unused": 0})
        row = json.loads((folder / "candidates.json").read_text(encoding="utf-8"))["candidates"][0]
        self.assertEqual((row["use_count"], row["used_by"]), (1, ["Lab261010-202-9"]))
        self.assertIn("已用 1 次（Lab261010-202-9）", (folder / "candidates.md").read_text(encoding="utf-8"))
        # The editor is told, in the prompt and in handoff.md, that the counts changed since the snapshot.
        handoff = (folder / "handoff.md").read_text(encoding="utf-8")
        self.assertIn("## 开剪前刷新的使用次数", handoff)
        self.assertIn("1 个片段次数有变化，现在 0 个片段还没用过", handoff)
        self.assertIn("## 开剪前刷新的使用次数", self.editor.calls[-1]["prompt"])
        # The recount runs in the editing lease (after its project check), before the client starts.
        self.assertEqual(self.commands[:3], ["candidates", "status", "candidates"])
        self.assertEqual((calls[1]["editor_calls"], calls[1]["resolve_held"]), (0, True))

    def test_unchanged_counts_leave_the_handoff_alone(self):
        edit, folder, calls = self.run_with_listings(self.listing())
        self.assertEqual(len(calls), 2)
        self.assertEqual({key: edit["usage_refresh"][key] for key in ("changed", "unused")}, {"changed": 0, "unused": 1})
        self.assertNotIn("开剪前刷新的使用次数", (folder / "handoff.md").read_text(encoding="utf-8"))
        self.assertNotIn("开剪前刷新的使用次数", self.editor.calls[-1]["prompt"])

    def test_a_failed_recount_keeps_the_snapshot_counts_and_the_edit(self):
        edit, folder, calls = self.run_with_listings(self.listing(), RuntimeError("达芬奇长时间没有响应"))
        self.assertEqual(len(calls), 2)
        self.assertEqual((edit["status"], edit["execution_status"]), ("completed", "completed"))
        self.assertIn("error", edit["usage_refresh"])
        row = json.loads((folder / "candidates.json").read_text(encoding="utf-8"))["candidates"][0]
        self.assertEqual((row["use_count"], row["used_by"]), (0, []))
        self.assertNotIn("开剪前刷新的使用次数", (folder / "handoff.md").read_text(encoding="utf-8"))

    def test_no_snapshot_counts_means_no_recount(self):
        # No candidate list at all, a list made without counts, and a list with no clips.
        uncounted, empty = self.listing(), self.listing()
        uncounted["usage_counted"] = False
        empty["materials"][0]["clips"] = []
        for listing in (None, uncounted, empty):
            with self.subTest(listing=listing and ("uncounted" if listing is uncounted else "empty")):
                if listing is None:
                    job = self.timeline_job()
                    self.start(job)
                    edit = self.wait(job["id"])
                    self.assertEqual(sum(call["command"] == "candidates" for call in self.bench.runner.calls), 1)
                else:
                    edit, folder, calls = self.run_with_listings(listing)
                    self.assertTrue((folder / "candidates.json").is_file())
                    self.assertEqual(len(calls), 1)
                self.assertIsNone(edit["usage_refresh"])

    def test_recount_swaps_a_finished_edits_plan_for_its_real_shots_and_never_counts_this_edits_own(self):
        job = self.timeline_job()
        config = with_location(self.bench.production.product_settings(self.bench.production.records(), job["product"]),
                               job["timelines"][-1])
        key = project_key(config)
        listing = self.listing()
        second = {**listing["materials"][0]["clips"][0], "uid": "mat-2", "start": 50, "end": 100, "source_start": 50,
                  "source_end": 99, "labels": ["检查 DG002"], "verified_labels": ["检查 DG002"]}
        listing["materials"][0]["clips"].append(second)
        # Edit A (another timeline of this project) planned DG001 first and is still running when this edit snapshots.
        with self.bench.lock:
            self.bench.__dict__.setdefault("_edit_occupied", {}).setdefault(key, {})["EDT-a"] = {
                "timeline": "Lab261010-202-8", "spans": [{"media_id": "m1", "media_path": "D:/a.mov", "source": [0, 50]}]}
        snapshots = []

        def plan(edit_id, folder, *args):
            snapshots.append(json.loads(Path(folder, "candidates.json").read_text(encoding="utf-8"))["candidates"])
            # This edit's own planners choose DG002.
            Path(folder, "planning-results.json").write_text(json.dumps([{"worker": 1, "status": "completed", "output": "",
                "picks": [row["key"] for row in snapshots[-1] if row["key"].startswith("DG002")], "unknown_picks": 0}]), encoding="utf-8")
            return {"workers": 1, "completed": 1, "failed": 0}

        def finished():
            # A has placed its real shot on DG002 and stopped holding its plan.
            self.bench._release_occupied("EDT-a")

        placed = {"timeline_name": "Lab261010-202-8", "timeline_uid": "uid-a", "uid": "shot-a", "media_id": "m1",
                  "media_path": "D:/a.mov", "source_start": 60, "source_end": 79, "start": 0, "end": 20}
        edit, folder, calls = self.run_with_listings(listing, {**listing, "used": [placed]}, job=job,
                                                     before_recount=finished, plan=plan)
        before = {row["key"][:5]: row["used_by"] for row in snapshots[0]}
        after = {row["key"][:5]: row["used_by"] for row in json.loads((folder / "candidates.json").read_text(encoding="utf-8"))["candidates"]}
        self.assertEqual(before, {"DG001": ["Lab261010-202-8（剪辑中）"], "DG002": []})
        # A's plan is gone, its real shot counts, and this edit's own choice of DG002 adds nothing.
        self.assertEqual(after, {"DG001": [], "DG002": ["Lab261010-202-8"]})
        self.assertEqual({key: edit["usage_refresh"][key] for key in ("changed", "unused")}, {"changed": 2, "unused": 1})

    def test_snapshot_failure_never_changes_the_delivery(self):
        for reply in (RuntimeError("达芬奇长时间没有响应"), {"ok": False, "error": "达芬奇当前打开的不是这次任务的项目「202H」，没有切换项目。"}):
            with self.subTest(reply=reply):
                job = self.timeline_job()
                self.bench.runner.inspect = reply
                started = self.start(job)["edit"]
                edit = self.wait(job["id"])
                self.assertEqual((edit["status"], edit["execution_status"], edit["validation"]), ("completed", "completed", None))
                self.assertIn("error", edit["delivery_snapshot"])
                self.assertFalse((self.bench.production.root / "edits" / started["id"] / "delivered-timeline.json").exists())
                self.assertFalse(self.bench.resolve.busy())
                # Usage marking still runs after a snapshot that could not be taken.
                self.assertEqual(self.bench.runner.calls[-1]["command"], "mark_usage")

    def test_each_edit_copies_the_current_personal_skill(self):
        job = self.timeline_job()
        source = self.bench.edit_skill_dir / "SKILL.md"
        source.write_text("当次规则：剪好保存后结束。", encoding="utf-8")
        self.start(job)
        self.wait(job["id"])
        first_copy = Path(self.editor.calls[-1]["cwd"]) / "skill" / "SKILL.md"
        # The copy carries the 2026-10-16 素材防复用 section after the personal skill.
        self.assertTrue(first_copy.read_text(encoding="utf-8").startswith(source.read_text(encoding="utf-8")))
        self.assertIn("素材防复用", first_copy.read_text(encoding="utf-8"))
        source.write_text("更新规则：完全按当前技能剪辑。", encoding="utf-8")
        self.start(job)
        self.wait(job["id"])
        second_copy = Path(self.editor.calls[-1]["cwd"]) / "skill" / "SKILL.md"
        self.assertTrue(second_copy.read_text(encoding="utf-8").startswith(source.read_text(encoding="utf-8")))
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
        self.assertEqual(judging_inspects(self.bench.runner.calls), [])

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

    def test_queued_task_requires_project_connection_before_editing_and_handoff_can_be_copied(self):
        job = self.timeline_job()
        self.bench.resolve = ResolveBridge(runner=lambda args, input, **o: subprocess.CompletedProcess(
            args, 0, json.dumps({"ok": False, "error": "达芬奇当前打开的是项目「X」"}, ensure_ascii=False).encode(), b""))
        self.start(job)
        edit = self.wait(job["id"])
        self.assertEqual(edit["status"], "failed")
        self.assertEqual(len(self.editor.calls), 0)
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
                self.assertEqual(judging_inspects(self.bench.runner.calls), [])

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
        self.current_readback(job)
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
        old.pop("completion_policy")  # Explicit historical engineering delivery keeps its baseline requirement.
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

    def test_restart_without_a_result_file_can_request_manual_current_coverage(self):
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
        self.current_readback(job)
        result = self.bench.recheck_edit(job["id"], started["id"], {"request_id": uuid4().hex})["edit"]
        self.assertEqual((result["status"], result["execution_status"]), ("awaiting_review", "interrupted"))
        self.assertEqual(result["validation"]["scope"], "current_coverage")
        self.assertNotIn("baseline_missing", [issue["code"] for issue in result["validation"]["issues"]])
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
        record.pop("completion_policy")  # Exercise the legacy engineering path's commit lock.
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
    def selection_handoff(self, mode, **values):
        job = {"id": "PRD-0123456789ab", "title": "稿", "script_id": "SCR-0123456789ab", "script_version": 1,
               "direction_id": "", "lines": [{"number": 1, "spoken": "Put tape between the clamp and blade.",
               "zh": "夹爪和刀面之间有保护纸。", "proof": "完整贴纸过程", "tags": "贴纸", "evidence": "写稿时待确认"}]}
        take = {"model_id": "eleven_v4", "groups": [{"number": 1, "start": 0, "end": 2, "coverage": 1}]}
        config = {"project": "202H", "fps": "29.97", "width": 1080, "height": 1920}
        return handoff_markdown(job, take, {"name": "Zack", "unique_id": "u", "frames": 59}, config, mode=mode,
                                instruction=values.get("instruction", ""), materials=[], voice_label="Zack",
                                notes={"opening": "品牌特写", "pickups": ["补拍贴纸全过程"]})

    def test_explain_handoff_does_not_turn_old_recommendations_into_gates(self):
        text = self.selection_handoff("explain")
        self.assertIn("- 选画面模式：解释画面逻辑", text)
        self.assertIn("允许用一组真实镜头共同说明", text)
        self.assertIn("不以词级时间定位作为选材或覆盖的前提", text)
        self.assertIn("贴好保护纸的近景可说明隔层", text)
        self.assertIn("不能自动升级为本次素材必须逐项满足的硬条件", text)
        self.assertIn("不额外要求身份特写", text)
        self.assertIn("已有画面描述可以支持其明确记载的信息", text)
        self.assertIn("各模式均保留真实动作、准确部件、工序阶段与效果的事实边界", text)
        self.assertIn("画面推荐（写稿线索）", text)
        # Keep the original recommendations available, while naming their provenance.
        self.assertIn("完整贴纸过程", text)
        self.assertIn("写稿时给的开头（选材线索）", text)
        self.assertIn("写稿时列的补拍建议（本次先查实际素材，不据此判缺）", text)
        self.assertIn("补拍贴纸全过程", text)

    def test_each_mode_receives_its_execution_standard_and_explicit_instruction(self):
        for mode, expected, excluded in (
                ("explain", "按完整意群解释原因", "在已有意群区间内确定关键动作的实际口播时点"),
                ("sync", "在已有意群区间内确定关键动作的实际口播时点", "贴好保护纸的近景可说明隔层"),
                ("montage", "可跨意群安排主题与信息推进", "贴好保护纸的近景可说明隔层")):
            with self.subTest(mode=mode):
                text = self.selection_handoff(mode, instruction="开头使用切纸结果")
                standards = text.split("## 本次画面模式执行标准", 1)[1].split("## 完整口播", 1)[0]
                self.assertIn(expected, standards)
                self.assertNotIn(excluded, standards)
                self.assertIn("- 本次额外要求：开头使用切纸结果", text)
                self.assertIn("本次明确选择的模式和额外要求优先于技能默认模式", text)
                self.assertIn("这些标记和配音轨不要删除、移动或改写", text)

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
        self.assertIn("| 1 | A ／ B | 甲 乙 | 无推荐 |", text)
        self.assertIn("未指定", text)


if __name__ == "__main__":
    unittest.main()
