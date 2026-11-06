"""1:1 replica of a case video: the case's own words become the script, its cuts become the shot table of the AI edit.

The voiceover never runs exactly as long as the original, so each cut is carried over through the time of the lines
around it: a cut one third into a sentence lands one third into the same sentence of the new voiceover.
"""
from __future__ import annotations

from pathlib import Path
import re
import shutil

from production_validation import rate
from retro_draft import PrepareError, _run, _sheet

SCENE = 0.2  # On real case footage 0.25 and up merged cuts inside hand-held shots; 0.15 adds nothing this does not catch.
MIN_SHOT = 0.15
FRAME_RATE = 4
LONG_SHOT = 1.0  # A shot this long gets frames across its action instead of a single one.
MAX_FRAMES = 6
TILE_H = 480
RANGE = re.compile(r"^\s*\[([^\]]+)\]\s*(\S.*)$")
CLOCK = re.compile(r"\d+(?::\d{1,2}){0,2}(?:\.\d+)?s?")
NO_SPEECH = re.compile(r"无口播|未检出")
MAX_CONTINUATIONS = 2


def _seconds(part):
    part = part.strip().replace(",", ".")
    if not CLOCK.fullmatch(part):
        return None
    total = 0.0
    for unit in part.rstrip("s").split(":"):
        total = total * 60 + float(unit)
    return total


def _timed(line):
    """(start, end, words) of a line like "[0.18s - 4.34s] words", else None."""
    found = RANGE.match(line)
    if not found:
        return None
    ends = [_seconds(part) for part in re.split(r"\s*[-–—]\s*", found.group(1))]
    if len(ends) != 2 or None in ends or ends[1] < ends[0]:
        return None
    return ends[0], ends[1], found.group(2).strip()


def parse_transcript(text):
    """The spoken lines and their times; every time is None unless every line carries one, as the transcript player does."""
    rows = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not rows or NO_SPEECH.search(rows[0]):
        return [], False
    parsed = [_timed(row) for row in rows]
    if all(parsed):
        return [{"spoken": words, "start": start, "end": end} for start, end, words in parsed], True
    return [{"spoken": item[2] if item else row, "start": None, "end": None} for item, row in zip(parsed, rows)], False


def case_source(case):
    """What a replica needs from a case. Raises PrepareError with the sentence the user should read."""
    current = case.get("current") or {}
    video = (current.get("assets") or {}).get("video")
    if not video or not Path(video).is_file():
        raise PrepareError("这条案例没有可读的原视频，无法对照画面。")
    lines, timed = parse_transcript((current.get("transcript") or {}).get("text"))
    if not lines:
        raise PrepareError("这条案例还没有口播文案，请先转写。")
    chinese = [line.strip() for line in str((current.get("translation_zh") or {}).get("text") or "").splitlines() if line.strip()]
    return {"video": video, "lines": lines, "timed": timed, "chinese": chinese if len(chinese) == len(lines) else [],
            "title": str(current.get("title") or case.get("title") or case.get("case_id") or "").strip(),
            "caption": str((current.get("metrics") or {}).get("published_title") or "").strip(),
            "direction_id": str((current.get("test_direction") or {}).get("direction_id") or ""),
            "origin": current.get("content_origin") or "", "machine_transcript": "未经人工" in str((current.get("transcript") or {}).get("status") or "")}


