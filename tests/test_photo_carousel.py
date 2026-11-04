import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

import carousel_render as render
import photo_carousel
import test_server as http_fixture
from photo_carousel import PhotoCarousels, PhotoSetError, migrate, parse_plan
from resolve_bridge import ResolveBridge

PLAN = {
    "versions": [
        {"name_zh": "一套配齐", "buyer_zh": "攒磨石的人", "motive_zh": "一次配齐", "recommended": True, "cta": "GET YOUR *202H* BELOW.",
         "caption_en": "One case. #KnifeSharpening", "caption_zh": "一箱装下。",
         "slides": [{"title_lines": ["STOP BUYING STONES", "*ONE BY ONE.*"], "body_lines": ["7 plates in one case"], "title_zh": "别一块块买",
                     "body_zh": "7 块磨板", "frame_time": 0.4, "picture_zh": "箱子", "crop_y": 0.2, "text_position": "bottom"},
                    {"title_lines": ["IT ALL PACKS", "BACK *IN.*"], "body_lines": ["3-year limited warranty", "GET YOUR *202H* BELOW."], "frame_time": 1.0}]},
        {"name_zh": "送礼", "slides": [{"title_lines": ["GIFT"], "frame_time": 99, "text_position": "nowhere"}]},
    ],
    "gaps_zh": ["没有荡刀特写"],
}


class Bridge:
    def __init__(self, output):
        self.output, self.calls = output, []

    def run(self, prompt, **options):
        self.calls.append((prompt, options))
        return self.output


class VideoFiles:
    def __init__(self, staged):
        self.staged = staged

    def temporary(self, path):
        return Path(path).resolve().parent == self.staged.resolve()


class Production:
    def job(self, identifier):
        return {"id": identifier, "product": "202H", "replica": {"case_id": "PVR-0123456789ab"},
                "timelines": [{"unique_id": "uid-1", "name": "Zack261104-202-1"}],
                "lines": [{"number": 1, "spoken": "All seven come in the case.", "zh": "七件都在箱子里。"}]}


class App:
    def __init__(self, root, output):
        self.runtime = root / "runtime"
        self.staged = root / "staged"
        self.staged.mkdir(parents=True)
        self.video_files = VideoFiles(self.staged)
        self.production = Production()
        self.store = type("Store", (), {"root": root / "cases"})()
        products = root / "cases" / "references" / "products"
        products.mkdir(parents=True)
        (products / "202H.md").write_text("# 202H\n7 块磨板", encoding="utf-8")
        self.ai = type("AI", (), {"bridge": Bridge(output), "skill_code_root": str(root / "skill" / "x"),
                                  "preferences": lambda self: {}})()


