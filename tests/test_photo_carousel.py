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

    def test_three_versions_take_the_three_writings_in_order(self):
        plan = {"versions": [{"slides": [{"title_lines": ["A"]}]}, {"writing": "list", "slides": [{"title_lines": ["B"]}]},
                             {"writing": "nonsense", "slides": [{"title_lines": ["C"]}]}]}
        versions = parse_plan(json.dumps(plan))[0]
        self.assertEqual([v["writing"] for v in versions], ["self", "list", "list"])  # the AI's own valid choice is kept
        self.assertEqual(photo_carousel.new_work(versions[0])["writing"], "self")
        self.assertEqual(photo_carousel.WRITINGS, {"self": "达人自述", "qa": "顾虑问答", "list": "理由清单"})

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
        for key in ("self", "qa", "list"):  # each version is written one way, in order A/B/C
            self.assertIn(photo_carousel.WRITING_RULES[key], prompt)
        self.assertIn("A 版 " + photo_carousel.WRITING_RULES["self"], prompt)
        self.assertIn("C 版 " + photo_carousel.WRITING_RULES["list"], prompt)
        self.assertIn(photo_carousel.SECOND_LINE_RULE, prompt)
        self.assertIn("优先缩短原话来用", prompt)
        self.assertIn("writing 依次是 self、qa、list", prompt)
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

    def test_running_photo_import_blocks_restart_and_data_replacement(self):
        with patch.object(self.server.app.photo_sets, "busy", return_value=True):
            self.assertIn("图文导入", self.server._activity_blocker())
            with self.assertRaisesRegex(Exception, "图文导入"):
                self.server.app.ensure_data_idle()


if __name__ == "__main__":
    unittest.main()
