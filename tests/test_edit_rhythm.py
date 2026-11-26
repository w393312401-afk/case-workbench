import unittest

import edit_rhythm

FPS = 30.0


def piece(start, end, source, label="", code="", media="a.mov", speed=100.0):
    return {"start": start, "end": end, "source_start": source, "source_end": source + end - start, "media_path": media,
            "label": label, "code": code, "speed": {"Percentage": speed}}


def triples(words):
    """Character timing for words given as (text, start, end): one triple per character, spread evenly."""
    found = []
    for text, start, end in words:
        step = (end - start) / len(text)
        found += [(char, start + step * index, start + step * (index + 1)) for index, char in enumerate(text)]
        found.append((" ", end, end))
    return found


class WordSpanTests(unittest.TestCase):
    def test_words_take_their_own_characters_time(self):
        spans = edit_rhythm.word_spans(["Sharp knife", "Buy now"], triples([("Sharp", 0, 0.4), ("knife", 0.5, 1.0), ("Buy", 1.5, 1.7), ("now", 1.8, 2.0)]))
        self.assertEqual([word["text"] for word in spans[0]], ["Sharp", "knife"])
        self.assertAlmostEqual(spans[0][1]["start"], 0.5)
        self.assertAlmostEqual(spans[1][1]["end"], 2.0)

    def test_a_word_the_voice_never_read_is_left_out(self):
        spans = edit_rhythm.word_spans(["Sharp 202H knife"], triples([("Sharp", 0, 0.4), ("knife", 0.5, 1.0)]))
        self.assertEqual([word["text"] for word in spans[0]], ["Sharp", "knife"])


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.lines = [{"number": 1, "start": 0, "end": 60}, {"number": 2, "start": 60, "end": 150}, {"number": 3, "start": 150, "end": 210}]
        self.spans = edit_rhythm.word_spans(["Sharp knife", "Grind it slowly", "Buy now"], triples([
            ("Sharp", 0, 0.5), ("knife", 0.6, 1.4), ("Grind", 2.0, 2.6), ("it", 2.7, 2.9), ("slowly", 3.0, 4.6), ("Buy", 5.0, 5.4), ("now", 5.6, 6.4)]))

    def test_pieces_whose_source_runs_on_are_one_shot(self):
        shots = edit_rhythm.merge_shots([piece(0, 30, 100), piece(30, 60, 130), piece(60, 90, 500)])
        self.assertEqual([(shot["start"], shot["end"]) for shot in shots], [(0, 60), (60, 90)])

    def test_resolve_inclusive_source_end_still_joins(self):
        first = piece(0, 30, 100)
        first["source_end"] = 129
        self.assertEqual(len(edit_rhythm.merge_shots([first, piece(30, 60, 130)])), 1)

    def test_cut_states(self):
        # 60 is the frame "Grind" starts; 100 is inside "slowly" (90–138); 148 is two frames before "Buy" (150).
        value = edit_rhythm.report([piece(0, 60, 0), piece(60, 100, 500), piece(100, 148, 900), piece(148, 210, 1500)], self.lines, self.spans, FPS)
        cuts = value["cuts"]
        self.assertEqual((cuts["total"], cuts["on"], cuts["mid"], cuts["pause"]), (3, 2, 1, 0))
        self.assertEqual([(cut["frame"], cut["word"]) for cut in cuts["items"]], [(100, "slowly")])

    def test_picture_changing_just_after_a_line_starts_is_late(self):
        value = edit_rhythm.report([piece(0, 68, 0), piece(68, 210, 500)], self.lines, self.spans, FPS)
        self.assertEqual(value["cuts"]["late"], 1)
        self.assertEqual(value["cuts"]["items"][0]["line"], 2)
        self.assertEqual(value["cuts"]["items"][0]["offset"], 8)

    def test_lead_frames_shift_the_words(self):
        lines = [{**line, "start": line["start"] + 15, "end": line["end"] + 15} for line in self.lines]
        value = edit_rhythm.report([piece(0, 75, 0), piece(75, 225, 500)], lines, self.spans, FPS, lead=15)
        self.assertEqual(value["cuts"]["on"], 1)

    def test_lengths_outside_the_range_skip_retimed_shots(self):
        items = [piece(0, 30, 0), piece(30, 60, 500, speed=150.0), piece(60, 210, 900)]
        value = edit_rhythm.report(items, self.lines, self.spans, FPS)
        self.assertEqual([(shot["start"], shot["problem"]) for shot in value["outside"]], [(0, "short"), (60, "long")])
        self.assertEqual(value["shots"], 3)
        self.assertEqual(value["median"], 1.0)

    def test_sections_follow_the_first_and_last_group(self):
        value = edit_rhythm.report([piece(0, 60, 0), piece(60, 150, 500), piece(150, 210, 900)], self.lines, self.spans, FPS)
        self.assertEqual([(part["name"], part["shots"]) for part in value["sections"]], [("开头", 1), ("中段", 1), ("收口", 1)])

    def test_neighbours_of_one_kind_count_only_with_a_label_code(self):
        items = [piece(0, 60, 0, "推磨 侧面 MD001", "MD001"), piece(60, 150, 500, "推磨 侧面 MD002", "MD002"),
                 piece(150, 180, 900, "迷你刀.mov"), piece(180, 210, 1500, "迷你刀.mov")]
        value = edit_rhythm.report(items, self.lines, self.spans, FPS)
        self.assertEqual([(item["start"], item["kind"]) for item in value["repeats"]], [(60, "推磨 侧面")])

    def test_no_word_timing_leaves_cuts_unknown(self):
        value = edit_rhythm.report([piece(0, 60, 0), piece(60, 210, 500)], self.lines, None, FPS)
        self.assertIsNone(value["cuts"])
        self.assertIn("2 个镜头", edit_rhythm.headline(value))

    def test_headline_lists_problems_only(self):
        value = edit_rhythm.report([piece(0, 60, 0), piece(60, 100, 500), piece(100, 148, 900), piece(148, 210, 1500)], self.lines, self.spans, FPS)
        self.assertEqual(edit_rhythm.headline(value), "4 个镜头，中位 1.8 秒；1 处切在词中间")

    def test_nothing_placed_gives_no_report(self):
        self.assertIsNone(edit_rhythm.report([], self.lines, self.spans, FPS))


