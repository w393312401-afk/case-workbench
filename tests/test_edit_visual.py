import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import edit_visual
from elevenlabs_client import VoiceError

JOB, EDIT, TL = "PRD-600adf42f237", "EDT-4c237e2aa54a", "82c1ce41-ae5a-43d1-8092-7b325be45311"

REPLICA_OUTPUT = """成片已铺满并保存：按原片镜头表放了 4 个镜头，没有空段。

全片用迷彩折刀。

**逐镜头对比**

| 镜头 | 原片 | 这次用了 | 差在哪 |
|---|---|---|---|
| 1 | 切纸 | MD109 一刀切到纸分开 | 刀型，白纸 |
| 2–3 | 调角度 | MD032 → MD033 | 对上：两段源帧相连 |
| 4 | 夹爪微距 | MD049 六角扳手拧紧 | 换了动作，低机位侧拍 |

- **已用 3 次的片段：** MD043 已用 3 次。

```json
{"timeline": "t", "shots": 4, "gaps": [], "questions": [], "saved": true, "replica": {"exact": 1, "close": 2, "substitute": 1, "missing": 0}}
```"""


def item(start, end, source, path="D:\\素材\\迷你刀.mov"):
    return {"start": start, "end": end, "name": "迷你刀.mov", "media_path": path, "source_start": source,
            "source_end": source + end - start, "enabled": True}


class EditVisualTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "production" / "edits" / EDIT).mkdir(parents=True)
        self.job = {
            "id": JOB, "lines": [{"number": 1, "zh": "第一句", "spoken": "One"}, {"number": 2, "zh": "第二句", "spoken": "Two"}],
            "timelines": [{"unique_id": TL, "fps": "29.97", "frames": 120, "lead_frames": 0, "take_id": "TAK-1"}],
            "takes": [{"id": "TAK-1", "groups": [{"number": 1, "start": 0, "end": 2}, {"number": 2, "start": 2, "end": 4}]}],
            "replica": {"case_id": "PVR-928cf8d5b47c", "duration": 4.0, "spans": [{"start": 0, "end": 2}, {"start": 2, "end": 4}],
                        "shots": [{"number": 1, "start": 0, "end": 1, "lines": [1]}, {"number": 2, "start": 1, "end": 1.5, "lines": [1]},
                                  {"number": 3, "start": 1.5, "end": 2, "lines": [1]}, {"number": 4, "start": 2, "end": 4, "lines": [2]}]},
            "edits": [{"id": EDIT, "mode": "replica", "timeline_uid": TL, "take_id": "TAK-1", "output": REPLICA_OUTPUT,
                       "report": {"gaps": [], "replica": {"exact": 1, "close": 2, "substitute": 1, "missing": 0}}}]}
        self.app = SimpleNamespace(production=SimpleNamespace(root=self.root / "production", job=self.find), runtime=self.root, store=None)
        self.snapshot([item(0, 30, 6745), item(30, 45, 1855), item(45, 60, 1870), item(60, 120, 3014)])
        self.write("candidates.json", {"candidates": [
            {"key": "MD109-aa", "label": "切纸 MD109", "media_path": "D:\\素材\\迷你刀.mov", "source": [6700, 6800]},
            {"key": "MD032-bb", "label": "调角 MD032", "media_path": "D:\\素材\\迷你刀.mov", "source": [1850, 1870]},
            {"key": "MD033-cc", "label": "调角 MD033", "media_path": "D:\\素材\\迷你刀.mov", "source": [1870, 1900]},
            {"key": "MD049-dd", "label": "拧紧 MD049", "media_path": "D:\\素材\\迷你刀.mov", "source": [3000, 3100]}]})

    def find(self, identifier):
        if identifier != JOB:
            raise VoiceError(404, "这张制作单不存在。")
        return self.job

    def write(self, name, value):
        (self.root / "production" / "edits" / EDIT / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def snapshot(self, items, start_frame=0):
        self.write("delivered-timeline.json", {"timeline": {
            "fps": 29.97, "start_frame": start_frame, "end_frame": start_frame + 120,
            "tracks": {"video": [{"index": 1, "name": "视频 1", "enabled": True, "items": [
                {**entry, "start": entry["start"] + start_frame, "end": entry["end"] + start_frame} for entry in items]}]},
            "markers": [{"frame": 0, "duration": 60, "custom": f"workbench-group:{JOB}:1"},
                        {"frame": 60, "duration": 60, "custom": f"workbench-group:{JOB}:2"}]}})

    def test_replica_table_lands_on_original_shot_ranges_with_status(self):
        data = edit_visual.visual(self.app, JOB, EDIT)
        self.assertTrue(data["available"])
        self.assertTrue(data["replica"])
        self.assertEqual([row["label"] for row in data["rows"]], ["1", "2–3", "4"])
        self.assertEqual([(row["start"], row["end"]) for row in data["rows"]], [(0, 30), (30, 60), (60, 120)])
        self.assertEqual([row["status"] for row in data["rows"]], ["close", "exact", "substitute"])
        self.assertEqual(data["rows"][1]["shots"], [2, 3])
        self.assertEqual([cut["code"] for cut in data["cut"]], ["MD109", "MD032", "MD033", "MD049"])
        self.assertEqual([line["zh"] for line in data["lines"]], ["第一句", "第二句"])
        # The table and its heading move into the strip; the rest of the write-up stays readable.
        self.assertNotIn("| 镜头 |", data["text"])
        self.assertNotIn("逐镜头对比", data["text"])
        self.assertIn("已用 3 次的片段", data["text"])
        self.assertNotIn("```json", data["text"])

    def test_structured_segments_win_over_the_table(self):
        self.job["edits"][0]["report"]["segments"] = [
            {"start": 0, "end": 45, "shots": [1, 2], "status": "substitute", "used": "MD109", "note": "换了动作", "original": "切纸"},
            {"start": 45, "end": 120, "shots": [3, 4], "status": "bogus", "used": "MD049", "note": ""}]
        rows = edit_visual.visual(self.app, JOB, EDIT)["rows"]
        self.assertEqual([(row["start"], row["end"], row["status"]) for row in rows], [(0, 45, "substitute"), (45, 120, None)])
        self.assertEqual(rows[0]["label"], "1–2")

    def test_explain_table_uses_frame_column_and_timeline_offset(self):
        self.snapshot([item(0, 60, 6745), item(60, 120, 3014)], start_frame=108000)
        self.job["edits"][0].update(mode="explain", output="剪完了。\n\n| 意群 | 目标帧 | 画面 | 素材片段 |\n|---|---|---|---|\n"
                                    "| 1 | 0–60 | 切纸 | MD109 |\n| 2 | 60–120 | 拧紧 | MD049 |\n")
        data = edit_visual.visual(self.app, JOB, EDIT)
        self.assertFalse(data["replica"])
        self.assertIsNone(data["original"])
        self.assertEqual([(row["start"], row["end"], row["status"]) for row in data["rows"]], [(0, 60, None), (60, 120, None)])
        self.assertEqual(data["rows"][0]["used"], "MD109")
        self.assertEqual(data["rows"][0]["picture"], "切纸")
        self.assertEqual(data["cut"][0]["start"], 0)

    def test_material_codes_place_rows_without_frames(self):
        self.job["edits"][0].update(mode="explain", output="| # | 画面 | 用的片段 |\n|---|---|---|\n| A | 切纸 | MD109 |\n| B | 调角 | MD032→MD033 |\n| C | 拧紧 | MD049 |\n")
        rows = edit_visual.visual(self.app, JOB, EDIT)["rows"]
        self.assertEqual([(row["start"], row["end"]) for row in rows], [(0, 30), (30, 60), (60, 120)])

    def test_bullet_write_up_is_placed_and_text_kept(self):
        self.job["edits"][0].update(mode="explain", output="**镜头安排**\n- **意群 1** [0, 60)：切纸\n- **意群 2** [60, 120)：拧紧\n")
        data = edit_visual.visual(self.app, JOB, EDIT)
        self.assertEqual([(row["label"], row["start"], row["end"]) for row in data["rows"]], [("意群 1", 0, 60), ("意群 2", 60, 120)])
        self.assertIn("意群 1", data["text"])

    def test_rhythm_report_counts_shots_and_reads_the_takes_word_timing(self):
        data = edit_visual.visual(self.app, JOB, EDIT)
        # MD032 and MD033 run on from each other in the source, so the four pieces are three shots.
        self.assertEqual(data["rhythm"]["shots"], 3)
        self.assertIsNone(data["rhythm"]["cuts"])
        self.assertIn("3 个镜头", data["rhythm"]["headline"])
        # "One" starts at frame 0 and "Two" at about frame 60: the cut at 30 lands in the pause, the one at 60 on a word.
        timing = [("O", 0.0, 0.1), ("n", 0.1, 0.2), ("e", 0.2, 0.3), ("T", 2.0, 2.1), ("w", 2.1, 2.2), ("o", 2.2, 2.3)]
        self.app.take_triples = lambda job_id, take: timing if (job_id, take["id"]) == (JOB, "TAK-1") else []
        cuts = edit_visual.visual(self.app, JOB, EDIT)["rhythm"]["cuts"]
        self.assertEqual((cuts["total"], cuts["on"], cuts["pause"], cuts["mid"]), (2, 1, 1, 0))

    def test_without_snapshot_only_the_text_is_offered(self):
        (self.root / "production" / "edits" / EDIT / "delivered-timeline.json").unlink()
        data = edit_visual.visual(self.app, JOB, EDIT)
        self.assertFalse(data["available"])
        self.assertIn("| 镜头 |", data["text"])

    def test_unplaceable_table_stays_in_the_text(self):
        self.job["edits"][0].update(mode="explain", output="| 项 | 说明 | 备注 |\n|---|---|---|\n| 甲 | 乙 | 丙 |\n")
        data = edit_visual.visual(self.app, JOB, EDIT)
        self.assertEqual(data["rows"], [])
        self.assertIn("| 甲 |", data["text"])

    def test_ids_and_unknown_edits_are_refused(self):
        with self.assertRaises(VoiceError):
            edit_visual.visual(self.app, "../x", EDIT)
        with self.assertRaises(VoiceError) as raised:
            edit_visual.visual(self.app, JOB, "EDT-000000000000")
        self.assertEqual(raised.exception.status, 404)

    def test_cut_frame_seeks_source_middle_by_media_rate_and_caches(self):
        video = self.root / "clip.mov"
        video.write_bytes(b"x")
        self.snapshot([item(0, 30, 300, str(video))])
        calls = []

        def grab(path, seconds, target):
            calls.append((path, round(seconds, 3)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\xff\xd8\xff")
            return target

        with mock.patch.object(edit_visual, "_media_rate", return_value=30.0), mock.patch.object(edit_visual, "_grab", side_effect=grab):
            first = edit_visual.frame(self.app, JOB, EDIT, "cut", 0)
            second = edit_visual.frame(self.app, JOB, EDIT, "cut", 0)
        self.assertEqual(first, second)
        self.assertEqual(calls, [(video, 10.5)])
        self.assertEqual(first, self.root / "thumbnail-cache" / "edits" / EDIT / "cut-0.jpg")
        with self.assertRaises(VoiceError):
            edit_visual.frame(self.app, JOB, EDIT, "cut", 5)
        with self.assertRaises(VoiceError):
            edit_visual.frame(self.app, JOB, EDIT, "other", 0)

    def test_missing_source_file_is_a_404(self):
        self.snapshot([item(0, 30, 300, str(self.root / "gone.mov"))])
        with self.assertRaises(VoiceError) as raised:
            edit_visual.frame(self.app, JOB, EDIT, "cut", 0)
        self.assertEqual(raised.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
