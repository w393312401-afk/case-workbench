"""Opening comment card: which comment is used, how its words fit, the Resolve steps, and the workbench flow, all with fakes."""
import json
from pathlib import Path
import subprocess
import sys
import shutil
import tempfile
import unittest
from unittest import mock
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

from PIL import Image

import comment_sticker as sticker
from elevenlabs_client import VoiceError
from opportunity_skeletons import brief
import resolve_worker as worker
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven
from test_production_resolve import BuildTests, FakeRunner


class MarkerTests(unittest.TestCase):
    SKELETON = {"opening": [{"pain_zh": "角度靠猜", "quotes": ["r1", "r2"]}, {"pain_zh": "磨不快", "quotes": ["r2", "r3"]}],
                "middle": [], "closing": None, "gaps": [], "notes": []}

    def test_the_writing_request_carries_the_openings_comments_and_reads_back(self):
        request = "写一条稿子\n\n" + brief(self.SKELETON)
        self.assertEqual(sticker.review_ids([{"instruction": request}]), ["r1", "r2", "r3"])

    def test_an_opening_without_comments_adds_nothing(self):
        self.assertNotIn(sticker.MARKER, brief({**self.SKELETON, "opening": [{"pain_zh": "角度靠猜", "quotes": []}]}))

    def test_a_request_with_no_marker_has_no_comment(self):
        self.assertEqual(sticker.review_ids([{"instruction": "普通写稿"}, {}]), [])


class WordingTests(unittest.TestCase):
    def test_handle_wins_and_masked_names_stay(self):
        self.assertEqual(sticker.username({"author_handle": "@zack", "author_name": "Zack B"}), "@zack")
        self.assertEqual(sticker.username({"author_name": "M**o"}), "@M**o")
        self.assertEqual(sticker.username({"author_name": "Jo Smith"}), "Jo Smith")

    def test_short_comment_is_untouched(self):
        self.assertEqual(sticker.comment_lines("Does this really work?"), ["Does this really work?"])

    def test_emoji_the_font_cannot_draw_are_dropped(self):
        self.assertEqual(sticker.comment_lines("Love it \U0001F525\U0001F525 so sharp"), ["Love it so sharp"])

    def test_long_comment_keeps_whole_sentences(self):
        text = "My knives go dull in a week. " * 3 + "x" * 200
        lines = sticker.comment_lines(text)
        self.assertLessEqual(len(lines), sticker.MAX_LINES)
        self.assertTrue(lines[-1].endswith("week..."))

    def test_a_short_first_sentence_does_not_leave_the_card_nearly_empty(self):
        text = "I open the measure app on my phone click level. " + "Stand the bottom edge of the phone on the blade tap zero then set it on top of the stone arm and adjust the angle of the stone until it matches what you want for that knife"
        lines = sticker.comment_lines(text)
        self.assertEqual(len(lines), sticker.MAX_LINES)
        self.assertTrue(lines[-1].endswith("..."))
        self.assertNotIn(" ...", lines[-1])
        self.assertLessEqual(max(map(len, lines)), sticker.LINE_WIDTH)

    def test_a_cut_after_a_full_stop_has_no_extra_dots(self):
        lines = sticker.comment_lines("word " * 26 + "end. " + "word " * 30)
        self.assertFalse(lines[-1].endswith("...."))

    def test_emoji_leaves_the_name_too(self):
        self.assertEqual(sticker.username({"author_name": "Bob\U0001F525"}), "@Bob")

    def test_a_name_or_comment_the_font_cannot_draw_gives_way_to_the_next(self):
        good = {"id": "ok", "author_name": "Ann", "text": "Café “sharp” — $20 ™"}
        self.assertEqual(sticker.pick([{"id": "ka", "author_name": "ლევან მეტრეველი", "text": "Is this liquid water?", "avatar": "ff"}, good])["id"], "ok")
        self.assertEqual(sticker.pick([{"id": "zh", "author_name": "Ann", "text": "好用", "avatar": "ff"}, good])["id"], "ok")
        self.assertEqual(sticker.pick([{"id": "ru", "author_name": "Иван", "text": "Отлично"}])["id"], "ru")
        self.assertIsNone(sticker.pick([{"id": "ka", "author_name": "ლევან", "text": "Water?"}]))

    def test_one_endless_sentence_is_cut_with_an_ellipsis(self):
        lines = sticker.comment_lines("word " * 100)
        self.assertEqual(len(lines), sticker.MAX_LINES)
        self.assertTrue(lines[-1].endswith("..."))
        self.assertLessEqual(max(map(len, lines)), sticker.LINE_WIDTH)

    def test_pick_prefers_a_saved_avatar(self):
        rows = [None, {"author_name": "", "text": "no name"}, {"id": "a", "author_name": "A", "text": "one"},
                {"id": "b", "author_name": "B", "text": "two", "avatar": "ff"}]
        self.assertEqual(sticker.pick(rows)["id"], "b")
        self.assertEqual(sticker.pick(rows[:3])["id"], "a")
        self.assertIsNone(sticker.pick(rows[:2]))

    def test_canvas_is_full_frame_with_the_avatar_in_the_circle(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "face.jpg")
            Image.new("RGB", (100, 60), (200, 10, 10)).save(source)
            with Image.open(sticker.avatar_canvas(source, Path(folder, "a", "c.png"), "@a")) as canvas:
                self.assertEqual(canvas.size, sticker.CANVAS)
                self.assertGreater(canvas.getpixel(sticker.AVATAR_CENTER)[0], 150)
                self.assertEqual(canvas.getpixel((5, 5))[3], 0)

    def test_broken_or_missing_avatar_falls_back_to_the_initial(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder, "bad.jpg")
            source.write_bytes(b"not an image")
            for origin in (source, None):
                with Image.open(sticker.avatar_canvas(origin, Path(folder, "c.png"), "@zed")) as canvas:
                    self.assertEqual(canvas.getpixel((sticker.AVATAR_CENTER[0] - 40, sticker.AVATAR_CENTER[1] - 40)), sticker.INITIAL_FILL)


class Tool:
    def __init__(self):
        self.inputs = {}

    def SetInput(self, name, value):
        self.inputs[name] = value

    def GetInput(self, name):
        return self.inputs.get(name)


