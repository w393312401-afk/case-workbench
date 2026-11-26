import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image

import carousel_render as render
import photo_comments
from photo_carousel import MAX_SLIDES, PhotoCarousels, PhotoSetError
from test_photo_carousel import PLAN, App, wait

KEYWORDS = {"cover": [{"word": "Slip", "zh": "打滑"}, {"word": "slip", "zh": "重复"}, {"word": "worth it", "zh": "值不值"}],
            "middle": [{"word": "two weeks", "zh": "两周"}, {"word": "", "zh": "空"}]}


def setUpModule():
    # The module is archived (hidden, off) in the app; these tests keep its code working for when it comes back.
    global _archived
    _archived, photo_comments.ARCHIVED = photo_comments.ARCHIVED, False


def tearDownModule():
    photo_comments.ARCHIVED = _archived

def row(identifier, text, likes=0, nature=(), video="7000000000000000001", handle="sam_k", avatar=None):
    return {"id": identifier, "text": text, "likes": likes, "tags": {"nature": list(nature)}, "source_ref": video, "posted_on": "2026-11-01",
            "author_handle": handle, "author_name": "Sam K", "avatar": avatar}


class Avatars:
    def __init__(self, folder):
        self.folder = folder

    def file(self, key):
        path = self.folder / f"{key}.png"
        return path if path.is_file() else None


class Reviews:
    def __init__(self, own=(), product=()):
        self.own, self.product, self.queries = list(own), list(product), []

    def search(self, query, limit=50, offset=0, sort="likes"):
        self.queries.append(query)
        return {"reviews": self.own if query.get("video") else self.product}


class Publications:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class Bridge:
    """Plans for the plan prompt, search words for the comment prompt."""
    def __init__(self):
        self.calls = []

    def run(self, prompt, **options):
        self.calls.append((prompt, options))
        return json.dumps(KEYWORDS if "搜索关键词" in prompt else PLAN, ensure_ascii=False)


class CandidateTests(unittest.TestCase):
    def test_own_video_first_and_unusable_comments_left_out(self):
        app = type("A", (), {})()
        app.reviews = Reviews(
            own=[row("own1", "Mine still wobbles on thin knives 😅", 1, ["defect"], "7000000000000000009")],
            product=[row("p1", "Got it last month, my chef knife shaves hair now", 50, ["owner", "result"]),
                     row("p2", "@sam you need this one for real", 90, ["advice"]),
                     row("p3", "lol", 100),
                     row("p4", "how much is it and where can I buy", 80, ["buy_info"]),
                     row("own1", "duplicate of the own comment text", 1)])
        app._video_publications = lambda: Publications({"jobs": {"PRD-1": {"publication": {
            "post_id": "7000000000000000009", "source": {"timeline": {"unique_id": "uid-1"}}}, "history": []}}})
        pool = photo_comments.candidates(app, {"job_id": "PRD-1", "timeline_uid": "uid-1", "product": "202H"})
        self.assertEqual([item["id"] for item in pool], ["own1", "p1"])
        self.assertEqual(pool[0]["text"], "Mine still wobbles on thin knives")
        self.assertTrue(pool[0]["same_video"])
        self.assertEqual(pool[0]["username"], "@sam_k")
        self.assertEqual(pool[0]["kind"], "concern")
        self.assertEqual(app.reviews.queries[0]["video"], "7000000000000000009")
        self.assertEqual(app.reviews.queries[1]["product"], "202H")

    def test_no_review_library_gives_nothing(self):
        self.assertEqual(photo_comments.candidates(object(), {"product": "202H"}), [])


