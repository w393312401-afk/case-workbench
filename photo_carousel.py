"""挂车图文 for one finished timeline: no-subtitle video → frames → AI writes 3 per-slide versions → pick one →
three type styles → per-slide picture swaps (another frame or an uploaded photo, crop and text position) → freeze.

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


def parse_plan(output):
    """Pull the JSON object out of the model's answer; tolerant of code fences and prose around it."""
    text = str(output or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = fenced.group(1) if fenced else text[text.find("{"):text.rfind("}") + 1]
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PhotoSetError(502, "AI 没按约定格式交回逐张表，可以再点一次。") from exc
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
            slides.append({
                "title_lines": title, "body_lines": [render.plain(line) for line in clean_lines(slide.get("body_lines"), 2)
                                                     if render.plain(line) != render.plain(item.get("cta"))],
                "title_zh": str(slide.get("title_zh") or "")[:120], "body_zh": str(slide.get("body_zh") or "")[:200],
                "picture_zh": str(slide.get("picture_zh") or "")[:200], "frame_time": round(time, 2), "crop_y": round(crop, 3),
                "text_position": slide.get("text_position") if slide.get("text_position") in render.POSITIONS else "split",
                "text_size": slide.get("text_size") if slide.get("text_size") in render.SIZES else "l",
            })
        if not slides:
            continue
        versions.append({
            "key": "ABC"[index], "name_zh": str(item.get("name_zh") or f"第 {index + 1} 版")[:40],
            "buyer_zh": str(item.get("buyer_zh") or "")[:200], "motive_zh": str(item.get("motive_zh") or "")[:200],
            "recommended": bool(item.get("recommended")), "cta": clean_lines([item.get("cta")], 1, 60)[0] if item.get("cta") else "",
            "caption_en": str(item.get("caption_en") or "")[:1200], "caption_zh": str(item.get("caption_zh") or "")[:800],
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
    return {"key": version["key"], "name_zh": version.get("name_zh", ""), "status": "todo", "style": "bar",
            "cta": version.get("cta") or "", "caption_en": version.get("caption_en", ""), "caption_zh": version.get("caption_zh", ""),
            "slides": [{**{k: s.get(k) for k in ("title_lines", "body_lines", "title_zh", "body_zh", "picture_zh", "crop_y",
                                                "text_position", "text_size")},
                        "body_lines": [render.plain(line) for line in s.get("body_lines") or []
                                       if render.plain(line) != render.plain(version.get("cta"))],
                        "image": {"kind": "frame", "t": s["frame_time"]}} for s in version.get("slides") or []],
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
            for key in ("frames_job", "ai"):
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
                                              "updated_at", "video", "frames_job", "ai", "versions", "gaps", "current")}
        out["exists"] = True
        out["works"] = [{"key": w["key"], "name_zh": w.get("name_zh", ""), "status": w["status"], "exports": len(w.get("exports") or []),
                         "openable": self._openable(data, w["key"])} for w in data.get("works") or []]
        work = self._work(data, required=False)
        out["work"] = ({key: work.get(key) for key in ("key", "name_zh", "status", "slides", "style", "cta", "caption_en", "caption_zh", "exports")}
                       | {"can_undo": bool(work.get("history"))}) if work else None
        nxt = self._next_todo(data)
        out["next"] = {"key": nxt["key"], "name_zh": nxt.get("name_zh", "")} if nxt and work and work["status"] == "done" else None
        out["styles"] = render.STYLES
        out["positions"] = render.POSITIONS
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
            if any((data.get(key) or {}).get("status") == "running" for data in self._all() for key in ("frames_job", "ai")):
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
                        raise PhotoSetError(409, f"{exc}；字幕和屏显轨恢复未确认：{recovery_error}") from exc
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

    def prompt(self, data, job, refs, note=""):
        lines = "\n".join(f"{line.get('number')}. {line.get('spoken')}｜{line.get('zh') or ''}"
                          + (f"｜画面：{line['proof']}" if line.get("proof") else "") for line in job.get("lines") or [])
        duration = (data.get("video") or {}).get("duration") or 0
        sheets = sorted((self.root / data["id"] / "sheets").glob("*.jpg"))
        brief = self._video_brief(data, job)
        return f"""按 tiktok-photo-commerce 技能，把这条带货视频做成 TikTok 挂车图文，先出 3 版逐张表。

附图是这条视频的无字幕版抽帧总览（共 {len(sheets)} 张，每格左上角黄字是秒数，全片约 {duration} 秒）。
产品型号：{data.get('product') or '未登记'}。产品资料（只读，事实以它为准）：{'、'.join(refs) or '没有找到，请只写画面里看得见、脚本里说过的事实'}。

视频脚本（英文｜中文｜画面要证明什么）：
{lines}
{('这条视频已定的开头和方向（图文沿用同一群人、同一个收口）：' + chr(10) + brief) if brief else '这条视频没有登记方向，人群和卖点从脚本里读。'}

每版都按三段写（技能「卖货理由链」；开头和收口的句位规则以写稿技能「开头、中段与收口」为准）：
1. 首张说到心里：用观众自己的处境或烦恼开场（如角度一直靠猜、磨刀配件一直在买），不懂这个品类的人也认得出；首张标题不介绍产品、不报配置，型号最多放在事实行。至少 1 版首张沿用视频的开头（上面的开头屏显或脚本第一句）。
2. 中段只讲 1–2 个卖点：每个卖点都回答首张那个烦恼，标题说观众得到的好处，事实行给证明，画面里看得见效果；一个卖点可以占一到两张。不把配件、磨板、箱子逐件列成清单，和首张烦恼无关的卖点不写。
3. 末张给下单理由：一个已核实的理由加购买入口。理由按写稿技能的优先顺序：质保（按产品资料原样说年限和“有限”）或价格（没给今天挂车价就不写数字，可以写去看今天的价格）；脚本收口里已经说过的理由可以直接用；只有这些都没有时才用整套，并说出观众因此避开什么（不用一件件凑、不会买错配件）。收纳、硬壳这类不算下单理由。
- 3 版用同一条视频、同一份事实，区别在首张说到的烦恼或开头写法（让步反差、结果先给、身份圈人、戳破以为正常），不是换一种买家；不默认做送礼版，除非补充说明要求。每版 3–5 张，不为凑张数加页。
- motive_zh 写这一版的三段：「烦恼：…｜卖点：…｜下单理由：…」。
- 每张三层：大标题 1–2 短行（不懂这个品类的人 2 秒能懂，重点词用 *星号* 包起来），事实行 1–2 行（具体、可核实：数字、规格、件数、目数、范围），页码由排版自动加。
- 每张选一个帧时间 frame_time（秒，必须是总览里看得到、画面能证明标题和事实行的那一格，优先清楚不糊的）；没有能证明的画面就收窄文案，不编。
- 价格数字、优惠、活动期限只用下面补充说明给的；不写资料里列为无依据的说法。
- 字尽量不挡主体（刀身、刃口、刀柄、磨刀架、夹具、磨板、箱子），缩成缩略图也要一眼认出拍的是什么；字放在桌面、墙、背景这类空处，其次手臂和杂物。
- crop_y：成图是 3:4，竖屏视频要上下裁掉一截，0 保留顶部、1 保留底部、0.5 居中；按主体位置给，尽量把主体推到一侧，空出另一侧放字。
- text_position：split（标题在上、事实在下，默认）、top（全部在上）、bottom（全部在下），选画面空的那一侧。
- text_size：l（默认）、m、s；空处不够大时先缩字，还不够就换一帧主体更集中的画面，不要把字压在主体上。
{('补充说明：' + note) if note else ''}

只输出一个 JSON 对象，不要别的文字：
{{"versions":[{{"name_zh":"不用再猜角度","buyer_zh":"卖给谁","motive_zh":"烦恼：…｜卖点：…｜下单理由：…","recommended":true,
"cta":"GET YOUR *202H* BELOW.","caption_en":"发布文案英文 1–3 句 + 4–6 个标签","caption_zh":"中文对照",
"slides":[{{"title_lines":["STILL *GUESSING*","YOUR ANGLE?"],"body_lines":["202H holds the knife for you"],
"title_zh":"中文标题","body_zh":"中文事实行","frame_time":34.0,"picture_zh":"这张画面里看得到什么","crop_y":0.5,"text_position":"split","text_size":"l"}}]}}],
"gaps_zh":["缺什么画面、哪些说法因为没证据没写"]}}
recommended 只给 1 版设 true。"""

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
    def _remember(work, index, label):
        work.setdefault("history", []).append({"at": _now(), "label": label, "slides": copy.deepcopy(work.get("slides") or []),
                                               "caption_en": work.get("caption_en", ""), "caption_zh": work.get("caption_zh", ""),
                                               "cta": work.get("cta", ""), "slide": index})
        work["history"] = work["history"][-MAX_HISTORY:]

    def edit_slide(self, set_id, number, body):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            slides = work.get("slides") or []
            if not 1 <= number <= len(slides):
                raise PhotoSetError(404, "这一张不存在。")
            slide = copy.deepcopy(slides[number - 1])
            changed = []
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
                    slide["image"] = {"kind": "frame", "t": time}
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
                    slide["image"] = {"kind": "upload", "file": name, "name": str(image.get("name") or source.name)[:200]}
                    slide.setdefault("crop_y", 0.5)
                else:
                    raise PhotoSetError(400, "图片来源无效。")
                changed.append("换图")
            for key in ("crop_y", "crop_x"):
                if key in body:
                    try:
                        slide[key] = round(min(1.0, max(0.0, float(body[key]))), 3)
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
            self._remember(work, number, f"第 {number} 张：" + "、".join(dict.fromkeys(changed or ["中文"])))
            slides[number - 1] = slide
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
            return self.public(self._save(data))

    def undo(self, set_id):
        with self.lock:
            data = self._read(set_id)
            work = self._work(data)
            if not work.get("history"):
                raise PhotoSetError(409, "没有可以撤销的改动。")
            last = work["history"].pop()
            work["slides"], work["cta"] = last["slides"], last.get("cta", "")
            work["caption_en"], work["caption_zh"] = last.get("caption_en", ""), last.get("caption_zh", work.get("caption_zh", ""))
            return self.public(self._save(data))

    # ---------- pictures ----------
    def frame_path(self, set_id, number, full=False):
        folder = self._folder(set_id)
        path = folder / ("frames" if full else "thumbs") / f"{int(number):04d}.jpg"
        if not path.is_file():
            raise PhotoSetError(404, "这一帧不存在。")
        return path

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

    def base_path(self, set_id, key, number):
        """The uncropped picture behind one slide, for dragging the crop window in the page."""
        work = self._work(self._read(set_id), key, required=False) or {}
        slides = work.get("slides") or []
        if not 1 <= number <= len(slides):
            raise PhotoSetError(404, "这一张不存在。")
        return self._base(set_id, slides[number - 1])

    def rendered(self, set_id, key, style, number, width=None):
        """Render on demand; cached by the exact inputs so an unchanged slide is never drawn twice."""
        data = self._read(set_id)
        work = self._work(data, key, required=False) or {}
        slides = work.get("slides") or []
        if style not in render.STYLES or not 1 <= number <= len(slides):
            raise PhotoSetError(404, "这一张不存在。")
        spec = self._spec(work, number)
        base = self._base(set_id, spec)
        width = int(width) if width and 120 <= int(width) <= render.W else render.W
        key = hashlib.sha256(json.dumps([spec, str(base), base.stat().st_size, style, number, len(slides), width],
                                        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:24]
        cache = self.root / set_id / "renders"
        cache.mkdir(exist_ok=True)
        path = cache / f"{key}.jpg"
        if not path.is_file():
            image = render.render(spec, base, style, number, len(slides))
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
                base = self._base(set_id, spec)
                image = render.render(spec, base, style, number, len(slides))
                image.save(folder / "无损PNG" / f"{number:02d}.png")
                image.save(folder / "发布JPG" / f"{number:02d}.jpg", quality=93)
                shutil.copy2(base, folder / "source" / f"{number:02d}{base.suffix}")
                picture = spec["image"]
                sources[str(number)] = (f"视频帧 {picture['t']} 秒" if picture["kind"] == "frame" else f"用户上传图 {picture.get('name')}")
            copy_json = {
                "content_type": "photo_carousel", "product": data.get("product"), "source_case_id": data.get("case_id"),
                "source_production": f"{data['job_id']} / 时间线 {data.get('timeline_name')}", "photo_set": set_id,
                "variant": f"{chosen} {version.get('name_zh', '')}：{version.get('motive_zh', '')}",
                "buyer_zh": version.get("buyer_zh", ""), "chosen_layout": render.STYLES[style],
                "slides": [{"number": n, "title_lines": [render.plain(t) for t in s["title_lines"]], "body": " / ".join(s.get("body_lines") or []),
                            "title_zh": s.get("title_zh", ""), "body_zh": s.get("body_zh", ""), "picture_zh": s.get("picture_zh", ""),
                            "source": sources[str(n)], "crop_y": s.get("crop_y"), "crop_x": s.get("crop_x", 0.5), "text_position": s.get("text_position")}
                           for n, s in enumerate(slides, 1)],
                "cta": render.plain(work.get("cta")), "caption_en": work.get("caption_en", ""), "caption_zh": work.get("caption_zh", ""),
                "market": "US", "image_language": "en", "dimensions": [render.W, render.H], "aspect_ratio": "3:4",
                "gaps_zh": data.get("gaps") or [], "version": folder.name, "exported_at": _now(),
                "method": "满版原图（视频帧或用户上传图）等比裁切铺满，文字直接叠加；未调用生图模型，未改背景、手或产品。",
            }
            _write_json(folder / "copy.json", copy_json)
            (folder / "发布文案.txt").write_text(f"{work.get('caption_en', '')}\n\n{work.get('caption_zh', '')}\n", encoding="utf-8")
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
