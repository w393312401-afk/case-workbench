"""Footage and brief for the retrospective draft of a posted TikTok product video.

The workbench prepares the footage in one pass (frames every 0.25 s for the opening, every shot after it, a small.en
transcript with timestamps, each script line's time) so the model only judges.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import threading
from pathlib import Path

FPS = 4
# Claude scales any image past ~1568 px down, so a sheet stays under that or its tiles lose detail. Shot tiles are
# 360 px wide (8 per sheet); the opening also goes out as full frames, since a mirror bevel is a few pixels wide
# in a small tile and disappears.
TILE_W, TILE_H, COLS, ROWS = 360, 640, 4, 2
GRID_W, GRID_H, GRID_COLS = 220, 391, 7
OPENING_FRAMES = 13  # 0.00–3.00 s every 0.25 s, for the sequence
DETAIL_TIMES = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)  # full frames, for detail
DETAIL_UNTIL = 8.0  # after 3 s, one full frame per shot that starts before this (an opening often runs past 3 s)
LONG_SHOT = 1.5
TRANSCRIPT_CHUNK = 2.5
# Reading thin details (a mirror bevel, small text) needs the strongest vision model.
DRAFT_MODEL = "claude-opus"
FONT = "fontfile='C\\:/Windows/Fonts/arial.ttf'"
_model_lock = threading.Lock()
_model = None


MATERIAL_RULES = (
    "只用本轮给出的材料：附图、镜头清单、口播、评论摘要和数据。材料里没有的事实（价格、质保、参数、对比结果）不要写成已知，"
    "需要时写「待核」；不要读取或引用其他文件和案例。你只看得到静帧，看不到连续动作，也听不到声音；"
    "音乐、读音、语气相关的风险写「待人工听审」。\n"
    "看不清不等于没有：凡是说画面里「没有」某样东西，或判断刃口、反光、小字这类细节，都要写明依据第几张图、第几秒，以及看得清不清楚。"
    "看不清就写「待核」，不能写成否定结论，也不能拿它推出原因或建议。镜头清单里标「已比对一致」的镜头，画面内容以清单里人工核过的素材标签为准。"
)


class PrepareError(RuntimeError):
    pass


def _run(*args, timeout=600, text=True):
    # The desktop service has no console; each media child must also start without one.
    return subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=text,
                          encoding="utf-8" if text else None, errors="replace" if text else None, timeout=timeout,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def words(text):
    return re.findall(r"[a-z0-9]+", str(text).lower().replace("’", "").replace("'", ""))


def _whisper():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=16)
    return _model


def _sheet(tiles, path, width=TILE_W, height=TILE_H, cols=COLS):
    inputs, labels = [], []
    for index, (frame, label) in enumerate(tiles):
        inputs += ["-i", str(frame)]
        labels.append(f"[{index}:v]scale={width}:{height},drawtext={FONT}:text='{label}':x=4:y=4:fontsize=20:"
                      f"fontcolor=yellow:box=1:boxcolor=black@0.7[v{index}]")
    if not tiles:
        raise PrepareError("拼图没有画面。")
    layout = "|".join(f"{(i % cols) * width}_{(i // cols) * height}" for i in range(len(tiles)))
    # xstack needs two inputs at least; a page holding one leftover tile is that tile alone.
    stack = (";" + "".join(f"[v{i}]" for i in range(len(tiles))) + f"xstack=inputs={len(tiles)}:layout={layout}:fill=black[out]"
             if len(tiles) > 1 else "")
    result = _run("ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(labels) + stack,
                  "-map", "[out]" if len(tiles) > 1 else "[v0]", "-frames:v", "1", "-q:v", "4", str(path))
    if result.returncode or not path.is_file():
        raise PrepareError("拼图生成失败：" + (result.stderr or "")[-300:])


def _check(cancel):
    if cancel is not None and cancel.is_set():
        raise PrepareError("本次起草已取消。")


def prepare(video, spoken, folder, cancel=None, on_step=None):
    """Frames, sheets and a timed transcript for one video; returns the report the draft works from."""
    step = on_step or (lambda text: None)
    folder = Path(folder)
    frames = folder / "frames"
    frames.mkdir(parents=True, exist_ok=True)
    probe = _run("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video), timeout=60)
    try:
        duration = float(probe.stdout.strip())
    except ValueError as exc:
        raise PrepareError("读不出成片时长，请确认文件是导出完成的视频。") from exc

    step("正在抽取画面")
    _run("ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", f"fps={FPS},scale={TILE_W}:-2", str(frames / "f%04d.jpg"))
    stills = sorted(frames.glob("f*.jpg"))
    if not stills:
        raise PrepareError("没能从成片抽取画面（需要本机 ffmpeg）。")
    at = lambda t: stills[min(len(stills) - 1, max(0, round(t * FPS)))]
    info = _run("ffmpeg", "-v", "info", "-i", str(video), "-vf", "select='gt(scene,0.25)',showinfo", "-f", "null", "-").stderr
    cuts = sorted({round(float(m), 3) for m in re.findall(r"pts_time:([\d.]+)", info)})
    bounds = [0.0] + [c for c in cuts if 0.15 < c < duration - 0.15] + [duration]
    shots = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.05]
    _check(cancel)

    step("正在转写口播")
    with _model_lock:
        segments, _ = _whisper().transcribe(str(video), word_timestamps=True, language="en", beam_size=1)
        heard = [w for segment in segments for w in (segment.words or [])]
    _check(cancel)
    heard_tokens, heard_times = [], []
    for item in heard:
        for token in words(item.word):
            heard_tokens.append(token)
            heard_times.append((item.start, item.end))
    # Each script line's time comes from matching its words in the transcript; without a script there is none.
    line_of = [n for n, line in enumerate(spoken) for _ in words(line)]
    script_tokens = words(" ".join(spoken))
    span = {}
    opcodes = difflib.SequenceMatcher(a=script_tokens, b=heard_tokens, autojunk=False).get_opcodes() if script_tokens else []
    for op, i1, i2, j1, j2 in opcodes:
        if op == "equal":
            for k in range(i2 - i1):
                n, (start, end) = line_of[i1 + k], heard_times[j1 + k]
                low, high = span.get(n, (start, end))
                span[n] = (min(low, start), max(high, end))
    chunks, current = [], None
    for item in heard:
        if current is None or item.start - current[0] >= TRANSCRIPT_CHUNK:
            current = [item.start, []]
            chunks.append(current)
        current[1].append(item.word.strip())
    transcript = "\n".join(f"[{start:.1f} 秒] {' '.join(text)}" for start, text in chunks)[:6000]

    def line_at(t):
        return next((n + 1 for n, (s, e) in sorted(span.items()) if s - 0.3 <= t <= e + 0.3), "-")

    step("正在拼图")
    sheets = []
    _sheet([(at(k / FPS), f"{k / FPS:.2f}s") for k in range(OPENING_FRAMES)], folder / "opening.jpg", GRID_W, GRID_H, GRID_COLS)
    sheets.append({"file": "opening.jpg", "label": "开头 0–3 秒，每 0.25 秒一帧（看顺序）"})
    moments = list(DETAIL_TIMES) + [round((a + b) / 2, 2) for a, b in shots if DETAIL_TIMES[-1] < (a + b) / 2 and a < DETAIL_UNTIL]
    for number, moment in enumerate(moments, 1):
        name = f"detail{number:02d}.jpg"
        _run("ffmpeg", "-y", "-v", "error", "-ss", f"{min(moment, max(duration - 0.1, 0)):.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(folder / name))
        if (folder / name).is_file():
            sheets.append({"file": name, "label": f"{moment:.1f} 秒原画（看刀刃、反光、字幕等细节）"})
    tiles, shot_rows = [], []
    for n, (a, b) in enumerate(shots, 1):
        picks = [a + 0.1, (a + b) / 2, b - 0.1] if b - a >= LONG_SHOT else [(a + b) / 2]
        tiles += [(at(t), f"L{line_at(t)} #{n} {t:.1f}" if span else f"#{n} {t:.1f}s") for t in picks]
        shot_rows.append({"shot": n, "start": round(a, 2), "end": round(b, 2), "frame": at((a + b) / 2).name})
    per_sheet = COLS * ROWS
    for k in range(0, len(tiles), per_sheet):
        name = f"shots{k // per_sheet + 1}.jpg"
        _sheet(tiles[k:k + per_sheet], folder / name)
        first, last = tiles[k][1], tiles[min(len(tiles), k + per_sheet) - 1][1]
        sheets.append({"file": name, "label": f"镜头拼图 {first.split()[-1].rstrip('s')}–{last.split()[-1].rstrip('s')} 秒"})
    return {"duration": round(duration, 2), "shots": shot_rows, "sheets": sheets, "transcript": transcript,
            "lines": [{"line": n + 1, "start": round(span[n][0], 2) if n in span else None,
                       "end": round(span[n][1], 2) if n in span else None} for n in range(len(spoken))]}


# Same footage measured 0.92–1.0 on real exports, replaced shots mostly below 0.5; 0.69–0.76 stayed ambiguous,
# so only a clear match lets a label stand.
SHOT_MATCH = 0.85


def _seconds(value):
    return f"{value:.1f}"


def label_shots(video, timeline, candidates, fps, cancel=None):
    """What the editor actually placed: each visible clip of the delivered timeline, the human-checked label of the
    material it came from, and whether the exported video still shows that footage at that second.

    A human polish after delivery may have replaced shots, so each clip is compared with the exported frame at its
    middle; only a match lets its label stand. Returns rows in output order; [] when nothing can be matched."""
    import cv2
    import numpy
    clips = []
    for track in sorted(timeline.get("tracks", {}).get("video", []), key=lambda row: row.get("index", 0)):
        if track.get("enabled") is False:
            continue
        for item in track.get("items", []):
            if item.get("enabled") is False or not item.get("media_path"):
                continue
            clips.append({**item, "track": track.get("index", 0)})
    # A higher track covers a lower one; keep each clip's visible middle only when no higher clip sits over it.
    visible = [clip for clip in clips if not any(other["track"] > clip["track"] and other["start"] <= (clip["start"] + clip["end"]) / 2 < other["end"]
                                                 for other in clips)]
    visible.sort(key=lambda clip: clip["start"])

    def vector(path, seconds):
        done = _run("ffmpeg", "-v", "error", "-ss", f"{max(seconds, 0):.3f}", "-i", str(path), "-frames:v", "1",
                    "-vf", "scale=72:128,format=gray", "-f", "rawvideo", "-", timeout=60, text=False)
        if done.returncode or len(done.stdout) != 72 * 128:
            return None
        value = numpy.frombuffer(done.stdout, dtype=numpy.uint8).astype(numpy.float32)
        value -= value.mean()
        norm = float(numpy.sqrt((value * value).sum()))
        return value / norm if norm else None

    rows = []
    for clip in visible:
        _check(cancel)
        start, end = clip["start"] / fps, clip["end"] / fps
        middle = (start + end) / 2
        source_seconds = (clip.get("source_start_time") or 0) + (middle - start) * ((clip.get("speed") or {}).get("Percentage", 100) / 100)
        best = max((row for row in candidates if row.get("media_id") == clip.get("media_id") and row.get("source")),
                   key=lambda row: min(row["source"][1], clip["source_end"]) - max(row["source"][0], clip["source_start"]), default=None)
        if best is not None and min(best["source"][1], clip["source_end"]) - max(best["source"][0], clip["source_start"]) <= 0:
            best = None
        exported, source = vector(video, middle), vector(clip["media_path"], source_seconds) if Path(clip["media_path"]).is_file() else None
        similarity = float((exported * source).sum()) if exported is not None and source is not None else None
        rows.append({"start": round(start, 2), "end": round(end, 2), "material": clip.get("name") or Path(clip["media_path"]).name,
                     "label": (best or {}).get("label") or "", "label_status": (best or {}).get("label_status") or "",
                     "description": (best or {}).get("description") or "",
                     "similarity": round(similarity, 3) if similarity is not None else None,
                     "match": None if similarity is None else similarity >= SHOT_MATCH})
    return rows


def _shot_list(rows):
    if not rows:
        return ""
    matched = sum(1 for row in rows if row["match"])
    lines = []
    for row in rows:
        what = row["label"] or row["description"] or f"素材 {row['material']}（没有标签）"
        status = {True: "已比对一致", False: "成片画面与剪辑记录不一致，这段以附图为准", None: "没能比对，以附图为准"}[row["match"]]
        checked = "人工核过的标签" if row["label_status"] == "verified" else "标签未人工核" if row["label"] else ""
        lines.append(f"- {_seconds(row['start'])}–{_seconds(row['end'])} 秒：{what}" + (f"（{checked}）" if checked else "") + f" · {status}")
    return (f"\n\n镜头清单（来自这条的剪辑记录和素材标签，已逐镜和导出成片比对，{matched}/{len(rows)} 个镜头一致；"
            "人工精修后换过的镜头会标不一致）：\n" + "\n".join(lines))


def _materials(lines, report, review_text):
    """What the draft works from: the attached sheets, the spoken words and the comment digest.
    `report` is None when no footage could be had: then only the script (if any) is given."""
    if report is None:
        report = {"lines": [{"start": None, "end": None} for _ in lines]}
    def when(timing):
        return f"{timing['start']:.1f}–{timing['end']:.1f} 秒" if timing["start"] is not None else "时间未对齐"

    if lines:
        spoken = "口播原稿逐句（时间从转写对齐）：\n" + "\n".join(
            f"L{row['number']}（{when(timing)}）{row['spoken']}" for row, timing in zip(lines, report["lines"]))
    else:
        spoken = "口播转写（自动转写，方括号是开始秒数）：\n" + (report.get("transcript") or "（没有转写出口播）")
    images = (f"附图：第 1 张是开头 0–3 秒每 0.25 秒一帧，看顺序；接着几张是开场原画（0–3 秒每 0.5 秒一张，之后到 8 秒每个镜头一张），看刀刃、刃口反光、字幕这类细节；"
              f"其余是全片每个镜头的抽帧，长镜头给首、中、尾三帧，"
              f"每格左上角写着镜头号和秒数{'，L 后面是那一刻在说的第几句口播' if lines else ''}。"
              f"全片 {report['duration']} 秒，{len(report['shots'])} 个镜头。") if "shots" in report else (
              "没有附图：这次没能取得成片，你没有看过画面，开头、画面和侵权相关的判断一律写「没看到成片，判断不了」。")
    shots = _shot_list((report or {}).get("shot_labels") or [])
    comments = ("\n\n评论区参考（本产品评论库摘要，评论者说的话未核实，只用来判断评论区最可能出现的质疑）：\n" + review_text) if review_text else ""
    return images + shots + "\n\n" + spoken + comments


LOW_EXPOSURE = 500  # observation-window views below this leave every rate too noisy to explain a result
DRAFT_MODES = {"sold": "出过单", "unsold": "没出单", "thin": "曝光太少"}

# The only judging rules of a retrospective (2026-11-05, replacing the adversarial framework and the S/A/B chain).
# Shared with the text-only retrospective prompt in ai_tasks; the skill's copy is product-video-case-analysis/references/retro-rules.md.
RETRO_RULES = (
    "复盘只看下面五件事，每个判断都落到具体秒数，分清画面和口播里实际有的与你的推测：\n"
    "1. 类型：唤醒型还是选择型。唤醒型＝对还没打算买、没意识到问题的人，开头先给「为什么需要」（例：以为刀钝是正常的）；"
    "选择型＝对正在挑同类产品的人，开头给「为什么选这个」（例：凭手感磨刀在赌角度，这个能锁住）。以开头和口播实际对谁说为准，不照抄方向登记。\n"
    "2. 停留理由和购买理由是不是同一件事。停留理由＝开头让人停下来的那件事；购买理由＝后面让人掏钱的那件事。"
    "两者是同一件事才算对上；开头靠反常、奇观、技巧或情绪留人，但和后面的购买理由无关，算对不上，要指出来。\n"
    "3. 开头节奏：痛点或身份是不是一句话说完，产品和证明画面是不是在 2–3 秒内出现。写出产品第一次入画、证明画面第一次出现的秒数；铺垫过长要指出。\n"
    "4. 证明画面：购买理由有没有一个看得见、观众照着做也能复现的证明（同一物品前后对比、推到极端的效果）。"
    "没有或不够有力时，给一个下一条要专门拍的证明画面，只写真实能拍出来的。\n"
    "5. 中段与收口：卖点有没有配画面证明，结尾有没有购买入口和已核实的下单理由；唤醒型这一段更不能省。\n"
    "读数据：播放和完播对应停留理由，点击率（CTR）、点击后转化（CTOR）和订单对应购买理由；单/万播放＝订单 ÷ 播放 × 10000，用来和同类比较。"
    "先和同类对照比，判断每一步强、弱还是持平，再回到画面和口播找原因。数据只说明相关，不能当作已证实的因果；样本小、口径不同或数据不够时写「判断不了」。"
    "静帧看不清或无法判断连续动作、效果时写待核；没有成片时不得编造画面。\n\n")
CARD_REASONS = 3
_CARD = ("在两行结尾之前，另交一个工作台画卡片用的结果块，用 ```retro-card 包住，里面是 JSON，所有文字都是短句中文：\n"
         '```retro-card\n{"verdict": "一句话结论，不超过 40 字", '
         '"reasons": [{"at": "秒数范围或「全片」", "point": "原因，不超过 24 字", "fix": "怎么做或怎么改，不超过 30 字"}], '
         '"keep": ["值得保留的做法，不超过 24 字"], '
         '"next": {"change": "下一条只改什么", "hold": "固定不动的", "judge": "看哪个数判输赢"}}\n```\n'
         f"reasons 最多 {CARD_REASONS} 条、按影响排序；keep 最多 3 条；next 每项不超过 30 字。"
         "卡片只是正文的摘要，不要写正文里没有的判断；漏斗数字由工作台自己画，卡片里不用重复。\n")
# The lines the workbench fills the review form with; shared with the text-only retrospective prompt in ai_tasks.
DRAFT_ENDING = ("全文最后单独写两行，工作台据此填表：\n"
                "结论：类型：……；停留理由：……；购买理由：……；对上：……\n"
                "结论这一行只写四件事，从上面的完整分析得出：类型写「唤醒型」或「选择型」，判断不了写「待核」；"
                "停留理由写开头让人停下来的那件事，不超过 20 字，括号里写秒数；购买理由写后面让人掏钱的那件事，不超过 20 字，"
                "能对上已登记卖点的在前面写编号；对上写「是」「否」或「待核」。不写评价、数字和建议。"
                "例：结论：类型：选择型；停留理由：凭手感磨刀在赌角度（0–4 秒）；购买理由：S1 锁住角度可重复；对上：是\n"
                "参考价值：高、中等、弱、无四选一，括号里用不超过 20 字说依据\n"
                "参考价值的选法：高＝高流量、高销量，值得复用；中等＝高流量但个位数销量；弱＝流量不高、个位数销量；"
                "无＝没流量、没成交，也没有可借鉴的发现。流量高低和同类对照比，不按固定数字划档；失败案例有可借鉴的发现时不选无。"
                "这只是推荐，由用户确认。closing_allowed 为 false（未满14天或数据不全）时照样写这两行。")
_CLOSING = ("根据冻结的发布后14天观察数据判断行动边界。" + DRAFT_ENDING +
            "只有 closing_allowed 为 true 且需要明确安排后续时才另写一行：\n建议动作：加码、换执行、停测三选一\n"
            "动作可省略；用户确认结论、选择参考价值即可保存复盘，不需要选择下一轮动作。"
            "未满14天、数据覆盖不全或没有冻结观察状态时，结论写成阶段判断并说明待补数据，不输出建议动作行。系统自动管理观察期，无需安排人工观察操作。"
            "说明依据与局限，不要求用户另填表现或证据等级。加码＝值得复制验证的候选，不代表方向已验证；"
            "换执行＝保留待验证假设，改动具体执行；停测＝结束本条复盘，不要求下一轮方向，不创建下一轮计划，不能从单条结果认定整个方向不成立；"
            "建议动作要和数据一致。")

# A video with no orders and too little exposure (or no figures yet): the conclusion line adds where it stuck and one
# suspicion to test, and the reference value follows from that. Shared with the text-only prompt in ai_tasks.
THIN_ENDING = ("\n这条没有销量、曝光也不足以判断，结论那一行在类型、停留理由、购买理由、对上之后再接两段，仍写成一行：\n"
               "结论：类型：……；停留理由：……；购买理由：……；对上：……；卡点：……；待验证：……\n"
               "卡点二选一：「数据未到」＝还没有销售数据，或未满14天、首14天数据不全；「曝光不足」＝数据已覆盖，但观察窗播放不到 "
               f"{LOW_EXPOSURE}。后面括号里写事实：观察窗播放、订单、是否满14天，例如「曝光不足（播放 312 · 0 单 · 已满14天）」。"
               "待验证只写一条最可能让它没跑起来的内容问题，落到秒数，明确是推测，例如「开头 0–3 秒没出现刀和人群词」；"
               "没有把握就写「无」，不硬凑原因，也不把低曝光直接归因于内容。\n"
               "参考价值按下面的规则推荐，优先于上面的选法：卡点是数据未到时写「参考价值：待定（数据未到）」；"
               "曝光不足且待验证是「无」时推荐无；曝光不足但待验证里有能用到下一条的具体发现时推荐弱。\n")


def draft_mode(orders, window_views):
    """Which brief a posted video gets: why it sold, where it lost people, or too little exposure to say."""
    if isinstance(orders, (int, float)) and orders > 0:
        return "sold"
    if not isinstance(window_views, (int, float)) or window_views < LOW_EXPOSURE:
        return "thin"
    return "unsold"


def _points_material(points, audience):
    """The product's registered selling points and the direction's audience, for naming the purchase reason."""
    rows = [f"{row['code']} {row['title']}" for row in points or [] if row.get("code") and row.get("title")]
    if not rows:
        return "本产品还没有登记卖点，购买理由用画面和口播里的说法概括，不写编号。\n\n"
    return ("本产品已登记的卖点（购买理由对得上的写编号）：\n" + "\n".join(rows) + "\n"
            + (f"方向登记的目标人群（只作对照）：{audience}\n" if audience else "") + "\n")


def draft_prompt(lines, report, *, source, review_text, figures, mode="sold", points=None, audience=""):
    """Retrospective draft of a posted video, read from the footage and the figures.

    A video that sold is explained (why it sold, what to copy). One that did not is attacked with the review framework,
    but only at the funnel step the data shows it lost people, and only with suspicions the data does not contradict.
    Too little exposure gets the same attack with the plain warning that most rates cannot be judged."""
    material = (MATERIAL_RULES + "\n\n" + f"成片来源：{source}。" + _materials(lines, report, review_text) + "\n\n"
                "表现数据（比率按每周导出原值读，不要相加或平均；同类对照是每条视频播放最多那一周的中位数）：\n"
                + json.dumps(figures, ensure_ascii=False, indent=1) + "\n\n" + _points_material(points, audience) + RETRO_RULES)
    if mode == "sold":
        return (
            "你在复盘一条已经发布、已经出过单的 TikTok 带货视频。结果已经由数据给出，任务是解释这个结果：它为什么出单，"
            "哪些做法值得下一条复制。不要为了挑毛病而挑毛病，也不要只夸；每个判断都要落到画面或口播的具体秒数，并对上漏斗里对应那一步的数据。\n\n"
            + material +
            "按这个结构用中文 Markdown 交付：\n"
            "## 一句话：这条为什么出单\n"
            "## 1. 类型：唤醒型还是选择型\n"
            "## 2. 停留理由和购买理由（各是什么、在第几秒、是不是同一件事，对上漏斗里对应那一步的数据）\n"
            "## 3. 开头节奏（痛点或身份几句说完，产品和证明画面第几秒出现）\n"
            "## 4. 证明画面（有没有、够不够有力；下一条要专门拍的证明画面）\n"
            "## 5. 中段与收口（卖点有没有配证明，购买入口和下单理由在不在）\n"
            "## 下一轮（怎么复制这条；如果要测，只改一处做 A/B）\n"
            "卡片的 reasons 写「为什么出单」最关键的几点。\n" + _CARD + _CLOSING
        )
    thin = mode == "thin"
    return (
        "你在复盘一条已经发布、没有出单的 TikTok 带货视频。"
        + (f"它的观察窗播放很少（不到 {LOW_EXPOSURE}），各项比率都不稳定：能判断的写出来，判断不了的如实写判断不了，不要硬找原因。"
           if thin else "")
        + "先用数据找出它最先在哪一步丢了人，再按下面五件事检查那一步的画面和口播。"
        "每个疑点都要拿数据检验：数据支持或判断不了的才写，数据不支持的（那一步并不比同类差）不要写。"
        "也要写出哪一步不比同类差，那里的做法值得保留。每个判断都落到具体秒数。\n\n"
        + material +
        "按这个结构用中文 Markdown 交付：\n"
        "## 一句话：这条卡在哪一步\n"
        "## 1. 漏斗对照（播放与完播、点击率、点击后转化，逐项和同类对照比，标出最先掉下去的一步）\n"
        "## 2. 类型：唤醒型还是选择型\n"
        "## 3. 停留理由和购买理由（各是什么、在第几秒、是不是同一件事）\n"
        "## 4. 开头节奏与证明画面（产品和证明画面第几秒出现；下一条要专门拍的证明画面）\n"
        "## 值得保留的（不比同类差的那几步，保留什么）\n"
        "## 下一轮（只改最先掉下去的那一步，做 A/B）\n"
        "卡片的 reasons 写最先掉下去那一步的原因。\n" + _CARD + _CLOSING
        + ("播放太少时，明确样本有限；已完成观察且数据覆盖齐全的，可以结束本条，但不能把低曝光直接归因于内容失败。" + THIN_ENDING if thin else "")
    )


def _clip(value, limit):
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def parse_card(output):
    """The draft's ```retro-card block, trimmed to the fields and lengths the card shows; None when absent or malformed."""
    blocks = re.findall(r"```retro-card\s*(\{.*?\})\s*```", output or "", re.S)
    try:
        data = json.loads(blocks[-1]) if blocks else None
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    reasons = [{"at": _clip(row.get("at"), 30), "point": _clip(row.get("point"), 60), "fix": _clip(row.get("fix"), 80)}
               for row in data.get("reasons") or [] if isinstance(row, dict) and _clip(row.get("point"), 60)][:CARD_REASONS]
    keep = [_clip(item, 60) for item in data.get("keep") or [] if isinstance(item, str) and _clip(item, 60)][:3]
    plan = data.get("next") if isinstance(data.get("next"), dict) else {}
    plan = {key: _clip(plan.get(key), 80) for key in ("change", "hold", "judge") if _clip(plan.get(key), 80)}
    card = {"verdict": _clip(data.get("verdict"), 120), "reasons": reasons, "keep": keep, "next": plan}
    return card if card["verdict"] or reasons else None