class PlaceTests(unittest.TestCase):
    def slides(self, n):
        return [{"title_lines": [f"S{i}"], "image": {"kind": "frame", "t": float(i)}, "alternates": [float(i), i + .5]} for i in range(n)]

    def cover(self, identifier):
        return {"id": identifier, "slot": "cover", "text": f"comment {identifier}"}

    def middle(self, identifier):
        return {"id": identifier, "slot": "middle", "text": f"comment {identifier}"}

    def test_cover_sticker_and_a_middle_slide_second_to_last(self):
        items = [{"t": t / 5, "sharp": 1} for t in range(30)]
        slides, placed, skipped = photo_comments.place(self.slides(4), [self.cover("x"), self.middle("y")], "A", items, 8)
        self.assertEqual((placed, skipped), (["x", "y"], []))
        self.assertEqual([(s.get("comment") or {}).get("review_id") for s in slides], ["x", None, None, "y", None])
        self.assertTrue(slides[0]["comment"]["cover"])
        self.assertEqual(slides[0]["title_lines"], ["S0"], "the cover keeps its own title")
        self.assertEqual(slides[3]["title_lines"], [], "the user writes the middle slide's title")
        times = [s["image"]["t"] for s in slides]
        self.assertEqual(len(times), len({round(t, 1) for t in times}), "a comment slide reuses no picture")
        swapped, placed, _ = photo_comments.place(slides, [self.cover("z")], "A", items, 8)
        self.assertEqual((placed, swapped[0]["comment"]["review_id"], len(swapped)), (["z"], "z", 5))

    def test_full_set_and_repeats(self):
        slides, placed, skipped = photo_comments.place(self.slides(8), [self.cover("x"), self.middle("y")], "A", [], 8)
        self.assertEqual((len(slides), placed, skipped), (8, ["x"], ["y"]))
        again, placed, _ = photo_comments.place(slides, [self.cover("x")], "A", [], 9)
        self.assertEqual((len(again), placed), (8, []))


class SearchTests(unittest.TestCase):
    def item(self, identifier, text, score=0):
        return {"id": identifier, "text": text, "score": score, "same_video": False}

    def test_any_word_matches_whole_words_more_matches_first(self):
        pool = [self.item("a", "Is it worth it though?", 50), self.item("b", "Does it slip? Worth it?", 1),
                self.item("c", "slippery handle", 99), self.item("d", " ".join(["slip"] * 20), 99)]
        hits, total = photo_comments.search(pool, ["Worth  It", "slip", "worth it"], "cover")
        self.assertEqual([h["id"] for h in hits], ["b", "a"], "slippery is not slip; d is too long for a cover sticker")
        self.assertEqual((total, hits[0]["matched"]), (2, ["worth it", "slip"]))
        self.assertNotIn("score", hits[0])
        hits, _ = photo_comments.search(pool, ["slip"], "middle")
        self.assertEqual([h["id"] for h in hits], ["d", "b"], "a middle slide can hold a longer comment")
        self.assertEqual(photo_comments.search(pool, [], "cover"), ([], 0))

    def test_chinese_word_also_finds_the_chinese_translation(self):
        pool = [{**self.item("a", "the clamp keeps moving around", 1), "zh": "夹具一直乱动"}, self.item("b", "great buy", 2)]
        hits, total = photo_comments.search(pool, ["乱动"], "middle")
        self.assertEqual(([h["id"] for h in hits], total, hits[0]["matched"]), (["a"], 1, ["乱动"]))

    def test_chinese_word_searches_its_english_words(self):
        from review_taxonomy import zh_groups
        word = next((w for w in ("锋利", "好用", "值") if zh_groups(w)), None)
        if not word:
            self.skipTest("no Chinese word list")
        english = zh_groups(word)[0][1][0]
        hits, _ = photo_comments.search([self.item("a", f"so {english} after a month")], [word], "middle")
        self.assertEqual([h["id"] for h in hits], ["a"])

    def test_keywords_from_the_ai(self):
        words = photo_comments.parse_keywords(json.dumps(KEYWORDS))
        self.assertEqual(words, {"cover": [{"word": "slip", "zh": "打滑"}, {"word": "worth it", "zh": "值不值"}],
                                 "middle": [{"word": "two weeks", "zh": "两周"}]})
        with self.assertRaises(PhotoSetError):
            photo_comments.parse_keywords(json.dumps({"cover": [], "middle": []}))