def wait(predicate, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError("timed out")


class RenderTests(unittest.TestCase):
    def test_words_marks_key_words(self):
        self.assertEqual(render.words("STOP *ONE BY* ONE."), [("STOP", False), ("ONE", True), ("BY", True), ("ONE.", False)])

    def test_every_style_and_position_gives_a_full_canvas(self):
        image = Image.new("RGB", (720, 1280), (90, 120, 150))
        for style in render.STYLES:
            for position, size in zip(render.POSITIONS, render.SIZES):
                slide = {"title_lines": ["A VERY LONG TITLE LINE THAT MUST SHRINK TO FIT", "*KEY*"], "body_lines": ["fact one", "fact two"],
                         "cta": "GET YOUR *202H* BELOW.", "crop_y": 0.3, "text_position": position, "text_size": size}
                out = render.render(slide, image, style, 5, 5)
                self.assertEqual(out.size, (render.W, render.H))

    def test_crop_keeps_requested_end(self):
        image = Image.new("RGB", (1080, 1920), "black")
        image.paste((255, 0, 0), (0, 0, 1080, 100))
        self.assertEqual(render.crop_fit(image, 0).getpixel((10, 10)), (255, 0, 0))
        self.assertNotEqual(render.crop_fit(image, 1).getpixel((10, 10)), (255, 0, 0))
        wide = Image.new("RGB", (2000, 1000), "black")
        wide.paste((0, 0, 255), (1900, 0, 2000, 1000))
        self.assertEqual(render.crop_fit(wide, 0.5, 1).getpixel((render.W - 5, 10)), (0, 0, 255))
        self.assertNotEqual(render.crop_fit(wide, 0.5, 0).getpixel((render.W - 5, 10)), (0, 0, 255))

    def test_bad_style_is_refused(self):
        with self.assertRaises(render.RenderError):
            render.render({"title_lines": ["X"]}, Image.new("RGB", (10, 10)), "neon", 1, 1)


class ParseTests(unittest.TestCase):
    def test_fenced_json_and_cleanup(self):
        versions, gaps = parse_plan("好的\n```json\n" + json.dumps(PLAN, ensure_ascii=False) + "\n```")
        self.assertEqual([v["key"] for v in versions], ["A", "B"])
        self.assertEqual(versions[1]["slides"][0]["text_position"], "split")
        self.assertEqual(sum(v["recommended"] for v in versions), 1)
        self.assertEqual(gaps, ["没有荡刀特写"])
        self.assertEqual(versions[0]["slides"][1]["body_lines"], ["3-year limited warranty"])  # the CTA is not a fact line

    def test_garbage_is_reported(self):
        with self.assertRaises(PhotoSetError):
            parse_plan("没有 JSON")

    def test_sharpest_moves_off_blur_only(self):
        items = [{"t": 1.0, "sharp": 10}, {"t": 1.2, "sharp": 100}, {"t": 1.4, "sharp": 95}]
        self.assertEqual(PhotoCarousels.sharpest(items, 1.0), 1.2)
        self.assertEqual(PhotoCarousels.sharpest(items, 1.4), 1.4)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = App(self.tmp, json.dumps(PLAN, ensure_ascii=False))
        self.sets = PhotoCarousels(self.app, export_root=self.tmp / "图文成品")
        self.video = self.app.staged / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=2",
                        "-pix_fmt", "yuv420p", str(self.video)], check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ready(self):
        created = self.sets.create("PRD-0123456789ab", {"timeline_uid": "uid-1", "video_path": str(self.video)})
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["frames_job"]["status"] != "running")
        return created["id"]

    def test_full_flow(self):
        set_id = self.ready()
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["frames_job"]["status"], "done")
        self.assertEqual(len(self.sets.frames(set_id)["items"]), 10)
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["ai"]["status"], "done", detail["ai"])
        prompt, options = self.app.ai.bridge.calls[0]
        self.assertIn("All seven come in the case.", prompt)
        self.assertTrue(options["images"])
        self.assertTrue((self.sets.root / set_id / "refs" / "202H.md").is_file())
        # B's frame time past the end snaps to the last frame
        self.assertLessEqual(detail["versions"][1]["slides"][0]["frame_time"], 1.8)

        state = self.sets.choose(set_id, {"versions": ["A", "B"]})
        self.assertEqual([(w["key"], w["status"]) for w in state["works"]], [("A", "doing"), ("B", "todo")])
        self.assertEqual(state["current"], "A")
        self.assertEqual(len(state["work"]["slides"]), 2)
        self.assertEqual(state["work"]["cta"], "GET YOUR *202H* BELOW.")
        with self.assertRaises(PhotoSetError):  # versions are made in order
            self.sets.open_work(set_id, {"key": "B"})
        state = self.sets.edit_slide(set_id, 1, {"image": {"kind": "frame", "t": 1.2}, "crop_y": 0.9, "text_position": "top", "text_size": "s"})
        self.assertEqual(state["work"]["slides"][0]["text_size"], "s")
        state = self.sets.edit_slide(set_id, 1, {"crop_y": 0.25, "crop_x": 1.7})
        self.assertEqual((state["work"]["slides"][0]["crop_y"], state["work"]["slides"][0]["crop_x"]), (0.25, 1.0))
        self.assertEqual(self.sets.base_path(set_id, "A", 1).name, "0007.jpg")
        self.assertEqual(state["work"]["slides"][0]["image"], {"kind": "frame", "t": 1.2})
        with self.assertRaises(PhotoSetError):
            self.sets.edit_slide(set_id, 1, {"image": {"kind": "frame", "t": 1.25}})
        photo = self.app.staged / "mine.png"
        Image.new("RGB", (720, 1280), "orange").save(photo)
        state = self.sets.edit_slide(set_id, 2, {"image": {"kind": "upload", "path": str(photo), "name": "mine.png"}})
        self.assertEqual(state["work"]["slides"][1]["image"]["kind"], "upload")
        state = self.sets.undo(set_id)
        self.assertEqual(state["work"]["slides"][1]["image"]["kind"], "frame")
        with self.assertRaises(PhotoSetError):
            self.sets.edit_slide(set_id, 1, {"image": {"kind": "upload", "path": str(self.tmp / "elsewhere.png")}})

        path = self.sets.rendered(set_id, "A", "bar", 2, 360)
        self.assertEqual(Image.open(path).size, (360, 480))
        self.assertEqual(self.sets.rendered(set_id, "A", "bar", 2, 360), path)  # cached

        result = self.sets.export(set_id)
        folder = Path(result["export"]["folder"])
        self.assertEqual(sorted(p.name for p in (folder / "发布JPG").iterdir()), ["01.jpg", "02.jpg"])
        copy_json = json.loads((folder / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual(copy_json["slides"][0]["title_lines"], ["STOP BUYING STONES", "ONE BY ONE."])
        self.assertEqual(copy_json["chosen_layout"], "黑色字条")
        self.assertIn("-A一套配齐", folder.name)
        state = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(state["work"]["status"], "done")
        self.assertEqual(state["next"]["key"], "B")
        second = Path(self.sets.export(set_id)["export"]["folder"])
        self.assertNotEqual(second, folder)
        self.assertTrue(folder.is_dir())

        state = self.sets.open_work(set_id, {"key": "B"})
        self.assertEqual((state["current"], state["work"]["status"]), ("B", "doing"))
        self.assertEqual(state["work"]["slides"][0]["image"]["kind"], "frame")  # B has its own pictures
        state = self.sets.open_work(set_id, {"key": "A"})  # finished versions can be reopened
        self.assertEqual(state["work"]["slides"][0]["image"], {"kind": "frame", "t": 1.2})
        with self.assertRaises(PhotoSetError):
            self.sets.remove_work(set_id, {"key": "A"})  # already exported
        state = self.sets.remove_work(set_id, {"key": "B"})
        self.assertEqual([w["key"] for w in state["works"]], ["A"])

    def test_legacy_single_choice_becomes_a_queue(self):
        snapshot = [{"title_lines": ["B ONE"], "body_lines": ["fact", "GET IT *NOW*."], "image": {"kind": "frame", "t": 0.2}}]
        legacy = {"versions": [{"key": "A", "name_zh": "甲", "cta": "GET IT *NOW*.", "slides": [{"title_lines": ["A ONE"], "frame_time": 0.0}]},
                               {"key": "B", "name_zh": "乙", "cta": "GET IT *NOW*.", "slides": [{"title_lines": ["B ONE"], "frame_time": 0.2}]}],
                  "chosen": "A", "style": "label", "cta": "GET IT *NOW*.",
                  "slides": [{"title_lines": ["A EDITED"], "body_lines": ["x"], "image": {"kind": "frame", "t": 0.4}}],
                  "history": [{"at": "2026-11-04T08:51:00", "label": "第 1 张：换图", "slides": snapshot},
                              {"at": "2026-11-04T08:53:00", "label": "换成第 A 版前", "slides": snapshot, "cta": "GET IT *NOW*."},
                              {"at": "2026-11-04T08:54:00", "label": "第 1 张：裁切", "slides": []}],
                  "exports": [{"at": "2026-11-04T08:50:00", "name": "PVR-x-20261104-B乙", "folder": "f", "style": "bar"}]}
        data = migrate(legacy)
        works = {w["key"]: w for w in data["works"]}
        self.assertEqual(data["current"], "A")
        self.assertEqual((works["B"]["status"], works["B"]["style"], len(works["B"]["exports"])), ("done", "bar", 1))
        self.assertEqual(works["B"]["slides"][0]["body_lines"], ["fact"])
        self.assertEqual((works["A"]["status"], works["A"]["style"], works["A"]["slides"][0]["title_lines"]), ("doing", "label", ["A EDITED"]))
        self.assertEqual([h["label"] for h in works["A"]["history"]], ["第 1 张：裁切"])
        self.assertNotIn("chosen", data)

    def test_restart_marks_running_work_failed(self):
        set_id = self.ready()
        data = self.sets._read(set_id)
        data["ai"] = {"status": "running"}
        self.sets._save(data)
        again = PhotoCarousels(self.app, export_root=self.tmp / "图文成品")
        self.assertEqual(again._read(set_id)["ai"]["status"], "failed")

    def test_new_video_replaces_old_set(self):
        first = self.ready()
        second = self.ready()
        self.assertNotEqual(first, second)
        self.assertEqual(self.sets.detail("PRD-0123456789ab", "uid-1")["id"], second)

    def test_video_must_be_a_staged_upload(self):
        outside = self.tmp / "outside.mp4"
        shutil.copy2(self.video, outside)
        with self.assertRaises(PhotoSetError):
            self.sets.create("PRD-0123456789ab", {"timeline_uid": "uid-1", "video_path": str(outside)})

    def bind_resolve(self, *, gate=None, fail=False):
        original = self.app.production.job
        self.app.production.job = lambda identifier: {**original(identifier), "timelines": [
            {"unique_id": "uid-1", "name": "Zack261104-202-1", "project": "202H", "project_id": "project-zack",
             "creator": "Zack", "folder": ["Zack"]}]}
        calls = []
        entered = threading.Event()
        def runner(command, **options):
            payload = json.loads(options["input"])
            calls.append(payload)
            if payload["command"] == "export_textless":
                photo_carousel._write_json(Path(payload["progress_path"]), {"message": "正在导出…", "percent": 42})
                entered.set()
                if gate:
                    gate.wait(10)
                if fail:
                    raise subprocess.TimeoutExpired(command, 1320)
                target = Path(payload["output_dir"]) / "textless.mp4"
                shutil.copy2(self.video, target)
                response = {"ok": True, "unique_id": "uid-1", "name": "Zack261104-202-1", "video_path": str(target), "restored": True}
            else:
                response = {"ok": True, "restored": True}
            return subprocess.CompletedProcess(command, 0, json.dumps(response).encode("utf-8"), b"")
        self.app.resolve = ResolveBridge(runner=runner)
        return calls, entered

    def test_resolve_import_is_bound_serialized_pollable_and_automatically_extracts(self):
        gate = threading.Event()
        self.addCleanup(gate.set)
        calls, entered = self.bind_resolve(gate=gate)
        started = self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
        self.assertFalse(started["exists"])
        self.assertEqual(started["resolve_import"]["status"], "running")
        self.assertTrue(entered.wait(5))
        self.assertTrue(self.app.resolve.busy())
        self.assertEqual(self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["percent"], 42)
        self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})  # duplicate stays on the same operation
        with self.assertRaises(PhotoSetError):
            self.sets.create("PRD-0123456789ab", {"timeline_uid": "uid-1", "video_path": str(self.video)})
        gate.set()
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["resolve_import"]["status"], "done", detail)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["frames_job"]["status"] != "running")
        self.assertEqual(self.sets.detail("PRD-0123456789ab", "uid-1")["frames_job"]["count"], 10)
        self.assertEqual(detail["video"]["resolve_source"]["timeline_uid"], "uid-1")
        self.assertEqual(detail["video"]["name"], "Zack261104-202-1-无字版.mp4")
        self.assertEqual(len(calls), 1)
        self.assertEqual({key: calls[0][key] for key in ("project_id", "folder", "creator", "unique_id")},
                         {"project_id": "project-zack", "folder": ["Zack"], "creator": "Zack", "unique_id": "uid-1"})
        self.assertFalse(self.app.resolve.busy())

    def test_resolve_timeout_attempts_recovery_keeps_old_set_and_allows_retry(self):
        first = self.ready()
        calls, _ = self.bind_resolve(fail=True)
        self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["resolve_import"]["status"], "failed")
        self.assertEqual(detail["id"], first)
        self.assertFalse(self.sets._read(first).get("discarded", False))
        self.assertEqual([call["command"] for call in calls], ["export_textless", "restore_textless"])
        self.assertFalse(self.app.resolve.busy())
        self.bind_resolve()
        self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["resolve_import"]["status"], "done", detail)
        self.assertNotEqual(detail["id"], first)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["frames_job"]["status"] != "running")

    def test_resolve_import_rejects_unknown_binding_and_client_supplied_paths(self):
        self.bind_resolve()
        for body in ({"timeline_uid": "other"}, {"timeline_uid": "uid-1", "output_dir": "elsewhere"}):
            with self.assertRaises(PhotoSetError):
                self.sets.import_resolve("PRD-0123456789ab", body)

    def test_resolve_import_waits_for_shared_lease_without_changing_tracks(self):
        calls, _ = self.bind_resolve()
        self.app.resolve.lock.acquire()
        try:
            self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
            wait(lambda: "等待" in self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["message"])
            self.assertFalse(calls)
        finally:
            self.app.resolve.lock.release()
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1").get("frames_job", {}).get("status") != "running")

    def test_unconfirmed_restore_never_replaces_the_previous_video(self):
        first = self.ready()
        self.bind_resolve()
        run = self.app.resolve.run
        def unconfirmed(command, *args, **kwargs):
            result = run(command, *args, **kwargs)
            return {**result, "restored": False} if command == "export_textless" else result
        with patch.object(self.app.resolve, "run", side_effect=unconfirmed):
            self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
            wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["resolve_import"]["status"], "failed")
        self.assertEqual(detail["id"], first)
        self.assertFalse(self.sets.busy())

    def test_failed_recovery_keeps_the_original_journal_across_retries(self):
        self.bind_resolve()
        folder = self.sets._import_folder("PRD-0123456789ab", "uid-1")
        original = folder / "original-run" / "recovery.json"
        original.parent.mkdir(parents=True)
        photo_carousel._write_json(original, {"restored": False})
        photo_carousel._write_json(folder / "job.json", {"status": "failed", "run_id": "original-run"})
        run, recoveries, failures = self.app.resolve.run, [], 2
        def recover(command, payload, **kwargs):
            nonlocal failures
            if command == "restore_textless":
                recoveries.append(payload["recovery_path"])
                if failures:
                    failures -= 1
                    raise RuntimeError("请先打开原项目后重试恢复")
            return run(command, payload, **kwargs)
        with patch.object(self.app.resolve, "run", side_effect=recover):
            for _ in range(3):
                self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
                wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        self.assertEqual(recoveries, [str(original)] * 3)
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["resolve_import"]["status"], "done", detail)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["frames_job"]["status"] != "running")


