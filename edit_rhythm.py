"""成片节奏报告：镜头时长、切点落在口播的什么位置、相邻镜头是不是同一类画面。

只读，也不看画面：数据是交付快照里的成片片段、意群标记，和这版配音自己的逐字时间。
报告只列数字和位置，不判断好坏以外的事；镜头该多长、能不能切在这里仍由剪辑技能决定。
"""
from __future__ import annotations

import difflib
import math
from pathlib import Path
import re
import statistics
import wave

# 剪辑技能的普通镜头时长范围（秒）；变速镜头按动作镜头公式算，不套这个范围。
SHORT, LONG = 1.2, 4.0
# 开头第一意群单镜头超过这个时长（秒）时，放置前提醒拆成两镜提节奏。
HOOK_LONG = 3.2
# 新画面比词早到几帧算踩在词上：早 4 帧以内，或晚 2 帧以内（约 0.07 秒，看上去仍是同时）。
AHEAD, BEHIND = 4, 2
# 一句话开口后这么久之内才换画面，算画面晚到（秒）。
LATE = 0.5
# 词间静音达到这个时长（秒）算明显气口，适合自然切点或让原声 ASMR 呼吸。
PAUSE_MIN = 0.30
# 单个词的 RMS 响度高于整段中位数这么多 dB 时标为重音词（★）。
STRESS_DB = 2.0
CODE = re.compile(r"[A-Za-z]+\d+[a-z]?")
LISTED = 12
RHYTHM_CONTEXT_FILE = "rhythm-context.json"


def word_spans(lines, triples):
    """Each line's words as (start, end) seconds from the voice's character timing; unmatched words are left out.

    Matched the way `workbench_production.word_times` does, so a reading swapped in for the voice only loses its own
    words."""
    words = [str(line or "").split() for line in lines]
    target = [(char.lower(), number, index) for number, line in enumerate(words) for index, word in enumerate(line) for char in word]
    source = [(char.lower(), start, end) for text, start, end in triples for char in text if not char.isspace()]
    matcher = difflib.SequenceMatcher(None, [item[0] for item in target], [item[0] for item in source], autojunk=False)
    found = [[None] * len(line) for line in words]
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            _, number, index = target[block.a + offset]
            _, start, end = source[block.b + offset]
            if not (math.isfinite(start) and math.isfinite(end)):
                continue
            span = found[number][index]
            found[number][index] = (start, end) if span is None else (min(span[0], start), max(span[1], end))
    return [[{"text": word, "start": span[0], "end": max(span[1], span[0])} for word, span in zip(line, spans) if span]
            for line, spans in zip(words, found)]


def kind(label):
    """What a clip shows without its own code, as the pick check reads it."""
    return " ".join(CODE.sub(" ", str(label or "")).split())


def _source(item):
    left, right = item.get("source_start"), item.get("source_end")
    if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
        return None
    if right - left + 1 == item["end"] - item["start"]:  # Resolve reports the last frame itself at normal speed.
        right += 1
    return left, right


def _retimed(item):
    speed = item.get("speed")
    value = speed.get("Percentage") if isinstance(speed, dict) else speed
    return isinstance(value, (int, float)) and abs(value - 100) > 0.5


def merge_shots(items):
    """Timeline pieces as shots: pieces whose source frames run on from each other are one shot, as the skill counts."""
    shots = []
    for item in sorted(items, key=lambda value: value["start"]):
        source, last = _source(item), shots[-1] if shots else None
        if (last and source and last["tail"] and last["end"] == item["start"] and last["media"] == item.get("media_path")
                and last["tail"] == source[0] and not last["retimed"] and not _retimed(item)):
            last.update(end=item["end"], tail=source[1])
            continue
        shots.append({"start": item["start"], "end": item["end"], "media": item.get("media_path"), "tail": source[1] if source else None,
                      "retimed": _retimed(item), "code": item.get("code") or "",
                      # A clip without a label code is named after its file, which says nothing about the picture.
                      "kind": kind(item.get("label")) if item.get("code") else ""})
    return shots