class RenderTests(unittest.TestCase):
    def test_comment_bubble_on_top_answer_below(self):
        image = Image.new("RGB", (720, 1280), (40, 90, 140))
        slide = {"title_lines": ["IT *HOLDS.*"], "body_lines": ["Thin blades stay flat."], "text_position": "split",
                 "comment": {"text": "Does it really hold a thin fillet knife without slipping around while you sharpen?"}}
        plain = render.render({k: v for k, v in slide.items() if k != "comment"}, image, "bar", 2, 5)
        out = render.render(slide, image, "bar", 2, 5)
        self.assertEqual(out.size, (render.W, render.H))
        self.assertGreater(sum(out.getpixel((75, y))[0] for y in range(100, 200, 10)) / 10, 230, "white bubble at the top")
        self.assertNotEqual(list(out.crop((0, 0, render.W, 300)).getdata()), list(plain.crop((0, 0, render.W, 300)).getdata()))
        for style in render.STYLES:
            render.render(slide, image, style, 2, 5)
        named = render.render({**slide, "comment": {**slide["comment"], "username": "@sam_k"}}, image, "bar", 2, 5)
        self.assertNotEqual(list(named.crop((0, 0, render.W, 200)).getdata()), list(out.crop((0, 0, render.W, 200)).getdata()))
        self.assertEqual(render.comment_label("@sam_k"), "@sam_k")
        self.assertNotIn("Reply", render.comment_label(""))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = App(self.tmp, "")
        self.app.ai.bridge = Bridge()
        self.app.reviews = Reviews(product=[row("r1", "Used it for two weeks, edges stay crazy sharp", 12, ["owner"], handle="", avatar=None),
                                            row("r2", "Do thin fillet knives slip in the clamp though?", 30, ["question"], handle="knife.guy", avatar="ab12")])
        faces = self.tmp / "avatars"
        faces.mkdir()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(faces / "ab12.png")
        self.app.reviews.avatars = Avatars(faces)
        self.sets = PhotoCarousels(self.app, export_root=self.tmp / "图文成品")
        video = self.app.staged / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=270x480:rate=30:duration=2",
                        "-pix_fmt", "yuv420p", str(video)], check=True)
        self.set_id = self.sets.create("PRD-0123456789ab", {"timeline_uid": "uid-1", "video_path": str(video)})["id"]
        wait(lambda: self.detail()["frames_job"]["status"] != "running")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def detail(self):
        return self.sets.detail("PRD-0123456789ab", "uid-1")

    def test_plan_suggests_words_and_picked_comments_go_into_new_versions(self):
        self.sets.plan(self.set_id)
        wait(lambda: (self.detail().get("comment_ai") or {}).get("status") == "done")
        task = self.detail()["comment_ai"]
        self.assertEqual([k["word"] for k in task["keywords"]["cover"]], ["slip", "worth it"])
        self.assertEqual([m["id"] for m in task["matches"]["cover"]], ["r2"])
        self.assertEqual([m["id"] for m in task["matches"]["middle"]], ["r1"])
        self.assertEqual((task["found"], task["pool"]), ({"cover": 1, "middle": 1}, 2))
        self.assertEqual(task["picks"], [], "the user picks, not the AI")
        prompt, options = self.app.ai.bridge.calls[-1]
        self.assertIn("Do thin fillet knives slip", prompt, "the AI sees how people talk")
        self.assertNotIn("images", options)
        with self.assertRaises(PhotoSetError):
            self.sets.pick_comments(self.set_id, {"slot": "cover", "id": "r9"})
        self.sets.pick_comments(self.set_id, {"slot": "cover", "id": "r2"})
        with self.assertRaises(PhotoSetError):
            self.sets.pick_comments(self.set_id, {"slot": "middle", "id": "r2"})
        out = self.sets.choose(self.set_id, {"versions": ["A"]})
        slides = out["work"]["slides"]
        self.assertEqual(len(slides), 2, "the cover comment is a sticker, no slide added")
        cover = slides[0]
        self.assertEqual(cover["comment"]["text"], "Do thin fillet knives slip in the clamp though?")
        self.assertTrue(cover["comment"]["cover"])
        self.assertEqual(cover["title_lines"], ["STOP BUYING STONES", "*ONE BY ONE.*"], "the cover keeps its title")
        self.assertEqual(cover["comment"]["username"], "@knife.guy")
        self.assertTrue((self.sets.root / self.set_id / "avatars" / cover["comment"]["avatar_file"]).is_file(), "the picture is kept with the set")
        picture = Image.open(self.sets.rendered(self.set_id, "A", "bar", 1))
        self.assertGreater(picture.getpixel((60 + 32 + 23, 40 + 40 + 26 + 18))[0], 150, "the commenter's red picture is on the card")
        # The middle comment picked later goes into the version already being made, second to last, and can be undone.
        out = self.sets.pick_comments(self.set_id, {"slot": "middle", "id": "r1"})
        self.assertEqual(out["comment_ai"]["chosen"], ["r2", "r1"])
        out = self.sets.apply_comments(self.set_id, {"key": "A"})
        self.assertEqual([(s.get("comment") or {}).get("review_id") for s in out["work"]["slides"]], ["r2", "r1", None])
        with self.assertRaises(PhotoSetError):
            self.sets.apply_comments(self.set_id, {"key": "A"})
        self.assertEqual(len(self.sets.undo(self.set_id)["work"]["slides"]), 2)
        self.sets.apply_comments(self.set_id, {"key": "A"})
        exported = self.sets.export(self.set_id)["export"]
        copy_json = json.loads((Path(exported["folder"]) / "copy.json").read_text(encoding="utf-8"))
        self.assertEqual(copy_json["slides"][0]["comment"]["review_id"], "r2")
        self.assertEqual(copy_json["slides"][0]["comment"]["username"], "@knife.guy")
        # Clearing a place takes it off the list for versions started later.
        self.assertEqual(self.sets.pick_comments(self.set_id, {"slot": "middle", "id": None})["comment_ai"]["chosen"], ["r2"])

    def test_own_words_search_without_ai_and_keep_picks(self):
        self.sets.plan(self.set_id)
        wait(lambda: (self.detail().get("comment_ai") or {}).get("status") == "done")
        calls = len(self.app.ai.bridge.calls)
        self.sets.pick_comments(self.set_id, {"slot": "cover", "id": "r2"})
        out = self.sets.search_comments(self.set_id, {"keywords": {"cover": ["clamp", "slip", "Clamp"], "middle": ["sharp"]}})
        task = out["comment_ai"]
        self.assertEqual(len(self.app.ai.bridge.calls), calls)
        self.assertEqual(task["keywords"]["cover"], [{"word": "clamp", "zh": ""}, {"word": "slip", "zh": "打滑"}])
        self.assertEqual([m["matched"] for m in task["matches"]["cover"]], [["clamp", "slip"]])
        self.assertEqual([m["id"] for m in task["matches"]["middle"]], ["r1"])
        self.assertEqual(task["chosen"], ["r2"])
        with self.assertRaises(PhotoSetError):
            self.sets.search_comments(self.set_id, {"keywords": "slip"})

    def test_old_recommendations_start_over(self):
        path = self.sets.root / self.set_id / "set.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["comment_ai"] = {"status": "done", "picks": [{"id": "r2", "slot": "cover", "text": "x"}], "chosen": ["r2"]}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertIsNone(self.detail()["comment_ai"])
        self.app.ai.bridge.calls.clear()
        self.sets.plan(self.set_id)
        wait(lambda: (self.detail().get("comment_ai") or {}).get("status") == "done")
        self.assertFalse(any(s.get("comment") for s in self.sets.choose(self.set_id, {"versions": ["A"]})["work"]["slides"]))

    def test_archived_module_is_off(self):
        photo_comments.ARCHIVED = True
        try:
            self.sets.plan(self.set_id)
            wait(lambda: (self.detail().get("ai") or {}).get("status") == "done")
            self.assertEqual(len(self.app.ai.bridge.calls), 1, "no keyword run after the plan")
            self.assertTrue(self.detail()["comments_archived"])
            for call in (lambda: self.sets.suggest_comments(self.set_id), lambda: self.sets.pick_comments(self.set_id, {"slot": "cover", "id": "r2"}),
                         lambda: self.sets.search_comments(self.set_id, {"keywords": {"cover": ["slip"]}})):
                with self.assertRaises(PhotoSetError):
                    call()
        finally:
            photo_comments.ARCHIVED = False

    def test_nothing_to_search_calls_no_ai(self):
        self.app.reviews = Reviews()
        self.sets.plan(self.set_id)
        wait(lambda: (self.detail().get("comment_ai") or {}).get("status") == "empty")
        self.assertEqual(len(self.app.ai.bridge.calls), 1)
        self.assertIn("评论库里没有", self.detail()["comment_ai"]["note"])

if __name__ == "__main__":
    unittest.main()