class PhotoRoutesTests(unittest.TestCase):
    setUp = http_fixture.WorkbenchHTTPTests.setUp
    stop_server = http_fixture.WorkbenchHTTPTests.stop_server
    request = http_fixture.WorkbenchHTTPTests.request

    def test_import_route_is_authenticated_and_returns_background_job(self):
        sets = self.server.app.photo_sets
        reply = {"exists": False, "resolve_import": {"status": "running", "message": "正在连接达芬奇…"}}
        route = "/api/production/jobs/PRD-0123456789ab/photo-set/from-resolve"
        with patch.object(sets, "import_resolve", return_value=reply) as start:
            status, _, body = self.request("POST", route, {"timeline_uid": "uid-2"}, authenticated=False)
            self.assertEqual(status, 403, body)
            start.assert_not_called()
            status, _, body = self.request("POST", route, {"timeline_uid": "uid-2"})
            self.assertEqual(status, 202, body)
            self.assertEqual(body, reply)
            start.assert_called_once_with("PRD-0123456789ab", {"timeline_uid": "uid-2"})
        with patch.object(sets, "detail", return_value=reply) as detail:
            status, _, body = self.request("GET", "/api/production/jobs/PRD-0123456789ab/photo-set?timeline=uid-2")
            self.assertEqual(status, 200, body)
            self.assertEqual(body, reply)
            detail.assert_called_once_with("PRD-0123456789ab", "uid-2")

    def test_running_photo_import_blocks_restart_and_data_replacement(self):
        with patch.object(self.server.app.photo_sets, "busy", return_value=True):
            self.assertIn("图文导入", self.server._activity_blocker())
            with self.assertRaisesRegex(Exception, "图文导入"):
                self.server.app.ensure_data_idle()


if __name__ == "__main__":
    unittest.main()