class Comp:
    def __init__(self):
        self.tools = {name: Tool() for name in ("CardShape", "MessageTailShape", "Username", "Comment")}
        self.tools["CardShape"].inputs.update(Height=0.1, Center={1: 0.4, 2: 0.7})
        self.tools["Comment"].inputs.update(Size=0.068, Center={1: 0.3, 2: 0.65})
        self.tools["MessageTailShape"].inputs.update(Point1={1: 0.2, 2: 0.6}, Point2={1: 0.2, 2: 0.5}, Point3={1: 0.3, 2: 0.5})

    def FindTool(self, name):
        return self.tools.get(name)


CLIP_COMP = "Composition {\n\tTools = {\n\t\tMediaIn1 = Loader {\n\t\t\tClips = { Clip { ID = \"Clip1\" } },\n\t\t},\n\t\tMediaOut1 = Saver {\n\t\t},\n\t},\n}\n"


class Item:
    def __init__(self, timeline, start, duration=150, track=2):
        self.timeline, self.start, self.duration, self.track, self.right, self.comp, self.imported = timeline, start, duration, track, 1000, None, None

    GetStart = lambda self: self.start
    GetEnd = lambda self: self.start + self.duration
    GetName = lambda self: "avatar.png"
    GetDuration = lambda self: self.duration
    GetRightOffset = lambda self: self.right
    GetTrackTypeAndIndex = lambda self: ["video", self.track]
    GetMediaPoolItem = lambda self: True
    GetFusionCompCount = lambda self: 1 if self.comp else 0
    GetFusionCompByIndex = lambda self, index: self.comp

    def SetRightOffset(self, value, *_):
        self.duration -= value - self.right
        self.right = value
        return True

    def AddFusionComp(self):
        self.comp = Comp()
        return self.comp

    def ExportFusionComp(self, path, index):
        Path(path).write_text(CLIP_COMP, encoding="utf-8")
        return True

    def ImportFusionComp(self, path):
        if self.timeline.refuse_import:
            return None
        self.imported = Path(path).read_text(encoding="utf-8")
        return self.comp


class Shot:
    def __init__(self, start, end):
        self.start, self.end = start, end

    GetStart = lambda self: self.start
    GetEnd = lambda self: self.end
    GetName = lambda self: "shot.mov"
    GetDuration = lambda self: self.end - self.start
    GetFusionCompCount = lambda self: 0


class Subtitle(Shot):
    def __init__(self, start, end, text):
        super().__init__(start, end)
        self.text = text

    GetName = lambda self: self.text


class Timeline:
    def __init__(self):
        self.tracks = {"video": [[Shot(0, 300)]], "audio": [[]], "subtitle": [[Subtitle(0, 45, "I keep buying"), Subtitle(45, 100, "new sharpeners"), Subtitle(100, 200, "and more")]]}
        self.names = {("video", 1): "V1"}
        self.locked, self.refuse_import, self.lock_history = set(), False, []

    GetStartFrame = lambda self: 0
    GetEndFrame = lambda self: 300
    GetSetting = lambda self, name: "30"
    GetUniqueId = lambda self: "TL-1"
    GetName = lambda self: "TL"
    GetCurrentTimecode = lambda self: "00:00:01:00"
    SetCurrentTimecode = lambda self, value: True
    GetTrackCount = lambda self, kind: len(self.tracks[kind])
    GetTrackName = lambda self, kind, index: self.names.get((kind, index), "")
    GetItemListInTrack = lambda self, kind, index: list(self.tracks[kind][index - 1])
    GetIsTrackLocked = lambda self, kind, index: (kind, index) in self.locked
    GetIsTrackEnabled = lambda self, kind, index: True

    def AddTrack(self, kind):
        self.tracks[kind].append([])
        return True

    def SetTrackName(self, kind, index, name):
        self.names[(kind, index)] = name
        return True

    def DeleteTrack(self, kind, index):
        self.tracks[kind].pop(index - 1)
        return True

    def SetTrackLock(self, kind, index, locked):
        (self.locked.add if locked else self.locked.discard)((kind, index))
        self.lock_history.append((kind, index, locked))
        return True

    def DeleteClips(self, items, ripple):
        for track in self.tracks["video"]:
            track[:] = [row for row in track if row not in items]
        return True


class Clip:
    def __init__(self, path):
        self.path = path

    GetClipProperty = lambda self, name: self.path


class Folder:
    def __init__(self, name):
        self.name, self.subs, self.clips = name, [], []

    GetName = lambda self: self.name
    GetSubFolderList = lambda self: self.subs
    GetClipList = lambda self: self.clips


class MediaPool:
    def __init__(self, timeline):
        self.timeline, self.root, self.current, self.imports, self.placed = timeline, Folder("root"), None, 0, False
        self.current = self.root

    GetRootFolder = lambda self: self.root
    GetCurrentFolder = lambda self: self.current

    def SetCurrentFolder(self, folder):
        self.current = folder
        return True

    def AddSubFolder(self, parent, name):
        parent.subs.append(Folder(name))
        return parent.subs[-1]

    def ImportMedia(self, paths):
        self.imports += 1
        self.current.clips.append(Clip(paths[0]))
        return [self.current.clips[-1]]

    def AppendToTimeline(self, infos):
        info = infos[0]
        item = Item(self.timeline, info["recordFrame"], track=info["trackIndex"])
        self.timeline.tracks["video"][info["trackIndex"] - 1].append(item)
        return [item]


class Project:
    def __init__(self, timeline):
        self.timeline, self.pool, self.current = timeline, MediaPool(timeline), None

    GetMediaPool = lambda self: self.pool
    GetCurrentTimeline = lambda self: self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True


class Manager:
    saves = 0

    def SaveProject(self):
        self.saves += 1
        return True


