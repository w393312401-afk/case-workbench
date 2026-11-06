"""挂车图文 for one finished timeline: no-subtitle video → frames → AI writes 3 per-slide versions → pick one →
three type styles → per-slide picture swaps (another frame or an uploaded photo, crop and text position; a slide can
also be a 2- or 4-cell collage, each cell with its own picture and crop) → freeze.

Only the copy is written by AI. Pictures are real frames or the user's own photos, laid out by carousel_render;
swapping a picture never calls AI. Data lives in <runtime>/production/photo-sets/<PHS-id>/set.json; a frozen
version is copied to <project>/图文成品/<case>-<date>-<version>/ and never changed afterwards.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat

import carousel_render as render
from resolve_bridge import render_recovery_message

FPS = 5
THUMB = (180, 320)
SHEET_TILE = (216, 384)
SHEET_GRID = (8, 3)
MAX_SHEETS = 8
MAX_SLIDES = 8
MAX_HISTORY = 60
SET_ID = re.compile(r"PHS-[0-9a-f]{12}")
APP_ROOT = Path(__file__).resolve().parent
EXPORT_ROOT = APP_ROOT / "图文成品"
SKILL = "tiktok-photo-commerce"
# Each of the 3 versions is written one way (2026-11-06 user decision, replacing "big title + fact label").
# The photo skill states the same rules; both prompts read them from here.
WRITINGS = {"self": "达人自述", "qa": "顾虑问答", "list": "理由清单"}
WRITING_ORDER = ("self", "qa", "list")
WRITING_RULES = {
    "self": ("达人自述：第一人称分享自己的发现和经历。标题给结论（I found… / Now I… / I don't… anymore），第二行用让步转折讲原因"
             "（老办法能做到什么，但做不到我想要的），如“I FOUND THE *NEXT LEVEL* FOR MY STONE. / One stone gets it sharp, just not "
             "the finish I'm after.”。只说视频或资料里有的经历，不新增亲历、数字和效果；视频口播不是第一人称时，gaps_zh 写“待核：出镜人需有此经历”。"),
    "qa": ("顾虑问答：每张标题是买家买之前会问的一个问题，用观众自己的话（如“ALREADY OWN A *WHETSTONE?*”“WHAT IF ONE *WEARS OUT?*”），"
           "第二行用口语回答，落到观众得到什么（如“Nope. All 7 plates come in one case.”）。问题按顾虑链排；首张的问题圈出目标人群、"
           "说中他们的处境，不把产品摆到主打卖点的反面。"),
    "list": ("理由清单：首张许诺“N 个理由”（N 等于后面的理由张数，如“*3 REASONS* I MOVED / PAST MY WHETSTONE”），之后每张一个编号理由"
             "（标题以“1.”“2.”开头），第二行讲这个理由给观众带来什么；最后一个理由落到下单理由。"),
}
SECOND_LINE_RULE = ("第二行是一句口语（约 6–14 个英文词，排版最多两行），接着标题往下说：给原因、回答或结果。能带具体数字（件数、目数、年限、范围）"
                    "就放进这句里说，信息量靠它；不写成规格标签，不描述画面拍到了什么（“… shown”“with the knife clamped behind it”“pictured” 都不算）。")
# 2026-11-06 user: closings read like a contract ("3-year limited warranty, covering materials and workmanship",
# "buy the full 202H at today's price"). Product-file wording and rule wording are for checking, not for the copy.
# 2026-11-06 user: any B-tier reason that fits the copy can close (4-year warranty, 30-day returns, carry-case), not only 3-year warranty/price.
CLOSING_REASON_RULE = ("理由从产品卖点分级表 B 类里选和这一版文案最搭的一项，回答观众下单前最后一个顾虑：质保（出问题怎么办）、"
                       "退换（买了不合适怎么办）、价格（值不值）、整箱带走（前文讲帮朋友磨刀、带去别处用时）等；3 版尽量各用不同的理由。"
                       "只说“有收纳箱、硬壳”不算理由；整套要说出观众因此避开什么（不用一件件凑、不会买错配件）。")
CLOSING_VOICE_RULE = ("下单理由和入口用达人顺口说话的口吻，不念产品资料的条款原文，也不把规则里的中文要求翻进文案："
                      "质保按资料能说到的最长年限，202H 注册后 4 年，如“It even comes with a 4-year warranty once you register it.”，"
                      "不写 limited、covering materials and workmanship 这类条款词，不说终身、无条件；"
                      "退换说“You've got 30 days to send it back.”，挂车页没写免费就不说 free；"
                      "入口短，如“Check out the full 202H set below.”（下方看看整套 202H）；没给今天挂车价就不提价格，"
                      "不写 at today's price、today's product page；不把理由、价格、整套、品牌全名塞进同一句。")


class PhotoSetError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _hidden_window():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def sharpness(path):
    """Edge variance on a half-size grey copy; bigger is sharper. Motion blur gives low values."""
    with Image.open(path) as image:
        grey = image.convert("L").resize((image.width // 2, image.height // 2))
    return round(ImageStat.Stat(grey.filter(ImageFilter.FIND_EDGES)).var[0], 1)


def clean_lines(values, limit, length=160):
    lines = [re.sub(r"\s+", " ", str(value or "")).strip() for value in (values or [])]
    return [line[:length] for line in lines if line][:limit]


def _number(value, default, low=0.0, high=None):
    try:
        number = max(low, float(value))
    except (TypeError, ValueError):
        return default
    return min(high, number) if high is not None else number


WORD_KEYS = ("title_lines", "body_lines", "title_zh", "body_zh")


def slide_id(slide):
    """A stable id so AI wordings land on the right slide after copying, deleting or reordering."""
    return slide.setdefault("id", uuid4().hex[:10])


def cell_count(slide):
    return len(render.LAYOUTS[render.layout_of(slide)][1])


def fill_cells(slide):
    """Make sure a collage slide has a picture for every cell after the first; new cells start as a copy of cell 1.
    Cells beyond the current layout are kept, so switching back to four cells brings their pictures back."""
    cells = slide.setdefault("cells", [])
    while len(cells) < cell_count(slide) - 1:
        cells.append({"image": copy.deepcopy(slide.get("image") or {}), "crop_x": 0.5, "crop_y": 0.5})
    return slide


def slide_cells(slide):
    """Every cell of the slide's layout as {image, crop_x, crop_y}; cell 1 is the slide's own picture."""
    first = {"image": slide.get("image") or {}, "crop_x": slide.get("crop_x", 0.5), "crop_y": slide.get("crop_y", 0.5)}
    return [first] + list(fill_cells(copy.deepcopy(slide))["cells"])[:cell_count(slide) - 1]


def _json_answer(output, problem):
    """Pull the JSON object out of the model's answer; tolerant of code fences and prose around it."""
    text = str(output or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PhotoSetError(502, problem) from exc
    if not isinstance(data, dict):
        raise PhotoSetError(502, problem)
    return data


def parse_slide(output, cta=""):
    """One rewritten slide: title (required), fact lines (never the closing line), Chinese, optional text position."""
    data = _json_answer(output, "AI 没按约定格式交回这一张，可以再点一次。")
    title = clean_lines(data.get("title_lines"), 3, 60)
    if not title:
        raise PhotoSetError(502, "AI 交回的这一张没有标题，可以再点一次。")
    out = {"title_lines": title,
           "body_lines": [render.plain(line) for line in clean_lines(data.get("body_lines"), 2) if render.plain(line) != render.plain(cta)],
           "title_zh": str(data.get("title_zh") or "")[:200], "body_zh": str(data.get("body_zh") or "")[:200]}
    if data.get("text_position") in render.POSITIONS:
        out["text_position"] = data["text_position"]
    return out, str(data.get("change_zh") or "")[:200]


def parse_options(output, cta=""):
    """Up to three wordings for one slide; an answer that is a single slide still counts as one option."""
    data = _json_answer(output, "AI 没按约定格式交回说法，可以再点一次。")
    items = data.get("options") if isinstance(data.get("options"), list) else [data]
    options = []
    for item in items[:3]:
        try:
            words, change = parse_slide(json.dumps(item, ensure_ascii=False), cta)
        except PhotoSetError:
            continue
        if any(o["title_lines"] == words["title_lines"] and o["body_lines"] == words["body_lines"] for o in options):
            continue  # the same wording twice is not a choice
        options.append({**words, "change_zh": change})
    if not options:
        raise PhotoSetError(502, "AI 交回的说法都没有标题，可以再点一次。")
    return options


HASHTAG = re.compile(r"#[^\s#]+")


def publish_title(line):
    """One publish title line in the video module's format: 🔴 + English title + hashtags straight after it
    (no space before the first #, one space between tags)."""
    text = re.sub(r"\s+", " ", str(line or "")).strip().lstrip("🔴").strip()
    if not text:
        return ""
    first = text.find("#")
    head, tags = (text, []) if first < 0 else (text[:first].strip(), HASHTAG.findall(text[first:]))
    return "🔴" + head + " ".join(tags)


def publish_titles(lines):
    return [line for line in (publish_title(line)[:300] for line in (lines or [])) if line][:3]


def fallback_titles(caption):
    """Versions written before titles existed: one 🔴 line from the caption's first sentence and its hashtags."""
    text = str(caption or "")
    words = HASHTAG.sub("", text).strip()
    first = re.split(r"(?<=[.!?])\s", words, maxsplit=1)[0].strip()
    return publish_titles([first + " ".join(HASHTAG.findall(text))]) if first else []


def parse_plan(output):
    data = _json_answer(output, "AI 没按约定格式交回逐张表，可以再点一次。")
    versions = []
    for index, item in enumerate((data.get("versions") or [])[:3]):
        slides = []
        for slide in (item.get("slides") or [])[:MAX_SLIDES]:
            title = clean_lines(slide.get("title_lines"), 3, 60)
            if not title:
                continue
            try:
                time = max(0.0, float(slide.get("frame_time", 0)))
            except (TypeError, ValueError):
                time = 0.0
            try:
                crop = min(1.0, max(0.0, float(slide.get("crop_y", 0.5))))
            except (TypeError, ValueError):
                crop = 0.5
            layout = slide.get("layout") if slide.get("layout") in render.LAYOUTS else "single"
            need = len(render.LAYOUTS[layout][1]) - 1
            cells = [{"frame_time": round(_number(cell.get("frame_time"), time), 2), "crop_y": round(_number(cell.get("crop_y"), 0.5, 0.0, 1.0), 3)}
                     for cell in (slide.get("cells") or [])[:need] if isinstance(cell, dict)]
            if len(cells) < need:  # a collage without a picture for every cell falls back to one full picture
                layout, cells = "single", []
            slides.append({
                "title_lines": title, "body_lines": [render.plain(line) for line in clean_lines(slide.get("body_lines"), 2)
                                                     if render.plain(line) != render.plain(item.get("cta"))],
                "title_zh": str(slide.get("title_zh") or "")[:120], "body_zh": str(slide.get("body_zh") or "")[:200],
                "picture_zh": str(slide.get("picture_zh") or "")[:200], "frame_time": round(time, 2), "crop_y": round(crop, 3),
                "text_position": slide.get("text_position") if slide.get("text_position") in render.POSITIONS else "split",
                "text_size": slide.get("text_size") if slide.get("text_size") in render.SIZES else "l",
                "layout": layout, "cells": cells,
            })
        if not slides:
            continue
        writing = item.get("writing") if item.get("writing") in WRITINGS else WRITING_ORDER[index]
        versions.append({
            "key": "ABC"[index], "writing": writing, "name_zh": str(item.get("name_zh") or f"第 {index + 1} 版")[:40],
            "buyer_zh": str(item.get("buyer_zh") or "")[:200], "motive_zh": str(item.get("motive_zh") or "")[:200],
            "recommended": bool(item.get("recommended")), "cta": clean_lines([item.get("cta")], 1, 60)[0] if item.get("cta") else "",
            "caption_en": str(item.get("caption_en") or "")[:1200], "caption_zh": str(item.get("caption_zh") or "")[:800],
            "titles": publish_titles(item.get("titles")), "titles_zh": [str(t or "")[:200] for t in (item.get("titles_zh") or [])[:3]],
            "slides": slides,
        })
    if not versions:
        raise PhotoSetError(502, "AI 交回的逐张表是空的，可以再点一次。")
    if sum(v["recommended"] for v in versions) != 1:
        for v in versions:
            v["recommended"] = v is versions[0]
    gaps = clean_lines(data.get("gaps_zh"), 8, 300)
    return versions, gaps


def new_work(version):
    return {"key": version["key"], "name_zh": version.get("name_zh", ""), "writing": version.get("writing") or "", "status": "todo", "style": "bar",
            "cta": version.get("cta") or "", "caption_en": version.get("caption_en", ""), "caption_zh": version.get("caption_zh", ""),
            "titles": list(version.get("titles") or []), "titles_zh": list(version.get("titles_zh") or []),
            "slides": [{**{k: s.get(k) for k in ("title_lines", "body_lines", "title_zh", "body_zh", "picture_zh", "crop_y",
                                                "text_position", "text_size")},
                        "body_lines": [render.plain(line) for line in s.get("body_lines") or []
                                       if render.plain(line) != render.plain(version.get("cta"))],
                        "image": {"kind": "frame", "t": s["frame_time"]}, "layout": s.get("layout") or "single",
                        "cells": [{"image": {"kind": "frame", "t": c["frame_time"]}, "crop_x": 0.5, "crop_y": c.get("crop_y", 0.5)}
                                  for c in s.get("cells") or []]} for s in version.get("slides") or []],
            "history": [], "exports": []}


def migrate(data):
    """Sets written before the 2026-11-04 multi-version change held one chosen version at the top level. Turn that into
    the work queue: the chosen version keeps its slides and recent edits; an exported other version gets back the
    slides saved in history right before switching away from it."""
    if "works" in data or "chosen" not in data:
        data.setdefault("works", [])
        data.setdefault("current", None)
        return data
    versions = {v["key"]: v for v in data.get("versions") or []}
    history = data.pop("history", None) or []
    switches = [h for h in history if re.fullmatch(r"换成第 [A-Z] 版前", h.get("label", ""))]
    last_switch = max((i for i, h in enumerate(history) if h in switches), default=-1)
    chosen = data.pop("chosen", None)
    works = {}
    if chosen in versions:
        work = new_work(versions[chosen])
        work.update(slides=data.get("slides") or work["slides"], style=data.get("style") or "bar",
                    cta=data.get("cta", work["cta"]), caption_en=data.get("caption_en", work["caption_en"]),
                    caption_zh=data.get("caption_zh", work["caption_zh"]), history=history[last_switch + 1:], status="doing")
        works[chosen] = work
    for item in data.pop("exports", None) or []:
        match = re.search(r"-\d{8}-([A-Z])", item.get("name", ""))
        key = match.group(1) if match else chosen
        if key not in works:
            version = versions.get(key, {"key": key, "name_zh": "", "slides": []})
            work = new_work(version)
            snapshot = next((h for h in switches if h["at"] > item["at"]), None)
            if snapshot:
                work.update(slides=snapshot["slides"], cta=snapshot.get("cta", work["cta"]),
                            caption_en=snapshot.get("caption_en", work["caption_en"]))
            work.update(status="done", style=item.get("style", "bar"))
            works[key] = work
        works[key]["exports"].append({**item, "key": key})
        if key != chosen:
            works[key]["status"] = "done"
    for work in works.values():  # early AI answers repeated the call to action as a fact line
        for slide in work["slides"]:
            slide["body_lines"] = [render.plain(line) for line in slide.get("body_lines") or []
                                   if render.plain(line) != render.plain(work.get("cta"))]
    for key in ("slides", "style", "cta", "caption_en", "caption_zh"):
        data.pop(key, None)
    order = [v["key"] for v in data.get("versions") or []]
    data["works"] = sorted(works.values(), key=lambda w: (w["status"] != "done", order.index(w["key"]) if w["key"] in order else 9))
    data["current"] = chosen if chosen in works else (data["works"][0]["key"] if data["works"] else None)
    return data


class PhotoCarousels:
    def __init__(self, app, root=None, export_root=None):
        self.app = app
        self.root = Path(root or Path(app.runtime) / "production" / "photo-sets")
        self.export_root = Path(export_root or EXPORT_ROOT)
        self.lock = threading.RLock()
        self._cancel = {}
        self.root.mkdir(parents=True, exist_ok=True)
        self._interrupt_stale()

    # ---------- storage ----------
    def _folder(self, set_id):
        if not SET_ID.fullmatch(str(set_id or "")):
            raise PhotoSetError(404, "这组图文不存在。")
        folder = self.root / set_id
        if not (folder / "set.json").is_file():
            raise PhotoSetError(404, "这组图文不存在。")
        return folder

    def _read(self, set_id):
        return migrate(json.loads((self._folder(set_id) / "set.json").read_text(encoding="utf-8")))

    def _save(self, data):
        data["updated_at"] = _now()
        _write_json(self.root / data["id"] / "set.json", data)
        return data

    def _all(self):
        for path in self.root.glob("PHS-*/set.json"):
            try:
                yield migrate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue

    def _interrupt_stale(self):
        """Work cut off by a restart is reported, never silently resumed."""
        for data in self._all():
            for key in ("frames_job", "ai", "slide_ai"):
                job = data.get(key) or {}
                if job.get("status") == "running":
                    job.update(status="failed", error="工作台重启打断了这一步，请再点一次。", finished_at=_now())
                    self._save(data)
        for path in (self.root / "resolve-imports").glob("*/job.json"):
            job = json.loads(path.read_text(encoding="utf-8"))
            if job.get("status") == "running":
                job.update(status="failed", error="工作台重启打断了导入，请等达芬奇导出结束后重试。", finished_at=_now())
                _write_json(path, job)

    def find(self, job_id, timeline_uid):
        found = [d for d in self._all() if d.get("job_id") == job_id and d.get("timeline_uid") == timeline_uid and not d.get("discarded")]
        return max(found, key=lambda d: d.get("created_at", ""), default=None)

    # ---------- public view ----------
    def public(self, data):
        if not data:
            return {"exists": False}
        out = {key: data.get(key) for key in ("id", "job_id", "timeline_uid", "timeline_name", "case_id", "product", "created_at",
                                              "updated_at", "video", "frames_job", "ai", "slide_ai", "versions", "gaps", "current")}
        out["exists"] = True
        out["slide_ai"] = {k: v for k, v in data["slide_ai"].items() if k != "snapshot"} if data.get("slide_ai") else None
        out["works"] = [{"key": w["key"], "name_zh": w.get("name_zh", ""), "status": w["status"], "exports": len(w.get("exports") or []),
                         "openable": self._openable(data, w["key"])} for w in data.get("works") or []]
        work = self._work(data, required=False)
        out["work"] = ({key: work.get(key) for key in ("key", "name_zh", "writing", "status", "slides", "style", "cta", "caption_en", "caption_zh", "exports")}
                       | {"titles": work.get("titles") or fallback_titles(work.get("caption_en")), "titles_zh": work.get("titles_zh") or []}
                       | {"can_undo": bool(work.get("history")), "can_redo": bool(work.get("future")),
                          "undo_label": (work.get("history") or [{}])[-1].get("label", ""),
                          "redo_label": (work.get("future") or [{}])[-1].get("label", "")}) if work else None
        nxt = self._next_todo(data)
        out["next"] = {"key": nxt["key"], "name_zh": nxt.get("name_zh", "")} if nxt and work and work["status"] == "done" else None
        out["styles"] = render.STYLES
        out["positions"] = render.POSITIONS
        out["layouts"] = {key: label for key, (label, _) in render.LAYOUTS.items()}
        out["writings"] = WRITINGS
        out["max_slides"] = MAX_SLIDES
        out["sizes"] = {key: label for key, (label, _) in render.SIZES.items()}
        return out

    def detail(self, job_id, timeline_uid):
        with self.lock:
            out = self.public(self.find(job_id, timeline_uid))
            task = self._resolve_import(job_id, timeline_uid)
            if task:
                out["resolve_import"] = {key: task.get(key) for key in ("status", "message", "error", "started_at", "finished_at")}
                if task["status"] == "running":
                    path = self._import_folder(job_id, timeline_uid) / task["run_id"] / "progress.json"
                    try:
                        progress = json.loads(path.read_text(encoding="utf-8"))
                        out["resolve_import"].update(message=progress["message"], percent=progress.get("percent"))
                    except (OSError, ValueError, KeyError):
                        pass
            return out

    def frames(self, set_id):
        path = self._folder(set_id) / "frames.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"items": []}

    # ---------- step 1: video and frames ----------
    def _timeline(self, job_id, uid):
        job = self.app.production.job(job_id)
        timeline = next((t for t in job.get("timelines") or [] if t.get("unique_id") == uid and not t.get("archived")), None)
        if not timeline:
            raise PhotoSetError(400, "请先选一条时间线。")
        return job, timeline

    def _import_folder(self, job_id, uid):
        key = hashlib.sha256(f"{job_id}\0{uid}".encode("utf-8")).hexdigest()[:24]
        return self.root / "resolve-imports" / key

    def _resolve_import(self, job_id, uid):
        path = self._import_folder(job_id, uid) / "job.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def busy(self):
        with self.lock:
            if any((data.get(key) or {}).get("status") == "running" for data in self._all() for key in ("frames_job", "ai", "slide_ai")):
                return True
            return any(json.loads(path.read_text(encoding="utf-8")).get("status") == "running"
                       for path in (self.root / "resolve-imports").glob("*/job.json"))

    def _check_video_change(self, job_id, uid, *, importing=False):
        if not importing and (self._resolve_import(job_id, uid) or {}).get("status") == "running":
            raise PhotoSetError(409, "正在从达芬奇导入无字版，请完成后再换视频。")
        old = self.find(job_id, uid)
        if old and (old.get("ai") or {}).get("status") == "running":
            raise PhotoSetError(409, "AI 正在写逐张表，等它写完或先停止再换视频。")
        return old

    def create(self, job_id, body):
        uid = str(body.get("timeline_uid") or "")
        job, timeline = self._timeline(job_id, uid)
        source = Path(str(body.get("video_path") or ""))
        if not self.app.video_files.temporary(source) or not source.is_file():
            raise PhotoSetError(400, "请把无字幕视频拖进来上传。")
        with self.lock:
            self._check_video_change(job_id, uid)
            return self._create_video(job, timeline, source, body.get("video_name") or source.name)

    def _create_video(self, job, timeline, source, name, *, resolve_source=None):
        job_id, uid = job["id"], timeline["unique_id"]
        with self.lock:
            old = self._check_video_change(job_id, uid, importing=True)
            set_id = "PHS-" + uuid4().hex[:12]
            folder = self.root / set_id
            folder.mkdir(parents=True)
            target = folder / ("video" + (source.suffix.lower() or ".mp4"))
            shutil.copy2(source, target)
            data = {
                "id": set_id, "job_id": job_id, "timeline_uid": uid, "timeline_name": timeline.get("name") or "",
                "case_id": (job.get("replica") or {}).get("case_id") or "", "product": job.get("product") or "",
                "created_at": _now(), "video": {"name": str(name)[:200], "file": target.name,
                                                 "bytes": target.stat().st_size},
                "frames_job": {"status": "running", "started_at": _now()}, "ai": {"status": "idle"},
                "versions": [], "gaps": [], "chosen": None, "style": "bar", "slides": [], "history": [], "exports": [],
            }
            if resolve_source:
                data["video"]["resolve_source"] = resolve_source
            self._save(data)
            if old:
                old["discarded"] = True
                self._save(old)
        threading.Thread(target=self._extract, args=(set_id,), daemon=True, name=f"photo-frames-{set_id}").start()
        return self.public(data)

    def import_resolve(self, job_id, body):
        if set(body) != {"timeline_uid"}:
            raise PhotoSetError(400, "请选择要导入的时间线。")
        uid = str(body.get("timeline_uid") or "")
        _, timeline = self._timeline(job_id, uid)
        if not timeline.get("project"):
            raise PhotoSetError(409, "这条成片没有达芬奇工程关联，请先在制作单关联原时间线。")
        with self.lock:
            previous = self._resolve_import(job_id, uid)
            if previous and previous["status"] == "running":
                return self.detail(job_id, uid)
            self._check_video_change(job_id, uid)
            folder = self._import_folder(job_id, uid)
            folder.mkdir(parents=True, exist_ok=True)
            task = {"job_id": job_id, "timeline_uid": uid, "run_id": uuid4().hex[:12], "status": "running",
                    "started_at": _now(), "message": "正在连接达芬奇…", "error": "",
                    "pending_recovery": (previous.get("pending_recovery") or previous["run_id"]) if previous else None}
            _write_json(folder / "job.json", task)
            reply = self.detail(job_id, uid)
            threading.Thread(target=self._import_resolve, args=(job_id, uid, task),
                             daemon=True, name=f"photo-resolve-{task['run_id']}").start()
            return reply

    def _import_resolve(self, job_id, uid, task):
        folder = self._import_folder(job_id, uid)
        scratch = folder / task["run_id"]
        payload = {"recovery_path": str(scratch / "recovery.json"), "progress_path": str(scratch / "progress.json"),
                   "output_dir": str(scratch), "render_timeout": 1200}
        def update(**values):
            with self.lock:
                task.update(values)
                _write_json(folder / "job.json", task)
        try:
            scratch.mkdir(parents=True, exist_ok=True)
            with self.app.resolve.access(on_wait=lambda: update(message="正在等待达芬奇空闲…")):
                if task.get("pending_recovery"):
                    recovery = folder / task["pending_recovery"] / "recovery.json"
                    if recovery.is_file():
                        self.app.resolve.run("restore_textless", {"recovery_path": str(recovery), "stop_render": False}, timeout=60, held=True)
                # Fetch the binding again after queuing, so a deleted/archived timeline cannot be rendered.
                job, timeline = self._timeline(job_id, uid)
                payload.update({key: timeline[key] for key in ("project", "creator", "folder", "project_id") if timeline.get(key)})
                payload.update(unique_id=uid, name=timeline["name"])
                # Carry this pointer across failed retries until recovery has actually succeeded.
                update(pending_recovery=task["run_id"])
                try:
                    result = self.app.resolve.run("export_textless", payload, timeout=1320, held=True)
                    if result.get("unique_id") != uid or result.get("restored") is not True:
                        raise PhotoSetError(502, "无字版的时间线或轨道恢复结果未确认，没有替换原视频。")
                except Exception as exc:
                    try:
                        self.app.resolve.run("restore_textless", payload, timeout=60, held=True)
                    except Exception as recovery_error:
                        raise PhotoSetError(409, render_recovery_message(exc, recovery_error)) from exc
                    raise
            source = Path(result["video_path"]).resolve()
            if source != (scratch / "textless.mp4").resolve() or not source.is_file() or not source.stat().st_size:
                raise PhotoSetError(502, "达芬奇未交回可用的无字版视频，没有替换原视频。")
            with self.lock:
                job, timeline = self._timeline(job_id, uid)
                self._create_video(job, timeline, source, f"{result.get('name') or timeline['name']}-无字版.mp4",
                                   resolve_source={"project": timeline["project"], "timeline_uid": uid, "imported_at": _now()})
                update(status="done", message="无字版已导入，字幕和屏显轨已恢复。", finished_at=_now())
            # A leftover scratch file must not turn a completed import into a reported failure.
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
        except Exception as exc:
            update(status="failed", error=str(exc), message="", finished_at=_now())

    def _extract(self, set_id):
        folder = self.root / set_id
        try:
            data = self._read(set_id)
            frames, thumbs, sheets = folder / "frames", folder / "thumbs", folder / "sheets"
            for path in (frames, thumbs, sheets):
                shutil.rmtree(path, ignore_errors=True)
                path.mkdir()
            result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(folder / data["video"]["file"]), "-vf", f"fps={FPS}",
                                     "-q:v", "2", str(frames / "%04d.jpg")], capture_output=True, timeout=600, **_hidden_window())
            files = sorted(frames.glob("*.jpg"))
            if result.returncode or not files:
                raise PhotoSetError(500, "没能从视频里抽出画面，请确认是能播放的 MP4。")
            items = []
            for index, path in enumerate(files):
                with Image.open(path) as image:
                    size = image.size
                    image.convert("RGB").resize(THUMB).save(thumbs / path.name, quality=80)
                items.append({"n": index + 1, "t": round(index / FPS, 2), "sharp": sharpness(path)})
            _write_json(folder / "frames.json", {"fps": FPS, "width": size[0], "height": size[1], "items": items})
            self._sheets(folder, files, items)
            with self.lock:
                data = self._read(set_id)
                data["video"]["duration"] = round(len(files) / FPS, 1)
                data["frames_job"] = {"status": "done", "count": len(files), "finished_at": _now()}
                self._save(data)
        except Exception as exc:  # reported on the panel; the set stays usable for another upload
            with self.lock:
                data = self._read(set_id)
                data["frames_job"] = {"status": "failed", "error": str(exc) if isinstance(exc, PhotoSetError) else f"抽帧失败：{exc}",
                                      "finished_at": _now()}
                self._save(data)

    @staticmethod
    def _sheets(folder, files, items):
        """Contact sheets with timestamps for the AI: at most MAX_SHEETS sheets, skipping frames evenly on long videos."""
        per = SHEET_GRID[0] * SHEET_GRID[1]
        step = max(1, -(-len(files) // (per * MAX_SHEETS)))
        chosen = list(range(0, len(files), step))
        try:
            label = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 22)
        except OSError:
            label = ImageFont.load_default()
        tw, th = SHEET_TILE
        for sheet, start in enumerate(range(0, len(chosen), per)):
            canvas = Image.new("RGB", (tw * SHEET_GRID[0], th * SHEET_GRID[1]), "black")
            draw = ImageDraw.Draw(canvas)
            for slot, index in enumerate(chosen[start:start + per]):
                x, y = (slot % SHEET_GRID[0]) * tw, (slot // SHEET_GRID[0]) * th
                with Image.open(files[index]) as image:
                    canvas.paste(image.convert("RGB").resize((tw, th)), (x, y))
                draw.rectangle((x, y, x + 74, y + 28), fill="black")
                draw.text((x + 4, y + 2), f"{items[index]['t']:.1f}", fill="yellow", font=label)
            canvas.save(folder / "sheets" / f"sheet{sheet + 1:02d}.jpg", quality=85)

    # ---------- step 2: AI writes three versions ----------
    def plan(self, set_id, body=None):
        with self.lock:
            data = self._read(set_id)
            if (self._resolve_import(data["job_id"], data["timeline_uid"]) or {}).get("status") == "running":
                raise PhotoSetError(409, "正在从达芬奇导入无字版，请完成后再出逐张表。")
            if (data.get("frames_job") or {}).get("status") != "done":
                raise PhotoSetError(409, "画面还没抽完，稍等一下。")
            if (data.get("ai") or {}).get("status") == "running":
                return self.public(data)
            data["ai"] = {"status": "running", "started_at": _now(), "progress": "正在准备脚本、产品资料和画面…"}
            self._save(data)
            event = threading.Event()
            self._cancel[set_id] = event
        threading.Thread(target=self._plan, args=(set_id, event, str((body or {}).get("note") or "")[:1000]),
                         daemon=True, name=f"photo-plan-{set_id}").start()
        return self.public(data)

    def cancel(self, set_id):
        event = self._cancel.get(set_id)
        if event:
            event.set()
        return self.public(self._read(set_id))

    def _progress(self, set_id, text):
        with self.lock:
            data = self._read(set_id)
            if (data.get("ai") or {}).get("status") == "running":
                data["ai"]["progress"] = str(text)[:200]
                self._save(data)

    def _references(self, folder, product):
        refs = folder / "refs"
        refs.mkdir(exist_ok=True)
        copied = []
        products = Path(self.app.store.root) / "references" / "products"
        for name in (f"{product}.md", f"{product}-selling-point-grading.md"):
            path = products / name
            if product and path.is_file():
                shutil.copy2(path, refs / name)
                copied.append(f"refs/{name}")
        return copied

    def _skill(self):
        root = Path(getattr(self.app.ai, "skill_code_root", "") or "")
        path = root.parent / SKILL / "SKILL.md"
        return [{"name": SKILL, "path": str(path)}] if path.is_file() else []

    def _video_brief(self, data, job):
        """What the video already settled for this timeline: its on-screen hook and the direction it tests (audience,
        main selling point, concern chain, closing). The carousel opens on the same viewer and closes the same way."""
        rows = []
        timeline = next((row for row in job.get("timelines") or [] if row.get("unique_id") == data.get("timeline_uid")), {})
        hook = (timeline.get("hook_overlay") or {}).get("text") or (job.get("supplement") or {}).get("hook_text")
        if hook:
            rows.append(f"开头屏显：{hook}")
        direction = None
        if job.get("direction_id") and getattr(self.app, "directions", None):
            try:
                direction = self.app.directions.find_direction(self.app.directions.read(), job.get("product"), job["direction_id"])
            except Exception:
                direction = None
        for key, label in (("audience", "目标人群"), ("main_difference", "主打卖点"), ("concern", "顾虑链"), ("closing", "收口")):
            if (direction or {}).get(key):
                rows.append(f"{label}：{direction[key]}")
        return "\n".join(rows)

    @staticmethod
    def _script(job):
        return "\n".join(f"{line.get('number')}. {line.get('spoken')}｜{line.get('zh') or ''}"
                         + (f"｜画面：{line['proof']}" if line.get("proof") else "") for line in job.get("lines") or [])

    def prompt(self, data, job, refs, note=""):
        lines = self._script(job)
        duration = (data.get("video") or {}).get("duration") or 0
        sheets = sorted((self.root / data["id"] / "sheets").glob("*.jpg"))
        brief = self._video_brief(data, job)
        return f"""按 tiktok-photo-commerce 技能，把这条带货视频做成 TikTok 挂车图文，先出 3 版逐张表。

附图是这条视频的无字幕版抽帧总览（共 {len(sheets)} 张，每格左上角黄字是秒数，全片约 {duration} 秒）。
产品型号：{data.get('product') or '未登记'}。产品资料（只读，事实以它为准）：{'、'.join(refs) or '没有找到，请只写画面里看得见、脚本里说过的事实'}。

视频脚本（英文｜中文｜画面要证明什么）：
{lines}
{('这条视频已定的开头和方向（图文沿用同一群人、同一个收口）：' + chr(10) + brief) if brief else '这条视频没有登记方向，人群和卖点从脚本里读。'}

3 版各用一种写法，按顺序（同一条视频、同一份事实，区别就是写法），都用美国 TikTok 达人对着镜头说话的口吻（口语、短、有节奏，不要翻译腔、说明书腔和广告腔）：
- A 版 {WRITING_RULES["self"]}
- B 版 {WRITING_RULES["qa"]}
- C 版 {WRITING_RULES["list"]}
- 视频口播里已经说过、放在这一张合适的句子，优先缩短原话来用，不另起炉灶；口播里收窄过的主张不放宽。

每版都按三段走（技能「卖货理由链」；开头和收口的句位规则以写稿技能「开头、中段与收口」为准）：
1. 首张说到心里：用观众自己的处境或烦恼开场（如角度一直靠猜、磨刀配件一直在买），不懂这个品类的人也认得出；首张不介绍产品、不报配置，型号最多放在第二行。至少 1 版首张沿用视频开头的意思（按这一版的写法改说法）。
2. 中段只讲 1–2 个卖点：每个卖点都回答首张那个烦恼，标题和第二行说观众得到什么、为什么，画面里看得见效果；一个卖点可以占一到两张。不把配件、磨板、箱子逐件列成清单，和首张烦恼无关的卖点不写。
3. 末张给下单理由：一个理由加购买入口。{CLOSING_REASON_RULE}脚本收口里已经说过的理由可以直接用。
   {CLOSING_VOICE_RULE}
- 3 版不是换一种买家；不默认做送礼版，除非补充说明要求。每版 3–5 张，不为凑张数加页。
- motive_zh 写这一版的三段：「烦恼：…｜卖点：…｜下单理由：…」。
- 每张三层：大标题 1–2 短行（不懂这个品类的人 2 秒能懂，重点词用 *星号* 包起来），第二行 body_lines 一句（{SECOND_LINE_RULE}），页码由排版自动加。
- 每张选一个帧时间 frame_time（秒，必须是总览里看得到、画面能证明标题和第二行的那一格，优先清楚不糊的）；画面负责证明文案，文案不描述画面；没有能证明的画面就收窄文案，不编。
- 价格数字、优惠、活动期限只用下面补充说明给的；不写资料里列为无依据的说法。
- 字尽量不挡主体（刀身、刃口、刀柄、磨刀架、夹具、磨板、箱子），缩成缩略图也要一眼认出拍的是什么；字放在桌面、墙、背景这类空处，其次手臂和杂物。
- crop_y：成图是 3:4，竖屏视频要上下裁掉一截，0 保留顶部、1 保留底部、0.5 居中；按主体位置给，尽量把主体推到一侧，空出另一侧放字。
- text_position：split（标题在上、第二行在下，默认）、top（全部在上）、middle（全部在中间）、bottom（全部在下），选画面空的那一侧。
- layout：默认 single（一张整图）。只有两种情况才拼图：前后对比（之前/之后、钝/利）用 lr（左右）或 tb（上下），每格一个状态；套装或几件东西要在一张里说清楚用 grid（四格）或 lr。拼图时 frame_time 是第 1 格（左/上/左上），其余格按顺序写进 cells：[{{"frame_time":12.0,"crop_y":0.5}}]，格数配齐（lr、tb 再给 1 格，grid 再给 3 格），配不齐就用 single。每格都要对得上它那一半文案；拼图仍只写一个大标题和第二行，不因格子多就多写字。拼图时字放在某一格的空处，上下拼图可以用 middle 压在接缝上。
- text_size：l（默认）、m、s；空处不够大时先缩字，还不够就换一帧主体更集中的画面，不要把字压在主体上。
{('补充说明：' + note) if note else ''}

只输出一个 JSON 对象，不要别的文字：
{{"versions":[{{"writing":"self","name_zh":"不用再猜角度","buyer_zh":"卖给谁","motive_zh":"烦恼：…｜卖点：…｜下单理由：…","recommended":true,
"cta":"GET YOUR *202H* BELOW.",
"titles":["🔴I Found the Next Level for My Stone#SHARPAL202H #knifesharpening #knifesharpener #whetstone #guidedsharpening #sharpeningkit #tooltok #knifetok"],
"titles_zh":["我找到了磨石的进阶方案（只写标题中文，不含标签）"],"caption_en":"发布正文英文 1–3 句，不带标签","caption_zh":"正文中文对照",
"slides":[{{"title_lines":["I FOUND THE *NEXT LEVEL*","FOR MY STONE."],"body_lines":["One stone gets it sharp, just not the finish I'm after."],
"title_zh":"中文标题","body_zh":"第二行的中文","frame_time":34.0,"picture_zh":"这张画面里看得到什么","crop_y":0.5,"text_position":"split","text_size":"l","layout":"single","cells":[]}}]}}],
"gaps_zh":["缺什么画面、哪些说法因为没证据没写"]}}
writing 依次是 self、qa、list。recommended 只给 1 版设 true。
发布文案和视频一样（写稿技能「交付」的 3 行「🔴主题+tags」格式）：每版 titles 给 3 行，每行 = 🔴 + 英文标题 + 紧跟标题、不加空格的 hashtag 串（标签之间一个空格），产品型号标签放第一位，约 8–10 个相关标签，3 行标题切入角度不同、和这一版的写法与首张一致；titles_zh 是这 3 行标题的中文（不含标签）。caption_en 是发布正文 1–3 句、不带标签，caption_zh 是它的中文。"""

    def _plan(self, set_id, event, note):
        folder = self.root / set_id
        try:
            data = self._read(set_id)
            job = self.app.production.job(data["job_id"])
            refs = self._references(folder, data.get("product"))
            images = [(f"抽帧总览第 {n} 张", path) for n, path in enumerate(sorted((folder / "sheets").glob("*.jpg")), 1)]
            try:
                settings = self.app.ai.preferences()
            except Exception:
                settings = {}
            self._progress(set_id, "AI 正在看画面、写 3 版逐张表…")
            output = self.app.ai.bridge.run(self.prompt(data, job, refs, note), cwd=str(folder), cancel_event=event,
                                            on_progress=lambda text: self._progress(set_id, text),
                                            model=settings.get("model"), reasoning_effort=settings.get("reasoning_effort"),
                                            skills=self._skill(), images=images)
            versions, gaps = parse_plan(output)
            items = self.frames(set_id)["items"]
            for version in versions:
                for slide in version["slides"]:
                    slide["frame_time"] = self.sharpest(items, slide["frame_time"])
                    for cell in slide.get("cells") or []:
                        cell["frame_time"] = self.sharpest(items, cell["frame_time"])
            with self.lock:
                data = self._read(set_id)
                data.update(versions=versions, gaps=gaps)
                data["ai"] = {"status": "done", "finished_at": _now()}
                self._save(data)
        except Exception as exc:
            cancelled = event.is_set()
            with self.lock:
                data = self._read(set_id)
                data["ai"] = {"status": "cancelled" if cancelled else "failed", "finished_at": _now(),
                              "error": "已停止。" if cancelled else (str(exc) or "AI 没有完成，请再点一次。")[:400]}
                self._save(data)
        finally:
            self._cancel.pop(set_id, None)

    @staticmethod
    def sharpest(items, time, window=0.4):
        """The clearest frame within ±window seconds of what the AI picked (motion blur is common mid-action)."""
        near = [item for item in items if abs(item["t"] - time) <= window + 1e-6]
        if not near:
            return min(items, key=lambda item: abs(item["t"] - time))["t"] if items else time
        best = max(item["sharp"] for item in near)
        # Prefer the AI's own frame when it is nearly as sharp; only move off it to escape real blur.
        own = min(near, key=lambda item: abs(item["t"] - time))
        return own["t"] if own["sharp"] >= best * 0.85 else max(near, key=lambda item: item["sharp"])["t"]

    # ---------- one slide: AI offers three ways to say it, the user picks one ----------
    def rewrite_prompt(self, data, job, work, number, refs, pictures, note="", zh=""):
        slides = work.get("slides") or []
        slide = slides[number - 1]
        rows = "\n".join(f"{i}. {' / '.join(s.get('title_lines') or [])}｜{' / '.join(s.get('body_lines') or [])}"
                         + ("　← 要改的这一张" if i == number else "") for i, s in enumerate(slides, 1))
        brief = self._video_brief(data, job)
        where = ("这是第 1 张：用观众自己的处境或烦恼开场，不懂这个品类的人也认得出；标题不介绍产品、不报配置。" if number == 1 else
                 f"这是最后一张：画面上会自动加收口「{render.plain(work.get('cta'))}」，标题和第二行给下单理由，不重复收口原话。{CLOSING_REASON_RULE}{CLOSING_VOICE_RULE}"
                 if number == len(slides) and work.get("cta") else "这是中段：只讲一个卖点，标题说观众得到的好处，第二行说为什么。")
        writing = WRITING_RULES.get(work.get("writing") or "")
        collage = f"这一张是{render.LAYOUTS[render.layout_of(slide)][0]}拼图，附图按格子顺序给出。" if pictures > 1 else ""
        meaning = (f"""用户用中文写了这一张要说的话（每行一句）：
{zh}
3 种说法按顺序这样分工，用户的意思优先于下面其他规则：
1. 第 1 种「照你的意思」：严格按用户的意思和分行——用户第 1 行做标题，第 2 行做第二行（只有一行时整句做标题，第二行按规则补一句）。用户写到的每个信息点都要在，不加、不删、不换成别的卖点，只把说法变成地道的美式口语；这一种不受这一版写法的限制。
2. 第 2、3 种「贴着意思发散」：还是用户这几个信息点、这个意思，只换说法、语气或先后顺序（如先说结果、换成问句、说成自己的经历），尽量贴这一版的写法。不引入用户没提到的卖点、部件或功能。"""
                   if zh else f"这一张现在：{' / '.join(slide.get('title_lines') or [])}｜{' / '.join(slide.get('body_lines') or [])}（中文：{slide.get('title_zh') or ''}｜{slide.get('body_zh') or ''}）。意思不变，写得更抓人、更具体。")
        spread = ("" if zh else "\n- 3 种说法讲同一个卖点，但整张都要不一样：标题换开头写法（如问句戳痛点、结果先给、让步反差、一句吐槽），"
                  "第二行换说法角度（带上数字规格、说用完的结果、和老办法比）。3 种的第二行不能相同，也不能只差几个词。")
        check = ("第 1 种的回译和用户写的中文一句一句对得上、信息点一个不少；第 2、3 种没有用户没说的卖点、部件或功能，第二行两两不同；标题读出来像人说话。"
                 if zh else "3 种的第二行两两不同；第二行没有描述画面（没有 shown、pictured、in the photo）；标题读出来像人说话。")
        return f"""按 tiktok-photo-commerce 技能，给这组 TikTok 挂车图文的第 {number} 张（共 {len(slides)} 张）出 3 种英文说法，其他张不动。用户会从 3 种里选一种。

附图是这一张现在用的底图（没有字）。{collage}
产品型号：{data.get('product') or '未登记'}。产品资料（只读，事实以它为准）：{'、'.join(refs) or '没有找到，请只写画面里看得见、脚本里说过的事实'}。

视频脚本（英文｜中文｜画面要证明什么）：
{self._script(job)}
{('这条视频已定的开头和方向：' + chr(10) + brief) if brief else ''}

整组现在的文案（标题｜第二行）。要和前后张接得上，不重复别张已经说过的卖点：
{rows}

这一张的画面说明：{slide.get('picture_zh') or '（没有说明，看附图）'}
{meaning}
{('用户的其他要求：' + note) if note else ''}

规则（技能「卖货理由链」；句位规则以写稿技能「开头、中段与收口」为准）：
- {where}{spread}{(chr(10) + '- 这一版的写法（这一张也照它写）：' + writing) if writing else ''}
- 标题用美国 TikTok 达人对着镜头说话的口吻：口语、短、有节奏，用缩写和日常词，像跟朋友吐槽或安利，不要翻译腔、说明书腔和广告腔。对比：“SHARP ISN'T THE *FINISH LINE.*”“SHARP? COOL. NOW MAKE IT *SHINE.*” 是达人腔；“I FOUND A GUIDED WAY TO REFINE THE EDGE” 是翻译腔。
- 大标题 1–2 短行（不懂这个品类的人 2 秒能懂，重点词用 *星号* 包起来）；第二行 body_lines 一句：{SECOND_LINE_RULE}
- 文案必须是这张底图看得见、能证明的；底图证明不了就收窄说法，不编（用户自己写的话除外）。
- 价格数字、优惠、活动期限只用用户写给的；不写资料里列为无依据的说法。
- title_zh、body_zh 按你写的英文回译成中文，让用户核对意思有没有跑偏；不要抄用户写的中文。
- 交回前自查：{check}

只输出一个 JSON 对象，不要别的文字：
{{"options":[{{"title_lines":["I FOUND THE *NEXT LEVEL*","FOR MY STONE."],"body_lines":["One stone gets it sharp, just not the finish I'm after."],"title_zh":"我找到了磨石的进阶方案","body_zh":"一块磨石能磨利，但磨不出我想要的刃面","change_zh":"一句话说这种写法的特点"}}]}}"""

    def rewrite(self, set_id, number, body=None):
        """AI offers three ways to word one slide (from the user's Chinese meaning, or the current words). Nothing is
        replaced until the user picks one; pictures, crops and layout never change. Runs in the background."""
        body = body or {}
        note, zh = str(body.get("note") or "")[:500], str(body.get("zh") or "").strip()[:500]
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            slides = work.get("slides") or []
            if not 1 <= number <= len(slides):
                raise PhotoSetError(404, "这一张不存在。")
            if (data.get("slide_ai") or {}).get("status") == "running":
                raise PhotoSetError(409, f"AI 正在给第 {data['slide_ai']['number']} 张出说法，等它写完或先停止。")
            data["slide_ai"] = {"status": "running", "key": work["key"], "number": number, "slide_id": slide_id(slides[number - 1]),
                                "note": note, "zh": zh, "started_at": _now(),
                                "progress": f"AI 正在给第 {number} 张出 3 种说法…", "snapshot": copy.deepcopy(slides[number - 1])}
            self._save(data)
            event = threading.Event()
            self._cancel[f"{set_id}:slide"] = event
        threading.Thread(target=self._rewrite, args=(set_id, event), daemon=True, name=f"photo-slide-{set_id}").start()
        return self.public(data)

    def stop_rewrite(self, set_id):
        event = self._cancel.get(f"{set_id}:slide")
        if event:
            event.set()
        return self.public(self._read(set_id))

    def _rewrite(self, set_id, event):
        folder = self.root / set_id
        try:
            data = self._read(set_id)
            task = data["slide_ai"]
            job = self.app.production.job(data["job_id"])
            work = self._work(data, task["key"])
            number = task["number"]
            pictures = self._bases(set_id, task["snapshot"])
            refs = self._references(folder, data.get("product"))
            try:
                settings = self.app.ai.preferences()
            except Exception:
                settings = {}
            def progress(text):
                with self.lock:
                    current = self._read(set_id)
                    if (current.get("slide_ai") or {}).get("status") == "running":
                        current["slide_ai"]["progress"] = str(text)[:200]
                        self._save(current)
            output = self.app.ai.bridge.run(self.rewrite_prompt(data, job, work, number, refs, len(pictures), task.get("note", ""), task.get("zh", "")),
                                            cwd=str(folder), cancel_event=event, on_progress=progress,
                                            model=settings.get("model"), reasoning_effort=settings.get("reasoning_effort"),
                                            skills=self._skill(), images=[(f"第 {number} 张底图" + (f" 第 {i} 格" if len(pictures) > 1 else ""), path)
                                                                          for i, path in enumerate(pictures, 1)])
            if event.is_set():
                raise PhotoSetError(409, "已停止。")
            options = parse_options(output, work.get("cta"))
            if task.get("zh"):
                for index, option in enumerate(options):
                    option["label"] = "照你的意思" if index == 0 else "贴着意思发散"
            with self.lock:
                data = self._read(set_id)
                data["slide_ai"].update(status="done", options=options, picked=None, finished_at=_now())
                data["slide_ai"].pop("progress", None)
                self._save(data)
        except Exception as exc:
            cancelled = event.is_set()
            with self.lock:
                data = self._read(set_id)
                task = data.get("slide_ai") or {}
                data["slide_ai"] = {"status": "cancelled" if cancelled else "failed", "key": task.get("key"), "number": task.get("number"),
                                    "zh": task.get("zh", ""), "note": task.get("note", ""), "finished_at": _now(),
                                    "error": "已停止。" if cancelled else (str(exc) or "AI 没有完成，请再点一次。")[:400]}
                self._save(data)
        finally:
            self._cancel.pop(f"{set_id}:slide", None)

    def pick_option(self, set_id, number, body):
        """Put one of the offered wordings on the slide they were written for, wherever it sits now. Only the words
        change: a picture, crop or layout changed after the options were written stays. Picking is the user's own
        click and can be undone, so it also replaces words typed by hand."""
        with self.lock:
            data = self._read(set_id)
            task = data.get("slide_ai") or {}
            work = self._work(data)
            slides = work.get("slides") or []
            if task.get("status") != "done" or task.get("key") != work["key"]:
                raise PhotoSetError(409, "这一张没有可选的说法，先点「出 3 种说法」。")
            if task.get("slide_id"):
                number = next((i for i, s in enumerate(slides, 1) if s.get("id") == task["slide_id"]), 0)
                if not number:
                    raise PhotoSetError(409, "出说法的那一张已经删掉了，可以在别的张上再出一次。")
            elif task.get("number") != number:
                raise PhotoSetError(409, "这一张没有可选的说法，先点「出 3 种说法」。")
            try:
                index = int(body.get("index"))
            except (TypeError, ValueError):
                raise PhotoSetError(400, "选哪一种说法？") from None
            options = task.get("options") or []
            if not 0 <= index < len(options):
                raise PhotoSetError(400, "这种说法不存在。")
            words = {key: options[index].get(key) or ([] if key.endswith("lines") else "") for key in WORD_KEYS}
            if all(slides[number - 1].get(key) == value for key, value in words.items()):
                return self.public(data)
            self._remember(work, number, f"第 {number} 张：换成 AI 第 {index + 1} 种说法")
            slides[number - 1] = {**slides[number - 1], **words}
            task.update(picked=index, number=number)
            return self.public(self._save(data))

    # ---------- step 3/4: a queue of chosen versions, made one after another ----------
    @staticmethod
    def _work(data, key=None, required=True):
        key = key or data.get("current")
        work = next((w for w in data.get("works") or [] if w["key"] == key), None)
        if not work and required:
            raise PhotoSetError(409, "先在第 2 步选要做的版本。")
        return work

    @staticmethod
    def _next_todo(data):
        return next((w for w in data.get("works") or [] if w["status"] != "done"), None)

    def _openable(self, data, key):
        """Finished versions can be reopened; otherwise only the first unfinished one, so versions are made in order."""
        work = self._work(data, key, required=False)
        if not work:
            return False
        nxt = self._next_todo(data)
        return work["status"] == "done" or (nxt is not None and nxt["key"] == key)

    @staticmethod
    def _new_work(version):
        return new_work(version)

    def _start(self, data, key):
        work = self._work(data, key)
        if work["status"] == "todo":
            work["status"] = "doing"
        data["current"] = key

    def choose(self, set_id, body):
        """Add versions to the queue in the order given. Versions already in the queue keep their edits."""
        keys = body.get("versions")
        if keys is None and body.get("version"):
            keys = [body["version"]]
        if not isinstance(keys, list) or not keys:
            raise PhotoSetError(400, "至少选一版。")
        with self.lock:
            data = self._read(set_id)
            versions = {v["key"]: v for v in data.get("versions") or []}
            if any(key not in versions for key in keys):
                raise PhotoSetError(400, "选的版本不存在。")
            works = data.setdefault("works", [])
            have = {w["key"] for w in works}
            for key in dict.fromkeys(keys):
                if key not in have:
                    works.append(new_work(versions[key]))
            current = self._work(data, required=False)
            if not current or current["status"] == "done":
                nxt = self._next_todo(data)
                if nxt:
                    self._start(data, nxt["key"])
            return self.public(self._save(data))

    def open_work(self, set_id, body):
        with self.lock:
            data = self._read(set_id)
            key = body.get("key")
            if not self._work(data, key, required=False):
                raise PhotoSetError(404, "这一版不在制作队列里。")
            if not self._openable(data, key):
                raise PhotoSetError(409, "按顺序做：先把前面的版本定稿，再做这一版。")
            self._start(data, key)
            return self.public(self._save(data))

    def remove_work(self, set_id, body):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data, body.get("key"))
            if work.get("exports"):
                raise PhotoSetError(409, "这一版已经定稿导出过，不能移出队列。")
            data["works"] = [w for w in data["works"] if w["key"] != work["key"]]
            if data.get("current") == work["key"]:
                data["current"] = None
                nxt = self._next_todo(data)
                if nxt:
                    self._start(data, nxt["key"])
                elif data["works"]:
                    data["current"] = data["works"][-1]["key"]
            return self.public(self._save(data))

    def set_style(self, set_id, body):
        if body.get("style") not in render.STYLES:
            raise PhotoSetError(400, "排版风格无效。")
        with self.lock:
            data = self._read(set_id)
            self._work(data)["style"] = body["style"]
            return self.public(self._save(data))

    @staticmethod
    def _state(work, index, label):
        return {"at": _now(), "label": label, "slides": copy.deepcopy(work.get("slides") or []),
                "caption_en": work.get("caption_en", ""), "caption_zh": work.get("caption_zh", ""),
                "titles": list(work.get("titles") or []), "titles_zh": list(work.get("titles_zh") or []),
                "cta": work.get("cta", ""), "slide": index}

    @staticmethod
    def _restore(work, state):
        work["slides"], work["cta"] = state["slides"], state.get("cta", "")
        work["caption_en"], work["caption_zh"] = state.get("caption_en", ""), state.get("caption_zh", work.get("caption_zh", ""))
        if "titles" in state:
            work["titles"], work["titles_zh"] = state["titles"], state.get("titles_zh", [])

    @classmethod
    def _remember(cls, work, index, label):
        """Save the state before a change. A new change drops whatever could still be redone."""
        work.setdefault("history", []).append(cls._state(work, index, label))
        work["history"] = work["history"][-MAX_HISTORY:]
        work["future"] = []

    def edit_slide(self, set_id, number, body):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            slides = work.get("slides") or []
            if not 1 <= number <= len(slides):
                raise PhotoSetError(404, "这一张不存在。")
            slide = copy.deepcopy(slides[number - 1])
            changed = []
            if "layout" in body:
                if body["layout"] not in render.LAYOUTS:
                    raise PhotoSetError(400, "版式无效。")
                slide["layout"] = body["layout"]
                changed.append("版式")
            if cell_count(slide) > 1:
                fill_cells(slide)
            try:
                cell = int(body.get("cell", 1))
            except (TypeError, ValueError):
                raise PhotoSetError(400, "格子编号无效。") from None
            if not 1 <= cell <= cell_count(slide):
                raise PhotoSetError(400, "这一格不存在。")
            target = slide if cell == 1 else slide["cells"][cell - 2]
            image = body.get("image")
            if isinstance(image, dict):
                if image.get("kind") == "frame":
                    items = self.frames(set_id)["items"]
                    try:
                        time = float(image.get("t"))
                    except (TypeError, ValueError):
                        raise PhotoSetError(400, "帧时间无效。") from None
                    if not any(abs(item["t"] - time) < 1e-6 for item in items):
                        raise PhotoSetError(400, "这一帧不在抽帧里。")
                    target["image"] = {"kind": "frame", "t": time}
                elif image.get("kind") == "upload":
                    source = Path(str(image.get("path") or ""))
                    if not self.app.video_files.temporary(source) or not source.is_file():
                        raise PhotoSetError(400, "请重新选择要上传的图片。")
                    try:
                        with Image.open(source) as probe:
                            probe.verify()
                    except Exception:
                        raise PhotoSetError(400, "这个文件不是能打开的图片。") from None
                    uploads = self.root / set_id / "uploads"
                    uploads.mkdir(exist_ok=True)
                    name = uuid4().hex[:12] + source.suffix.lower()
                    shutil.copy2(source, uploads / name)
                    target["image"] = {"kind": "upload", "file": name, "name": str(image.get("name") or source.name)[:200]}
                    target.setdefault("crop_y", 0.5)
                else:
                    raise PhotoSetError(400, "图片来源无效。")
                changed.append("换图")
            for key in ("crop_y", "crop_x"):
                if key in body:
                    try:
                        target[key] = round(min(1.0, max(0.0, float(body[key]))), 3)
                    except (TypeError, ValueError):
                        raise PhotoSetError(400, "裁切位置无效。") from None
                    if "裁切" not in changed:
                        changed.append("裁切")
            if "text_position" in body:
                if body["text_position"] not in render.POSITIONS:
                    raise PhotoSetError(400, "文字位置无效。")
                slide["text_position"] = body["text_position"]
                changed.append("文字位置")
            if "text_size" in body:
                if body["text_size"] not in render.SIZES:
                    raise PhotoSetError(400, "字号无效。")
                slide["text_size"] = body["text_size"]
                changed.append("字号")
            for key, limit, length in (("title_lines", 3, 60), ("body_lines", 2, 160)):
                if key in body:
                    lines = clean_lines(body[key], limit, length)
                    if key == "body_lines":
                        lines = [render.plain(line) for line in lines]
                    if key == "title_lines" and not lines:
                        raise PhotoSetError(400, "标题不能为空。")
                    slide[key] = lines
                    changed.append("文案")
            for key in ("title_zh", "body_zh"):
                if key in body:
                    slide[key] = str(body[key] or "")[:200]
            if not changed and slide == slides[number - 1]:
                return self.public(data)
            where = f"第 {number} 张第 {cell} 格" if cell > 1 else f"第 {number} 张"
            self._remember(work, number, f"{where}：" + "、".join(dict.fromkeys(changed or ["中文"])))
            slides[number - 1] = slide
            return self.public(self._save(data))

    def arrange(self, set_id, number, body):
        """Copy a slide (the copy goes right after it), delete one, or move one to another position. Page numbers and the
        closing line on the last slide follow automatically; every change can be undone."""
        action = body.get("action")
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            slides = work.get("slides") or []
            if not 1 <= number <= len(slides):
                raise PhotoSetError(404, "这一张不存在。")
            if action == "duplicate":
                if len(slides) >= MAX_SLIDES:
                    raise PhotoSetError(409, f"一组最多 {MAX_SLIDES} 张，先删掉一张再复制。")
                self._remember(work, number, f"复制第 {number} 张")
                twin = copy.deepcopy(slides[number - 1])
                twin.pop("id", None)  # the copy is a slide of its own
                slides.insert(number, twin)
            elif action == "delete":
                if len(slides) <= 1:
                    raise PhotoSetError(409, "至少要留一张。")
                self._remember(work, number, f"删掉第 {number} 张")
                slides.pop(number - 1)
            elif action == "move":
                try:
                    to = int(body.get("to"))
                except (TypeError, ValueError):
                    raise PhotoSetError(400, "要移到第几张？") from None
                if not 1 <= to <= len(slides):
                    raise PhotoSetError(400, "要移到的位置不存在。")
                if to == number:
                    return self.public(data)
                self._remember(work, number, f"第 {number} 张移到第 {to} 张")
                slides.insert(to - 1, slides.pop(number - 1))
            else:
                raise PhotoSetError(400, "操作无效。")
            work["slides"] = slides
            return self.public(self._save(data))

    def edit_set(self, set_id, body):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            self._remember(work, None, "发布文案或收口")
            if "cta" in body:
                work["cta"] = clean_lines([body["cta"]], 1, 60)[0] if body.get("cta") else ""
            for key, limit in (("caption_en", 1200), ("caption_zh", 800)):
                if key in body:
                    work[key] = str(body[key] or "")[:limit]
            if "titles" in body:
                lines = body["titles"] if isinstance(body["titles"], list) else str(body["titles"] or "").splitlines()
                work["titles"] = publish_titles(lines)
                if "titles_zh" not in body:  # a hand-edited title has no Chinese yet
                    work["titles_zh"] = []
            if "titles_zh" in body:
                work["titles_zh"] = [str(t or "")[:200] for t in (body["titles_zh"] or [])[:3]]
            return self.public(self._save(data))

    def undo(self, set_id):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            if not work.get("history"):
                raise PhotoSetError(409, "没有可以撤销的改动。")
            last = work["history"].pop()
            work.setdefault("future", []).append(self._state(work, last.get("slide"), last.get("label", "")))
            work["future"] = work["future"][-MAX_HISTORY:]
            self._restore(work, last)
            return self.public(self._save(data))

    def redo(self, set_id):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            if not work.get("future"):
                raise PhotoSetError(409, "没有可以恢复的改动。")
            nxt = work["future"].pop()
            work.setdefault("history", []).append(self._state(work, nxt.get("slide"), nxt.get("label", "")))
            work["history"] = work["history"][-MAX_HISTORY:]
            self._restore(work, nxt)
            return self.public(self._save(data))

    # ---------- pictures ----------
    def frame_path(self, set_id, number, full=False):
        folder = self._folder(set_id)
        path = folder / ("frames" if full else "thumbs") / f"{int(number):04d}.jpg"
        if not path.is_file():
            raise PhotoSetError(404, "这一帧不存在。")
        return path

    def _bases(self, set_id, slide):
        """One picture per cell of the slide's layout (one for a plain slide)."""
        return [self._base(set_id, cell) for cell in slide_cells(slide)]

    def _base(self, set_id, slide):
        image = slide.get("image") or {}
        folder = self._folder(set_id)
        if image.get("kind") == "upload":
            path = folder / "uploads" / Path(image.get("file", "")).name
        else:
            path = folder / "frames" / f"{round(float(image.get('t', 0)) * FPS) + 1:04d}.jpg"
        if not path.is_file():
            raise PhotoSetError(404, "这张的底图找不到了，请换一张。")
        return path

    @staticmethod
    def _spec(work, number):
        slides = work.get("slides") or []
        slide = dict(slides[number - 1])
        if number == len(slides) and work.get("cta"):
            slide["cta"] = work["cta"]
        return slide

    def base_path(self, set_id, key, number, cell=1):
        """The uncropped picture behind one slide (or one cell of a collage), for dragging the crop window in the page."""
        work = self._work(self._read(set_id), key, required=False) or {}
        slides = work.get("slides") or []
        if not 1 <= number <= len(slides):
            raise PhotoSetError(404, "这一张不存在。")
        cells = slide_cells(slides[number - 1])
        if not 1 <= cell <= len(cells):
            raise PhotoSetError(404, "这一格不存在。")
        return self._base(set_id, cells[cell - 1])

    def rendered(self, set_id, key, style, number, width=None):
        """Render on demand; cached by the exact inputs so an unchanged slide is never drawn twice."""
        data = self._read(set_id)
        work = self._work(data, key, required=False) or {}
        slides = work.get("slides") or []
        if style not in render.STYLES or not 1 <= number <= len(slides):
            raise PhotoSetError(404, "这一张不存在。")
        spec = self._spec(work, number)
        bases = self._bases(set_id, spec)
        width = int(width) if width and 120 <= int(width) <= render.W else render.W
        key = hashlib.sha256(json.dumps([spec, [(str(b), b.stat().st_size) for b in bases], style, number, len(slides), width],
                                        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        cache = self.root / set_id / "renders"
        cache.mkdir(exist_ok=True)
        path = cache / f"{key}.jpg"
        if not path.is_file():
            image = render.render(spec, bases, style, number, len(slides))
            if width != render.W:
                image = image.resize((width, round(width * render.H / render.W)), Image.LANCZOS)
            tmp = path.with_suffix(".part")
            image.save(tmp, "JPEG", quality=90)
            os.replace(tmp, path)
        return path

    # ---------- freeze ----------
    def export(self, set_id):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            slides, style, chosen = work.get("slides") or [], work.get("style"), work["key"]
            if not slides or style not in render.STYLES:
                raise PhotoSetError(409, "先选一版文案和排版风格。")
            version = next((v for v in data["versions"] if v["key"] == chosen), {})
            stamp = datetime.now().strftime("%Y%m%d")
            label = re.sub(r'[\\/:*?"<>|\s]+', "", version.get("name_zh") or work.get("name_zh") or chosen)[:20]
            base_name = f"{data.get('case_id') or data['job_id']}-{stamp}-{chosen}{label}"
            folder = self.export_root / base_name
            suffix = 2
            while folder.exists():
                folder = self.export_root / f"{base_name}-v{suffix}"
                suffix += 1
            (folder / "发布JPG").mkdir(parents=True)
            (folder / "无损PNG").mkdir()
            (folder / "source").mkdir()
            sources = {}
            for number in range(1, len(slides) + 1):
                spec = self._spec(work, number)
                bases = self._bases(set_id, spec)
                image = render.render(spec, bases, style, number, len(slides))
                image.save(folder / "无损PNG" / f"{number:02d}.png")
                image.save(folder / "发布JPG" / f"{number:02d}.jpg", quality=93)
                described = []
                for cell, (base, picture) in enumerate(zip(bases, (c["image"] for c in slide_cells(spec))), 1):
                    shutil.copy2(base, folder / "source" / (f"{number:02d}{base.suffix}" if cell == 1 else f"{number:02d}-{cell}{base.suffix}"))
                    text = f"视频帧 {picture['t']} 秒" if picture["kind"] == "frame" else f"用户上传图 {picture.get('name')}"
                    described.append(f"第 {cell} 格 {text}" if len(bases) > 1 else text)
                sources[str(number)] = "；".join(described)
            copy_json = {
                "content_type": "photo_carousel", "product": data.get("product"), "source_case_id": data.get("case_id"),
                "source_production": f"{data['job_id']} / 时间线 {data.get('timeline_name')}", "photo_set": set_id,
                "variant": f"{chosen} {version.get('name_zh', '')}：{version.get('motive_zh', '')}",
                "buyer_zh": version.get("buyer_zh", ""), "chosen_layout": render.STYLES[style],
                "slides": [{"number": n, "title_lines": [render.plain(t) for t in s["title_lines"]], "body": " / ".join(s.get("body_lines") or []),
                            "title_zh": s.get("title_zh", ""), "body_zh": s.get("body_zh", ""), "picture_zh": s.get("picture_zh", ""),
                            "source": sources[str(n)], "crop_y": s.get("crop_y"), "crop_x": s.get("crop_x", 0.5), "text_position": s.get("text_position"),
                            "layout": render.LAYOUTS[render.layout_of(s)][0]}
                           for n, s in enumerate(slides, 1)],
                "cta": render.plain(work.get("cta")), "caption_en": work.get("caption_en", ""), "caption_zh": work.get("caption_zh", ""),
                "titles": work.get("titles") or fallback_titles(work.get("caption_en")), "titles_zh": work.get("titles_zh") or [],
                "market": "US", "image_language": "en", "dimensions": [render.W, render.H], "aspect_ratio": "3:4",
                "gaps_zh": data.get("gaps") or [], "version": folder.name, "exported_at": _now(),
                "method": "满版原图（视频帧或用户上传图，拼图时每格一张）等比裁切铺满，文字直接叠加；未调用生图模型，未改背景、手或产品。",
            }
            _write_json(folder / "copy.json", copy_json)
            titles = work.get("titles") or fallback_titles(work.get("caption_en"))
            (folder / "发布文案.txt").write_text("\n".join(titles + ["", work.get("caption_zh", ""), "", work.get("caption_en", "")]).strip() + "\n",
                                                encoding="utf-8")
            work.setdefault("exports", []).append({"at": _now(), "folder": str(folder), "name": folder.name, "style": style})
            work["status"] = "done"
            self._save(data)
            return {**self.public(data), "export": {"folder": str(folder), "name": folder.name}}

    def export_folder(self, set_id, name):
        data = self._read(set_id)
        found = next((item for w in data.get("works") or [] for item in w.get("exports") or [] if item["name"] == name), None)
        if not found or not Path(found["folder"]).is_dir():
            raise PhotoSetError(404, "这个成品文件夹不在了。")
        return Path(found["folder"])
