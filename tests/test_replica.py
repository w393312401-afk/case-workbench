"""照原片做视频：解析原稿、切镜头、时间换算，以及从案例到制作单、到剪辑交接的数据流。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import case_replica
from case_replica import estimated_spans, handoff_section, line_records, parse_transcript, shot_targets
from edit_planning import mode_selection_rules, prepare_workers
from creation_store import CreationStore
from elevenlabs_client import VoiceError
from retro_draft import PrepareError
from workbench_editing import MODES, handoff_markdown
from workbench_replica import ReplicaWorkbenchMixin


def reference(shots, spans, duration):
    return {"case_id": "PVR-000000000001", "case_title": "原片", "duration": duration, "width": 720, "height": 1280,
            "timed": True, "spans": spans, "notes": [],
            "shots": [{"number": n, "start": a, "end": b, "lines": lines} for n, (a, b, lines) in enumerate(shots, 1)]}


class TranscriptTest(unittest.TestCase):
    def test_both_stamp_styles_are_read(self):
        lines, timed = parse_transcript("[0.18s - 4.34s] One.\n\n[4.80–6.92s] Two.\n")
        self.assertTrue(timed)
        self.assertEqual(lines, [{"spoken": "One.", "start": .18, "end": 4.34}, {"spoken": "Two.", "start": 4.8, "end": 6.92}])

    def test_any_unstamped_line_makes_the_whole_transcript_untimed(self):
        lines, timed = parse_transcript("[0.18s - 4.34s] One.\nTwo.")
        self.assertFalse(timed)
        self.assertEqual([line["spoken"] for line in lines], ["One.", "Two."])
        self.assertIsNone(lines[0]["start"])

    def test_a_note_about_silence_is_not_a_script(self):
        self.assertEqual(parse_transcript("无口播")[0], [])
        self.assertEqual(parse_transcript("ASR未检出可辨口播。屏显原文：Before you replace")[0], [])

    def test_estimated_spans_cover_the_video_by_share_of_words(self):
        self.assertEqual(estimated_spans(["aaa", "a"], 8), [{"start": 0.0, "end": 6.0}, {"start": 6.0, "end": 8.0}])


class ShotTargetTest(unittest.TestCase):
    def test_a_cut_keeps_its_place_inside_the_line_when_the_voice_is_slower(self):
        replica = reference([(0, 2, [1]), (2, 4, [1]), (4, 6, [2])], [{"start": 0, "end": 4}, {"start": 4, "end": 6}], 6)
        groups = [{"start": 0, "end": 8}, {"start": 8, "end": 10}]
        rows = shot_targets(replica, groups, "30", 0, 300)
        self.assertEqual([(row["target_start"], row["target_end"]) for row in rows], [(0, 120), (120, 240), (240, 300)])

    def test_lead_and_pauses_are_carried_and_the_ranges_tile_the_timeline(self):
        replica = reference([(0, 1, []), (1, 3, [1]), (3, 5, [1, 2]), (5, 6.5, [2])],
                            [{"start": 1, "end": 3.5}, {"start": 4, "end": 6}], 6.5)
        groups = [{"start": 0, "end": 2}, {"start": 2.5, "end": 5}]
        rows = shot_targets(replica, groups, "30", 30, 180)
        self.assertEqual(rows[0]["target_start"], 0)
        self.assertEqual(rows[0]["target_end"], 30)  # The silence before the first line becomes the lead.
        self.assertEqual(rows[-1]["target_end"], 180)
        self.assertTrue(all(left["target_end"] == right["target_start"] for left, right in zip(rows, rows[1:])))
        self.assertTrue(all(row["target_end"] > row["target_start"] for row in rows))

    def test_every_shot_keeps_a_frame_when_the_voice_is_far_shorter(self):
        replica = reference([(index, index + 1, [1]) for index in range(10)], [{"start": 0, "end": 10}], 10)
        rows = shot_targets(replica, [{"start": 0, "end": .3}], "30", 0, 12)
        self.assertEqual(rows[-1]["target_end"], 12)
        self.assertTrue(all(row["target_end"] - row["target_start"] >= 1 for row in rows))

    def test_a_voice_group_list_shorter_than_the_lines_still_tiles(self):
        replica = reference([(0, 2, [1]), (2, 4, [2])], [{"start": 0, "end": 2}, {"start": 2, "end": 4}], 4)
        rows = shot_targets(replica, [{"start": 0, "end": 1}], "24", 0, 96)
        self.assertEqual((rows[0]["target_start"], rows[-1]["target_end"]), (0, 96))


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.replica = reference([(0, 2, [1]), (2, 4, [2])], [{"start": 0, "end": 2}, {"start": 2, "end": 4}], 4)
        self.job = {"id": "PRD-000000000001", "title": "t", "script_id": "SCR-000000000001", "script_version": 1, "replica": self.replica,
                    "lines": [{"number": 1, "spoken": "One.", "zh": "一"}, {"number": 2, "spoken": "Two.", "zh": "二"}]}
        self.take = {"model_id": "m", "voice_id": "v",
                     "groups": [{"number": 1, "start": 0, "end": 3, "coverage": 1}, {"number": 2, "start": 3, "end": 6, "coverage": 1}]}
        self.timeline = {"name": "Lab-1", "unique_id": "u", "frames": 180, "fps": "30", "lead_frames": 0, "width": 720, "height": 1280}
        self.config = {"project": "202H", "fps": "30", "width": 720, "height": 1280}

    def handoff(self, mode):
        return handoff_markdown(self.job, self.take, self.timeline, self.config, mode=mode, instruction="", materials=[],
                                notes={"opening": "", "onscreen": "", "pickups": []}, voice_label="v")

    def test_replica_mode_carries_the_shot_table_and_its_precedence(self):
        text = self.handoff("replica")
        self.assertIn("原片镜头表", text)
        self.assertIn("[0, 90)", text)
        self.assertIn("reference/shot-02.jpg", text)
        self.assertIn("优先于技能的分类时长上限", text)
        self.assertLess(text.index("原片镜头表"), text.index("## 交付"))

    def test_an_optimized_script_is_named_as_such_to_the_editor(self):
        self.assertIn("口播已和原片逐句相同", self.handoff("replica"))
        self.replica["wording"] = "aligned"
        text = self.handoff("replica")
        self.assertIn("逐句对应原片的优化稿", text)
        self.assertNotIn("口播已和原片逐句相同", text)

    def test_other_modes_of_the_same_job_stay_as_they_were(self):
        self.assertNotIn("原片镜头表", self.handoff("explain"))

    def test_other_modes_do_not_inherit_the_lines_copy_the_original_hint(self):
        self.job["lines"][0]["proof"] = "照原片 镜头 1（0.00–2.00 秒）"
        self.job["lines"][1]["proof"] = "Show the edge"
        for mode in ("explain", "sync", "montage"):
            text = self.handoff(mode)
            self.assertNotIn("照原片 镜头", text)
            self.assertIn("Show the edge", text)
        self.assertIn("照原片 镜头 1", self.handoff("replica"))

    def test_the_mode_has_selection_rules(self):
        self.assertIn("replica", MODES)
        self.assertIn("1:1 复刻原片", mode_selection_rules("replica"))

    def test_copied_instructions_can_point_at_the_real_folder(self):
        text = handoff_section(self.job, self.take, self.timeline, "30", 0, "D:/refs")
        self.assertIn("D:/refs/shot-01.jpg", text)

    def test_line_records_name_the_shots_so_no_line_looks_unfilled(self):
        rows = line_records(["One.", "Two."], ["一", "二"], self.replica)
        self.assertEqual(rows[0]["zh"], "一")
        self.assertIn("镜头 1", rows[0]["proof"])
        self.assertTrue(all(row["proof"] for row in rows))

    def test_planners_get_the_reference_images_too(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "reference").mkdir()
            (root / "reference" / "shot-01.jpg").write_bytes(b"x")
            (root / "candidates.json").write_text('{"candidates": [{"key": "a", "timeline": "t", "timeline_uid": "u"}]}', encoding="utf-8")
            workers = prepare_workers(root, "handoff")
            self.assertTrue((workers[0] / "reference" / "shot-01.jpg").is_file())


class StoreSource:
    def __init__(self, case):
        self.case = case

    def read_case(self, identifier):
        if not self.case:
            raise KeyError(identifier)
        return self.case


class Production:
    def __init__(self, root):
        self.root = Path(root)
        self.jobs = []

    def records(self):
        return {"jobs": self.jobs}


class Bench(ReplicaWorkbenchMixin):
    def __init__(self, root, case):
        import threading
        self.store, self.production, self.lock = StoreSource(case), Production(root), threading.RLock()


CASE = {"case_id": "PVR-000000000001", "product": "SHARPAL 202H Kit", "current": {
    "title": "Good Knives", "content_origin": "self_produced", "assets": {"video": ""},
    "transcript": {"text": "[0.00s - 1.00s] One.\n[1.00s - 2.00s] Two.", "status": "机器转写，未经人工逐字听校"},
    "translation_zh": {"text": "一\n二"}, "metrics": {"published_title": "🔴Title #tag"}}}


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class MediaTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.video = self.root / "original.mp4"
        # Two seconds of red, then two of blue: one hard cut at 2 s.
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=180x320:d=2:r=25", "-f", "lavfi",
                        "-i", "color=blue:s=180x320:d=2:r=25", "-filter_complex", "[0][1]concat=n=2:v=1[v]", "-map", "[v]",
                        "-pix_fmt", "yuv420p", str(self.video)], check=True)
        self.case = {**CASE, "current": {**CASE["current"], "assets": {"video": str(self.video)}}}

    def test_cut_is_found_and_frames_are_written_per_shot(self):
        bench = Bench(self.root / "production", self.case)
        source = bench._replica_case("PVR-000000000001")[2]
        job = {"id": "PRD-000000000001", "replica": case_replica.snapshot("PVR-000000000001", source, ["One.", "Two."])}
        shots = job["replica"]["shots"]
        self.assertEqual(len(shots), 2)
        self.assertAlmostEqual(shots[0]["end"], 2, delta=.1)
        self.assertTrue(job["replica"]["timed"])
        folder = bench.replica_frames(job)
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["shot-01.jpg", "shot-02.jpg"])

    def test_an_edited_script_falls_back_to_estimated_times_and_says_so(self):
        source = Bench(self.root, self.case)._replica_case("PVR-000000000001")[2]
        result = case_replica.snapshot("PVR-000000000001", source, ["Changed words."])
        self.assertFalse(result["timed"])
        self.assertIn("按字数估算", result["notes"][0])

    def test_an_optimized_script_line_for_line_keeps_the_original_times(self):
        source = Bench(self.root, self.case)._replica_case("PVR-000000000001")[2]
        result = case_replica.snapshot("PVR-000000000001", source, ["First, better.", "Second, better."], aligned=True)
        self.assertTrue(result["timed"])
        self.assertEqual("aligned", result["wording"])
        self.assertEqual([{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}], result["spans"])
        self.assertIn("逐句对应原稿的优化稿", result["notes"][0])
        # An optimized script that lost or gained a line can no longer sit on the original's lines.
        drifted = case_replica.snapshot("PVR-000000000001", source, ["All in one."], aligned=True)
        self.assertEqual(("changed", False), (drifted["wording"], drifted["timed"]))

    def test_the_machine_transcript_is_flagged(self):
        source = Bench(self.root, self.case)._replica_case("PVR-000000000001")[2]
        self.assertTrue(any("机器转写" in note for note in case_replica.snapshot("PVR-000000000001", source, ["One.", "Two."])["notes"]))

    def test_a_job_can_no_longer_replicate_after_its_case_is_gone(self):
        bench = Bench(self.root / "production", None)
        job = {"id": "PRD-000000000001", "replica": reference([(0, 1, [1])], [{"start": 0, "end": 1}], 1)}
        with self.assertRaises(VoiceError) as caught:
            bench.replica_frames(job)
        self.assertEqual(caught.exception.status, 409)


class UntilFilledTest(unittest.TestCase):
    def run_with(self, first, replies):
        prompts, queue = [], list(replies)

        def rerun(prompt):
            prompts.append(prompt)
            return queue.pop(0)
        return case_replica.until_filled(first, rerun, lambda output: output), prompts

    def done(self, *statuses):
        return ("completed", {"gaps": [{"start": 1, "end": 9, "status": status} for status in statuses]}, "", {})

    def test_unverified_gaps_send_it_back_until_filled(self):
        result, prompts = self.run_with(self.done("unverified"), [self.done()])
        self.assertEqual(result, self.done())
        self.assertEqual(len(prompts), 1)
        self.assertIn("[1, 9)", prompts[0])

    def test_a_real_missing_gap_is_accepted(self):
        self.assertEqual(self.run_with(self.done("missing"), [])[1], [])

    def test_it_stops_at_the_limit_and_when_cancelled(self):
        stuck = self.done("unverified")
        self.assertEqual(len(self.run_with(stuck, [stuck, stuck])[1]), case_replica.MAX_CONTINUATIONS)
        stopped = ("cancelled", stuck[1], "", {})
        self.assertEqual(self.run_with(stopped, [])[1], [])


class EditReferenceTest(unittest.TestCase):
    def bench(self, job):
        from workbench_editing import EditingWorkbenchMixin

        class Editing(EditingWorkbenchMixin):
            production = type("Store", (), {"job": staticmethod(lambda identifier: job)})

            def replica_frames(self, record):
                return "frames-of-" + record["id"]
        return Editing()

    def test_other_modes_need_nothing_prepared(self):
        self.assertIsNone(self.bench({"id": "PRD-1"})._edit_reference("PRD-1", "explain"))

    def test_an_ordinary_job_cannot_pick_the_replica_mode(self):
        with self.assertRaises(VoiceError) as caught:
            self.bench({"id": "PRD-1"})._edit_reference("PRD-1", "replica")
        self.assertEqual(caught.exception.status, 400)

    def test_a_replica_job_gets_its_frames(self):
        self.assertEqual(self.bench({"id": "PRD-1", "replica": {"shots": []}})._edit_reference("PRD-1", "replica"), "frames-of-PRD-1")


class CaseRequirementsTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.video = Path(self.folder.name) / "v.mp4"
        self.video.write_bytes(b"x")

    def refused(self, case):
        with self.assertRaises(VoiceError) as caught:
            Bench(self.folder.name, case)._replica_case("PVR-000000000001")
        return caught.exception

    def test_missing_video_is_refused(self):
        self.assertIn("原视频", str(self.refused(CASE)))

    def test_no_speech_is_refused(self):
        current = {**CASE["current"], "assets": {"video": str(self.video)}, "transcript": {"text": "无口播"}}
        self.assertIn("口播文案", str(self.refused({**CASE, "current": current})))

    def test_unbound_product_is_refused(self):
        self.assertIn("绑定产品", str(self.refused({**CASE, "product": ""})))

    def test_a_missing_case_is_404(self):
        self.assertEqual(self.refused(None).status, 404)

    def test_a_mismatched_translation_is_dropped_not_misaligned(self):
        current = {**CASE["current"], "assets": {"video": str(self.video)}, "translation_zh": {"text": "只有一行"}}
        self.assertEqual(case_replica.case_source({"current": current})["chinese"], [])


class OptimizedOriginTest(unittest.TestCase):
    """Scripts adopted from a 「照原片优化」 task replicate the task's case, line for line; others do not."""

    def bench(self, tasks):
        class AI:
            def get(self, task_id):
                if task_id not in tasks:
                    raise KeyError(task_id)
                return tasks[task_id]
        bench = Bench(".", CASE)
        bench.ai = AI()
        return bench

    def test_the_task_mode_decides(self):
        bench = self.bench({"AIT-replica": {"case_id": "PVR-000000000001", "context": {"writing_mode": {"mode": "replica", "count": 2}}},
                            "AIT-diverge": {"case_id": "PVR-000000000001", "context": {"writing_mode": {"mode": "diverge", "count": 2}}},
                            "AIT-plain": {"case_id": "PVR-000000000001", "context": {}}})
        self.assertEqual(("PVR-000000000001", True), bench._replica_origin({"source": {"task_id": "AIT-replica", "round_id": "r"}}))
        self.assertEqual(("PVR-000000000002", False), bench._replica_origin({"source": {"kind": "case_replica", "case_id": "PVR-000000000002"}}))
        for task_id in ("AIT-diverge", "AIT-plain", "AIT-gone"):
            self.assertIsNone(bench._replica_origin({"source": {"task_id": task_id}}))
        self.assertIsNone(bench._replica_origin({"source": {}}))

    def test_an_adopted_optimized_script_becomes_an_aligned_replica_job(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        video = Path(folder.name) / "v.mp4"
        video.write_bytes(b"x")
        bench = self.bench({"AIT-replica": {"case_id": "PVR-000000000001", "context": {"writing_mode": {"mode": "replica", "count": 2}}}})
        bench.store = StoreSource({**CASE, "current": {**CASE["current"], "assets": {"video": str(video)}}})

        class Creation:
            def get_script(self, identifier):
                return {"id": identifier, "text": "Better one.\nBetter two.", "source": {"task_id": "AIT-replica", "round_id": "r"}}
        bench.creation = Creation()
        seen = []
        with patch("workbench_replica.snapshot", lambda case_id, source, spoken, aligned=False: seen.append((case_id, spoken, aligned)) or
                   {"notes": [], "wording": "aligned", "shots": [], "spans": []}):
            reference, _ = bench._replica_snapshot("SCR-000000000001")
        self.assertEqual([("PVR-000000000001", ["Better one.", "Better two."], True)], seen)
        resolved = bench.apply_replica({}, ["Better one.", "Better two."], [], (
            {"notes": [], "wording": "aligned", "shots": [], "spans": [{"start": 0, "end": 1}, {"start": 1, "end": 2}]}, {"caption": ""}))
        self.assertEqual("1:1 复刻·优化稿", resolved["variant_label"])

    def test_the_optimize_count_is_checked_before_anything_is_made(self):
        bench = Bench(".", CASE)
        for count in (-1, 6, "2", 1.5):
            with self.assertRaises(VoiceError):
                bench.start_case_replica("PVR-000000000001", {"new": True, "optimize": count})


class ScriptSourceTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = CreationStore(Path(self.folder.name))
        self.source = {"kind": "case_replica", "case_id": "PVR-000000000001", "case_title": "t"}

    def save(self, source, request="replica-request-1", text="One.\nTwo."):
        return self.store.mutate("script", {"request_id": request, "revision": self.store.revision(self.store.read()),
                                            "script": {"title": "t", "text": text, "product": "202H", "confirmed": True}}, source)

    def test_replica_source_is_kept_on_the_script(self):
        self.assertEqual(self.save(self.source)["script"]["source"], self.source)

    def test_same_words_from_the_same_case_stay_one_script(self):
        first = self.save(self.source)["script"]["id"]
        second = self.save(self.source, "replica-request-2")
        self.assertEqual(second["script"]["id"], first)
        self.assertTrue(second["reused"])

    def test_changed_words_become_a_new_script(self):
        first = self.save(self.source)["script"]["id"]
        self.assertNotEqual(self.save(self.source, "replica-request-2", "Other.")["script"]["id"], first)

    def test_an_unverified_ai_source_is_still_refused(self):
        with self.assertRaises(Exception):
            self.save({"kind": "other", "case_id": "PVR-000000000001"})
        with self.assertRaises(Exception):
            self.save({"kind": "case_replica"})

    def test_deleting_the_case_scrubs_the_script_source(self):
        self.save(self.source)
        self.store.forget_case("PVR-000000000001")
        source = self.store.public()["scripts"][0]["source"]
        self.assertNotIn("case_id", source)
        self.assertTrue(source["source_deleted"])


if __name__ == "__main__":
    unittest.main()
