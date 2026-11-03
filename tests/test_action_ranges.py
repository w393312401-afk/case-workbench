"""动作区间读自片段上的范围标记：改过的标记按改后的算，且不被当成标签或用量标记的对象。"""
import json
from pathlib import Path
import tempfile
import unittest

from edit_candidates import VisualIndex, build_candidates, write_task_files
from resolve_worker import ACTION_PREFIX, _mark_item, clip_actions, clip_markers


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
