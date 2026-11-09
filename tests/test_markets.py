import unittest

import ai_tasks
import markets
from elevenlabs_client import ElevenLabsClient, VoiceError
from production_store import apply_readings
from variant_parse import _sentences, looks_like_script, parse_variants, reformat_problems
from workbench_production import map_groups


GERMAN = """**变体 #1 测试**

**口播（配音用）**

```
Für dein Küchenmesser
Schärfe in fünf Minuten
```

**对应中文**

```
给你的厨刀
五分钟变锋利
```

**意群表**

| # | 口播 | 画面要证明什么 | 素材标签 | 证据 |
|---|---|---|---|---|
| 1 | Für dein Küchenmesser | 厨刀 | a | b |
| 2 | Schärfe in fünf Minuten | 切纸 | a | b |
"""


class FakeTransport:
    def __init__(self):
        self.payloads = []

    def __call__(self, method, url, headers, data, timeout):
        import base64
        import json
        self.payloads.append(json.loads(data))
        return 200, {}, json.dumps({"audio_base64": base64.b64encode(b"mp3").decode()}).encode()


class MarketTests(unittest.TestCase):
    def test_old_records_are_american(self):
        self.assertEqual(markets.stored(None), "us")
        self.assertEqual(markets.stored("fr"), "us")
        self.assertEqual(markets.stored("de"), "de")

    def test_default_market_leaves_the_prompt_unchanged(self):
        self.assertEqual(ai_tasks._market_rule("us"), "")
        self.assertEqual(ai_tasks._delivery_format("us"), ai_tasks.DELIVERY_FORMAT)

    def test_other_markets_name_their_language(self):
        self.assertIn("写作参考“目标市场”", ai_tasks._market_rule("de"))
        layout = ai_tasks._delivery_format("de")
        self.assertIn("块内只放要念的德语", layout)
        self.assertIn("与德语逐行对应", layout)
        self.assertIn("英式英语", ai_tasks._delivery_format("uk"))

    def test_read_estimate_counts_accented_words_at_the_markets_speed(self):
        german = ai_tasks.read_estimates(GERMAN, "de")[0]
        self.assertEqual(german["words"], 7)
        self.assertEqual(german["seconds"], round(7 * 60 / 130))
        self.assertEqual(ai_tasks.read_estimates(GERMAN)[0]["seconds"], round(7 * 60 / 150))


class ParsingTests(unittest.TestCase):
    def test_strict_delivery_keeps_accents(self):
        variant = parse_variants(GERMAN)[0]
        self.assertEqual(variant["problems"], [])
        self.assertEqual(variant["lines"][0]["spoken"], "Für dein Küchenmesser")

    def test_reformat_notices_a_changed_accented_letter(self):
        changed = GERMAN.replace("Für dein", "Fur dein")
        self.assertTrue(reformat_problems(GERMAN, changed))
        self.assertEqual(reformat_problems(GERMAN, GERMAN), [])

    def test_loose_heading_in_spanish_or_german_is_recognised(self):
        for label in ("Deutsch", "Español", "西班牙语"):
            text = f"## 版本 A\n\n**{label}**\n\n" + "Una frase para probar el texto. " * 3
            self.assertEqual(len(parse_variants(text)), 1, label)
        self.assertTrue(looks_like_script("Für dein Küchenmesser " * 14))

    def test_sentences_split_before_accented_capitals_and_inverted_marks(self):
        self.assertEqual(_sentences("Das geht schnell. Überall gleich."), ["Das geht schnell.", "Überall gleich."])
        self.assertEqual(_sentences("Es fácil. ¿Lo ves? Ánimo."), ["Es fácil.", "¿Lo ves?", "Ánimo."])

    def test_reading_does_not_replace_inside_an_accented_word(self):
        readings = [{"written": "mm", "spoken": "Millimeter"}]
        self.assertEqual(apply_readings(["Kümm 2 mm"], readings), (["Kümm 2 Millimeter"], 1))


class VoiceTests(unittest.TestCase):
    def speak(self, model_id, **extra):
        transport = FakeTransport()
        ElevenLabsClient(key_provider=lambda: "sk_test", transport=transport).speech("VOICE123456789", "Hallo", model_id, {}, **extra)
        return transport.payloads[0]

    def test_language_goes_to_models_that_accept_it(self):
        self.assertEqual(self.speak("eleven_v4", language_code="de")["language_code"], "de")
        self.assertEqual(self.speak("eleven_v4_turbo", language_code="es")["language_code"], "es")

    def test_multilingual_v2_is_never_sent_a_language(self):
        self.assertNotIn("language_code", self.speak("eleven_multilingual_v2", language_code="de"))

    def test_no_language_by_default(self):
        self.assertNotIn("language_code", self.speak("eleven_v4"))

    def test_british_english_sends_no_code(self):
        self.assertEqual(markets.MARKETS["uk"]["tts_language"], "")

    def test_alignment_matches_accented_characters(self):
        lines = ["Für Küchen", "Straße ñu"]
        triples = [(char, index * 0.1, index * 0.1 + 0.1) for index, char in enumerate("Für Küchen\nStraße ñu")]
        groups = map_groups(lines, triples)
        self.assertEqual([group["coverage"] for group in groups], [1.0, 1.0])
        self.assertLess(groups[0]["end"], groups[1]["start"] + 0.11)

    def test_voice_error_type_is_shared(self):
        self.assertTrue(issubclass(VoiceError, ValueError))


if __name__ == "__main__":
    unittest.main()
