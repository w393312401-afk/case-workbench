"""Freeze the reviewed production version and a managed copy of the video actually posted.

The catalog, recovery journals and media are business data below runtime/production.
Publication is a local registration only; this module never exports or uploads a video.
"""
from __future__ import annotations

import copy
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from campaign_store import pacific_today
from creation_store import CreationError
from direction_store import fingerprint, now
from elevenlabs_client import VoiceError
from production_operations import durable_json, file_lock
from production_store import (JOB_ID, active_timelines, current_timeline, skill_delivery_reviewable, text, timeline_manual_receipt,
                              timeline_running, timeline_task_status)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
CHUNK_BYTES = 8 * 1024 * 1024


def remap_publication_document(original, mapped, stamp, revisions):
    """Re-sign verified snapshots after the backup manager's explicit path-only rewrite.

    Called only on extracted, archive-checksummed publication JSON. Old signatures
    must verify before any replacement; the migration itself remains auditable.
    """
    if isinstance(original, list):
        for old, new in zip(original, mapped):
            remap_publication_document(old, new, stamp, revisions)
    elif isinstance(original, dict):
        for key, value in original.items():
            if key in mapped:
                remap_publication_document(value, mapped[key], stamp, revisions)
        if str(original.get("id", "")).startswith("VPU-") and "source" in original:
            before = fingerprint(original["source"])
            if original.get("source_signature") != before:
                raise VoiceError(409, "备份中的原视频发布快照签名不匹配，未重新签名。")
            after = fingerprint(mapped["source"])
            if after != before:
                mapped["source_signature"] = after
                mapped["path_migrations"] = [*mapped.get("path_migrations", []), {
                    "at": stamp, "reason": "full_backup_path_restore", "previous_source_signature": before,
                    "source_signature": after}]
        if "publication" in original and "history" in original:
            # An order's entry also keeps the posts refused for it; its revision covers them.
            old_entry = {key: original[key] for key in ("publication", "history", "dismissed") if key in original}
            new_entry = {key: mapped[key] for key in ("publication", "history", "dismissed") if key in mapped}
            revisions[fingerprint(old_entry)] = fingerprint(new_entry)
    return mapped


def remap_publication_revisions(value, revisions):
    """Keep prepared journals and saved reply revisions usable after path migration."""
    if isinstance(value, list):
        for item in value:
            remap_publication_revisions(item, revisions)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"revision", "expected_entry"} and isinstance(item, str) and item in revisions:
                value[key] = revisions[item]
            else:
                remap_publication_revisions(item, revisions)
    return value


def campaign_words(campaign):
    """The campaign's name as viewers read it (the Chinese gloss dropped) and its last day, e.g. ("Black Friday & Cyber Monday", "Nov 30")."""
    name = re.split(r"[（(]", str(campaign.get("name") or ""))[0].strip().replace(" / ", " & ")
    try:
        day = date.fromisoformat(campaign["end"])
        return name, f"{day:%b} {day.day}"
    except (KeyError, TypeError, ValueError):
        return name, ""


def campaign_card_text(campaign):
    """Two lines for the end card: the campaign, and when it ends."""
    name, end = campaign_words(campaign)
    return f"{name} sale / Ends {end}" if end else f"{name} sale"


def campaign_pin_text(campaign):
    name, end = campaign_words(campaign)
    return f"{name} sale{f' ends {end} (Pacific)' if end else ''}. Tap the cart below to grab it before then."


def title_headline(line):
    """A 🔴 title line without its marker and hashtags."""
    return re.sub(r"^🔴\s*", "", str(line or "")).split("#", 1)[0].strip()


def record_timeline(record):
    """The timeline a registration was made for, read from its frozen review version."""
    return (((record or {}).get("source") or {}).get("timeline") or {}).get("unique_id")


def timeline_records(entry, timeline_uid):
    """Registrations of one timeline, oldest first; the last one stands. Registering another timeline leaves them be."""
    return [record for record in (*entry.get("history", []), entry.get("publication")) if record and record_timeline(record) == timeline_uid]


def reviewed_source(job, timeline_uid=None):
    """Require review of the chosen timeline of the selected take (the latest by default), never an older edit."""
    if job.get("archived"):
        raise VoiceError(409, "制作单已归档，请恢复后再登记发布。")
    take = next((item for item in job.get("takes", []) if item.get("id") == job.get("selected_take")), None)
    if timeline_uid is None:
        timeline = current_timeline(job)
    else:
        timeline = next((row for row in active_timelines(job) if row.get("unique_id") == timeline_uid), None)
        if not timeline:
            raise VoiceError(409, "这条时间线已变化、已归档或不属于当前选用的配音，请刷新后重新选择。")
    # Another timeline's running edit does not hold this one back.
    if (timeline_running(job, timeline) if timeline else any(edit.get("status") == "running" for edit in job.get("edits", []))):
        raise VoiceError(409, "剪辑仍在运行，请完成当前版本的人工审查后登记。")
    if not take or not timeline or timeline.get("saved") is False:
        raise VoiceError(409, "请先选用配音、保存对应时间线并完成人工审查。")
    edit = next((item for item in reversed(job.get("edits", [])) if item.get("timeline_uid") == timeline.get("unique_id")), None)
    receipt = copy.deepcopy(timeline_manual_receipt(job, timeline))
    if not receipt:
        valid = bool(edit and edit.get("approval") and edit.get("status") in {"completed", "awaiting_review"})
        if valid and edit.get("completion_policy") == "skill_delivery":
            valid = skill_delivery_reviewable(edit)
        elif valid:
            valid = edit.get("status") == "awaiting_review"
        if not valid:
            raise VoiceError(409, "当前时间线尚未通过人工审查，请完成审查后登记发布。")
    return {"script": {key: copy.deepcopy(job.get(key)) for key in
                       ("script_id", "script_version", "script_family", "product", "direction_id", "title",
                        "variant_label", "lines", "source", "text_sha256")},
            "take": copy.deepcopy(take),
            # Organizing a timeline does not change the reviewed media. Keep
            # signatures compatible with snapshots saved before these fields
            # existed, including archive/restore and public readback defaults.
            "timeline": {key: copy.deepcopy(value) for key, value in timeline.items()
                         if key not in {"archived", "task_status", "manual_completion", "manual_completion_blocked", "subtitle_check", "hook_overlay"}},
            "edit": copy.deepcopy(edit), "manual_completion": receipt}