class Resolve:
    def __init__(self):
        self.manager = Manager()

    GetProjectManager = lambda self: self.manager
    GetCurrentPage = lambda self: "edit"
    OpenPage = lambda self, page: True


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="comment-card-")
        self.addCleanup(temp.cleanup)
        self.avatar = str(Path(temp.name, "avatar.png"))
        Path(self.avatar).write_bytes(b"x")
        self.timeline, self.resolve = Timeline(), Resolve()
        self.project = Project(self.timeline)
        for name, value in (("request_project", lambda resolve, request: self.project), ("find_timeline", lambda project, request: self.timeline)):
            patch = mock.patch.object(worker, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def add(self, **extra):
        return worker.add_comment_sticker(self.resolve, {"project": "202H", "unique_id": "TL-1", "avatar": self.avatar, "username": "@a", "lines": ["one", "two"],
                                                         "first_line": "I keep buying new sharpeners", **extra})

    def test_card_sits_on_its_own_track_until_the_first_line_is_said(self):
        self.assertEqual(self.add(), {"created": True, "start": 0, "end": 100, "saved": True})
        self.assertEqual(self.timeline.GetTrackName("video", 2), "评论贴纸")
        card = self.timeline.tracks["video"][1][0]
        self.assertEqual(card.comp.tools["Comment"].inputs["StyledText"], "one\ntwo")
        self.assertEqual(card.comp.tools["Username"].inputs["StyledText"], "@a")
        self.assertIn("MediaIn1 = Loader", card.imported)
        self.assertIn("GlobalOut = Input { Value = 99, }", card.imported)
        self.assertIn("RenderRange = { 0, 99 }", card.imported)
        self.assertEqual(self.timeline.locked, set())
        self.assertEqual(self.resolve.manager.saves, 1)

    def test_each_extra_line_grows_the_card_downward(self):
        self.add()
        tools = self.timeline.tracks["video"][1][0].comp.tools
        shift = worker.COMMENT_LINE_PITCH * 2  # The fake template's text is twice the measured size.
        self.assertAlmostEqual(tools["CardShape"].inputs["Height"], 0.1 + shift)
        self.assertEqual(tools["CardShape"].inputs["Center"][1], 0.4)
        self.assertAlmostEqual(tools["CardShape"].inputs["Center"][2], 0.7 - shift / 2)
        self.assertAlmostEqual(tools["Comment"].inputs["Center"][2], 0.65 - shift / 2)
        self.assertAlmostEqual(tools["MessageTailShape"].inputs["Point1"][2], 0.6 - shift)

    def test_one_line_keeps_the_templates_own_layout(self):
        self.add(lines=["one"])
        tools = self.timeline.tracks["video"][1][0].comp.tools
        self.assertEqual((tools["CardShape"].inputs["Height"], tools["CardShape"].inputs["Center"]), (0.1, {1: 0.4, 2: 0.7}))

    def test_a_synced_template_is_used_when_given(self):
        synced = Path(self.avatar).with_name("synced.comp")
        synced.write_text(worker.COMMENT_TEMPLATE.read_text(encoding="utf-8").replace("Does this really work?", "SYNCED MARK"), encoding="utf-8")
        self.add(template=str(synced))
        self.assertIn("SYNCED MARK", self.timeline.tracks["video"][1][0].imported)

    def test_other_tracks_are_locked_while_it_goes_on(self):
        self.add()
        self.assertIn(("video", 1, True), self.timeline.lock_history)
        self.assertNotIn(("video", 2, True), self.timeline.lock_history)

    def test_length_stays_within_bounds(self):
        self.assertEqual(self.add(first_line="I")["end"], 60)  # at least 2 seconds
        self.timeline.tracks["subtitle"][0] = [Subtitle(0, 280, "I")]
        self.assertEqual(self.add(first_line="I")["end"], 180)  # at most 6 seconds
        self.timeline.tracks["subtitle"][0] = []
        self.assertEqual(self.add(first_line="I")["end"], 90)  # no subtitles: 3 seconds

    def test_second_click_replaces_the_card(self):
        self.add()
        result = self.add(lines=["new"])
        self.assertFalse(result["created"])
        self.assertEqual(len(self.timeline.tracks["video"][1]), 1)
        self.assertEqual(self.project.pool.imports, 1)

    def test_a_foreign_clip_on_the_track_stops_it(self):
        self.timeline.tracks["video"].append([Shot(0, 30)])
        self.timeline.names[("video", 2)] = "评论贴纸"
        with self.assertRaisesRegex(worker.WorkerError, "别的片段"):
            self.add()
        self.assertEqual(self.resolve.manager.saves, 0)

    def test_failure_takes_the_card_and_new_track_back_and_restores_locks(self):
        self.timeline.refuse_import = True
        self.timeline.SetTrackLock("audio", 1, True)
        with self.assertRaisesRegex(worker.WorkerError, "模板"):
            self.add()
        self.assertEqual(self.timeline.GetTrackCount("video"), 1)
        self.assertEqual(self.timeline.locked, {("audio", 1)})
        self.assertEqual(self.resolve.manager.saves, 0)

    def test_missing_avatar_never_touches_resolve(self):
        Path(self.avatar).unlink()
        with self.assertRaisesRegex(worker.WorkerError, "头像"):
            self.add()
        self.assertEqual(self.timeline.GetTrackCount("video"), 1)

    def test_shipped_template_has_what_the_worker_fills(self):
        text = worker.COMMENT_TEMPLATE.read_text(encoding="utf-8")
        for node in ("CardShape", "MessageTailShape", "Username", "Comment", "AvatarCircle"):
            self.assertIn(f"\t\t{node} = ", text)
        self.assertIn('SourceOp = "MediaIn1"', text)
        self.assertNotIn("AvatarInitial", text)
        self.assertNotIn("CacheClip", text)

    def test_card_narrows_to_its_words_keeping_its_left_edge(self):
        comp = Comp()
        comp.tools["CardShape"].inputs["Width"] = 0.1
        worker.fill_comment_card(comp, "@a", ["one"], 0.04)
        self.assertAlmostEqual(comp.tools["CardShape"].inputs["Width"], 0.04)
        self.assertAlmostEqual(comp.tools["CardShape"].inputs["Center"][1], 0.4 - 0.03)
        comp = Comp()
        comp.tools["CardShape"].inputs["Width"] = 0.1
        worker.fill_comment_card(comp, "@a", ["one"], 0.5)  # never wider than the template
        self.assertEqual(comp.tools["CardShape"].inputs["Width"], 0.1)

    def test_a_reply_label_template_shows_only_the_name(self):
        comp = Comp()
        comp.tools["Username"].inputs["StyledText"] = "Reply to @username's comment"
        worker.fill_comment_card(comp, "@sam_k", ["one"])
        self.assertEqual(comp.tools["Username"].inputs["StyledText"], "@sam_k")


class StickerRunner(FakeRunner):
    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] != "add_comment_sticker":
            return super().__call__(args, input, **options)
        self.calls.append(request)
        body = {"ok": True, "created": sum(call["command"] == "add_comment_sticker" for call in self.calls) == 1, "start": 0, "end": 100, "saved": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(body).encode("utf-8"), b"")