def _cut(frame, words, firsts, fps):
    """Where one cut lands in the voiceover: on a word's start, inside a word, or in a pause."""
    nearest = min(words, key=lambda word: abs(word["frame"] - frame), default=None)
    late = next((word for word in firsts if BEHIND < frame - word["frame"] <= LATE * fps), None)
    if nearest is not None and -BEHIND <= nearest["frame"] - frame <= AHEAD:
        state, word = "on", nearest
    else:
        inside = next((word for word in words if word["frame"] + BEHIND < frame < word["end"] - 1), None)
        state, word = ("mid", inside) if inside else ("pause", nearest)
    return {"frame": frame, "state": state, "late": bool(late), "word": (late or word or {}).get("text", ""),
            "line": (late or word or {}).get("line"), "offset": frame - (late or word)["frame"] if (late or word) else None}


def report(items, lines, spans, fps, lead=0):
    """The rhythm of one delivered cut.

    items: picture pieces in cut frames from 0 (start, end, source, speed, label). lines: meaning groups in cut
    frames (number, start, end). spans: `word_spans` per line in voiceover seconds, or None when the take kept no
    timing. lead: frames before the voiceover starts."""
    shots = merge_shots(items)
    if not shots or not fps:
        return None
    lengths = [(shot["end"] - shot["start"]) / fps for shot in shots]
    lines = sorted(lines, key=lambda line: line["start"])
    opening_end = lines[0]["end"] if lines else 0
    closing_start = lines[-1]["start"] if len(lines) > 1 else math.inf
    sections = []
    for name, inside in (("开头", lambda shot: shot["start"] < opening_end),
                         ("中段", lambda shot: opening_end <= shot["start"] < closing_start),
                         ("收口", lambda shot: shot["start"] >= closing_start and shot["start"] >= opening_end)):
        own = [length for shot, length in zip(shots, lengths) if inside(shot)]
        if own:
            sections.append({"name": name, "shots": len(own), "average": round(sum(own) / len(own), 2)})
    outside = [{"start": shot["start"], "end": shot["end"], "seconds": round(length, 2), "problem": "short" if length < SHORT else "long"}
               for shot, length in zip(shots, lengths) if not shot["retimed"] and (length < SHORT - 1e-6 or length > LONG + 1e-6)]
    # Two shots in a row cut from different places but showing the same kind of picture add nothing the first did not show.
    repeats = [{"start": after["start"], "kind": after["kind"], "codes": [before["code"], after["code"]]}
               for before, after in zip(shots, shots[1:]) if after["kind"] and after["kind"] == before["kind"]]
    result = {"shots": len(shots), "seconds": round((shots[-1]["end"] - shots[0]["start"]) / fps, 1),
              "median": round(statistics.median(lengths), 2), "shortest": round(min(lengths), 2), "longest": round(max(lengths), 2),
              "sections": sections, "outside": outside, "repeats": repeats, "cuts": None}
    words, firsts = [], []
    for line, own in zip(lines, spans or []):
        for index, word in enumerate(own or []):
            entry = {"text": word["text"], "line": line.get("number"), "frame": lead + word["start"] * fps, "end": lead + word["end"] * fps}
            words.append(entry)
            if index == 0:
                firsts.append(entry)
    if words:
        cuts = [_cut(shot["start"], words, firsts, fps) for shot in shots[1:]]
        for cut in cuts:
            cut["offset"] = None if cut["offset"] is None else round(cut["offset"], 1)
        count = lambda state: sum(1 for cut in cuts if cut["state"] == state)
        result["cuts"] = {"total": len(cuts), "on": count("on"), "pause": count("pause"), "mid": count("mid"),
                          "late": sum(1 for cut in cuts if cut["late"]),
                          "items": [cut for cut in cuts if cut["state"] == "mid" or cut["late"]][:LISTED * 3]}
    return result


