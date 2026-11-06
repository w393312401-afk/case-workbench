"""A timeline cut on another computer replaces an order's timeline: the worker's import checks and the workbench flow, with fakes."""
import base64
import io
import json
import struct
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(APP / "scripts"))

import resolve_worker as worker
from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge
from workbench_production import voice_file_seconds
import test_hook_text
from test_production import Bench, FakeEleven
import test_production_resolve
from test_production_resolve import FakeRunner


def drt(extra=b""):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("project.xml", "<p/>")
        archive.writestr("SeqContainer/abc.xml", "<s/>" + extra.decode())
    return output.getvalue()


def voice_drt(seconds):
    clip = (f'<Sm2TiAudioClip DbId="a"><Name>voiceover.mp3</Name><Duration>980</Duration>'
            f'<MediaTimemapBA>02{struct.pack(">d", seconds).hex()}</MediaTimemapBA></Sm2TiAudioClip>'
            '<Sm2TiAudioClip DbId="b"><Name>shot.mov</Name><MediaTimemapBA>02408d36f0298191f4</MediaTimemapBA></Sm2TiAudioClip>')
    return drt(clip.encode())


class VoiceFileTests(unittest.TestCase):
    def test_only_files_named_voiceover_are_read(self):
        self.assertEqual(voice_file_seconds(voice_drt(35.3625)), [35.362])
        self.assertEqual(voice_file_seconds(drt()), [])
        self.assertEqual(voice_file_seconds(b"not a zip"), [])


class ImportRunner(FakeRunner):
    def __init__(self, reply=None):
        super().__init__()
        self.import_reply = reply
        self.export_bytes = drt()
        self.voice_files = []

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        if request["command"] == "export_drt":
            self.calls.append(request)
            if self.export_bytes is not None:
                Path(request["file"]).write_bytes(self.export_bytes)
            body = {"ok": True, "name": request["name"], "size": len(self.export_bytes or b""), "voice_files": self.voice_files}
            return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")
        if request["command"] != "import_timeline":
            return super().__call__(args, input, **options)
        self.calls.append(request)
        self.saw_file = Path(request["file"]).read_bytes()
        body = self.import_reply or {"ok": True, "name": request["name"], "unique_id": "uid-home", "start_frame": 0, "end_frame": 450,
                                     "fps": "29.97", "clips": 12, "fonts": ["CapCut Sans Text"], "missing_fonts": [], "warnings": [], "saved": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class FlowTestsHome(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="home-timeline-")
        self.addCleanup(temp.cleanup)
        self.bench = Bench(temp.name, FakeEleven())
        self.runner = ImportRunner()
        self.bench.resolve = ResolveBridge(runner=self.runner)
        test_hook_text.FlowTests.mutate(self, "settings", settings={"creators": ["Zack", "Lab"]})
        self.job, _ = test_production_resolve.BuildTests.voiced_job(self)
        self.line = self.bench.build_timeline(self.job["id"], {"request_id": uuid4().hex, "creator": "Lab"})["timeline"]

    def mutate(self, kind, **values):
        return test_hook_text.FlowTests.mutate(self, kind, **values)

    def upload(self, data=None, request_id=None):
        self.sent = data if data is not None else drt()
        return self.bench.import_home_timeline(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": request_id or uuid4().hex,
                                                                "file_name": "Lab-家里.drt",
                                                                "data": base64.b64encode(self.sent).decode()})

    def imports(self):
        return [call for call in self.runner.calls if call["command"] == "import_timeline"]

    def test_new_timeline_replaces_the_old_one_which_is_archived(self):
        result = self.upload()
        call = self.imports()[0]
        self.assertEqual((call["unique_id"], call["name"]), (self.line["unique_id"], f"{self.line['name']}-家里"))
        self.assertEqual(self.runner.saw_file, self.sent)
        new = result["timeline"]
        self.assertEqual((new["unique_id"], new["frames"], new["take_id"], new["creator"]), ("uid-home", 450, self.line["take_id"], "Lab"))
        self.assertEqual(new["imported_from"]["timeline_uid"], self.line["unique_id"])
        stored = self.bench.production.job(self.job["id"])
        old = next(row for row in stored["timelines"] if row["unique_id"] == self.line["unique_id"])
        self.assertTrue(old["archived"])
        self.assertEqual(old["replaced_by"], "uid-home")
        self.assertIn("导入另一台电脑剪的时间线", stored["history"][-1]["note"])

    def test_voice_length_from_the_file_and_take_go_to_resolve(self):
        self.runner.import_reply = {"ok": True, "name": "x-家里", "unique_id": "uid-home", "start_frame": 0, "end_frame": 10, "clips": 1,
                                    "voice_relinked": 1, "saved": True}
        warnings = self.upload(voice_drt(35.363))["timeline"]["warnings"]
        call = self.imports()[0]
        take = next(item for item in self.bench.production.job(self.job["id"])["takes"] if item["id"] == self.line["take_id"])
        self.assertEqual((call["home_voice_seconds"], call["take_seconds"], call["voice_track"]), ([35.363], take["duration"], 2))
        self.assertTrue(any("已换回这张单的配音" in item for item in warnings))

    def test_same_request_imports_once(self):
        request_id = uuid4().hex
        self.upload(request_id=request_id)
        self.upload(request_id=request_id)
        self.assertEqual(len(self.imports()), 1)

    def test_missing_fonts_and_unsaved_project_are_warned(self):
        self.runner.import_reply = {"ok": True, "name": "x-家里", "unique_id": "uid-home", "start_frame": 0, "end_frame": 10, "clips": 1,
                                    "fonts": ["Metropolis"], "missing_fonts": ["Metropolis"], "warnings": ["帧率和原时间线不同：25（原来 29.97）。"], "saved": False}
        warnings = self.upload()["timeline"]["warnings"]
        self.assertTrue(any("Metropolis" in item for item in warnings))
        self.assertTrue(any("帧率" in item for item in warnings))
        self.assertTrue(any("没有保存" in item for item in warnings))

    def test_not_a_drt_never_reaches_resolve(self):
        with self.assertRaisesRegex(VoiceError, "不是达芬奇时间线文件"):
            self.upload(b"plain text")
        with self.assertRaisesRegex(VoiceError, "为空"):
            self.upload(b"")
        with self.assertRaisesRegex(VoiceError, "超过 3 MB"):
            self.upload(b"x" * (3 * 1024 * 1024 + 1))
        self.assertEqual(self.imports(), [])

    def test_resolve_refusal_records_nothing(self):
        self.runner.import_reply = {"ok": False, "error": "导入后有 2 个片段在这台电脑上找不到素材"}
        with self.assertRaisesRegex(VoiceError, "找不到素材"):
            self.upload()
        stored = self.bench.production.job(self.job["id"])
        self.assertEqual([row["unique_id"] for row in stored["timelines"]], [self.line["unique_id"]])
        self.assertFalse(stored["timelines"][0].get("archived"))

    def test_archived_or_unknown_timeline_is_refused(self):
        with self.assertRaises(VoiceError):
            self.bench.import_home_timeline(self.job["id"], {"timeline_uid": "nope", "request_id": uuid4().hex,
                                                             "data": base64.b64encode(drt()).decode()})
        self.bench.archive_timeline(self.job["id"], {"timeline_uid": self.line["unique_id"], "request_id": uuid4().hex, "archived": True,
                                                        "revision": self.bench.production.public()["revision"]})
        with self.assertRaisesRegex(VoiceError, "已归档"):
            self.upload()
        self.assertEqual(self.imports(), [])


