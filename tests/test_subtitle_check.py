"""Subtitle check: wording comparison, the worker's replace-in-place steps and the workbench flow, all with fakes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import resolve_worker as worker
from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge
from subtitle_check import compare, summary
from test_production import Bench, FakeEleven
from test_production_resolve import BuildTests, FakeRunner


def row(start, end, text):
    return {"start": start, "end": end, "text": text}


class CompareTests(unittest.TestCase):
    def test_wrong_brand_is_fixed_and_punctuation_is_left_alone(self):
        plan = compare([row(0, 40, "On the Sharpeball 202H,"), row(40, 80, "adjust the stages. Diamond")],
                       ["On the SHARPAL 202H, adjust the stages: diamond"])
        self.assertEqual(plan["changes"], [{"start": 0, "end": 40, "before": "On the Sharpeball 202H,", "after": "On the SHARPAL 202H,"}])
        self.assertEqual(plan["review"], [])

    def test_matching_subtitles_need_nothing(self):
        plan = compare([row(0, 40, "Full 202H kit's"), row(40, 80, "below.")], ["Full 202H kit's below."])
        self.assertEqual((plan["changes"], plan["review"], plan["similarity"]), ([], [], 1.0))

    def test_written_form_of_a_product_code_is_restored_but_sentence_case_is_not(self):
        plan = compare([row(0, 40, "Get the sharpal 202h kit."), row(40, 80, "Below it goes")], ["get the SHARPAL 202H kit. below it goes"])
        self.assertEqual([item["after"] for item in plan["changes"]], ["Get the SHARPAL 202H kit."])

    def test_missing_word_inside_a_subtitle_is_added(self):
        plan = compare([row(0, 40, "keep the angle steady today"), row(40, 80, "then make a pass")],
                       ["keep the same angle steady today then make a pass"])
        self.assertEqual([item["after"] for item in plan["changes"]], ["keep the same angle steady today"])

    def test_words_the_script_lacks_are_kept_because_they_may_be_on_screen_text(self):
        plan = compare([row(0, 40, "Looks like overkill."), row(40, 80, "keep the angle steady today and make a pass with the guide")],
                       ["keep the angle steady today and make a pass with the guide"])
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["review"][0]["wrote"], "Looks like overkill")

    def test_word_missing_between_subtitles_goes_to_review(self):
        plan = compare([row(0, 40, "keep the angle"), row(50, 80, "steady for every pass on the stone")],
                       ["keep the angle very steady for every pass on the stone"])
        self.assertEqual(plan["changes"], [])
        self.assertEqual([item["expected"] for item in plan["review"]], ["very"])

    def test_missing_opening_is_not_reported(self):
        plan = compare([row(110, 134, "You want that edge"), row(134, 179, "cutting cleanly today"), row(179, 209, "with every single stroke")],
                       ["Still leaving ink here? You want that edge cutting cleanly today with every single stroke"])
        self.assertEqual((plan["changes"], plan["review"], plan["different"]), ([], [], False))

    def test_sentence_missing_later_on_still_goes_to_review(self):
        plan = compare([row(0, 40, "You want that edge"), row(40, 80, "cutting cleanly today"), row(80, 120, "with every single stroke")],
                       ["You want that edge. Still leaving ink here? cutting cleanly today with every single stroke"])
        self.assertEqual([item["expected"] for item in plan["review"]], ["Still leaving ink here"])

    def test_wrong_word_after_a_missing_opening_is_still_fixed(self):
        plan = compare([row(110, 134, "On the Sharpeball 202H"), row(134, 179, "adjust and tighten the"), row(179, 209, "angle setting today")],
                       ["Still leaving ink? On the SHARPAL 202H adjust and tighten the angle setting today"])
        self.assertEqual(plan["changes"][0]["after"], "On the SHARPAL 202H")
        self.assertEqual(plan["review"], [])

    def test_difference_across_two_subtitles_goes_to_review(self):
        plan = compare([row(0, 40, "use the water"), row(40, 80, "stone every day and keep going")], ["use the whetstone every day and keep going"])
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["review"][0]["wrote"], "water stone")

    def test_long_difference_goes_to_review_as_a_possible_cut(self):
        text = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty"
        plan = compare([row(0, 40, text.replace("five six seven eight", "").replace("  ", " "))], [text])
        self.assertEqual(plan["changes"], [])
        self.assertEqual(plan["review"][0]["expected"], "five six seven eight")

    def test_subtitles_of_another_script_change_nothing(self):
        plan = compare([row(0, 40, "something else entirely was written here")], ["keep sharpening by hand with less angle guesswork"])
        self.assertTrue(plan["different"])
        self.assertEqual(plan["changes"], [])

    def test_three_lines_are_flagged_whichever_break_davinci_used(self):
        plan = compare([row(0, 40, "keep sharpening\nby hand")], ["keep sharpening by hand"])
        self.assertEqual(plan["review"][0]["expected"], "超过 2 行")
        self.assertEqual(compare([row(0, 40, "keep sharpening by hand")], ["keep sharpening by hand"])["review"], [])

    def test_summary_says_what_happened(self):
        self.assertEqual(summary({"status": "fixed", "changes": [{}], "review": []}), "字幕已自动改 1 处")
        self.assertEqual(summary({"status": "needs_review", "changes": [{}], "review": [{}, {}]}), "字幕已自动改 1 处，2 处待你确认")
        self.assertEqual(summary({"status": "clean", "changes": [], "review": []}), "字幕用词和配音稿一致")
        self.assertIn("还没有字幕", summary({"status": "no_subtitles"}))


class Subtitle:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetName(self):
        return self.text


class SubtitleTimeline:
    def __init__(self, items, move=True, fail_delete=False, stuck_after_probe=False):
        self.items, self.fail_delete, self.stuck_after_probe, self.moves = items, fail_delete, stuck_after_probe, 0
        self.timecode, self.locked = "00:00:01:00", False
        if move:
            self.MoveItems = self._move

    def GetName(self):
        return "Lab-1"

    def GetUniqueId(self):
        return "TL-1"

    def GetStartFrame(self):
        return 0

    def GetEndFrame(self):
        return max(item.end for item in self.items)

    def GetSetting(self, key):
        return "29.97"

    def GetTrackCount(self, kind):
        return 1

    def GetIsTrackEnabled(self, kind, index):
        return True

    def GetIsTrackLocked(self, kind, index):
        return self.locked

    def GetItemListInTrack(self, kind, index):
        return sorted(self.items, key=lambda item: item.start)

    def GetCurrentTimecode(self):
        return self.timecode

    def SetCurrentTimecode(self, value):
        self.timecode = value
        return True

    def DeleteClips(self, items, ripple):
        if ripple or self.fail_delete:
            return False
        self.items = [item for item in self.items if item not in items]
        return True

    def _move(self, items, position, linked):
        self.moves += 1
        if linked or (self.stuck_after_probe and self.moves > 1):
            return False
        length = items[0].end - items[0].start
        items[0].start, items[0].end = position["startFrame"], position["startFrame"] + length
        return True


class SubtitlePool:
    def __init__(self, project, length=47):
        self.project, self.length, self.current, self.imported, self.deleted = project, length, "shelf", [], []

    def GetCurrentFolder(self):
        return self.current

    def SetCurrentFolder(self, value):
        self.current = value
        return True

    def GetRootFolder(self):
        return self

    def GetSubFolderList(self):
        return [self]

    def GetName(self):
        return "音频字幕"

    def ImportMedia(self, paths):
        self.imported.append(Path(paths[0]).read_text(encoding="utf-8"))
        return [paths[0]]

    def AppendToTimeline(self, infos):
        self.appended_to = self.project.current
        text = self.imported[-1].split("\n")[2]
        timeline = self.project.target
        start = timeline.GetEndFrame() + 254
        item = Subtitle(start, start + self.length, text)
        timeline.items.append(item)
        return [item]

    def DeleteClips(self, clips):
        self.deleted.extend(clips)
        return True


class SubtitleProject:
    def __init__(self, timeline, other):
        self.target, self.current, self.pool = timeline, other, None
        self.other = other

    def GetName(self):
        return "202H"

    def GetMediaPool(self):
        return self.pool

    def GetTimelineCount(self):
        return 1

    def GetTimelineByIndex(self, index):
        return self.target

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetCurrentTimeline(self):
        return self.current


class SubtitleResolve:
    def __init__(self, project):
        self.project, self.saved = project, 0

    def GetProjectManager(self):
        return self

    def GetCurrentProject(self):
        return self.project

    def SaveProject(self):
        self.saved += 1
        return True

    def GetVersionString(self):
        return "21.1.2.10"


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="subtitle-worker-")
        self.addCleanup(temp.cleanup)
        self.folder = temp.name

    def world(self, length=47, **options):
        timeline = SubtitleTimeline([Subtitle(211, 238, "every stroke."), Subtitle(253, 300, "On the Sharpeball 202H,"), Subtitle(310, 346, "adjust the")], **options)
        other = SubtitleTimeline([Subtitle(0, 10, "other")])
        project = SubtitleProject(timeline, other)
        project.pool = SubtitlePool(project, length)
        return SubtitleResolve(project), project, timeline, other

    def request(self, **values):
        return {"project": "202H", "unique_id": "TL-1", "name": "Lab-1", "folder": self.folder, "audio_bin": "音频字幕",
                "changes": [{"start": 253, "end": 300, "before": "On the Sharpeball 202H,", "after": "On the SHARPAL 202H,"}], **values}

    def test_replace_puts_the_new_subtitle_exactly_where_the_old_one_was(self):
        resolve, project, timeline, other = self.world()
        result = worker.replace_subtitles(resolve, self.request())
        self.assertEqual(result["error"], "")
        self.assertTrue(result["saved"])
        self.assertEqual(resolve.saved, 1)
        self.assertEqual([(item["start"], item["end"], item["text"]) for item in result["subtitles"]],
                         [(211, 238, "every stroke."), (253, 300, "On the SHARPAL 202H,"), (310, 346, "adjust the")])
        self.assertIs(project.pool.appended_to, timeline)  # Appending goes to the current timeline.
        self.assertIn("00:00:08,442 --> 00:00:10,010\nOn the SHARPAL 202H,", project.pool.imported[0])

    def test_users_timeline_playhead_and_media_folder_come_back(self):
        resolve, project, timeline, other = self.world()
        worker.replace_subtitles(resolve, self.request())
        self.assertIs(project.current, other)
        self.assertEqual(other.timecode, "00:00:01:00")
        self.assertEqual(project.pool.current, "shelf")

    def test_old_version_without_moveitems_changes_nothing(self):
        resolve, project, timeline, other = self.world(move=False)
        self.assertFalse(worker.read_subtitles(resolve, self.request())["can_replace"])
        with self.assertRaisesRegex(worker.WorkerError, "21.1.2"):
            worker.replace_subtitles(resolve, self.request())
        self.assertEqual(project.pool.imported, [])
        self.assertEqual(len(timeline.items), 3)

    def test_wrong_length_keeps_the_original_and_removes_the_new_one(self):
        resolve, project, timeline, other = self.world(length=46)
        result = worker.replace_subtitles(resolve, self.request())
        self.assertIn("时长", result["error"])
        self.assertEqual(result["replaced"], [])
        self.assertFalse(result["saved"])
        self.assertEqual([item["text"] for item in result["subtitles"]], ["every stroke.", "On the Sharpeball 202H,", "adjust the"])
        self.assertEqual(len(project.pool.deleted), 1)

    def test_changed_subtitle_is_not_touched(self):
        resolve, project, timeline, other = self.world()
        result = worker.replace_subtitles(resolve, self.request(changes=[{"start": 253, "end": 300, "before": "Something else", "after": "New"}]))
        self.assertIn("已经变了", result["error"])
        self.assertEqual(project.pool.imported, [])

    def test_failed_final_move_says_where_the_new_subtitle_is(self):
        resolve, project, timeline, other = self.world(stuck_after_probe=True)
        result = worker.replace_subtitles(resolve, self.request())
        self.assertIn("手动移回", result["error"])
        self.assertIs(project.current, other)

    def test_switched_off_track_still_counts_as_having_subtitles(self):
        resolve, project, timeline, other = self.world()
        timeline.GetIsTrackEnabled = lambda kind, index: False
        self.assertEqual(len(worker.read_subtitles(resolve, self.request())["subtitles"]), 3)

    def test_locked_track_is_refused(self):
        resolve, project, timeline, other = self.world()
        timeline.locked = True
        with self.assertRaisesRegex(worker.WorkerError, "锁定"):
            worker.replace_subtitles(resolve, self.request())


class SubtitleRunner(FakeRunner):
    def __init__(self, subtitles, can_replace=True):
        super().__init__()
        self.subtitles, self.can_replace = subtitles, can_replace

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] == "read_subtitles":
            self.calls.append(request)
            body = {"ok": True, "version": "21.1.2.10", "can_replace": self.can_replace, "subtitles": self.subtitles}
        elif request["command"] == "replace_subtitles":
            self.calls.append(request)
            after = {change["start"]: change["after"] for change in request["changes"]}
            self.subtitles = [{**item, "text": after.get(item["start"], item["text"])} for item in self.subtitles]
            body = {"ok": True, "replaced": request["changes"], "error": "", "saved": True, "subtitles": self.subtitles}
        else:
            return super().__call__(args, input, **options)
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class FlowTests(unittest.TestCase):
    RIGHT = [row(0, 60, "One kit, replacing this jig."), row(60, 120, "Out goes the jig."), row(120, 180, "Full 202H kit's below.")]

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="subtitle-flow-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())

    def timeline(self, subtitles, **options):
        self.runner = SubtitleRunner(subtitles, **options)
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.mutate("settings", settings={"creators": ["Zack", "Lab"]})
        job, _ = BuildTests.voiced_job(self)
        timeline = self.bench.build_timeline(job["id"], {"request_id": uuid4().hex, "creator": "Lab"})["timeline"]
        return job, timeline

    def mutate(self, kind, **values):
        return self.bench.production.mutate(kind, {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values})

    def check(self, job, timeline, request_id=None):
        return self.bench.check_timeline_subtitles(job["id"], {"timeline_uid": timeline["unique_id"], "request_id": request_id or uuid4().hex})

    def commands(self):
        return [call["command"] for call in self.runner.calls]

    def test_wrong_word_is_fixed_without_asking_and_recorded_on_the_timeline(self):
        job, timeline = self.timeline([row(0, 60, "One kit, replacing this jigg."), *self.RIGHT[1:]])
        result = self.check(job, timeline)
        check = result["timeline"]["subtitle_check"]
        self.assertEqual((check["status"], check["message"]), ("fixed", "字幕已自动改 1 处"))
        self.assertEqual(check["changes"][0]["after"], "One kit, replacing this jig.")
        self.assertEqual(self.commands()[-2:], ["read_subtitles", "replace_subtitles"])
        self.assertEqual(self.runner.calls[-1]["audio_bin"], "音频字幕")
        self.assertTrue(self.runner.calls[-1]["folder"].endswith(job["id"]))
        stored = self.bench.production.job(job["id"])
        self.assertEqual(stored["timelines"][0]["subtitle_check"]["status"], "fixed")
        self.assertIn("自动修改 1 处", stored["history"][-1]["note"])

    def test_clean_subtitles_touch_nothing(self):
        job, timeline = self.timeline(list(self.RIGHT))
        check = self.check(job, timeline)["timeline"]["subtitle_check"]
        self.assertEqual(check["status"], "clean")
        self.assertNotIn("replace_subtitles", self.commands())

    def test_no_subtitles_is_reported(self):
        job, timeline = self.timeline([])
        check = self.check(job, timeline)["timeline"]["subtitle_check"]
        self.assertEqual((check["status"], check["message"]), ("no_subtitles", "这条时间线还没有字幕"))

    def test_old_resolve_reports_the_error_without_trying(self):
        job, timeline = self.timeline([row(0, 60, "One kit, replacing this jigg."), *self.RIGHT[1:]], can_replace=False)
        check = self.check(job, timeline)["timeline"]["subtitle_check"]
        self.assertEqual(check["status"], "blocked")
        self.assertNotIn("replace_subtitles", self.commands())

    def test_subtitles_of_another_script_are_left_alone(self):
        job, timeline = self.timeline([row(0, 60, "Completely different words here today friends")])
        self.assertEqual(self.check(job, timeline)["timeline"]["subtitle_check"]["status"], "different")
        self.assertNotIn("replace_subtitles", self.commands())

    def test_busy_resolve_is_refused(self):
        job, timeline = self.timeline(list(self.RIGHT))
        self.bench.resolve.lock.acquire()
        self.addCleanup(self.bench.resolve.lock.release)
        with self.assertRaisesRegex(VoiceError, "另一项"):
            self.check(job, timeline)

    def test_same_request_is_not_run_twice(self):
        job, timeline = self.timeline([row(0, 60, "One kit, replacing this jigg."), *self.RIGHT[1:]])
        key = uuid4().hex
        first = self.check(job, timeline, key)
        self.assertEqual(self.check(job, timeline, key)["timeline"]["subtitle_check"], first["timeline"]["subtitle_check"])
        self.assertEqual(self.commands().count("replace_subtitles"), 1)

    def test_archived_timeline_is_refused(self):
        job, timeline = self.timeline(list(self.RIGHT))
        self.mutate("timeline_archive", job_id=job["id"], timeline_uid=timeline["unique_id"], archived=True)
        with self.assertRaisesRegex(VoiceError, "已归档"):
            self.check(job, timeline)


if __name__ == "__main__":
    unittest.main()