def _stat_signature(value):
    # Windows fstat ctime is change-time while path.stat ctime may be birth-time.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _local_video(value):
    raw = text(value, "本地成片路径", limit=2000)
    # No network paths, devices, alternate streams or file URLs. We never serve arbitrary files.
    if raw.startswith(("\\\\", "//")) or "\x00" in raw or "://" in raw:
        raise VoiceError(400, "请填写本机磁盘中视频文件的完整路径。")
    path = Path(raw)
    if not path.is_absolute() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise VoiceError(400, "请填写本机 MP4、MOV、M4V、MKV、WebM 或 AVI 视频的完整路径。")
    if os.name == "nt" and any(":" in part for part in path.parts[1:]):
        raise VoiceError(400, "成片路径不能包含设备或备用数据流。")
    try:
        for item in (path, *path.parents):
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise VoiceError(400, "成片路径不能使用符号链接或目录链接，请填写视频实际路径。")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size < 12:
            raise VoiceError(400, "成片不是有效的本地视频文件。")
        return path.resolve(strict=True)
    except OSError as exc:
        raise VoiceError(400, "本地成片无法读取，请核对路径和文件权限。") from exc


def _calendar_flag(value):
    # Whether the post is already on the publish calendar kept outside the workbench.
    if type(value) is not bool:
        raise VoiceError(400, "发布日历状态须为已上传或未上传。")
    return value


def _video_header(header, suffix):
    if suffix in {".mp4", ".mov", ".m4v"}:
        return header[4:8] in {b"ftyp", b"moov", b"mdat", b"wide", b"free", b"skip"}
    if suffix in {".mkv", ".webm"}:
        return header[:4] == b"\x1aE\xdf\xa3"
    return header[:4] == b"RIFF" and header[8:12] == b"AVI "


