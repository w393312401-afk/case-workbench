"""Permanently delete a 内容制作 order (a video job) together with its local files.

Archiving is not required first; orders with running work are refused. The catalog record is removed first (under the catalog revision rules), then the files;
whatever cannot be removed is reported, never silently kept as a record. DaVinci timelines, the script versions in the
creation store and the case library are not touched.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from contextlib import nullcontext

from elevenlabs_client import VoiceError
from production_operations import TERMINAL
from production_store import EDIT_ID, timeline_edits
import re

JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")


def _tree(path):
    total = files = 0
    for current, _, names in os.walk(path):
        for name in names:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
                files += 1
            except OSError:
                continue
    return total, files


def _safe_folder(path, parent):
    return path.is_dir() and not path.is_symlink() and path.resolve().parent == parent.resolve()


class OrderDeleteMixin:
    """Needs self.production, self.lock, self.activity and the editing/production mixins."""

    # -- what belongs to a video job ------------------------------------------------------------------------------
    def _job_footprint(self, job):
        root = self.production.root
        folders = []
        audio = root / "audio" / job["id"]
        if _safe_folder(audio, root / "audio"):
            folders.append(audio)
        for edit in job.get("edits", []):
            if EDIT_ID.fullmatch(str(edit.get("id"))):
                folder = root / "edits" / edit["id"]
                if _safe_folder(folder, root / "edits"):
                    folders.append(folder)
        replica = root / "replica" / job["id"]
        if _safe_folder(replica, root / "replica"):
            folders.append(replica)
        files = [path for path in (root / "handoffs").glob(f"{job['id']}-*.md") if path.is_file() and not path.is_symlink()]
        for path in (root / "operations").glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("job_id") == job["id"] and (
                    record.get("state") in TERMINAL or self._abandoned_build(record)):
                files.append(path)
        footprint = getattr(self, "video_publication_footprint", None)
        if footprint:
            publication_folders, publication_files = footprint(job["id"])
            folders.extend(publication_folders)
            files.extend(publication_files)
        return folders, files

    def _abandoned_build(self, record):
        """A build that ended before the worker journaled anything and is not waiting now: DaVinci was never touched."""
        live = getattr(self, "_timeline_building", {}).get(record.get("job_id")) or {}
        return (record.get("kind") == "timeline" and record.get("request_id") != live.get("request_id") and
                isinstance(record.get("operation_key"), str) and
                not self._production_operations().recovery_file(record).exists())

    def _job_blockers(self, job):
        with self.lock:
            self._edits()
            if job["id"] in self._voicing():
                return "这张制作单正在生成配音，请等它结束。"
            if job["id"] in getattr(self, "_timeline_building", {}):
                return "这张制作单的建时间线正在排队或进行，请等它结束或先取消排队。"
            for edit in job.get("edits", []):
                if edit.get("status") == "running" or edit["id"] in self._edit_live or edit["id"] in self._edit_pending:
                    return "这张制作单还有 AI 剪辑在运行或结果尚未保存，请等它结束。"
        if [item for item in self._production_operations().public(job["id"]) if item["kind"] != "timeline" or item.get("has_journal")]:
            return "这张制作单还有未完成的配音或建线操作，请先在制作单里处理（恢复或确认失败）后再删除。"
        return ""

    # -- shared ----------------------------------------------------------------------------------------------------
    def delete_job_order(self, identifier, body):
        guard = getattr(self, "_video_publication_job_guard", None)
        with guard(identifier) if guard else nullcontext():
            return self._delete_order("job", identifier, body)

    def _delete_order(self, kind, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(404, "这张视频制作单不存在。")
        preview = body.get("preview") is True
        if not preview:
            self._validate_edit_request(body)
            prior = self.production.replay("job_delete", {**body, "job_id": identifier})
            if prior:
                warnings = self.purge_video_publication(identifier) if hasattr(self, "purge_video_publication") else []
                return {**prior, "warnings": warnings}
        record = self.production.job(identifier)
        if not record:
            raise VoiceError(404, "这张视频制作单不存在。")
        problem = self._job_blockers(record)
        if problem:
            raise VoiceError(409, problem)
        folders, files = self._job_footprint(record)
        if preview:
            size = count = 0
            for folder in folders:
                part, number = _tree(folder)
                size, count = size + part, count + number
            for path in files:
                try:
                    size, count = size + path.lstat().st_size, count + 1
                except OSError:
                    continue
            summary = {"kind": kind, "id": identifier, "title": record.get("title", ""), "bytes": size, "files": count}
            summary.update(takes=len(record.get("takes", [])), edits=len(record.get("edits", [])),
                           timelines=len(record.get("timelines", [])))
            if hasattr(self, "_video_publications"):
                publications = self._video_publications().read()["jobs"].get(identifier, {})
                summary.update(published=bool(publications.get("publication")),
                               publications=len(publications.get("history", [])) + bool(publications.get("publication")))
            return summary
        result = self.production.mutate("job_delete", {**body, "job_id": identifier})
        warnings = []

        def unlock(function, path, _):
            os.chmod(path, 0o700)
            function(path)
        for folder in folders:
            try:
                shutil.rmtree(folder, onerror=unlock)
            except OSError as exc:
                warnings.append(f"记录已删除，但本地文件夹没有清理干净：{exc}")
            if folder.exists():
                warnings.append(f"记录已删除，但本地文件夹仍有残留，可手动删除：{folder}")
        for path in files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.append(f"记录已删除，但有文件没有清理干净：{exc}")
        if hasattr(self, "purge_video_publication"):
            warnings.extend(self.purge_video_publication(identifier))
        if hasattr(self, "delete_prepublish_reviews"):
            self.delete_prepublish_reviews(identifier)
        try:
            activity = getattr(self, "activity", None)
            if activity is not None:
                activity.delete_owner(identifier)
        except Exception:
            pass
        return {**result, "warnings": warnings}

    # -- one timeline ----------------------------------------------------------------------------------------------
    def delete_timeline(self, identifier, body):
        """Permanently delete one timeline of an order with its AI edit runs and local files; the DaVinci timeline stays.

        body.preview=true only reports what would be removed. A published timeline is refused: its video and sales
        records would lose their source."""
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(404, "这张视频制作单不存在。")
        uid = body.get("timeline_uid")
        if not isinstance(uid, str) or not uid.strip() or len(uid) > 500:
            raise VoiceError(400, "时间线标识无效。")
        guard = getattr(self, "_video_publication_job_guard", None)
        with guard(identifier) if guard else nullcontext():
            return self._delete_timeline(identifier, uid, body)

    def _timeline_footprint(self, job, timeline):
        root = self.production.root
        folders, files = [], []
        for edit in timeline_edits(job, timeline):
            if EDIT_ID.fullmatch(str(edit.get("id"))):
                folder = root / "edits" / edit["id"]
                if _safe_folder(folder, root / "edits"):
                    folders.append(folder)
        # Handoff notes are named after the order; the timeline's id inside them says which one they are for.
        for path in (root / "handoffs").glob(f"{job['id']}-*.md"):
            try:
                if path.is_file() and not path.is_symlink() and timeline["unique_id"] in path.read_text(encoding="utf-8"):
                    files.append(path)
            except (OSError, ValueError):
                continue
        key = timeline.get("operation_key")
        if key:
            for path in (root / "operations").glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (isinstance(record, dict) and record.get("kind") == "timeline" and record.get("job_id") == job["id"] and
                        record.get("operation_key") == key and (record.get("state") in TERMINAL or self._abandoned_build(record))):
                    files.append(path)
                    folder = root / "operations" / key
                    if _safe_folder(folder, root / "operations"):
                        folders.append(folder)
        return folders, files

    def _timeline_blockers(self, job, timeline):
        with self.lock:
            self._edits()
            for edit in timeline_edits(job, timeline):
                if edit.get("status") == "running" or edit["id"] in self._edit_live or edit["id"] in self._edit_pending:
                    return "这条时间线的 AI 剪辑还在运行或结果尚未保存，请等它结束。"
        if hasattr(self, "_video_publications"):
            from workbench_video_publish import timeline_records
            entry = self._video_publications().read()["jobs"].get(job["id"], {})
            if timeline_records(entry, timeline["unique_id"]):
                return "这条时间线已经登记发布，删除会让发布和销售记录失去来源，不能删除。"
        return ""

    def _delete_timeline(self, identifier, uid, body):
        preview = body.get("preview") is True
        if not preview:
            self._validate_edit_request(body)
            prior = self.production.replay("timeline_delete", {**body, "job_id": identifier})
            if prior:
                return {**prior, "warnings": []}
        job = self.production.job(identifier)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        problem = self._timeline_blockers(job, timeline)
        if problem:
            raise VoiceError(409, problem)
        folders, files = self._timeline_footprint(job, timeline)
        edit_ids = [edit["id"] for edit in timeline_edits(job, timeline)]
        if preview:
            size = count = 0
            for folder in folders:
                part, number = _tree(folder)
                size, count = size + part, count + number
            for path in files:
                try:
                    size, count = size + path.lstat().st_size, count + 1
                except OSError:
                    continue
            return {"timeline_uid": uid, "name": timeline.get("name", ""), "bytes": size, "files": count, "edits": len(edit_ids)}
        result = self.production.mutate("timeline_delete", {**body, "job_id": identifier})
        warnings = []

        def unlock(function, path, _):
            os.chmod(path, 0o700)
            function(path)
        for folder in folders:
            try:
                shutil.rmtree(folder, onerror=unlock)
            except OSError as exc:
                warnings.append(f"记录已删除，但本地文件夹没有清理干净：{exc}")
            if folder.exists():
                warnings.append(f"记录已删除，但本地文件夹仍有残留，可手动删除：{folder}")
        for path in files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                warnings.append(f"记录已删除，但有文件没有清理干净：{exc}")
        try:
            activity = getattr(self, "activity", None)
            if activity is not None:
                for source in (*edit_ids, uid):
                    activity.delete_source(identifier, source)
        except Exception:
            pass
        return {**result, "warnings": warnings}

    # -- batch -----------------------------------------------------------------------------------------------------
    BATCH_LIMIT = 100

    def batch_orders(self, body):
        """Archive, restore or permanently delete several orders; each one is judged on its own and reported.

        One order that cannot be changed (running work, not archived yet) never stops the rest. Request ids are derived
        from the batch id, so repeating the same batch after a lost response replays the saved results.
        """
        if not isinstance(body, dict):
            raise VoiceError(400, "批量操作参数格式无效。")
        action, items, batch = body.get("action"), body.get("items"), body.get("request_id")
        if action not in {"archive", "restore", "delete"}:
            raise VoiceError(400, "批量操作只支持归档、恢复和永久删除。")
        if not isinstance(batch, str) or not re.fullmatch(r"[\w-]{10,40}", batch):
            raise VoiceError(400, "缺少保存标识，请刷新后重试。")
        if not isinstance(items, list) or not items or len(items) > self.BATCH_LIMIT:
            raise VoiceError(400, f"请选择 1–{self.BATCH_LIMIT} 张制作单。")
        seen, normalized = set(), []
        for item in items:
            kind, identifier = (item or {}).get("kind") if isinstance(item, dict) else None, (item or {}).get("id") if isinstance(item, dict) else None
            if kind != "job" or not isinstance(identifier, str) or not JOB_ID.fullmatch(identifier):
                raise VoiceError(400, "所选制作单的类型或编号无效。")
            if identifier not in seen:
                seen.add(identifier)
                normalized.append((kind, identifier))
        results = []
        for kind, identifier in normalized:
            request_id = f"{batch}-{identifier}"
            entry = {"kind": kind, "id": identifier, "ok": True, "warnings": []}
            try:
                if action == "delete":
                    outcome = self.delete_job_order(identifier, {"request_id": request_id})
                    entry["warnings"] = outcome.get("warnings", [])
                else:
                    wanted = action == "archive"
                    with self.lock:
                        current = self.production.job(identifier)
                        if not current:
                            raise VoiceError(404, "这张制作单不存在。")
                        if bool(current.get("archived")) == wanted:
                            entry["skipped"] = "已经是归档状态" if wanted else "本来就没有归档"
                            results.append(entry)
                            continue
                        revision = self.production.public()["revision"]
                        self.archive_job(
                            identifier, {"request_id": request_id, "revision": revision, "archived": wanted})
            except VoiceError as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
        changed = sum(1 for item in results if item["ok"] and not item.get("skipped"))
        return {"action": action, "results": results, "changed": changed,
                "failed": sum(1 for item in results if not item["ok"]), "skipped": sum(1 for item in results if item.get("skipped"))}
