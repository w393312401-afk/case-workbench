"""动作区间读自片段上的范围标记：改过的标记按改后的算，且不被当成标签或用量标记的对象。

磨削阶段读自素材时间线上的阶段区间。"""
import json
from pathlib import Path
import tempfile
import unittest

from edit_candidates import STAGE_NOTE, VisualIndex, build_candidates, write_task_files
from resolve_worker import (ACTION_PREFIX, STAGE_PREFIX, _mark_item, clip_actions, clip_markers, clip_stage, source_shift,
                            timeline_stages, true_source)


class Item:
    def __init__(self, markers, left=1000, source=1001, start=0, end=100):
        self.markers, self.left, self.source, self.start, self.end = markers, left, source, start, end
        self.deleted = []

    def GetLeftOffset(self):
        return self.left

    def GetSourceStartFrame(self):
        return self.source

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetMarkers(self):
        return self.markers

    def GetClipColor(self):
        return ""

    def SetClipColor(self, color):
        return True

    def DeleteMarkerAtFrame(self, frame):
        self.deleted.append(frame)
        return True

    def AddMarker(self, *args):
        return True


ACTION = {"name": "切纸", "duration": 40, "customData": f"{ACTION_PREFIX}:uid"}


class ClipActionsTest(unittest.TestCase):
    def test_marker_becomes_a_source_range(self):
        self.assertEqual(clip_actions(Item({1010: ACTION})), [{"name": "切纸", "start": 1011, "end": 1051, "complete": True}])

    def test_partial_is_read_from_the_name(self):
        found = clip_actions(Item({1010: {**ACTION, "name": "切纸（不完整）"}}))[0]
        self.assertEqual((found["name"], found["complete"]), ("切纸", False))

    def test_range_stops_at_the_clip_and_other_clips_ranges_are_ignored(self):
        self.assertEqual(clip_actions(Item({1090: ACTION}))[0]["end"], 1101)
        self.assertEqual(clip_actions(Item({5000: ACTION})), [])

    def test_it_is_not_a_label(self):
        label = {"name": "切纸测试 BK1", "customData": "material_index_v1:x"}
        self.assertEqual([info["name"] for _, info in clip_markers(Item({1010: ACTION, 1020: label}))], ["切纸测试 BK1"])

    def test_usage_marking_leaves_it_alone(self):
        item = Item({1010: ACTION, 1020: {"name": "标签", "customData": "material_index_v1:x"}})
        _mark_item(item, 2, {"colored": 0, "cleared": 0})
        self.assertNotIn(1010, item.deleted)
        self.assertIn(1020, item.deleted)


class ListedActionsTest(unittest.TestCase):
    def test_range_reaches_the_list(self):
        clip = {"uid": "u", "start": 0, "end": 100, "source_start": 10, "source_end": 109, "media_id": "m", "media_path": "x", "name": "切纸",
                "labels": ["切纸 BK1"], "verified_labels": ["切纸 BK1"], "actions": [{"name": "切纸", "start": 20, "end": 90, "complete": False}]}
        listing = {"materials": [{"name": "素材", "unique_id": "t", "clips": [clip]}]}
        with tempfile.TemporaryDirectory() as folder:
            write_task_files(folder, build_candidates(listing), VisualIndex(Path(folder) / "i", "202H"), {}, listing)
            row = json.loads((Path(folder) / "candidates.json").read_text(encoding="utf-8"))["candidates"][0]
            self.assertEqual(row["actions"][0]["start"], 20)
            self.assertIn("切纸 [20, 90)（不完整）", (Path(folder) / "candidates.md").read_text(encoding="utf-8"))