class VideoPublicationStore:
    def __init__(self, production_root):
        self.root = Path(production_root) / "video-publications"
        self.path = self.root / "catalog.json"

    def read(self):
        if not self.path.exists():
            return {"schema_version": 1, "jobs": {}, "requests": {}}
        try:
            result = json.loads(self.path.read_text(encoding="utf-8"))
            if result.get("schema_version") != 1 or not isinstance(result.get("jobs"), dict) or not isinstance(result.get("requests"), dict):
                raise ValueError()
            for job_id, entry in result["jobs"].items():
                if not JOB_ID.fullmatch(job_id) or not isinstance(entry, dict) or not isinstance(entry.get("history"), list):
                    raise ValueError()
                for record in [entry.get("publication"), *entry["history"]]:
                    if record is None:
                        continue
                    if (not isinstance(record, dict) or record.get("job_id") != job_id or
                            not re.fullmatch(r"VPU-[0-9a-f]{12}", str(record.get("id"))) or
                            not isinstance(record.get("source"), dict) or
                            record.get("source_signature") != fingerprint(record["source"])):
                        raise ValueError()
                    # The video ID alone registers a post; a bound copy of the exported file is optional.
                    media = record.get("file")
                    if media is None:
                        continue
                    if (not isinstance(media, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(media.get("sha256"))) or
                            type(media.get("size")) is not int or media["size"] < 12):
                        raise ValueError()
                    if not str(media.get("managed_path", "")).startswith("files/" + job_id + "/" + media["sha256"] + "."):
                        raise ValueError()
                    self.managed_path(media)
            return result
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise VoiceError(500, "视频发布资料无法读取，请保留原文件并从备份恢复。") from exc

    def validate(self, *, check_files=False):
        """Restore preflight: validate persisted structure; optionally hash every referenced video."""
        data = self.read()
        if check_files:
            checked = set()
            for entry in data["jobs"].values():
                for record in [entry.get("publication"), *entry["history"]]:
                    if record and record.get("file") and record["file"]["managed_path"] not in checked:
                        if not self.check(record["file"])["matches"]:
                            raise VoiceError(409, "备份中的视频成片与发布记录不一致。")
                        checked.add(record["file"]["managed_path"])
        return data

    def write(self, data):
        if self.path.exists():
            old = self.read()
            backup = self.root / "backups" / (fingerprint(old) + ".json")
            if not backup.exists():
                durable_json(backup, old)
        durable_json(self.path, data)

    @staticmethod
    def entry(data, identifier):
        return data["jobs"].get(identifier, {"publication": None, "history": []})

    @staticmethod
    def revision(entry):
        return fingerprint(entry)

    def managed_path(self, media):
        relative = str(media.get("managed_path") or "")
        if not re.fullmatch(r"files/PRD-[0-9a-f]{12}/[0-9a-f]{64}\.(?:mp4|mov|m4v|mkv|webm|avi)", relative):
            raise VoiceError(409, "托管成片路径无效，请保留发布记录并核对备份。")
        path = self.root / relative
        try:
            if (not path.resolve().is_relative_to(self.root.resolve()) or
                    not path.resolve().is_relative_to((self.root / "files").resolve()) or path.is_symlink() or path.parent.is_symlink()):
                raise VoiceError(409, "托管成片路径已变化，请保留发布记录并核对备份。")
        except OSError as exc:
            raise VoiceError(409, "托管成片无法读取。") from exc
        return path

    def capture(self, filename, job_id):
        source = _local_video(filename)
        folder = self.root / "files" / job_id
        folder.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".copy-", suffix=".tmp", dir=folder)
        try:
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output, source.open("rb") as stream:
                before = os.fstat(stream.fileno())
                header = stream.read(32)
                if not _video_header(header, source.suffix.lower()):
                    raise VoiceError(400, "文件内容与视频格式不符，请选择实际导出的成片。")
                output.write(header)
                digest.update(header)
                while chunk := stream.read(CHUNK_BYTES):
                    output.write(chunk)
                    digest.update(chunk)
                output.flush()
                os.fsync(output.fileno())
                if _stat_signature(before) != _stat_signature(os.fstat(stream.fileno())) or _stat_signature(before) != _stat_signature(source.stat()):
                    raise VoiceError(409, "视频文件在复制期间发生变化，请等待导出完成后重新登记。")
            sha = digest.hexdigest()
            relative = "files/" + job_id + "/" + sha + source.suffix.lower()
            # Replacing a same-content target is atomic; no consumer sees a partial copy.
            os.replace(temporary, self.root / relative)
            return {"original_path": str(source), "name": source.name, "managed_path": relative,
                    "sha256": sha, "size": before.st_size, "captured_at": now()}
        except OSError as exc:
            raise VoiceError(503, "复制成片未完成，请核对文件权限与磁盘空间后用原请求重试。") from exc
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def check(self, media):
        path = self.managed_path(media)
        try:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise VoiceError(409, "托管成片不是普通文件。")
                while chunk := stream.read(CHUNK_BYTES):
                    digest.update(chunk)
                if _stat_signature(before) != _stat_signature(os.fstat(stream.fileno())) or _stat_signature(before) != _stat_signature(path.stat()):
                    raise VoiceError(409, "托管成片在核对期间发生变化，请核对文件后重试。")
            return {"matches": digest.hexdigest() == media.get("sha256") and before.st_size == media.get("size"),
                    "checked_at": now(), "actual_sha256": digest.hexdigest()}
        except OSError as exc:
            raise VoiceError(409, "托管成片缺失或无法读取，请从完整备份恢复。") from exc