class ExportFlowTests(unittest.TestCase):
    setUp = FlowTestsHome.setUp
    mutate = FlowTestsHome.mutate

    def test_download_packs_the_drt_with_the_voice_under_its_own_name(self):
        voice = self.bench.production.audio / self.job["id"] / "build-0123456789abcdef.wav"
        voice.parent.mkdir(parents=True, exist_ok=True)
        voice.write_bytes(b"RIFF voice")
        outside = Path(self.bench.production.root).parent / "elsewhere.wav"
        outside.write_bytes(b"RIFF other")
        self.runner.voice_files = [str(voice), str(outside)]
        content, name = self.bench.export_timeline_file(self.job["id"], self.line["unique_id"])
        self.assertEqual(name, f"{self.line['name']}.zip")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(sorted(archive.namelist()), sorted([f"{self.line['name']}.drt", "build-0123456789abcdef.wav", "说明.txt"]))
            self.assertEqual(archive.read(f"{self.line['name']}.drt"), self.runner.export_bytes)
            self.assertEqual(archive.read("build-0123456789abcdef.wav"), b"RIFF voice")
            self.assertIn("elsewhere.wav", archive.read("说明.txt").decode("utf-8"))
        call = [call for call in self.runner.calls if call["command"] == "export_drt"][0]
        self.assertEqual((call["unique_id"], call["voice_track"]), (self.line["unique_id"], 2))
        self.assertFalse(Path(call["file"]).exists())

    def test_unknown_timeline_or_empty_export_is_refused(self):
        with self.assertRaises(VoiceError):
            self.bench.export_timeline_file(self.job["id"], "nope")
        self.runner.export_bytes = b""
        with self.assertRaisesRegex(VoiceError, "为空"):
            self.bench.export_timeline_file(self.job["id"], self.line["unique_id"])