def headline(value):
    """One line for the edit card; problems only after the counts."""
    if not value:
        return ""
    parts = [f"{value['shots']} 个镜头，中位 {value['median']} 秒"]
    cuts = value.get("cuts")
    if cuts and cuts["mid"]:
        parts.append(f"{cuts['mid']} 处切在词中间")
    if cuts and cuts["late"]:
        parts.append(f"{cuts['late']} 处画面晚到")
    if value["outside"]:
        parts.append(f"{len(value['outside'])} 个镜头超出 {SHORT:g}–{LONG:g} 秒")
    return "；".join(parts)


def _read_wav_mono(wav_path):
    """Read a PCM WAV file into (sample_rate, mono float64 samples in [-1, 1]), or None if unreadable."""
    if not wav_path:
        return None
    path = Path(wav_path)
    if not path.is_file():
        return None
    try:
        import numpy as np
        with wave.open(str(path), "rb") as reader:
            channels = reader.getnchannels()
            width = reader.getsampwidth()
            rate = reader.getframerate()
            raw = reader.readframes(reader.getnframes())
        if not raw or rate <= 0 or channels <= 0 or width not in (1, 2, 4):
            return None
        if width == 1:
            samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
        elif width == 2:
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
        else:
            samples = np.frombuffer(raw, dtype=np.int32).astype(np.float64) / 2147483648.0
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return rate, samples
    except Exception:
        return None


def voice_acoustics(wav_path, spans):
    """Per-word stress flags (from WAV RMS relative to utterance median) and inter-word pauses >= PAUSE_MIN.

    Returns {"stressed": {(line_idx, word_idx), ...}, "pauses": {(line_idx, word_idx): seconds, ...}} where
    pauses[(li, wi)] is the silence gap right *after* word (li, wi). Never raises on bad/missing WAV."""
    stressed, pauses = set(), {}
    flat = [(li, wi, word) for li, own in enumerate(spans or []) for wi, word in enumerate(own or [])]
    for (li, wi, cur), (_, _, nxt) in zip(flat, flat[1:]):
        gap = nxt["start"] - cur["end"]
        if gap >= PAUSE_MIN:
            pauses[(li, wi)] = round(gap, 2)
    audio = _read_wav_mono(wav_path)
    if audio is None or not flat:
        return {"stressed": stressed, "pauses": pauses}
    try:
        import numpy as np
        rate, samples = audio
        total = len(samples)
        energies = []
        for li, wi, word in flat:
            a = max(0, int(word["start"] * rate))
            b = min(total, max(a + 1, int(word["end"] * rate)))
            seg = samples[a:b]
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            energies.append((li, wi, word["text"], rms))
        positive = [rms for _, _, _, rms in energies if rms > 1e-6]
        if not positive:
            return {"stressed": stressed, "pauses": pauses}
        median_rms = statistics.median(positive)
        by_line = {}
        for li, wi, text, rms in energies:
            if rms <= 1e-6:
                continue
            db = 20.0 * math.log10(rms / median_rms)
            clean = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", text)
            if db >= STRESS_DB and (len(clean) >= 3 or db >= STRESS_DB + 1.5):
                by_line.setdefault(li, []).append((db, wi))
        for li, items in by_line.items():
            for _, wi in sorted(items, reverse=True)[:3]:
                stressed.add((li, wi))
    except Exception:
        pass
    return {"stressed": stressed, "pauses": pauses}