class CandidateOptionsTests(unittest.TestCase):
    IDS = ("106e5d79395dbc48", "1a249448f7a6d869", "21b35bfb1fe0cd06")

    def setUp(self):
        self.app = mock.Mock()
        self.job = {"script_id": "SCR-adopted", "source": {"variant_index": 0}}
        self.provenance = {"task_id": "AIT-source", "round_id": "AIR-adopted", "variant_index": 0,
                           "candidate_text": (f"- 稿 1 回应评论 {self.IDS[0]}（质疑类）。\n"
                                              f"- 稿 2 回应评论 {self.IDS[1]}（提问类）。\n"
                                              f"- 稿 3 回应 Reddit 评论 {self.IDS[2]}（陈述类）。")}
        self.task = {"rounds": [{"round_id": "AIR-first", "instruction": "\n".join(
            f"评论 {identifier}｜TikTok｜用户\nComment {index}\n来源：https://example.test/{identifier}"
            for index, identifier in enumerate(self.IDS))}], "context": {"materials": []}}
        self.rows = {identifier: {"id": identifier, "author_handle": f"user{index}", "text": f"Question {index}?",
                                  "avatar": "saved.png" if index == 1 else None}
                     for index, identifier in enumerate(self.IDS)}
        self.app.creation.get_script.return_value = {"source": self.provenance}
        self.app.ai.get.return_value = self.task
        self.app.reviews.review.side_effect = self.rows.get

    def options(self):
        return [row["id"] for row in sticker.options(self.app, self.job)]

    def test_each_adopted_variant_recovers_its_own_comment_before_other_avatars(self):
        for index, identifier in enumerate(self.IDS):
            with self.subTest(index=index):
                self.provenance["variant_index"] = index
                options = self.options()
                self.assertEqual(options[0], identifier)
                self.assertCountEqual(options, self.IDS)

    def test_manual_edit_keeps_the_adopted_header_instead_of_using_current_outputs(self):
        self.task["rounds"].append({"round_id": "AIR-adopted", "kind": "manual_edit", "instruction": "只重译稿 1"})
        for turn in self.task["rounds"]:
            turn["output"] = f"- 稿 1 回应评论 {self.IDS[1]}（提问类）。"
        self.assertEqual(self.options()[0], self.IDS[0])

    def test_comment_after_each_draft_is_matched_to_its_own_opening(self):
        self.provenance["candidate_text"] = "\n\n---\n\n".join(
            f"开头逻辑：A\n**口播（配音用）**\n```\nDraft {i}\n```\n"
            f"- 评论 {identifier}：第 1 行回答问题。"
            for i, identifier in enumerate(self.IDS))
        for index, identifier in enumerate(self.IDS):
            with self.subTest(index=index):
                self.provenance["variant_index"] = index
                self.assertEqual(self.options()[0], identifier)

    def test_draft_comment_requires_an_unambiguous_opening_and_supplied_id(self):
        first, second, _ = self.IDS
        draft = "**口播（配音用）**\n```\nDraft\n```\n"
        for annotation in (f"- 评论 {first}：第 2 行回应问题。",
                           f"- 评论 {first}：第 1 行回答问题。\n- 评论 {second}：第 1 行回应问题。",
                           "- 评论 invented：第 1 行回答问题。"):
            with self.subTest(annotation=annotation):
                self.provenance["candidate_text"] = draft + annotation
                self.assertEqual(self.options(), [])
        self.provenance["candidate_text"] = draft + f"- 评论 {first}：第 1 行回答问题。\n" + draft
        self.assertEqual(self.options(), [])

    def test_structured_mapping_is_scoped_to_variant_and_supplied_comments(self):
        self.provenance["candidate_text"] = "\n".join(
            f"评论贴纸关联：第 {i + 1} 稿｜评论 {identifier}" for i, identifier in enumerate(self.IDS))
        for index, identifier in enumerate(self.IDS):
            self.provenance["variant_index"] = index
            self.assertEqual(sticker.adoption_comments(self.app, self.provenance, self.task)["selected_id"], identifier)
        self.provenance["candidate_text"] += "\n评论贴纸关联：第 3 稿｜评论 invented"
        self.assertEqual(sticker.adoption_comments(self.app, self.provenance, self.task)["selected_id"], "")

    def test_frozen_choice_survives_missing_task_and_comment_database(self):
        self.provenance["candidate_text"] = f"评论贴纸关联：第 1 稿｜评论 {self.IDS[0]}"
        frozen = sticker.adoption_comments(self.app, self.provenance, self.task)
        self.provenance["comment_sticker"] = frozen
        self.app.ai.get.side_effect = RuntimeError("task deleted")
        self.app.reviews.review.side_effect = RuntimeError("database unavailable")
        self.assertEqual(self.options()[0], self.IDS[0])
        frozen["selected_id"] = ""
        self.assertEqual(self.options(), [])

    def test_adoption_validates_and_freezes_the_persons_choice(self):
        from workbench_creation import CreationWorkbenchMixin
        from creation_store import CreationError
        app = mock.MagicMock()
        app.creation.replay.return_value = None
        app.creation.mutate.return_value = {"script": {"id": "SCR-test"}}
        app.creation_catalog.return_value = {"scripts": []}
        app._script_alias.side_effect = lambda script: script
        source = {"task_id": "AIT-source", "round_id": "AIR-source", "product": "202H",
                  "candidate_text": "**口播（配音用）**\n```\nFirst line.\n```"}
        app.ai.candidate.return_value = source
        app.ai.get.return_value = self.task
        app.reviews.review.side_effect = self.rows.get
        body = {"script": {"product": "202H"}, "source": {"task_id": "AIT-source", "round_id": "AIR-source",
                "task_revision": 1, "variant_index": 0, "comment_review_id": self.IDS[1]}}
        CreationWorkbenchMixin.save_script(app, body)
        frozen = app.creation.mutate.call_args.args[2]["comment_sticker"]
        self.assertEqual(frozen["selected_id"], self.IDS[1])
        self.assertEqual(frozen["candidates"][1]["text"], "Question 1?")
        body["source"]["comment_review_id"] = "not-supplied"
        with self.assertRaises(CreationError):
            CreationWorkbenchMixin.save_script(app, body)

    def test_only_comments_explicitly_supplied_in_instruction_lines_are_allowed(self):
        invented = "0123456789abcdef"
        self.provenance["candidate_text"] = f"- 稿 1 回应评论 {invented}（质疑类）。"
        self.task["rounds"][0]["instruction"] += f"\n来源：https://example.test/评论/{invented}\n参考编号 {invented}"
        self.task["rounds"][0]["review_context"] = {"status": "attached", "text": f"评论 {invented}｜摘要"}
        self.rows[invented] = {"id": invented, "author_handle": "extra", "text": "Not supplied."}
        self.assertEqual(self.options(), [])

    def test_unknown_or_ambiguous_mapping_does_not_guess_the_first_input(self):
        first, second, _ = self.IDS
        candidates = ("No explicit mapping.", f"正文里说稿 1 回应评论 {first}",
                      f"- 稿 9 回应评论 {first}（质疑类）。",
                      f"- 稿 1 回应评论 {first}（质疑类）。\n- 稿 1 回应评论 {second}（提问类）。",
                      f"- 稿 1 回应评论 {first}、{second}（质疑类）。",
                      f"- 稿 1 回应评论 {first}（也回应评论 {second}）。")
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.provenance["candidate_text"] = candidate
                self.assertEqual(self.options(), [])

    def test_source_variant_index_must_be_an_actual_nonnegative_integer(self):
        for index in (None, True, False, -1, "0", 1.0):
            with self.subTest(index=index):
                self.provenance["variant_index"] = index
                self.assertEqual(self.options(), [])

    def test_legacy_marker_and_basket_priorities_and_global_deduplication_stay(self):
        first, second, third = self.IDS
        self.task["rounds"][0]["instruction"] += f"\n{sticker.MARKER}{second}"
        self.task["context"] = {"materials": [{"type": "review", "source_id": third}],
                                "linked_reviews": [{"review_id": first, "linked_as": "方向顾虑"}]}
        self.assertEqual(self.options(), [second, third, first])
        self.provenance["candidate_text"] = "No mapping."
        self.assertEqual(self.options(), [second, third, first])

    def stance_task(self, identifier):
        self.task["rounds"] = [{"round_id": "AIR-first", "instruction": (
            f"顺着这条评论写稿（站在观众这边）。评论 {identifier}｜@user｜TikTok｜观众（评论者说法未核实）：\nQuestion?\n\n写法：……")}]

    def test_stance_comment_is_the_default_sticker_for_every_draft(self):
        second = self.IDS[1]
        self.stance_task(second)
        self.assertEqual(sticker.stance_ids(self.task["rounds"]), [second])
        self.provenance["candidate_text"] = "No mapping."
        for index in range(3):
            with self.subTest(index=index):
                self.provenance["variant_index"] = index
                chosen = sticker.adoption_comments(self.app, self.provenance, self.task)
                self.assertEqual(chosen["selected_id"], second)
                self.assertEqual([row["id"] for row in chosen["candidates"]], [second])
        self.assertEqual(self.options()[0], second)

    def test_explicit_mapping_still_wins_over_the_stance_comment(self):
        first, second, _ = self.IDS
        self.stance_task(second)
        self.task["context"] = {"materials": [{"type": "review", "source_id": first}]}
        self.provenance["candidate_text"] = f"评论贴纸关联：第 1 稿｜评论 {first}"
        self.assertEqual(sticker.adoption_comments(self.app, self.provenance, self.task)["selected_id"], first)

    def test_structured_mapping_may_leave_out_the_word_comment(self):
        self.provenance["candidate_text"] = "\n".join(
            f"评论贴纸关联：第 {i + 1} 稿｜{identifier}" for i, identifier in enumerate(self.IDS))
        for index, identifier in enumerate(self.IDS):
            self.provenance["variant_index"] = index
            self.assertEqual(sticker.adoption_comments(self.app, self.provenance, self.task)["selected_id"], identifier)

    def test_other_requests_have_no_stance_comment(self):
        self.assertEqual(sticker.stance_ids(self.task["rounds"]), [])
        self.assertEqual(sticker.stance_ids([{"instruction": f"别顺着这条评论写稿。评论 {self.IDS[0]}｜x"}]), [])


