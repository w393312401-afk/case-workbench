"""Upload a chosen timeline's exported video and publishing copy to Notion.

Receipts and managed media live under production/notion-uploads. Uploading is
independent of registering a TikTok post and never changes a script to published.
"""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
from uuid import uuid4

from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from notion_bridge import NotionBridge, UPLOAD_LIMIT, notion_id, page_properties, send_video
from production_operations import file_lock
from production_store import active_timelines, timeline_running
from workbench_video_publish import VideoPublicationStore


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


class NotionUploads:
    def __init__(self, workbench, settings=None, bridge=None):
        self.app = workbench
        self.settings = copy.deepcopy(settings or {})
        self.store = NotionUploadStore(workbench.production.root)
        self.bridge = bridge or NotionBridge()
        self.lock = threading.RLock()
        self.running = set()
        # A restart never silently retries a cloud write. The next click searches
        # by the saved page/name and recovers the same receipt.
        with self.transaction() as data:
            for row in data["uploads"].values():
                if row.get("status") == "running":
                    row.update(status="interrupted", error="工作台已重启；重试会先查找已有 Notion 页面。")

    def transaction(self):
        from contextlib import contextmanager
        @contextmanager
        def edit():
            with self.lock, file_lock(self.store.root / "catalog.lock", "Notion 上传记录正在保存，请稍后重试。"):
                data = self.store.read()
                yield data
                self.store.write(data)
        return edit()

    def busy(self, job_id=None):
        with self.lock:
            return bool(self.running if job_id is None else any(key[0] == job_id for key in self.running))

    @staticmethod
    def public(row):
        return {key: copy.deepcopy(row[key]) for key in ("id", "job_id", "timeline_uid", "status", "progress", "error",
            "page_url", "page_id", "name", "published_on", "account", "title", "updated_at", "warning") if key in row}

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
        saved = (previous.get("source") or {})
        screen = (timeline.get("hook_overlay") or {}).get("text") or source.get("screen_en") or saved.get("screen_en", "")
        options = source.get("screen_options") or saved.get("screen_options") or []
        source["screen_options"] = options
        source.update(screen_en=screen, screen_zh=next((row.get("zh", "") for row in options if row.get("en") == screen),
            source.get("screen_zh") or saved.get("screen_zh", "")))
        file = (previous.get("file") or {}).get("original_path", "")
        if not file or not Path(file).is_file():
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", timeline.get("name", ""))
            path = self.app.production.root / "exports" / (safe + ".mp4")
            file = str(path) if path.is_file() else ""
        primary = (list(reversed(active_timelines(job))) or [{}])[0].get("unique_id")
        content_id = "video:" + job_id + ("_tl_" + re.sub(r"[^A-Za-z0-9-]", "", uid)[:120] if uid != primary else "")
        calendar = self.app.content_calendar({"id": [content_id]})
        return {"timeline_uid": uid, "name": timeline.get("name", ""), "copy": source,
            "published_on": calendar.get("calendar_date") or previous.get("published_on") or date.today().isoformat(),
            "account": previous.get("account") or self.settings.get("accounts", {}).get(timeline.get("creator"), timeline.get("creator") or job.get("creator", "")),
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
                    "request_id": request, "request_hash": digest, "name": detail["name"], "published_on": day,
                    **values, "source": {**detail["copy"], "screen_en": values["screen_en"], "screen_zh": values["screen_zh"]},
                    "destination_id": notion_id(self.settings["data_source_id"]), "file_path": body.get("file_path"),
                    "page_id": previous.get("page_id", ""), "page_url": previous.get("page_url", "")}
                data["uploads"][row["id"]] = row
            row.update(status="running", progress="正在准备成片和 Notion 连接。", error="", updated_at=now())
            self.running.add(key)
            result = self.public(row)
        threading.Thread(target=self._run, args=(row["id"],), daemon=True, name="notion-upload").start()
        return result

    def _save(self, identifier, **patch):
        with self.transaction() as data:
            data["uploads"][identifier].update(patch, updated_at=now())

    def _run(self, identifier):
        row = copy.deepcopy(self.store.read()["uploads"][identifier])
        progress = lambda message: self._save(identifier, progress=message)
        try:
            media = row.get("file") or self.store.capture(row["file_path"], row["job_id"])
            self._save(identifier, file=media)
            path = upload_video(self.store.managed_path(media), self.store.root / "previews" / (identifier + ".mp4"), progress)
            progress("正在连接 Notion 并核对发布账号。")
            with self.bridge.client() as client:
                self._publish(client, row, path, progress)
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
            self._save(identifier, status="completed", progress="已上传到 Notion。", warning=warning)
        except Exception as exc:
            message = str(exc) if isinstance(exc, VoiceError) else "上传未完成，已有 Notion 页面会保留，重试不会主动新建副本。"
            self._save(identifier, status="failed", error=message, progress="")
        finally:
            with self.lock:
                self.running.discard((row["job_id"], row["timeline_uid"]))

    def _publish(self, client, row, path, progress):
        destination = client.call("fetch", {"id": "collection://" + row["destination_id"]})
        # Fetch and use the actual schemas before querying any properties.
        state = re.search(r"<data-source-state>\s*(.*?)\s*</data-source-state>", destination.get("text", ""), re.S)
        state = json.loads(state[1]) if state else {}
        schema, table = state.get("schema", {}), state.get("url", "")
        if not table or notion_id(table) != row["destination_id"]:
            raise VoiceError(409, "Notion 发布日历地址与配置不一致。")
        required = {"名称", "文件和媒体", "日期", "标题＋tags", "发布账号"}
        if not required.issubset(schema):
            raise VoiceError(409, "Notion 发布日历缺少名称、日期、账号或文件字段，请核对目标数据库。")
        account_source = schema["发布账号"].get("dataSourceUrl")
        if not account_source:
            raise VoiceError(409, "Notion 发布账号关联没有配置账号列表。")
        account_schema = client.call("fetch", {"id": account_source})
        account_state = re.search(r"<data-source-state>\s*(.*?)\s*</data-source-state>", account_schema.get("text", ""), re.S)
        account_state = json.loads(account_state[1]) if account_state else {}
        if not {"名称", "Username", "Nick Name"}.issubset(account_state.get("schema", {})):
            raise VoiceError(409, "Notion 账号列表缺少用户名字段，请核对发布账号关联。")
        access = client.call("get_tool_access", {"tool_names": ["query_data_sources"]})
        if access.get("current_tool_access", {}).get("query_data_sources", {}).get("status") not in {"available", "available_with_limit"}:
            raise VoiceError(409, "Notion 连接暂不能查询账号，请检查连接权限。")
        account = client.call("query_data_sources", {"data": {"mode": "sql", "data_source_urls": [account_source],
            "query": f'SELECT url, "名称", "Username", "Nick Name" FROM "{account_source}" WHERE lower("Username") = ? OR lower("名称") = ? OR lower("Nick Name") = ? LIMIT 3',
            "params": [row["account"].lstrip("@").casefold()] * 3}}).get("results", [])
        if len(account) != 1:
            raise VoiceError(409, "Notion 中未找到唯一的发布账号，请填写账号列表里的用户名后重试。")
        if not row.get("page_id"):
            found = client.call("query_data_sources", {"data": {"mode": "sql", "data_source_urls": [table],
                "query": f'SELECT url, "名称" FROM "{table}" WHERE "名称" = ? LIMIT 3', "params": [row["name"]]}}).get("results", [])
            if len(found) > 1:
                raise VoiceError(409, "Notion 中有多条同名成片，请先保留唯一的对应记录再上传。")
            if found:
                row.update(page_id=notion_id(found[0]["url"]), page_url=found[0]["url"])
                self._save(row["id"], page_id=row["page_id"], page_url=row["page_url"])
        properties = {"名称": row["name"], "标题＋tags": row["title"], "date:日期:start": row["published_on"],
            "date:日期:is_datetime": 0, "发布账号": json.dumps([account[0]["url"]])}
        markdown = body_markdown(row["source"])
        if row.get("page_id"):
            existing = client.call("fetch", {"id": row["page_id"]})
            parent = re.search(r'<parent-data-source url="([^"]+)"', existing.get("text", ""))
            if not parent or notion_id(parent[1]) != row["destination_id"]:
                raise VoiceError(409, "原 Notion 页面已移动到其他数据库，未覆盖内容。")
        else:
            created = client.call("create_pages", {"parent": {"data_source_id": row["destination_id"]},
                "pages": [{"properties": properties, "content": markdown}]})
            pages = created.get("pages") or []
            if len(pages) != 1:
                raise VoiceError(502, "Notion 页面创建结果未确认，请重试以查找已有页面。")
            row.update(page_id=notion_id(pages[0]["id"]), page_url=pages[0]["url"])
            self._save(row["id"], page_id=row["page_id"], page_url=row["page_url"])
        progress("正在上传视频到 Notion。")
        mime = {".mov": "video/quicktime", ".webm": "video/webm"}.get(path.suffix.lower(), "video/mp4")
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", row["name"]) + path.suffix.lower()
        upload = client.call("create_file_upload", {"filename": filename, "content_type": mime})
        sent = send_video(upload, path)
        client.call("update_page", {"page_id": row["page_id"], "command": "update_properties", "properties": {
            **properties, "文件和媒体": [{"type": "file_upload", "file_upload": {"id": sent["file_upload_id"]}}]}})
        client.call("update_page", {"page_id": row["page_id"], "command": "replace_content", "new_str": markdown})
        progress("正在确认 Notion 的成片、日期和文案。")
        saved = client.call("fetch", {"id": row["page_id"]})
        actual = page_properties(saved)
        normalized = re.sub(r"\s+", " ", saved.get("text", ""))
        copy_matches = all(re.sub(r"\s+", " ", row["source"].get(key, "")) in normalized
                           for key in ("en", "zh", "screen_en") if row["source"].get(key))
        if (actual.get("date:日期:start") != row["published_on"] or actual.get("标题＋tags") != row["title"]
                or not actual.get("文件和媒体") or not copy_matches
                or not any(notion_id(url) == notion_id(account[0]["url"]) for url in actual.get("发布账号", []))):
            raise VoiceError(502, "Notion 页面已保留，但日期、账号、成片或文案尚未核对一致，请重试。")
        self._save(row["id"], page_id=row["page_id"], page_url=saved.get("url") or row["page_url"])
