"""Opening hook as Text+: the worker's placing steps and what other checks see, and the workbench flow, all with fakes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import resolve_worker as worker
from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven
from test_production_resolve import BuildTests, FakeRunner
from variant_parse import screen_copy, screen_options
from workbench_production import hook_lines


class Tool:
    def __init__(self):
        self.inputs = {}

    def SetInput(self, name, value):
        self.inputs[name] = value

    def GetInput(self, name):
        return self.inputs.get(name)


class Comp:
    def __init__(self, tool):
        self.tool = tool

    def FindTool(self, name):
        return self.tool if name == "Template" else None


class Clip:
    def __init__(self, start, end, name="shot.mov", media=True):
        self.start, self.end, self.name, self.media = start, end, name, media

    GetStart = lambda self: self.start
    GetEnd = lambda self: self.end
    GetName = lambda self: self.name
    GetDuration = lambda self: self.end - self.start
    GetMediaPoolItem = lambda self: self.media or None
    GetFusionCompCount = lambda self: 0


class Title(Clip):
    def __init__(self, start, end):
        super().__init__(start, end, "Text+", False)
        self.tool, self.right = Tool(), 0

    GetFusionCompCount = lambda self: 1
    timeline = None

    def GetFusionCompByIndex(self, index):
        return Comp(self.tool)

    def GetTrackTypeAndIndex(self):
        rows = self.timeline.tracks["video"]
        return ["video", next(number for number, row in enumerate(rows, 1) if self in row)]

    def GetRightOffset(self):
        return self.right

    def SetRightOffset(self, offset, subframe, linked):
        self.end -= offset - self.right
        self.right = offset
        return True


class Timeline:
    """Inserting a title behaves as Resolve does: at the playhead on the lowest unlocked video track, cutting and pushing every unlocked track."""

    def __init__(self, clips=None, *, moves=True, locks_hold=True, inserts=True):
        self.tracks = {"video": [clips if clips is not None else [Clip(0, 60), Clip(60, 120)]], "audio": [[Clip(0, 120, "voice")]], "subtitle": [[]]}
        self.names, self.timecode, self.locked = {1: "V1"}, "00:00:01:00", set()
        self.moves, self.movements, self.locks_hold, self.inserts = moves, [], locks_hold, inserts

    GetUniqueId = lambda self: "TL-1"
    GetName = lambda self: "Lab-1"
    GetStartFrame = lambda self: 0
    GetCurrentTimecode = lambda self: self.timecode

    def GetEndFrame(self):
        return max(item.end for rows in self.tracks.values() for row in rows for item in row)

    def GetSetting(self, name):
        return {"timelineFrameRate": 30, "timelineDropFrameTimecode": "0", "timelineResolutionWidth": 1080, "timelineResolutionHeight": 1920}[name]

    def GetTrackCount(self, kind):
        return len(self.tracks[kind])

    def GetItemListInTrack(self, kind, index):
        return list(self.tracks[kind][index - 1])

    def GetTrackName(self, kind, index):
        return self.names.get(index, "") if kind == "video" else ""

    def SetTrackName(self, kind, index, name):
        self.names[index] = name
        return True

    def AddTrack(self, kind):
        self.tracks[kind].append([])
        return True

    def DeleteTrack(self, kind, index):
        self.tracks[kind].pop(index - 1)
        self.names.pop(index, None)
        return True

    def SetCurrentTimecode(self, value):
        self.timecode = value
        return True

    def GetIsTrackLocked(self, kind, index):
        return (kind, index) in self.locked

    def SetTrackLock(self, kind, index, locked):
        if not self.locks_hold:
            return True
        (self.locked.add if locked else self.locked.discard)((kind, index))
        return True

    def InsertFusionTitleIntoTimeline(self, name):
        if not self.inserts:
            return None
        hours, minutes, seconds, frames = (int(part) for part in self.timecode.split(":"))
        at, length = (hours * 3600 + minutes * 60 + seconds) * 30 + frames, 150
        for kind, rows in self.tracks.items():
            for index, row in enumerate(rows, 1):
                if (kind, index) in self.locked:
                    continue
                for item in list(row):
                    if item.start < at < item.end:  # The insert cuts the clip it lands in.
                        row.append(Clip(at, item.end, item.name))
                        item.end = at
                for item in row:
                    if item.start >= at:
                        item.start, item.end = item.start + length, item.end + length
                row.sort(key=lambda item: item.start)
        target = next(index for index in range(1, len(self.tracks["video"]) + 1) if ("video", index) not in self.locked)
        title = Title(at, at + length)
        title.timeline = self
        self.tracks["video"][target - 1].append(title)
        return title

    def DeleteClips(self, items, ripple):
        for item in items:
            for row in self.tracks["video"]:
                if item in row:
                    row.remove(item)
                    for other in row if ripple else []:
                        if other.start >= item.start:
                            other.start, other.end = other.start - item.GetDuration(), other.end - item.GetDuration()
        return True

    def _move(self, items, position, linked):
        item = items[0]
        self.movements.append(position)
        if not self.moves:
            return False
        for row in self.tracks["video"]:
            if item in row:
                row.remove(item)
        length = item.GetDuration()
        item.start, item.end = position["startFrame"], position["startFrame"] + length
        self.tracks["video"][position["trackIndex"] - 1].append(item)
        return True


class Project:
    def __init__(self, timeline, current=None):
        self.timeline, self.current = timeline, current or timeline

    def GetName(self):
        return "202H"

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True


class Manager:
    def __init__(self):
        self.saves = 0

    def SaveProject(self):
        self.saves += 1
        return True


class Resolve:
    def __init__(self, page="edit"):
        self.page, self.manager = page, Manager()

    def GetProjectManager(self):
        return self.manager

    def GetCurrentPage(self):
        return self.page

    def OpenPage(self, page):
        self.page = page
        return True


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)

    def world(self, timeline=None, current=None, page="edit", **options):
        timeline = timeline or Timeline(**options)
        timeline.MoveItems = timeline._move
        project = Project(timeline, current)
        mock.patch.object(worker, "request_project", lambda resolve, request: project).start()
        mock.patch.object(worker, "find_timeline", lambda project, request: project.timeline).start()
        return Resolve(page), project, timeline

    def add(self, resolve, text="Stop guessing\nCheck the bevel"):
        return worker.add_hook(resolve, {"project": "202H", "unique_id": "TL-1", "text": text})

    def test_hook_goes_on_its_own_track_and_the_cut_stays_where_it_was(self):
        resolve, project, timeline = self.world()
        result = self.add(resolve)
        self.assertEqual(result, {"created": True, "start": 0, "end": 90, "saved": True})
        self.assertEqual(timeline.GetTrackName("video", 2), "屏显")
        self.assertEqual(timeline.movements, [{"trackIndex": 2, "startFrame": 0}])
        self.assertEqual(timeline.locked, set())
        self.assertEqual([(item.start, item.end) for item in timeline.tracks["video"][0]], [(0, 60), (60, 120)])
        title = timeline.tracks["video"][1][0]
        self.assertEqual((title.start, title.end), (0, 90))
        self.assertEqual(title.tool.inputs["StyledText"], "Stop guessing\nCheck the bevel")
        self.assertEqual((title.tool.inputs["Font"], title.tool.inputs["Style"], title.tool.inputs["Size"]), ("TikTok Sans", "Medium", 0.1))
        self.assertEqual((title.tool.inputs["Enabled4"], title.tool.inputs["ElementShape4"], title.tool.inputs["Level4"]), (1, 2, 1))
        self.assertEqual(title.tool.inputs["Center"], {1: 0.5, 2: 0.809})
        self.assertEqual(resolve.manager.saves, 1)

    def test_campaign_card_sits_at_the_end_on_its_own_track(self):
        resolve, _, timeline = self.world()
        self.add(resolve)
        result = worker.add_campaign_card(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Black Friday sale\nThrough Nov 30"})
        self.assertEqual(result, {"created": True, "start": 30, "end": 120, "saved": True})
        self.assertEqual((timeline.GetTrackName("video", 2), timeline.GetTrackName("video", 3)), ("屏显", "活动屏显"))
        self.assertEqual(timeline.movements[-1], {"trackIndex": 3, "startFrame": 30})
        self.assertEqual([(item.start, item.end) for item in timeline.tracks["video"][0]], [(0, 60), (60, 120)])
        self.assertEqual(timeline.GetEndFrame(), 120)
        self.assertEqual(timeline.locked, set())
        # The opening hook is untouched, and a second click rewrites the card in place.
        self.assertEqual((timeline.tracks["video"][1][0].start, timeline.tracks["video"][1][0].end), (0, 90))
        again = worker.add_campaign_card(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Cyber Monday"})
        self.assertEqual((again["created"], timeline.tracks["video"][2][0].tool.inputs["StyledText"]), (False, "Cyber Monday"))

    def test_campaign_card_comes_off_and_leaves_the_rest(self):
        resolve, _, timeline = self.world()
        worker.add_campaign_card(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Black Friday sale"})
        self.assertEqual(worker.remove_campaign_card(resolve, {"project": "202H", "unique_id": "TL-1"}), {"removed": 1, "saved": True})
        self.assertEqual(timeline.tracks["video"][1], [])
        self.assertEqual([(item.start, item.end) for item in timeline.tracks["video"][0]], [(0, 60), (60, 120)])
        self.assertEqual(worker.remove_campaign_card(resolve, {"project": "202H", "unique_id": "TL-1"})["removed"], 0)

    def test_long_line_shrinks_to_stay_inside_the_frame(self):
        resolve, _, timeline = self.world()
        self.add(resolve, "x" * 76)
        self.assertAlmostEqual(timeline.tracks["video"][1][0].tool.inputs["Size"], 3.0 / 76)

    def test_line_that_fits_keeps_the_styles_size(self):
        resolve, _, timeline = self.world()
        self.add(resolve, "x" * 38)
        self.assertAlmostEqual(timeline.tracks["video"][1][0].tool.inputs["Size"], 3.0 / 38)

    def test_a_named_style_is_used_for_a_new_hook(self):
        resolve, _, timeline = self.world()
        custom = {"label": "大字", "seconds": 2, "full_line": 20, "inputs": {"Font": "Impact", "Size": 0.2}}
        with mock.patch.dict(worker.HOOK_STYLES, {"big": custom}):
            worker.add_hook(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Short", "style": "big"})
        title = timeline.tracks["video"][1][0]
        self.assertEqual((title.tool.inputs["Font"], title.tool.inputs["Size"], title.end), ("Impact", 0.2, 60))

    def test_naming_a_style_restyles_an_existing_hook(self):
        resolve, _, timeline = self.world()
        self.add(resolve)
        title = timeline.tracks["video"][1][0]
        with mock.patch.dict(worker.HOOK_STYLES, {"big": {"label": "大字", "seconds": 2, "full_line": 20, "inputs": {"Font": "Impact", "Size": 0.2}}}):
            worker.add_hook(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Short", "style": "big"})
        self.assertEqual((title.tool.inputs["StyledText"], title.tool.inputs["Font"]), ("Short", "Impact"))
        self.assertEqual(timeline.GetTrackCount("video"), 2)

    def test_unknown_style_is_refused_before_touching_resolve(self):
        resolve, _, timeline = self.world()
        with self.assertRaisesRegex(worker.WorkerError, "没有名为「nope」"):
            worker.add_hook(resolve, {"project": "202H", "unique_id": "TL-1", "text": "Short", "style": "nope"})
        self.assertEqual(timeline.GetTrackCount("video"), 1)

    def test_clicking_again_rewrites_the_text_and_keeps_the_users_adjustments(self):
        resolve, _, timeline = self.world()
        self.add(resolve)
        title = timeline.tracks["video"][1][0]
        title.tool.inputs["Size"] = 0.07
        title.end = 70
        result = self.add(resolve, "New words")
        self.assertEqual(result, {"created": False, "start": 0, "end": 70, "saved": True})
        self.assertEqual(timeline.GetTrackCount("video"), 2)
        self.assertEqual((title.tool.inputs["StyledText"], title.tool.inputs["Size"]), ("New words", 0.07))

    def test_an_emptied_hook_track_is_used_again(self):
        resolve, _, timeline = self.world()
        self.add(resolve)
        timeline.tracks["video"][1].clear()
        self.assertTrue(self.add(resolve)["created"])
        self.assertEqual(timeline.GetTrackCount("video"), 2)

    def test_users_timeline_playhead_and_page_come_back(self):
        other = Timeline()
        other.timecode = "00:00:05:00"
        resolve, project, timeline = self.world(current=other, page="color")
        self.add(resolve)
        self.assertIs(project.current, other)
        self.assertEqual((other.timecode, resolve.page), ("00:00:05:00", "color"))

    def test_same_timeline_gets_its_playhead_back(self):
        resolve, _, timeline = self.world()
        self.add(resolve)
        self.assertEqual(timeline.timecode, "00:00:01:00")

    def test_playhead_inside_a_clip_cuts_nothing(self):
        # The playhead in setUp is at frame 30, inside the first clip and the voiceover.
        resolve, _, timeline = self.world()
        self.add(resolve)
        self.assertEqual([(item.start, item.end) for item in timeline.tracks["video"][0]], [(0, 60), (60, 120)])
        self.assertEqual([(item.start, item.end) for item in timeline.tracks["audio"][0]], [(0, 120)])

    def test_locks_go_back_as_they_were(self):
        resolve, _, timeline = self.world()
        timeline.locked = {("audio", 1)}
        self.add(resolve)
        self.assertEqual(timeline.locked, {("audio", 1)})

    def test_locks_that_do_not_hold_stop_before_anything_is_inserted(self):
        resolve, _, timeline = self.world(locks_hold=False)
        timeline.InsertFusionTitleIntoTimeline = None
        with self.assertRaisesRegex(worker.WorkerError, "锁住"):
            self.add(resolve)
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(resolve.manager.saves, 0)

    def test_title_on_the_wrong_track_is_taken_back_out(self):
        resolve, _, timeline = self.world()
        real = timeline.InsertFusionTitleIntoTimeline
        def strayed(name):
            title = real(name)
            timeline.tracks["video"][1].remove(title)
            timeline.tracks["video"][0].append(title)
            return title
        timeline.InsertFusionTitleIntoTimeline = strayed
        with self.assertRaisesRegex(worker.WorkerError, "没有落在屏显轨"):
            self.add(resolve)
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(len(timeline.tracks["video"][0]), 2)
        self.assertEqual(timeline.locked, set())
        self.assertEqual(resolve.manager.saves, 0)

    def test_failed_move_removes_the_title_and_the_new_track_and_unlocks(self):
        resolve, _, timeline = self.world(moves=False)
        with self.assertRaisesRegex(worker.WorkerError, "移到开头"):
            self.add(resolve)
        self.assertEqual(timeline.GetTrackCount("video"), 1)
        self.assertEqual(len(timeline.tracks["video"][0]), 2)
        self.assertEqual(timeline.locked, set())

    def test_failed_insert_on_an_existing_empty_track_keeps_the_track(self):
        resolve, _, timeline = self.world(inserts=False)
        timeline.tracks["video"].append([])
        timeline.names[2] = "屏显"
        with self.assertRaisesRegex(worker.WorkerError, "插入 Text\\+"):
            self.add(resolve)
        self.assertEqual(timeline.GetTrackCount("video"), 2)
        self.assertEqual(timeline.locked, set())

    def test_other_clips_on_the_hook_track_are_not_touched(self):
        resolve, _, timeline = self.world()
        timeline.tracks["video"].append([Clip(0, 30)])
        timeline.names[2] = "屏显"
        with self.assertRaisesRegex(worker.WorkerError, "别的片段"):
            self.add(resolve)
        self.assertEqual(resolve.manager.saves, 0)

    def test_old_resolve_is_refused(self):
        resolve, _, timeline = self.world()
        timeline.MoveItems = None
        with self.assertRaisesRegex(worker.WorkerError, "21.1.2"):
            self.add(resolve)

    def test_titles_are_not_footage(self):
        self.assertTrue(worker.is_title(Title(0, 90)))
        self.assertFalse(worker.is_title(Clip(0, 90)))
        self.assertFalse(worker.is_title(Clip(0, 90, media=False)))  # An unreadable clip still counts as unknown.

    def test_inspect_does_not_count_the_hook_as_a_shot(self):
        timeline = Timeline()
        timeline.GetMarkers = lambda: {}
        timeline.tracks["video"].append([Title(0, 90)])
        timeline.names[2] = "屏显"
        resolve, _, _ = self.world(timeline)
        with mock.patch.object(worker, "located", lambda project: {}, create=True):
            result = worker.inspect(resolve, {"project": "202H", "unique_id": "TL-1"})
        self.assertEqual([track["index"] for track in result["tracks"]["video"]], [1])

    def test_inspect_keeps_real_clips_placed_on_that_track(self):
        timeline = Timeline()
        timeline.GetMarkers = lambda: {}
        timeline.tracks["video"].append([Clip(0, 90)])
        timeline.names[2] = "屏显"
        resolve, _, _ = self.world(timeline)
        with mock.patch.object(worker, "located", lambda project: {}, create=True):
            result = worker.inspect(resolve, {"project": "202H", "unique_id": "TL-1"})
        self.assertEqual([track["index"] for track in result["tracks"]["video"]], [1, 2])


class ScreenOptionsTests(unittest.TestCase):
    DELIVERY = """开头画面：手推磨板