def detect_shots(video):
    """The video's size, duration and hard cuts."""
    probe = _run("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                 "-of", "default=noprint_wrappers=1", str(video), timeout=60)
    facts = dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    try:
        duration, width, height = float(facts["duration"]), int(facts["width"]), int(facts["height"])
    except (KeyError, ValueError) as exc:
        raise PrepareError("读不出原视频的时长和画幅。") from exc
    found = _run("ffmpeg", "-v", "info", "-i", str(video), "-an", "-vf", f"select='gt(scene,{SCENE})',showinfo", "-f", "null", "-").stderr
    bounds = [0.0]
    for cut in sorted({round(float(value), 3) for value in re.findall(r"pts_time:([\d.]+)", found)}):
        if cut - bounds[-1] >= MIN_SHOT and duration - cut >= MIN_SHOT:
            bounds.append(cut)
    bounds.append(round(duration, 3))
    return {"duration": round(duration, 3), "width": width, "height": height,
            "shots": [{"number": n, "start": start, "end": end} for n, (start, end) in enumerate(zip(bounds, bounds[1:]), 1)]}


def estimated_spans(spoken, duration):
    """Line times by share of the words, for a transcript without timestamps or a script edited after the original."""
    sizes = [max(len(line), 1) for line in spoken]
    spans, spent, total = [], 0, sum(sizes)
    for size in sizes:
        spans.append({"start": round(duration * spent / total, 3), "end": round(duration * (spent + size) / total, 3)})
        spent += size
    return spans


def snapshot(case_id, source, spoken, aligned=False):
    """The frozen reference a job carries: shots of the original and which script line each one sits under.

    `aligned` is an optimized script written line for line against the original (照原片优化): when its line count
    still matches, line n keeps the time of the original's line n, so the cuts carry over as for the original words."""
    video = detect_shots(source["video"])
    same = [line["spoken"] for line in source["lines"]] == spoken
    matched = same or (aligned and len(spoken) == len(source["lines"]))
    timed = matched and source["timed"]
    spans = ([{"start": line["start"], "end": line["end"]} for line in source["lines"]] if timed else
             estimated_spans(spoken, video["duration"]))
    for shot in video["shots"]:
        shot["lines"] = [n for n, span in enumerate(spans, 1) if min(shot["end"], span["end"]) - max(shot["start"], span["start"]) > .05]
    notes = []
    if not matched:
        notes.append("口播和原稿不完全相同，每句对应的原片时间按字数估算，镜头切点可能有偏差。")
    elif not same and timed:
        notes.append("口播是逐句对应原稿的优化稿，每句沿用原稿那一句的原片时间；措辞变了，配音快慢会让切点略有偏差。")
    elif not timed:
        notes.append("原稿没有时间戳，每句对应的原片时间按字数估算，镜头切点可能有偏差；可先在案例里重新转写。")
    if source["machine_transcript"]:
        notes.append("口播来自机器转写，没有人工听校；配音前请核对，有错到案例的「编辑文案」改后重新创建。")
    return {"case_id": case_id, "case_title": source["title"], "duration": video["duration"], "width": video["width"],
            "height": video["height"], "timed": timed, "spans": spans, "shots": video["shots"], "notes": notes,
            "wording": "original" if same else "aligned" if matched else "changed"}


def line_records(spoken, chinese, reference):
    """Lines of the 意群表: what each one must show is the original's shots under it."""
    records = []
    for number, line in enumerate(spoken, 1):
        shots = [shot for shot in reference["shots"] if number in shot["lines"]]
        span = reference["spans"][number - 1]
        where = f"镜头 {shots[0]['number']}" + (f"–{shots[-1]['number']}" if len(shots) > 1 else "") if shots else "无对应镜头"
        records.append({"number": number, "spoken": line, "zh": chinese[number - 1] if len(chinese) == len(spoken) else "",
                        "proof": f"照原片 {where}（{span['start']:.2f}–{span['end']:.2f} 秒）", "tags": "", "evidence": ""})
    return records


def write_frames(video, reference, folder):
    """shot-NN.jpg for every shot: its frames in time order, as the editor looks them up in the handoff's table."""
    folder = Path(folder)
    stills = folder / "frames"
    shutil.rmtree(stills, ignore_errors=True)
    stills.mkdir(parents=True)
    height = TILE_H
    width = round(height * reference["width"] / max(reference["height"], 1) / 2) * 2
    made = _run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", f"fps={FRAME_RATE},scale={width}:{height}", str(stills / "f%04d.jpg"))
    frames = sorted(stills.glob("f*.jpg"))
    if not frames:
        raise PrepareError("没能从原视频抽取画面：" + str(made.stderr or "")[-200:])
    for shot in reference["shots"]:
        start, end = shot["start"], shot["end"]
        count = min(MAX_FRAMES, max(2, round((end - start) / LONG_SHOT) + 1)) if end - start >= LONG_SHOT / 2 else 1
        times = [(start + end) / 2] if count == 1 else [start + .05 + (end - start - .1) * k / (count - 1) for k in range(count)]
        tiles = [(frames[min(len(frames) - 1, int(moment * FRAME_RATE))], f"{moment:.2f}s") for moment in times]
        _sheet(tiles, folder / f"shot-{shot['number']:02d}.jpg", width, height, count)
    shutil.rmtree(stills, ignore_errors=True)
    return folder


def shot_targets(reference, groups, fps, lead_frames, total_frames):
    """Each shot's range on the target timeline, half-open frames from 0; ranges tile the timeline with no gap."""
    per_second = rate(fps)
    lead, total = lead_frames / per_second, total_frames / per_second
    knots = [(0.0, 0.0)]
    for span, group in zip(reference["spans"], groups):
        for original, target in ((span["start"], lead + group["start"]), (span["end"], lead + group["end"])):
            if original > knots[-1][0] and target >= knots[-1][1]:
                knots.append((original, target))
    knots.append((max(reference["duration"], knots[-1][0] + .001), max(total, knots[-1][1])))

    def carried(moment):
        for (left, low), (right, high) in zip(knots, knots[1:]):
            if moment <= right:
                return low + (high - low) * (moment - left) / (right - left)
        return knots[-1][1]

    rows, frame = [], 0
    for position, shot in enumerate(reference["shots"]):
        left = len(reference["shots"]) - position - 1  # Later shots keep a frame each however tight the voiceover is.
        end = total_frames if not left else min(max(frame + 1, round(carried(shot["end"]) * per_second)), total_frames - left)
        rows.append({**shot, "target_start": frame, "target_end": end})
        frame = end
    return rows


def unfinished_gaps(report):
    """Ranges the editor left empty only because it had not looked yet."""
    gaps = (report or {}).get("gaps")
    return [gap for gap in gaps if isinstance(gap, dict) and gap.get("status") == "unverified"] if isinstance(gaps, list) else []


def until_filled(result, rerun, read_report, limit=MAX_CONTINUATIONS):
    """A 1:1 edit may not stop at "still to check": send it back to the same timeline a limited number of times.

    `result` and rerun(prompt)'s return are (status, output, error, actual); the last run's reply is the delivery."""
    for _ in range(limit):
        gaps = unfinished_gaps(read_report(result[1])) if result[0] == "completed" else []
        if not gaps:
            break
        ranges = "；".join(f"[{gap.get('start')}, {gap.get('end')})" for gap in gaps)
        result = rerun("上一轮交付不完整，这条 1:1 复刻还没剪完。目标时间线上已放的镜头保留，先确认当前项目和目标时间线（按 UniqueId），"
                       f"再按当前目录的 handoff.md 和 planning.md 补满这些空段（帧，半开）：{ranges}。"
                       "每个空段都放标签最接近的片段并看帧定好入出点，不能再报 status=unverified；差异写在正文，不写进 questions。"
                       "剪完保存工程，最终回复按 handoff.md 的格式附完整 json（统计整条时间线）。")
    return result


def handoff_section(job, take, timeline, fps, lead_frames, folder="reference"):
    """The part of the handoff that makes the edit a copy of the original instead of a fresh selection."""
    reference = job["replica"]
    rows = shot_targets(reference, take["groups"], fps, lead_frames, timeline["frames"])
    per_second = rate(fps)
    table = ["| 镜头 | 原片（秒） | 对应口播行 | 目标区间（帧，半开） | 目标时长（秒） | 参考图 |", "|---|---|---|---|---|---|"]
    for row in rows:
        table.append(f"| {row['number']} | {row['start']:.2f}–{row['end']:.2f} | {'、'.join(map(str, row['lines'])) or '无口播'} | "
                     f"[{row['target_start']}, {row['target_end']}) | {(row['target_end'] - row['target_start']) / per_second:.2f} | "
                     f"{folder}/shot-{row['number']:02d}.jpg |")
    return "\n".join([
        "## 原片镜头表（1:1 复刻，本次的主要依据）", "",
        f"- 原片：案例 {reference['case_id']}「{reference['case_title']}」，{reference['duration']:.1f} 秒，{len(rows)} 个镜头。"
        + {"aligned": "口播是逐句对应原片的优化稿（第 n 行对应原片第 n 句，措辞改过），",
           "changed": "口播和原片不是逐句相同，每句对应的原片时间是按字数估算的，"}.get(reference.get("wording"), "口播已和原片逐句相同，")
        + "配音是新录的，所以目标区间是把原片切点按实际配音换算过来的。",
        "- 分两遍做。第一遍铺满：读参考图（图内按时间顺序排列，左上角是原片秒数），按 planning.md 的首选和已核标签，"
        "把表里每个镜头都放上片段，已核标签对得上就直接放，不用先把候选看完。第二遍替换：查看对照图，把不够贴合的镜头换成更贴合的。"
        "镜头顺序和切点都照表，不合并、不调换、不跳过。",
        "- 本次不使用 status=unverified，也不能以「还没核实、还没比较」为由留空或提前交付：没看完的镜头同样先放标签最接近的片段，交付前看帧核对入出点，不把「未看帧核对」留给用户。"
        "这条优先于下面交付里关于待核实空段的说法。status=missing 只用于相关候选都看过、确实没有相关画面的镜头。",
        "- 判断同一画面按这个优先级：同动作同部件同工序阶段 > 动作方向和结果 > 景别和机位角度 > 手、刀型和背景。"
        "原片里的动作从哪开始、到哪结束，入出点就取到哪。素材片段比目标区间短时，用同一次操作的相邻片段接满，再不够才变速，不留空。",
        "- 本表的镜头时长和切点优先于技能的分类时长上限、开头规则、信息重复检查和素材防复用：最贴合原片的片段即使用过也用，"
        "只有同样贴合时才看使用次数。动作和工序必须真实，不用其他阶段冒充。",
        "- 参考图上烧录的字幕和贴纸是原片后期，不用找，也不要加；产品在原片里和本次产品不同时，用本次产品的同一动作。",
        "- 找不到完全相同的画面时不留空：依次放同动作不同角度、同工序不同动作的片段。差异只写在最终回复正文的逐镜头对比里，"
        "写法「镜头 N：原片是什么，这次用了什么，差在哪」，不写进 questions。实在没有相关画面才按交付要求报 gaps。",
        '- 最后的 json 多加一项统计每个镜头的贴合程度，四个数加起来等于镜头数：'
        '"replica": {"exact": 0, "close": 0, "substitute": 0, "missing": 0}（完全对上 / 只差角度或景别 / 换了动作 / 留空）。', "",
        *table, ""])
