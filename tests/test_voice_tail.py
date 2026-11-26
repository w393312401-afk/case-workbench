"""Picture must last until the voiceover's last partial frame: worker fix-up, delivery wording and coverage checks."""
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import resolve_worker as worker
from production_validation import engineering_summary
from workbench_editing import voice_tail_handoff, voice_tail_problem


class Clip:
    def __init__(self, name, start, end, *, left=0, right=0, enabled=True):
        self.name, self.start, self.end, self.left, self.right, self.enabled = name, start, end, left, right, enabled

    def GetName(self):
        return self.name

    def GetClipEnabled(self):
        return self.enabled

    def GetStart(self, subframe=False):
        return self.start if subframe else int(self.start)

    def GetEnd(self, subframe=False):
        return self.end if subframe else int(self.end)  # DaVinci drops the partial frame unless asked

    def GetLeftOffset(self, subframe=False):
        return self.left

    def GetRightOffset(self, subframe=False):
        return self.right

    def SetRightOffset(self, offset, subframe=False, linked=True):
        self.end += self.right - offset
        self.right = offset
        return True

    def SetLeftOffset(self, offset, subframe=False, linked=True):
        self.start -= self.left - offset
        self.left = offset
        return True


class Timeline:
    def __init__(self, video, voice, names=None):
        self.video, self.voice, self.names = video, voice, names or {}
        self.moves = []

    def GetStartFrame(self):
        return 0

    def GetTrackCount(self, kind):
        return len(self.video) if kind == "video" else 2

    def GetTrackName(self, kind, index):
        return self.names.get(index, "视频 %d" % index) if kind == "video" else {1: "音频 1", 2: "配音"}[index]

    def GetIsTrackEnabled(self, kind, index):
        return True

    def GetItemListInTrack(self, kind, index):
        return self.video[index - 1] if kind == "video" else ([] if index == 1 else self.voice)

    def MoveItems(self, items, position, linked=True):
        self.moves.append(position)
        for item in items:
            length = item.end - item.start
            item.start = position["startFrame"]
            item.end = item.start + length
        return True


class Project:
    saved = 0

    def GetName(self):
        return "Zack-202H"


def run(timeline):
    project = Project()
    resolve = SimpleNamespace(GetProjectManager=lambda: SimpleNamespace(SaveProject=lambda: True))
    with patch.object(worker, "request_project", return_value=project), patch.object(worker, "find_timeline", return_value=timeline):
        return worker.cover_voice_tail(resolve, {"unique_id": "tl", "name": "x"})


