"""Upload a chosen timeline's exported video and publishing copy to Notion.

Receipts and managed media live under production/notion-uploads. Uploading is
independent of registering a TikTok post and never changes a script to published.

挂车图文 goes to the same calendar: one page per carousel version, its exported 发布 JPGs in 「文件和媒体」 and
the frozen copy.json text in the body. Its receipts live in photo-catalog.json beside the video catalog, so the
video readers (task states, overview, needs-you) never mistake a picture upload for a video one.
"""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
from uuid import uuid4

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from feishu_sync import FeishuError, FeishuSync
from file_identity import file_identity
from notion_bridge import NotionBridge, UPLOAD_LIMIT, notion_id, page_properties, query_allowance_message, send_video
from production_operations import file_lock
from production_store import active_timelines, timeline_running
from release_naming import release_filename, release_name
from workbench_video_publish import VideoPublicationStore


ACCOUNT_FIELDS = ("账号", "发布账号")
STATUS_FIELD, STATUS_READY = "视频状态", "就绪"  # A freshly uploaded video is ready to post.


def body_markdown(source):
    def block(value):
        fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", value)), default=3))
        return f"{fence}text\n{value}\n{fence}"
    parts = ["## 英文文案", block(source["en"])]
    if source.get("zh"):
        parts += ["## 中文文案", block(source["zh"])]
    if source.get("screen_en"):
        parts += ["## 钩子屏显", block(source["screen_en"])]
        if source.get("screen_zh"):
            parts.append(source["screen_zh"])
        alternatives = [option for option in source.get("screen_options", []) if option.get("en") and option["en"] != source["screen_en"]]
        for index, option in enumerate(alternatives, 1):
            parts += [f"### 屏显备选 {index}", block(option["en"])]
            if option.get("zh"):
                parts.append(option["zh"])
    return "\n\n".join(parts)


def photo_markdown(source):
    """Body of a carousel page: the publish copy, then every slide's English text with its Chinese reading."""
    def block(value):
        fence = "`" * max(3, max((len(x) + 1 for x in re.findall(r"`+", value)), default=3))
        return f"{fence}text\n{value}\n{fence}"
    parts = []
    if source.get("caption_en"):
        # 德语、西语图文的正文也放在 caption_en；copy.json 的 market 说是哪种语言（2026-11-23）。
        heading = {"DE": "德语文案", "ES": "西语文案"}.get(str(source.get("market") or "").upper(), "英文文案")
        parts += [f"## {heading}", block(source["caption_en"])]
    if source.get("caption_zh"):
        parts += ["## 中文文案", block(source["caption_zh"])]
    slides = source.get("slides") or []
    if slides:
        parts.append("## 逐张文字")
    for slide in slides:
        text = "\n".join([*(slide.get("title_lines") or []), *([slide["body"]] if slide.get("body") else [])]).strip()
        parts.append(f"### 第 {slide.get('number')} 张")
        if text:
            parts.append(block(text))
        reading = " / ".join(filter(None, [slide.get("title_zh", ""), slide.get("body_zh", "")]))
        if reading:
            parts.append(reading)
    return "\n\n".join(parts) or "挂车图文"


def upload_video(source, output, progress):
    """Keep the captured original; make a bounded MP4 only when the MCP needs it."""
    if source.stat().st_size <= UPLOAD_LIMIT and source.suffix.lower() in {".mp4", ".mov", ".webm"}:
        return source
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise VoiceError(409, "此成片需要生成 20 MiB 内的上传版，请安装 FFmpeg 或选择较小的 MP4。")
    flags = subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0
    try:
        probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)],
            capture_output=True, timeout=30, creationflags=flags, check=True)
        duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not 0 < duration <= 3600:
            raise ValueError()
        rate = min(8000, int(18 * 1024 * 1024 * 8 / duration / 1000) - 192)
        if rate < 500:
            raise VoiceError(409, "视频较长，无法在 20 MiB 内保留可用画质，请先准备较小的上传版。")
        progress("正在生成 Notion 上传版，高清原件保留。")
        output.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-maxrate", f"{rate}k", "-bufsize", f"{rate * 2}k", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(output)], capture_output=True, timeout=600, creationflags=flags, check=True)
        if not 12 <= output.stat().st_size <= UPLOAD_LIMIT:
            raise ValueError()
        return output
    except VoiceError:
        raise
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise VoiceError(502, "Notion 上传版未生成完成，原成片已保留，请核对视频后重试。") from None