class FlowTests(unittest.TestCase):
    REVIEW = {"id": "r1", "author_handle": "zack", "author_name": "", "text": "Does this really work?", "avatar": None}

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="comment-card-flow-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.runner = StickerRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        self.bench._video_publish_copy = lambda job: None
        self.mutate("settings", settings={"creators": ["Zack", "Lab"]})
        self.job, _ = BuildTests.voiced_job(self)
        self.line = self.bench.build_timeline(self.job["id"], {"request_id": uuid4().hex, "creator": "Lab"})["timeline"]
        self.bench.reviews = mock.Mock()
        self.bench.reviews.avatars.file.return_value = None

    def mutate(self, kind, **values):
        return self.bench.production.mutate(kind, {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"], **values})

    def use(self, review):
        patch = mock.patch.object(sticker, "source", lambda app, job: review)
        patch.start()
        self.addCleanup(patch.stop)

    def click(self, request_id=None):
        return self.bench.add_timeline_comment_sticker(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": request_id or uuid4().hex})

    def calls(self):
        return [call for call in self.runner.calls if call["command"] == "add_comment_sticker"]

    def test_no_linked_comment_is_refused_before_resolve(self):
        self.use(None)
        with self.assertRaisesRegex(VoiceError, "没有带评论"):
            self.click()
        self.assertEqual(self.calls(), [])

    def typed(self, name, words):
        return self.bench.add_timeline_comment_sticker(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "username": name, "text": words})

    def test_typed_comment_goes_on_an_order_without_a_link(self):
        self.use(None)
        card = self.typed("zed", "Does it work on cheap knives?")["timeline"]["comment_sticker"]
        payload = self.calls()[0]
        self.assertEqual((payload["username"], payload["lines"]), ("@zed", ["Does it work on cheap knives?"]))
        self.assertTrue(Path(payload["avatar"]).is_file())
        self.assertIsNone(card["review_id"])
        self.bench.reviews.avatars.file.assert_not_called()

    def test_typed_comment_needs_both_fields_and_a_drawable_script(self):
        self.use(None)
        with self.assertRaisesRegex(VoiceError, "同时填"):
            self.typed("", "Only the words")
        with self.assertRaisesRegex(VoiceError, "同时填"):
            self.typed("zed", "")
        with self.assertRaisesRegex(VoiceError, "显示不了"):
            self.typed("zed", "这把磨刀器好用吗")
        self.assertEqual(self.calls(), [])

    def test_picked_comment_is_used_and_a_stale_pick_is_refused(self):
        other = {**self.REVIEW, "id": "r2", "author_handle": "amy"}
        with mock.patch.object(sticker, "options", lambda app, job: [self.REVIEW, other]):
            pick = lambda identifier: self.bench.add_timeline_comment_sticker(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "review_id": identifier})
            self.assertEqual(pick("r2")["timeline"]["comment_sticker"]["review_id"], "r2")
            self.assertEqual(self.calls()[0]["username"], "@amy")
            with self.assertRaisesRegex(VoiceError, "找不到"):
                pick("r9")

    def test_a_comment_picked_from_the_library_search_is_used_and_goes_to_the_basket(self):
        found = {"id": "lib1", "author_handle": "knife.guy", "author_name": "", "text": "Mine slipped every time.", "avatar": None, "thread": []}
        self.bench.reviews.review.side_effect = lambda identifier: found if identifier == "lib1" else None
        self.bench.reviews.note_used.return_value = {"added": ["lib1"]}
        with mock.patch.object(sticker, "options", lambda app, job: [self.REVIEW]):
            pick = lambda identifier: self.bench.add_timeline_comment_sticker(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "review_id": identifier})
            card = pick("lib1")["timeline"]["comment_sticker"]
            self.assertEqual((card["review_id"], card["username"]), ("lib1", "@knife.guy"))
            self.assertEqual(self.bench.reviews.note_used.call_args.args[0][0][0], "lib1")
            with self.assertRaisesRegex(VoiceError, "找不到"):
                pick("gone")

    def test_options_put_comments_with_avatars_first(self):
        self.bench.ai = mock.Mock()
        self.bench.ai.get.return_value = {"rounds": [{"instruction": f"{sticker.MARKER}r1、r2、r3"}]}
        rows = {"r1": self.REVIEW, "r2": {**self.REVIEW, "id": "r2", "avatar": "a.png"}, "r3": {**self.REVIEW, "id": "r3", "text": "这里是中文"}}
        self.bench.reviews.review.side_effect = rows.get
        with mock.patch.object(self.bench.creation, "get_script", return_value={"source": {"task_id": "AIT-1"}}):
            self.assertEqual([row["id"] for row in sticker.options(self.bench, self.job)], ["r2", "r1"])

    def test_options_include_every_comment_the_writing_carried(self):
        self.bench.ai = mock.Mock()
        self.bench.ai.get.return_value = {"rounds": [{"instruction": f"{sticker.MARKER}open"}], "context": {
            "materials": [{"type": "review", "source_id": "basket"}, {"type": "case", "source_id": "not-a-comment"}, {"type": "review", "source_id": "open"}],
            "linked_reviews": [{"review_id": "proof", "linked_as": "卖点证据"}, {"review_id": "worry", "linked_as": "方向顾虑"}, {"review_id": "basket", "linked_as": "方向顾虑"}]}}
        # The basket comment has an avatar, but the comment the opening was written from still comes first.
        self.bench.reviews.review.side_effect = lambda identifier: {**self.REVIEW, "id": identifier, "avatar": "a.png" if identifier == "basket" else None} if identifier != "not-a-comment" else None
        with mock.patch.object(self.bench.creation, "get_script", return_value={"source": {"task_id": "AIT-1"}}):
            self.assertEqual([row["id"] for row in sticker.options(self.bench, self.job)], ["open", "basket", "worry", "proof"])

    def test_chinese_reading_is_generated_once_and_skips_undrawable_text(self):
        from workbench_video_publish import VideoPublishMixin as Publish
        app = mock.Mock()
        app._video_publications.return_value.root = Path(self.bench.production.root)
        app._title_translations = lambda: Publish._title_translations(app)
        app._chinese_readings = lambda lines, failure: Publish._chinese_readings(app, lines, failure)
        with mock.patch("case_breakdown_runner.translate_lines", return_value=["真的好用吗"]) as translate:
            first = Publish.translate_sticker_comment(app, self.job["id"], {"text": "Does  it work?"})
            again = Publish.translate_sticker_comment(app, self.job["id"], {"text": "Does it work?"})
            none = Publish.translate_sticker_comment(app, self.job["id"], {"text": "中文评论"})
        self.assertEqual((first, again, none), ({"zh": "真的好用吗"}, {"zh": "真的好用吗"}, {"zh": ""}))
        self.assertEqual(translate.call_count, 1)

    def test_click_sends_the_comment_and_records_it(self):
        self.use(self.REVIEW)
        card = self.click()["timeline"]["comment_sticker"]
        payload = self.calls()[0]
        self.assertEqual((payload["username"], payload["lines"]), ("@zack", ["Does this really work?"]))
        self.assertTrue(Path(payload["avatar"]).is_file())
        self.assertEqual(payload["first_line"], self.job["lines"][0]["spoken"])
        self.assertEqual((card["message"], card["review_id"], card["status"]), ("评论贴纸已加", "r1", "saved"))
        self.assertIn("评论贴纸：@zack", self.bench.production.job(self.job["id"])["history"][-1]["note"])

    def test_second_click_says_updated(self):
        self.use(self.REVIEW)
        self.click()
        self.assertEqual(self.click()["timeline"]["comment_sticker"]["message"], "评论贴纸已更新")

    def test_repeated_request_does_not_run_twice(self):
        self.use(self.REVIEW)
        request_id = uuid4().hex
        self.click(request_id)
        self.click(request_id)
        self.assertEqual(len(self.calls()), 1)

    def test_source_follows_script_task_and_request(self):
        self.bench.ai = mock.Mock()
        self.bench.ai.get.return_value = {"rounds": [{"instruction": f"写稿\n{sticker.MARKER}r1、r2"}]}
        self.bench.reviews.review.side_effect = lambda identifier: {**self.REVIEW, "id": identifier}
        with mock.patch.object(self.bench.creation, "get_script", return_value={"source": {"task_id": "AIT-1"}}):
            self.assertEqual(sticker.source(self.bench, self.job)["id"], "r1")
        self.bench.ai.get.assert_called_once_with("AIT-1")

    def test_source_is_none_when_any_link_is_missing(self):
        self.bench.ai = mock.Mock()
        with mock.patch.object(self.bench.creation, "get_script", return_value={"source": {}}):
            self.assertIsNone(sticker.source(self.bench, self.job))
        self.bench.ai.get.side_effect = RuntimeError("gone")
        with mock.patch.object(self.bench.creation, "get_script", return_value={"source": {"task_id": "AIT-1"}}):
            self.assertIsNone(sticker.source(self.bench, self.job))