class ClipNameLabelTest(unittest.TestCase):
    def status(self, name, labels=()):
        from edit_candidates import label_status
        return label_status({"name": name, "labels": list(labels), "verified_labels": []})

    def test_a_labeled_name_counts_as_checked(self):
        self.assertEqual(self.status("切纸测试 夹持状态 BK024"), "verified")
        self.assertEqual(self.status("夹具翻面 LD114a"), "verified")

    def test_file_names_and_unsure_names_do_not(self):
        self.assertEqual(self.status("Buck110折刀成品素材.mp4"), "none")
        self.assertEqual(self.status("C0012"), "none")
        self.assertEqual(self.status("研磨 目数待核 BK030"), "unsure")

    def test_the_name_wins_over_old_markers(self):
        from edit_candidates import clip_labels
        self.assertEqual(clip_labels({"name": "马克笔上色 BK025", "labels": ["旧标签 BK025"]}), ["马克笔上色 BK025"])
        self.assertEqual(clip_labels({"name": "IMG_1234", "labels": ["手写"]}), ["手写"])


if __name__ == "__main__":
    unittest.main()


class Timeline:
    def __init__(self, markers, start=0):
        self.markers, self.start = markers, start

    def GetStartFrame(self):
        return self.start

    def GetMarkers(self):
        return self.markers


STAGE = {"duration": 100, "customData": f"{STAGE_PREFIX}:t:0:100"}


class StageRangesTest(unittest.TestCase):
    """磨削阶段读自素材时间线上的阶段区间：用户填的名称照原样读，片段落在哪一段就算哪一段。"""

    def test_only_stage_ranges_count_and_frames_start_at_the_timeline_start(self):
        timeline = Timeline({0.0: {**STAGE, "name": "600"}, 100.0: {**STAGE, "name": "目数待填 02", "duration": 50},
                             150.0: {"name": "#1 口播", "duration": 60, "customData": "workbench-group:PRD-1:1"},
                             210.0: {"name": "手画的范围", "duration": 60, "customData": ""}}, start=86400)
        self.assertEqual(timeline_stages(timeline), [{"name": "600", "start": 86400, "end": 86500, "index": 1, "count": 2},
                                                     {"name": "目数待填 02", "start": 86500, "end": 86550, "index": 2, "count": 2}])

    def test_a_clip_takes_the_stage_covering_most_of_it(self):
        stages = timeline_stages(Timeline({0: {**STAGE, "name": "600"}, 100: {**STAGE, "name": "1000"}}))
        self.assertEqual(clip_stage(stages, 20, 80), {"name": "600", "index": 1, "count": 2})
        self.assertEqual(clip_stage(stages, 90, 130)["name"], "1000")
        self.assertIsNone(clip_stage(stages, 200, 260))
        self.assertIsNone(clip_stage([], 0, 60))

    def test_stage_reaches_the_list_only_for_clips_inside_one(self):
        clip = {"uid": "u", "start": 0, "end": 100, "source_start": 10, "source_end": 109, "media_id": "m", "media_path": "x", "name": "研磨",
                "labels": ["研磨 目数待确认 BK1"], "verified_labels": ["研磨 目数待确认 BK1"], "stage": {"name": "600", "index": 3, "count": 18}}
        outside = {**clip, "uid": "v", "start": 100, "end": 200, "source_start": 200, "source_end": 299, "stage": None}
        listing = {"materials": [{"name": "素材", "unique_id": "t", "clips": [clip, outside]}]}
        with tempfile.TemporaryDirectory() as folder:
            stats = write_task_files(folder, build_candidates(listing), VisualIndex(Path(folder) / "i", "202H"), {}, listing)
            rows = json.loads((Path(folder) / "candidates.json").read_text(encoding="utf-8"))["candidates"]
            text = (Path(folder) / "candidates.md").read_text(encoding="utf-8")
        self.assertEqual(([row["stage"] for row in rows], stats["staged"]), ([{"name": "600", "index": 3, "count": 18}, None], 1))
        self.assertEqual(text.count("；磨削阶段：600（第 3/18 段）"), 1)
        self.assertIn(STAGE_NOTE, text)

    def test_a_list_without_stages_says_nothing_about_them(self):
        clip = {"uid": "u", "start": 0, "end": 100, "source_start": 10, "source_end": 109, "media_id": "m", "media_path": "x", "name": "切纸",
                "labels": ["切纸 BK1"], "verified_labels": ["切纸 BK1"]}
        listing = {"materials": [{"name": "素材", "unique_id": "t", "clips": [clip]}]}
        with tempfile.TemporaryDirectory() as folder:
            write_task_files(folder, build_candidates(listing), VisualIndex(Path(folder) / "i", "202H"), {}, listing)
            self.assertNotIn("磨削阶段", (Path(folder) / "candidates.md").read_text(encoding="utf-8"))