class NotionUploadStore(VideoPublicationStore):
    def __init__(self, production_root):
        self.root = Path(production_root) / "notion-uploads"
        self.path = self.root / "catalog.json"

    def read(self):
        """Validated receipts, parsed once per file version.

        Checking every managed video path resolves each one on disk; on Windows
        that is several handle opens per receipt and made every task list,
        content view and upload dialog wait on it. A changed file identity
        (size, write or change time) re-reads and re-validates; the upload
        itself still re-checks its file through managed_path.
        """
        try:
            identity = file_identity(self.path)
        except FileNotFoundError:
            return {"schema_version": 1, "uploads": {}}
        except OSError:
            raise VoiceError(500, "Notion 上传记录无法读取，请保留资料并核对备份。") from None
        cached = getattr(self, "_parsed", None)
        if cached is None or cached[0] != identity:
            data = self._read_validated()
            try:
                if file_identity(self.path) == identity:
                    self._parsed = cached = (identity, data)
                else:
                    return data
            except OSError:
                return data
        return copy.deepcopy(cached[1])

    def _read_validated(self):
        if not self.path.exists():
            return {"schema_version": 1, "uploads": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or not isinstance(data.get("uploads"), dict):
                raise ValueError()
            for key, row in data["uploads"].items():
                if (not re.fullmatch(r"NUP-[0-9a-f]{12}", key) or row.get("id") != key
                        or not re.fullmatch(r"PRD-[0-9a-f]{12}", row.get("job_id", ""))):
                    raise ValueError()
                if row.get("file"):
                    self.managed_path(row["file"])
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise VoiceError(500, "Notion 上传记录无法读取，请保留资料并核对备份。") from None


class PhotoNotionStore(NotionUploadStore):
    """Receipts for carousel uploads: one row per (photo set, version), each pointing at a frozen 图文成品 folder."""
    def __init__(self, production_root):
        self.root = Path(production_root) / "notion-uploads"
        self.path = self.root / "photo-catalog.json"

    def _read_validated(self):
        if not self.path.exists():
            return {"schema_version": 1, "uploads": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("schema_version") != 1 or not isinstance(data.get("uploads"), dict):
                raise ValueError()
            for key, row in data["uploads"].items():
                if (not re.fullmatch(r"NPH-[0-9a-f]{12}", key) or row.get("id") != key
                        or not re.fullmatch(r"PRD-[0-9a-f]{12}", row.get("job_id", ""))
                        or not re.fullmatch(r"PHS-[0-9a-f]{12}", row.get("set_id", ""))):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise VoiceError(500, "图文 Notion 上传记录无法读取，请保留资料并核对备份。") from None


class NotionUploads:
    def __init__(self, workbench, settings=None, bridge=None, feishu=None):
        self.app = workbench
        self.settings = copy.deepcopy(settings or {})
        # 上传 Notion 成功后顺带写飞书多维表格（config.local.json 的 feishu_sync）；没配就不碰飞书。
        self.feishu = feishu if feishu is not None else (
            FeishuSync(self.settings["feishu_sync"]) if self.settings.get("feishu_sync") else None)
        self.store = NotionUploadStore(workbench.production.root)
        self.photo_store = PhotoNotionStore(workbench.production.root)
        self.bridge = bridge or NotionBridge(cache_path=self.store.root / "connection.json")
        self.lock = threading.RLock()
        self.running = set()
        # A restart never silently retries a cloud write. The next click searches
        # by the saved page/name and recovers the same receipt.
        for store in (self.store, self.photo_store):
            with self.transaction(store) as data:
                for row in data["uploads"].values():
                    if row.get("status") == "running":
                        row.update(status="interrupted", error="工作台已重启；重试会先查找已有 Notion 页面。")

    def transaction(self, store=None):
        from contextlib import contextmanager
        store = store or self.store
        @contextmanager
        def edit():
            with self.lock, file_lock(store.path.with_suffix(".lock"), "Notion 上传记录正在保存，请稍后重试。"):
                data = store.read()
                yield data
                store.write(data)
        return edit()

    def _store_of(self, identifier):
        return self.photo_store if identifier.startswith("NPH-") else self.store

    def busy(self, job_id=None):
        with self.lock:
            return bool(self.running if job_id is None else any(key[0] == job_id for key in self.running))

    @staticmethod
    def public(row):
        return {key: copy.deepcopy(row[key]) for key in ("id", "job_id", "timeline_uid", "status", "progress", "error",
            "page_url", "page_id", "name", "published_on", "account", "title", "updated_at", "warning",
            "feishu_status", "feishu_error", "feishu_record_id") if key in row}

    def latest(self, job_id, uid):
        rows = [row for row in self.store.read()["uploads"].values() if row["job_id"] == job_id and row["timeline_uid"] == uid]
        return max(rows, key=lambda row: row.get("updated_at", ""), default={})

    def detail(self, job_id, uid):
        job = self.app.production.job(job_id)
        timeline = next((row for row in active_timelines(job) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "请选择这张制作单的有效时间线。")
        previous = self.latest(job_id, uid)
        source = self.app._video_publish_copy(job) or {}
        source.update(en="\n".join(line["spoken"] for line in job["lines"]),
                      zh="\n".join(line.get("zh", "") for line in job["lines"]).strip() or source.get("zh", ""))
        if source.get("titles") and hasattr(self.app, "_title_translations"):
            from workbench_video_publish import title_headline
            known = self.app._title_translations()
            source["titles_zh"] = [known.get(title_headline(line), "") for line in source["titles"]]
        saved = (previous.get("source") or {})
        screen = (timeline.get("hook_overlay") or {}).get("text") or source.get("screen_en") or saved.get("screen_en", "")
        options = source.get("screen_options") or saved.get("screen_options") or []
        source["screen_options"] = options
        source.update(screen_en=screen, screen_zh=next((row.get("zh", "") for row in options if row.get("en") == screen),
            source.get("screen_zh") or saved.get("screen_zh", "")))
        # The newest export wins: the timeline's latest preview capture, a manual export, or the last uploaded file.
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", timeline.get("name", ""))
        candidates = [(previous.get("file") or {}).get("original_path", ""), self.app.production.root / "exports" / (safe + ".mp4")]
        previews = getattr(self.app, "video_previews", None)
        if previews:
            try:
                candidates.append(previews.export_file(job_id, uid))
            except (VoiceError, OSError, ValueError):
                pass
        found = []
        for item in candidates:
            try:
                if item and Path(item).is_file():
                    found.append((Path(item).stat().st_mtime, str(Path(item))))
            except OSError:
                pass
        file = max(found)[1] if found else ""
        primary = (list(reversed(active_timelines(job))) or [{}])[0].get("unique_id")
        content_id = "video:" + job_id + ("_tl_" + re.sub(r"[^A-Za-z0-9-]", "", uid)[:120] if uid != primary else "")
        calendar = self.app.content_calendar({"id": [content_id]})
        naming = (timeline.get("name", ""), job.get("market"))
        return {"timeline_uid": uid, "name": release_name(*naming, "video", creator=timeline.get("creator", "")),
            "file_name": release_filename(*naming, creator=timeline.get("creator", "")), "legacy_name": timeline.get("name", ""), "copy": source,
            "published_on": calendar.get("calendar_date") or previous.get("published_on") or date.today().isoformat(),
            # An account Notion said it could not find is not offered again.
            "account": ((previous.get("account") if "发布账号" not in str(previous.get("error") or "") else "")
                        or self.settings.get("accounts", {}).get(timeline.get("creator"))
                        or self._last_account(timeline.get("creator")) or timeline.get("creator") or job.get("creator", "")),
            "file_path": file, "content_id": content_id, "destination": self.settings.get("name", "TK排期计划"),
            "configured": bool(self.settings.get("data_source_id")), "receipt": self.public(previous) if previous else None,
            "blocked_reason": "AI 正在剪辑，结束后可上传成片。" if timeline_running(job, timeline) else "制作单已归档。" if job.get("archived") else ""}

    def start(self, job_id, body):
        if not isinstance(body, dict) or set(body) - {"timeline_uid", "request_id", "file_path", "published_on", "account", "title", "screen_en", "screen_zh"}:
            raise VoiceError(400, "Notion 上传参数无效。")
        request = str(body.get("request_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", request):
            raise VoiceError(400, "上传请求标识无效，请重新打开上传窗口。")
        digest = fingerprint({"job_id": job_id, **body})
        uid = body.get("timeline_uid")
        detail = self.detail(job_id, uid)
        if detail["blocked_reason"]:
            raise VoiceError(409, detail["blocked_reason"])
        if not detail["configured"]:
            raise VoiceError(409, "请先配置 Notion 发布日历地址。")
        day = body.get("published_on", "")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except (ValueError, TypeError):
            raise VoiceError(400, "请选择有效的发布日期。") from None
        values = {}
        for key, limit in (("account", 200), ("title", 2000), ("screen_en", 300), ("screen_zh", 300)):
            value = body.get(key, "")
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise VoiceError(400, "上传文案内容无效。")
            values[key] = value.strip()
        if not values["account"] or not values["title"] or not detail["copy"].get("en"):
            raise VoiceError(400, "请补齐发布账号、标题和英文文案。")
        key = (job_id, uid)
        with self.transaction() as data:
            row = next((row for row in data["uploads"].values() if row.get("request_id") == request), None)
            if row and row["request_hash"] != digest:
                raise VoiceError(409, "此请求已用于另一项上传，请重新打开窗口。")
            if row and (row["status"] == "completed" or key in self.running):
                return self.public(row)
            if key in self.running:
                raise VoiceError(409, "这条时间线正在上传，请等待完成。")
            if not row:
                previous = self.latest(job_id, uid)
                row = {"id": "NUP-" + uuid4().hex[:12], "job_id": job_id, "timeline_uid": uid,
                    "request_id": request, "request_hash": digest, "name": detail["name"], "file_name": detail["file_name"],
                    "legacy_name": detail["legacy_name"], "published_on": day,
                    **values, "source": {**detail["copy"], "screen_en": values["screen_en"], "screen_zh": values["screen_zh"]},
                    "destination_id": notion_id(self.settings["data_source_id"]), "file_path": body.get("file_path"),
                    "page_id": previous.get("page_id", ""), "page_url": previous.get("page_url", ""),
                    "feishu_record_id": previous.get("feishu_record_id", "")}
                data["uploads"][row["id"]] = row
            row.update(status="running", progress="正在准备成片和 Notion 连接。", error="", feishu_status="", feishu_error="", updated_at=now())
            self.running.add(key)
            result = self.public(row)
        threading.Thread(target=self._run, args=(row["id"],), daemon=True, name="notion-upload").start()
        return result

    def _save(self, identifier, **patch):
        with self.transaction(self._store_of(identifier)) as data:
            data["uploads"][identifier].update(patch, updated_at=now())

    def _run(self, identifier):
        row = copy.deepcopy(self.store.read()["uploads"][identifier])
        progress = lambda message: self._save(identifier, progress=message)
        try:
            media = row.get("file") or self.store.capture(row["file_path"], row["job_id"])
            self._save(identifier, file=media)
            path = upload_video(self.store.managed_path(media), self.store.root / "previews" / (identifier + ".mp4"), progress)
            progress("正在连接 Notion 并核对发布账号。")
            mime = {".mov": "video/quicktime", ".webm": "video/webm"}.get(path.suffix.lower(), "video/mp4")
            filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", row.get("file_name") or row["name"]) + path.suffix.lower()
            texts = [row["source"].get(key, "") for key in ("en", "zh", "screen_en")]
            with self.bridge.client() as client:
                self._publish(client, row, [(path, mime, filename)], body_markdown(row["source"]), texts, progress)
                warning = "；".join(getattr(client, "warnings", []))
            # Readback succeeded before either local upload marker changes.
            job = self.app.production.job(row["job_id"])
            primary = (list(reversed(active_timelines(job))) or [{}])[0].get("unique_id")
            suffix = "_tl_" + re.sub(r"[^A-Za-z0-9-]", "", row["timeline_uid"])[:120] if row["timeline_uid"] != primary else ""
            try:
                self.app.set_content_calendar({"id": "video:" + row["job_id"] + suffix, "calendar_uploaded": True, "calendar_date": row["published_on"]})
                entry = self.app._video_publications().entry(self.app._video_publications().read(), row["job_id"])
                from workbench_video_publish import timeline_records
                current = (timeline_records(entry, row["timeline_uid"]) or [None])[-1]
                if current:
                    self.app.set_video_calendar_status(row["job_id"], {"publication_id": current["id"], "calendar_uploaded": True})
            except Exception:
                warning = "；".join(filter(None, [warning, "Notion 已上传，本地发布日历标记未更新，可手动点为已上传。"]))
            feishu = self._feishu_sync(identifier, "video", row, [(path, filename)])
            self._save(identifier, status="completed", progress="已上传到 Notion。", warning=warning, **feishu)
        except Exception as exc:
            message = str(exc) if isinstance(exc, VoiceError) else "上传未完成，已有 Notion 页面会保留，重试不会主动新建副本。"
            self._save(identifier, status="failed", error=message, progress="")
        finally:
            with self.lock:
                self.running.discard((row["job_id"], row["timeline_uid"]))

    # ---------- 挂车图文: one Notion page per carousel version ----------
    def photo_latest(self, set_id, work_key):
        rows = [row for row in self.photo_store.read()["uploads"].values() if row["set_id"] == set_id and row.get("work") == work_key]
        return max(rows, key=lambda row: row.get("updated_at", ""), default={})

    def photo_busy(self, set_ids):
        with self.lock:
            return any(str(key[1]).startswith(f"photo:{set_id}:") for key in self.running for set_id in set_ids)

    def photo_receipts(self, set_ids):
        return [row for row in self.photo_store.read()["uploads"].values() if row["set_id"] in set_ids]

    def forget_photo(self, set_ids):
        """Drop the local upload receipts of deleted carousels; the pages on Notion are left alone."""
        with self.transaction(self.photo_store) as catalog:
            for key, row in list(catalog["uploads"].items()):
                if row["set_id"] in set_ids:
                    del catalog["uploads"][key]

    def photo_summary(self, set_ids=None):
        """{set_id: {work_key: status}} from each version's latest upload; status is completed / running / failed / interrupted."""
        latest = {}
        for row in sorted(self.photo_store.read()["uploads"].values(), key=lambda row: row.get("updated_at", "")):
            if set_ids is None or row["set_id"] in set_ids:
                latest.setdefault(row["set_id"], {})[row.get("work", "")] = row.get("status", "")
        return latest

    def _photo_source(self, set_id, work_key):
        """The set, the version's newest export and that export's frozen copy.json: what goes up is what was exported."""
        from photo_carousel import PhotoSetError
        photos = getattr(self.app, "photo_sets", None)
        if photos is None:
            raise VoiceError(404, "图文功能未启用。")
        try:
            data = photos._read(set_id)
        except PhotoSetError as exc:
            raise VoiceError(exc.status, str(exc)) from None
        work = next((item for item in data.get("works") or [] if item.get("key") == (work_key or (data.get("current") or ""))), None)
        if work is None and not work_key:
            work = next(iter(data.get("works") or []), None)
        if work is None:
            raise VoiceError(404, "这一版图文不存在。")
        export = (work.get("exports") or [None])[-1]
        folder = Path(export["folder"]) if export else None
        pictures = sorted((folder / "发布JPG").glob("*.jpg")) if folder and (folder / "发布JPG").is_dir() else []
        try:
            frozen = json.loads((folder / "copy.json").read_text(encoding="utf-8")) if pictures else {}
        except (OSError, ValueError):
            frozen = {}
        return data, work, export, pictures, frozen

    def photo_detail(self, set_id, work_key=""):
        data, work, export, pictures, frozen = self._photo_source(set_id, work_key)
        job = self.app.production.job(data["job_id"])
        timeline = next((row for row in job.get("timelines") or [] if row.get("unique_id") == data.get("timeline_uid")), {})
        previous = self.photo_latest(set_id, work["key"])
        video = self.latest(data["job_id"], data.get("timeline_uid"))
        creator = timeline.get("creator") or job.get("creator", "")
        from photo_carousel import fallback_titles
        # Exports made before titles existed get the same caption-derived line the carousel window shows.
        titles = frozen.get("titles") or fallback_titles(frozen.get("caption_en"))
        naming = (timeline.get("name") or data.get("timeline_name") or data["job_id"], work.get("market") or data.get("market"))
        blocked = ("制作单已归档。" if job.get("archived") else "先定稿导出这一版，再上传到 Notion。" if not export
                   else "这一版的成品文件夹不在了，请再导出一次。" if not pictures or not frozen else "")
        return {"set_id": set_id, "work": work["key"], "work_name": work.get("name_zh", ""),
            "name": release_name(*naming, "photo", work["key"], creator), "file_name": release_filename(*naming, work["key"], creator),
            "legacy_name": f"{naming[0]} 图文{work['key']}",
            "export_name": (export or {}).get("name", ""), "pictures": len(pictures),
            "copy": {"titles": titles, "titles_zh": frozen.get("titles_zh") or [], "caption_en": frozen.get("caption_en", ""), "caption_zh": frozen.get("caption_zh", "")},
            "published_on": previous.get("published_on") or video.get("published_on") or date.today().isoformat(),
            "account": ((previous.get("account") if "发布账号" not in str(previous.get("error") or "") else "")
                        or self.settings.get("accounts", {}).get(creator) or video.get("account")
                        or self._last_account(creator) or creator),
            "title": previous.get("title") or (titles[0] if titles else ""),
            "destination": self.settings.get("name", "TK排期计划"), "configured": bool(self.settings.get("data_source_id")),
            "receipt": self.public(previous) | {"export_name": previous.get("export_name", "")} if previous else None,
            "blocked_reason": blocked}

    def photo_start(self, set_id, body):
        if not isinstance(body, dict) or set(body) - {"work", "request_id", "published_on", "account", "title"}:
            raise VoiceError(400, "图文上传参数无效。")
        request = str(body.get("request_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,120}", request):
            raise VoiceError(400, "上传请求标识无效，请刷新后重试。")
        work_key = body.get("work")
        if not isinstance(work_key, str) or not re.fullmatch(r"[A-Z](?:-[A-Z]{2})?", work_key):  # A-DE：外语版
            raise VoiceError(400, "请选择要上传的图文版本。")
        self.app.photo_sets.ensure_open(set_id)
        detail = self.photo_detail(set_id, work_key)
        if detail["blocked_reason"]:
            raise VoiceError(409, detail["blocked_reason"])
        if not detail["configured"]:
            raise VoiceError(409, "请先配置 Notion 发布日历地址。")
        day = body.get("published_on", "")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError()
        except (ValueError, TypeError):
            raise VoiceError(400, "请选择有效的发布日期。") from None
        values = {}
        for key, limit in (("account", 200), ("title", 2000)):
            value = body.get(key, "")
            if not isinstance(value, str) or len(value) > limit or "\x00" in value:
                raise VoiceError(400, "上传文案内容无效。")
            values[key] = value.strip()
        if not values["account"] or not values["title"]:
            raise VoiceError(400, "请补齐发布账号和标题。")
        data, work, export, pictures, frozen = self._photo_source(set_id, work_key)
        if any(path.stat().st_size > UPLOAD_LIMIT for path in pictures):
            raise VoiceError(409, "有图片超过 20 MiB，Notion 收不下，请再导出一次。")
        digest = fingerprint({"set_id": set_id, **body})
        key = (data["job_id"], f"photo:{set_id}:{work_key}")
        with self.transaction(self.photo_store) as catalog:
            row = next((row for row in catalog["uploads"].values() if row.get("request_id") == request), None)
            if row and row["request_hash"] != digest:
                raise VoiceError(409, "此请求已用于另一项上传，请刷新后重试。")
            if row and (row["status"] == "completed" or key in self.running):
                return self.public(row) | {"export_name": row.get("export_name", "")}
            if key in self.running:
                raise VoiceError(409, "这一版图文正在上传，请等待完成。")
            if not row:
                previous = self.photo_latest(set_id, work_key)
                row = {"id": "NPH-" + uuid4().hex[:12], "job_id": data["job_id"], "timeline_uid": data.get("timeline_uid", ""),
                    "set_id": set_id, "work": work_key, "request_id": request, "request_hash": digest, "name": detail["name"],
                    "file_name": detail["file_name"], "legacy_name": detail["legacy_name"],
                    "published_on": day, **values, "export_name": export["name"], "folder": export["folder"],
                    "source": {key: frozen.get(key) for key in ("caption_en", "caption_zh", "titles", "slides", "market")},
                    "destination_id": notion_id(self.settings["data_source_id"]),
                    "page_id": previous.get("page_id", ""), "page_url": previous.get("page_url", ""),
                    "feishu_record_id": previous.get("feishu_record_id", "")}
                catalog["uploads"][row["id"]] = row
            row.update(status="running", progress="正在准备图片和 Notion 连接。", error="", feishu_status="", feishu_error="", updated_at=now())
            self.running.add(key)
            result = self.public(row) | {"export_name": row["export_name"]}
        threading.Thread(target=self._photo_run, args=(row["id"],), daemon=True, name="notion-photo-upload").start()
        return result

    def _photo_run(self, identifier):
        row = copy.deepcopy(self.photo_store.read()["uploads"][identifier])
        progress = lambda message: self._save(identifier, progress=message)
        try:
            pictures = sorted((Path(row["folder"]) / "发布JPG").glob("*.jpg"))
            if not pictures:
                raise VoiceError(409, "这一版的成品文件夹不在了，请再导出一次。")
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", row.get("file_name") or row["name"])
            files = [(path, "image/jpeg", f"{safe}-{index:02d}.jpg") for index, path in enumerate(pictures, 1)]
            progress("正在连接 Notion 并核对发布账号。")
            with self.bridge.client() as client:
                self._publish(client, row, files, photo_markdown(row["source"]), [row["source"].get("caption_en", ""), row["source"].get("caption_zh", "")], progress)
                warning = "；".join(getattr(client, "warnings", []))
            feishu = self._feishu_sync(identifier, "photo", row, [(path, name) for path, _mime, name in files])
            self._save(identifier, status="completed", progress="已上传到 Notion。", warning=warning, **feishu)
        except Exception as exc:
            message = str(exc) if isinstance(exc, VoiceError) else "上传未完成，已有 Notion 页面会保留，重试不会主动新建副本。"
            self._save(identifier, status="failed", error=message, progress="")
        finally:
            with self.lock:
                self.running.discard((row["job_id"], f"photo:{row['set_id']}:{row['work']}"))

    def _feishu_sync(self, identifier, kind, row, files):
        """Notion 已成功后写飞书；返回要存进上传记录的 feishu_* 字段。飞书出什么错都不改 Notion 的结果。"""
        if not self.feishu or not self.feishu.enabled:
            return {}
        self._save(identifier, progress="Notion 已上传，正在同步到飞书。")
        try:
            job = self.app.production.job(row["job_id"])
            timeline = next((line for line in job.get("timelines") or [] if line.get("unique_id") == row.get("timeline_uid")), {})
            saved = self._store_of(identifier).read()["uploads"].get(identifier, {})
            current = {**row, "feishu_record_id": saved.get("feishu_record_id") or row.get("feishu_record_id", "")}
            result = self.feishu.sync(kind, current, files, creator=timeline.get("creator") or job.get("creator", ""),
                                      product=job.get("product", ""),
                                      on_record=lambda record_id: self._save(identifier, feishu_record_id=record_id))
            return {"feishu_status": result["status"], "feishu_record_id": result["record_id"], "feishu_error": result["warning"]}
        except Exception as exc:
            return {"feishu_status": "failed",
                    "feishu_error": str(exc) if isinstance(exc, FeishuError) else "飞书同步没完成，重新上传会再试。"}

    # Account pages rarely change, so a confirmed match is kept locally and reused without a SQL query.
    def _last_account(self, creator):
        """The account this creator's most recent successful upload went to: once one upload names it, the next
        upload of the same creator starts from it instead of the creator's short name."""
        if not creator:
            return ""
        rows = sorted((row for row in self.store.read()["uploads"].values() if row.get("status") == "completed" and row.get("account")),
                      key=lambda row: row.get("updated_at", ""), reverse=True)
        jobs = None
        for row in rows:
            owner = row.get("creator")
            if owner is None:
                if jobs is None:
                    jobs = {job["id"]: job for job in self.app.production.records()["jobs"]}
                line = next((item for item in (jobs.get(row["job_id"]) or {}).get("timelines", []) if item.get("unique_id") == row["timeline_uid"]), {})
                owner = line.get("creator")
            if owner == creator:
                return row["account"]
        return ""

    def _accounts_path(self):
        return self.store.root / "accounts.json"

    def _accounts(self):
        try:
            data = json.loads(self._accounts_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _known_account(self, source, name):
        url = self._accounts().get(source, {}).get(name)
        return url if isinstance(url, str) and url.startswith("https://") else None

    def _write_accounts(self, data):
        path = self._accounts_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            pass

    def _remember_account(self, source, name, url):
        with self.lock:
            data = self._accounts()
            data.setdefault(source, {})[name] = url
            self._write_accounts(data)

    def _forget_account(self, source, name):
        with self.lock:
            data = self._accounts()
            if data.get(source, {}).pop(name, None) is not None:
                self._write_accounts(data)

    def _publish(self, client, row, files, markdown, texts, progress):
        used = {}
        try:
            self._write_page(client, row, files, markdown, texts, progress, used)
        except Exception as exc:
            # A remembered account may have been removed or renamed in Notion; the next try looks it up again.
            # An exhausted quota or a dropped connection says nothing about the account, so it stays.
            if used.get("key") and not used.get("keep") and getattr(exc, "status", None) not in {429, 503}:
                self._forget_account(*used["key"])
            raise

    def _write_page(self, client, row, files, markdown, texts, progress, used):
        """files: [(path, mime, filename)] for 「文件和媒体」; texts: body strings the readback must contain."""
        destination = client.call("fetch", {"id": "collection://" + row["destination_id"]})
        # Fetch and use the actual schemas before querying any properties.
        state = re.search(r"<data-source-state>\s*(.*?)\s*</data-source-state>", destination.get("text", ""), re.S)
        state = json.loads(state[1]) if state else {}
        schema, table = state.get("schema", {}), state.get("url", "")
        if not table or notion_id(table) != row["destination_id"]:
            raise VoiceError(409, "Notion 发布日历地址与配置不一致。")
        # 「账号」 links to the full account list (账号列表（总制作）); older calendars only have 「发布账号」.
        field = next((name for name in ACCOUNT_FIELDS if (schema.get(name) or {}).get("dataSourceUrl")),
                     next((name for name in ACCOUNT_FIELDS if name in schema), ""))
        required = {"名称", "文件和媒体", "日期", "标题＋tags"}
        if not field or not required.issubset(schema):
            raise VoiceError(409, "Notion 发布日历缺少名称、日期、账号或文件字段，请核对目标数据库。")
        account_source = schema[field].get("dataSourceUrl")
        if not account_source:
            raise VoiceError(409, "Notion 发布账号关联没有配置账号列表。")
        checked = []
        def can_query():
            # SQL queries draw on a small hourly Notion allowance, so they run only when needed.
            if not checked:
                access = client.call("get_tool_access", {"tool_names": ["query_data_sources"]})
                status = str(access.get("current_tool_access", {}).get("query_data_sources", {}).get("status") or "")
                if status not in {"available", "available_with_limit"}:
                    # A failed precheck says nothing about the remembered account, so it is kept.
                    used["keep"] = True
                    print(f"Notion 查询预检未通过：status={status or '空'}", file=sys.stderr, flush=True)
                    if not status or "limit" in status.casefold() or "usage" in status.casefold() or "quota" in status.casefold():
                        raise VoiceError(429, query_allowance_message())
                    raise VoiceError(409, f"Notion 连接暂不能查询账号（Notion 返回：{status}），请检查连接权限。")
                checked.append(True)
        account_key = (notion_id(account_source), row["account"].strip().lstrip("@").casefold())
        remembered = self._known_account(*account_key)
        if remembered:
            account = [{"url": remembered}]
            used["key"] = account_key
        else:
            account_schema = client.call("fetch", {"id": account_source})
            account_state = re.search(r"<data-source-state>\s*(.*?)\s*</data-source-state>", account_schema.get("text", ""), re.S)
            account_state = json.loads(account_state[1]) if account_state else {}
            if not {"名称", "Username", "Nick Name"}.issubset(account_state.get("schema", {})):
                raise VoiceError(409, f"Notion 账号列表缺少用户名字段，请核对「{field}」关联。")
            can_query()
            account = client.call("query_data_sources", {"data": {"mode": "sql", "data_source_urls": [account_source],
                # Usernames are stored with or without a leading @, so both spellings match.
                "query": f'SELECT url, "名称", "Username", "Nick Name" FROM "{account_source}" WHERE lower("Username") IN (?, ?) OR lower("名称") = ? OR lower("Nick Name") IN (?, ?) LIMIT 3',
                "params": (lambda bare: [bare, "@" + bare, row["account"].strip().casefold(), bare, "@" + bare])(account_key[1])}}).get("results", [])
            if len(account) != 1:
                raise VoiceError(409, "Notion 中未找到唯一的发布账号，请填写账号列表里的用户名后重试。")
            self._remember_account(*account_key, account[0]["url"])
        if not row.get("page_id"):
            can_query()
            found = client.call("query_data_sources", {"data": {"mode": "sql", "data_source_urls": [table],
                "query": f'SELECT url, "名称" FROM "{table}" WHERE "名称" IN (?, ?) LIMIT 3',
                "params": [row["name"], row.get("legacy_name") or row["name"]]}}).get("results", [])
            if len(found) > 1:
                raise VoiceError(409, "Notion 中有多条同名成片，请先保留唯一的对应记录再上传。")
            if found:
                row.update(page_id=notion_id(found[0]["url"]), page_url=found[0]["url"])
                self._save(row["id"], page_id=row["page_id"], page_url=row["page_url"])
        properties = {"名称": row["name"], "标题＋tags": row["title"], "date:日期:start": row["published_on"],
            "date:日期:is_datetime": 0, field: json.dumps([account[0]["url"]])}
        if row.get("page_id"):
            existing = client.call("fetch", {"id": row["page_id"]})
            parent = re.search(r'<parent-data-source url="([^"]+)"', existing.get("text", ""))
            if not parent or notion_id(parent[1]) != row["destination_id"]:
                raise VoiceError(409, "原 Notion 页面已移动到其他数据库，未覆盖内容。")
            # Re-uploading never moves a page back: a status someone already set (e.g. 已发布) stays.
            if STATUS_FIELD in schema and not page_properties(existing).get(STATUS_FIELD):
                properties[STATUS_FIELD] = STATUS_READY
        else:
            if STATUS_FIELD in schema:
                properties[STATUS_FIELD] = STATUS_READY
            created = client.call("create_pages", {"parent": {"data_source_id": row["destination_id"]},
                "pages": [{"properties": properties, "content": markdown}]})
            pages = created.get("pages") or []
            if len(pages) != 1:
                raise VoiceError(502, "Notion 页面创建结果未确认，请重试以查找已有页面。")
            row.update(page_id=notion_id(pages[0]["id"]), page_url=pages[0]["url"])
            self._save(row["id"], page_id=row["page_id"], page_url=row["page_url"])
        sent = []
        for index, (path, mime, filename) in enumerate(files, 1):
            progress("正在上传视频到 Notion。" if mime.startswith("video/") else f"正在上传第 {index}/{len(files)} 张图到 Notion。")
            upload = client.call("create_file_upload", {"filename": filename, "content_type": mime})
            sent.append(send_video(upload, path)["file_upload_id"])
        client.call("update_page", {"page_id": row["page_id"], "command": "update_properties", "properties": {
            **properties, "文件和媒体": [{"type": "file_upload", "file_upload": {"id": uid}} for uid in sent]}})
        client.call("update_page", {"page_id": row["page_id"], "command": "replace_content", "new_str": markdown})
        progress("正在确认 Notion 的文件、日期和文案。")
        saved = client.call("fetch", {"id": row["page_id"]})
        actual = page_properties(saved)
        normalized = re.sub(r"\s+", " ", saved.get("text", ""))
        copy_matches = all(re.sub(r"\s+", " ", text) in normalized for text in texts if text)
        if (actual.get("date:日期:start") != row["published_on"] or actual.get("标题＋tags") != row["title"]
                or not actual.get("文件和媒体") or not copy_matches
                or not any(notion_id(url) == notion_id(account[0]["url"]) for url in actual.get(field, []))):
            raise VoiceError(502, "Notion 页面已保留，但日期、账号、文件或文案尚未核对一致，请重试。")
        self._save(row["id"], page_id=row["page_id"], page_url=saved.get("url") or row["page_url"])
