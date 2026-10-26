"""视频转图文：成片抽帧去重，AI 写逐张表并选帧，建关联图文制作单，候选帧和合成图放进对应张。"""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_tasks import FakeBridge
import test_production_carousel_table as table_tests
import workbench_video_carousel as vc

PICKS = {"content_type": "frame_picks", "slides": [
    {"number": 1, "frames": ["F01", "F02"], "layout": "stack",
     "panels": [{"file": "F01", "focus_y": .4}, {"file": "F02"}], "caption_space": "top", "reason": "前后对比"},
    {"number": 2, "frames": ["F03"], "layout": "single", "panels": [{"file": "F03"}], "reason": "一帧说清"},
    {"number": 3, "frames": ["F99"], "layout": "single", "reason": "不存在的帧"}]}


def make_video(target):
    command = ["ffmpeg", "-v", "error", "-y",
               "-f", "lavfi", "-i", "color=c=red:size=360x640:rate=10:duration=1.5",
               "-f", "lavfi", "-i", "smptebars=size=360x640:rate=10:duration=1.5",
               "-f", "lavfi", "-i", "testsrc=size=360x640:rate=10:duration=1.5",
               "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0", "-pix_fmt", "yuv420p", str(target)]
    subprocess.run(command, check=True)
    return target


class HandoffTests(unittest.TestCase):
    def test_asset_paths_do_not_depend_on_the_analysis_working_directory(self):
        with tempfile.TemporaryDirectory(prefix="carousel sources ") as temporary:
            source = Path(temporary) / "成片资料"
            source.mkdir()
            frames = [{"name": "F01", "time": 0, "size": [360, 640], "line": 1}]
            handoff = vc.video_handoff({"id": "JOB-test", "lines": []}, {"product": "202H"},
                                      frames, [("sheets/sheet1.jpg", "F01", "F01")], duration=2,
                                      source_label="达芬奇时间线", slides_wanted=3, instruction="", note="",
                                      context_dir=source)
            for relative in ("sheets/sheet1.jpg", "frames", "frames/F03.jpg", "refs", "skill/references"):
                self.assertIn((source.resolve() / relative).as_posix(), handoff)
            self.assertNotIn("当前目录", handoff)
            prompt = vc.video_carousel_prompt(handoff)
            self.assertIn(handoff, prompt)
            self.assertIn("只读分析", prompt)
            self.assertIn(vc.PICKS_FORMAT, prompt)


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class FrameTests(unittest.TestCase):
    def test_repeated_frames_collapse_to_one_per_shot(self):
        with tempfile.TemporaryDirectory() as folder:
            video = make_video(Path(folder) / "v.mp4")
            samples, duration = vc.sample_frames(video, Path(folder) / "raw")
            self.assertGreaterEqual(len(samples), 8)
            kept = vc.distinct_frames(samples)
            self.assertLessEqual(len(kept), 4, [frame["time"] for frame in kept])
            self.assertGreaterEqual(len(kept), 2)
            for index, frame in enumerate(kept, 1):
                frame.update(name=f"F{index:02d}", file=frame["path"], line=1)
            sheets = vc.contact_sheets(kept, Path(folder) / "sheets")
            self.assertTrue((Path(folder) / sheets[0][0]).is_file())

    def test_line_at_follows_voiceover_starts(self):
        groups = [{"start": 0, "end": 1.2}, {"start": 1.4, "end": 3}, {"start": 3.1, "end": 4}]
        self.assertEqual([vc.line_at(t, groups) for t in (0, 1.3, 1.4, 3.5)], [1, 1, 2, 3])

    def test_frame_picks_drop_unknown_frames(self):
        text = "```json\n" + json.dumps(PICKS) + "\n```"
        picks = vc.parse_frame_picks(text, {"F01", "F02", "F03"})
        self.assertEqual(picks[1]["frames"], ["F01", "F02"])
        self.assertNotIn(3, picks)


