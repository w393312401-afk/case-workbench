"""Read-only content aggregation; the human controls are archiving a row and deleting archived rows for good."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import threading
import time

from ai_tasks import AITaskError
from content_management import build_content_items
from creation_store import CreationError
from direction_store import product_key
from direction_store import fingerprint
from elevenlabs_client import VoiceError
from workbench_video_publish import reviewed_source
from task_activity import settle_resolved_activity

# Running work stays visible separately from rows waiting for the reader.
VIEWS = ("attention", "running", "publish", "all")
KINDS = {"all", "task", "script", "video", "carousel"}
STATUSES = {"all", "workflow_incomplete", "in_progress", "needs_attention", "awaiting_review", "approved", "published", "archived"}
# Rows 删除已归档 can remove for good; a script family goes only while nothing else uses it.
DELETABLE = ("task", "video", "carousel", "script")
FILTER_FIELDS = {"kind": KINDS - {"all"}, "status": STATUSES - {"all"}, "view": set(VIEWS) - {"all"},
                 "review_calendar": {"added", "not_added"}}
MAX_FILTER_CONDITIONS = 12
MAX_FILTER_LENGTH = 16384
# Rebuilding the snapshot re-reads every task, script and order. Filter, search and page changes
# do not alter that data, so they may reuse a snapshot this young; refresh, polling and writes never do.
SNAPSHOT_REUSE_SECONDS = 8


def _parse_content_filter(value):
    """Validate one flat filter shared by list, archive preview and archive deletion."""
    if isinstance(value, str):
        if len(value) > MAX_FILTER_LENGTH:
            raise CreationError(400, "内容筛选条件过长。")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError) as exc:
            raise CreationError(400, "内容筛选条件无效。") from exc
    if (not isinstance(value, dict) or set(value) != {"match", "conditions"} or
            value.get("match") not in ("all", "any") or not isinstance(value.get("conditions"), list) or
            len(value["conditions"]) > MAX_FILTER_CONDITIONS):
        raise CreationError(400, "内容筛选条件无效。")
    conditions = []
    for condition in value["conditions"]:
        if (not isinstance(condition, dict) or set(condition) != {"field", "values"} or
                not isinstance(condition.get("field"), str) or condition["field"] not in FILTER_FIELDS):
            raise CreationError(400, "内容筛选字段无效。")
        field, values = condition["field"], condition["values"]
        allowed = FILTER_FIELDS[field]
        if (not isinstance(values, list) or not 1 <= len(values) <= len(allowed) or
                any(not isinstance(entry, str) or entry not in allowed for entry in values)):
            raise CreationError(400, "内容筛选选项无效。")
        conditions.append({"field": field, "values": list(dict.fromkeys(values))})
    return {"match": value["match"], "conditions": conditions}


def _view_matches(item, mode):
    if mode == "all":
        return True
    if item.get("history") or item.get("archived"):
        return False
    if mode == "publish":
        return item["stage"] == "awaiting_publication"
    if mode == "running":
        return bool(item.get("busy"))
    return bool(item.get("needs_attention"))


def _filter_matches(item, content_filter):
    if not content_filter or not content_filter["conditions"]:
        return True

    def matches(condition):
        field, values = condition["field"], condition["values"]
        if field == "kind":
            return item["kind"] in values
        if field == "review_calendar":
            # Use the current workflow fact, not a historic approval or a queue label.
            reviewed = item["kind"] == "video" and any(step.get("key") == "edit" and step.get("done") is True
                                                        for step in item.get("workflow_steps", []))
            calendar = item.get("calendar_uploaded")
            return reviewed and (("added" in values and calendar is True) or
                                 ("not_added" in values and calendar is False))
        predicate = _status_matches if field == "status" else _view_matches
        return any(predicate(item, value) for value in values)

    combine = all if content_filter["match"] == "all" else any
    return combine(matches(condition) for condition in content_filter["conditions"])


def _matches(item, kind, search):
    return ((kind == "all" or item["kind"] == kind) and
            (not search or search in " ".join(str(item.get(key, "")) for key in
                                              ("title", "source_title", "timeline_name", "task_started_label", "product", "direction_id", "id", "stage_label")).casefold()
             or search in " ".join(item.get("script_ids", []) + item.get("task_ids", [])
                                   + item.get("production_ids", [])).casefold()))


def _status_matches(item, status):
    if status == "all":
        return True
    if status == "archived":
        return bool(item.get("archived"))
    if status == "workflow_incomplete":
        return (item["kind"] in {"video", "carousel"} and not item.get("archived") and
                item.get("workflow_complete") is False)
    if status == "in_progress":
        return bool(not item.get("archived") and not item.get("history") and
                    (item.get("busy") or item["kind"] in {"video", "carousel"} and item["stage"] != "awaiting_publication"))
    if status == "needs_attention":
        return bool(item.get("needs_attention"))
    return item["stage"] in {"awaiting_review": {"awaiting_review", "reviewing"},
                             "approved": {"awaiting_publication"},
                             "published": {"published", "reviewed"}}[status]


class ContentWorkbenchMixin:
    _content_snapshot_lock = threading.Lock()
    _content_snapshot_generation = 0
    _content_snapshot_writes = 0
    _content_snapshot_cache = None

    def content_write_started(self):
        """A write request is in flight: nothing read from now until it ends may be reused."""
        self._content_write_event(1)

    def content_write_finished(self):
        # The response is sent before this runs, so a follow-up read must not meet a snapshot from the write window.
        self._content_write_event(-1)

    def _content_write_event(self, change):
        with self._content_snapshot_lock:
            self._content_snapshot_writes = max(0, self._content_snapshot_writes + change)
            self._content_snapshot_generation += 1
            self._content_snapshot_cache = None

    def _reusable_content_snapshot(self, product, reuse):
        key = product or ""
        with self._content_snapshot_lock:
            generation, entry = self._content_snapshot_generation, self._content_snapshot_cache
            if (reuse and entry and not self._content_snapshot_writes and entry["key"] == key
                    and entry["generation"] == generation and time.monotonic() - entry["started"] <= SNAPSHOT_REUSE_SECONDS):
                return entry["snapshot"]
        started = time.monotonic()
        snapshot = self._content_snapshot(product)
        with self._content_snapshot_lock:
            # A write that began or ended while this was building may have left it stale: serve it, never keep it.
            if self._content_snapshot_generation == generation and not self._content_snapshot_writes:
                self._content_snapshot_cache = {"key": key, "generation": generation, "started": started, "snapshot": snapshot}
        return snapshot

    def content_views_catalog(self, query):
        if set(query) - {"product"}:
            raise CreationError(400, "内容视图查询参数无效。")
        products = query.get("product", [""])
        if not isinstance(products, list) or len(products) != 1:
            raise CreationError(400, "内容视图所属产品不能重复。")
        return self.content_views.public(products[0])

    def save_content_views(self, body):
        return self.content_views.mutate(body)

    def attach_content_publications(self, production):
        publications = self._video_publications().read().get("jobs", {})
        for job in production.get("jobs", []):
            publication = (publications.get(job["id"]) or {}).get("publication")
            if publication:
                publication = dict(publication)
                try:
                    publication["source_current"] = publication["source_signature"] == fingerprint(reviewed_source({**job, "archived": False}))
                except VoiceError:
                    publication["source_current"] = False
            job["current_publication"] = publication
            entry = publications.get(job["id"]) or {}
            job["publication_posts"] = [dict(record) for record in (*entry.get("history", []), entry.get("publication")) if record]
        for item in production.get("carousels", []):
            publication = item.get("publication")
            if publication:
                self.carousel_delivery_live(item)
                item["publication_current"] = any(delivery.get("current") and delivery.get("id") == publication.get("delivery_id")
                                                  for delivery in item.get("deliveries", []))
        return production

    def _content_snapshot(self, product=None):
        with self.lock:
            tasks = self.ai.list_tasks(product=product or None)
            scripts = self.creation_catalog(product or None, include_usage=False)["scripts"]
            production = self.production_catalog(product or None)
            # A green completion dot must describe the current image bytes and
            # package, not merely the existence of an old generation or ZIP.
            # These checks annotate independent snapshots; they never write the stores.
            for item in production.get("carousels", []):
                if any(slide.get("outputs") for slide in item.get("slides", [])):
                    self.carousel_images_live(item)
                self.carousel_delivery_live(item)
                for delivery in item.get("deliveries", []):
                    delivery["available"] = False
                    if delivery.get("current"):
                        try:
                            self._checked_delivery(item["id"], delivery, complete=True)
                        except VoiceError:
                            pass
                        else:
                            delivery["available"] = True
                publication = item.get("publication")
                if publication:
                    item["publication_current"] = any(delivery.get("current") and delivery.get("available") and
                        delivery.get("id") == publication.get("delivery_id") for delivery in item.get("deliveries", []))
            management = self.content_management.public()
            # One read of the run feed serves every order; only an order whose feed is out of date touches it again.
            activity = getattr(self, "activity", None)
            runs = activity.existing() if activity is not None else None
            for kind, key in (("job", "jobs"), ("carousel", "carousels")):
                for source in production.get(key, []):
                    settle_resolved_activity(self, kind, source, runs)
            # Every catalog above is a fresh snapshot of its own, so rows need no further copy.
            items = build_content_items(tasks, scripts, production["jobs"], production.get("carousels", []),
                                        management=management, detached=True)
            return items, management

    def content_catalog(self, query):
        def single(name, default):
            values = query.get(name, [default])
            if len(values) != 1:
                raise CreationError(400, "内容筛选参数不能重复。")
            return values[0]

        product = single("product", "")
        view = single("view", "attention")
        kind = single("kind", "all")
        status = single("status", "all")
        search = single("q", "").strip().casefold()
        content_filter = _parse_content_filter(single("filter", None)) if "filter" in query else None
        if view not in VIEWS:
            raise CreationError(400, "内容视图无效。")
        if kind not in KINDS or status not in STATUSES or len(search) > 200:
            raise CreationError(400, "内容筛选条件无效。")
        try:
            page, size = int(single("page", "1")), int(single("page_size", "30"))
        except (TypeError, ValueError) as exc:
            raise CreationError(400, "内容页码无效。") from exc
        if page < 1 or not 1 <= size <= 100:
            raise CreationError(400, "内容页码或每页条数无效。")
        reuse = single("reuse", "")
        if reuse not in {"", "1"}:
            raise CreationError(400, "内容筛选条件无效。")
        items, management = self._reusable_content_snapshot(product, reuse == "1")

        counts = {mode: sum(_view_matches(item, mode) for item in items) for mode in VIEWS}
        selected = [item for item in items if _view_matches(item, view) and _matches(item, kind, search)
                    and _status_matches(item, status) and _filter_matches(item, content_filter)]
        # Newest first; in 全部, finished and archived rows follow the open ones.
        selected.sort(key=lambda item: (item.get("updated_at", ""), item["id"]), reverse=True)
        selected.sort(key=lambda item: bool(item.get("history") or item.get("archived")))
        total = len(selected)
        pages = max(1, (total + size - 1) // size)
        page = min(page, pages)
        # Posted videos whose publish-calendar mark is still 未上传, oldest post first; independent of the list filters.
        calendar = sorted((item for item in items if item.get("calendar_pending")),
                          key=lambda item: (item.get("published_on", ""), item["id"]))
        return {"items": selected[(page - 1) * size:page * size], "total": total, "page": page,
                "pages": pages, "page_size": size, "counts": counts, "revision": management["revision"],
                "calendar_pending": {"total": len(calendar), "items": calendar[:8]},
                # Archived rows in view that 删除已归档 can remove.
                "archived_deletable": len(self._archived_deletable([item for item in selected if item.get("archived")])[0]),
                "product": product_key(product) if product else ""}

    def _script_family_blocker(self, family, creation, production):
        """Why an archived script family must stay, or "": it is still used somewhere."""
        problem = self.creation.family_blocker(creation, family)
        if problem:
            return problem
        ids = {row["id"] for row in creation["scripts"] if (row.get("family_id") or row["id"]) == family}
        if any(row.get("script_id") in ids for row in [*production.get("jobs", []), *production.get("carousels", [])]):
            return "有版本被制作单用到，保留。"
        running = getattr(self, "_table_state", dict)()
        if any(record.get("script_id") in ids and record.get("id") in running for record in getattr(self, "_read_tables", list)()):
            return "图文逐张表还在生成，结束后再删。"
        return ""

    def _archived_deletable(self, archived):
        """Split archived rows into those 删除已归档 removes and those that stay, with the reason."""
        blocked, creation, production = {}, None, None
        # Read-only checks on the stores' own snapshots under the workbench lock, without copying whole catalogs.
        with self.lock:
            for item in archived:
                if item["kind"] not in DELETABLE:
                    blocked[item["id"]] = "这类内容没有永久删除。"
                elif item.get("busy"):
                    blocked[item["id"]] = "还在运行，这次跳过。"
                elif item["kind"] == "script":
                    if creation is None:
                        creation, production = self.creation._read_catalog(), self.production._read_catalog()
                    problem = self._script_family_blocker(item["id"].split(":", 1)[1], creation, production)
                    if problem:
                        blocked[item["id"]] = problem
        return {item["id"]: item for item in archived if item["id"] not in blocked}, blocked

    def _delete_script_family(self, family, request_id):
        """Delete one unused script family for good, with its 图文逐张表 runs and reviewed variant links."""
        with self.lock:
            creation, production = self.creation._read_catalog(), self.production._read_catalog()
            problem = self._script_family_blocker(family, creation, production)
            if problem:
                raise CreationError(409, problem)
            ids = {row["id"] for row in creation["scripts"] if (row.get("family_id") or row["id"]) == family}
            tables = [record for record in getattr(self, "_read_tables", list)() if record.get("script_id") in ids]
            result = self.creation.mutate("script_delete", {"request_id": request_id,
                                                            "revision": self.creation.current_revision(), "family_id": family})
        warnings = []
        root = self.production.root / "carousel_tables"

        def unlock(function, path, _):
            os.chmod(path, 0o700)
            function(path)
        for record in tables:
            folder = root / str(record.get("id"))
            if folder.is_dir() and not folder.is_symlink() and folder.resolve().parent == root.resolve():
                try:
                    shutil.rmtree(folder, onerror=unlock)
                except OSError as exc:
                    warnings.append(f"脚本已删除，但逐张表文件没有清理干净：{exc}")
        activity = getattr(self, "activity", None)
        for identifier in ids if activity is not None else ():
            try:
                activity.delete_owner(identifier)
            except Exception:
                pass  # The task log is secondary; the deletion itself is complete.
        return {**result, "warnings": warnings}

    def delete_archived_content(self, body):
        """Permanently delete the archived rows of one product through each record's own delete.

        AI tasks go through the 协作任务 delete and video/carousel orders through the order delete, so their files and
        links are cleaned the same way. A script family goes only while no order, case, posted video ID or plan uses
        any of its versions; running rows are skipped. The preview lists exactly what would go; the delete takes that
        list back and removes only rows that still qualify. This action does not require or create a data backup.
        """
        if not isinstance(body, dict) or set(body) - {"product", "view", "kind", "status", "filter", "q", "ids", "preview"}:
            raise CreationError(400, "删除参数无效。")
        product, kind, search = body.get("product", ""), body.get("kind", "all"), body.get("q", "")
        view, status = body.get("view", "all"), body.get("status", "all")
        if (not isinstance(product, str) or not isinstance(kind, str) or kind not in KINDS or
                not isinstance(status, str) or status not in STATUSES or view not in VIEWS or
                not isinstance(search, str) or len(search) > 200):
            raise CreationError(400, "删除范围无效。")
        content_filter = _parse_content_filter(body["filter"]) if "filter" in body else None
        search = search.strip().casefold()
        items, _ = self._content_snapshot(product)
        deletable, blocked = self._archived_deletable([item for item in items if item.get("archived") and
            _view_matches(item, view) and _matches(item, kind, search) and _status_matches(item, status) and
            _filter_matches(item, content_filter)])
        if body.get("preview") is True:
            kinds = {item["id"]: item["kind"] for item in items}
            return {"items": [{"id": item["id"], "kind": item["kind"], "title": item["title"]} for item in deletable.values()],
                    "counts": {name: sum(item["kind"] == name for item in deletable.values()) for name in DELETABLE},
                    "kept": {"script": sum(kinds[key] == "script" and reason != "还在运行，这次跳过。" for key, reason in blocked.items()),
                             "busy": sum(reason == "还在运行，这次跳过。" for reason in blocked.values())}}
        ids = body.get("ids")
        if (not isinstance(ids, list) or not ids or len(ids) > 1000 or
                not all(isinstance(value, str) and len(value) <= 220 for value in ids)):
            raise CreationError(400, "请先确认要删除的归档内容。")
        ids = list(dict.fromkeys(ids))
        results = [{"id": value, "ok": False, "error": blocked.get(value, "已不在归档里，没有删除。")} for value in ids if value not in deletable]
        targets = [deletable[value] for value in ids if value in deletable]
        if not targets:
            return {"deleted": 0, "failed": len(results), "results": results, "backup": None}
        tasks = {item["id"].split(":", 1)[1]: item["id"] for item in targets if item["kind"] == "task"}
        if tasks:
            revisions = {row.get("task_id"): row.get("revision") for row in self.ai.list_tasks()}
            known = [{"task_id": task_id, "revision": revisions[task_id]} for task_id in tasks if type(revisions.get(task_id)) is int]
            results.extend({"id": tasks[task_id], "ok": False, "error": "这条协作任务已变化，请刷新后重试。"}
                           for task_id in tasks if type(revisions.get(task_id)) is not int)
            try:
                outcome = self.ai.delete_tasks({"tasks": known}) if known else {"deleted": [], "failed": []}
            except AITaskError as exc:
                outcome = {"deleted": [], "failed": [{"task_id": entry["task_id"], "error": str(exc)} for entry in known]}
            results.extend({"id": tasks[task_id], "ok": True, "warnings": []} for task_id in outcome["deleted"])
            results.extend({"id": tasks[entry["task_id"]], "ok": False, "error": entry["error"]} for entry in outcome["failed"])
        token = secrets.token_hex(8)
        # Orders before scripts, so a script is judged after the orders in this same batch are gone.
        for item in sorted((item for item in targets if item["kind"] != "task"), key=lambda item: item["kind"] == "script"):
            identifier = item["id"].split(":", 1)[1]
            delete = {"video": self.delete_job_order, "carousel": self.delete_carousel_order,
                      "script": self._delete_script_family}[item["kind"]]
            try:
                outcome = delete(identifier, {"request_id": f"content-delete-{token}-{identifier}"}) if item["kind"] != "script" \
                    else delete(identifier, f"content-delete-{token}-{identifier}")
                results.append({"id": item["id"], "ok": True, "warnings": outcome.get("warnings", [])})
            except (VoiceError, CreationError) as exc:
                results.append({"id": item["id"], "ok": False, "error": str(exc)})
        deleted = sum(1 for entry in results if entry["ok"])
        return {"deleted": deleted, "failed": len(results) - deleted, "results": results, "backup": None}

    def _restore_content_source(self, identifier, request_id):
        """Restore only this row's archived source through its ordinary store API.

        Called after the metadata mutation has passed validation and revision
        checks. Its source write and the list receipt are independently durable;
        a lost final receipt can be retried without adding another restore event.
        """
        kind, source_id = identifier.split(":", 1)
        token = "content-restore-" + fingerprint([request_id, identifier])[:40]
        if kind == "task":
            task = self.ai.get(source_id)
            if not task.get("archived"):
                return
            result = self.ai.manage_tasks({"request_id": token, "archived": False,
                                          "items": [{"task_id": source_id, "revision": task["revision"]}]})
            if result["failed"]:
                failure = result["failed"][0]
                raise CreationError(failure.get("status", 409), failure["error"])
        elif kind == "script":
            catalog = self.creation.public()
            versions = [row for row in catalog["scripts"] if (row.get("family_id") or row["id"]) == source_id]
            versions.sort(key=lambda row: (row.get("version", 0), row.get("created_at", ""), row["id"]))
            if not versions or any(not row.get("archived") and row.get("status") != "archived" for row in versions):
                return
            self.creation.mutate("script_status", {"request_id": token, "revision": catalog["revision"],
                                                    "script_status": {"script_id": versions[-1]["id"], "archived": False}})
        else:
            catalog = self.production.public()
            rows = catalog["jobs" if kind == "video" else "carousels"]
            source = next((row for row in rows if row["id"] == source_id), None)
            if source is None or not source.get("archived"):
                return
            operation, field = ("job_archive", "job_id") if kind == "video" else ("carousel_archive", "carousel_id")
            self.production.mutate(operation, {"request_id": token, "revision": catalog["revision"],
                                                field: source_id, "archived": False})

    def content_calendar(self, query):
        identifier = (query.get("id") or [""])[0]
        entry = self.content_management.public()["items"].get(identifier, {})
        return {"id": identifier, "calendar_uploaded": entry.get("calendar_uploaded") is True,
                "calendar_date": entry.get("calendar_date") if isinstance(entry.get("calendar_date"), str) else ""}

    def set_content_calendar(self, body):
        """Mark a not-yet-published video or carousel order as in the publish calendar, and/or set its planned posting day."""
        patch = {key: body[key] for key in ("calendar_uploaded", "calendar_date") if isinstance(body, dict) and key in body}
        if (not isinstance(body, dict) or set(body) - {"id", "calendar_uploaded", "calendar_date"} or not patch
                or not str(body.get("id", "")).startswith(("video:", "carousel:"))):
            raise CreationError(400, "发布日历参数无效。")
        return self.save_content_management({"request_id": "calendar-" + secrets.token_hex(8), "id": body["id"],
                                             "revision": self.content_management.public()["revision"], "management": patch})

    def save_content_management(self, body):
        if not isinstance(body, dict):
            raise CreationError(400, "内容安排参数无效。")
        with self.lock:
            prior = self.content_management.replay(body)
            if prior is not None:
                return prior
            if not isinstance(body.get("management"), dict):
                raise CreationError(400, "内容安排参数无效。")
            items, _ = self._content_snapshot()
            tasks = self.ai.list_tasks()
            scripts = self.creation.public()["scripts"]
            production = self.production.public()
            allowed = {item["id"] for item in items}
            allowed.update("task:" + row["task_id"] for row in tasks)
            allowed.update("script:" + (row.get("family_id") or row["id"]) for row in scripts)
            allowed.update("video:" + row["id"] for row in production["jobs"])
            allowed.update("carousel:" + row["id"] for row in production.get("carousels", []))
            item = next((row for row in items if row["id"] == body.get("id")), None)
            if (body.get("management") or {}).get("archived") is True and item and item.get("busy"):
                raise CreationError(409, "这条内容仍有任务在运行，结束后可归档。")
            restore = body["management"].get("archived") is False
            try:
                result = self.content_management.mutate(body, allowed_ids=allowed,
                    before_write=(lambda: self._restore_content_source(body["id"], body["request_id"])) if restore else None)
                if "archived" in body["management"] and getattr(self, "activity", None) is not None:
                    self.activity.archive_owner(body["id"].split(":", 1)[1], body["management"]["archived"], source="content")
                return result
            except AITaskError as exc:
                raise CreationError(exc.status, str(exc)) from exc