class VideoPublishMixin:
    def _video_publications(self):
        return VideoPublicationStore(self.production.root)

    def video_publication_for_script(self, script_id):
        """Deleted jobs retain historical media but do not forever lock the surviving script."""
        active = {job["id"] for job in self.production.public()["jobs"]}
        for job_id, entry in self._video_publications().read()["jobs"].items():
            item = entry.get("publication") or {}
            if job_id in active and item.get("script_id") == script_id:
                return copy.deepcopy(item)
        return None

    def script_registrations(self, script_id):
        """Distinct posts registered for one script across video and picture orders, earliest first."""
        first = {}

        def note(record):
            if record and record.get("script_id") == script_id and record.get("post_id"):
                stamp = str(record.get("registered_at") or record.get("updated_at") or "")
                first[record["post_id"]] = min(stamp, first.get(record["post_id"], stamp))

        for entry in self._video_publications().read().get("jobs", {}).values():
            for record in (*entry.get("history", []), entry.get("publication")):
                note(record)
        return [post for post, _ in sorted(first.items(), key=lambda pair: (pair[1], pair[0]))]

    def video_publication_detail(self, identifier, timeline_uid=None):
        """The registration of one timeline (the current one unless another is named) and a line for each timeline of the order."""
        job = self.production.job(identifier)
        store = self._video_publications()
        entry = copy.deepcopy(store.entry(store.read(), identifier))
        revision = store.revision(entry)
        if timeline_uid is not None and (not isinstance(timeline_uid, str) or not timeline_uid.strip() or len(timeline_uid) > 500):
            raise VoiceError(400, "时间线标识无效。")
        uid = timeline_uid or (current_timeline(job) or {}).get("unique_id")
        try:
            source, reason = reviewed_source(job, timeline_uid), ""
        except VoiceError as exc:
            source, reason = None, str(exc)
        signature = fingerprint(source) if source else ""
        records = timeline_records(entry, uid) if uid else []
        # With several timelines, each stands on its own: one without a registration shows none, never another timeline's.
        if records or timeline_uid is not None or len(active_timelines(job)) > 1:
            shown = {"publication": records[-1] if records else None, "history": records[:-1]}
        else:
            # Nothing is registered for the current timeline: the order's latest registration shows as an earlier version.
            shown = {"publication": entry.get("publication"), "history": list(entry.get("history", []))}
        current, entry = shown["publication"], shown
        if current:
            current["source_current"] = current["source_signature"] == signature
            if not current.get("file"):
                current["file_available"] = False
            else:
                try:
                    path = store.managed_path(current["file"])
                    current["file_available"] = path.is_file() and path.stat().st_size == current["file"]["size"]
                except (VoiceError, OSError):
                    current["file_available"] = False
        if current:
            # A post that went up during a campaign: once it is over its pinned comment and end card need changing.
            campaign = self._campaign_on(current.get("published_on"))
            if campaign:
                current["campaign"] = {**campaign, "ended": campaign["end"] < pacific_today().isoformat()}
        candidates = []
        if source and not records and hasattr(self, "publication_candidates"):
            try:
                candidates = self.publication_candidates(identifier, source["timeline"].get("unique_id"))
            except (VoiceError, OSError, ValueError, KeyError):
                candidates = []
        publish_copy = self._video_publish_copy(job)
        if publish_copy:
            saved = self._title_translations()
            publish_copy["titles_zh"] = [saved.get(title_headline(line), "") for line in publish_copy["titles"]]
        # Revision excludes presentation-only readback fields.
        return {"revision": revision, "source_signature": signature, "timeline_uid": uid, "can_register": bool(source),
                "blocked_reason": reason, "source": source, "copy": publish_copy, "candidates": candidates,
                "timelines": self._notion_marked(identifier, self._video_publication_timelines(job, store.entry(store.read(), identifier))),
                "script_registrations": self.script_registrations(job["script_id"]), **entry}

    def order_states(self, identifiers):
        """What each order needs now, summed over its current timelines, for the task panel: a timeline is 完成 once
        uploaded to Notion or registered, 待登记 once its cut is finished, otherwise 制作中. Archived and replaced timelines
        are history and not counted. One read of each store serves every order."""
        wanted = {value for value in identifiers if isinstance(value, str) and JOB_ID.fullmatch(value)}
        if not wanted:
            return {}
        jobs = {job["id"]: job for job in self.production.records()["jobs"] if job["id"] in wanted}
        publications = self._video_publications().read()
        uploads = getattr(self, "notion_uploads", None)
        uploaded = set()
        if uploads is not None:
            try:
                rows = sorted(uploads.store.read()["uploads"].values(), key=lambda row: row.get("updated_at", ""))
                latest = {(row["job_id"], row["timeline_uid"]): row.get("status") for row in rows}
                uploaded = {key for key, status in latest.items() if status == "completed"}
            except Exception:
                uploaded = set()
        states = {}
        for identifier, job in jobs.items():
            if job.get("archived"):
                states[identifier] = {"label": "已归档", "tone": "muted"}
                continue
            if job.get("timeline_building") or any(edit.get("status") in {"running", "queued"} for edit in job.get("edits", [])):
                states[identifier] = {"label": "剪辑中", "tone": "running"}
                continue
            entry = VideoPublicationStore.entry(publications, identifier)
            counts = {}
            for row in active_timelines(job):
                uid = row.get("unique_id")
                if (identifier, uid) in uploaded or timeline_records(entry, uid):
                    key = "完成"
                elif timeline_task_status(job, row) == "completed":
                    key = "待登记"
                else:
                    key = "制作中"
                counts[key] = counts.get(key, 0) + 1
            if not counts:
                states[identifier] = {"label": "还没有时间线", "tone": "muted"}
                continue
            open_parts = [f"{counts[key]} 条{key}" for key in ("待登记", "制作中") if counts.get(key)]
            states[identifier] = ({"label": "完成", "tone": "success"} if not open_parts else
                                  {"label": " · ".join(open_parts), "tone": "warning" if counts.get("待登记") else "neutral"})
        return states

    def _notion_marked(self, identifier, lines):
        """Mark each timeline whose video has been uploaded to Notion: that upload is where its task ends."""
        uploads = getattr(self, "notion_uploads", None)
        for line in lines:
            try:
                line["notion_uploaded"] = bool(uploads) and uploads.latest(identifier, line["unique_id"]).get("status") == "completed"
            except Exception:
                line["notion_uploaded"] = False
        return lines

    @staticmethod
    def _video_publication_timelines(job, entry):
        """One line per timeline that can be published: whether it can be registered yet, and what stands for it."""
        lines = []
        for row in reversed(active_timelines(job)):
            uid = row["unique_id"]
            try:
                signature, ready, reason = fingerprint(reviewed_source(job, uid)), True, ""
            except VoiceError as exc:
                signature, ready, reason = "", False, str(exc)
            records = timeline_records(entry, uid)
            current = records[-1] if records else None
            lines.append({"unique_id": uid, "name": row.get("name", ""), "can_register": ready, "blocked_reason": reason,
                          "post_id": (current or {}).get("post_id"), "published_on": (current or {}).get("published_on"),
                          "registered": bool(current), "source_current": bool(current and ready and current.get("source_signature") == signature)})
        return lines

    def _campaign_now(self):
        """The campaign running today and the next one starting soon, from the trusted calendar; empty when unreadable."""
        try:
            rows = self.campaigns.for_writing() if hasattr(self, "campaigns") else []
        except Exception:
            rows = []
        pick = lambda status: next((dict(row) for row in rows if row["status"] == status), None)
        return {"running": pick("running"), "upcoming": pick("upcoming")}

    def _campaign_on(self, day):
        """The trusted campaign a post went up during, if any."""
        if not hasattr(self, "campaigns") or not isinstance(day, str):
            return None
        from campaign_store import WRITING_CONFIDENCE
        try:
            rows = self.campaigns.merged(self.campaigns.read())
        except Exception:
            return None
        return next(({key: row[key] for key in ("name", "start", "end")} for row in rows
                     if row["start"] <= day <= row["end"] and (row.get("verified") or row.get("confidence") in WRITING_CONFIDENCE)), None)

    def _campaign_copy(self, swap):
        """What the publish panel offers for 「为什么现在买」 in a campaign: the end card and the pinned comment.
        The script's own 活动版 and 置顶评论建议 come first; without them the wording is made from the calendar."""
        now = self._campaign_now()
        campaign = now["running"] or now["upcoming"]
        written = next((option for option in (swap or {}).get("options", []) if option["kind"] == "活动版"), None)
        pin = (swap or {}).get("pin") or ({"en": campaign_pin_text(campaign), "zh": ""} if campaign else None)
        return {"campaign": {**campaign, "status": "running" if now["running"] else "upcoming"} if campaign else None,
                "card_text": campaign_card_text(campaign) if campaign else "", "pin": pin, "written": written}

    def set_video_pin_status(self, identifier, body):
        """Mark that a post's campaign pinned comment (and end card) were dealt with after the campaign ended."""
        if not isinstance(body, dict) or set(body) - {"publication_id", "pin_cleared"} or not isinstance(body.get("pin_cleared"), bool):
            raise VoiceError(400, "置顶评论参数无效。")
        with self._video_publication_job_guard(identifier):
            store = self._video_publications()
            with self.lock, file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试。"):
                self.production.job(identifier)
                data = store.read()
                record = self._standing_publication(store.entry(data, identifier), body.get("publication_id"))
                if not record:
                    raise VoiceError(409, "发布记录已变化，请刷新后再标记。")
                if record.get("pin_cleared") is not body["pin_cleared"]:
                    record["pin_cleared"] = body["pin_cleared"]
                    record["pin_updated_at"] = now()
                    store.write(data)
                    self.state_cache = None
                return {"publication_id": record["id"], "pin_cleared": body["pin_cleared"],
                        "revision": store.revision(store.entry(data, identifier))}

    def _video_publish_copy(self, job):
        """Title-and-hashtag lines, Chinese text and English voice-over of the adopted script, for pasting at publish time."""
        from variant_parse import parse_variants, publish_titles, same_lines, screen_copy, screen_options, swap_notes, variant_script
        from production_store import supplement_markdown
        try:
            script = self.creation.get_script(job["script_id"])
        except (CreationError, KeyError):
            return None
        index = (job.get("source") or {}).get("variant_index")
        # A job filled in afterwards (by the AI 写稿 round or by hand) carries its own 发布文案.
        titles = (publish_titles(job.get("fill_markdown") or "", 0)
                  or publish_titles((script.get("source") or {}).get("candidate_text") or "", index)
                  or [line for line in (job.get("supplement") or {}).get("captions", []) if line.strip()])
        candidate = (script.get("source") or {}).get("candidate_text") or ""
        chosen = next((item for item in parse_variants(candidate) if type(index) is int and item["index"] == index), None)
        matching = chosen and same_lines([line["spoken"] for line in chosen["lines"]],
                                         [line["spoken"] for line in job["lines"]])
        markdown = job.get("fill_markdown") or (variant_script(candidate, index) if matching else "")
        if not markdown and (job.get("supplement") or candidate):
            markdown = supplement_markdown(job)
        screen_en, screen_zh = screen_copy(markdown)
        swap = swap_notes(job["fill_markdown"], 0) if job.get("fill_markdown") else swap_notes(candidate, index) if matching else None
        return {"script_id": script.get("id"), "version": script.get("version"), "swap": swap, **self._campaign_copy(swap),
                "titles": titles,
                "screen_en": screen_en, "screen_zh": screen_zh, "screen_options": screen_options(markdown),
                "zh": script.get("translation_zh") or ("\n".join(line["zh"] for line in job["lines"]) if all(line.get("zh") for line in job["lines"]) else ""),
                "en": script.get("text") or ""}

    def _title_translations(self):
        """Saved Chinese readings of title headlines, shared across orders. Regenerable, so an unreadable file counts as empty."""
        try:
            saved = json.loads((self._video_publications().root / "title-translations.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return saved if isinstance(saved, dict) else {}

    def translate_video_titles(self, identifier, body=None):
        """Chinese reading of each title's headline, for the eye only: tags stay out and the copied line stays foreign."""
        from case_breakdown_runner import translate_lines
        titles = (self._video_publish_copy(self.production.job(identifier)) or {}).get("titles") or []
        headlines = [title_headline(line) for line in titles]
        saved = self._title_translations()
        missing = list(dict.fromkeys(line for line in headlines if line and line not in saved))
        if missing:
            with tempfile.TemporaryDirectory() as workdir:
                chinese = translate_lines(self, missing, workdir)
            if not chinese:
                raise VoiceError(502, "标题的中文对照没生成成功，可以稍后重新打开这张制作单再试。")
            root = self._video_publications().root
            with file_lock(root / "title-translations.lock", "标题中文对照正在保存，请稍后再试。"):
                saved = {**self._title_translations(), **dict(zip(missing, chinese))}
                durable_json(root / "title-translations.json", saved)
        return {"titles_zh": [saved.get(line, "") for line in headlines]}

    def _video_publication_links(self, data, job, post, timeline_uid=None):
        """一张制作单可以绑定多个视频，但同一个帖子只能绑定一张制作单；同一张制作单里一个帖子也只属于一条时间线。"""
        for record in (*data["jobs"].get(job["id"], {}).get("history", []), (data["jobs"].get(job["id"]) or {}).get("publication")):
            if record and record.get("post_id") == post and timeline_uid and record_timeline(record) != timeline_uid:
                name = ((record.get("source") or {}).get("timeline") or {}).get("name") or "另一条时间线"
                raise VoiceError(409, f"这条帖子已登记在本制作单的时间线「{name}」上，同一个帖子只能属于一条时间线，请核对原登记。")
        for job_id, entry in data["jobs"].items():
            if job_id != job["id"] and any(record.get("post_id") == post for record in (entry.get("publication"), *entry.get("history", [])) if record):
                raise VoiceError(409, f"这条帖子已绑定到另一视频制作单（{job_id}）。一张制作单可以绑定多个视频，但同一个帖子只能绑定一张制作单，请先核对原登记。")

    @staticmethod
    def _video_post_fields(body):
        post = text(body.get("post_id"), "帖子 ID", limit=25)
        if not re.fullmatch(r"[0-9]{6,25}", post):
            raise VoiceError(400, "帖子 ID 须为 6–25 位数字。")
        published = text(body.get("published_on"), "发布日期", limit=10)
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", published):
                raise ValueError()
            date.fromisoformat(published)
        except ValueError as exc:
            raise VoiceError(400, "发布日期须为有效的 YYYY-MM-DD。") from exc
        url = text(body.get("post_url", ""), "帖子链接", False, 2000)
        if url:
            try:
                parts = urlsplit(url)
                if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
                    raise ValueError()
                linked = re.search(r"/(?:video|photo)/(\d{6,25})(?:/|$)", parts.path)
                if linked and linked[1] != post:
                    raise ValueError()
            except ValueError as exc:
                raise VoiceError(400, "帖子链接须为完整 http/https 地址，且与填写的帖子 ID 一致。") from exc
        if str(body.get("file_path") or "").strip() and body.get("file_confirmed") is not True:
            raise VoiceError(400, "请确认所选成片就是该审查版本实际发布的视频。")
        return {"post_id": post, "published_on": published, "post_url": url,
                "account": text(body.get("account", ""), "发布账号", False, 200),
                "note": text(body.get("note", ""), "发布备注", False, 2000),
                "calendar_uploaded": _calendar_flag(body.get("calendar_uploaded", False))}

    def _video_publication_job_guard(self, identifier):
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(400, "制作单标识无效。")
        return file_lock(self._video_publications().root / "job-locks" / (identifier + ".lock"),
                         "这张制作单正在登记发布或清理成片，请稍后重试。")

    def save_video_publication(self, identifier, body, auto=None):
        """auto: set only by the post matcher (never from a request), so the registration can be undone later."""
        with self._video_publication_job_guard(identifier):
            return self._save_video_publication(identifier, body, auto)

    def _save_video_publication(self, identifier, body, auto=None):
        if not JOB_ID.fullmatch(str(identifier)) or not isinstance(body, dict):
            raise VoiceError(400, "发布登记参数无效。")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[\w-]{10,100}", request_id):
            raise VoiceError(400, "缺少有效的保存标识，请刷新后重试。")
        try:
            request_hash = fingerprint([identifier, body])
        except (TypeError, ValueError) as exc:
            raise VoiceError(400, "发布登记参数无效。") from exc
        timeline_uid = body.get("timeline_uid")
        if timeline_uid is not None and (not isinstance(timeline_uid, str) or not timeline_uid.strip() or len(timeline_uid) > 500):
            raise VoiceError(400, "时间线标识无效。")
        store = self._video_publications()
        operation_path = store.root / "operations" / (hashlib.sha256(request_id.encode()).hexdigest() + ".json")
        with file_lock(operation_path.with_suffix(".lock"), "这次发布登记正在处理，请稍后用原请求重试。"):
            operation = None
            if operation_path.exists():
                try:
                    operation = json.loads(operation_path.read_text(encoding="utf-8"))
                    if operation.get("request_hash") != request_hash:
                        raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                except (OSError, ValueError, AttributeError) as exc:
                    if isinstance(exc, VoiceError):
                        raise
                    raise VoiceError(500, "发布恢复记录无法读取，请保留文件并核对备份。") from exc
            with self.lock:
                data = store.read()
                prior = data["requests"].get(request_id)
                if prior:
                    if prior["hash"] != request_hash:
                        raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                    return copy.deepcopy(prior["result"])
                job = self.production.job(identifier)
                if operation is None:
                    fields = self._video_post_fields(body)
                    source = reviewed_source(job, timeline_uid)
                    entry = store.entry(data, identifier)
                    if body.get("revision") != store.revision(entry) or body.get("source_signature") != fingerprint(source):
                        raise VoiceError(409, "制作版本或发布记录已变化，请刷新后重新核对。")
                    self._video_publication_links(data, job, fields["post_id"], source["timeline"].get("unique_id"))
            # Copy/hash large files without holding the workbench or production catalog lock.
            if operation is None:
                media = store.capture(body.get("file_path"), identifier) if str(body.get("file_path") or "").strip() else None
            else:
                if operation["publication"].get("file") and not store.check(operation["publication"]["file"])["matches"]:
                    raise VoiceError(409, "托管成片与待恢复记录不符，请从备份恢复后用原请求重试。")
            with self.lock, self.production.catalog_transaction(), file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后用原请求重试。"):
                data = store.read()
                entry = store.entry(data, identifier)
                job = self.production.job(identifier)
                if operation is None:
                    source = reviewed_source(job, timeline_uid)
                    if body.get("revision") != store.revision(entry) or body.get("source_signature") != fingerprint(source):
                        raise VoiceError(409, "复制成片期间制作版本或发布记录发生变化，请刷新后重新核对。")
                    script = self.creation.get_script(job["script_id"])
                    # Only this timeline's own earlier registration is the one being replaced.
                    previous = (timeline_records(entry, source["timeline"].get("unique_id")) or [{}])[-1]
                    # A script may be registered again under another post; its own post link keeps the first one.
                    repeat = script.get("video_id") not in {"", fields["post_id"], previous.get("post_id")}
                    if script.get("version") != job.get("script_version"):
                        raise VoiceError(409, "脚本版本与制作单不一致，未登记发布。")
                    record = {"id": "VPU-" + uuid4().hex[:12], "job_id": identifier, "script_id": job["script_id"],
                              "script_version": job["script_version"], "source": source, "source_signature": fingerprint(source),
                              "file": media, **fields, "registered_at": now()}
                    if auto:
                        record["auto"] = {**auto, "script_updated": not repeat,
                                          "script_before": {key: script.get(key) for key in ("video_id", "published_on", "status")}}
                    operation = {"request_hash": request_hash, "request_id": request_id, "repeat": repeat,
                                 "expected_entry": store.revision(entry), "publication": record,
                                 "expected_script_state": {key: script.get(key) for key in ("video_id", "published_on", "status")},
                                 "script_body": {"request_id": "videopub-" + hashlib.sha256(request_id.encode()).hexdigest()[:32],
                                                 "revision": self.creation.public()["revision"],
                                                 "script_status": {"status": "published", "video_id": fields["post_id"],
                                                                   "published_on": fields["published_on"],
                                                                   "note": f"登记视频发布；制作单 {identifier}，成片 {record['id']}"}}}
                    self._video_publication_links(data, job, record["post_id"], record_timeline(record))
                    durable_json(operation_path, operation)
                record = operation["publication"]
                if operation["expected_entry"] != store.revision(entry):
                    raise VoiceError(409, "发布记录已被另一操作修改，本次未覆盖，请核对最新登记。")
                self._video_publication_links(data, job, record["post_id"], record_timeline(record))
                if not operation.get("repeat"):
                    script_body = operation["script_body"]
                    replay_body = {**script_body, "script_status": {**script_body["script_status"], "script_id": record["script_id"]}}
                    prior_script = self.creation.replay("script_status", replay_body)
                    if prior_script:
                        current = self.creation.get_script(record["script_id"])
                        if (current.get("video_id"), current.get("published_on"), current.get("status")) not in {
                            (record["post_id"], record["published_on"], "published"),
                            (record["post_id"], record["published_on"], "reviewed")}:
                            raise VoiceError(409, "脚本发布状态已变化，未恢复旧状态，请核对最新登记。")
                    else:
                        current = self.creation.get_script(record["script_id"])
                        if {key: current.get(key) for key in ("video_id", "published_on", "status")} != operation["expected_script_state"]:
                            raise VoiceError(409, "脚本发布状态已被另一操作修改，请核对后重新登记。")
                        # An unrelated script edit after preparation need not strand an uncommitted journal.
                        script_body["revision"] = self.creation.public()["revision"]
                        operation["script_body"] = script_body
                        durable_json(operation_path, operation)
                        self.save_script_status(record["script_id"], script_body, video_job_id=identifier)
                history = copy.deepcopy(entry["history"])
                if entry.get("publication"):
                    history.append(entry["publication"])
                updated = {**entry, "publication": record, "history": history}
                data["jobs"][identifier] = updated
                result = {**copy.deepcopy(updated), "revision": store.revision(updated)}
                data["requests"][request_id] = {"hash": request_hash, "result": result}
                store.write(data)
                self.state_cache = None
                return copy.deepcopy(result)

    @staticmethod
    def _standing_publication(entry, publication_id):
        """The registration with this ID, if it is the one standing for its timeline (a replaced one is history)."""
        for record in (entry.get("publication"), *entry.get("history", [])):
            if record and record.get("id") == publication_id:
                standing = (timeline_records(entry, record_timeline(record)) or [None])[-1]
                return record if standing is record or (standing and standing.get("id") == publication_id) else None
        return None

    def edit_video_publication(self, identifier, body):
        """Correct a registration in place: its post ID, account or day. The bound script, voiceover, timeline and file
        stay as registered; each change is kept on the record, and the script's video link follows a changed post."""
        if not isinstance(body, dict) or set(body) - {"publication_id", "revision", "post_id", "account", "published_on", "post_url"}:
            raise VoiceError(400, "修改登记参数无效。")
        with self._video_publication_job_guard(identifier):
            store = self._video_publications()
            with self.lock, self.production.catalog_transaction(), file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试。"):
                job = self.production.job(identifier)
                data = store.read()
                entry = store.entry(data, identifier)
                if body.get("revision") != store.revision(entry):
                    raise VoiceError(409, "发布记录已变化，请刷新后再修改。")
                record = self._standing_publication(entry, body.get("publication_id"))
                if not record:
                    raise VoiceError(409, "只能修改每条时间线当前的登记，请刷新后重试。")
                fields = self._video_post_fields({"post_id": body.get("post_id"), "published_on": body.get("published_on"),
                                                  "account": body.get("account", record.get("account", "")), "post_url": body.get("post_url", "")})
                before = {key: record.get(key) for key in ("post_id", "account", "published_on")}
                after = {key: fields[key] for key in ("post_id", "account", "published_on")}
                if before == after and not fields["post_url"]:
                    return {**copy.deepcopy(entry), "revision": store.revision(entry)}
                if after["post_id"] != before["post_id"]:
                    self._video_publication_links(data, job, after["post_id"], record_timeline(record))
                script = self.creation.get_script(record["script_id"])
                if script and script.get("video_id") == before["post_id"]:
                    self.save_script_status(record["script_id"], {
                        "request_id": "videopub-edit-" + uuid4().hex, "revision": self.creation.public()["revision"],
                        "script_status": {"video_id": after["post_id"], "published_on": after["published_on"],
                                          "note": f"修改视频发布登记；制作单 {identifier}，成片 {record['id']}：{before['post_id']} → {after['post_id']}"}},
                        video_job_id=identifier)
                if fields["post_url"]:
                    record["post_url"] = fields["post_url"]
                elif after["post_id"] != before["post_id"]:
                    record["post_url"] = ""  # The old link points at the old post.
                record.update(after)
                record.setdefault("edits", []).append({"at": now(), "before": before, "after": after})
                store.write(data)
                self.state_cache = None
                return {**copy.deepcopy(entry), "revision": store.revision(entry)}

    def set_video_calendar_status(self, identifier, body):
        """Flip the publish-calendar mark of the current registration in place; the registration itself stays as saved."""
        if not isinstance(body, dict) or set(body) - {"publication_id", "calendar_uploaded"}:
            raise VoiceError(400, "发布日历参数无效。")
        flag = _calendar_flag(body.get("calendar_uploaded"))
        with self._video_publication_job_guard(identifier):
            store = self._video_publications()
            with self.lock, file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试。"):
                self.production.job(identifier)
                data = store.read()
                record = self._standing_publication(store.entry(data, identifier), body.get("publication_id"))
                if not record:
                    raise VoiceError(409, "发布记录已变化，请刷新后再标记发布日历。")
                if record.get("calendar_uploaded") is not flag:
                    record["calendar_uploaded"] = flag
                    record["calendar_updated_at"] = now()
                    store.write(data)
                    self.state_cache = None
                return {"publication_id": record["id"], "calendar_uploaded": flag,
                        "revision": store.revision(store.entry(data, identifier))}

    def verify_video_publication(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "核对参数无效。")
        self.production.job(identifier)
        store = self._video_publications()
        record = self._standing_publication(store.entry(store.read(), identifier), body.get("publication_id"))
        if not record:
            raise VoiceError(409, "发布记录已变化，请刷新后再核对成片。")
        if not record.get("file"):
            raise VoiceError(409, "这条发布登记只记了视频 ID，没有绑定成片文件。")
        result = store.check(record["file"])
        return {"publication_id": record["id"], **result}

    def video_publication_file(self, identifier, publication_id):
        self.production.job(identifier)
        store = self._video_publications()
        entry = store.entry(store.read(), identifier)
        record = next((item for item in [entry.get("publication"), *entry.get("history", [])]
                       if item and item.get("id") == publication_id), None)
        if not record or not record.get("file"):
            raise VoiceError(404, "这版发布成片不存在。")
        if not store.check(record["file"])["matches"]:
            raise VoiceError(409, "托管视频与发布成片不符，请从备份恢复。")
        return store.managed_path(record["file"])

    def video_publication_footprint(self, identifier):
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(400, "制作单标识无效。")
        store = self._video_publications()
        folder = store.root / "files" / identifier
        folders = [folder] if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == (store.root / "files").resolve() else []
        files = []
        for path in (store.root / "operations").glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if (record.get("publication") or {}).get("job_id") == identifier and not path.is_symlink():
                    files.append(path)
            except (OSError, ValueError, AttributeError):
                continue
        return folders, files

    def purge_video_publication(self, identifier):
        """Called under the job guard after permanent deletion; safe to retry after partial cleanup."""
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(400, "制作单标识无效。")
        store, warnings = self._video_publications(), []
        with self.lock, file_lock(store.root / "catalog.lock", "发布资料正在保存，请稍后重试清理。"):
            def scrub(data):
                data.get("jobs", {}).pop(identifier, None)
                data["requests"] = {key: value for key, value in data.get("requests", {}).items()
                                    if ((value.get("result") or {}).get("publication") or {}).get("job_id") != identifier}
                return data
            # Do not create another backup containing the just-deleted records.
            if store.path.exists():
                durable_json(store.path, scrub(store.read()))
            for path in (store.root / "backups").glob("*.json"):
                try:
                    durable_json(path, scrub(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, ValueError, AttributeError) as exc:
                    warnings.append(f"视频发布备份尚未清理：{path.name}（{exc}）")
            folders, files = self.video_publication_footprint(identifier)
            for folder in folders:
                try:
                    if folder.resolve().parent != (store.root / "files").resolve():
                        raise OSError("文件夹不在成片目录内")
                    shutil.rmtree(folder)
                except OSError as exc:
                    warnings.append(f"托管成片尚未清理：{folder}（{exc}）")
            for path in files:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    warnings.append(f"视频发布操作记录尚未清理：{path.name}（{exc}）")
        return warnings