@unittest.skipUnless(shutil.which("ffmpeg"), "需要 ffmpeg")
class VideoCarouselTests(table_tests.CarouselTableTests):
    # Reuse the table fixture (server, photo skill, script) without rerunning its tests.
    for _name in [name for name in dir(table_tests.CarouselTableTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name

    def job(self):
        script = self.script()
        created = self.ok("POST", "/api/production/jobs", {"request_id": uuid4().hex,
                          "revision": self.ok("GET", "/api/production")["revision"], "job": {"script_id": script["id"]}})
        return created["job"]

    def wait_video(self, job_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            state = self.ok("GET", f"/api/production/jobs/{job_id}/video-carousel")
            # create_carousel clears the live marker a moment before the run's final write adds problems and finished_at.
            if not state["running"] and all(run["finished_at"] for run in state["runs"]):
                return state
            time.sleep(.1)
        self.fail("转图文没有结束")

    def test_finished_video_becomes_a_linked_carousel_with_frames_and_a_composite(self):
        bridge = FakeBridge(output=table_tests.TABLE + "\n```json\n" + json.dumps(PICKS, ensure_ascii=False) + "\n```\n")
        self.server.app.ai.bridge = bridge
        job = self.job()
        with tempfile.TemporaryDirectory() as folder:
            video = make_video(Path(folder) / "成片.mp4")
            self.ok("POST", f"/api/production/jobs/{job['id']}/video-carousel",
                    {"request_id": uuid4().hex, "file_path": str(video), "slides": 3, "instruction": "首图用对比", "autopilot": False})
            state = self.wait_video(job["id"])
        run = state["runs"][0]
        self.assertEqual(run["status"], "completed", run)
        self.assertTrue(run["carousel_id"])
        prompt = bridge.calls[-1]["prompt"]
        for fragment in ("成片帧", "sheets/sheet1.jpg", "frame_picks", "首图用对比", "Seven plates. Overkill?", "3 张"):
            self.assertIn(fragment, prompt)
        source = Path(bridge.calls[-1]["cwd"])
        for relative in ("sheets/sheet1.jpg", "frames/F03.jpg", "refs", "skill/references"):
            self.assertTrue((source / relative).exists(), relative)
            self.assertIn((source / relative).as_posix(), prompt)
        carousel = self.ok("GET", f"/api/production/carousels/{run['carousel_id']}")["carousel"]
        self.assertEqual(carousel["source"]["video_job_id"], job["id"])
        slides = {slide["number"]: slide for slide in carousel["slides"]}
        self.assertEqual([c["origin"] for c in slides[1]["candidates"]], ["video_frame", "video_frame", "composite"])
        composite = slides[1]["candidates"][-1]
        self.assertEqual(composite["layout"], "stack")
        self.assertEqual(composite["composed_from"], [c["id"] for c in slides[1]["candidates"][:2]])
        # A single frame is composed too, so its crop and caption band are settled when the base is chosen.
        self.assertEqual([c["origin"] for c in slides[2]["candidates"]], ["video_frame", "composite"])
        single = slides[2]["candidates"][-1]
        self.assertEqual(single["layout"], "single")
        self.assertEqual(single["composed_from"], [slides[2]["candidates"][0]["id"]])
        self.assertEqual(single["compose_layout"]["size"], [1536, 2048])
        self.assertEqual(slides[3]["candidates"], [])
        self.assertTrue(any("第 3 张" in item for item in run["problems"]), run)
        image = self.request("GET", f"/api/production/carousels/{run['carousel_id']}/images/{composite['id']}?size=full")
        self.assertEqual(image[0], 200)
        self.assertFalse(run['autopilot'])
        self.assertFalse(run.get('delivery_id'))
        self.assertFalse(carousel.get('autopilot_runs'))

    def configure_full_autopilot(self, *, wrong_words=False):
        from test_production_carousel_images import ImageGenerator
        picks = json.loads(json.dumps(PICKS))
        picks['slides'][2].update(frames=['F03'], panels=[{'file': 'F03'}], reason='整套收口')
        bridge = FakeBridge(output=table_tests.TABLE + '\n```json\n' + json.dumps(picks, ensure_ascii=False) + '\n```\n')
        self.server.app.ai.bridge = bridge
        generator = ImageGenerator()
        if wrong_words:
            generator.review_mode = 'wrong_words'
        self.server.app.image_generator = generator
        self.server.app.image_reviewer = generator
        self.addCleanup(self.server.app.stop_carousel_autopilots)
        self.addCleanup(generator.release.set)
        return generator

    def test_video_autopilot_remains_running_until_real_package_and_keeps_its_link(self):
        generator = self.configure_full_autopilot()
        from test_carousel_prompt_planning import PromptPlanner
        self.server.app.image_reviewer = PromptPlanner()
        app, job = self.server.app, self.job()
        at_package, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = app._autopilot_update

        def update(identifier, run_id, **fields):
            result = original(identifier, run_id, **fields)
            if fields.get('phase') == '整理成品':
                at_package.set()
                if not release.wait(15):
                    raise RuntimeError('隔离测试：成品打包放行超时')
            return result

        with tempfile.TemporaryDirectory() as folder, patch.object(app, '_autopilot_update', side_effect=update):
            video = make_video(Path(folder) / '成片.mp4')
            body = {'request_id': uuid4().hex, 'file_path': str(video), 'slides': 3, 'autopilot': True,
                    'workflow': 'generation_first'}
            path = f"/api/production/jobs/{job['id']}/video-carousel"
            self.ok('POST', path, body)
            try:
                self.assertTrue(at_package.wait(20), self.ok('GET', path))
                state = self.ok('GET', path)
                run = state['runs'][0]
                self.assertTrue(state['running'])
                self.assertEqual(run['status'], 'running')
                self.assertTrue(run['autopilot'])
                self.assertTrue(run['carousel_id'])
                self.assertFalse(run.get('delivery_id'))
                self.assertFalse(run['finished_at'])
                replay = self.ok('POST', path, body)
                self.assertEqual([record['id'] for record in replay['runs']], [run['id']])
                carousel = self.ok('GET', f"/api/production/carousels/{run['carousel_id']}")['carousel']
                self.assertEqual(carousel['autopilot_runs'][-1]['status'], 'running')
                self.assertEqual(len(carousel.get('deliveries', [])), 0)
                for slide in carousel['slides']:
                    output = next(output for output in slide['outputs'] if output['id'] == slide['output_id'])
                    self.assertTrue(output['approved'])
                    self.assertEqual(output['approval_actor'], 'assistant_on_user_authorization')
                # The table remains a valid route back to work already created, even before packaging.
                table = self.ok('GET', f"/api/production/carousel-tables/{run['id']}")['table']
                self.assertEqual(table['status'], 'running')
            finally:
                release.set()
                completed = self.wait_video(job['id'])
        run = completed['runs'][0]
        self.assertEqual(run['status'], 'completed', run)
        self.assertFalse(completed['running'])
        self.assertTrue(run['delivery_id'])
        carousel = self.ok('GET', f"/api/production/carousels/{run['carousel_id']}")['carousel']
        auto = carousel['autopilot_runs'][-1]
        self.assertEqual((auto['status'], auto['delivery_id']), ('completed', run['delivery_id']))
        response = self.request('GET', f"/api/production/carousels/{run['carousel_id']}/deliveries/{run['delivery_id']}/download")
        self.assertEqual(response[0], 200)
        self.assertTrue(response[2].startswith(b'PK'))
        self.assertEqual(sum(not call['read_only'] for call in generator.calls), 3)
        self.assertEqual(table['carousel_id'], run['carousel_id'])

    def test_default_video_autopilot_captions_the_composed_bases_and_packages(self):
        generator = self.configure_full_autopilot()
        generator.supports_cutout = True
        cutouts = []

        def cutout(bridge, base, cache, **options):
            # The cutout itself is covered by the cutout suite; here the subject is the frame's centre.
            from PIL import Image as Picture
            cutouts.append(Path(base).name)
            with Picture.open(base) as image:
                width, height = image.size
            alpha = Picture.new("L", (width, height), 0)
            alpha.paste(255, (width // 4, height // 3, width * 3 // 4, height * 2 // 3))
            cache.mkdir(parents=True, exist_ok=True)
            Picture.merge("RGBA", (alpha, alpha, alpha, alpha)).save(cache / "mask.png")
            (cache / "metadata.json").write_text("{}", encoding="utf-8")

        job = self.job()
        with tempfile.TemporaryDirectory() as folder, patch("carousel_cutout.refine_subject_mask", side_effect=cutout):
            video = make_video(Path(folder) / '成片.mp4')
            self.ok('POST', f"/api/production/jobs/{job['id']}/video-carousel",
                    {'request_id': uuid4().hex, 'file_path': str(video), 'slides': 3, 'autopilot': True})
            run = self.wait_video(job['id'])['runs'][0]
        self.assertEqual(run['status'], 'completed', run)
        self.assertTrue(run['delivery_id'])
        self.assertTrue(run['autopilot_id'])
        carousel = self.ok('GET', f"/api/production/carousels/{run['carousel_id']}")['carousel']
        self.assertEqual(carousel['autopilot_runs'][-1]['status'], 'completed')
        self.assertEqual(carousel['requirements']['caption_layer'], 'behind_subject')
        self.assertEqual(carousel['requirements']['mode'], 'layout_only')
        # Only local layout and read-only checks: nothing was sent to image generation.
        self.assertEqual([call for call in generator.calls if not call['read_only']], [])
        for slide in carousel['slides']:
            output = next(entry for entry in slide['outputs'] if entry['id'] == slide['output_id'])
            self.assertTrue(output['approved'])
            self.assertEqual(output['layout_receipt']['method'], 'caption_on_base')
            self.assertEqual(output['width'] * 4, output['height'] * 3)
        self.assertEqual(cutouts, [])

    def test_frames_come_straight_from_the_davinci_timeline_and_the_view_is_restored(self):
        from resolve_bridge import ResolveBridge
        bridge = FakeBridge(output=table_tests.TABLE + "\n```json\n" + json.dumps(PICKS, ensure_ascii=False) + "\n```\n")
        self.server.app.ai.bridge = bridge
        job = self.job()
        calls = []
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        video = make_video(Path(scratch.name) / "timeline.mp4")

        def runner(args, input, **options):
            request = json.loads(input.decode("utf-8"))
            calls.append(request)
            if request["command"] == "status":
                reply = {"project": job_product, "current_timeline_uid": "OLD", "current_timeline": "我的时间线", "current_timecode": "01:00:00:05"}
            elif request["command"] == "timeline_stills":
                folder = Path(request["folder"])
                folder.mkdir(parents=True)
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf", "fps=2", str(folder / "%d.png")], check=True)
                files = sorted(folder.glob("*.png"), key=lambda path: int(path.stem))
                reply = {"name": "TL", "fps": 10.0, "start_frame": 0, "end_frame": len(files) * 5,
                         "files": [{"offset": index * 5, "file": str(path)} for index, path in enumerate(files)], "skipped": 0}
            else:
                reply = {"name": "我的时间线"}
            return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, **reply}).encode("utf-8"), b"")

        self.server.app.resolve = ResolveBridge(runner=runner)
        original = self.server.app.production.job
        from production_store import product_key
        job_product = product_key(self.server.app.creation.get_script(job["script_id"])["product"])
        self.server.app.production.job = lambda identifier: {**original(identifier), "timelines": [
            {"unique_id": "TL-1", "name": "Lab-1", "take_id": "x", "lead_frames": 0}]}
        state = self.ok("GET", f"/api/production/jobs/{job['id']}/video-carousel")
        self.assertEqual(state["source"]["kind"], "timeline")
        self.assertEqual(state["source"]["timeline"], "TL-1")
        self.ok("POST", f"/api/production/jobs/{job['id']}/video-carousel", {"request_id": uuid4().hex, "slides": 3})
        run = self.wait_video(job["id"])["runs"][0]
        self.assertEqual(run["status"], "completed", run)
        self.assertIn("达芬奇时间线", run["video_source"])
        commands = [call["command"] for call in calls]
        self.assertEqual(commands, ["status", "timeline_stills", "restore"])
        self.assertEqual(calls[2]["unique_id"], "OLD")
        self.assertEqual(calls[2]["timecode"], "01:00:00:05")
        self.assertFalse(self.server.app.resolve.lock.locked())

    def test_without_a_finished_video_nothing_starts(self):
        self.server.app.ai.bridge = FakeBridge(output="")
        job = self.job()
        status, _, data = self.request("POST", f"/api/production/jobs/{job['id']}/video-carousel", {"request_id": uuid4().hex})
        self.assertEqual(status, 409, data)
        self.assertIn("成片", data["error"])


class VideoCarouselQueueTests(unittest.TestCase):
    """Real queue/worker threads, with only Resolve and AI replaced by local fakes."""

    fixture = table_tests.CarouselTableTests.fixture
    stop_server = table_tests.CarouselTableTests.stop_server
    request = table_tests.CarouselTableTests.request
    ok = table_tests.CarouselTableTests.ok
    script = table_tests.CarouselTableTests.script
    job = VideoCarouselTests.job
    wait_video = VideoCarouselTests.wait_video

    def setUp(self):
        from resolve_bridge import ResolveBridge
        table_tests.CarouselTableTests.setUp(self)
        self.app = self.server.app
        self.bridge = FakeBridge(output=table_tests.TABLE + "\n```json\n" + json.dumps(PICKS) + "\n```\n")
        self.app.ai.bridge = self.bridge
        self.current_job = self.job()
        self.commands = []
        self.view = {"unique_id": "BEFORE", "name": "等待前的时间线", "timecode": "01:00:00:01"}
        self.app.resolve = ResolveBridge(runner=self.resolve_runner)
        original = self.app.production.job
        self.app.production.job = lambda identifier: {**original(identifier), "timelines": [
            {"unique_id": "TL-1", "name": "Lab-1", "take_id": "x", "lead_frames": 0}]}
        self.addCleanup(self.app.stop_carousel_tables)
        self.endpoint = f"/api/production/jobs/{self.current_job['id']}/video-carousel"

    @staticmethod
    def samples(folder):
        from PIL import Image
        folder.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, color in enumerate(("red", "blue", "green")):
            path = folder / f"{index}.png"
            Image.new("RGB", (120, 160), color).save(path)
            paths.append((index * .5, path))
        return paths, 1.5

    def resolve_runner(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.commands.append(request)
        self.assertTrue(self.app.resolve.busy())
        if request["command"] == "status":
            reply = {"project": "202H", "current_timeline_uid": self.view["unique_id"],
                     "current_timeline": self.view["name"], "current_timecode": self.view["timecode"]}
        elif request["command"] == "timeline_stills":
            samples, duration = self.samples(Path(request["folder"]))
            reply = {"fps": 10, "start_frame": 0, "end_frame": round(duration * 10),
                     "files": [{"offset": round(seconds * 10), "file": str(path)} for seconds, path in samples]}
        else:
            reply = {}
        return subprocess.CompletedProcess(args, 0, json.dumps({"ok": True, **reply}).encode("utf-8"), b"")

    def wait_queued(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.ok("GET", self.endpoint)
            if state["runs"] and "等待达芬奇" in state["runs"][0].get("progress", ""):
                return state["runs"][0]
            time.sleep(.02)
        self.fail("取帧任务没有进入达芬奇等待队列")

    def test_busy_timeline_queues_and_snapshots_view_only_after_admission(self):
        ai_busy = []
        original_run = self.bridge.run

        def analyze(*args, **options):
            ai_busy.append(self.app.resolve.busy())
            return original_run(*args, **options)

        self.app.resolve.lock.acquire()
        held = True
        try:
            with patch.object(self.bridge, "run", side_effect=analyze):
                body = {"request_id": uuid4().hex, "slides": 3}
                started = self.ok("POST", self.endpoint, body)
                self.assertTrue(started["running"])
                queued = self.wait_queued()
                self.assertEqual(self.commands, [])
                self.assertEqual(self.bridge.calls, [])
                replay = self.ok("POST", self.endpoint, body)
                self.assertEqual(replay["runs"][0]["id"], queued["id"])
                self.view.update(unique_id="AFTER", name="剪辑交付后的时间线", timecode="01:00:12:03")
                self.app.resolve.lock.release()
                held = False
                run = self.wait_video(self.current_job["id"])["runs"][0]
        finally:
            if held:
                self.app.resolve.lock.release()
        self.assertEqual(run["status"], "completed", run)
        self.assertEqual([entry["command"] for entry in self.commands], ["status", "timeline_stills", "restore"])
        self.assertEqual(self.commands[-1]["unique_id"], "AFTER")
        self.assertEqual(self.commands[-1]["timecode"], "01:00:12:03")
        self.assertEqual(ai_busy, [False])
        self.assertFalse(self.app.resolve.busy())

    def test_cancelled_waiter_never_touches_resolve_or_releases_foreign_lock(self):
        self.app.resolve.lock.acquire()
        try:
            self.ok("POST", self.endpoint, {"request_id": uuid4().hex})
            queued = self.wait_queued()
            self.app.cancel_carousel_table(queued["id"])
            run = self.wait_video(self.current_job["id"])["runs"][0]
            self.assertEqual(run["status"], "cancelled", run)
            self.assertTrue(self.app.resolve.busy())
            self.assertEqual(self.commands, [])
            self.assertEqual(self.bridge.calls, [])
        finally:
            self.app.resolve.lock.release()

    def test_local_video_continues_without_resolve_while_edit_holds_it(self):
        local = self.root / "成片.mp4"
        local.write_bytes(b"local video fixture")
        self.app.resolve.lock.acquire()
        try:
            with patch.object(vc, "sample_frames", side_effect=lambda path, folder: self.samples(folder)):
                self.ok("POST", self.endpoint, {"request_id": uuid4().hex, "file_path": str(local)})
                run = self.wait_video(self.current_job["id"])["runs"][0]
            self.assertEqual(run["status"], "completed", run)
            self.assertTrue(self.app.resolve.busy())
            self.assertEqual(self.commands, [])
        finally:
            self.app.resolve.lock.release()

    def test_worker_start_failure_is_saved_without_touching_foreign_lock_and_can_retry(self):
        from elevenlabs_client import VoiceError
        self.app.resolve.lock.acquire()
        held = True
        try:
            with patch("workbench_video_carousel.threading.Thread.start", side_effect=RuntimeError("thread failed")):
                with self.assertRaises(VoiceError) as caught:
                    self.app.start_video_carousel(self.current_job["id"], {"request_id": uuid4().hex})
            self.assertEqual(caught.exception.status, 503)
            state = self.app.video_carousel_status(self.current_job["id"])
            failed = state["runs"][0]
            self.assertFalse(state["running"])
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(failed["finished_at"])
            self.assertIn("未能启动", failed["error"])
            self.assertNotIn(failed["id"], self.app._table_state())
            self.assertNotIn(failed["id"], self.app._table_cancel)
            self.assertTrue(self.app.resolve.busy())
            self.assertEqual(self.commands, [])
            self.assertEqual(self.bridge.calls, [])
            self.ok("POST", self.endpoint, {"request_id": uuid4().hex})
            queued = self.wait_queued()
            self.assertNotEqual(queued["id"], failed["id"])
            self.app.resolve.lock.release()
            held = False
            completed = self.wait_video(self.current_job["id"])["runs"][0]
            self.assertEqual(completed["status"], "completed", completed)
        finally:
            if held:
                self.app.resolve.lock.release()


if __name__ == "__main__":
    unittest.main()