class WordSectionTests(unittest.TestCase):
    def test_each_group_lists_its_words_with_their_frame(self):
        spans = edit_rhythm.word_spans(["Sharp knife", "Buy now"], triples([("Sharp", 0, 0.4), ("knife", 0.5, 1.0), ("Buy", 1.5, 1.7), ("now", 1.8, 2.0)]))
        text = edit_rhythm.word_section([{"number": 1}, {"number": 2}], spans, FPS, lead=10)
        self.assertIn("- #1：10 Sharp｜25 knife", text)
        self.assertIn("- #2：55 Buy｜64 now", text)

    def test_no_timing_gives_no_section(self):
        self.assertEqual(edit_rhythm.word_section([{"number": 1}], [[]], FPS), "")

    def test_word_section_marks_acoustic_stress_and_pauses(self):
        import struct
        import tempfile
        import wave
        from pathlib import Path
        spans = edit_rhythm.word_spans(
            ["Soft word LOUD", "After pause"],
            triples([("Soft", 0.0, 0.3), ("word", 0.35, 0.65), ("LOUD", 0.7, 1.0), ("After", 1.5, 1.8), ("pause", 1.85, 2.1)]),
        )
        sr = 8000
        samples = [0] * int(sr * 2.2)
        for idx in range(int(0.0 * sr), int(0.65 * sr)):
            samples[idx] = 2000 if idx % 2 == 0 else -2000
        for idx in range(int(0.7 * sr), int(1.0 * sr)):
            samples[idx] = 16000 if idx % 2 == 0 else -16000
        for idx in range(int(1.5 * sr), int(2.1 * sr)):
            samples[idx] = 2000 if idx % 2 == 0 else -2000
        wav_path = Path(tempfile.mkdtemp()) / "voice.wav"
        with wave.open(str(wav_path), "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(sr)
            writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        text = edit_rhythm.word_section([{"number": 1}, {"number": 2}], spans, FPS, lead=0, wav_path=wav_path)
        self.assertIn("LOUD★", text)
        self.assertIn("[停0.5s]", text)
        self.assertIn("声学重音词", text)


if __name__ == "__main__":
    unittest.main()