class Clip(Item):
    """A clip of one long file: Resolve may report its source start one frame before the frame it begins on."""

    def __init__(self, left, reported, length=96, reported_end=None, markers=None):
        super().__init__(markers or {}, left=left, source=reported, start=left, end=left + length)
        self.reported_end = reported + length if reported_end is None else reported_end

    def GetSourceEndFrame(self):
        return self.reported_end


class TrueSourceTest(unittest.TestCase):
    """接口报的源起点比真实首帧早 1 帧时，交给剪辑的是真实范围：照报的数取材，镜头开头会带进上一个片段的 1 帧。"""

    def value(self, item):
        return {"start": item.GetStart(), "end": item.GetEnd(), "source_start": item.GetSourceStartFrame(), "source_end": item.GetSourceEndFrame()}

    def test_only_a_difference_of_exactly_one_frame_is_a_misreport(self):
        self.assertEqual([source_shift(Clip(10056, reported)) for reported in (10055, 10056, 10054, 11195)], [1, 0, 0, 0])
        self.assertEqual(source_shift(Item({}, left=None)), 0)

    def test_the_real_range_starts_one_later_and_is_as_long_as_the_clip(self):
        shifted, long_end = Clip(10056, 10055), Clip(497, 496, length=201, reported_end=698)
        self.assertEqual(true_source(shifted, self.value(shifted)), [10056, 10152])
        self.assertEqual(true_source(long_end, self.value(long_end)), [497, 698])
        exact = Clip(10056, 10056)
        self.assertIsNone(true_source(exact, self.value(exact)))

    def test_a_retimed_clip_keeps_its_reported_length(self):
        fast = Clip(1000, 999, length=50, reported_end=1099)
        self.assertEqual(true_source(fast, self.value(fast)), [1000, 1100])

    def test_action_ranges_count_from_the_real_first_frame(self):
        marker = {10060: {"name": "切纸", "duration": 40, "customData": f"{ACTION_PREFIX}:uid"}}
        self.assertEqual(clip_actions(Clip(10056, 10055, markers=marker))[0]["start"], 10060)
        self.assertEqual(clip_actions(Clip(10056, 10056, markers=marker))[0]["start"], 10060)

    def test_the_editor_gets_the_real_range_under_the_same_key(self):
        clip = {"uid": "u", "start": 10056, "end": 10152, "source_start": 10055, "source_end": 10151, "media_id": "m", "media_path": "x",
                "name": "切纸测试 LZZ130", "labels": [], "verified_labels": []}
        listing = lambda **extra: {"materials": [{"name": "素材", "unique_id": "t", "clips": [{**clip, **extra}]}]}
        reported, real = build_candidates(listing())[0], build_candidates(listing(true_source=[10056, 10152]))[0]
        self.assertEqual((reported["source"], real["source"], real["free"]), ([10055, 10151], [10056, 10152], [[10056, 10152]]))
        self.assertEqual(reported["key"], real["key"])

    def test_a_shot_from_the_clip_head_is_not_a_use_of_the_clip_before_it(self):
        before = {"uid": "a", "start": 0, "end": 60, "source_start": 9996, "source_end": 10056, "media_id": "m", "media_path": "x",
                  "name": "导向研磨 LZZ129", "labels": [], "verified_labels": []}
        after = {**before, "uid": "b", "start": 60, "end": 156, "source_start": 10055, "source_end": 10151, "name": "切纸测试 LZZ130",
                 "true_source": [10056, 10152]}
        shot = {"timeline_name": "成片", "start": 0, "end": 96, "source_start": 10056, "source_end": 10152, "media_id": "m", "media_path": "x"}
        rows = build_candidates({"materials": [{"name": "素材", "unique_id": "t", "clips": [before, after]}], "used": [shot]}, count_usage=True)
        self.assertEqual([row["use_count"] for row in rows], [0, 1])
