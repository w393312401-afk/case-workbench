"""Candidate footage prepared before an AI edit, and clip descriptions remembered across edits."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "tests"))

from PIL import Image

import resolve_worker
from edit_candidates import (VisualIndex, build_candidates, candidate_key, compose_grid, merge_notes, rank_by_relevance, sample_frames,
                             label_status, source_span, write_task_files)
from test_production_editing import EditBench, Editor, Runner

CLIPS = [
    {"uid": "item-1", "start": 0, "end": 60, "name": "a.mp4", "media_id": "m1", "media_path": "C:/media/a.mp4",
     "source_start": 0, "source_end": 60, "has_audio": True, "labels": ["调节夹具间距 BK001"], "verified_labels": ["调节夹具间距 BK001"]},
    {"uid": "item-2", "start": 60, "end": 260, "name": "a.mp4", "media_id": "m1", "media_path": "C:/media/a.mp4",
     "source_start": 100, "source_end": 300, "has_audio": True, "labels": ["连续推磨 BK002"]},
    {"uid": "item-3", "start": 260, "end": 300, "name": "b.mp4", "media_id": "m2", "media_path": "C:/media/b.mp4",
     "source_start": 0, "source_end": 40, "has_audio": True, "labels": ["合箱号召 BK003"]},
]
LISTING = {"materials": [{"name": "Buck110折刀成品素材", "unique_id": "mat-1", "start_frame": 0, "fps": "29.97", "drop": False, "clips": CLIPS}],
           "missing": [], "used_complete": True,
           "used": [{"timeline_name": "Lab261008-202-1", "media_id": "m1", "media_path": "C:/media/a.mp4", "source_start": 150, "source_end": 180},
                    {"timeline_name": "Lab261009-202-2", "media_id": "m2", "media_path": "C:/media/b.mp4", "source_start": 0, "source_end": 40}]}


class CandidateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="edit-candidates-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_free_ranges_follow_other_edits_in_source_frames(self):
        found = {item["label"]: item for item in build_candidates(LISTING)}
        self.assertEqual(found["调节夹具间距 BK001"]["free"], [[0, 60]])
        self.assertEqual(found["连续推磨 BK002"]["free"], [[100, 150], [180, 300]])
        self.assertEqual(found["连续推磨 BK002"]["used_by"], ["Lab261008-202-1"])
        self.assertEqual(found["合箱号召 BK003"]["free_frames"], 0)
        self.assertTrue(found["调节夹具间距 BK001"]["key"].startswith("BK001-"))

    def test_inclusive_source_end_becomes_half_open_at_normal_speed(self):
        self.assertEqual(source_span({"start": 0, "end": 60, "source_start": 0, "source_end": 59}), (0, 60))
        self.assertEqual(source_span({"start": 0, "end": 30, "source_start": 0, "source_end": 59}), (0, 59))

    def test_only_a_clip_labeling_label_bound_to_the_clip_counts_as_seen(self):
        found = {item["label"]: item["label_status"] for item in build_candidates(LISTING)}
        self.assertEqual((found["调节夹具间距 BK001"], found["连续推磨 BK002"]), ("verified", "manual"))
        unsure = {**CLIPS[0], "labels": ["合盖 手势待核 BK009"], "verified_labels": ["合盖 手势待核 BK009"]}
        self.assertEqual(label_status(unsure), "unsure")
        self.assertEqual(label_status({**CLIPS[0], "labels": [], "verified_labels": []}), "none")

    def test_clips_worded_like_the_script_come_first(self):
        found = build_candidates(LISTING)
        ranked = rank_by_relevance(found, "可移动夹具固定刀身 组装；连续推磨（新拍）")
        self.assertEqual(ranked[0]["label"], "连续推磨 BK002")

    def test_key_is_the_source_range_whatever_the_label_or_position(self):
        moved = {**CLIPS[0], "start": 500, "end": 560, "labels": ["新标签 ZZ9"], "media_path": "c:\\media\\A.mp4"}
        self.assertEqual(candidate_key(CLIPS[0]).split("-")[1], candidate_key(moved).split("-")[1])
        self.assertNotEqual(candidate_key(CLIPS[0]), candidate_key({**CLIPS[0], "source_end": 61}))

    def test_sampling_takes_three_frames_or_six_for_long_clips(self):
        self.assertEqual(sample_frames(0, 60), [2, 30, 57])
        self.assertEqual(len(sample_frames(60, 260)), 6)
        self.assertEqual(sample_frames(10, 11), [10])

    def test_grid_keeps_each_frame_large_and_in_order(self):
        files = []
        for index in range(6):
            path = self.root / f"f{index}.png"
            Image.new("RGB", (1080, 1920), (index * 40, 0, 0)).save(path)
            files.append(path)
        target = self.root / "grid" / "k.jpg"
        compose_grid(files, target, [0, 10, 20, 30, 40, 50])
        with Image.open(target) as image:
            self.assertLessEqual(max(image.size), 1560)
            self.assertGreaterEqual(image.size[0] // 3, 400)

    def test_task_files_link_grids_list_free_clips_and_reuse_descriptions(self):
        index = VisualIndex(self.root / "index", "202H")
        found = build_candidates(LISTING)
        index.frames.mkdir(parents=True)
        first = found[0]
        Image.new("RGB", (10, 10)).save(index.frames / f"{first['key']}.jpg")
        entries = {first["key"]: {"description": {"summary": "手调夹具间距，特写", "cta": False}}}
        folder = self.root / "task"
        stats = write_task_files(folder, found, index, entries, LISTING)
        self.assertEqual((stats["listed"], stats["used_up"], stats["described"], stats["grids"]), (2, 1, 1, 1))
        text = (folder / "candidates.md").read_text(encoding="utf-8")
        self.assertIn("手调夹具间距，特写；无号召手势", text)
        self.assertIn("可用源帧 [100, 150) [180, 300)", text)
        self.assertNotIn("BK003", text)
        self.assertTrue((folder / "candidates" / f"{first['key']}.jpg").is_file())

    def test_notes_are_kept_only_for_listed_clips_with_a_summary(self):
        index = VisualIndex(self.root / "index", "202H")
        found = build_candidates(LISTING)
        folder = self.root / "task"
        write_task_files(folder, found, index, {}, LISTING)
        (folder / "visual-notes.json").write_text(json.dumps([
            {"key": found[1]["key"], "summary": "连续推磨，中景", "subject": "Buck110", "cta": False, "clear": True, "extra": "x"},
            {"key": "BK999-00000000", "summary": "不存在"}, {"key": found[0]["key"], "summary": ""}], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(merge_notes(folder, index, source={"edit_id": "EDT-1"}), {"saved": 1, "rejected": 2})
        entry = index.load()[found[1]["key"]]
        self.assertEqual(entry["description"], {"summary": "连续推磨，中景", "subject": "Buck110", "cta": False, "clear": True})
        self.assertEqual((entry["source"], entry["described_by"]["edit_id"]), ([100, 300], "EDT-1"))


class Item:
    def __init__(self, uid, start, end, source, labels=(), custom=""):
        self.uid, self.start, self.end, self.source, self.labels, self.custom = uid, start, end, source, labels, custom

    def GetUniqueId(self): return self.uid
    def GetStart(self): return self.start
    def GetEnd(self): return self.end
    def GetName(self): return "a.mp4"
    def GetSourceStartFrame(self): return self.source
    def GetSourceEndFrame(self): return self.source + self.end - self.start
    def GetClipEnabled(self): return True
    def GetMarkers(self): return {index: {"name": label, "customData": self.custom} for index, label in enumerate(self.labels)}
    def GetMediaPoolItem(self):
        class Media:
            def GetUniqueId(self): return "m1"
            def GetClipProperty(self): return {"File Path": "C:/media/a.mp4", "Audio Ch": "2"}
        return Media()


class Timeline:
    def __init__(self, name, uid, items, drop="0"):
        self.name, self.uid, self.items, self.drop, self.position = name, uid, items, drop, None

    def GetName(self): return self.name
    def GetUniqueId(self): return self.uid
    def GetStartFrame(self): return 0
    def GetTrackCount(self, kind): return 1 if kind == "video" else 0
    def GetItemListInTrack(self, kind, index): return self.items
    def GetSetting(self, key): return {"timelineFrameRate": "29.97", "timelineDropFrameTimecode": self.drop}.get(key)

    def SetCurrentTimecode(self, text):
        hours, minutes, seconds, frames = map(int, text.replace(";", ":").split(":"))
        self.position = ((hours * 60 + minutes) * 60 + seconds) * 30 + frames
        return True

    def GetCurrentVideoItem(self):
        return next((item for item in self.items if item.start <= self.position < item.end), None)


class Project:
    def __init__(self, timelines):
        self.timelines, self.current, self.exported = timelines, timelines[-1], []

    def GetName(self): return "202H"
    def GetTimelineCount(self): return len(self.timelines)
    def GetTimelineByIndex(self, index): return self.timelines[index - 1]
    def GetCurrentTimeline(self): return self.current
    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def ExportCurrentFrameAsStill(self, path):
        Image.new("RGB", (108, 192)).save(path)
        self.exported.append((self.current.position, Path(path).name))
        return True


class Resolve:
    def __init__(self, project):
        self.project = project

    def GetProjectManager(self):
        project = self.project
        class Manager:
            def GetCurrentProject(self): return project
        return Manager()

    def GetCurrentPage(self): return "edit"


class WorkerTests(unittest.TestCase):
    def test_timecode_counts_drop_frames_only_when_the_timeline_uses_them(self):
        self.assertEqual(resolve_worker.timecode(1800, "29.97", False), "00:01:00:00")
        self.assertEqual(resolve_worker.timecode(1800, "29.97", True), "00:01:00;02")
        self.assertEqual(resolve_worker.timecode(17982, "29.97", True), "00:10:00;00")

    def test_candidates_list_material_clips_and_where_other_edits_used_them(self):
        material = Timeline("Buck110折刀成品素材", "mat-1", [Item("i1", 0, 60, 0, ["夹具 BK001"], "material_index_v1:i1"),
                                                         Item("i2", 60, 120, 60, ["手写 推磨"], "material_index_v1:other")])
        edit = Timeline("Lab261008-202-1", "e1", [Item("x", 0, 20, 70)])
        backup = Timeline("Lab261008-202-1_重剪前备份_1009", "b1", [Item("y", 0, 20, 0)])
        target = Timeline("Zack261011-202-1", "t1", [Item("z", 0, 20, 0)])
        reply = resolve_worker.candidates(Resolve(Project([material, edit, backup, target])),
                                          {"project": "202H", "materials": [], "target_uid": "t1"})
        self.assertEqual([(clip["labels"], clip["verified_labels"]) for clip in reply["materials"][0]["clips"]],
                         [(["夹具 BK001"], ["夹具 BK001"]), (["手写 推磨"], [])])
        self.assertEqual([(item["timeline_name"], item["source_start"]) for item in reply["used"]], [("Lab261008-202-1", 70)])

    def test_copied_timeline_labels_count_while_the_marker_is_still_in_the_visible_clip(self):
        class Copied(Item):
            def __init__(self, uid, marker, left=100):
                super().__init__(uid, 0, 60, left, ["研磨 BK011"], "material_index_v1:old-id")
                self.marker, self.left = marker, left
            def GetLeftOffset(self): return self.left
            def GetMarkers(self): return {self.marker: {"name": "研磨 BK011", "customData": self.custom}}
        material = Timeline("Buck110折刀成品素材", "mat-1", [Copied("i1", 130), Copied("i2", 20)])
        reply = resolve_worker.candidates(Resolve(Project([material])), {"project": "202H", "materials": [], "target_uid": ""})
        self.assertEqual([clip["verified_labels"] for clip in reply["materials"][0]["clips"]], [["研磨 BK011"], []])

    def test_frames_land_on_the_expected_clip_before_exporting(self):
        material = Timeline("素材", "mat-1", [Item("i1", 0, 60, 0), Item("i2", 60, 120, 60)])
        project = Project([material, Timeline("Zack", "t1", [])])
        with tempfile.TemporaryDirectory() as folder:
            reply = resolve_worker.frames(Resolve(project), {"project": "202H", "unique_id": "mat-1", "folder": folder, "shots": [
                {"key": "A", "item_uid": "i1", "frames": [2, 30, 57]}, {"key": "B", "item_uid": "i1", "frames": [70]}]})
        self.assertEqual([len(shot["files"]) for shot in reply["shots"]], [3, 0])
        self.assertEqual(reply["shots"][1]["problem"], "播放头没有落在这个片段上")
        self.assertEqual([position for position, _ in project.exported], [2, 30, 57])


class PreparingRunner(Runner):
    """The fake worker also lists material clips and exports frames."""

    def __init__(self):
        super().__init__()
        self.listing = json.loads(json.dumps(LISTING))

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] == "candidates":
            body = {"ok": True, **self.listing}
        elif request["command"] == "frames":
            self.calls.append(request)
            shots = []
            for shot in request["shots"]:
                files = []
                for number, _ in enumerate(shot["frames"]):
                    path = Path(request["folder"]) / f"{shot['key']}_{number}.png"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    Image.new("RGB", (108, 192)).save(path)
                    files.append(str(path))
                shots.append({"key": shot["key"], "files": files, "problem": ""})
            body = {"ok": True, "shots": shots}
        elif request["command"] == "status":
            body = {"ok": True, "project": "202H", "current_timeline_uid": "uid-1", "current_timeline": "Lab", "current_timecode": "00:00:01:00", "page": "edit"}
        else:
            return super().__call__(args, input, **options)
        if request["command"] != "frames":
            self.calls.append(request)
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class NotingEditor(Editor):
    def run(self, prompt, *, cwd, **options):
        path = Path(cwd, "candidates.json")
        listed = json.loads(path.read_text(encoding="utf-8"))["candidates"] if path.is_file() else []
        Path(cwd, "visual-notes.json").write_text(json.dumps(
            [{"key": item["key"], "summary": f"看过 {item['label']}", "cta": False} for item in listed], ensure_ascii=False), encoding="utf-8")
        return super().run(prompt, cwd=cwd, **options)


class PreparedEditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="production-prepared-")
        self.addCleanup(temp.cleanup)
        mcp = Path(temp.name) / "ResolveMCP.exe"
        mcp.write_bytes(b"")
        environment = patch.dict(os.environ, {"RESOLVE_MCP": str(mcp)})
        environment.start()
        self.addCleanup(environment.stop)
        self.editor = NotingEditor()
        self.bench = EditBench(temp.name, self.editor)
        self.bench.runner = PreparingRunner()
        self.bench.resolve.runner = self.bench.runner
        from test_production_editing import EditingTests
        self.helper = EditingTests()
        self.helper.bench, self.helper.editor = self.bench, self.editor

    def run_edit(self, job):
        self.helper.start(job)
        return self.helper.wait(job["id"])

    def test_first_edit_prepares_grids_and_the_next_reuses_grids_and_descriptions(self):
        job = self.helper.timeline_job()
        edit = self.run_edit(job)
        commands = [call["command"] for call in self.bench.runner.calls]
        self.assertEqual(commands.count("frames"), 1)
        self.assertLess(commands.index("restore"), commands.index("inspect", commands.index("candidates")))
        # BK001 has a clip-labeling label, so only the hand-labelled BK002 needs a contact image.
        self.assertEqual((edit["preparation"]["new_grids"], edit["preparation"]["verified"]), (1, 1))
        self.assertEqual([call["shots"][0]["key"].split("-")[0] for call in self.bench.runner.calls if call["command"] == "frames"], ["BK002"])
        self.assertEqual(edit["visual_notes"], {"saved": 2, "rejected": 0})
        prompt = self.editor.calls[0]["prompt"]
        self.assertIn("## 候选画面（工作台已整理）", prompt)
        self.assertIn("1 个片段是「已核标签」", prompt)
        folder = Path(self.editor.calls[0]["cwd"])
        self.assertIn((folder / "handoff.md").read_text(encoding="utf-8"), prompt)
        self.assertFalse((folder / "stills" / "_prepare").exists())

        self.bench.runner.calls.clear()
        job = self.bench.production.job(job["id"])
        edit = self.run_edit(job)
        self.assertNotIn("frames", [call["command"] for call in self.bench.runner.calls])
        self.assertEqual((edit["preparation"]["described"], edit["preparation"]["grids"], edit["preparation"]["to_look"]), (2, 1, 0))
        folder = Path(self.editor.calls[1]["cwd"])
        self.assertIn("看过 连续推磨 BK002", (folder / "candidates.md").read_text(encoding="utf-8"))

    def test_time_budget_defers_the_rest_to_later_edits(self):
        job = self.helper.timeline_job()
        with patch("workbench_editing.PREPARE_BUDGET", -1):
            edit = self.run_edit(job)
        self.assertEqual((edit["preparation"]["new_grids"], edit["preparation"]["deferred"]), (0, 1))
        self.assertIn("另有 1 个需看图的片段这次来不及导出对照图", self.editor.calls[0]["prompt"])
        self.assertIn("无对照图", (Path(self.editor.calls[0]["cwd"]) / "candidates.md").read_text(encoding="utf-8"))

    def test_preparation_failure_lets_the_ai_look_at_footage_itself(self):
        self.bench.runner.listing = {"error": "x"}
        job = self.helper.timeline_job()
        edit = self.run_edit(job)
        self.assertEqual(edit["execution_status"], "completed")
        self.assertIn("没能整理候选画面", self.editor.calls[0]["prompt"])

    def test_stop_during_preparation_never_starts_the_ai(self):
        job = self.helper.timeline_job()
        original = self.bench._export_grids

        def stop_then_export(edit_id, *args):
            self.bench.cancel_edit(job["id"], edit_id)
            return original(edit_id, *args)

        self.bench._export_grids = stop_then_export
        edit = self.run_edit(job)
        self.assertEqual((edit["status"], self.editor.calls), ("cancelled", []))
        self.assertIn("还没开始剪辑", edit["error"])
        self.assertIn("restore", [call["command"] for call in self.bench.runner.calls])


if __name__ == "__main__":
    unittest.main()