DESIGNED = (APP / "templates" / "comment-sticker.comp").read_text(encoding="utf-8")


def designed():
    """The shipped template put back the way it looks in Resolve: grey circle, initial, their merges, window layout."""
    color = '\t\tAvatarColor = Background {\n\t\t\tInputs = {\n\t\t\t\tEffectMask = Input {\n\t\t\t\t\tSourceOp = "AvatarCircle",\n\t\t\t\t\tSource = "Mask",\n\t\t\t\t}\n\t\t\t},\n\t\t},\n'
    merge = '\t\tMerge1 = Merge {\n\t\t\tInputs = {\n\t\t\t\tBackground = Input {\n\t\t\t\t\tSourceOp = "BubbleWithTail",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tForeground = Input {\n\t\t\t\t\tSourceOp = "AvatarColor",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t},\n\t\t},\n'
    initial = '\t\tAvatarInitial = TextPlus {\n\t\t\tInputs = {\n\t\t\t},\n\t\t},\n\t\tMerge2 = Merge {\n\t\t\tInputs = {\n\t\t\t\tBackground = Input {\n\t\t\t\t\tSourceOp = "Merge1",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tForeground = Input {\n\t\t\t\t\tSourceOp = "AvatarInitial",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t},\n\t\t},\n'
    start = DESIGNED.index("\n\t\tMerge1 = Merge {") + 1
    end = DESIGNED.index("\n\t\tUsername = ") + 1
    text = DESIGNED[:start] + color + merge + initial + DESIGNED[end:]
    text = text.replace('SourceOp = "Merge1"', 'SourceOp = "Merge2"').replace('\t\t\t\t\tSourceOp = "Merge2",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tForeground = Input {\n\t\t\t\t\tSourceOp = "AvatarInitial"', '\t\t\t\t\tSourceOp = "Merge1",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tForeground = Input {\n\t\t\t\t\tSourceOp = "AvatarInitial"')
    return text.rstrip()[:-1] + '\tFrames = {\n\t\tCacheClip = "C:/machine/path",\n\t},\n}\n'