def word_section(lines, spans, fps, lead=0, wav_path=None):
    """The handoff part that lets the editor put cuts on words: each group's words with the frame they start at."""
    acoustics = voice_acoustics(wav_path, spans) if wav_path else {"stressed": set(), "pauses": {}}
    stressed, pauses = acoustics["stressed"], acoustics["pauses"]
    rows = []
    for li, (line, own) in enumerate(zip(lines, spans or [])):
        if not own:
            continue
        tokens = []
        for wi, word in enumerate(own):
            frame = math.floor(lead + word["start"] * fps + 1e-6)
            mark = "★" if (li, wi) in stressed else ""
            token = f"{frame} {word['text']}{mark}"
            gap = pauses.get((li, wi))
            if gap is not None:
                token += f" [停{gap:.1f}s]"
            tokens.append(token)
        rows.append(f"- #{line['number']}：" + "｜".join(tokens))
    if not rows:
        return ""
    notes = ["每行一个意群，「帧 词」是这个词在目标时间线上开口的帧（从第 0 帧算），取自这版配音自己的逐字时间。"
             "按技能「切点对词」使用：新画面在它要配的那个词开口前 0–3 帧出现，切点不落在一个词的中间。"
             "配音在达芬奇里被剪过或移动过时这些帧不再准确，以当前工程为准。"]
    if stressed or pauses:
        notes.append("标「★」的是声学重音词（适合做视觉冲击或切景别），「[停Xs]」是 ≥0.3s 气口停顿（适合自然切点或留给原声 ASMR 呼吸）。")
    return "\n".join(["## 词起点（切点对词用）", "", *notes, "", *rows, ""])


