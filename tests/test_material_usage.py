"""素材防复用: candidates carry how many finished timelines used them, and material clips are colored by that count."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import resolve_worker
from edit_candidates import (VisualIndex, add_in_progress_usage, build_candidates, pick_keys, planned_spans, refreshed_usage,
                             write_task_files)


class Media:
    def __init__(self, uid, path):
        self.uid, self.path = uid, path

    def GetUniqueId(self):
        return self.uid

    def GetClipProperty(self):
        return {"File Path": self.path, "Audio Ch": "2"}


class Item:
    def __init__(self, uid, media, start, end, source_start, markers=None, color=""):
        self.uid, self.media, self.start, self.end = uid, media, start, end
        self.source_start, self.markers, self.color = source_start, dict(markers or {}), color

    def GetUniqueId(self):
        return self.uid

    def GetMediaPoolItem(self):
        return self.media

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetName(self):
        return "clip"

    def GetSourceStartFrame(self):
        return self.source_start

    def GetSourceEndFrame(self):
        return self.source_start + self.end - self.start - 1

    def GetLeftOffset(self):
        return self.source_start

    def GetMarkers(self):
        return dict(self.markers)

    def DeleteMarkerAtFrame(self, frame):
        return self.markers.pop(frame, None) is not None

    def AddMarker(self, frame, color, name, note, duration, custom):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "name": name, "note": note, "duration": duration, "customData": custom}
        return True

    def GetClipColor(self):
        return self.color

    def SetClipColor(self, color):
        self.color = color
        return True

    def ClearClipColor(self):
        self.color = ""
        return True


class Timeline:
    def __init__(self, name, items):
        self.name, self.items = name, items

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return "uid-" + self.name

    def GetStartFrame(self):
        return 0

    def GetSetting(self, key):
        return "29.97" if key == "timelineFrameRate" else "0"

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 0

    def GetItemListInTrack(self, kind, index):
        return self.items


class Project:
    def __init__(self, timelines):
        self.list = timelines

    def GetName(self):
        return "202H"

    def GetTimelineCount(self):
        return len(self.list)

    def GetTimelineByIndex(self, index):
        return self.list[index - 1]


class Manager:
    def __init__(self, project):
        self.project, self.saved = project, 0

    def GetCurrentProject(self):
        return self.project

    def SaveProject(self):
        self.saved += 1
        return True


class Resolve:
    def __init__(self, timelines):
        self.manager = Manager(Project(timelines))

    def GetProjectManager(self):
        return self.manager


A, B = Media("m1", "C:/media/a.mp4"), Media("m2", "C:/media/b.mp4")


def project(finished_count):
    labeled = Item("mat-1", A, 0, 60, 100, {110: {"color": "Blue", "name": "连续推磨 BK002", "note": "手稳",
                                                  "duration": 1, "customData": "material_index_v1:mat-1"}})
    plain = Item("mat-2", B, 60, 100, 0)
    unused = Item("mat-3", A, 100, 160, 900, {905: {"color": "Blue", "name": "合箱 BK003", "note": "", "duration": 1, "customData": ""}})
    finished = [Timeline(f"Lab26100{number}-202-1", [Item(f"s{number}", A, 0, 20, 120), Item(f"t{number}", B, 20, 40, 10),
                                                     Item(f"u{number}", A, 40, 50, 130)])
                for number in range(finished_count)]
    material = Timeline("202H成品素材", [labeled, plain, unused])
    return Resolve([material, Timeline("Lab备份", [Item("x", A, 0, 60, 100)]), *finished]), labeled, plain, unused


class MarkUsageTests(unittest.TestCase):
    def test_colors_follow_davinci_menu_order_and_stop_at_the_last(self):
        self.assertEqual([resolve_worker.usage_color(count) for count in (0, 1, 2, 16, 40)],
                         ["", "Orange", "Apricot", "Chocolate", "Chocolate"])

    def test_each_finished_timeline_counts_once_and_notes_keep_the_users_text(self):
        resolve, labeled, plain, unused = project(2)
        result = resolve_worker.mark_usage(resolve, {"project": "202H"})
        self.assertEqual((labeled.color, plain.color, unused.color), ("Apricot", "Apricot", ""))
        marker = labeled.markers[110]
        self.assertEqual((marker["name"], marker["note"], marker["customData"]), ("连续推磨 BK002", "已使用 2 次\n手稳", "material_index_v1:mat-1"))
        ours = next(iter(plain.markers.values()))
        self.assertEqual((ours["note"], ours["customData"]), ("已使用 2 次", resolve_worker.USAGE_MARKER))
        self.assertEqual(unused.markers[905]["note"], "")
        self.assertEqual((result["clips"], result["used"], result["finished"]), (3, 2, 2))
        self.assertEqual(resolve.manager.saved, 1)

    def test_counts_update_and_vanish_with_the_timelines(self):
        resolve, labeled, plain, _ = project(3)
        resolve_worker.mark_usage(resolve, {"project": "202H"})
        self.assertEqual(labeled.markers[110]["note"], "已使用 3 次\n手稳")
        project_list = resolve.manager.project.list
        del project_list[2:]
        resolve_worker.mark_usage(resolve, {"project": "202H"})
        self.assertEqual((labeled.color, plain.color), ("", ""))
        self.assertEqual(labeled.markers[110]["note"], "手稳")
        self.assertEqual(plain.markers, {})

    def test_an_unmarked_clip_keeps_the_users_own_color(self):
        resolve, _, _, unused = project(1)
        unused.color = "Pink"
        resolve_worker.mark_usage(resolve, {"project": "202H"})
        self.assertEqual(unused.color, "Pink")

    def test_usage_markers_are_not_labels(self):
        resolve, _, plain, _ = project(1)
        resolve_worker.mark_usage(resolve, {"project": "202H"})
        listing = resolve_worker.candidates(resolve, {"project": "202H", "count_usage": True})
        clip = next(item for item in listing["materials"][0]["clips"] if item["uid"] == "mat-2")
        self.assertEqual(clip["labels"], [])
        self.assertTrue(listing["usage_counted"])
        self.assertEqual({item["timeline_name"] for item in listing["used"]}, {"Lab261000-202-1"})


class CandidateUsageTests(unittest.TestCase):
    def test_candidates_list_use_counts_least_used_first_and_keep_every_clip(self):
        resolve, *_ = project(2)
        listing = resolve_worker.candidates(resolve, {"project": "202H", "count_usage": True})
        found = build_candidates(listing, count_usage=True)
        counts = {item["item_uid"]: item["use_count"] for item in found}
        self.assertEqual(counts, {"mat-1": 2, "mat-2": 2, "mat-3": 0})
        self.assertTrue(all(item["free"] == [item["source"]] for item in found))
        with tempfile.TemporaryDirectory() as temp:
            stats = write_task_files(Path(temp) / "task", found, VisualIndex(Path(temp) / "index", "202H"), {}, listing, count_usage=True)
            text = (Path(temp) / "task" / "candidates.md").read_text(encoding="utf-8")
            data = json.loads((Path(temp) / "task" / "candidates.json").read_text(encoding="utf-8"))
        self.assertEqual((stats["listed"], stats["unused"]), (3, 1))
        self.assertTrue(data["usage_counted"])
        rows = [line for line in text.splitlines() if line.startswith("- ")]
        self.assertIn("未用过", rows[0])
        self.assertIn("已用 2 次（Lab261000-202-1、Lab261001-202-1）", rows[1])

    def test_footage_planned_by_a_running_edit_counts_once(self):
        found = [{"key": "A-1", "media_id": "m1", "media_path": "D:/a.mov", "source": [0, 50], "used_by": [], "use_count": 0},
                 {"key": "B-1", "media_id": "m1", "media_path": "D:/a.mov", "source": [50, 90], "used_by": ["Lab-1"], "use_count": 1},
                 {"key": "C-1", "media_id": "m2", "media_path": "D:/b.mov", "source": [0, 50], "used_by": [], "use_count": 0}]
        spans = planned_spans(["A-1", "B-1"], found)
        self.assertEqual([span["source"] for span in spans], [[0, 50], [50, 90]])
        # Lab-1 already placed B-1, so its plan does not count it again; Lab-2 still running counts both.
        add_in_progress_usage(found, {"Lab-1": spans, "Lab-2": spans[:1]})
        self.assertEqual([item["used_by"] for item in found], [["Lab-1（剪辑中）", "Lab-2（剪辑中）"], ["Lab-1"], []])
        self.assertEqual([item["use_count"] for item in found], [2, 1, 0])
        add_in_progress_usage(found, {"Lab-2": spans[:1]})
        self.assertEqual(found[0]["use_count"], 2)

    def test_first_choice_entries_resolve_to_candidate_keys(self):
        found = [{"key": "DG043-3d173093", "media_id": "m1", "media_path": "", "source": [0, 50]},
                 {"key": "DG220-aaaaaaaa", "media_id": "m1", "media_path": "", "source": [50, 90]},
                 {"key": "JM051-bbbbbbbb", "media_id": "m2", "media_path": "", "source": [0, 40]},
                 {"key": "JM051-cccccccc", "media_id": "m2", "media_path": "", "source": [40, 80]},
                 {"key": "DIA600-eeeeeeee", "media_id": "m4", "media_path": "", "source": [0, 20]},
                 {"key": "DIA600-ffffffff", "media_id": "m5", "media_path": "", "source": [0, 20]},
                 {"key": "C-dddddddd", "media_id": "m3", "media_path": "", "source": [0, 30]}]
        # Exact keys, as objects or plain strings, in the planner's order and without repeats.
        self.assertEqual(pick_keys([{"group": 2, "key": "DG220-aaaaaaaa"}, "DG043-3d173093", {"key": " DG220-aaaaaaaa "}], found),
                         (["DG220-aaaaaaaa", "DG043-3d173093"], 0))
        # A bare code or a mistyped digest still names a clip whose code is on one media file only.
        self.assertEqual(pick_keys(["DG043", {"key": "DG220-aaaaaaab"}], found), (["DG043-3d173093", "DG220-aaaaaaaa"], 0))
        # JM051 is one labelled clip split into adjacent pieces of one media file: the code names every piece.
        self.assertEqual(pick_keys(["JM051"], found), (["JM051-bbbbbbbb", "JM051-cccccccc"], 0))
        # DIA600 is on two media files, so only the exact key names one of them.
        self.assertEqual(pick_keys(["DIA600", "DIA600-eeeeeeef"], found), ([], 2))
        self.assertEqual(pick_keys(["DIA600-ffffffff"], found), (["DIA600-ffffffff"], 0))
        # Codeless clips, placeholders, other workers' clips and junk entries name nothing.
        self.assertEqual(pick_keys(["C", "C-ddddddde", "候选编号", "ZS999-12345678", 7, None, {"group": 1}], found), ([], 7))
        self.assertEqual(pick_keys(["C-dddddddd"], found), (["C-dddddddd"], 0))

    def test_held_spans_follow_the_chosen_keys_only(self):
        shared = {"key": "BK021-eeeeeeee", "media_id": "m1", "media_path": "", "source": [0, 50]}
        found = [{**shared, "timeline": "Buck110成品素材"}, {**shared, "timeline": "Buck110成品素材 副本"},
                 {"key": "BK022-ffffffff", "media_id": "m1", "media_path": "", "source": [50, 90]}]
        # The same footage on two material timelines shares one key; both listings are the clip.
        self.assertEqual([span["source"] for span in planned_spans(["BK021-eeeeeeee"], found)], [[0, 50], [0, 50]])
        self.assertEqual(planned_spans([], found), [])

    def test_refreshed_counts_follow_the_source_range_and_keep_vanished_clips(self):
        rows = [{"key": "DG001-aaaaaaaa", "timeline": "素材A", "use_count": 0, "used_by": [], "grid": "candidates/x.jpg"},
                {"key": "DG001-aaaaaaaa", "timeline": "素材A 副本", "use_count": 0, "used_by": []},
                {"key": "DG002-bbbbbbbb", "timeline": "素材A", "use_count": 1, "used_by": ["Lab-1"]},
                {"key": "DG003-cccccccc", "timeline": "素材A", "use_count": 2, "used_by": ["Lab-1", "Lab-2"]}]
        fresh = [{"key": "DG001-aaaaaaaa", "use_count": 1, "used_by": ["Lab-3"]},
                 {"key": "DG002-bbbbbbbb", "use_count": 1, "used_by": ["Lab-1"]}]
        updated, changed = refreshed_usage(rows, fresh)
        # Both listings of DG001 change; DG002 is unchanged; DG003, trimmed away since, keeps its count.
        self.assertEqual(changed, 2)
        self.assertEqual([(row["use_count"], row["used_by"]) for row in updated],
                         [(1, ["Lab-3"]), (1, ["Lab-3"]), (1, ["Lab-1"]), (2, ["Lab-1", "Lab-2"])])
        self.assertEqual(updated[0]["grid"], "candidates/x.jpg")
        self.assertEqual(rows[0]["use_count"], 0)

    def test_without_counting_nothing_changes(self):
        resolve, *_ = project(2)
        listing = resolve_worker.candidates(resolve, {"project": "202H"})
        self.assertEqual(listing["used"], [])
        self.assertTrue(all(item["used_by"] == [] for item in build_candidates(listing)))


if __name__ == "__main__":
    unittest.main()