class MadeUpCommentTests(unittest.TestCase):
    MADE = {"type": "ai_comment", "source_id": "AI-0123456789ab", "author_handle": "@samk", "title": "@samk", "text": "does it do serrated?", "zh": "锯齿刀能磨吗"}

    def test_a_made_up_basket_comment_is_offered_but_never_goes_to_the_basket_as_used(self):
        task = {"rounds": [{"instruction": "写稿"}], "context": {"materials": [self.MADE]}}
        app = mock.Mock()
        app.creation.get_script.return_value = {"source": {"task_id": "T1"}}
        app.ai.get.return_value = task
        app.reviews.review.return_value = None
        [row] = sticker.options(app, {"script_id": "S1"})
        self.assertEqual((row["id"], sticker.username(row), row["avatar"]), ("AI-0123456789ab", "@samk", None))
        app.reviews.review.assert_not_called()
        self.assertEqual(sticker.attached_ids(task["context"]), ["AI-0123456789ab"])
        self.assertIsNone(sticker.video_use({"id": "PRD-1"}, {"unique_id": "u"}, {"review_id": "AI-0123456789ab"}))
        frozen = sticker.adoption_comments(app, {"candidate_text": "评论贴纸关联：第 1 稿｜评论 AI-0123456789ab", "variant_index": 0}, task)
        self.assertEqual(frozen["selected_id"], "AI-0123456789ab")
        self.assertEqual(frozen["candidates"][0]["text"], "does it do serrated?")


class MadeUpOpeningRuleTests(unittest.TestCase):
    def test_writing_is_told_to_open_every_draft_as_a_reply_to_the_made_up_comment(self):
        import ai_tasks
        self.assertEqual(ai_tasks._made_up_opening_rule({"materials": [{"type": "review", "source_id": "r1"}]}), "")
        rule = ai_tasks._made_up_opening_rule({"materials": [MadeUpCommentTests.MADE]})
        self.assertIn('评论 AI-0123456789ab｜"@samk"："does it do serrated?"', rule)
        for words in ("每条稿都用上面的一条评论开场", "不反复叫评论者名字", "不用 Your move", "第一句口播直接接这条评论说话", "不要把评论者的处境、经历或原话改成讲述者自己的经历", "评论贴纸关联：第 N 稿｜评论 ID"):
            self.assertIn(words, rule)


class LibraryAndComposeTests(unittest.TestCase):
    def test_search_keeps_only_comments_the_card_can_carry(self):
        reviews = mock.Mock()
        reviews.search.return_value = {"total": 3, "reviews": [
            {"id": "a", "author_handle": "amy", "author_name": "", "text": "Works great", "zh": "很好用", "avatar": "k1", "likes": 9, "product": "202H"},
            {"id": "b", "author_handle": "bob", "author_name": "", "text": "很好用", "avatar": None},
            {"id": "c", "author_handle": "", "author_name": "", "text": "No name", "avatar": None}]}
        result = sticker.search_library(reviews, "  好用 ")
        self.assertEqual(reviews.search.call_args.args[0], {"q": "好用"})
        self.assertEqual(result["comments"], [{"id": "a", "username": "@amy", "text": "Works great", "zh": "很好用", "avatar": "k1", "product": "202H", "likes": 9}])
        self.assertEqual(sticker.search_library(reviews, " "), {"total": 0, "comments": []})

    def test_composed_comment_is_cleaned_or_refused(self):
        self.assertEqual(sticker.parse_composed('好的\n{"username":"@Sam K!","text":"does it  work on serrated?","zh":"锯齿刀能用吗"}'),
                         {"username": "@SamK", "text": "does it work on serrated?", "zh": "锯齿刀能用吗"})
        self.assertIsNone(sticker.parse_composed('{"username":"sam","text":"真的好用"}'))
        self.assertIsNone(sticker.parse_composed('{"username":"","text":"works"}'))
        self.assertIsNone(sticker.parse_composed("not json"))
        self.assertIn("<要求>温柔 \\u003cb></要求>", sticker.compose_prompt("温柔 <b>", "202H", "Stop."))


