"""Engineering status checks using snapshots; never opens or edits Resolve."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from production_validation import delivery_gap_status, review_current_coverage, validate_edit

JOB = {"id": "PRD-validation", "lines": [{"number": n} for n in (1, 2, 3)]}
TIMELINE = {"frames": 90}


def inspection(*, edited=True):
    def track(index, name, items):
        return {"index": index, "name": name, "locked": False, "enabled": True, "items": items}
    def item(uid, start, end, source, linked):
        return {"uid": uid, "name": uid, "media_id": source, "media_path": f"C:/media/{source}.mov", "start": start, "end": end,
                "source_start": start, "source_end": end, "properties": {"AudioVolume": 0}, "enabled": True,
                "linked_ids": linked, "has_audio": True}
    videos = [item(f"v{n}", n * 30, (n + 1) * 30, "source", [f"a{n}"]) for n in range(3)] if edited else []
    audio = [item(f"a{n}", n * 30, (n + 1) * 30, "source", [f"v{n}"]) for n in range(3)] if edited else []
    return {"name": "编辑时间线", "unique_id": "timeline-1", "start_frame": 1000, "end_frame": 1090, "fps": "30",
            "width": 1080, "height": 1920, "saved": True, "dedup_complete": True, "other_shots": [],
            "tracks": {"video": [track(1, "画面", [{**i, "start": i["start"] + 1000, "end": i["end"] + 1000} for i in videos])],
                       "audio": [track(1, "原声", [{**i, "start": i["start"] + 1000, "end": i["end"] + 1000} for i in audio]),
                                 track(2, "配音", [{**item("voice", 0, 90, "voice", []), "start": 1000, "end": 1090}])], "subtitle": []},
            "markers": [{"frame": n * 30, "duration": 30, "color": "Blue", "custom": f"workbench-group:{JOB['id']}:{n + 1}",
                         "name": f"#{n + 1}", "note": "意群资料"} for n in range(3)]}


class ValidationTests(unittest.TestCase):
    def check(self, post=None, base=None, **kwargs):
        return validate_edit(post or inspection(), JOB, TIMELINE, base or inspection(edited=False), **kwargs)[0]

    def codes(self, result):
        return {item["code"] for item in result["issues"]}

    def test_success_is_waiting_for_human_not_final_completion(self):
        result = self.check()
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["issues"], [])
        self.assertTrue(all(result["checks"].values()))
        self.assertIn("字幕、声音听审和成片导出", result["manual_scope"])

    def test_only_named_red_range_with_a_note_can_cover_missing_picture(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:2]
        marker = {"frame": 60, "duration": 30, "color": "Red", "name": "缺素材｜切纸结果", "note": "需补切纸结果", "custom": "gap"}
        post["markers"].append(marker)
        self.assertEqual(self.check(post)["status"], "needs_material")
        marker["name"] = "人工提醒"
        result = self.check(post)
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("unmarked_picture_gap", self.codes(result))
        marker["name"], marker["note"] = "缺素材｜切纸结果", ""
        self.assertIn("invalid_missing_marker", self.codes(self.check(post)))

    def test_gaps_reported_in_the_final_json_cover_missing_picture_without_markers(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:2]
        self.assertIn("unmarked_picture_gap", self.codes(self.check(post)))
        # A range a few frames off still declares the empty stretch it overlaps.
        result, summary = validate_edit(post, JOB, TIMELINE, inspection(edited=False),
                                        report={"gaps": [{"start": 62, "end": 88, "need": "切纸结果特写，1 秒"}]})
        self.assertEqual(result["status"], "needs_material")
        self.assertEqual([(gap["start"], gap["end"], gap["name"]) for gap in summary["gaps"]], [(60, 90, "切纸结果特写，1 秒")])
        stale = self.check(post, report={"gaps": [{"start": 62, "end": 88, "need": "切纸"}, {"start": 0, "end": 10, "need": "开头"}]})
        self.assertEqual(stale["status"], "needs_repair")
        self.assertIn("reported_gap_not_gap", self.codes(stale))
        malformed = self.check(post, report={"gaps": [{"start": "60", "end": 90}]})
        self.assertIn("unmarked_picture_gap", self.codes(malformed))

    def test_unverified_empty_range_is_not_confirmed_missing_material(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:2]
        report = {"gaps": [{"start": 60, "end": 90, "status": "unverified", "need": "待查看推磨细节"}]}
        result, summary = validate_edit(post, JOB, TIMELINE, inspection(edited=False), report=report)
        self.assertEqual(result["status"], "needs_verification")
        self.assertEqual(summary["gaps"][0]["status"], "unverified")
        self.assertNotIn("missing_material", self.codes(result))
        self.assertIn("material_unverified", self.codes(result))
        self.assertEqual(delivery_gap_status(report), "needs_verification")
        # Old prose is never used to silently reclassify a declared missing range.
        report["gaps"][0].pop("status")
        self.assertEqual(delivery_gap_status(report), "needs_material")

    def test_adjacent_gaps_keep_missing_and_unverified_states_separate(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:1]
        report = {"gaps": [{"start": 30, "end": 60, "status": "missing", "need": "缺切纸"},
                           {"start": 60, "end": 90, "status": "unverified", "need": "待看锁角"}]}
        result, summary = review_current_coverage(post, JOB, {**TIMELINE, "unique_id": "timeline-1"}, report=report)
        self.assertEqual([(gap["start"], gap["end"], gap["status"]) for gap in summary["gaps"]],
                         [(30, 60, "missing"), (60, 90, "unverified")])
        self.assertEqual(result["status"], "needs_material")
        self.assertIn("material_unverified", self.codes(result))

    def test_current_refresh_never_expands_partial_declaration_into_unreported_empty_picture(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:1]
        for state in ("missing", "unverified"):
            with self.subTest(state=state):
                report = {"gaps": [{"start": 60, "end": 90, "status": state, "need": "尾段画面"}]}
                result, summary = review_current_coverage(post, JOB, {**TIMELINE, "unique_id": "timeline-1"}, report=report)
                self.assertEqual([(gap["start"], gap["end"], gap["status"]) for gap in summary["gaps"]], [(60, 90, state)])
                self.assertEqual([(gap["start"], gap["end"]) for gap in summary["uncovered"]], [(30, 60)])
                self.assertEqual(result["status"], "needs_repair")
                self.assertIn("unmarked_picture_gap", self.codes(result))
                # The historical engineering path keeps its established tolerance.
                _, legacy = validate_edit(post, JOB, TIMELINE, inspection(edited=False), report=report)
                self.assertEqual([(gap["start"], gap["end"]) for gap in legacy["gaps"]], [(30, 90)])

    def test_current_refresh_intersects_overlapping_declarations_with_missing_priority_in_both_orders(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:1]
        pending = {"start": 30, "end": 70, "status": "unverified", "need": "待看"}
        missing = {"start": 50, "end": 90, "status": "missing", "need": "缺画面"}
        for declarations in ([pending, missing], [missing, pending]):
            with self.subTest(order=declarations):
                result, summary = review_current_coverage(post, JOB, {**TIMELINE, "unique_id": "timeline-1"}, report={"gaps": declarations})
                self.assertEqual([(gap["start"], gap["end"], gap["status"]) for gap in summary["gaps"]],
                                 [(30, 50, "unverified"), (50, 70, "missing"), (70, 90, "missing")])
                self.assertEqual(summary["uncovered"], [])
                self.assertEqual(result["status"], "needs_material")

    def test_skill_manual_refresh_resolves_old_ranges_without_claiming_baseline_protection(self):
        post = inspection()
        report = {"gaps": [{"start": 0, "end": 30, "need": "锁角细节"}]}
        timeline = {**TIMELINE, "unique_id": "timeline-1"}
        result, summary = review_current_coverage(post, JOB, timeline, report=report,
            voice_paths=["C:/media/voice.mov"], expected_markers=post["markers"])
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(result["scope"], "current_coverage")
        self.assertIsNone(result["checks"]["baseline_comparison"])
        self.assertNotIn("voice_preserved", result["checks"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(summary["gaps"], [])
        self.assertEqual(summary["resolved_gaps"][0]["coverage_status"], "covered")
        self.assertIsNone(summary["voice_changed"])
        self.assertEqual(report["gaps"], [{"start": 0, "end": 30, "need": "锁角细节"}])

    def test_skill_manual_refresh_keeps_uncertain_and_unreported_empty_ranges_open(self):
        post = inspection()
        post["tracks"]["video"][0]["items"] = post["tracks"]["video"][0]["items"][:2]
        timeline = {**TIMELINE, "unique_id": "timeline-1"}
        report = {"gaps": [{"start": 60, "end": 90, "status": "unverified", "need": "需看候选"}]}
        result, summary = review_current_coverage(post, JOB, timeline, report=report)
        self.assertEqual(result["status"], "needs_verification")
        self.assertFalse(result["checks"]["picture_coverage"])
        self.assertEqual(summary["resolved_gaps"], [])
        result, summary = review_current_coverage(post, JOB, timeline)
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("unmarked_picture_gap", self.codes(result))

    def test_skill_manual_refresh_checks_current_identity_position_and_marker_record(self):
        timeline = {**TIMELINE, "unique_id": "timeline-1"}
        post = inspection()
        result, _ = review_current_coverage(post, JOB, timeline, voice_paths=["C:/different.wav"])
        self.assertIn("current_voice_identity_mismatch", self.codes(result))
        post["tracks"]["audio"][1]["items"][0]["start"] += 1
        result, _ = review_current_coverage(post, JOB, timeline, voice_paths=["C:/media/voice.mov"])
        self.assertIn("current_voice_position_changed", self.codes(result))
        self.assertEqual(result["status"], "awaiting_review")
        self.assertFalse(result["checks"]["current_voice_position_record"])
        self.assertEqual(next(issue for issue in result["issues"] if issue["code"] == "current_voice_position_changed")["severity"], "info")
        expected = inspection()["markers"]
        post["markers"][0]["frame"] += 1
        result, _ = review_current_coverage(post, JOB, timeline, expected_markers=expected)
        self.assertIn("current_group_markers_mismatch", self.codes(result))

    def test_current_refresh_accepts_existing_991_frame_voice_trim_from_992_frame_record(self):
        post = inspection()
        start = post["start_frame"]
        voice = post["tracks"]["audio"][1]["items"][0]
        voice.update(end=start + 991, source_end=991)
        template = post["tracks"]["video"][0]["items"][0]
        boundaries = [round(number * 991 / 23) for number in range(24)]
        post["tracks"]["video"][0]["items"] = [{**template, "uid": f"shot-{number}", "start": start + a, "end": start + b,
                                                 "source_start": a, "source_end": b}
                                                for number, (a, b) in enumerate(zip(boundaries, boundaries[1:]))]
        post["end_frame"] = start + 991
        report = {"gaps": [{"start": a, "end": b, "need": "原空段"} for a, b in ((0, 30), (200, 230), (400, 430), (600, 630))]}
        result, summary = review_current_coverage(post, JOB, {"unique_id": "timeline-1", "frames": 992}, report=report,
            voice_paths=["C:/media/voice.mov"], expected_markers=post["markers"])
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(summary["voice_frames"], 991)
        self.assertEqual(summary["shots"], 23)
        self.assertEqual(summary["picture_gaps"], [])
        self.assertEqual(len(summary["resolved_gaps"]), 4)
        self.assertTrue(result["checks"]["current_voice_record"])
        self.assertFalse(result["checks"]["current_voice_position_record"])
        self.assertEqual(summary["voice_preservation"], "not_compared")
        self.assertIsNone(result["checks"]["baseline_comparison"])

    def test_current_refresh_accepts_same_take_voice_pieces_with_different_source_points_but_rejects_other_take(self):
        post = inspection()
        voice_track = post["tracks"]["audio"][1]
        original = voice_track["items"][0]
        voice_track["items"] = [{**original, "uid": "piece-a", "start": 1000, "end": 1040, "source_start": 5, "source_end": 45},
                                {**original, "uid": "piece-b", "start": 1040, "end": 1090, "source_start": 50, "source_end": 100}]
        timeline = {**TIMELINE, "unique_id": "timeline-1"}
        result, summary = review_current_coverage(post, JOB, timeline, voice_paths=["C:/media/voice.mov"])
        self.assertEqual(result["status"], "awaiting_review")
        self.assertEqual(summary["voice_items"], 2)
        self.assertEqual(summary["voice_frames"], 90)
        self.assertTrue(result["checks"]["current_voice_record"])
        self.assertFalse(result["checks"]["current_voice_position_record"])
        voice_track["items"][1]["media_path"] = "C:/media/another-take.mov"
        result, _ = review_current_coverage(post, JOB, timeline, voice_paths=["C:/media/voice.mov"])
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("current_voice_identity_mismatch", self.codes(result))

    def test_same_duration_replacement_movement_and_property_change_are_detected(self):
        for key, value in (("media_id", "replacement"), ("source_start", 1), ("start", 1001), ("properties", {"AudioVolume": -12})):
            with self.subTest(field=key):
                post = inspection()
                post["tracks"]["audio"][1]["items"][0][key] = value
                result = self.check(post)
                self.assertEqual(result["status"], "needs_repair")
                self.assertIn("voice_changed", self.codes(result))

    def test_every_meaning_marker_attribute_is_compared(self):
        for key, value in (("frame", 1), ("duration", 29), ("custom", "another"), ("name", "改过"), ("note", "改过"), ("color", "Red")):
            with self.subTest(field=key):
                post = inspection()
                post["markers"][0][key] = value
                self.assertIn("group_markers_changed", self.codes(self.check(post)))

    def test_saved_confirmation_and_complete_attributes_are_required(self):
        post = inspection()
        post["saved"] = False
        self.assertEqual(self.check(post)["status"], "verification_failed")
        post = inspection()
        post["tracks"]["video"][0]["locked"] = None
        self.assertIn("readback_incomplete", self.codes(self.check(post)))
        post["fps"] = "unreadable"
        self.assertIn("readback_invalid", self.codes(self.check(post)))

    def test_track_locks_and_settings_are_protected_by_real_baseline(self):
        post = inspection()
        post["tracks"]["audio"][1]["locked"] = True
        post["fps"] = "25"
        result = self.check(post)
        self.assertIn("track_properties_changed", self.codes(result))
        self.assertIn("timeline_settings_changed", self.codes(result))

    def test_paired_audio_requires_same_source_range_position_and_reciprocal_link(self):
        for key, value in (("source_start", 1), ("media_path", "C:/different.mp4"), ("start", 1001), ("linked_ids", [])):
            with self.subTest(field=key):
                post = inspection()
                sound = post["tracks"]["audio"][0]["items"][0]
                sound[key] = value
                if key == "media_path":
                    sound["media_id"] = "different"
                self.assertIn("original_audio_unpaired", self.codes(self.check(post)))
        post = inspection()
        post["tracks"]["video"][0]["items"][0]["has_audio"] = False
        post["tracks"]["audio"][0]["items"] = post["tracks"]["audio"][0]["items"][1:]
        self.assertEqual(self.check(post)["status"], "awaiting_review")

    def test_source_dedup_is_by_media_and_half_open_source_range(self):
        post = inspection()
        other = {"media_id": "new-id-for-same-path", "media_path": "c:\\MEDIA\\SOURCE.mov", "source_start": 20,
                 "source_end": 35, "timeline_name": "其他成片", "timeline_uid": "other"}
        post["other_shots"] = [other]
        self.assertIn("source_range_used_elsewhere", self.codes(self.check(post)))
        other["source_start"], other["source_end"] = 90, 120
        self.assertEqual(self.check(post)["status"], "awaiting_review")
        # Keep an existing source range when revising the same video.
        other["source_start"], other["source_end"] = 20, 35
        self.assertEqual(self.check(post, inspection())["status"], "awaiting_review")
        post["dedup_complete"] = False
        self.assertEqual(self.check(post)["status"], "verification_failed")

    def test_duplicate_sources_inside_one_timeline_cannot_pass(self):
        post = inspection()
        video = post["tracks"]["video"][0]["items"][1]
        sound = post["tracks"]["audio"][0]["items"][1]
        video["source_start"] = sound["source_start"] = 0
        video["source_end"] = sound["source_end"] = 30
        self.assertIn("source_range_reused", self.codes(self.check(post)))

    def test_archived_dedup_is_not_calculated_or_required_for_manual_verification(self):
        post = inspection()
        post.update(dedup_enabled=False, dedup_complete=False, other_shots=None)
        video = post["tracks"]["video"][0]["items"][1]
        sound = post["tracks"]["audio"][0]["items"][1]
        video["source_start"] = sound["source_start"] = 0
        video["source_end"] = sound["source_end"] = 30
        result = self.check(post)
        self.assertEqual(result["status"], "awaiting_review")
        self.assertFalse(result["dedup_enabled"])
        self.assertEqual(result["dedup_status"], "disabled")
        self.assertNotIn("source_range_reused", self.codes(result))
        self.assertNotIn("external_sources_unverified", self.codes(result))
        self.assertNotIn("source_ranges_unique", result["checks"])
        self.assertNotIn("external_source_ranges_unique", result["checks"])
        self.assertTrue(result["checks"]["original_audio_paired"])
        self.assertIn("未计算", result["source_scope"])

    def test_explicit_or_legacy_enabled_dedup_retains_the_original_checks(self):
        for enabled in (True, None):
            with self.subTest(dedup_enabled=enabled):
                post = inspection()
                if enabled is not None:
                    post["dedup_enabled"] = enabled
                post["other_shots"] = [{"media_path": "C:/media/source.mov", "source_start": 10, "source_end": 20}]
                result = self.check(post)
                self.assertTrue(result["dedup_enabled"])
                self.assertIn("source_range_used_elsewhere", self.codes(result))

    def test_manual_recheck_can_accept_sound_changes_and_old_record_stays_unverified(self):
        post = inspection()
        voice = post["tracks"]["audio"][1]
        voice["enabled"], voice["items"][0]["enabled"], voice["items"][0]["properties"] = False, False, {"AudioVolume": -20}
        self.assertEqual(self.check(post, recheck=True)["status"], "awaiting_review")
        result, _ = validate_edit(inspection(), JOB, TIMELINE, None, recheck=True)
        self.assertEqual(result["status"], "needs_repair")
        self.assertIn("baseline_missing", self.codes(result))

    def test_readback_malformed_is_an_explicit_failure(self):
        result, summary = validate_edit({"tracks": {"video": {}, "audio": []}}, JOB, TIMELINE)
        self.assertEqual(result["status"], "verification_failed")
        self.assertIsNone(summary)


if __name__ == "__main__":
    unittest.main()
