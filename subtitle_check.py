"""Compare a timeline's subtitles with the script its voiceover read, and work out which wording fixes are safe.

Text only: `resolve_worker.replace_subtitles` makes the changes in DaVinci. Punctuation and line breaks are left
alone. A few wrong, missing or extra words are fixed without asking, a missing word between two subtitles goes to the
one the script's punctuation ties it to; anything this cannot place with confidence goes to review instead.
"""
from __future__ import annotations

import difflib
import re

WORD = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*")
CLAUSE_END = re.compile(r"[,.!?;:，。！？；：…]")
LINE_BREAK = re.compile("\r\n|[\n\r ]")  # DaVinci writes U+2028 for a break typed inside a subtitle.
MIN_SIMILARITY = 0.8  # Below this the subtitles probably belong to another script or a re-cut voiceover.
MAX_AUTO_WORDS = 3  # A longer difference is more likely a deliberate cut than a wrong word.
MAX_LINES = 2


def _key(word):
    return word.casefold().replace("’", "'")


def _case_matters(word):
    """SHARPAL and 202H are written one way; a capital at the start of a sentence means nothing."""
    return any(letter.isupper() for letter in word[1:])


def _tokens(subtitles):
    return [{"index": index, "word": found.group(0), "start": found.start(), "end": found.end()}
            for index, subtitle in enumerate(subtitles) for found in WORD.finditer(subtitle["text"])]


def _line_count(text):
    return len(LINE_BREAK.split(text))


def _belongs_to_next(gaps, j1, j2):
    """A word missing between two subtitles joins the next one when the script ends a clause just before it."""
    before, after = gaps[j1], gaps[j2] if j2 < len(gaps) else ""
    return bool(CLAUSE_END.search(before)) and not CLAUSE_END.search(after)


def _plan(subtitles, tokens, expected, gaps):
    edits, review = {}, []
    def flag(index, wrote, wanted):
        subtitle = subtitles[index]
        review.append({"start": subtitle["start"], "end": subtitle["end"], "text": subtitle["text"],
                       "wrote": " ".join(wrote), "expected": " ".join(wanted)})
    words = {}
    for token in tokens:
        words[token["index"]] = words.get(token["index"], 0) + 1
    matcher = difflib.SequenceMatcher(None, [_key(token["word"]) for token in tokens], [_key(word) for word in expected], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for token, word in zip(tokens[i1:i2], expected[j1:j2]):
                if _case_matters(word) and token["word"] != word:
                    edits.setdefault(token["index"], []).append((token["start"], token["end"], word))
            continue
        if tag == "insert" and i1 == 0:  # The opening line often has no subtitle on purpose, as the on-screen hook covers it.
            continue
        wrote, wanted = [token["word"] for token in tokens[i1:i2]], expected[j1:j2]
        where = tokens[i1] if i1 < len(tokens) else tokens[-1]
        indexes = {token["index"] for token in tokens[i1:i2]}
        if len(wrote) > MAX_AUTO_WORDS or len(wanted) > MAX_AUTO_WORDS or len(indexes) > 1:
            flag(where["index"], wrote, wanted)
        elif tag == "delete":
            # Extra words come out, but never a whole subtitle or the opening: those may be on-screen hook text.
            if i1 == 0 or words[where["index"]] == len(wrote):
                flag(where["index"], wrote, wanted)
                continue
            if tokens[i1 - 1]["index"] == where["index"]:  # From the word before, so punctuation after the extra words stays.
                span = (tokens[i1 - 1]["end"], tokens[i2 - 1]["end"])
            else:  # Opening words of a subtitle: up to the next word.
                span = (where["start"], tokens[i2]["start"])
            edits.setdefault(where["index"], []).append((*span, ""))
        elif tag == "replace":
            edits.setdefault(where["index"], []).append((tokens[i1]["start"], tokens[i2 - 1]["end"], " ".join(wanted)))
        elif 0 < i1 < len(tokens) and tokens[i1 - 1]["index"] == tokens[i1]["index"]:
            edits.setdefault(where["index"], []).append((tokens[i1]["start"], tokens[i1]["start"], " ".join(wanted) + " "))
        elif i1 < len(tokens) and _belongs_to_next(gaps, j1, j2):
            edits.setdefault(where["index"], []).append((where["start"], where["start"], " ".join(wanted) + " "))
        else:  # Missing between two subtitles (or at the end): it continues the subtitle before.
            before = tokens[i1 - 1]
            edits.setdefault(before["index"], []).append((before["end"], before["end"], " " + " ".join(wanted)))
    return edits, review


def compare(subtitles, script_lines):
    """Return the wording fixes to make and what needs a person: `changes` and `review` hold frames and text."""
    subtitles = sorted(subtitles, key=lambda item: item["start"])
    script = " ".join(script_lines)
    found = list(WORD.finditer(script))
    tokens, expected = _tokens(subtitles), [item.group(0) for item in found]
    # gaps[j] is the script text between word j - 1 and word j.
    gaps = [script[(found[j - 1].end() if j else 0):item.start()] for j, item in enumerate(found)]
    keys = [_key(token["word"]) for token in tokens], [_key(word) for word in expected]
    similarity = difflib.SequenceMatcher(None, *keys, autojunk=False).ratio() if tokens and expected else 0.0
    result = {"subtitles": len(subtitles), "similarity": round(similarity, 3), "changes": [], "review": [], "different": False}
    if not subtitles or not expected:
        return result
    long = [{"start": item["start"], "end": item["end"], "text": item["text"], "wrote": "", "expected": f"超过 {MAX_LINES} 行"}
            for item in subtitles if _line_count(item["text"]) > MAX_LINES]
    if similarity < MIN_SIMILARITY:
        return {**result, "different": True, "review": long}
    edits, review = _plan(subtitles, tokens, expected, gaps)
    for index, spans in sorted(edits.items()):
        before = after = subtitles[index]["text"]
        for start, end, words in sorted(spans, reverse=True):
            after = after[:start] + words + after[end:]
        result["changes"].append({"start": subtitles[index]["start"], "end": subtitles[index]["end"], "before": before, "after": after})
    return {**result, "review": sorted(review + long, key=lambda item: item["start"])}


def summary(check):
    """One line for the timeline card and the toast."""
    changed, review = len(check.get("changes") or []), len(check.get("review") or [])
    if check["status"] == "no_subtitles":
        return "这条时间线还没有字幕"
    if check["status"] == "different":
        return "字幕和这版配音稿差异太大，没有自动修改，请确认字幕是不是这版稿子"
    if check["status"] == "blocked":
        return f"发现 {changed} 处用词错误，但当前达芬奇版本不能用脚本修改"
    if check["status"] == "failed":
        return f"字幕修改没有全部完成：{check.get('error') or '请到达芬奇核对'}"
    parts = ([f"已自动改 {changed} 处"] if changed else []) + ([f"{review} 处待你确认"] if review else [])
    return "字幕" + "，".join(parts) if parts else "字幕用词和配音稿一致"