class TemplateTests(unittest.TestCase):
    def test_shipped_template_gives_the_measured_sizes(self):
        # The 2026-11-16 reply-to-comment look: small avatar beside the grey label, bold comment from the card's left edge.
        self.assertEqual(sticker.template_metrics(DESIGNED), {"line_width": 46, "avatar_center": (213, 664), "avatar_size": 37})
        self.assertIn('StyledText = Input { Value = "@username", }', DESIGNED)
        self.assertNotIn("Reply to", DESIGNED)

    def test_card_width_follows_the_longest_line(self):
        fonts = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fonts, True)
        found = {name: next((p for p in (Path("C:/Windows/Fonts") / name, Path("/usr/share/fonts/truetype/liberation") / alt,
                                         Path("/usr/share/fonts/truetype/liberation2") / alt) if p.is_file()), None)
                 for name, alt in (("arial.ttf", "LiberationSans-Regular.ttf"), ("arialbd.ttf", "LiberationSans-Bold.ttf"))}
        if not all(found.values()):
            self.skipTest("no Arial or Liberation Sans here")
        for name, path in found.items():
            shutil.copy(path, fonts / name)
        short = sticker.card_width(DESIGNED, "@sam_k", ["Does this really work?"], fonts)
        long = sticker.card_width(DESIGNED, "@sam_k", ["Little pricey of an option. Average people do", "not need this big of a set."], fonts)
        self.assertAlmostEqual(short, 0.296, delta=0.01)  # measured off the Resolve render: text ends at 0.43, plus the 0.03 margin
        self.assertLess(short, long)
        self.assertLessEqual(long, 0.68)
        self.assertEqual(sticker.label_text(DESIGNED, "@sam_k"), "@sam_k")
        self.assertEqual(sticker.label_text(DESIGNED.replace('"@username"', '"Reply to @username\'s comment"'), "@sam_k"), "@sam_k")
        self.assertIsNone(sticker.card_width(DESIGNED, "@sam_k", ["x"], fonts / "missing"))

    def test_the_first_regular_template_keeps_its_measured_sizes(self):
        first = (DESIGNED.replace("{ 0.197, 0.654 }", "{ 0.218, 0.64 }").replace("Width = Input { Value = 0.03, }", "Width = Input { Value = 0.073, }")
                 .replace("{ 0.19, 0.627 }", "{ 0.275, 0.627 }").replace('Value = "Bold"', 'Value = "Regular"').replace("Value = 0.032,", "Value = 0.034,"))
        self.assertEqual(sticker.template_metrics(first), {"line_width": sticker.LINE_WIDTH, "avatar_center": sticker.AVATAR_CENTER, "avatar_size": sticker.AVATAR_SIZE})

    def test_larger_text_fits_fewer_characters_and_a_moved_avatar_moves_the_picture(self):
        text = DESIGNED.replace("Size = Input { Value = 0.032, }", "Size = Input { Value = 0.064, }").replace("Center = Input { Value = { 0.197, 0.654 }, }", "Center = Input { Value = { 0.3, 0.5 }, }")
        metrics = sticker.template_metrics(text)
        self.assertEqual(metrics["line_width"], 23)
        self.assertEqual(metrics["avatar_center"], (324, 960))

    def test_the_design_becomes_a_clip_avatar_template(self):
        text = sticker.adapt_template(designed())
        for gone in ("AvatarColor", "AvatarInitial", "Merge2", "CacheClip", "Frames"):
            self.assertNotIn(gone, text)
        block = text[text.index("Merge1 = Merge"):text.index("Username = ")]
        self.assertIn('SourceOp = "MediaIn1"', block)
        self.assertIn('SourceOp = "AvatarCircle"', block)
        merge3 = text[text.index("Merge3 = Merge"):text.index("Comment = ")]
        self.assertIn('SourceOp = "Merge1"', merge3)

    def test_an_already_adapted_template_passes_unchanged_in_its_nodes(self):
        self.assertIn('SourceOp = "MediaIn1"', sticker.adapt_template(DESIGNED))

    def test_renamed_or_broken_nodes_are_named_in_the_refusal(self):
        with self.assertRaisesRegex(sticker.TemplateError, "Username"):
            sticker.adapt_template(DESIGNED.replace("Username = TextPlus", "Name = TextPlus"))
        with self.assertRaisesRegex(sticker.TemplateError, "AvatarColor"):
            sticker.adapt_template(DESIGNED.replace('"MediaIn1"', '"Merge3"', 1).replace('SourceOp = "Merge3",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tEffectMask', 'SourceOp = "Card",\n\t\t\t\t\tSource = "Output",\n\t\t\t\t},\n\t\t\t\tEffectMask'))


class SyncFlowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="comment-template-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.bench.resolve = mock.Mock()

    def test_shipped_template_until_a_sync_then_the_synced_one(self):
        self.assertEqual(self.bench.comment_template_status()["synced"], False)
        self.assertEqual(self.bench._comment_template_path(), sticker.SHIPPED_TEMPLATE)
        self.bench.resolve.run.return_value = {"ok": True, "comp": DESIGNED, "project": "Lab-202H"}
        result = self.bench.sync_comment_template({})
        self.assertEqual((result["synced"], result["project"]), (True, "Lab-202H"))
        self.assertEqual(self.bench.resolve.run.call_args[0][1]["name"], sticker.TEMPLATE_TIMELINE)
        self.assertNotEqual(self.bench._comment_template_path(), sticker.SHIPPED_TEMPLATE)

    def test_a_bad_design_keeps_the_current_template(self):
        self.bench.resolve.run.return_value = {"ok": True, "comp": DESIGNED.replace("Comment = TextPlus", "Words = TextPlus"), "project": "P"}
        with self.assertRaisesRegex(VoiceError, "Comment"):
            self.bench.sync_comment_template({})
        self.assertEqual(self.bench._comment_template_path(), sticker.SHIPPED_TEMPLATE)


class ReadTemplateWorkerTests(unittest.TestCase):
    def world(self, timelines):
        manager = mock.Mock()
        manager.GetCurrentProject.return_value = mock.Mock()
        resolve = mock.Mock()
        resolve.GetProjectManager.return_value = manager
        patch = mock.patch.object(worker, "timelines", lambda project: timelines)
        patch.start()
        self.addCleanup(patch.stop)
        return resolve

    def test_missing_timeline_says_what_to_do(self):
        with self.assertRaisesRegex(worker.WorkerError, "新建一条"):
            worker.read_comment_template(self.world([]), {"name": "T"})

    def test_timeline_without_the_sticker_is_refused(self):
        timeline = mock.Mock()
        timeline.GetName.return_value = "T"
        timeline.GetTrackCount.return_value = 1
        timeline.GetItemListInTrack.return_value = []
        with self.assertRaisesRegex(worker.WorkerError, "没有贴纸"):
            worker.read_comment_template(self.world([timeline]), {"name": "T"})


class FrontendTests(unittest.TestCase):
    def test_row_is_bound_to_the_routes_and_offers_typing(self):
        code = (APP / "static" / "video-publication.js").read_text(encoding="utf-8")
        self.assertIn("/comment-sticker`", code)
        self.assertIn("/publication/comment-zh`", code)
        self.assertIn("${commentStickerRow(timeline,copy,hookLock)}", code)
        self.assertIn("data-comment-reading", code)
        self.assertNotIn("copy?.comment_sticker;", code)


if __name__ == "__main__":
    unittest.main()