开头屏显：`Still not sharp?`（还是不快？）

屏显搭配：同写。前 3 秒配音念到 “after a pass”

屏显备选：点破｜`Still not sharp? / Check your angle`（还是不快？/ 看看角度）

屏显备选：好处｜`Your hands do the work / The guide holds the angle`（手上的活还是你的 / 角度交给导杆）

**口播（配音用）**

```
屏显备选：不是｜`Not A Hook`
Still leaving ink on the edge after a pass?
```
"""

    def test_recommended_comes_first_with_its_alternatives(self):
        self.assertEqual(screen_options(self.DELIVERY), [
            {"label": "同写", "en": "Still not sharp?", "zh": "还是不快？"},
            {"label": "点破", "en": "Still not sharp? / Check your angle", "zh": "还是不快？/ 看看角度"},
            {"label": "好处", "en": "Your hands do the work / The guide holds the angle", "zh": "手上的活还是你的 / 角度交给导杆"}])

    def test_the_recommended_hook_is_still_the_one_screen_text(self):
        self.assertEqual(screen_copy(self.DELIVERY), ("Still not sharp?", "还是不快？"))

    def test_older_script_has_one_option(self):
        self.assertEqual(screen_options("开头屏显：`One / Two`（一 / 二）"), [{"label": "", "en": "One / Two", "zh": "一 / 二"}])

    def test_no_hook_no_options(self):
        self.assertEqual(screen_options("开头屏显：无开头屏显\n屏显备选：点破｜`Spare`"), [])


class HookLinesTests(unittest.TestCase):
    def test_spaced_slash_breaks_the_line(self):
        self.assertEqual(hook_lines("Stop guessing / Check the bevel"), ["Stop guessing", "Check the bevel"])

    def test_dangling_slash_is_dropped(self):
        self.assertEqual(hook_lines("Dull ≠ ruined /"), ["Dull ≠ ruined"])

    def test_slash_inside_a_word_stays(self):
        self.assertEqual(hook_lines("50/50 bevel"), ["50/50 bevel"])

    def test_no_hook(self):
        self.assertEqual(hook_lines("无开头屏显：第一句够强"), [])
        self.assertEqual(hook_lines(""), [])


class HookRunner(FakeRunner):
    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] != "add_hook":
            return super().__call__(args, input, **options)
        self.calls.append(request)
        body = {"ok": True, "created": sum(call["command"] == "add_hook" for call in self.calls) == 1, "start": 0, "end": 90, "saved": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(body).encode("utf-8"), b"")


class CampaignRunner(FakeRunner):
    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] not in {"add_campaign_card", "remove_campaign_card"}:
            return super().__call__(args, input, **options)
        self.calls.append(request)
        body = ({"ok": True, "created": True, "start": 30, "end": 120, "saved": True} if request["command"] == "add_campaign_card"
                else {"ok": True, "removed": 1, "saved": True})
        return subprocess.CompletedProcess(args, 0, json.dumps(body).encode("utf-8"), b"")


class CampaignFlowTests(unittest.TestCase):
    """The end card is offered only while a trusted campaign runs; the voice-over never changes."""

    RUNNING = {"name": "Black Friday / Cyber Monday（黑五网一）", "start": "2026-11-11", "end": "2026-11-30", "status": "running"}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="campaign-flow-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.runner = CampaignRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.bench._video_publish_copy = lambda job: None
        FlowTests.mutate(self, "settings", settings={"creators": ["Zack", "Lab"]})
        self.job, _ = BuildTests.voiced_job(self)
        self.line = self.bench.build_timeline(self.job["id"], {"request_id": uuid4().hex, "creator": "Lab"})["timeline"]

    def mutate(self, kind, **values):
        return FlowTests.mutate(self, kind, **values)

    def click(self, action="add", text=""):
        return self.bench.set_timeline_campaign(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex,
                                                                 "action": action, "text": text})

    def calls(self):
        return [call for call in self.runner.calls if call["command"] in {"add_campaign_card", "remove_campaign_card"}]

    def test_no_running_campaign_no_card(self):
        self.bench._campaign_now = lambda: {"running": None, "upcoming": dict(self.RUNNING, status="upcoming")}
        with self.assertRaisesRegex(VoiceError, "没有进行中的活动"):
            self.click()
        self.assertEqual(self.calls(), [])

    def test_card_is_made_from_the_calendar_and_taken_off_later(self):
        self.bench._campaign_now = lambda: {"running": self.RUNNING, "upcoming": None}
        card = self.click()["timeline"]["campaign_overlay"]
        self.assertEqual(self.calls()[0]["text"], "Black Friday & Cyber Monday sale\nEnds Nov 30")
        self.assertEqual((card["status"], card["campaign"]["end"], card["message"]), ("saved", "2026-11-30", "活动屏显已加"))
        spoken = [line["spoken"] for line in self.bench.production.job(self.job["id"])["lines"]]
        self.assertEqual(spoken, [line["spoken"] for line in self.job["lines"]])
        removed = self.click("remove")["timeline"]["campaign_overlay"]
        self.assertEqual((removed["status"], self.runner.calls[-1]["command"]), ("removed", "remove_campaign_card"))
        with self.assertRaisesRegex(VoiceError, "最多两行"):
            self.click(text="a / b / c")


class FlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="hook-flow-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.runner = HookRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.bench._video_publish_copy = lambda job: None
        self.mutate("settings", settings={"creators": ["Zack", "Lab"]})
        self.job, _ = BuildTests.voiced_job(self)
        self.line = self.bench.build_timeline(self.job["id"], {"request_id": uuid4().hex, "creator": "Lab"})["timeline"]

    def mutate(self, kind, **values):
        return self.bench.production.mutate(kind, {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values})

    def set_hook(self, value):
        # The publish panel's 钩子屏显 is what the button uses; the bench has no publish side, so the panel's answer stands in.
        self.bench._video_publish_copy = lambda job: {"screen_en": value}

    def click(self, request_id=None):
        return self.bench.add_timeline_hook(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": request_id or uuid4().hex})

    def hook_calls(self):
        return [call for call in self.runner.calls if call["command"] == "add_hook"]

    def test_adds_the_orders_hook_and_records_it(self):
        self.set_hook("Stop guessing / Check the bevel")
        hook = self.click()["timeline"]["hook_overlay"]
        self.assertEqual((hook["message"], hook["text"], hook["status"]), ("屏显已加", "Stop guessing / Check the bevel", "saved"))
        self.assertEqual((self.hook_calls()[0]["text"], self.hook_calls()[0]["style"]), ("Stop guessing\nCheck the bevel", ""))
        stored = self.bench.production.job(self.job["id"])
        self.assertEqual(stored["timelines"][0]["hook_overlay"]["end"], 90)
        self.assertIn("加屏显：Stop guessing / Check the bevel", stored["history"][-1]["note"])

    def test_second_click_says_updated(self):
        self.set_hook("One line")
        self.click()
        self.assertEqual(self.click()["timeline"]["hook_overlay"]["message"], "屏显已更新")

    def test_repeated_request_does_not_run_twice(self):
        self.set_hook("One line")
        request_id = uuid4().hex
        self.click(request_id)
        self.click(request_id)
        self.assertEqual(len(self.hook_calls()), 1)

    def test_refusals_leave_resolve_alone(self):
        with self.assertRaisesRegex(VoiceError, "还没有开头屏显"):
            self.click()
        self.set_hook("无开头屏显：第一句够强")
        with self.assertRaisesRegex(VoiceError, "还没有开头屏显"):
            self.click()
        self.set_hook("one / two / three / four")
        with self.assertRaisesRegex(VoiceError, "最多三行"):
            self.click()
        self.assertEqual(self.hook_calls(), [])

    def test_chosen_wording_is_used_over_the_recommended_one(self):
        self.set_hook("Recommended / Hook")
        result = self.bench.add_timeline_hook(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "text": "Picked one / Second line"})
        self.assertEqual(self.hook_calls()[0]["text"], "Picked one\nSecond line")
        self.assertEqual(result["timeline"]["hook_overlay"]["text"], "Picked one / Second line")

    def test_own_wording_works_on_an_order_with_no_hook(self):
        self.bench.add_timeline_hook(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "text": "My own words"})
        self.assertEqual(self.hook_calls()[0]["text"], "My own words")

    def test_own_wording_past_three_lines_is_refused(self):
        with self.assertRaisesRegex(VoiceError, "最多三行"):
            self.bench.add_timeline_hook(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "text": "a / b / c / d"})
        self.assertEqual(self.hook_calls(), [])

    def test_unknown_timeline_is_refused(self):
        with self.assertRaises(VoiceError) as caught:
            self.bench.add_timeline_hook(self.job["id"], {"timeline_uid": "nope", "request_id": uuid4().hex})
        self.assertEqual(caught.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
