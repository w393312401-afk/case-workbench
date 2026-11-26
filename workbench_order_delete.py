"""Permanently delete a 内容制作 order (a video job) together with its local files.

Archiving is not required first; orders with running work are refused. The catalog record is removed first (under the catalog revision rules), then the files;
whatever cannot be removed is reported, never silently kept as a record. The case library is not touched.

The confirmation lists the order's DaVinci timelines and its adopted script. Timelines the user leaves ticked are
deleted from the DaVinci project that is open, after the record is gone; a closed or busy DaVinci only leaves a warning
naming what stayed, so the order delete never stops halfway. The adopted script stays (only the link goes) unless the
user ticks it, and it can only be ticked while no other order, case, posted video or plan uses it.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
from contextlib import nullcontext

from case_deletion import DeletionError, _safe_path
from elevenlabs_client import VoiceError
from production_operations import TERMINAL, file_lock
from production_store import EDIT_ID, TAKE_ID, timeline_edits
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


def _checked_path(path, root):
    try:
        return _safe_path(path.absolute(), root.absolute())
    except (DeletionError, OSError, RuntimeError) as exc:
        raise VoiceError(409, f"文件位置异常，无法安全清理：{path}") from exc


def _safe_folder(path, parent):
    return path.parent == parent and _checked_path(path, parent).is_dir()


def _clean_paths(folders, files, root):
    warnings = []
    for folder in folders:
        def unlock(function, path, _):
            target = _checked_path(Path(path), folder)
            os.chmod(target, 0o700)
            function(target)
        try:
            _checked_path(folder, root)
            shutil.rmtree(folder, onerror=unlock)
            if folder.exists():
                warnings.append(f"记录已删除，但本地文件夹仍有残留：{folder}")
        except (OSError, VoiceError) as exc:
            warnings.append(f"记录已删除，但本地文件夹没有清理干净：{exc}")
    for path in files:
        try:
            _checked_path(path, root).unlink(missing_ok=True)
        except (OSError, VoiceError) as exc:
            warnings.append(f"记录已删除，但有文件没有清理干净：{exc}")
    return warnings


class OrderDeleteMixin:
    """Needs self.production, self.lock, self.activity and the editing/production mixins."""

    # -- what belongs to a video job ------------------------------------------------------------------------------
    def _job_footprint(self, job):
        root = self.production.root
        folders = []
        audio = root / "audio" / job["id"]
        if _safe_folder(audio, root / "audio"):
            folders.append(audio)
        previews = root / "video-previews" / job["id"]
        if _safe_folder(previews, root / "video-previews"):
            folders.append(previews)
        for edit in job.get("edits", []):
            if EDIT_ID.fullmatch(str(edit.get("id"))):
                folder = root / "edits" / edit["id"]
                if _safe_folder(folder, root / "edits"):
                    folders.append(folder)
        replica = root / "replica" / job["id"]
        if _safe_folder(replica, root / "replica"):
            folders.append(replica)
        rules = root / "edit-skill" / job["id"]
        if _safe_folder(rules, root / "edit-skill"):
            folders.append(rules)
        previews = root / "previews" / job["id"]
        if _safe_folder(previews, root / "previews"):
            folders.append(previews)
        handoffs = _checked_path(root / "handoffs", root)
        files = [path for path in handoffs.glob(f"{job['id']}-*.md") if _checked_path(path, root).is_file()]
        for path in _checked_path(root / "operations", root).glob("*.json"):
            _checked_path(path, root)
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
        notion = getattr(self, "notion_uploads", None)
        if notion:
            for parent in (notion.store.root / "files",):
                folder = parent / job["id"]
                if _safe_folder(folder, parent):
                    folders.append(folder)
            for row in notion.store.read()["uploads"].values():
                if row["job_id"] == job["id"]:
                    preview = notion.store.root / "previews" / (row["id"] + ".mp4")
                    if _checked_path(preview, root).is_file():
                        files.append(preview)
        return folders, files

    def _abandoned_build(self, record):
        """A build that ended before the worker journaled anything and is not waiting now: DaVinci was never touched."""
        live = getattr(self, "_timeline_building", {}).get(record.get("job_id")) or {}
        return (record.get("kind") == "timeline" and record.get("request_id") != live.get("request_id") and
                isinstance(record.get("operation_key"), str) and
                not self._production_operations().recovery_file(record).exists())

    def _job_blockers(self, job):
        for manager in (getattr(self, "preview_exports", None), getattr(self, "video_previews", None)):
            if manager and manager.busy(job["id"]):
                return "这张制作单正在导出视频预览，请完成后再删除。"
        notion = getattr(self, "notion_uploads", None)
        if notion and notion.busy(job["id"]):
            return "这张制作单正在上传到 Notion，请等它结束。"
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
            result = self._delete_order("job", identifier, body)
        if body.get("preview") is True or result.pop("replayed", False):
            return result
        timelines, script = result.pop("resolve_targets", []), result.pop("script_target", None)
        warnings = list(result.get("warnings", []))
        if script:
            outcome = self._delete_order_script(script, body["request_id"])
            warnings.extend(outcome.get("warnings", []))
            result["script_deleted"] = outcome.get("deleted", False)
            if outcome.get("task"):
                result["task"] = outcome["task"]
        if timelines:
            outcome = self._delete_resolve_timelines(timelines)
            warnings.extend(outcome["warnings"])
            result["resolve_deleted"] = outcome["deleted"]
        return {**result, "warnings": warnings}

    # -- DaVinci timelines and the adopted script that go with an order ------------------------------------------
    def _order_timelines(self, record):
        """The order's DaVinci timelines. One that another order also records is marked shared and never deleted."""
        with self.lock:
            others = {row.get("unique_id") for job in self.production._read_catalog()["jobs"] if job["id"] != record["id"]
                      for row in job.get("timelines", [])}
        entry, records = {}, None
        if hasattr(self, "_video_publications"):
            from workbench_video_publish import timeline_records as records
            entry = self._video_publications().read()["jobs"].get(record["id"], {})
        rows, seen = [], set()
        for row in record.get("timelines", []):
            uid = row.get("unique_id")
            if not isinstance(uid, str) or not uid or uid in seen:
                continue
            seen.add(uid)
            rows.append({"unique_id": uid, "name": row.get("name", ""), "project": row.get("project", ""),
                         "archived": bool(row.get("archived")), "shared": uid in others,
                         "published": bool(entry and records and records(entry, uid))})
        return rows

    def _order_resolve_preview(self, record):
        """What the confirmation shows about DaVinci: the timelines, and whether they can be deleted right now."""
        rows = self._order_timelines(record)
        state = {"timelines": rows, "state": "none" if not rows else "", "project": "", "message": ""}
        if not rows:
            return state
        resolve = getattr(self, "resolve", None)
        if resolve is None:
            state.update(state="unavailable", message="这里连不到达芬奇，时间线不能从工作台删除，需要的话请在达芬奇里手动删。")
        elif resolve.closed():
            state.update(state="closed", message="达芬奇没有打开。要连时间线一起删，请先打开达芬奇和对应项目，再点「重新检查达芬奇」；不删时间线也可以直接删除制作单。")
        elif resolve.busy():
            state.update(state="busy", message="达芬奇正在执行另一项工作台操作（可能是 AI 剪辑），现在删不了时间线。可以等它结束后点「重新检查达芬奇」，或只删制作单、时间线留着。")
        else:
            try:
                found = resolve.run("timeline_ids", {}, timeout=30, launch=False)
            except VoiceError as exc:
                state.update(state="error", message=f"读不到达芬奇里的时间线：{exc}")
            else:
                present = set(found.get("unique_ids") or [])
                state.update(state="open", project=found.get("project", ""))
                for row in rows:
                    row["in_open_project"] = row["unique_id"] in present
        return state

    def _order_script_preview(self, record):
        """Whether the order's adopted script (all its versions) may be deleted with it, and why not."""
        creation, script_id = getattr(self, "creation", None), record.get("script_id")
        if creation is None or not script_id or not hasattr(self, "_delete_order_script"):
            return {"deletable": False, "blocker": "这张制作单没有可以一起删除的采用稿。"}
        with self.lock:
            catalog, jobs = creation._read_catalog(), self.production._read_catalog()["jobs"]
            row = next((item for item in catalog["scripts"] if item["id"] == script_id), None)
            if not row:
                return {"deletable": False, "blocker": "采用稿已经不存在。"}
            family = row.get("family_id") or row["id"]
            versions = [item for item in catalog["scripts"] if (item.get("family_id") or item["id"]) == family]
            ids = {item["id"] for item in versions}
            others = [job for job in jobs if job["id"] != record["id"] and job.get("script_id") in ids]
            links = [item for item in catalog.get("variant_links", []) if item.get("kind") == "script" and item.get("record_id") in ids]
            blocker = creation.family_blocker(catalog, family)
            if not blocker and others:
                blocker = f"还有 {len(others)} 张别的制作单在用这条稿（含它的其他版本），不能删，只断开和这张制作单的关联。"
        return {"family_id": family, "title": row.get("title", ""), "version": row.get("version", 1), "versions": len(versions),
                "task_id": (row.get("source") or {}).get("task_id") or "", "variant_links": len(links),
                "other_jobs": [{"id": job["id"], "title": job.get("title", "")} for job in others[:5]],
                "deletable": not blocker, "blocker": blocker}

    def _order_delete_choices(self, record, body):
        """Check what the user ticked before anything is deleted, so a bad choice refuses the whole delete."""
        wanted = body.get("delete_timelines", [])
        if not isinstance(wanted, list) or len(wanted) > 200 or not all(isinstance(uid, str) for uid in wanted):
            raise VoiceError(400, "要删除的时间线参数无效。")
        if not isinstance(body.get("delete_script", False), bool):
            raise VoiceError(400, "是否删除采用稿的参数无效。")
        rows = {row["unique_id"]: row for row in self._order_timelines(record)} if wanted else {}
        targets = []
        for uid in dict.fromkeys(wanted):
            row = rows.get(uid)
            if not row:
                raise VoiceError(409, "要删除的时间线已不属于这张制作单，请刷新后重试。")
            if row["shared"]:
                raise VoiceError(409, f"时间线「{row['name']}」还被别的制作单记录着，不能一起删除。")
            targets.append(row)
        script = None
        if body.get("delete_script"):
            plan = self._order_script_preview(record)
            if not plan["deletable"]:
                raise VoiceError(409, plan["blocker"])
            script = {"family_id": plan["family_id"], "task_id": plan["task_id"]}
        return targets, script

    def _delete_resolve_timelines(self, rows):
        """Delete the order's ticked timelines from the open DaVinci project; whatever stays is named in a warning."""
        def named(items):
            return "、".join(f"「{row['name'] or row['unique_id']}」" for row in items)
        resolve = getattr(self, "resolve", None)
        if resolve is None or resolve.closed():
            reason = "达芬奇没有打开" if resolve is not None else "这里连不到达芬奇"
            return {"deleted": [], "warnings": [f"制作单已删除，但{reason}，这些时间线没有删：{named(rows)}。需要的话请在达芬奇里手动删除。"]}
        try:
            result = resolve.run("delete_timelines", {"unique_ids": [row["unique_id"] for row in rows]}, timeout=120, launch=False)
        except VoiceError as exc:
            return {"deleted": [], "warnings": [f"制作单已删除，但达芬奇时间线没有删（{exc}）：{named(rows)}。需要的话请在达芬奇里手动删除。"]}
        deleted, missing = set(result.get("deleted") or []), set(result.get("missing") or [])
        warnings = []
        away = [row for row in rows if row["unique_id"] in missing]
        if away:
            warnings.append(f"这些时间线不在达芬奇当前打开的项目「{result.get('project', '')}」里，没有删：{named(away)}。"
                            "如果它们在别的项目里，请打开那个项目手动删除。")
        failed = [row for row in rows if row["unique_id"] not in deleted and row["unique_id"] not in missing]
        if failed:
            warnings.append(f"达芬奇没能删除这些时间线：{named(failed)}，请在达芬奇里手动删除。")
        if deleted and result.get("saved") is False:
            warnings.append("时间线已在达芬奇里删除，但项目没能自动保存，请在达芬奇里保存一下（Ctrl+S）。")
        return {"deleted": [row["name"] for row in rows if row["unique_id"] in deleted], "warnings": warnings}

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
                return {**prior, "warnings": warnings, "replayed": True}
        record = self.production.job(identifier)
        if not record:
            raise VoiceError(404, "这张视频制作单不存在。")
        problem = self._job_blockers(record)
        if problem:
            raise VoiceError(409, problem)
        folders, files = self._job_footprint(record)
        if not preview:
            resolve_targets, script_target = self._order_delete_choices(record, body)
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
            summary.update(resolve=self._order_resolve_preview(record), script=self._order_script_preview(record))
            return summary
        result = self.production.mutate("job_delete", {**body, "job_id": identifier})
        warnings = _clean_paths(folders, files, self.production.root)
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
        forget = getattr(self, "forget_script_reviews", None)
        if forget:
            forget([f"job:{identifier}"])  # its 口播审查 and their rows in the task panel
        return {**result, "warnings": warnings, "resolve_targets": resolve_targets, "script_target": script_target}

    def delete_take(self, identifier, take_id, body):
        """Preview or delete one unreferenced take, leaving all other production assets intact."""
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        if not JOB_ID.fullmatch(str(identifier)) or not TAKE_ID.fullmatch(str(take_id)):
            raise VoiceError(404, "这版配音不存在。")
        preview = body.get("preview") is True
        operation_body = {**body, "job_id": identifier, "take_id": take_id}
        if not preview:
            self._validate_edit_request(body)
            prior = self.production.replay("take_delete", operation_body)
            if prior:
                folder = self.production.audio / identifier / take_id
                warnings = [f"配音记录已删除，但本地文件夹仍有残留，可手动删除：{folder}"] if folder.exists() or folder.is_symlink() else []
                return {**prior, "warnings": warnings}
        operations = self._production_operations()
        # The same job lock covers snapshot through commit for voice generation and timeline builds.
        with file_lock(operations.root / "locks" / (identifier + ".lock"), "这张制作单正在生成配音或建立时间线，请等它结束。"):
            # Preview managers have their own locks and may read production while holding them.
            # Do not acquire them while holding the production lock.
            problem = self._job_blockers(self.production.job(identifier))
            unfinished = any(row.get("job_id") == identifier and row.get("state") not in TERMINAL for row in operations.records())
            with self.lock:
                job = self.production.job(identifier)
                take = next((item for item in job.get("takes", []) if item.get("id") == take_id), None)
                if not take:
                    raise VoiceError(404, "这版配音不存在。")
                blockers = []
                timeline_count = sum(item.get("take_id") == take_id for item in job.get("timelines", []))
                edit_count = sum(item.get("take_id") == take_id for item in job.get("edits", []))
                if timeline_count:
                    blockers.append(f"这版配音已被 {timeline_count} 条时间线引用，请先处理关联时间线。")
                if edit_count:
                    blockers.append(f"这版配音已被 {edit_count} 次剪辑引用，请先处理关联剪辑。")
                if job.get("archived"):
                    blockers.append("制作单已归档，请恢复后再删除配音。")
                if problem:
                    blockers.append(problem)
                elif unfinished:
                    blockers.append("这张制作单还有未完成或待恢复的配音或建线操作，请先处理。")
                parent = self.production.audio / identifier
                folder = parent / take_id
                try:
                    safe_parent = _safe_folder(parent, self.production.audio)
                    safe_folder = safe_parent and _safe_folder(folder, parent)
                except VoiceError as exc:
                    blockers.append(str(exc))
                    safe_parent = safe_folder = False
                if parent.is_symlink() or (parent.exists() and not safe_parent) or (folder.exists() and not safe_folder) or folder.is_symlink():
                    blockers.append("配音文件夹位置异常，无法安全删除，请先核对文件。")
                size, count = _tree(folder) if safe_folder else (0, 0)
                if preview:
                    return {"take_id": take_id, "selected": job.get("selected_take") == take_id,
                            "bytes": size, "files": count, "blockers": blockers}
                if blockers:
                    raise VoiceError(409, " ".join(blockers))
                result = self.production.mutate("take_delete", operation_body)
                warnings = []
                if safe_folder:
                    try:
                        _checked_path(folder, self.production.root)
                        shutil.rmtree(folder)
                    except (OSError, VoiceError) as exc:
                        warnings.append(f"配音记录已删除，但本地文件夹没有清理干净：{exc}")
                    if folder.exists():
                        warnings.append(f"配音记录已删除，但本地文件夹仍有残留，可手动删除：{folder}")
            try:
                activity = getattr(self, "activity", None)
                if activity is not None:
                    activity.delete_source(identifier, take_id)
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
        previews = getattr(self, "preview_exports", None)
        if previews:
            for row in previews._records(job['id']):
                if row['timeline_uid'] == timeline['unique_id']:
                    folder = previews._folder(job['id'], row['id'])
                    if _safe_folder(folder, previews.root / job['id']):
                        folders.append(folder)
        for edit in timeline_edits(job, timeline):
            if EDIT_ID.fullmatch(str(edit.get("id"))):
                folder = root / "edits" / edit["id"]
                if _safe_folder(folder, root / "edits"):
                    folders.append(folder)
        # Handoff notes are named after the order; the timeline's id inside them says which one they are for.
        for path in _checked_path(root / "handoffs", root).glob(f"{job['id']}-*.md"):
            _checked_path(path, root)
            try:
                if path.is_file() and timeline["unique_id"] in path.read_text(encoding="utf-8"):
                    files.append(path)
            except (OSError, ValueError):
                continue
        key = timeline.get("operation_key")
        if key:
            for path in _checked_path(root / "operations", root).glob("*.json"):
                _checked_path(path, root)
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
        for manager in (getattr(self, "preview_exports", None), getattr(self, "video_previews", None)):
            if manager and manager.busy(job["id"]):
                return "这张制作单正在导出视频预览，请完成后再删除时间线。"
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
        warnings = _clean_paths(folders, files, self.production.root)
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