class Item:
    def __init__(self, name, online="Online", font=None):
        self.name, self.online, self.font = name, online, font

    GetName = lambda self: self.name
    GetFusionCompCount = lambda self: 1 if self.font else 0

    def GetMediaPoolItem(self):
        return None if self.font else Media(self.online)

    def GetFusionCompByIndex(self, index):
        tool = type("Tool", (), {"GetInput": lambda _, key: self.font})()
        return type("Comp", (), {"GetToolList": lambda _, selected, kind: {1: tool}})()


class Media:
    def __init__(self, online):
        self.online = online

    def GetClipProperty(self, key):
        return self.online


class Voice:
    def __init__(self, media_id, start, end):
        self.media_id, self.start, self.end = media_id, start, end

    GetStart = lambda self: self.start
    GetEnd = lambda self: self.end
    GetName = lambda self: "voice"
    GetLeftOffset = lambda self: 0

    def GetMediaPoolItem(self):
        return type("Media", (), {"GetUniqueId": lambda _: self.media_id, "GetName": lambda _: f"{self.media_id}_voice",
                                  "GetClipProperty": lambda _, key: "Online"})()


class Line:
    def __init__(self, uid, name, video=(), settings=None, voice=()):
        self.uid, self.name, self.video, self.voice = uid, name, list(video), list(voice)
        self.settings = settings or {"timelineFrameRate": "29.97", "timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"}
        self.timecode = "00:00:01:00"

    GetUniqueId = lambda self: self.uid
    GetName = lambda self: self.name
    GetStartFrame = lambda self: 0
    GetEndFrame = lambda self: 300
    GetSetting = lambda self, key: self.settings.get(key)
    GetCurrentTimecode = lambda self: self.timecode

    def SetCurrentTimecode(self, value):
        self.timecode = value
        return True

    def SetName(self, name):
        self.name = name
        return True

    def DeleteClips(self, items, ripple):
        self.voice = [item for item in self.voice if item not in items]
        return True

    def GetTrackCount(self, kind):
        return 1 if kind == "video" else 2 if kind == "audio" and self.voice else 0

    def GetItemListInTrack(self, kind, index):
        return self.video if kind == "video" else self.voice if kind == "audio" and index == 2 else []


class Pool:
    def __init__(self, project, imported):
        self.project, self.imported, self.deleted = project, imported, []

    def ImportTimelineFromFile(self, path, options):
        self.options = options
        if self.imported:
            self.project.lines.append(self.imported)
            self.project.current = self.imported
        return self.imported

    def AppendToTimeline(self, clips):
        clip = clips[0]
        placed = Voice(clip["mediaPoolItem"].GetUniqueId(), clip["recordFrame"], clip["recordFrame"] + clip["endFrame"] - clip["startFrame"])
        self.project.current.voice.append(placed)
        return [placed]

    def DeleteTimelines(self, rows):
        self.deleted.extend(rows)
        self.project.lines = [line for line in self.project.lines if line not in rows]
        return True


class Project:
    def __init__(self, lines, imported):
        self.lines, self.current = lines, lines[0]
        self.pool = Pool(self, imported)

    GetName = lambda self: "Lab-202H"
    GetUniqueId = lambda self: "project-1"
    GetMediaPool = lambda self: self.pool
    GetCurrentTimeline = lambda self: self.current
    GetTimelineCount = lambda self: len(self.lines)
    GetTimelineByIndex = lambda self, index: self.lines[index - 1]

    def SetCurrentTimeline(self, line):
        self.current = line
        return True


class Fonts:
    GetFontList = lambda self: {"CapCut Sans Text": {}, "Arial": {}}


