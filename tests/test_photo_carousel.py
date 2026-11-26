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
from photo_carousel import PhotoCarousels, PhotoSetError, migrate, parse_options, parse_plan, parse_slide
from resolve_bridge import ResolveBridge

PLAN = {
    "versions": [
        {"writing": "self", "name_zh": "一套配齐", "buyer_zh": "攒磨石的人", "motive_zh": "一次配齐", "recommended": True, "cta": "GET YOUR *202H* BELOW.",
         "caption_en": "One case. #KnifeSharpening", "caption_zh": "一箱装下。",
         "slides": [{"title_lines": ["STOP BUYING STONES", "*ONE BY ONE.*"], "body_lines": ["7 plates in one case"], "title_zh": "别一块块买",
                     "body_zh": "7 块磨板", "frame_time": 0.4, "picture_zh": "箱子", "crop_y": 0.2, "text_position": "bottom"},
                    {"title_lines": ["IT ALL PACKS", "BACK *IN.*"], "body_lines": ["3-year limited warranty", "GET YOUR *202H* BELOW."], "frame_time": 1.0}]},
        {"writing": "qa", "name_zh": "送礼", "slides": [{"title_lines": ["GIFT"], "frame_time": 99, "text_position": "nowhere"}]},
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

    def test_zoom_blows_the_picture_up_inside_its_window(self):
        image = Image.new("RGB", (1080, 1920), "black")
        image.paste((255, 0, 0), (440, 860, 640, 1060))  # a 200 px red square in the middle
        probe = (render.W // 2 + 150, render.H // 2)
        self.assertEqual(render.crop_fit(image).getpixel((render.W // 2, render.H // 2)), (255, 0, 0))
        self.assertNotEqual(render.crop_fit(image).getpixel(probe), (255, 0, 0))
        zoomed = render.crop_fit(image, zoom=2)
        self.assertEqual(zoomed.size, (render.W, render.H))
        self.assertEqual(zoomed.getpixel(probe), (255, 0, 0))  # the square is now 400 px wide
        corner = render.crop_fit(image, 0, 0, zoom=2)  # zoomed in on the top-left: the middle square is out of the window
        self.assertNotEqual(corner.getpixel((render.W // 2, render.H // 2)), (255, 0, 0))
        self.assertEqual(render.crop_fit(image, zoom=9).tobytes(), render.crop_fit(image, zoom=3).tobytes())  # at most 3×
        self.assertEqual(render.crop_fit(image, zoom="x").tobytes(), render.crop_fit(image).tobytes())
        small = Image.new("RGB", (540, 960), "black")
        small.paste((0, 0, 255), (0, 0, 540, 40))
        cell = render.crop_fit(small, 0, 0.5, 540, 720, zoom=1.5)
        self.assertEqual((cell.size, cell.getpixel((270, 5))), ((540, 720), (0, 0, 255)))
        collage = render.compose({"layout": "lr", "zoom": 2, "cells": [{"zoom": 1}]}, [image, image])
        self.assertEqual(collage.getpixel((render.W // 4 + 120, render.H // 2)), (255, 0, 0))  # left cell zoomed
        self.assertNotEqual(collage.getpixel((3 * render.W // 4 + 120, render.H // 2)), (255, 0, 0))  # right one not

    def test_collage_puts_each_picture_in_its_cell(self):
        red, blue = Image.new("RGB", (720, 1280), (255, 0, 0)), Image.new("RGB", (720, 1280), (0, 0, 255))
        slide = {"title_lines": ["BEFORE / *AFTER*"], "layout": "lr", "cells": [{"crop_x": 0.5, "crop_y": 0.5}], "text_position": "middle"}
        out = render.render(slide, [red, blue], "bar", 1, 3)
        self.assertEqual(out.size, (render.W, render.H))
        self.assertEqual(out.getpixel((40, render.H // 2 + 200)), (255, 0, 0))
        self.assertEqual(out.getpixel((render.W - 40, render.H // 2 + 200)), (0, 0, 255))
        self.assertEqual(out.getpixel((render.W // 2, render.H - 200)), (255, 255, 255))  # thin seam between cells
        grid = render.compose({"layout": "grid"}, [red, blue, blue, red])
        self.assertEqual([grid.getpixel(xy) for xy in ((10, 10), (render.W - 10, 10), (10, render.H - 10), (render.W - 10, render.H - 10))],
                         [(255, 0, 0), (0, 0, 255), (0, 0, 255), (255, 0, 0)])
        with self.assertRaises(render.RenderError):
            render.compose({"layout": "tb"}, [red])
        self.assertEqual(render.compose({"layout": "unknown"}, [red]).getpixel((render.W // 2, render.H // 2)), (255, 0, 0))

    def test_a_long_spoken_second_line_breaks_into_two_even_rows(self):
        sentence = "One stone gets it sharp, just not the finish I'm after."
        rows = render.balance([sentence], lambda line: len(line) > 40)
        self.assertEqual(len(rows), 2)
        self.assertEqual(" ".join(rows), sentence)
        self.assertLess(abs(len(rows[0]) - len(rows[1])), 12)
        self.assertEqual(render.balance(["Short."], lambda line: True), ["Short."])  # one word cannot be split
        self.assertEqual(render.balance(["a b", "c d"], lambda line: True), ["a b", "c d"])  # two rows already
        image = Image.new("RGB", (720, 1280), (90, 120, 150))
        for style in render.STYLES:
            out = render.render({"title_lines": ["I FOUND THE *NEXT LEVEL*", "FOR MY STONE."], "body_lines": [sentence]}, image, style, 1, 5)
            self.assertEqual(out.size, (render.W, render.H))

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

    def test_collage_needs_a_picture_for_every_cell(self):
        plan = {"versions": [{"name_zh": "对比", "slides": [
            {"title_lines": ["DULL / *SHARP*"], "frame_time": 0.2, "layout": "lr", "cells": [{"frame_time": 1.4, "crop_y": 3}]},
            {"title_lines": ["FULL KIT"], "frame_time": 0.2, "layout": "grid", "cells": [{"frame_time": 1.0}]},
            {"title_lines": ["PLAIN"], "frame_time": 0.2, "layout": "mosaic"}]}]}
        slides = parse_plan(json.dumps(plan))[0][0]["slides"]
        self.assertEqual((slides[0]["layout"], slides[0]["cells"]), ("lr", [{"frame_time": 1.4, "crop_y": 1.0}]))
        self.assertEqual((slides[1]["layout"], slides[1]["cells"]), ("single", []))
        self.assertEqual(slides[2]["layout"], "single")
        work = photo_carousel.new_work(parse_plan(json.dumps(plan))[0][0])
        self.assertEqual(work["slides"][0]["cells"], [{"image": {"kind": "frame", "t": 1.4}, "crop_x": 0.5, "crop_y": 1.0}])

    def test_rewritten_slide_is_cleaned_like_the_plan(self):
        answer = "好的\n```json\n" + json.dumps({"title_lines": ["  ONE CASE,  ", "*ALL SEVEN*", "x", "y"], "body_lines": ["7 *plates*", "GET YOUR 202H BELOW."],
                                                 "title_zh": "一箱装下", "text_position": "nowhere", "change_zh": "改讲件数"}) + "\n```"
        words, change = parse_slide(answer, "GET YOUR *202H* BELOW.")
        self.assertEqual(words, {"title_lines": ["ONE CASE,", "*ALL SEVEN*", "x"], "body_lines": ["7 plates"], "title_zh": "一箱装下", "body_zh": ""})
        self.assertEqual(change, "改讲件数")
        self.assertEqual(parse_slide('{"title_lines":["A"],"text_position":"middle"}')[0]["text_position"], "middle")
        for bad in ("没有 JSON", '{"title_lines":[]}', "[1, 2]"):
            with self.assertRaises(PhotoSetError):
                parse_slide(bad)

    def test_options_keep_up_to_three_usable_wordings(self):
        answer = json.dumps({"options": [{"title_lines": ["ONE"], "body_lines": ["GET YOUR 202H BELOW."], "change_zh": "问句"},
                                         {"title_lines": []}, {"title_lines": ["TWO"]}, {"title_lines": ["THREE"]}, {"title_lines": ["FOUR"]}]})
        options = parse_options(answer, "GET YOUR *202H* BELOW.")
        self.assertEqual([o["title_lines"] for o in options], [["ONE"], ["TWO"]])  # the first three, the empty one dropped
        self.assertEqual((options[0]["body_lines"], options[0]["change_zh"]), ([], "问句"))
        self.assertEqual(parse_options('{"title_lines":["SOLO"]}')[0]["title_lines"], ["SOLO"])
        twice = json.dumps({"options": [{"title_lines": ["SAME"], "body_lines": ["x"]}, {"title_lines": ["SAME"], "body_lines": ["x"]},
                                        {"title_lines": ["SAME"], "body_lines": ["y"]}]})
        self.assertEqual([o["body_lines"] for o in parse_options(twice)], [["x"], ["y"]])
        with self.assertRaises(PhotoSetError):
            parse_options('{"options":[{"title_lines":[]}]}')

    def test_versions_take_the_two_writings_and_retired_ones_are_not_taken(self):
        plan = {"versions": [{"slides": [{"title_lines": ["A"]}]}, {"writing": "list", "slides": [{"title_lines": ["B"]}]},
                             {"writing": "kit", "slides": [{"title_lines": ["C"]}]}]}
        versions = parse_plan(json.dumps(plan))[0]
        # 顾虑问答 / 理由清单 were retired 2026-11-19: an answer naming one takes the writing at its place instead.
        self.assertEqual([(v["key"], v["writing"]) for v in versions], [("A", "self"), ("B", "kit"), ("C", "kit")])
        self.assertEqual(photo_carousel.new_work(versions[0])["writing"], "self")
        self.assertEqual(photo_carousel.WRITING_ORDER, ("self", "kit"))
        # Old versions still show their writing's name and can still be reworded by its rule.
        self.assertEqual(photo_carousel.WRITINGS, {"self": "达人自述", "kit": "整套配置", "rescue": "钝刀救回", "slots": "三槽一机", "qa": "顾虑问答", "list": "理由清单"})
        self.assertEqual(set(photo_carousel.WRITING_RULES), set(photo_carousel.WRITINGS))

    def test_garbage_is_reported(self):
        with self.assertRaises(PhotoSetError):
            parse_plan("没有 JSON")

    def test_asked_writings_give_letters_backups_and_recommend_b(self):
        plan = {"versions": [{"recommended": False, "slides": [{"title_lines": ["Q"], "frame_time": 2, "alt_times": [5, "7.5", -1, "x", 9]}]},
                             {"recommended": True, "slides": [{"title_lines": ["L"], "frame_time": 3}]}]}
        versions = parse_plan(json.dumps(plan), ["kit", "self"])[0]
        self.assertEqual([(v["key"], v["writing"], v["recommended"]) for v in versions], [("B", "kit", True), ("A", "self", False)])
        self.assertEqual(versions[0]["slides"][0]["alt_times"], [5.0, 7.5])  # at most two, only real times
        work = photo_carousel.new_work(versions[0])
        self.assertEqual(work["slides"][0]["alternates"], [2.0, 5.0, 7.5])  # the AI's own pick stays first
        self.assertNotIn("alternates", photo_carousel.new_work(versions[1])["slides"][0])
        # Two versions claiming the same writing still get different letters.
        twice = {"versions": [{"writing": "kit", "slides": [{"title_lines": ["1"]}]}, {"writing": "kit", "slides": [{"title_lines": ["2"]}]}]}
        self.assertEqual([v["key"] for v in parse_plan(json.dumps(twice), ["self", "kit"])[0]], ["B", "A"])

    def test_sharpest_moves_off_blur_only(self):
        items = [{"t": 1.0, "sharp": 10}, {"t": 1.2, "sharp": 100}, {"t": 1.4, "sharp": 95}]
        self.assertEqual(PhotoCarousels.sharpest(items, 1.0), 1.2)
        self.assertEqual(PhotoCarousels.sharpest(items, 1.4), 1.4)


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class DecisionTests(unittest.TestCase):
    """要不要做: the 图文 overview's per-timeline state and the stored 要做 / 不做 decisions."""
    JOB = "PRD-0123456789ab"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sets = PhotoCarousels(App(self.tmp, "{}"), export_root=self.tmp / "图文成品")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def state(self, uid):
        return self.sets.states([(self.JOB, uid)])[(self.JOB, uid)]

    def write_set(self, set_id, uid, exports=0, **extra):
        folder = self.sets.root / set_id
        folder.mkdir()
        data = {"id": set_id, "job_id": self.JOB, "timeline_uid": uid, "created_at": "2026-11-06T00:00:00+00:00",
                "works": [{"key": "A", "status": "done" if exports else "doing", "exports": [{}] * exports}], "current": "A", **extra}
        (folder / "set.json").write_text(json.dumps(data), encoding="utf-8")

    def test_new_timeline_is_pending_and_decisions_round_trip(self):
        self.assertEqual(self.state("u1")["state"], "pending")
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "skip", "reason": "  没有实拍  "})["state"], "skip")
        self.assertEqual(self.state("u1")["reason"], "没有实拍")
        self.assertEqual(self.state("u2")["state"], "pending")  # another timeline is untouched
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "want"})["state"], "want")
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "clear"})["state"], "pending")
        self.assertEqual(json.loads((self.sets.root / "decisions.json").read_text(encoding="utf-8")), {})

    def test_existing_set_decides_the_state(self):
        self.write_set("PHS-000000000001", "u1")
        self.write_set("PHS-000000000002", "u2", exports=1)
        self.assertEqual((self.state("u1")["state"], self.state("u1")["set_id"]), ("doing", "PHS-000000000001"))
        self.assertEqual(self.state("u2")["state"], "exported")
        self.write_set("PHS-000000000003", "u3", discarded=True)
        self.assertEqual(self.state("u3")["state"], "pending")

    def test_work_wins_over_a_stored_decision_and_skipping_started_work_is_refused(self):
        self.sets.decide(self.JOB, "u1", {"decision": "skip"})
        self.write_set("PHS-000000000001", "u1")
        self.assertEqual(self.state("u1")["state"], "doing")
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.decide(self.JOB, "u1", {"decision": "skip"})
        self.assertEqual(caught.exception.status, 409)

    def test_finish_locks_the_set_keeps_its_files_and_can_be_undone(self):
        self.write_set("PHS-000000000001", "u1", exports=1)
        before = (self.sets.root / "PHS-000000000001" / "set.json").read_text(encoding="utf-8")
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "finish"})["state"], "finished")
        self.assertEqual((self.sets.root / "PHS-000000000001" / "set.json").read_text(encoding="utf-8"), before)
        self.assertTrue(self.state("u1")["finished_at"])
        for attempt in (lambda: self.sets.undo("PHS-000000000001"), lambda: self.sets.edit_set("PHS-000000000001", {"cta": "x"}), lambda: self.sets.export("PHS-000000000001")):
            with self.assertRaises(PhotoSetError) as caught:
                attempt()
            self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(PhotoSetError):
            self.sets._check_video_change(self.JOB, "u1")
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "reopen"})["state"], "exported")
        self.assertEqual(json.loads((self.sets.root / "decisions.json").read_text(encoding="utf-8")), {})
        self.sets.ensure_open("PHS-000000000001")

    def test_finish_applies_to_one_timeline_only_and_silences_its_failures(self):
        self.write_set("PHS-000000000001", "u1", ai={"status": "failed", "error": "超时"})
        self.write_set("PHS-000000000002", "u2", ai={"status": "failed", "error": "超时"})
        self.sets.decide(self.JOB, "u1", {"decision": "finish"})
        self.assertEqual(self.state("u2")["state"], "doing")
        self.assertEqual([row["timeline_uid"] for row in self.sets.failures()], ["u2"])

    def test_finish_is_refused_while_running_or_before_anything_started(self):
        self.write_set("PHS-000000000001", "u1", ai={"status": "running"})
        for uid in ("u1", "u2"):
            with self.assertRaises(PhotoSetError) as caught:
                self.sets.decide(self.JOB, uid, {"decision": "finish"})
            self.assertEqual(caught.exception.status, 409)

    def test_a_wanted_timeline_finishes_and_reopens_as_wanted(self):
        self.sets.decide(self.JOB, "u1", {"decision": "want"})
        self.assertTrue(self.sets.detail(self.JOB, "u1")["can_finish"])
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "finish"})["state"], "finished")
        self.assertFalse(self.sets.detail(self.JOB, "u1")["can_finish"])
        self.assertEqual(self.sets.decide(self.JOB, "u1", {"decision": "reopen"})["state"], "want")

    def footprint(self):
        """One timeline with a current set, a replaced set, an export, a Resolve import and a stored decision."""
        self.sets.export_root.mkdir(parents=True)
        export = self.sets.export_root / "PRD-20261108-A"
        (export / "发布JPG").mkdir(parents=True)
        (export / "发布JPG" / "01.jpg").write_bytes(b"x" * 10)
        self.write_set("PHS-000000000001", "u1", works=[{"key": "A", "status": "done", "exports": [{"folder": str(export), "name": export.name}]}], created_at="2026-11-07T00:00:00+00:00")
        self.write_set("PHS-000000000002", "u1", discarded=True)
        self.write_set("PHS-000000000003", "u2")
        (self.sets.root / "PHS-000000000001" / "video.mp4").write_bytes(b"v" * 5)
        imported = self.sets._import_folder(self.JOB, "u1")
        imported.mkdir(parents=True)
        (imported / "job.json").write_text(json.dumps({"status": "done"}), encoding="utf-8")
        self.sets.decide(self.JOB, "u1", {"decision": "finish"})
        return export, imported

    def test_preview_reports_what_would_go_and_changes_nothing(self):
        export, imported = self.footprint()
        info = self.sets.delete(self.JOB, "u1", {"preview": True})
        self.assertEqual((info["versions"], info["exports"]), (1, 1))
        self.assertGreaterEqual(info["files"], 5)
        self.assertTrue(export.is_dir() and imported.is_dir() and (self.sets.root / "PHS-000000000002").is_dir())
        self.assertEqual(self.state("u1")["state"], "finished")

    def test_delete_removes_everything_local_for_that_timeline_only(self):
        export, imported = self.footprint()
        activity = type("Activity", (), {"gone": [], "delete_source": lambda me, owner, source: me.gone.append(source)})()
        self.sets.app.activity = activity
        notion = type("Notion", (), {"forgot": [], "photo_busy": lambda me, ids: False, "photo_receipts": lambda me, ids: [],
                                     "forget_photo": lambda me, ids: me.forgot.append(set(ids))})()
        self.sets.app.notion_uploads = notion
        self.assertEqual(self.sets.delete(self.JOB, "u1", {}), {"deleted": True, "warnings": []})
        for gone in (export, imported, self.sets.root / "PHS-000000000001", self.sets.root / "PHS-000000000002"):
            self.assertFalse(gone.exists(), gone)
        self.assertEqual(json.loads((self.sets.root / "decisions.json").read_text(encoding="utf-8")), {})
        self.assertEqual(self.state("u1")["state"], "pending")
        self.assertEqual(self.state("u2")["state"], "doing")
        self.assertTrue((self.sets.root / "PHS-000000000003").is_dir())
        self.assertEqual(notion.forgot, [{"PHS-000000000001", "PHS-000000000002"}])
        self.assertEqual(set(activity.gone), {"PHS-000000000001", "PHS-000000000002", "u1"})

    def test_delete_never_follows_a_record_outside_the_export_folder(self):
        outside = self.tmp / "别的资料"
        outside.mkdir()
        nested = self.sets.export_root / "a" / "b"
        nested.mkdir(parents=True)
        self.write_set("PHS-000000000001", "u1", works=[{"key": "A", "status": "done", "exports": [{"folder": str(outside)}, {"folder": str(nested)}, {"folder": str(self.sets.root)}]}])
        self.sets.delete(self.JOB, "u1", {})
        self.assertTrue(outside.is_dir() and nested.is_dir() and self.sets.root.is_dir())
        self.assertFalse((self.sets.root / "PHS-000000000001").exists())

    def test_delete_is_refused_while_work_runs_or_uploads_and_when_there_is_nothing(self):
        self.write_set("PHS-000000000001", "u1", ai={"status": "running"})
        self.write_set("PHS-000000000002", "u2")
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.delete(self.JOB, "u1", {})
        self.assertEqual(caught.exception.status, 409)
        self.sets.app.notion_uploads = type("Notion", (), {"photo_busy": lambda me, ids: True})()
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.delete(self.JOB, "u2", {})
        self.assertIn("Notion", str(caught.exception))
        self.assertTrue((self.sets.root / "PHS-000000000002").is_dir())
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.delete(self.JOB, "u9", {})
        self.assertEqual(caught.exception.status, 404)
        for bad in ({"preview": "yes"}, {"extra": 1}, []):
            with self.assertRaises(PhotoSetError):
                self.sets.delete(self.JOB, "u2", bad)

    def two_exports(self):
        """Version A of u1 finalized twice; another set's version claims one more folder in the same 图文成品."""
        self.sets.export_root.mkdir(parents=True)
        mine = [self.sets.export_root / "PRD-20261108-A", self.sets.export_root / "PRD-20261108-A-v2"]
        theirs = self.sets.export_root / "PRD-20261108-B"
        for folder in (*mine, theirs):
            (folder / "发布JPG").mkdir(parents=True)
            (folder / "发布JPG" / "01.jpg").write_bytes(b"x" * 10)
        self.write_set("PHS-000000000001", "u1", works=[
            {"key": "A", "status": "done", "slides": [{"title_lines": ["HI"]}], "exports": [{"folder": str(f), "name": f.name} for f in mine]},
            {"key": "B", "status": "doing", "exports": []}], current="B")
        self.write_set("PHS-000000000002", "u2", works=[{"key": "B", "status": "done", "exports": [{"folder": str(theirs), "name": theirs.name}]}])
        return mine, theirs

    def test_unfinalize_reopens_the_version_and_deletes_only_its_exports(self):
        mine, theirs = self.two_exports()
        info = self.sets.unfinalize("PHS-000000000001", {"key": "A", "preview": True})
        self.assertEqual((info["exports"], info["files"], info["bytes"], info["notion_page"]), (2, 2, 20, False))
        self.assertTrue(all(folder.is_dir() for folder in mine))
        result = self.sets.unfinalize("PHS-000000000001", {"key": "A"})
        self.assertEqual(result["warnings"], [])
        self.assertEqual((result["work"]["key"], result["work"]["status"], result["work"]["exports"]), ("A", "doing", []))
        self.assertEqual(result["work"]["slides"], [{"title_lines": ["HI"]}])
        self.assertFalse(any(folder.exists() for folder in mine))
        self.assertTrue(theirs.is_dir())
        self.assertEqual(self.state("u1")["state"], "doing")
        self.assertEqual(self.state("u2")["state"], "exported")
        self.sets.remove_work("PHS-000000000001", {"key": "A"})  # no longer exported, so it can leave the queue

    def test_unfinalize_never_deletes_a_folder_outside_or_shared(self):
        mine, theirs = self.two_exports()
        outside = self.tmp / "别的资料"
        outside.mkdir()
        data = json.loads((self.sets.root / "PHS-000000000001" / "set.json").read_text(encoding="utf-8"))
        data["works"][0]["exports"] += [{"folder": str(outside)}, {"folder": str(theirs)}, {"folder": str(self.sets.root)}]
        (self.sets.root / "PHS-000000000001" / "set.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.sets.unfinalize("PHS-000000000001", {"key": "A", "preview": True})["exports"], 2)
        self.sets.unfinalize("PHS-000000000001", {"key": "A"})
        self.assertTrue(outside.is_dir() and theirs.is_dir() and self.sets.root.is_dir())

    def test_unfinalize_is_refused_when_not_done_finished_running_or_uploading(self):
        self.two_exports()
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.unfinalize("PHS-000000000001", {"key": "B"})
        self.assertEqual(caught.exception.status, 409)
        self.sets.app.notion_uploads = type("Notion", (), {"photo_busy": lambda me, ids: True})()
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.unfinalize("PHS-000000000001", {"key": "A"})
        self.assertIn("Notion", str(caught.exception))
        del self.sets.app.notion_uploads
        self.sets.decide(self.JOB, "u1", {"decision": "finish"})
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.unfinalize("PHS-000000000001", {"key": "A"})
        self.assertIn("完结", str(caught.exception))
        self.sets.decide(self.JOB, "u1", {"decision": "reopen"})
        data = json.loads((self.sets.root / "PHS-000000000001" / "set.json").read_text(encoding="utf-8"))
        data["ai"] = {"status": "running"}
        (self.sets.root / "PHS-000000000001" / "set.json").write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.unfinalize("PHS-000000000001", {"key": "A"})
        self.assertEqual(caught.exception.status, 409)
        for bad in ({"key": "A", "preview": "yes"}, {"key": "A", "extra": 1}, []):
            with self.assertRaises(PhotoSetError):
                self.sets.unfinalize("PHS-000000000002", bad)
        self.assertTrue((self.sets.export_root / "PRD-20261108-A").is_dir())

    def test_failed_steps_are_listed_for_the_needs_you_view(self):
        self.write_set("PHS-000000000001", "u1", ai={"status": "failed", "error": "超时", "finished_at": "t1"})
        self.write_set("PHS-000000000002", "u2", frames_job={"status": "done"}, slide_ai={"status": "running"})
        self.write_set("PHS-000000000003", "u3", frames_job={"status": "failed", "error": "ffmpeg"}, discarded=True)
        self.assertEqual(self.sets.failures(), [{"job_id": self.JOB, "timeline_uid": "u1", "step": "写逐张表", "error": "超时", "at": "t1"}])

    def test_bad_decisions_are_refused(self):
        for body in ({}, {"decision": "maybe"}, {"decision": "skip", "extra": 1}, {"decision": "skip", "reason": "x" * 101}, {"decision": "skip", "reason": "a\tb"}, []):
            with self.assertRaises(PhotoSetError, msg=body) as caught:
                self.sets.decide(self.JOB, "u1", body)
            self.assertEqual(caught.exception.status, 400)

    def test_unreadable_decision_file_is_reported_on_write_and_ignored_on_read(self):
        (self.sets.root / "decisions.json").write_text("{broken", encoding="utf-8")
        self.assertEqual(self.state("u1")["state"], "pending")
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.decide(self.JOB, "u1", {"decision": "skip"})
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual((self.sets.root / "decisions.json").read_text(encoding="utf-8"), "{broken")


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
        self.assertEqual(state["work"]["cta"], "")  # 末张不默认带收口
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
        self.assertEqual("US-202-Zack-1104-1A", folder.name)  # 成品统一命名（2026-11-24）
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

    def test_plan_writes_a_and_the_kit_version_and_recommends_the_kit(self):
        self.assertEqual(self.sets._writings(None), ["self", "kit"])
        self.assertEqual(self.sets._writings({"all": True}), ["self", "kit"])  # the old "all three" switch no longer adds anything
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        detail = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(detail["ai"]["status"], "done", detail["ai"])
        prompt = self.app.ai.bridge.calls[0][0]
        self.assertIn("先出 2 版逐张表", prompt)
        self.assertIn("B 版 " + photo_carousel.WRITING_RULES["kit"], prompt)
        self.assertIn("A 版 " + photo_carousel.WRITING_RULES["self"], prompt)
        for retired in ("qa", "list"):
            self.assertNotIn(photo_carousel.WRITING_RULES[retired], prompt)
        self.assertIn("writing 依次是 self、kit", prompt)
        # The kit version is exempt from the three-part chain and the spoken second line.
        self.assertIn("除整套配置版外，每版都按三段走", prompt)
        self.assertIn(photo_carousel.KIT_PLAN_RULE, prompt)
        self.assertIn("alt_times", prompt)
        self.assertEqual([v["key"] for v in detail["versions"] if v["recommended"]], ["B"])

    def detail(self):
        return self.sets.detail("PRD-0123456789ab", "uid-1")

    def test_plan_in_german_keeps_the_language_through_rewording_and_export(self):
        set_id = self.ready()
        self.assertEqual(self.detail()["market"], "us")
        self.assertIn("de", [row["id"] for row in self.detail()["markets"]])
        with self.assertRaises(PhotoSetError):
            self.sets.plan(set_id, {"market": "fr"})
        self.sets.plan(set_id, {"market": "de"})
        wait(lambda: self.detail()["ai"]["status"] != "running")
        detail = self.detail()
        self.assertEqual((detail["ai"]["status"], detail["market"]), ("done", "de"), detail["ai"])
        prompt = self.app.ai.bridge.calls[0][0]
        self.assertIn("本轮目标市场：德国 · 德语", prompt)
        self.assertIn("德国 TikTok 达人对着镜头说话", prompt)
        self.assertIn("🔴 + 德语标题", prompt)
        self.assertNotIn("美国 TikTok 达人", prompt)
        self.assertEqual({v["market"] for v in detail["versions"]}, {"de"})
        state = self.sets.choose(set_id, {"versions": ["A"]})
        self.assertEqual(state["work"]["market"], "de")
        self.sets.rewrite(set_id, 1, {"zh": "不用再猜角度"})
        wait(lambda: (self.detail().get("slide_ai") or {}).get("status") != "running")
        prompt = self.app.ai.bridge.calls[1][0]
        self.assertIn("出 3 种德语说法", prompt)
        self.assertIn("地道的德语口语", prompt)
        self.assertIn("标题用德国 TikTok 达人", prompt)
        self.assertNotIn("美式", prompt)
        folder = Path(self.sets.export(set_id)["export"]["folder"])
        self.assertTrue(folder.name.startswith("DE-"), folder.name)
        copy_json = json.loads((folder / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual((copy_json["market"], copy_json["image_language"]), ("DE", "de"))
        # Planning again in another language leaves the version already being made in German.
        self.sets.plan(set_id, {"market": "uk"})
        wait(lambda: self.detail()["ai"]["status"] != "running")
        detail = self.detail()
        self.assertEqual({v["market"] for v in detail["versions"]}, {"uk"})
        self.assertEqual(detail["work"]["market"], "de")

    def test_a_set_follows_its_video_job_market_and_american_prompts_stay_as_before(self):
        original = self.app.production.job
        self.app.production.job = lambda identifier: {**original(identifier), "market": "uk"}
        set_id = self.ready()
        self.assertEqual(self.detail()["market"], "uk")
        self.sets.plan(set_id)
        wait(lambda: self.detail()["ai"]["status"] != "running")
        prompt = self.app.ai.bridge.calls[0][0]
        self.assertIn("本轮目标市场：英国 · 英语", prompt)
        self.assertIn("英国 TikTok 达人", prompt)
        self.assertIn("🔴 + 英式英语标题", prompt)
        self.app.production.job = original
        self.sets.plan(set_id, {"market": "us"})
        wait(lambda: self.detail()["ai"]["status"] != "running")
        prompt = self.app.ai.bridge.calls[1][0]
        self.assertNotIn("本轮目标市场", prompt)
        self.assertIn("都用美国 TikTok 达人对着镜头说话的口吻", prompt)
        self.assertIn("🔴 + 英文标题", prompt)
        state = self.sets.choose(set_id, {"versions": ["A"]})
        self.assertEqual(state["work"]["market"], "us")
        copy_json = json.loads((Path(self.sets.export(set_id)["export"]["folder"]) / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual((copy_json["market"], copy_json["image_language"]), ("US", "en"))

    def test_backup_frames_snap_to_real_frames(self):
        plan = json.loads(json.dumps(PLAN))
        plan["versions"][0]["slides"][0]["alt_times"] = [0.45, 1.6, 0.5]
        self.app.ai.bridge.output = json.dumps(plan)
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        frames = self.sets.root / set_id / "frames.json"
        info = json.loads(frames.read_text(encoding="utf-8"))
        for item in info["items"]:  # every frame equally sharp: nothing is flagged as blurry yet
            item["sharp"] = 100.0
        frames.write_text(json.dumps(info), encoding="utf-8")
        state = self.sets.choose(set_id, {"versions": ["A"]})
        slide = state["work"]["slides"][0]
        items = {item["t"] for item in info["items"]}
        self.assertEqual(slide["alternates"][0], slide["image"]["t"])
        self.assertTrue(set(slide["alternates"]) <= items)
        self.assertEqual(len(slide["alternates"]), len(set(slide["alternates"])))  # backups too close to the pick are dropped
        self.assertTrue(all(abs(a - b) >= 0.5 for i, a in enumerate(slide["alternates"]) for b in slide["alternates"][:i]))
        self.assertNotIn("checks", state)  # the pre-export reminder was removed 2026-11-25

    def test_background_steps_show_in_the_task_panel_under_their_order(self):
        from task_activity import ActivityStore
        self.app.activity = ActivityStore(self.tmp / "activity")
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        self.sets.choose(set_id, {"versions": ["A"]})
        self.sets.rewrite(set_id, 1, {})
        wait(lambda: (self.sets.detail("PRD-0123456789ab", "uid-1").get("slide_ai") or {}).get("status") != "running")
        runs = sorted(self.app.activity.feed(kind="photo", limit=10)["items"], key=lambda run: run["started_at"])
        self.assertEqual([run["title"].split(" · ")[0] for run in runs], ["图文抽帧", "图文逐张表", "改写第 1 张"])
        self.assertEqual({run["owner_id"] for run in runs}, {"PRD-0123456789ab"})
        self.assertEqual({run["source_id"] for run in runs}, {set_id})
        # The fake AI answers the rewrite with a plan, so that step fails and says why.
        self.assertEqual([run["status"] for run in runs], ["completed", "completed", "failed"], [run["message"] for run in runs])
        self.assertIn("step=cut", runs[0]["url"])
        self.assertEqual(runs[1]["message"], "2 版逐张表已写好，去选一版")
        self.assertIn("没有标题", runs[2]["message"])

    def test_slides_can_be_copied_deleted_and_reordered_with_undo(self):
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        state = self.sets.choose(set_id, {"versions": ["A"]})
        titles = lambda st: [s["title_lines"][0] for s in st["work"]["slides"]]
        self.assertEqual(titles(state), ["STOP BUYING STONES", "IT ALL PACKS"])
        self.assertEqual(state["max_slides"], photo_carousel.MAX_SLIDES)
        state = self.sets.arrange(set_id, 1, {"action": "duplicate"})
        self.assertEqual(titles(state), ["STOP BUYING STONES", "STOP BUYING STONES", "IT ALL PACKS"])
        state = self.sets.edit_slide(set_id, 2, {"title_lines": ["NEW *ONE*"], "image": {"kind": "frame", "t": 1.6}})
        self.assertNotEqual(state["work"]["slides"][0]["image"], state["work"]["slides"][1]["image"])  # the copy is independent
        state = self.sets.arrange(set_id, 3, {"action": "move", "to": 1})
        self.assertEqual(titles(state), ["IT ALL PACKS", "STOP BUYING STONES", "NEW *ONE*"])
        self.assertTrue(self.sets.rendered(set_id, "A", "bar", 3, 360).is_file())  # closing line on the new last slide
        state = self.sets.arrange(set_id, 2, {"action": "delete"})
        self.assertEqual(titles(state), ["IT ALL PACKS", "NEW *ONE*"])
        state = self.sets.undo(set_id)
        self.assertEqual(titles(state), ["IT ALL PACKS", "STOP BUYING STONES", "NEW *ONE*"])
        labels = [h["label"] for h in self.sets._read(set_id)["works"][0]["history"]]
        self.assertEqual(labels[0], "复制第 1 张")
        self.assertIn("第 3 张移到第 1 张", labels)
        for body in ({"action": "move", "to": 9}, {"action": "move", "to": "x"}, {"action": "spin"}):
            with self.assertRaises(PhotoSetError):
                self.sets.arrange(set_id, 1, body)
        with self.assertRaises(PhotoSetError):
            self.sets.arrange(set_id, 7, {"action": "delete"})
        before = self.sets._read(set_id)["updated_at"]
        self.sets.arrange(set_id, 2, {"action": "move", "to": 2})  # no-op, nothing saved
        self.assertEqual(self.sets._read(set_id)["updated_at"], before)
        while len(self.sets._read(set_id)["works"][0]["slides"]) < photo_carousel.MAX_SLIDES:
            self.sets.arrange(set_id, 1, {"action": "duplicate"})
        with self.assertRaises(PhotoSetError):
            self.sets.arrange(set_id, 1, {"action": "duplicate"})
        while len(self.sets._read(set_id)["works"][0]["slides"]) > 1:
            self.sets.arrange(set_id, 1, {"action": "delete"})
        with self.assertRaises(PhotoSetError):  # at least one slide stays
            self.sets.arrange(set_id, 1, {"action": "delete"})
        result = self.sets.export(set_id)
        self.assertEqual([p.name for p in (Path(result["export"]["folder"]) / "发布JPG").iterdir()], ["01.jpg"])

    def chosen(self):
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        self.sets.choose(set_id, {"versions": ["A"]})
        return set_id

    def rewritten(self, set_id):
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["slide_ai"]["status"] != "running")
        return self.sets.detail("PRD-0123456789ab", "uid-1")

    def test_chinese_meaning_gives_three_wordings_and_only_the_picked_one_is_used(self):
        set_id = self.chosen()
        self.sets.edit_slide(set_id, 2, {"layout": "lr", "image": {"kind": "frame", "t": 1.4}})
        self.sets.edit_set(set_id, {"cta": "GET YOUR *202H* BELOW."})  # 末张不默认带收口，这里是用户自己填的
        before = self.sets._read(set_id)["works"][0]["slides"]
        self.app.ai.bridge.output = json.dumps({"options": [
            {"title_lines": ["3 YEARS", "*COVERED.*"], "body_lines": ["3-year limited warranty", "GET YOUR *202H* BELOW."],
             "title_zh": "三年质保", "body_zh": "三年有限质保", "change_zh": "结果先给"},
            {"title_lines": ["WORRIED IT *BREAKS?*"], "body_lines": ["3-year limited warranty"], "title_zh": "怕坏？", "change_zh": "问句"},
            {"title_lines": ["*NO RISK*", "FOR 3 YEARS"], "text_position": "top"}]})
        state = self.sets.rewrite(set_id, 2, {"zh": "三年有限质保，坏了能找我们", "note": "短一点"})
        self.assertEqual(state["slide_ai"]["status"], "running")
        self.assertNotIn("snapshot", state["slide_ai"])
        with self.assertRaises(PhotoSetError):  # one at a time
            self.sets.rewrite(set_id, 1, {})
        state = self.rewritten(set_id)
        self.assertEqual(state["slide_ai"]["status"], "done", state["slide_ai"])
        self.assertEqual([o["title_lines"][0] for o in state["slide_ai"]["options"]], ["3 YEARS", "WORRIED IT *BREAKS?*", "*NO RISK*"])
        plain = lambda slide: {k: v for k, v in slide.items() if k != "id"}  # the slide gets a stable id when AI writes for it
        self.assertEqual([plain(s) for s in state["work"]["slides"]], before)  # nothing replaced before a pick
        prompt, options = self.app.ai.bridge.calls[-1]
        self.assertIn("出 3 种英文说法", prompt)
        self.assertIn("三年有限质保，坏了能找我们", prompt)
        self.assertIn("美国 TikTok 达人", prompt)
        self.assertIn("第 1 种「照你的意思」：严格按用户的意思和分行", prompt)  # one faithful, two close variations
        self.assertIn("第 2、3 种「贴着意思发散」", prompt)
        self.assertIn("不引入用户没提到的卖点", prompt)
        self.assertNotIn("3 种的第二行不能相同", prompt)
        self.assertEqual([o["label"] for o in state["slide_ai"]["options"]], ["照你的意思", "贴着意思发散", "贴着意思发散"])
        self.assertIn("不要抄用户写的中文", prompt)
        self.assertIn("用户的其他要求：短一点", prompt)
        self.assertIn(photo_carousel.SECOND_LINE_RULE, prompt)
        self.assertIn("1. STOP BUYING STONES / *ONE BY ONE.*｜7 plates in one case", prompt)
        self.assertIn("这一版的写法（这一张也照它写）：" + photo_carousel.WRITING_RULES["self"], prompt)  # A is 达人自述
        self.assertIn("这是最后一张", prompt)
        self.assertIn("左右拼图", prompt)
        self.assertEqual([label for label, _ in options["images"]], ["第 2 张底图 第 1 格", "第 2 张底图 第 2 格"])
        for bad in ({"index": 3}, {"index": "x"}):
            with self.assertRaises(PhotoSetError):
                self.sets.pick_option(set_id, 2, bad)
        state = self.sets.pick_option(set_id, 2, {"index": 0})
        slide = state["work"]["slides"][1]
        self.assertEqual((slide["title_lines"], slide["body_lines"], slide["title_zh"]), (["3 YEARS", "*COVERED.*"], ["3-year limited warranty"], "三年质保"))
        self.assertEqual((slide["image"], slide["layout"], slide["cells"]), (before[1]["image"], "lr", before[1]["cells"]))
        self.assertNotIn("change_zh", slide)
        self.assertNotIn("label", slide)
        self.assertEqual(plain(state["work"]["slides"][0]), before[0])
        state = self.sets.pick_option(set_id, 2, {"index": 2})  # switching to another wording is allowed
        slide = state["work"]["slides"][1]
        self.assertEqual((slide["title_lines"], slide["body_lines"]), (["*NO RISK*", "FOR 3 YEARS"], []))
        self.assertEqual(slide["text_position"], before[1].get("text_position"))  # only words change, never the layout
        self.assertEqual(slide["title_zh"], "")
        self.assertEqual(state["slide_ai"]["picked"], 2)
        state = self.sets.undo(set_id)
        self.assertEqual(state["work"]["slides"][1]["title_lines"], ["3 YEARS", "*COVERED.*"])
        state = self.sets.undo(set_id)
        self.assertEqual(plain(state["work"]["slides"][1]), before[1])
        self.sets.pick_option(set_id, 2, {"index": 1})  # after undoing back to the original, picking still works
        self.sets.edit_slide(set_id, 2, {"title_lines": ["MY OWN"], "image": {"kind": "frame", "t": 0.2}, "layout": "single"})
        state = self.sets.pick_option(set_id, 2, {"index": 0})  # a later picture or hand edit never blocks a click
        slide = state["work"]["slides"][1]
        self.assertEqual((slide["title_lines"], slide["image"], slide["layout"]), (["3 YEARS", "*COVERED.*"], {"kind": "frame", "t": 0.2}, "single"))
        before_noop = self.sets._read(set_id)["updated_at"]
        self.sets.pick_option(set_id, 2, {"index": 0})  # the wording already on the slide: nothing saved
        self.assertEqual(self.sets._read(set_id)["updated_at"], before_noop)
        with self.assertRaises(PhotoSetError):
            self.sets.rewrite(set_id, 5, {})
        self.sets.rewrite(set_id, 1, {})
        self.rewritten(set_id)
        prompt = self.app.ai.bridge.calls[-1][0]
        self.assertIn("意思不变，写得更抓人", prompt)  # no Chinese given: keep the meaning
        self.assertIn("3 种的第二行不能相同", prompt)
        self.assertNotIn("照你的意思", prompt)
        self.assertNotIn("label", self.sets.detail("PRD-0123456789ab", "uid-1")["slide_ai"]["options"][0])
        self.assertIn("这是第 1 张", prompt)

    def test_publish_titles_use_the_video_format_and_copy_list(self):
        self.assertEqual(photo_carousel.publish_title("I found it  #SHARPAL202H  #knife"), "🔴I found it#SHARPAL202H #knife")
        self.assertEqual(photo_carousel.publish_title("🔴 Already red #a"), "🔴Already red#a")
        self.assertEqual(photo_carousel.publish_titles(["a", "", "b", "c", "d"]), ["🔴a", "🔴b", "🔴c"])
        self.assertEqual(photo_carousel.fallback_titles("One case. Seven plates. #SHARPAL202H #knife"), ["🔴One case.#SHARPAL202H #knife"])
        plan = {"versions": [{"titles": ["Title one#SHARPAL202H #a", "🔴Two#b"], "titles_zh": ["标题一", "二"], "caption_en": "Text.",
                              "slides": [{"title_lines": ["A"]}]}]}
        version = parse_plan(json.dumps(plan))[0][0]
        self.assertEqual((version["titles"], version["titles_zh"]), (["🔴Title one#SHARPAL202H #a", "🔴Two#b"], ["标题一", "二"]))
        set_id = self.chosen()
        prompt = self.app.ai.bridge.calls[0][0]
        self.assertIn("3 行「🔴主题+tags」", prompt)
        state = self.sets.detail("PRD-0123456789ab", "uid-1")
        self.assertEqual(state["work"]["titles"], ["🔴One case.#KnifeSharpening"])  # written before titles existed
        state = self.sets.edit_set(set_id, {"titles": ["My own title #SHARPAL202H #knife", "  ", "Second"]})
        self.assertEqual((state["work"]["titles"], state["work"]["titles_zh"]), (["🔴My own title#SHARPAL202H #knife", "🔴Second"], []))
        state = self.sets.undo(set_id)
        self.assertEqual(state["work"]["titles"], ["🔴One case.#KnifeSharpening"])
        self.sets.redo(set_id)
        folder = Path(self.sets.export(set_id)["export"]["folder"])
        text = (folder / "发布文案.txt").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("🔴My own title#SHARPAL202H #knife\n🔴Second\n\n一箱装下。\n\nOne case. #KnifeSharpening"), text)
        self.assertEqual(json.loads((folder / "copy.json").read_text(encoding="utf-8"))["titles"][1], "🔴Second")

    def test_undo_can_be_redone_until_a_new_change(self):
        set_id = self.chosen()
        titles = lambda st: [s["title_lines"][0] for s in st["work"]["slides"]]
        self.sets.edit_slide(set_id, 1, {"title_lines": ["FIRST"]})
        state = self.sets.edit_slide(set_id, 1, {"title_lines": ["SECOND"]})
        self.assertEqual((state["work"]["can_undo"], state["work"]["can_redo"], state["work"]["undo_label"]), (True, False, "第 1 张：文案"))
        with self.assertRaises(PhotoSetError):
            self.sets.redo(set_id)
        state = self.sets.undo(set_id)
        state = self.sets.undo(set_id)
        self.assertEqual(titles(state)[0], "STOP BUYING STONES")
        self.assertEqual((state["work"]["can_redo"], state["work"]["redo_label"]), (True, "第 1 张：文案"))
        state = self.sets.redo(set_id)
        self.assertEqual(titles(state)[0], "FIRST")
        state = self.sets.redo(set_id)
        self.assertEqual(titles(state)[0], "SECOND")
        self.assertFalse(state["work"]["can_redo"])
        state = self.sets.undo(set_id)
        self.assertEqual(titles(state)[0], "FIRST")
        state = self.sets.arrange(set_id, 1, {"action": "duplicate"})  # a new change drops what could be redone
        self.assertFalse(state["work"]["can_redo"])
        state = self.sets.undo(set_id)
        self.assertEqual(titles(state), ["FIRST", "IT ALL PACKS"])
        state = self.sets.redo(set_id)
        self.assertEqual(titles(state), ["FIRST", "FIRST", "IT ALL PACKS"])

    def test_a_wording_follows_its_slide_after_moves_and_writing_can_be_stopped(self):
        set_id = self.chosen()
        self.app.ai.bridge.output = json.dumps({"options": [{"title_lines": ["AI WORDS"]}]})
        self.sets.rewrite(set_id, 1, {"zh": "别一块块买"})
        state = self.rewritten(set_id)
        self.assertTrue(state["slide_ai"]["slide_id"])
        self.sets.arrange(set_id, 1, {"action": "duplicate"})  # the copy gets an id of its own
        state = self.sets.arrange(set_id, 1, {"action": "move", "to": 3})
        ids = [s.get("id") for s in state["work"]["slides"]]
        self.assertEqual(ids[2], state["slide_ai"]["slide_id"])
        self.assertNotEqual(ids[0], ids[2])
        state = self.sets.pick_option(set_id, 1, {"index": 0})  # clicked while showing slide 1's number: lands on slide 3
        self.assertEqual([s["title_lines"][0] for s in state["work"]["slides"]], ["STOP BUYING STONES", "IT ALL PACKS", "AI WORDS"])
        self.assertEqual(state["slide_ai"]["number"], 3)
        self.sets.arrange(set_id, 3, {"action": "delete"})
        with self.assertRaises(PhotoSetError) as caught:
            self.sets.pick_option(set_id, 1, {"index": 0})
        self.assertIn("已经删掉", str(caught.exception))
        gate, started = threading.Event(), threading.Event()

        class Slow:
            def run(self, prompt, **options):
                started.set()
                while not gate.is_set() and not options["cancel_event"].is_set():
                    time.sleep(.05)
                return json.dumps({"options": [{"title_lines": ["LATE"]}]})
        self.app.ai.bridge = Slow()
        self.sets.rewrite(set_id, 1, {"zh": "中文"})
        started.wait(10)
        self.sets.stop_rewrite(set_id)
        state = self.rewritten(set_id)
        self.assertEqual((state["slide_ai"]["status"], state["slide_ai"]["error"], state["slide_ai"]["zh"]), ("cancelled", "已停止。", "中文"))
        data = self.sets._read(set_id)
        data["slide_ai"] = {"status": "running", "number": 1}
        self.sets._save(data)
        self.assertTrue(self.sets.busy())
        PhotoCarousels(self.app, export_root=self.tmp / "图文成品")  # a restart reports the cut-off job
        self.assertEqual(self.sets._read(set_id)["slide_ai"]["status"], "failed")

    def test_zoom_is_saved_per_cell_and_exported(self):
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        self.sets.choose(set_id, {"versions": ["A"]})
        plain = self.sets.rendered(set_id, "A", "bar", 1, 360)
        state = self.sets.edit_slide(set_id, 1, {"zoom": 2.5})
        self.assertEqual(state["work"]["slides"][0]["zoom"], 2.5)
        self.assertEqual(self.sets._read(set_id)["works"][0]["history"][-1]["label"], "第 1 张：放大")
        self.assertNotEqual(self.sets.rendered(set_id, "A", "bar", 1, 360), plain)
        state = self.sets.edit_slide(set_id, 1, {"zoom": 9})
        self.assertEqual(state["work"]["slides"][0]["zoom"], 3.0)
        with self.assertRaises(PhotoSetError):
            self.sets.edit_slide(set_id, 1, {"zoom": "big"})
        state = self.sets.edit_slide(set_id, 1, {"zoom": 1})
        self.assertNotIn("zoom", state["work"]["slides"][0])
        self.assertEqual(self.sets.rendered(set_id, "A", "bar", 1, 360), plain)  # back to the cached picture
        self.sets.edit_slide(set_id, 1, {"layout": "lr"})
        state = self.sets.edit_slide(set_id, 1, {"cell": 2, "zoom": 2, "crop_x": 0.2})
        slide = state["work"]["slides"][0]
        self.assertEqual((slide.get("zoom"), slide["cells"][0]["zoom"], slide["cells"][0]["crop_x"]), (None, 2.0, 0.2))
        self.assertEqual(self.sets._read(set_id)["works"][0]["history"][-1]["label"], "第 1 张第 2 格：裁切、放大")
        self.sets.edit_slide(set_id, 2, {"zoom": 1.6})
        result = self.sets.export(set_id)
        copy_json = json.loads((Path(result["export"]["folder"]) / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual([s["zoom"] for s in copy_json["slides"]], [1.0, 1.6])

    def test_each_slide_is_laid_out_once_for_every_preview_width(self):
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        self.sets.choose(set_id, {"versions": ["A"]})
        real, calls = render.render, []
        def counted(*args, **kwargs):
            calls.append(args[3])
            return real(*args, **kwargs)
        with patch.object(render, "render", counted):
            paths = [self.sets.rendered(set_id, "A", "bar", 1, width) for width in (270, 540, 270)]
            full = self.sets.rendered(set_id, "A", "bar", 1)
            # asked for at the same moment (the strip and the big preview after an edit): still one layout
            threads = [threading.Thread(target=self.sets.rendered, args=(set_id, "A", "bar", 2, width)) for width in (270, 540, 360)]
            [thread.start() for thread in threads]
            [thread.join() for thread in threads]
        self.assertEqual(calls, [1, 2])
        self.assertEqual([Image.open(path).size for path in paths], [(270, 360), (540, 720), (270, 360)])
        self.assertEqual(paths[0], paths[2])
        self.assertEqual(Image.open(full).size, (render.W, render.H))
        self.assertTrue(full.name.endswith("-full.jpg"))
        self.assertEqual(len(list((self.sets.root / set_id / "renders").glob("*.part"))), 0)

    def test_collage_cells_have_their_own_pictures_and_crops(self):
        set_id = self.ready()
        self.sets.plan(set_id)
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["ai"]["status"] != "running")
        state = self.sets.choose(set_id, {"versions": ["A"]})
        self.assertEqual(state["layouts"], {"single": "单图", "lr": "左右", "tb": "上下", "grid": "四格"})
        with self.assertRaises(PhotoSetError):  # a plain slide has only one cell
            self.sets.edit_slide(set_id, 1, {"cell": 2, "crop_x": 0})
        with self.assertRaises(PhotoSetError):
            self.sets.edit_slide(set_id, 1, {"layout": "mosaic"})
        first = state["work"]["slides"][0]["image"]
        state = self.sets.edit_slide(set_id, 1, {"layout": "lr"})
        slide = state["work"]["slides"][0]
        self.assertEqual((slide["layout"], slide["cells"]), ("lr", [{"image": first, "crop_x": 0.5, "crop_y": 0.5}]))
        state = self.sets.edit_slide(set_id, 1, {"cell": 2, "image": {"kind": "frame", "t": 1.6}, "crop_x": 0.1})
        slide = state["work"]["slides"][0]
        self.assertEqual(slide["image"], first)  # cell 1 untouched
        self.assertEqual(slide["cells"][0], {"image": {"kind": "frame", "t": 1.6}, "crop_x": 0.1, "crop_y": 0.5})
        self.assertEqual(self.sets.base_path(set_id, "A", 1, 2).name, "0009.jpg")
        with self.assertRaises(PhotoSetError):
            self.sets.base_path(set_id, "A", 1, 3)
        with self.assertRaises(PhotoSetError):
            self.sets.edit_slide(set_id, 1, {"cell": 3, "crop_x": 0})
        plain = self.sets.rendered(set_id, "A", "bar", 2, 360)
        collage = self.sets.rendered(set_id, "A", "bar", 1, 360)
        self.assertEqual(Image.open(collage).size, (360, 480))
        state = self.sets.edit_slide(set_id, 1, {"layout": "grid"})
        self.assertEqual(len(state["work"]["slides"][0]["cells"]), 3)
        self.assertNotEqual(self.sets.rendered(set_id, "A", "bar", 1, 360), collage)
        state = self.sets.edit_slide(set_id, 1, {"layout": "tb"})
        self.assertEqual(state["work"]["slides"][0]["cells"][0]["image"], {"kind": "frame", "t": 1.6})  # kept across layouts
        self.assertIn("第 1 张：版式", [h["label"] for h in self.sets._read(set_id)["works"][0]["history"]])
        result = self.sets.export(set_id)
        folder = Path(result["export"]["folder"])
        self.assertEqual(sorted(p.name for p in (folder / "source").iterdir()), ["01-2.jpg", "01.jpg", "02.jpg"])
        copy_json = json.loads((folder / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual(copy_json["slides"][0]["layout"], "上下")
        self.assertIn("第 2 格 视频帧 1.6 秒", copy_json["slides"][0]["source"])
        self.assertEqual(copy_json["slides"][1]["layout"], "单图")
        self.assertTrue(plain.is_file())

    def test_kit_version_slides_are_reworded_by_the_kit_rules(self):
        job = self.app.production.job("PRD-0123456789ab")
        slides = [{"title_lines": ["KNIFE GUYS.", "THIS KIT'S *FOR YOU.*"], "body_lines": ["6 abrasives + leather strop."]},
                  {"title_lines": ["FOUR DIAMOND GRITS."], "body_lines": ["220 · 325 · 600 · 1000"]},
                  {"title_lines": ["YOUR WHOLE SETUP.", "*ONE HARD CASE.*"], "body_lines": ["Store it together."]}]
        data = {"id": "PHS-x", "timeline_uid": "uid-1", "product": "202H"}
        kit = {"key": "B", "writing": "kit", "cta": "GET YOUR *202H* BELOW.", "slides": slides}
        first, middle, last = (self.sets.rewrite_prompt(data, job, kit, number, [], 1) for number in (1, 2, 3))
        self.assertIn("这是第 1 张：标题点名人群加整套", first)
        self.assertIn("这是中段：只讲一个部件或功能", middle)
        self.assertIn("标题和第二行说整套一箱带走", last)
        for prompt in (first, middle, last):
            self.assertIn(photo_carousel.KIT_SECOND_LINE_RULE, prompt)
            self.assertIn("这一版的写法（这一张也照它写）：" + photo_carousel.WRITING_RULES["kit"], prompt)
            self.assertNotIn(photo_carousel.SECOND_LINE_RULE, prompt)  # no spoken second line, no worry opening
            self.assertNotIn("处境或烦恼开场", prompt)
            self.assertNotIn(photo_carousel.CLOSING_REASON_RULE, prompt)
        # A version written the retired 顾虑问答 way keeps its own rule and the spoken second line.
        old = self.sets.rewrite_prompt(data, job, {**kit, "writing": "qa"}, 2, [], 1)
        self.assertIn("这一版的写法（这一张也照它写）：" + photo_carousel.WRITING_RULES["qa"], old)
        self.assertIn(photo_carousel.SECOND_LINE_RULE, old)

    def test_198h_routes_b_version_by_material_and_rewords_by_writing(self):
        self.assertEqual(self.sets._writings(None, product="198H"), ["self", "198h_b"])
        self.assertEqual(self.sets._writings(None, product="202H"), ["self", "kit"])
        job = {**self.app.production.job("PRD-0123456789ab"), "product": "198H",
               "lines": [{"number": 1, "spoken": "Plug it in and pull it through.", "zh": "插电拉过去就行。"}]}
        data = {"id": "PHS-x", "timeline_uid": "uid-1", "product": "198H"}
        prompt = self.sets.prompt(data, job, [])
        self.assertIn(photo_carousel.WRITING_RULES["self"], prompt)
        self.assertIn(photo_carousel.WRITING_RULES["rescue"], prompt)
        self.assertIn(photo_carousel.WRITING_RULES["slots"], prompt)
        self.assertIn(photo_carousel.ROUTED_198H_PLAN_RULE, prompt)
        self.assertIn("writing 依次是 self、rescue 或 slots（B 版二选一）", prompt)
        self.assertNotIn(photo_carousel.KIT_PLAN_RULE, prompt)
        for chosen in ("rescue", "slots"):
            payload = {"versions": [
                {"writing": "self", "recommended": True, "slides": [{"title_lines": ["A"]}]},
                {"writing": chosen, "recommended": False, "slides": [{"title_lines": ["B"]}]},
            ]}
            versions = parse_plan(json.dumps(payload), ["self", "198h_b"])[0]
            self.assertEqual([(v["key"], v["writing"], v["recommended"]) for v in versions],
                             [("A", "self", False), ("B", chosen, True)])
        # A retired writing returned in the 198H B slot falls back to slots and stays recommended.
        fallback = {"versions": [
            {"writing": "self", "slides": [{"title_lines": ["A"]}]},
            {"writing": "qa", "slides": [{"title_lines": ["B"]}]},
        ]}
        versions = parse_plan(json.dumps(fallback), ["self", "198h_b"])[0]
        self.assertEqual([(v["key"], v["writing"], v["recommended"]) for v in versions],
                         [("A", "self", False), ("B", "slots", True)])
        # Single-slide rewording uses slots and rescue rules respectively.
        slides = [{"title_lines": ["ONE"], "body_lines": ["a"]},
                  {"title_lines": ["TWO"], "body_lines": ["b"]},
                  {"title_lines": ["THREE"], "body_lines": ["c"]}]
        slots_first = self.sets.rewrite_prompt(data, job, {"key": "B", "writing": "slots", "slides": slides}, 1, [], 1)
        slots_mid = self.sets.rewrite_prompt(data, job, {"key": "B", "writing": "slots", "slides": slides}, 2, [], 1)
        self.assertIn("标题点名家庭厨师人群加一台搞定", slots_first)
        self.assertIn(photo_carousel.SLOTS_SECOND_LINE_RULE, slots_mid)
        self.assertNotIn(photo_carousel.SECOND_LINE_RULE, slots_mid)
        rescue_first = self.sets.rewrite_prompt(data, job, {"key": "B", "writing": "rescue", "slides": slides}, 1, [], 1)
        rescue_last = self.sets.rewrite_prompt(data, job, {"key": "B", "writing": "rescue", "slides": slides}, 3, [], 1)
        self.assertIn("磨前钝刀翻车的痛点", rescue_first)
        self.assertIn("磨后利落切开的结果", rescue_last)
        self.assertIn(photo_carousel.SECOND_LINE_RULE, rescue_last)

    def test_prompt_follows_three_part_chain_and_carries_video_hook_and_direction(self):
        direction = {"product": "202H", "direction_id": "202H-S1", "audience": "徒手磨石守不住角度的人",
                     "main_difference": "S1 夹持控角", "concern": "S1 → 夹得住吗", "closing": "下方看整套 202H"}
        self.app.directions = type("Directions", (), {"read": lambda self: {"directions": [direction]},
                                                      "find_direction": staticmethod(lambda data, product, identifier: next(
                                                          (d for d in data["directions"] if d["direction_id"] == identifier), None))})()
        job = {**self.app.production.job("PRD-0123456789ab"), "direction_id": "202H-S1",
               "timelines": [{"unique_id": "uid-1", "hook_overlay": {"text": "Less angle guesswork"}}],
               "lines": [{"number": 1, "spoken": "I hate whetstones.", "zh": "讨厌磨石", "proof": "手在磨石上来回"}]}
        prompt = self.sets.prompt({"id": "PHS-x", "timeline_uid": "uid-1", "product": "202H"}, job, [])
        for expected in ("开头屏显：Less angle guesswork", "目标人群：徒手磨石守不住角度的人", "收口：下方看整套 202H",
                         "画面：手在磨石上来回", "首张说到心里", "只讲 1–2 个卖点", "末张给下单理由", "不默认做送礼版"):
            self.assertIn(expected, prompt)
        self.assertNotIn("送礼、打消顾虑", prompt)
        for key in ("self", "kit"):  # each version is written one way, in order A/B
            self.assertIn(photo_carousel.WRITING_RULES[key], prompt)
        self.assertIn("A 版 " + photo_carousel.WRITING_RULES["self"], prompt)
        self.assertIn("B 版 " + photo_carousel.WRITING_RULES["kit"], prompt)
        self.assertIn(photo_carousel.SECOND_LINE_RULE, prompt)
        self.assertIn("优先缩短原话来用", prompt)
        self.assertIn("writing 依次是 self、kit", prompt)
        self.assertNotIn("事实行", prompt)
        # The closing is spoken, not read off the warranty terms (2026-11-06).
        self.assertIn(photo_carousel.CLOSING_VOICE_RULE, prompt)
        # Any fitting B-tier reason can close, not only warranty and price.
        self.assertIn(photo_carousel.CLOSING_REASON_RULE, prompt)
        for reason in ("退换", "整箱带走", "4 年"):
            self.assertIn(reason, prompt)
        self.assertNotIn("不把注册延保算进去", prompt)
        self.assertNotIn("原样说年限", prompt)
        self.assertNotIn("去看今天的价格", prompt)
        # No direction store and no hook: still a valid prompt that says where to read the audience from.
        del self.app.directions
        bare = self.sets.prompt({"id": "PHS-x", "timeline_uid": "uid-1", "product": "202H"}, self.app.production.job("PRD-0123456789ab"), [])
        self.assertIn("这条视频没有登记方向", bare)

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

    def test_day_export_queues_open_timelines_and_reports_the_rest(self):
        job = "PRD-0123456789ab"
        gate = threading.Event()
        self.addCleanup(gate.set)
        calls, entered = self.bind_resolve(gate=gate)
        for body in ({}, {"items": []}, {"items": [{"job_id": job}]}, {"items": [{"job_id": job, "timeline_uid": "uid-1"}], "x": 1},
                     {"items": [{"job_id": job, "timeline_uid": str(n)} for n in range(61)]}):
            with self.assertRaises(PhotoSetError):
                self.sets.import_resolve_many(body)
        self.sets.decide(job, "uid-1", {"decision": "skip"})
        reply = self.sets.import_resolve_many({"items": [{"job_id": job, "timeline_uid": "uid-1"}]})
        self.assertEqual(reply, {"started": [], "skipped": [{"job_id": job, "timeline_uid": "uid-1", "error": "已选不做"}]})
        self.sets.decide(job, "uid-1", {"decision": "clear"})
        items = [{"job_id": job, "timeline_uid": "uid-1"}, {"job_id": job, "timeline_uid": "uid-9"}, {"job_id": job, "timeline_uid": "uid-1"}]
        reply = self.sets.import_resolve_many({"items": items})
        self.assertEqual(reply["started"], [{"job_id": job, "timeline_uid": "uid-1"}])
        self.assertEqual(reply["skipped"], [{"job_id": job, "timeline_uid": "uid-9", "error": "请先选一条时间线。"}])
        self.assertTrue(entered.wait(5))
        state = self.sets.states([(job, "uid-1")])[(job, "uid-1")]
        self.assertEqual((state["state"], state.get("importing")), ("pending", True))
        # Asking again while it runs neither fails nor starts a second export.
        self.assertEqual(self.sets.import_resolve_many({"items": items[:1]})["started"], items[:1])
        gate.set()
        wait(lambda: self.sets.detail(job, "uid-1")["resolve_import"]["status"] != "running")
        wait(lambda: self.sets.detail(job, "uid-1")["frames_job"]["status"] != "running")
        self.assertEqual(len([call for call in calls if call["command"] == "export_textless"]), 1)
        state = self.sets.states([(job, "uid-1")])[(job, "uid-1")]
        self.assertEqual(state["state"], "doing")
        self.assertNotIn("importing", state)
        reply = self.sets.import_resolve_many({"items": items[:1]})
        self.assertEqual(reply["skipped"], [{"job_id": job, "timeline_uid": "uid-1", "error": "已经在做图文"}])

    def test_failed_import_shows_on_the_undecided_row(self):
        self.bind_resolve(fail=True)
        self.sets.import_resolve_many({"items": [{"job_id": "PRD-0123456789ab", "timeline_uid": "uid-1"}]})
        wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
        state = self.sets.states([("PRD-0123456789ab", "uid-1")])[("PRD-0123456789ab", "uid-1")]
        self.assertEqual(state["state"], "pending")
        self.assertTrue(state["import_error"])

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

    def test_resolve_import_reports_recovery_failure_once_without_mislabeling_tracks(self):
        self.bind_resolve()
        reason = "原导出设置备份已丢失。请核对达芬奇后重试。"
        initial = "导出后恢复未完成：" + reason
        for error, expected in ((initial, initial), ("导出超时。", "导出超时。再次恢复时，" + reason)):
            with self.subTest(error=error), patch.object(self.app.resolve, "run",
                    side_effect=[RuntimeError(error), RuntimeError(reason)]):
                self.sets.import_resolve("PRD-0123456789ab", {"timeline_uid": "uid-1"})
                wait(lambda: self.sets.detail("PRD-0123456789ab", "uid-1")["resolve_import"]["status"] != "running")
                detail = self.sets.detail("PRD-0123456789ab", "uid-1")
                self.assertEqual(detail["resolve_import"]["error"], expected)
                self.assertFalse(detail["exists"])


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

    def test_day_export_route_hands_the_list_over(self):
        reply = {"started": [], "skipped": []}
        items = {"items": [{"job_id": "PRD-0123456789ab", "timeline_uid": "uid-2"}]}
        with patch.object(self.server.app.photo_sets, "import_resolve_many", return_value=reply) as start:
            status, _, body = self.request("POST", "/api/photo-sets/import-day", items, authenticated=False)
            self.assertEqual(status, 403, body)
            start.assert_not_called()
            status, _, body = self.request("POST", "/api/photo-sets/import-day", items)
            self.assertEqual((status, body), (202, reply))
            start.assert_called_once_with(items)

    def test_running_photo_import_blocks_restart_and_data_replacement(self):
        with patch.object(self.server.app.photo_sets, "busy", return_value=True):
            self.assertIn("图文导入", self.server._activity_blocker())
            with self.assertRaisesRegex(Exception, "图文导入"):
                self.server.app.ensure_data_idle()


if __name__ == "__main__":
    unittest.main()
