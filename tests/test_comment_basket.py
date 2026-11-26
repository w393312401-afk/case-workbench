"""Comment stickers used in a finalized 图文 go into the 摘录篮 on their own, once, with where they were used."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from photo_carousel import PhotoCarousels
from review_store import ReviewStore
from test_review_store import build_sources

SET = "PHS-0123456789ab"


class App:
    def __init__(self, root):
        self.runtime = root / "runtime"
        self.reviews = ReviewStore(self.runtime)
        build_sources(self.reviews.sources)
        self.reviews.sync()


def comment_set(first, second):
    slide = {"title_lines": ["A"], "body_lines": [], "image": {"kind": "frame", "t": 0.4}, "layout": "single", "cells": []}
    return {"id": SET, "job_id": "PRD-0123456789ab", "timeline_uid": "uid-1", "case_id": "PVR-0123456789ab", "product": "202H",
            "versions": [{"key": "A", "name_zh": "一套配齐"}, {"key": "B", "name_zh": "顾虑问答"}],
            "current": "B",
            "works": [{"key": "A", "name_zh": "一套配齐", "status": "done", "exports": [{"name": "PVR-20261118-A一套配齐", "at": "x"}],
                       "slides": [{**slide, "comment": {"review_id": first, "text": "t", "cover": True}},
                                  {**slide, "comment": {"review_id": second, "text": "t"}}, slide]},
                      {"key": "B", "name_zh": "顾虑问答", "status": "doing", "exports": [],
                       "slides": [{**slide, "comment": {"review_id": second, "text": "t", "cover": True}}]}]}


class CommentBasketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="comment-basket-")
        self.addCleanup(self.temp.cleanup)
        self.app = App(Path(self.temp.name))
        self.store = self.app.reviews
        ids = [r["id"] for r in self.store.search({}, limit=100)["reviews"]]
        self.first, self.second, self.third = ids[:3]
        self.sets = PhotoCarousels(self.app, export_root=Path(self.temp.name) / "图文成品")
        folder = self.sets.root / SET
        folder.mkdir(parents=True)
        (folder / "set.json").write_text(json.dumps(comment_set(self.first, self.second), ensure_ascii=False), encoding="utf-8")

    def picks(self):
        return {r["id"]: r["pick"] for r in self.store.picks()}

    def test_note_used_adds_once_and_keeps_a_hand_pick_as_it_was(self):
        self.store.set_pick(self.second, True, "开头可用")
        before = self.picks()[self.second]["added"]
        source = {"key": "photo:x:A", "label": "图文 x · A 版 · 首图贴纸"}
        outcome = self.store.note_used([(self.first, source), (self.second, source), ("missing", source)])
        self.assertEqual((outcome["added"], outcome["kept"], outcome["skipped"]), ([self.first], [self.second], ["missing"]))
        self.store.note_used([(self.first, source), (self.first, {"key": "photo:x:B", "label": "B"})])
        picks = self.picks()
        self.assertEqual([u["key"] for u in picks[self.first]["used"]], ["photo:x:A", "photo:x:B"])  # same place never twice
        self.assertEqual((picks[self.second]["note"], picks[self.second]["added"]), ("开头可用", before))
        # editing the note by hand keeps where it was used
        self.store.set_pick(self.first, True, "首图")
        self.assertEqual(len(self.picks()[self.first]["used"]), 2)

    def test_finalized_versions_hand_their_stickers_over_once(self):
        self.assertEqual(self.sets.collect_used_comments(), 2)
        picks = self.picks()
        self.assertEqual(set(picks), {self.first, self.second})  # B is not finalized yet
        used = picks[self.first]["used"][0]
        self.assertEqual((used["photo_set"], used["version"], used["case_id"], used["slot"], used["export"]),
                         (SET, "A", "PVR-0123456789ab", "首图贴纸", "PVR-20261118-A一套配齐"))
        self.assertEqual(picks[self.second]["used"][0]["slot"], "第 2 张评论页")
        self.assertEqual(self.sets.collect_used_comments(), 0)
        # taken out by hand: exporting the same version again does not bring it back
        self.store.set_pick(self.first, False)
        data = self.sets._read(SET)
        self.assertEqual(self.sets._comments_to_basket(data, data["works"][0]), {"added": 0, "kept": 0})
        self.assertNotIn(self.first, self.picks())
        # finalizing B hands over its own sticker; the comment already in the basket gains a second place
        data["works"][1]["status"] = "done"
        self.sets._save(data)
        self.assertEqual(self.sets.collect_used_comments(), 0)
        self.assertEqual([u["version"] for u in self.picks()[self.second]["used"]], ["A", "B"])

    def test_basket_shows_where_a_comment_was_used(self):
        self.sets.collect_used_comments()
        from workbench_creation import CreationWorkbenchMixin
        review = next(r for r in self.store.picks() if r["id"] == self.first)
        material = CreationWorkbenchMixin._review_material(review)
        self.assertIn("A 版 一套配齐 · 首图贴纸", material["used"][0]["label"])

    def test_a_place_never_brings_back_a_comment_taken_out_by_hand(self):
        source = {"key": "video:PRD-1:uid-1", "label": "视频 x · 开头评论贴纸"}
        self.assertEqual(self.store.note_used([(self.third, source)])["added"], [self.third])
        self.store.set_pick(self.third, False)
        self.assertEqual(self.store.note_used([(self.third, source)])["handed"], [self.third])
        self.assertNotIn(self.third, self.picks())
        # used somewhere new: it comes back
        self.assertEqual(self.store.note_used([(self.third, {"key": "video:PRD-2:uid-9"})])["added"], [self.third])

    def test_video_stickers_on_timelines_go_in_too(self):
        import comment_sticker
        jobs = [{"id": "PRD-1", "product": "202H", "replica": {"case_id": "PVR-1"}, "timelines": [
                    {"unique_id": "uid-1", "name": "Lab-1", "comment_sticker": {"review_id": self.first, "at": "t"}},
                    {"unique_id": "uid-2", "name": "Lab-2", "comment_sticker": {"review_id": self.first, "at": "t"}},
                    {"unique_id": "uid-3", "name": "Lab-3", "comment_sticker": {"review_id": None, "username": "@typed"}},
                    {"unique_id": "uid-4", "name": "Lab-4"}]},
                {"id": "PRD-2", "timelines": [{"unique_id": "u", "name": "Zack-1", "comment_sticker": {"review_id": self.third}}]}]
        self.app.production = type("Production", (), {"read": lambda self: {"jobs": jobs}})()
        self.assertEqual(comment_sticker.collect_video_stickers(self.app), 2)
        picks = self.picks()
        self.assertEqual([u["label"] for u in picks[self.first]["used"]], ["视频 Lab-1 · 开头评论贴纸", "视频 Lab-2 · 开头评论贴纸"])
        self.assertEqual(picks[self.first]["used"][0]["case_id"], "PVR-1")
        self.assertEqual(comment_sticker.collect_video_stickers(self.app), 0)

    def test_no_reviews_no_work(self):
        self.app.reviews = None
        self.assertEqual(self.sets.collect_used_comments(), 0)


if __name__ == "__main__":
    unittest.main()