def check_plan(picks, by_key, lines, spans, fps, lead=0):
    """Pre-placement rhythm & fluency check on an ordered picks.json shot list.

    Returns (issues, messages). Each pick may carry optional `start`/`end`, `shot_size`, `retimed`, `reason`.
    Contiguous picks whose source frames run on from each other merge into one shot, matching `merge_shots`.
    """
    if not picks or not fps:
        return [], []
    shots = []
    cursor = lead
    for index, (pick, row) in enumerate(picks):
        frames = pick.get("frames") if isinstance(pick.get("frames"), int) and not isinstance(pick.get("frames"), bool) and pick.get("frames") > 0 else 0
        start = pick.get("start") if isinstance(pick.get("start"), int) and not isinstance(pick.get("start"), bool) else cursor
        end = pick.get("end") if isinstance(pick.get("end"), int) and not isinstance(pick.get("end"), bool) and pick.get("end") > start else (start + frames)
        if end <= start:
            continue
        cursor = end
        source = (row or {}).get("source") if isinstance((row or {}).get("source"), list) and len((row or {}).get("source")) == 2 else None
        media = (row or {}).get("media_path") or ""
        retimed = bool(pick.get("retimed"))
        reason = str(pick.get("reason") or "").strip()
        code = str((row or {}).get("key") or pick.get("key") or "").split("-")[0]
        label_kind = kind((row or {}).get("label")) if row and row.get("label") else ""
        shot_size = str(pick.get("shot_size") or ((row or {}).get("description") or {}).get("shot_size") or "").strip()
        last = shots[-1] if shots else None
        if (last and source and last["tail"] is not None and last["end"] == start and last["media"] == media
                and last["tail"] == source[0] and not last["retimed"] and not retimed):
            last["end"] = end
            last["tail"] = source[1]
            if reason and not last["reason"]:
                last["reason"] = reason
            continue
        shots.append({"index": index, "start": start, "end": end, "media": media, "tail": source[1] if source else None,
                      "retimed": retimed, "reason": reason, "code": code, "key": (row or {}).get("key") or str(pick.get("key") or ""),
                      "kind": label_kind, "stage": ((row or {}).get("stage") or {}).get("name") or "", "shot_size": shot_size})
    if not shots:
        return [], []
    issues, messages = [], []
    words, firsts = [], []
    for line, own in zip(sorted(lines or [], key=lambda item: item.get("start", 0)), spans or []):
        for wi, word in enumerate(own or []):
            entry = {"text": word["text"], "line": line.get("number"), "frame": lead + word["start"] * fps, "end": lead + word["end"] * fps}
            words.append(entry)
            if wi == 0:
                firsts.append(entry)
    # 1. Cut-to-word checks: mid-word cuts and late visual arrivals.
    if words:
        for shot in shots[1:]:
            if shot["reason"]:
                continue
            cut = _cut(shot["start"], words, firsts, fps)
            if cut["late"]:
                suggest = max(lead, math.floor((shot["start"] - (cut["offset"] or 0)) - 2))
                issues.append({"key": shot["key"], "problem": "late_cut", "frame": shot["start"], "word": cut["word"], "line": cut["line"], "suggest": suggest})
                messages.append(f"- 节奏（第 {shot['start']} 帧，{shot['code']}）：画面晚于意群 #{cut['line']} 开头词「{cut['word']}」约 {round(cut['offset'] or 0)} 帧，建议把切点提前到第 {suggest} 帧或写 reason。")
            elif cut["state"] == "mid":
                suggest = max(lead, math.floor((shot["start"] - (cut["offset"] or 0)) - 2))
                issues.append({"key": shot["key"], "problem": "mid_cut", "frame": shot["start"], "word": cut["word"], "line": cut["line"], "suggest": suggest})
                messages.append(f"- 节奏（第 {shot['start']} 帧，{shot['code']}）：切在「{cut['word']}」（意群 #{cut['line']}）中间，建议把切点前移到第 {suggest} 帧（该词开口前 2 帧）或写 reason。")
    # 2. Adjacent same-kind shots (non-continuous source, same grinding stage if staged).
    for before, after in zip(shots, shots[1:]):
        same_stage = (not before["stage"] or not after["stage"] or before["stage"] == after["stage"])
        if after["kind"] and after["kind"] == before["kind"] and same_stage and not after["reason"]:
            issues.append({"key": after["key"], "problem": "repeat_kind", "frame": after["start"], "kind": after["kind"], "codes": [before["code"], after["code"]]})
            messages.append(f"- 流畅性（第 {after['start']} 帧，{before['code']} → {after['code']}）：相邻两镜同属「{after['kind']}」且源帧不相连，容易产生视觉重复/跳切，建议换不同景别或动作镜头，或在 {after['code']} 的 reason 说明。")
    # 3. Three consecutive shots with the same shot_size.
    for a, b, c in zip(shots, shots[1:], shots[2:]):
        if a["shot_size"] and a["shot_size"] == b["shot_size"] == c["shot_size"] and not c["reason"]:
            issues.append({"key": c["key"], "problem": "same_shot_size", "frame": c["start"], "shot_size": c["shot_size"]})
            messages.append(f"- 景别交替（第 {c['start']} 帧，{a['code']} → {b['code']} → {c['code']}）：连续 3 个「{c['shot_size']}」，建议穿插特写/近景/中景拉开节奏，或在 {c['code']} 的 reason 说明。")
    # 4. Opening hook pacing: single shot > HOOK_LONG seconds covering the opening group.
    sorted_lines = sorted(lines or [], key=lambda item: item.get("start", 0))
    if sorted_lines:
        opening_end = sorted_lines[0].get("end", 0)
        opening_shots = [shot for shot in shots if shot["start"] < opening_end]
        if len(opening_shots) == 1 and not opening_shots[0]["retimed"] and not opening_shots[0]["reason"]:
            sec = (opening_shots[0]["end"] - opening_shots[0]["start"]) / fps
            if sec > HOOK_LONG:
                issues.append({"key": opening_shots[0]["key"], "problem": "slow_hook", "seconds": round(sec, 2)})
                messages.append(f"- 开头节奏（{opening_shots[0]['code']}，{sec:.2f} 秒）：开头第一意群仅用 1 个超过 {HOOK_LONG:g} 秒的长镜头，建议拆成 2 个有视觉递进/反差的镜头，或在 reason 说明为何一镜到底。")
    return issues, messages
