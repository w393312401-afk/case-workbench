"""定稿图文批量出外语版（2026-11-24，方案 C）：画面照搬、评论贴纸不带、写完可自动定稿导出。"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import photo_carousel
from photo_carousel import PhotoCarousels, foreign_slides, parse_translation, PhotoSetError
from test_photo_carousel import App, PLAN, wait

JOB, UID = "PRD-0123456789ab", "uid-1"
GERMAN = {"slides": [{"number": 1, "title_lines": ["HÖR AUF, STEINE", "*EINZELN* ZU KAUFEN."], "body_lines": ["7 Platten in einem Koffer"],
                      "title_zh": "别再一块块买磨石", "body_zh": "一个箱子 7 块磨板"},
                     {"number": 2, "title_lines": ["ALLES PASST", "*ZURÜCK* REIN."], "body_lines": ["3 Jahre Garantie"],
                      "title_zh": "全都放得回去", "body_zh": "3 年质保"}],
          "cta": "", "titles": ["🔴Ein Koffer, alles drin #SHARPAL202H #messerschärfen"], "titles_zh": ["一个箱子全装下"],
          "caption_en": "Ein Koffer, sieben Platten. #messerschärfen", "caption_zh": "一个箱子，七块磨板。"}


class TranslateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = App(self.tmp, json.dumps(PLAN, ensure_ascii=False))
        self.sets = PhotoCarousels(self.app, export_root=self.tmp / "图文成品")
        video = self.app.staged / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=2",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
        self.set_id = self.sets.create(JOB, {"timeline_uid": UID, "video_path": str(video)})["id"]
        wait(lambda: self.detail()["frames_job"]["status"] != "running")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def detail(self):
        return self.sets.detail(JOB, UID)

    def finalize_a_with_comments(self):
        self.sets.plan(self.set_id)
        wait(lambda: self.detail()["ai"]["status"] != "running")
        self.sets.choose(self.set_id, {"versions": ["A"]})
        path = self.sets.root / self.set_id / "set.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        slides = data["works"][0]["slides"]
        slides[0]["comment"] = {"review_id": "r1", "text": "I think it's too expensive", "username": "@someone", "cover": True}
        slides.insert(1, {**slides[1], "title_lines": ["MY ANSWER"], "body_lines": [], "comment": {"review_id": "r2", "text": "Does it fit?", "username": "@b"}})
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.sets.export(self.set_id)

    def translate(self, **extra):
        self.app.ai.bridge.output = json.dumps(GERMAN, ensure_ascii=False)
        reply = self.sets.translate_many({"items": [{"job_id": JOB, "timeline_uid": UID}], **extra})
        wait(lambda: (self.sets._read(self.set_id).get("translate") or {}).get("status") in {"done", "failed"})
        return reply

    def test_finalized_version_becomes_a_german_export_without_comment_stickers(self):
        self.finalize_a_with_comments()
        state = self.sets.states([(JOB, UID)])[(JOB, UID)]
        self.assertTrue(state["translatable"])
        reply = self.translate()
        self.assertEqual((len(reply["started"]), reply["skipped"]), (1, []))
        data = self.sets._read(self.set_id)
        self.assertEqual(data["translate"]["status"], "done", data["translate"])
        work = next(w for w in data["works"] if w["key"] == "A-DE")
        source = next(w for w in data["works"] if w["key"] == "A")
        self.assertEqual((work["source"], work["market"], work["status"]), ("A", "de", "done"))
        self.assertEqual(len(work["slides"]), 2)  # the comment-only slide is gone
        self.assertFalse(any(s.get("comment") for s in work["slides"]))
        self.assertEqual(work["slides"][0]["title_lines"], ["HÖR AUF, STEINE", "*EINZELN* ZU KAUFEN."])
        self.assertEqual([s["image"] for s in work["slides"]], [source["slides"][0]["image"], source["slides"][2]["image"]])
        self.assertEqual(work["slides"][0].get("crop_y"), source["slides"][0].get("crop_y"))
        self.assertTrue(source["slides"][0].get("comment"))  # the English version is untouched
        self.assertEqual(data["current"], "A")
        prompt = self.app.ai.bridge.calls[-1][0]
        self.assertIn("改成德国 · 德语版", prompt)
        self.assertIn("本轮目标市场：德国 · 德语", prompt)
        self.assertIn("slides 正好 2 张", prompt)
        self.assertNotIn("too expensive", prompt)
        self.assertNotIn("MY ANSWER", prompt)
        folder = Path(work["exports"][-1]["folder"])
        self.assertEqual("DE-202-Zack-1104-1A", folder.name)  # 成品统一命名：外语版仍是 A，国家放最前
        self.assertEqual(len(list((folder / "发布JPG").glob("*.jpg"))), 2)
        frozen = json.loads((folder / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual((frozen["market"], frozen["translated_from"], frozen["caption_en"]), ("DE", "A", GERMAN["caption_en"]))
        self.assertNotIn("comment", frozen["slides"][0])
        self.assertTrue(self.sets.rendered(self.set_id, "A-DE", "bar", 1).is_file())
        state = self.sets.states([(JOB, UID)])[(JOB, UID)]
        self.assertEqual(state["foreign"], [{"market": "de", "label": "DE", "keys": ["A-DE"], "done": 1}])
        self.assertNotIn("translating", state)
        # Asking again finds nothing left to translate into German; British English is still open.
        again = self.sets.translate_many({"items": [{"job_id": JOB, "timeline_uid": UID}]})
        self.assertEqual(again["skipped"][0]["error"], "已经有德语版")
        self.assertEqual([w["key"] for w in self.sets._translate_sources(self.sets._read(self.set_id), "uk")], ["A"])

    def test_without_auto_export_the_new_version_waits_in_the_queue(self):
        self.finalize_a_with_comments()
        self.translate(market="uk", export=False)
        data = self.sets._read(self.set_id)
        work = next(w for w in data["works"] if w["key"] == "A-UK")
        self.assertEqual((work["status"], work["exports"], data["current"]), ("doing", [], "A-UK"))
        self.assertEqual(self.sets.export(self.set_id)["works"][-1]["status"], "done")

    def test_a_wrong_slide_count_fails_and_adds_nothing(self):
        self.finalize_a_with_comments()
        self.app.ai.bridge.output = json.dumps({**GERMAN, "slides": GERMAN["slides"][:1]}, ensure_ascii=False)
        self.sets.translate_many({"items": [{"job_id": JOB, "timeline_uid": UID}]})
        wait(lambda: (self.sets._read(self.set_id).get("translate") or {}).get("status") in {"done", "failed"})
        data = self.sets._read(self.set_id)
        self.assertEqual(data["translate"]["status"], "failed")
        self.assertIn("原版是 2 张", data["translate"]["error"])
        self.assertEqual([w["key"] for w in data["works"]], ["A"])
        self.assertEqual(self.sets.states([(JOB, UID)])[(JOB, UID)]["translating"]["status"], "failed")
        self.assertIn("出外语版", [row["step"] for row in self.sets.failures()])

    def test_only_finalized_sets_are_queued_and_bad_requests_are_refused(self):
        reply = self.sets.translate_many({"items": [{"job_id": JOB, "timeline_uid": UID}, {"job_id": JOB, "timeline_uid": "nope"}]})
        self.assertEqual([row["error"] for row in reply["skipped"]], ["还没有定稿的版本", "还没做图文"])
        for body in ({"items": []}, {"items": [{"job_id": JOB, "timeline_uid": UID}], "market": "fr"},
                     {"items": [{"job_id": JOB, "timeline_uid": UID}], "export": "yes"}, {"items": [], "x": 1}):
            with self.assertRaises(PhotoSetError):
                self.sets.translate_many(body)

    def test_restart_marks_an_unfinished_batch_failed(self):
        path = self.sets.root / self.set_id / "set.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["translate"] = {"status": "queued", "market": "de"}
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertTrue(self.sets.busy())
        PhotoCarousels(self.app, export_root=self.tmp / "图文成品")
        self.assertEqual(self.sets._read(self.set_id)["translate"]["status"], "failed")


class HelperTests(unittest.TestCase):
    def test_foreign_slides_drop_stickers_and_comment_pages(self):
        slides = [{"title_lines": ["A"], "comment": {"text": "x", "cover": True}}, {"title_lines": ["B"], "comment": {"text": "y"}}, {"title_lines": ["C"]}]
        out = foreign_slides(slides)
        self.assertEqual([s["title_lines"] for s in out], [["A"], ["C"]])
        self.assertTrue(all("comment" not in s for s in out))
        self.assertIn("comment", slides[0])

    def test_parse_translation_keeps_missing_second_lines_missing(self):
        slides = [{"title_lines": ["A"], "body_lines": []}]
        words = parse_translation(json.dumps({"slides": [{"title_lines": ["X"], "body_lines": ["extra"]}], "caption_en": "c"}), slides, {"caption_en": "c"})
        self.assertEqual(words["slides"][0]["body_lines"], [])
        with self.assertRaises(PhotoSetError):
            parse_translation("{}", slides, {})
        with self.assertRaises(PhotoSetError):
            parse_translation(json.dumps({"slides": [{"title_lines": []}], "caption_en": "c"}), slides, {})


if __name__ == "__main__":
    unittest.main()
