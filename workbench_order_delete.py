"""Permanently delete a 内容制作 order (video job or carousel) together with its local files.

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
from production_store import CAROUSEL_ID, EDIT_ID
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
    """Needs self.production, self.lock, self.activity and the editing/production/carousel mixins."""

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

    # -- carousel --------------------------------------------------------------------------------------------------
    def _carousel_blockers(self, item):
        if any(entry.get("status") == "running" for key in ("searches", "generations") for entry in item.get(key, [])):
            return "这张图文制作单还有找底图或出图任务在运行，请先停止或等它结束。"
        for slide in item.get("slides", []):
            for output in slide.get("outputs", []):
                if (output.get("review") or {}).get("status") == "running":
                    return "这张图文制作单还有成图核对在运行，请先等它结束。"
        return ""

    # -- shared ----------------------------------------------------------------------------------------------------
    def delete_job_order(self, identifier, body):
        guard = getattr(self, "_video_publication_job_guard", None)
        with guard(identifier) if guard else nullcontext():
            return self._delete_order("job", identifier, body)

    def delete_carousel_order(self, identifier, body):
        return self._delete_order("carousel", identifier, body)

    def _delete_order(self, kind, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        pattern, label = (JOB_ID, "视频制作单") if kind == "job" else (CAROUSEL_ID, "图文制作单")
        if not pattern.fullmatch(str(identifier)):
            raise VoiceError(404, f"这张{label}不存在。")
        store_kind = "job_delete" if kind == "job" else "carousel_delete"
        preview = body.get("preview") is True
        if not preview:
            self._validate_edit_request(body)
            prior = self.production.replay(store_kind, {**body, f"{kind}_id": identifier})
            if prior:
                warnings = self.purge_video_publication(identifier) if kind == "job" and hasattr(self, "purge_video_publication") else []
                return {**prior, "warnings": warnings}
        if kind == "job":
            record = self.production.job(identifier)
        else:
            record = self.carousel_detail(identifier)["carousel"]
        if not record:
            raise VoiceError(404, f"这张{label}不存在。")
        problem = self._job_blockers(record) if kind == "job" else self._carousel_blockers(record)
        if problem:
            raise VoiceError(409, problem)
        root = self.production.root
        if kind == "job":
            folders, files = self._job_footprint(record)
        else:
            folder = root / "carousels" / identifier
            folders, files = ([folder] if _safe_folder(folder, root / "carousels") else []), []
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
            if kind == "job":
                summary.update(takes=len(record.get("takes", [])), edits=len(record.get("edits", [])),
                               timelines=len(record.get("timelines", [])))
                if hasattr(self, "_video_publications"):
                    publications = self._video_publications().read()["jobs"].get(identifier, {})
                    summary.update(published=bool(publications.get("publication")),
                                   publications=len(publications.get("history", [])) + bool(publications.get("publication")))
            else:
                summary.update(slides=len(record.get("slides", [])), deliveries=len(record.get("deliveries", [])),
                               published=bool(record.get("publication")),
                               searches=len(record.get("searches", [])), generations=len(record.get("generations", [])))
            return summary
        result = self.production.mutate(store_kind, {**body, f"{kind}_id": identifier})
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
        if kind == "job" and hasattr(self, "purge_video_publication"):
            warnings.extend(self.purge_video_publication(identifier))
        if kind == "job" and hasattr(self, "delete_prepublish_reviews"):
            self.delete_prepublish_reviews(identifier)
        try:
            activity = getattr(self, "activity", None)
            if activity is not None:
                activity.delete_owner(identifier)
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
            pattern = {"job": JOB_ID, "carousel": CAROUSEL_ID}.get(kind)
            if pattern is None or not isinstance(identifier, str) or not pattern.fullmatch(identifier):
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
                    outcome = (self.delete_job_order if kind == "job" else self.delete_carousel_order)(identifier, {"request_id": request_id})
                    entry["warnings"] = outcome.get("warnings", [])
                else:
                    wanted = action == "archive"
                    with self.lock:
                        current = self.production.job(identifier) if kind == "job" else next(
                            (value for value in self.production.public()["carousels"] if value["id"] == identifier), None)
                        if not current:
                            raise VoiceError(404, "这张制作单不存在。")
                        if bool(current.get("archived")) == wanted:
                            entry["skipped"] = "已经是归档状态" if wanted else "本来就没有归档"
                            results.append(entry)
                            continue
                        revision = self.production.public()["revision"]
                        (self.archive_job if kind == "job" else self.archive_carousel)(
                            identifier, {"request_id": request_id, "revision": revision, "archived": wanted})
            except VoiceError as exc:
                entry.update(ok=False, error=str(exc))
            results.append(entry)
        changed = sum(1 for item in results if item["ok"] and not item.get("skipped"))
        return {"action": action, "results": results, "changed": changed,
                "failed": sum(1 for item in results if not item["ok"]), "skipped": sum(1 for item in results if item.get("skipped"))}