class Resolve:
    def __init__(self, project):
        self.project, self.page, self.saved = project, "edit", 0
        manager = type("Manager", (), {"GetCurrentProject": lambda _: project, "SaveProject": lambda _: self._save()})()
        self.manager = manager

    def _save(self):
        self.saved += 1
        return True

    GetProjectManager = lambda self: self.manager
    GetCurrentPage = lambda self: self.page
    Fusion = lambda self: type("Fusion", (), {"FontManager": Fonts()})()

    def OpenPage(self, page):
        self.page = page


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="home-import-")
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "cut.drt"
        self.file.write_bytes(drt())
        self.source = Line("uid-old", "Lab261101-202-3", [Item("a.mov")])
        self.other = Line("uid-busy", "Lab261101-202-3-家里")

    def run_import(self, imported, lines=None, request=None):
        project = Project(lines or [self.source, self.other], imported)
        resolve = Resolve(project)
        request = {"project": "Lab-202H", "unique_id": "uid-old", "name": "Lab261101-202-3-家里", "file": str(self.file), **(request or {})}
        return project, resolve, (lambda: worker.import_timeline(resolve, request))

    def test_linked_cut_is_renamed_beside_the_old_one_and_saved(self):
        imported = Line("uid-new", "cut", [Item("a.mov"), Item("Text+", font="Metropolis"), Item("t", font="CapCut Sans Text")])
        project, resolve, run = self.run_import(imported)
        result = run()
        self.assertEqual(project.pool.options, {"importSourceClips": False})
        self.assertEqual((result["name"], result["unique_id"], result["clips"]), ("Lab261101-202-3-家里-2", "uid-new", 1))
        self.assertEqual((result["fonts"], result["missing_fonts"]), (["CapCut Sans Text", "Metropolis"], ["Metropolis"]))
        self.assertTrue(result["saved"])
        self.assertIs(project.current, self.source)
        self.assertEqual(project.pool.deleted, [])

    def test_offline_clips_take_the_import_back_out(self):
        imported = Line("uid-new", "cut", [Item("a.mov"), Item("b.mov", online="Offline")])
        project, resolve, run = self.run_import(imported)
        with self.assertRaisesRegex(worker.WorkerError, "1 个片段在这台电脑上找不到素材（b.mov）"):
            run()
        self.assertEqual(project.pool.deleted, [imported])
        self.assertIs(project.current, self.source)
        self.assertEqual(resolve.saved, 0)

    def test_a_different_frame_rate_is_reported(self):
        imported = Line("uid-new", "cut", [Item("a.mov")], {"timelineFrameRate": "25", "timelineResolutionWidth": "1080", "timelineResolutionHeight": "1920"})
        _, _, run = self.run_import(imported)
        self.assertTrue(any("帧率" in item for item in run()["warnings"]))

    def test_a_file_resolve_cannot_read_changes_nothing(self):
        project, _, run = self.run_import(None)
        with self.assertRaisesRegex(worker.WorkerError, "没能导入"):
            run()
        self.assertEqual(len(project.lines), 2)

    def test_export_writes_the_timeline_as_drt(self):
        written = []
        self.source.Export = lambda path, kind: written.append((path, kind)) or Path(path).write_bytes(b"PK") or True
        project = Project([self.source], None)
        resolve = Resolve(project)
        resolve.EXPORT_DRT = "drt"
        target = self.file.with_name("out.drt")
        result = worker.export_drt(resolve, {"project": "Lab-202H", "unique_id": "uid-old", "file": str(target)})
        self.assertEqual((result["name"], result["size"], written[0][1]), ("Lab261101-202-3", 2, "drt"))

    def test_voice_of_unknown_length_linked_elsewhere_is_taken_back_out(self):
        self.source.voice = [Voice("voice-this-order", 0, 1059)]
        imported = Line("uid-new", "cut", [Item("a.mov")], voice=[Voice("voice-old-order", 0, 980)])
        project, _, run = self.run_import(imported, request={"take_seconds": 35.36, "home_voice_seconds": [32.7]})
        with self.assertRaisesRegex(worker.WorkerError, "没有接上这张单的配音.*voice-old-order_voice.*32.7 秒.*35.4 秒"):
            run()
        self.assertEqual(project.pool.deleted, [imported])

    def test_same_length_voice_is_put_back_on_the_orders_own_file(self):
        self.source.voice = [Voice("voice-this-order", 0, 1059)]
        imported = Line("uid-new", "cut", [Item("a.mov")], voice=[Voice("voice-old-order", 0, 980)])
        project, _, run = self.run_import(imported, request={"take_seconds": 35.36, "home_voice_seconds": [35.363]})
        result = run()
        self.assertEqual(result["voice_relinked"], 1)
        self.assertEqual([(v.media_id, v.start, v.end) for v in imported.voice], [("voice-this-order", 0, 980)])
        self.assertIs(project.current, self.source)

    def test_the_orders_own_voice_passes(self):
        self.source.voice = [Voice("voice-this-order", 0, 1059)]
        imported = Line("uid-new", "cut", [Item("a.mov")], voice=[Voice("voice-this-order", 0, 1000)])
        _, _, run = self.run_import(imported)
        self.assertEqual(run()["unique_id"], "uid-new")

    def test_empty_cut_is_taken_back_out(self):
        imported = Line("uid-new", "cut", [])
        project, _, run = self.run_import(imported)
        with self.assertRaisesRegex(worker.WorkerError, "没有视频片段"):
            run()
        self.assertEqual(project.pool.deleted, [imported])


if __name__ == "__main__":
    unittest.main()
