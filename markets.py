"""Target markets for writing and voicing a script: one table, read by the AI task, the job and the voiceover.

What each market means for the words themselves lives in the writing skill's 「目标市场」 section; this only names
the markets and holds what the code needs. British English has no language code of its own: the accent comes from
the voice, so only German and Spanish tell ElevenLabs a language. Reading speeds are planning estimates.
"""
from __future__ import annotations

DEFAULT = "us"
MARKETS = {
    "us": {"label": "美国 · 英语", "language": "英文", "tts_language": "", "words_per_minute": 150, "voice_hint": ""},
    "uk": {"label": "英国 · 英语", "language": "英式英语", "tts_language": "", "words_per_minute": 150,
           "voice_hint": "英式口音由音色决定，请选英式音色。"},
    "de": {"label": "德国 · 德语", "language": "德语", "tts_language": "de", "words_per_minute": 130,
           "voice_hint": "请选能说德语的音色。Multilingual v2 不接受语言指定，会按文字自动判断。"},
    "es": {"label": "西班牙 · 西语", "language": "西班牙语（西班牙本土）", "tts_language": "es", "words_per_minute": 170,
           "voice_hint": "请选西班牙本土口音的音色。Multilingual v2 不接受语言指定，会按文字自动判断。"},
}


def stored(value):
    """The market a saved record names; records from before markets existed are American."""
    return value if isinstance(value, str) and value in MARKETS else DEFAULT


def public():
    return [{"id": key, "label": item["label"], "voice_hint": item["voice_hint"]} for key, item in MARKETS.items()]
