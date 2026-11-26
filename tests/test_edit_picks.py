"""The editor's shot list is checked against least-used-first before any footage is placed."""
import json
from pathlib import Path
import tempfile
import unittest

from edit_picks import LIMIT, PickCheck, check, kind, waived_question

STAGE = {"name": "80", "index": 1, "count": 9}


def clip(code, label, count=0, frames=60, stage=None):
    return {"key": f"{code}-{code.lower()}0000", "label": f"{label} {code}", "use_count": count, "frames": frames, "stage": stage,
            "used_by": [f"成片{number}" for number in range(count)]}


CLIPS = [clip("SOG022", "研磨 80目阶段推定", 0, 73, STAGE), clip("SOG024", "研磨 80目阶段推定", 1, 55, STAGE),
         clip("SOG025", "研磨 80目阶段推定", 0, 54, STAGE), clip("SOG029", "研磨 80目阶段推定", 0, 30, STAGE),
         clip("SOG051", "研磨 80目阶段推定", 0, 90, {"name": "325", "index": 2, "count": 9}),
         clip("SOG074", "展示棕色磨板", 4, 40), clip("SOG340", "CTA手势 指向下方", 1, 35), clip("SOG341", "打开工具箱", 0, 50)]


class PickTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.write_candidates()

    def write_candidates(self, counted=True):
        (self.folder / "candidates.json").write_text(json.dumps({"usage_counted": counted, "candidates": CLIPS}, ensure_ascii=False), encoding="utf-8")

    def picks(self, *rows):
        (self.folder / "picks.json").write_text(json.dumps(list(rows), ensure_ascii=False), encoding="utf-8")
        return check(self.folder)

    def test_kind_is_the_label_without_the_clip_code(self):
        self.assertEqual(kind(CLIPS[0]), kind(CLIPS[1]))
        self.assertEqual(kind(CLIPS[0]), "研磨 80目阶段推定")
        self.assertNotEqual(kind(CLIPS[5]), kind(CLIPS[6]))

    def test_a_used_clip_is_refused_while_a_same_kind_clip_used_less_is_long_enough(self):
        result = self.picks({"group": 4, "key": "SOG024-sog0240000", "frames": 50})
        self.assertFalse(result["ok"])
        issue = result["issues"][0]
        # SOG029 is too short for 50 frames, SOG051 is another grinding stage.
        self.assertEqual((issue["problem"], issue["alternatives"]), ("less_used", ["SOG022-sog0220000", "SOG025-sog0250000"]))
        self.assertIn("SOG022（未用过，73 帧）", result["message"])
        self.assertIn("本次调用没有执行", result["message"])

    def test_alternatives_already_picked_or_a_reason_let_the_used_clip_through(self):
        every = [{"key": code, "frames": 30} for code in ("SOG022", "SOG025", "SOG029", "SOG024")]
        result = self.picks(*every)
        self.assertTrue(result["ok"])  # short codes are accepted; nothing less used is left over
        self.assertEqual((result["picks"], result["used"]), (4, 1))
        result = self.picks({"key": "SOG024", "frames": 50, "reason": "接 SOG023 的延续画面"})
        self.assertTrue(result["ok"])
        self.assertEqual(result["reasons"], [{"key": "SOG024-sog0240000", "use_count": 1, "reason": "接 SOG023 的延续画面"}])

    def test_used_once_without_an_alternative_passes_but_three_times_needs_a_reason(self):
        self.assertTrue(self.picks({"key": "SOG340", "frames": 35}, {"key": "SOG341", "frames": 40})["ok"])
        result = self.picks({"key": "SOG074", "frames": 21})
        self.assertEqual(result["issues"][0]["problem"], "often_used")
        self.assertTrue(self.picks({"key": "SOG074", "frames": 21, "reason": "唯一的皮革板展示"})["ok"])

    def test_missing_unreadable_and_unknown_lists_are_refused_and_an_empty_one_passes(self):
        self.assertIn("还没有可读的 picks.json", check(self.folder)["message"])
        (self.folder / "picks.json").write_text("not json", encoding="utf-8")
        self.assertFalse(check(self.folder)["ok"])
        self.assertEqual(self.picks({"key": "SOG999"})["issues"][0]["problem"], "unknown")
        self.assertTrue(self.picks()["ok"])
        (self.folder / "picks.json").write_text(json.dumps({"picks": [{"key": "SOG341"}]}), encoding="utf-8")
        self.assertTrue(check(self.folder)["ok"])

    def test_nothing_is_checked_when_use_counts_were_not_listed(self):
        self.write_candidates(counted=False)
        self.assertTrue(check(self.folder)["ok"])

    def test_the_edit_is_let_through_after_the_limit_and_the_open_points_are_kept(self):
        self.picks({"key": "SOG024", "frames": 50})
        asked = PickCheck(self.folder)
        self.assertTrue(all(asked() for _ in range(LIMIT)))
        self.assertEqual(asked(), "")
        summary = asked.summary()
        self.assertEqual((summary["refused"], summary["waived"], summary["open"][0]["key"]), (LIMIT, True, "SOG024-sog0240000"))
        self.assertIn("SOG024", waived_question(summary))
        self.picks({"key": "SOG022", "frames": 50})
        self.assertEqual(asked(), "")
        self.assertFalse(asked.summary()["waived"])
        self.assertEqual(waived_question(asked.summary()), "")

    def test_a_check_that_cannot_run_never_blocks(self):
        asked = PickCheck(self.folder / "missing")
        self.assertEqual(asked(), "")
        self.assertTrue(asked.summary()["error"])

    def test_rhythm_precheck_catches_mid_cut_late_cut_repeat_shot_size_and_slow_hook(self):
        from edit_rhythm import RHYTHM_CONTEXT_FILE, word_spans
        from tests.test_edit_rhythm import triples
        lines = [{"number": 1, "start": 0, "end": 120}, {"number": 2, "start": 120, "end": 210}]
        spans = word_spans(
            ["One fine stone slowly", "Grind it now"],
            triples([("One", 0.0, 0.4), ("fine", 0.5, 0.9), ("stone", 1.0, 1.5), ("slowly", 1.6, 3.8),
                     ("Grind", 4.0, 4.5), ("it", 4.6, 4.9), ("now", 5.0, 6.8)]),
        )
        (self.folder / RHYTHM_CONTEXT_FILE).write_text(
            json.dumps({"fps": 30.0, "lead": 0, "lines": lines, "spans": spans}, ensure_ascii=False),
            encoding="utf-8",
        )
        # 1) Single 126-frame (4.2s) hook + late cut on "Grind" (frame 126 vs 120) + mid cut inside "now" (frame 165)
        result = self.picks(
            {"group": 1, "key": "SOG341", "frames": 126, "shot_size": "中景"},
            {"group": 2, "key": "SOG022", "frames": 39, "shot_size": "中景"},
            {"group": 2, "key": "SOG025", "frames": 45, "shot_size": "中景"},
        )
        self.assertFalse(result["ok"])
        problems = {issue["problem"] for issue in result["issues"]}
        self.assertTrue({"slow_hook", "late_cut", "mid_cut", "repeat_kind", "same_shot_size"}.issubset(problems))
        # 2) Clean plan: 2 hook shots, cuts on word starts (frame 45 -> "stone" at 30? no, frame 30; frame 118 -> 2f before "Grind" at 120; frame 148 -> 2f before "now" at 150), alternating shot_size & kind.
        clean = self.picks(
            {"group": 1, "key": "SOG341", "frames": 48, "shot_size": "中景"},
            {"group": 1, "key": "SOG022", "frames": 70, "shot_size": "特写"},
            {"group": 2, "key": "SOG051", "frames": 30, "shot_size": "近景"},
            {"group": 2, "key": "SOG340", "frames": 62, "shot_size": "中景"},
        )
        self.assertTrue(clean["ok"], clean["message"])


if __name__ == "__main__":
    unittest.main()
