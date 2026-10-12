"""Base frames cut straight from labeled material clips: listing and ranking, frame cache, crop/rotate, replay, reveal."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from PIL import Image

from elevenlabs_client import VoiceError
from resolve_bridge import ResolveBridge
from test_production import Bench, FakeEleven
from test_production_carousel import CANDIDATE
from variant_parse import carousel_text, parse_carousels
from workbench_carousel import CarouselWorkbenchMixin
from workbench_carousel_clips import CarouselClipsMixin, _clean_adjustments, apply_adjustments
from workbench_carousel_stills import CarouselStillsMixin

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


class Runner:
    def __init__(self, media):
        self.calls, self.media = [], media

    def __call__(self, args, input, **options):
        request = json.loads(input.decode("utf-8"))
        self.calls.append(request)
        if request["command"] == "candidates":
            body = {"ok": True, "missing": [], "used": [], "used_complete": True, "materials": [
                {"name": "蓝点折刀成品素材", "unique_id": "uid-material", "start_frame": 86400, "fps": "30", "drop": False, "clips": [
                    {"uid": "clip-a", "start": 86400, "end": 86490, "name": "a.mp4", "media_id": "m1", "media_path": str(self.media),
                     "source_start": 30, "source_end": 119, "has_audio": False, "labels": [self.labels[0]],
                     "verified_labels": [self.labels[0]]},
                    {"uid": "clip-b", "start": 86490, "end": 86580, "name": "b.mp4", "media_id": "m1", "media_path": str(self.media),
                     "source_start": 150, "source_end": 239, "has_audio": False, "labels": [self.labels[1]], "verified_labels": []}]}]}
        elif request["command"] == "reveal":
            body = {"ok": True, "name": request["name"], "timecode": "01:00:03:00"}
        else:
            body = {"ok": True}
        return subprocess.CompletedProcess(args, 0, json.dumps(body, ensure_ascii=False).encode("utf-8"), b"")


class FakeAI:
    """Stands in for the default AI client: records the prompt and answers with a canned pick."""

    def __init__(self):
        self.calls, self.answer = [], None

    def run(self, prompt, **options):
        self.calls.append({"prompt": prompt, **options})
        return self.answer(prompt) if callable(self.answer) else self.answer


class ClipBench(Bench, CarouselWorkbenchMixin, CarouselStillsMixin, CarouselClipsMixin):
    def __init__(self, root, media):
        super().__init__(root, FakeEleven())
        self.fake_ai = FakeAI()
        self.ai = SimpleNamespace(resolve_default_settings=lambda body, effort_key: {"model": None, effort_key: None},
                                  bridge=self.fake_ai)
        self.runner = Runner(media)
        self.resolve = ResolveBridge(runner=self.runner)
        self.carousel_image_root = Path(root) / "图文素材"


@unittest.skipUnless(HAVE_FFMPEG, "需要本机 ffmpeg/ffprobe")
class ClipTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="carousel-clips-")
        self.addCleanup(temp.cleanup)
        self.media = Path(temp.name) / "source.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=10",
                        "-pix_fmt", "yuv420p", "-g", "15", str(self.media)], check=True)
        self.bench = ClipBench(temp.name, self.media)
        found = parse_carousels(CANDIDATE)[0]
        source = {"task_id": "AIT-0123456789ab", "round_id": "AIR-0123456789ab", "task_revision": 1, "product": "202H",
                  "case_id": None, "candidate_text": CANDIDATE, "candidate_sha256": "x", "snapshot": {}}
        script = self.bench.creation.mutate("script", {"request_id": uuid4().hex, "revision": self.bench.creation.public()["revision"],
            "script": {"product": "202H", "direction_id": "202H-S2", "title": "送礼 · 图文", "text": carousel_text(found), "confirmed": True}},
            source)["script"]
        self.item = self.bench.create_carousel({"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                "carousel": {"script_id": script["id"]}})["carousel"]
        first = self.item["slides"][0]
        self.bench.runner.labels = [f"{first['source']} ZS161", "完全无关 XQ999"]
        # product_settings needs a project; the fake Resolve answers any.
        self.materials = ["蓝点折刀成品素材"]

    def listing(self, **options):
        return self.bench.carousel_clips(self.item["id"], self.materials, **options)

    def test_listing_ranks_by_slide_words_and_is_cached(self):
        listing = self.listing()
        self.assertEqual(len(listing["clips"]), 2)
        self.assertEqual(listing["suggested"]["1"][0].split("-")[0], "ZS161")
        self.assertNotIn(next(row["key"] for row in listing["clips"] if row["label"].endswith("XQ999")), listing["suggested"]["1"])
        calls = len([call for call in self.bench.runner.calls if call["command"] == "candidates"])
        self.listing()
        self.assertEqual(len([call for call in self.bench.runner.calls if call["command"] == "candidates"]), calls)
        self.listing(refresh=True)
        self.assertEqual(len([call for call in self.bench.runner.calls if call["command"] == "candidates"]), calls + 1)

    def test_frame_is_cut_from_the_source_file_and_cached(self):
        key = self.listing()["clips"][0]["key"]
        info = self.bench.clip_info(key)
        self.assertEqual(info["source"], [30, 120])
        self.assertAlmostEqual(info["fps"], 30, places=1)
        path = self.bench.clip_frame(self.item["id"], key, 60, "view")
        with Image.open(path) as image:
            self.assertEqual(image.size, (320, 240))
        self.assertEqual(self.bench.clip_frame(self.item["id"], key, 60, "view"), path)
        with self.assertRaises(VoiceError) as caught:
            self.bench.clip_frame(self.item["id"], key, 500, "view")
        self.assertEqual(caught.exception.status, 400)
        with self.assertRaises(VoiceError) as caught:
            self.bench.clip_frame(self.item["id"], "ZZ1-00000000", 60, "view")
        self.assertEqual(caught.exception.status, 409)

    def test_strip_is_one_contact_sheet_of_evenly_spaced_frames(self):
        key = self.listing()["clips"][0]["key"]
        info = self.bench.clip_info(key)
        self.assertEqual(len(info["strip"]), 6)
        self.assertEqual((info["strip"][0], info["strip"][-1]), (0, info["frames"] - 1))
        path = self.bench.clip_strip(self.item["id"], key)
        with Image.open(path) as sheet:
            self.assertEqual(sheet.size[0], 6 * 180)
            self.assertIn(sheet.size[1], (134, 135, 136))
        self.assertEqual(self.bench.clip_strip(self.item["id"], key), path)

    def test_use_frame_applies_crop_and_rotation_and_replays(self):
        key = self.listing()["clips"][0]["key"]
        body = {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": 60, "rotate": 90, "brightness": 1.2,
                "crop": {"x": 0, "y": 0.1, "w": 1, "h": 0.5}, "evidence": True}
        result = self.bench.use_clip_frame(self.item["id"], body)
        candidate = result["candidate"]
        self.assertEqual((candidate["origin"], candidate["slide"], candidate["clip_key"], candidate["source_frame"]), ("clip", 1, key, 60))
        self.assertTrue(candidate["evidence"])
        self.assertIn("旋转 90°", candidate["reason"])
        # 320x240 rotated to 240x320, then half the height kept.
        self.assertEqual((candidate["width"], candidate["height"]), (240, 160))
        again = self.bench.use_clip_frame(self.item["id"], body)
        self.assertEqual(again["candidate"]["id"], candidate["id"])
        carousel = self.bench.production.carousel(self.item["id"])
        self.assertEqual(len(carousel["slides"][0]["candidates"]), 1)
        picked = self.bench.choose_base(self.item["id"], {"request_id": uuid4().hex, "revision": self.bench.production.public()["revision"],
                                                          "slide": 1, "candidate": candidate["id"]})
        self.assertEqual(picked["carousel"]["slides"][0]["base"]["id"], candidate["id"])
        self.assertTrue(self.bench.carousel_image(self.item["id"], candidate["id"], "full").is_file())

    def test_choosing_a_base_keeps_only_that_image_for_the_slide(self):
        key = self.listing()["clips"][0]["key"]
        made = [self.bench.use_clip_frame(self.item["id"], {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": frame})["candidate"]
                for frame in (45, 60, 75)]
        root = self.bench._carousel_dir(self.item["id"])
        self.assertTrue(all((root / entry["file"]).is_file() for entry in made))
        revision = lambda: self.bench.production.public()["revision"]
        self.bench.choose_base(self.item["id"], {"request_id": uuid4().hex, "revision": revision(), "slide": 1, "candidate": made[1]["id"]})
        slide = self.bench.production.carousel(self.item["id"])["slides"][0]
        self.assertEqual([entry["id"] for entry in slide["candidates"]], [made[1]["id"]])
        self.assertTrue((root / made[1]["file"]).is_file())
        for gone in (made[0], made[2]):
            self.assertFalse((root / gone["file"]).exists())
            self.assertFalse((root / gone["thumb"]).exists())
        # Switching to another image: the earlier base goes, the new one stays.
        other = self.bench.use_clip_frame(self.item["id"], {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": 90})["candidate"]
        self.bench.choose_base(self.item["id"], {"request_id": uuid4().hex, "revision": revision(), "slide": 1, "candidate": other["id"]})
        slide = self.bench.production.carousel(self.item["id"])["slides"][0]
        self.assertEqual([entry["id"] for entry in slide["candidates"]], [other["id"]])
        self.assertEqual(slide["base"]["id"], other["id"])
        self.assertFalse((root / made[1]["file"]).exists())
        self.assertTrue((root / other["file"]).is_file())

    def test_tidy_drops_leftover_candidates_of_slides_that_have_a_base(self):
        key = self.listing()["clips"][0]["key"]
        revision = lambda: self.bench.production.public()["revision"]
        first = self.bench.use_clip_frame(self.item["id"], {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": 45})["candidate"]
        self.bench.choose_base(self.item["id"], {"request_id": uuid4().hex, "revision": revision(), "slide": 1, "candidate": first["id"]})
        extra = self.bench.use_clip_frame(self.item["id"], {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": 60})["candidate"]
        loose = self.bench.use_clip_frame(self.item["id"], {"request_id": uuid4().hex, "slide": 2, "key": key, "frame": 70})["candidate"]
        root = self.bench._carousel_dir(self.item["id"])
        result = self.bench.tidy_candidates(self.item["id"], {"request_id": uuid4().hex})
        self.assertEqual(result["removed"], 1)
        slides = self.bench.production.carousel(self.item["id"])["slides"]
        self.assertEqual([entry["id"] for entry in slides[0]["candidates"]], [first["id"]])
        self.assertEqual([entry["id"] for entry in slides[1]["candidates"]], [loose["id"]])
        self.assertFalse((root / extra["file"]).exists())
        self.assertTrue((root / first["file"]).is_file() and (root / loose["file"]).is_file())
        self.assertEqual(self.bench.tidy_candidates(self.item["id"], {"request_id": uuid4().hex})["removed"], 0)

    def test_ai_picks_clips_from_labels_without_exporting_frames(self):
        rows = self.listing()["clips"]
        first, second = rows[0]["key"], rows[1]["key"]
        payload = {"slides": {"1": [{"key": first, "reason": "标签写了打开的箱子"}, {"key": "NOPE-00000000", "reason": "编造"},
                                    {"key": second, "reason": "备选"}], "2": []},
                   "gaps": [{"slide": 3, "need": "收口要看到一排刀"}]}
        self.bench.fake_ai.answer = "好的。```json" + json.dumps(payload, ensure_ascii=False) + "```"
        result = self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials, "per_slide": 2})
        self.assertEqual([entry["key"] for entry in result["picks"]["1"]], [first, second])
        self.assertEqual(result["picks"]["2"], [])
        self.assertEqual(sorted(gap["slide"] for gap in result["gaps"]), [2, 3])
        prompt = self.bench.fake_ai.calls[0]["prompt"]
        self.assertIn(first, prompt)
        self.assertIn("| 张 | 作用 |", prompt)
        self.assertEqual([call["command"] for call in self.bench.runner.calls], ["candidates"])  # no frame export, no timeline switch
        listing = self.listing()
        self.assertEqual([entry["key"] for entry in listing["ai_picks"]["1"]], [first, second])
        self.assertEqual(listing["ai_gaps"][0]["slide"] in (2, 3), True)
        detail = self.bench.carousel_detail(self.item["id"])
        self.assertEqual(detail["clip_picks"]["counts"]["1"], 2)

    def test_unreadable_ai_answer_is_an_error_and_keeps_earlier_picks(self):
        key = self.listing()["clips"][0]["key"]
        self.bench.fake_ai.answer = json.dumps({"slides": {"1": [{"key": key, "reason": "ok"}]}})
        self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials})
        self.bench.fake_ai.answer = "我没法回答"
        with self.assertRaises(VoiceError) as caught:
            self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials})
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(self.listing()["ai_picks"]["1"][0]["key"], key)
        with self.assertRaises(VoiceError) as caught:
            self.bench.ai_pick_clips(self.item["id"], {"materials": self.materials, "per_slide": 9})
        self.assertEqual(caught.exception.status, 400)

    def test_uploaded_picture_becomes_a_candidate_and_bad_files_are_refused(self):
        import io
        buffer = io.BytesIO()
        Image.new("RGB", (90, 120), "orange").save(buffer, "PNG")
        data, request_id = buffer.getvalue(), uuid4().hex
        result = self.bench.import_uploaded_image(self.item["id"], 2, "我的 图片.png", data, request_id)
        candidate = result["candidate"]
        self.assertEqual((candidate["origin"], candidate["slide"], candidate["width"], candidate["height"]), ("upload", 2, 90, 120))
        self.assertTrue((self.bench._carousel_dir(self.item["id"]) / candidate["file"]).is_file())
        again = self.bench.import_uploaded_image(self.item["id"], 2, "我的 图片.png", data, request_id)
        self.assertEqual(again["candidate"]["id"], candidate["id"])
        self.assertEqual(len(self.bench.production.carousel(self.item["id"])["slides"][1]["candidates"]), 1)
        for name, payload, slide, status in (("x.png", b"not an image", 2, 400), ("x.png", data, 9, 400)):
            with self.assertRaises(VoiceError) as caught:
                self.bench.import_uploaded_image(self.item["id"], slide, name, payload, uuid4().hex)
            self.assertEqual(caught.exception.status, status)
        gif = io.BytesIO()
        Image.new("RGB", (8, 8)).save(gif, "GIF")
        with self.assertRaises(VoiceError) as caught:
            self.bench.import_uploaded_image(self.item["id"], 2, "x.gif", gif.getvalue(), uuid4().hex)
        self.assertEqual(caught.exception.status, 400)

    def test_bad_adjustments_and_slide_are_refused(self):
        key = self.listing()["clips"][0]["key"]
        for patch_body, status in (({"crop": {"x": 0.8, "y": 0, "w": 0.5, "h": 1}}, 400), ({"rotate": 45}, 400),
                                   ({"brightness": 9}, 400), ({"slide": 9}, 400), ({"frame": 5}, 400)):
            body = {"request_id": uuid4().hex, "slide": 1, "key": key, "frame": 60, **patch_body}
            with self.assertRaises(VoiceError) as caught:
                self.bench.use_clip_frame(self.item["id"], body)
            self.assertEqual(caught.exception.status, status, patch_body)

    def test_reveal_puts_the_playhead_inside_the_clip(self):
        key = self.listing()["clips"][0]["key"]
        result = self.bench.reveal_clip(self.item["id"], {"key": key, "frame": 75})
        call = [entry for entry in self.bench.runner.calls if entry["command"] == "reveal"][-1]
        self.assertEqual(call["unique_id"], "uid-material")
        self.assertEqual(call["frame"], 86400 + 45)
        self.assertEqual(result["name"], "蓝点折刀成品素材")


class AdjustmentTests(unittest.TestCase):
    def test_defaults_are_no_ops(self):
        adjustments = _clean_adjustments({})
        self.assertEqual(adjustments, {"crop": None, "rotate": 0, "brightness": 1.0, "contrast": 1.0})
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "a.png", Path(folder) / "b.png"
            Image.new("RGB", (40, 30), (10, 20, 30)).save(source)
            self.assertEqual(apply_adjustments(source, target, adjustments), (40, 30))
            self.assertEqual(Image.open(target).getpixel((5, 5)), (10, 20, 30))


if __name__ == "__main__":
    unittest.main()