class WorkerTests(unittest.TestCase):
    def test_closing_shot_is_lengthened_to_the_voiceovers_partial_last_frame(self):
        closing = Clip("cta.mov", 1063, 1093, right=40)
        timeline = Timeline([[Clip("a.mov", 0, 1063), closing]], [Clip("配音", 0, 1093.31)])
        result = run(timeline)
        self.assertEqual((result["status"], result["target"], result["picture_end"]), ("extended", 1094, 1093))
        self.assertEqual((closing.start, closing.end), (1063, 1094))
        self.assertEqual(result["extended"][0]["how"], "tail")
        self.assertTrue(result["saved"])

    def test_closing_shot_without_tail_footage_is_moved_and_opened_at_its_head(self):
        closing = Clip("cta.mov", 1063, 1093, left=200, right=0)
        timeline = Timeline([[Clip("a.mov", 0, 1063), closing]], [Clip("配音", 0, 1093.31)])
        result = run(timeline)
        self.assertEqual(result["status"], "extended")
        self.assertEqual((closing.start, closing.end), (1063, 1094))
        self.assertEqual(result["extended"][0]["how"], "head")

    def test_picture_already_past_the_voiceover_is_left_alone(self):
        closing = Clip("cta.mov", 1063, 1094, right=40)
        result = run(Timeline([[closing]], [Clip("配音", 0, 1093.31)]))
        self.assertEqual(result["status"], "covered")
        self.assertEqual(closing.end, 1094)

    def test_whole_frame_voiceover_needs_nothing(self):
        result = run(Timeline([[Clip("cta.mov", 0, 900, right=5)]], [Clip("配音", 0, 900.0)]))
        self.assertEqual(result["status"], "covered")

    def test_lettering_tracks_do_not_count_as_picture(self):
        hook = Clip("hook", 1000, 1094)
        closing = Clip("cta.mov", 1063, 1093, right=40)
        result = run(Timeline([[closing], [hook]], [Clip("配音", 0, 1093.31)], names={2: worker.HOOK_TRACK}))
        self.assertEqual((result["status"], closing.end, hook.end), ("extended", 1094, 1094))

    def test_large_shortfall_is_reported_not_stretched(self):
        closing = Clip("cta.mov", 1000, 1080, right=400)
        result = run(Timeline([[closing]], [Clip("配音", 0, 1093.31)]))
        self.assertEqual(result["status"], "too_short")
        self.assertEqual(closing.end, 1080)

    def test_no_footage_on_either_side_is_reported(self):
        closing = Clip("cta.mov", 1063, 1093)
        result = run(Timeline([[closing]], [Clip("配音", 0, 1093.31)]))
        self.assertEqual((result["status"], result["clip"]), ("no_footage", "cta.mov"))
        self.assertEqual(closing.end, 1093)

    def test_unextendable_track_leaves_every_closing_shot_untouched(self):
        first = Clip("first", 80, 100, right=8)
        second = Clip("second", 80, 100)
        result = run(Timeline([[first], [second]], [Clip("配音", 0, 101.2)]))
        self.assertEqual(result["status"], "no_footage")
        self.assertEqual((first.end, second.end, first.right), (100, 100, 8))

    def test_write_failure_rolls_back_changes_already_made_to_other_tracks(self):
        first = Clip("first", 80, 100, left=8)
        second = Clip("second", 80, 100, right=8)
        with patch.object(second, "SetRightOffset", return_value=False):
            result = run(Timeline([[first], [second]], [Clip("配音", 0, 101.2)]))
        self.assertEqual((result["status"], result["extended"], result["rollback_failed"]), ("failed", [], []))
        self.assertEqual((first.start, first.end, first.left, second.end), (80, 100, 8, 100))


class DeliveryWordingTests(unittest.TestCase):
    def test_problems_are_spelled_out_for_the_user(self):
        self.assertIsNone(voice_tail_problem({"status": "covered"}))
        self.assertIsNone(voice_tail_problem({"status": "extended", "saved": True}))
        self.assertIn("保存", voice_tail_problem({"status": "extended", "saved": False}))
        text = voice_tail_problem({"status": "no_footage", "clip": "cta.mov", "target": 1094, "picture_end": 1093})
        self.assertIn("cta.mov", text)
        self.assertIn("1 帧", text)
        self.assertIn("未能确认", voice_tail_problem({"status": "unavailable", "error": "达芬奇没开"}))

    def test_handoff_names_the_frame_picture_must_reach(self):
        line = voice_tail_handoff({"duration": 36.48}, {"frames": 1093, "lead_frames": 0}, "29.97")
        self.assertIn("1094", line)
        self.assertEqual(voice_tail_handoff({"duration": 30.03}, {"frames": 900, "lead_frames": 0}, "29.97"), "")


class CoverageTests(unittest.TestCase):
    def inspect(self, picture_end):
        voice = {"start": 0, "end": 1093, "end_exact": 1093.31}
        return {"name": "t", "start_frame": 0, "end_frame": 1093, "fps": "29.97", "markers": [],
                "tracks": {"video": [{"index": 1, "items": [{"start": 0, "end": picture_end}]}],
                           "audio": [{"index": 2, "name": "配音", "items": [voice]}]}}

    def test_partial_last_frame_counts_as_uncovered_picture(self):
        summary = engineering_summary(self.inspect(1093), {"id": "PRD-1", "lines": []}, {"frames": 1093})
        self.assertEqual(summary["uncovered"], [{"start": 1093, "end": 1094, "seconds": 36.47}])
        self.assertEqual((summary["voice_frames"], summary["voice_changed"]), (1093, False))

    def test_picture_reaching_the_next_frame_covers_it(self):
        summary = engineering_summary(self.inspect(1094), {"id": "PRD-1", "lines": []}, {"frames": 1093})
        self.assertEqual(summary["uncovered"], [])


if __name__ == "__main__":
    unittest.main()
