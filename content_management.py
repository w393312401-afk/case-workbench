"""A read-only content queue and its separate, durable archive marks.

Only explicit provenance connects records. Product/direction similarities never
identify a piece of content; every production order remains its own deliverable.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import re
import threading
from urllib.parse import quote, urlencode

from ai_resolution import ai_task_resolutions
from creation_store import CreationError, CreationStore
from direction_store import fingerprint, now, product_key
from elevenlabs_client import VoiceError
from pathlib import Path
from production_operations import durable_json, file_lock
from content_naming import content_heading, label_task_starts, task_created_at
from production_naming import timeline_label
from production_store import (active_timelines, current_timeline, job_status, manual_completion_current)
from workbench_video_publish import reviewed_source


# Scheduling (priority, dates, pausing, notes) was removed unused; a content row is only archived or not.
DEFAULT_MANAGEMENT = {"archived": False}
# calendar_uploaded: a pre-publish reminder that the order sits in the publish calendar; a registration's own mark wins once posted.
# skip_publish: a finished video the user decided not to post (不发了), with an optional one-line reason.
MANAGEMENT_FIELDS = frozenset({*DEFAULT_MANAGEMENT, "calendar_uploaded", "calendar_date", "skip_publish", "skip_reason"})
SKIP_REASON_LIMIT = 200
# A posted video leaves the working lists this many days after its posting day (已完结); nothing is deleted.
FINISH_AFTER_DAYS = 7
CONTENT_ID = re.compile(r"(?:task|script|video):[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
# Marks saved for retired picture orders stay in the file; nothing new can be written for them.
STORED_ID = re.compile(r"(?:task|script|video|carousel):[A-Za-z0-9][A-Za-z0-9_-]{0,199}\Z")
REQUEST_ID = re.compile(r"[\w-]{10,100}\Z")


def _valid_day(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _validate_patch(patch):
    if not isinstance(patch, dict) or not patch or set(patch) - MANAGEMENT_FIELDS:
        raise CreationError(400, "内容进程只能归档、恢复、标记发布日历或标记不发。")
    day = patch.get("calendar_date")
    if "calendar_date" in patch and not (day == "" or isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) and _valid_day(day)):
        raise CreationError(400, "发布日期须为有效的 YYYY-MM-DD。")
    reason = patch.get("skip_reason")
    if "skip_reason" in patch and not (isinstance(reason, str) and len(reason) <= SKIP_REASON_LIMIT):
        raise CreationError(400, f"不发原因最多 {SKIP_REASON_LIMIT} 字。")
    if any(type(value) is not bool for key, value in patch.items() if key not in {"calendar_date", "skip_reason"}):
        raise CreationError(400, "归档与发布日历须为明确的开关。")
    return copy.deepcopy(patch)


class ContentManagementStore:
    def __init__(self, runtime_root, lock=None):
        self.root = Path(runtime_root) / "creation"
        self.path = self.root / "content-management.json"
        self.lock = lock or threading.RLock()
        self.version = 0

    @staticmethod
    def empty():
        return {"schema_version": 1, "items": {}, "requests": {}}

    def read(self):
        import json
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(data, dict) or data.get("schema_version") != 1 or
                    not isinstance(data.get("items"), dict) or not isinstance(data.get("requests"), dict)):
                raise ValueError()
            for identifier, entry in data["items"].items():
                if (not isinstance(identifier, str) or not STORED_ID.fullmatch(identifier) or
                        not isinstance(entry, dict) or not isinstance(entry.get("updated_at"), str)):
                    raise ValueError()
                _validate_patch({key: value for key, value in entry.items() if key != "updated_at"})
            for request_id, receipt in data["requests"].items():
                if (not isinstance(request_id, str) or not REQUEST_ID.fullmatch(request_id) or
                        not isinstance(receipt, dict) or not isinstance(receipt.get("hash"), str) or
                        not re.fullmatch(r"[0-9a-f]{64}", receipt["hash"]) or
                        not isinstance(receipt.get("result"), dict) or not isinstance(receipt.get("at"), str)):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise CreationError(500, "内容归档记录无法读取，请保留原文件并核对备份。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

    def public(self):
        with self.lock:
            data = self.read()
            return {"items": copy.deepcopy(data["items"]), "revision": self.revision(data)}

    @staticmethod
    def _receipt(data, body):
        receipt = data["requests"].get(body.get("request_id"))
        if receipt:
            if receipt.get("hash") != fingerprint(body):
                raise CreationError(409, "保存标识已用于另一项修改，请重新提交。")
            return copy.deepcopy(receipt["result"])
        return None

    def replay(self, body):
        if not isinstance(body, dict):
            raise CreationError(400, "内容归档参数须为对象。")
        if not isinstance(body.get("request_id"), str) or not REQUEST_ID.fullmatch(body["request_id"]):
            raise CreationError(400, "缺少保存标识，请刷新后重试。")
        with self.lock:
            return self._receipt(self.read(), body)

    def mutate(self, body, allowed_ids=None, before_write=None):
        if not isinstance(body, dict) or set(body) - {"request_id", "revision", "id", "management"}:
            raise CreationError(400, "内容归档参数格式无效。")
        if not isinstance(body.get("request_id"), str) or not REQUEST_ID.fullmatch(body["request_id"]):
            raise CreationError(400, "缺少保存标识，请刷新后重试。")
        identifier = body.get("id")
        if not isinstance(identifier, str) or not CONTENT_ID.fullmatch(identifier):
            raise CreationError(400, "内容编号格式无效。")
        patch = _validate_patch(body.get("management"))
        with self.lock:
            try:
                with file_lock(self.root / "content-management.lock", "内容归档正在保存，请稍后重试。"):
                    data = self.read()
                    previous = self._receipt(data, body)
                    if previous is not None:
                        return previous
                    if body.get("revision") != self.revision(data):
                        raise CreationError(409, "内容归档状态已更新，请刷新后重试。")
                    if allowed_ids is not None and identifier not in allowed_ids:
                        raise CreationError(404, "这条内容已变化，请刷新列表后重试。")
                    # Validate the whole list mutation before restoring an archived
                    # source. If the metadata write fails, a retry sees that source
                    # already restored and only finishes this durable receipt.
                    if before_write is not None:
                        before_write()
                    entry = {**data["items"].get(identifier, {}), **patch, "updated_at": now()}
                    data["items"][identifier] = entry
                    result = {"id": identifier, "management": copy.deepcopy(entry), "revision": self.revision(data)}
                    data["requests"][body["request_id"]] = {"hash": fingerprint(body), "result": copy.deepcopy(result), "at": now()}
                    # Match other catalog retry windows, without discarding content entries.
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="microseconds")
                    data["requests"] = {key: value for key, value in data["requests"].items() if value.get("at", "") >= cutoff}
                    durable_json(self.path, data)
                    self.version += 1
                    return result
            except VoiceError as exc:
                raise CreationError(exc.status, str(exc)) from exc


def _rows(value, key, detached=False):
    if isinstance(value, dict):
        value = value.get(key, [])
    return [row if detached else copy.deepcopy(row) for row in (value or []) if isinstance(row, dict)]


def _source(row):
    value = row.get("source")
    return value if isinstance(value, dict) else {}


def _provenance(row):
    source = _source(row)
    task_id, round_id = source.get("task_id"), source.get("round_id")
    return (task_id, round_id) if task_id and round_id else None


def _stamp(rows):
    return max((str(row.get("updated_at") or row.get("created_at") or "") for row in rows), default="")


def _link(label, destination):
    return {"label": label, "hash": destination}


def _hash(kind, identifier, step=""):
    value = f"#{kind}/{quote(str(identifier), safe='')}"
    return value + (f"?step={quote(step)}" if step else "")


def _video_stage(row):
    # A publication catalog receipt records an actual posted deliverable, even
    # if later editing has changed its production source.
    published = row.get("current_publication") or row.get("publication")
    if (isinstance(published, dict) and published.get("post_id") and published.get("published_on") and
            published.get("source_current") is not False):
        return "published", "已发布", "查看发布记录", "publish", False, True
    if row.get("voicing"):
        return "voicing", "配音中", "查看配音进度", "voice", False, False
    if row.get("timeline_building"):
        waiting = row["timeline_building"].get("stage") == "waiting" if isinstance(row["timeline_building"], dict) else False
        return "timeline_building", "建线排队中" if waiting else "建线中", "查看建线进度", "timeline", False, False
    if row.get("recovery_operations"):
        return "needs_repair", "结果待恢复", "查看并恢复", "edit", True, False
    if any(run.get("status") in {"running", "queued"} for run in row.get("edits", [])):
        return "editing", "剪辑中", "查看剪辑进度", "edit", False, False
    state = row.get("status") or job_status(row)
    return {
        "draft": ("draft", "待配音", "制作配音", "voice", True, False),
        "voiced": ("voiced", "待建时间线", "创建时间线", "timeline", True, False),
        "timeline": ("timeline", "待剪辑", "开始剪辑", "edit", True, False),
        "editing": ("editing", "剪辑中", "查看剪辑进度", "edit", False, False),
        "awaiting_review": ("awaiting_review", "待人工审查", "核对剪辑结果", "edit", True, False),
        # Finished and waiting to be posted: listed under 待发布, not 待我处理.
        "approved": ("awaiting_publication", "待发布", "查看成片与登记发布", "publish", False, False),
        "needs_material": ("needs_material", "待补素材", "补齐素材", "edit", True, False),
        "needs_verification": ("needs_verification", "素材待核实", "核对素材", "edit", True, False),
        "needs_repair": ("needs_repair", "待修正", "查看并修正", "edit", True, False),
        "verification_failed": ("verification_failed", "核验未完成", "核对结果", "edit", True, False),
        "unverified": ("unverified", "旧记录待核验", "核对结果", "edit", True, False),
        "failed": ("failed", "剪辑失败", "查看并重试", "edit", True, False),
        "cancelled": ("cancelled", "剪辑已停止", "继续制作", "edit", True, False),
        "interrupted": ("interrupted", "剪辑已中断", "查看并恢复", "edit", True, False),
    }.get(state, (state or "unknown", "待核对制作状态", "查看制作单", "", True, False))


def _progress_text(row, kind):
    """A brief snapshot of actual live work; do not infer that a file is ready."""
    texts = []
    for key in ("timeline_building", "voicing_live", "live"):
        live = row.get(key)
        if isinstance(live, dict):
            texts.append(live.get("progress") or live.get("message"))
    for run in reversed(row.get("edits", [])):
        if run.get("status") not in {"running", "queued"}:
            continue
        live = run.get("live") or {}
        texts.append(live.get("progress") or run.get("message") or run.get("progress"))
    if kind == "task":
        texts.append(row.get("progress"))
    return next((value.strip()[:180] for value in texts if isinstance(value, str) and value.strip()), "")


def _tiktok_url(posted, kind):
    """The registered post link, or the standard one built from the account and post ID."""
    if not posted or not posted.get("post_id"):
        return ""
    url = str(posted.get("post_url") or "")
    if url.startswith(("https://", "http://")):
        return url
    account = str(posted.get("account") or "").lstrip("@")
    return f"https://www.tiktok.com/@{quote(account or '_', safe='')}/video/{posted['post_id']}"


def _bound_posts(row, kind):
    """Every video bound to this order, oldest first, one entry per post (its latest registration wins)."""
    records = row.get("publication_posts")
    latest = {}
    for record in records or []:
        if isinstance(record, dict) and record.get("post_id"):
            latest[record["post_id"]] = record
    return [{"post_id": post, "url": _tiktok_url(record, kind)} for post, record in latest.items()]


def _all_calendar_marked(identifier, row, overrides):
    """Whether every current timeline of an order is marked uploaded to the publish calendar. The newest keeps the
    order's own entry; each older one has its own (video-publication.js builds the same IDs)."""
    if not isinstance(overrides, dict):
        return False
    lines = list(reversed(active_timelines(row))) if isinstance(row.get("timelines"), list) else []
    keys = [identifier] + [f"{identifier}_tl_{re.sub(r'[^A-Za-z0-9-]', '', str(line.get('unique_id') or ''))[:120]}" for line in lines[1:]]
    return all(isinstance(overrides.get(key), dict) and overrides[key].get("calendar_uploaded") is True for key in keys)


def _calendar_state(posted, own, kind):
    """Registration mark once posted (None for old records without one); otherwise the manual mark, default not uploaded."""
    if kind != "video":
        return None
    if posted:
        return posted["calendar_uploaded"] if isinstance(posted.get("calendar_uploaded"), bool) else None
    return own.get("calendar_uploaded") is True


def _finished(kind, stage, published_on, archived, today=None):
    """已完结: a video posted more than FINISH_AFTER_DAYS ago, or one marked 不发了. Archived rows stay 已归档."""
    if kind != "video" or archived:
        return False
    if stage == "skipped":
        return True
    if stage != "published" or not isinstance(published_on, str):
        return False
    try:
        posted = datetime.strptime(published_on[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return ((today or datetime.now().date()) - posted).days >= FINISH_AFTER_DAYS


def _workflow_steps(row, values):
    """Completion is a current-version fact, independent of the queue's status label."""
    steps = [{"key": key, "label": label, "done": bool(done), "active": False,
              "hash": _hash("production", row["id"], panel)} for key, label, done, panel in values]
    pending = next((step for step in steps if not step["done"]), None)
    if pending:
        pending["active"] = True
    return steps


def _video_workflow(row):
    take = next((take for take in row.get("takes", []) if take.get("id") == row.get("selected_take")), None)
    timeline = current_timeline(row) if take else None
    # Archiving changes visibility, not the version that was reviewed.
    try:
        source = reviewed_source({**row, "archived": False})
    except VoiceError:
        source = None
    edited = bool(source)
    publication = row.get("current_publication") or row.get("publication") or {}
    published = bool(edited and publication.get("post_id") and publication.get("published_on") and
                     publication.get("source_current") is True and
                     publication.get("source_signature") == fingerprint(source))
    steps = _workflow_steps(row, [
        ("lines", "脚本", bool(row.get("lines")), "lines"),
        ("voice", "配音", take, "voice"),
        ("timeline", "时间线", timeline and timeline.get("saved") is not False, "timeline"),
        ("edit", "剪辑审查", edited, "edit"),
        ("publish", "发布", published, "publish"),
    ])
    if all(step["done"] for step in steps):
        note = "当前视频已完成剪辑审查并登记发布"
    elif edited:
        note = ("剪辑已人工完成，尚未登记发布" if manual_completion_current(row)
                else "剪辑审查已完成，尚未登记发布")
        if publication.get("post_id"):
            note = "当前剪辑审查已完成，已有发布记录对应历史版本"
    else:
        note = {"lines": "待完成脚本", "voice": "待选用配音", "timeline": "待建立并保存当前配音的时间线",
                "edit": _edit_note(row)}[next(step["key"] for step in steps if not step["done"])]
    return steps, note


def _edit_note(row):
    """While 剪辑审查 is the open step, the note gives the same reason as the order's status chip."""
    status = row.get("status") or job_status(row)
    if status == "needs_repair" or row.get("recovery_operations"):
        return "剪辑结果待修正"
    return {"needs_material": "素材不足，补齐后重新剪辑", "needs_verification": "素材待核实",
            "failed": "上次剪辑失败", "cancelled": "剪辑已停止", "interrupted": "剪辑已中断",
            "verification_failed": "剪辑结果待核验", "unverified": "剪辑结果待核验",
            "awaiting_review": "剪辑结果待人工审查"}.get(status, "当前时间线尚未完成剪辑审查")


def _workflow(row, kind):
    if kind != "video":
        return {}
    steps, note = _video_workflow(row)
    complete = all(step["done"] for step in steps)
    return {"workflow_steps": steps, "workflow_complete": complete,
            "workflow_label": "视频" + ("链已完结" if complete else "链未完结"), "summary_note": note,
            "unit_count": len(row.get("lines") or []), "unit_label": "个意群"}


def build_content_items(tasks, scripts, jobs, management=None, detached=False, variants=None):
    """Return stable queue rows without modifying task/script/production facts.

    detached=True means the caller passes fresh snapshots nobody else keeps, so they are read in place
    instead of being copied again.

    Lists or public catalog dictionaries are accepted. A script catalog's
    reviewed variant_links may supply direction labels, but never merge rows.
    Management holds each row's own archive mark; nothing is inherited.
    variants, when given, is {"owners": {(kind, id): {"direction": (product, direction_id), "indexes": set}},
    "labels": {(product, direction_id): [(label, structure), …]}}: which catalog variant each script, order and
    writing task belongs to, as the direction page assigns them. Without it rows keep their older names.
    """
    task_rows, script_rows = _rows(tasks, "tasks", detached), _rows(scripts, "scripts", detached)
    script_rows = [CreationStore.script_lifecycle(row) if "lifecycle_status" not in row else row for row in script_rows]
    job_rows = _rows(jobs, "jobs", detached)
    variant_links = _rows(scripts, "variant_links", detached) if isinstance(scripts, dict) else []
    links_by_record = {(row.get("kind"), row.get("record_id")): row for row in variant_links}
    metadata = management or {}
    overrides = metadata.get("items", metadata) if isinstance(metadata, dict) else {}
    revision = metadata.get("revision", "") if isinstance(metadata, dict) else ""
    by_task = {row.get("task_id", row.get("id")): row for row in task_rows}
    by_script = {row.get("id"): row for row in script_rows}
    families = {}
    for row in script_rows:
        families.setdefault(row.get("family_id") or row["id"], []).append(row)
    for rows in families.values():
        rows.sort(key=lambda row: (row.get("version", 0), row.get("created_at", ""), row["id"]))
    used_script_ids = {row.get("script_id") for row in job_rows if row.get("script_id")}
    adopted_pairs = {_provenance(row) for row in script_rows if _provenance(row)}
    result = []

    def make(identifier, row, kind, related_scripts, related_tasks, production_ids, state):
        own = overrides.get(identifier, {}) if isinstance(overrides, dict) else {}
        own = own if isinstance(own, dict) else {}
        # 不发了 only settles a finished, unposted video; a later post or a reopened edit shows its real stage again.
        if kind == "video" and state[0] == "awaiting_publication" and own.get("skip_publish") is True:
            state = ("skipped", "不发了", "查看成片与发布", "publish", False, True)
        # Uploaded to the Notion publish calendar (by the upload, or marked by hand): the workbench's part is done.
        # With several current timelines each is its own video, so every one of them must be marked.
        elif kind == "video" and state[0] == "awaiting_publication" and _all_calendar_marked(identifier, row, overrides):
            state = ("uploaded", "完成", "查看成片与发布", "publish", False, True)
        stage, label, action, step, attention, completed = state
        source_archived = bool(row.get("archived") or kind == "script" and row.get("status") == "archived")
        archived = source_archived or bool(own.get("archived"))
        busy = bool(row.get("busy") or row.get("stopping") or row.get("voicing") or row.get("timeline_building") or
                    row.get("status") in {"running", "queued", "editing"} or
                    any(run.get("status") in {"running", "queued"} for run in row.get("edits", []) if isinstance(run, dict)))
        attention = bool(not archived and not completed and not busy and attention)
        if kind == "task":
            destination = _hash("ai", row.get("task_id", row.get("id")))
        elif kind == "script":
            destination = _hash("content", row["id"])
            source = _source(row)
            source_task = by_task.get(source.get("task_id"))
            task_context = (source_task or {}).get("context") or {}
            task_product = (source_task or {}).get("product") or (task_context.get("product") if isinstance(task_context, dict) else "")
            if (not archived and not completed and _provenance(row) and source_task and
                    task_product == row.get("product")):
                destination = _hash("ai", source["task_id"]) + "?" + urlencode({
                    "round": source["round_id"], "adopted": row["id"]})
        else:
            destination = _hash("production", row["id"], step)
        record_links = []
        for task_id in related_tasks:
            record_links.append(_link("协作记录", _hash("ai", task_id)))
        for script in related_scripts:
            record_links.append(_link(f"采用稿版本详情 · 第 {script.get('version', 1)} 版", _hash("content", script["id"])))
        if production_ids:
            record_links.append(_link("视频制作单", _hash("production", row["id"])))
            publication = row.get("current_publication") or row.get("publication")
            if isinstance(publication, dict) and publication.get("post_id") and publication.get("published_on"):
                record_links.append(_link("发布登记" if stage == "published" else "历史发布记录",
                                          _hash("production", row["id"], "publish")))
        posted = row.get("current_publication") if kind == "video" and stage == "published" else None
        context = row.get("context") or {}
        context = context if isinstance(context, dict) else {}
        direction = row.get("direction_id") or context.get("direction_id") or ""
        reviewed_link = row.get("variant_link") or links_by_record.get(("production" if production_ids else "script", row.get("id")))
        if not direction and related_scripts:
            exact = next((entry for entry in related_scripts if entry["id"] == row.get("script_id")), None)
            if exact:
                direction = exact.get("direction_id") or ""
                reviewed_link = reviewed_link or exact.get("variant_link")
        if not direction and isinstance(reviewed_link, dict) and reviewed_link.get("product") == row.get("product"):
            direction = reviewed_link.get("direction_id") or ""
        sources = [row, *related_scripts, *(by_task[key] for key in related_tasks if key in by_task), own]
        exact_script = by_script.get(row.get("script_id")) if kind == "video" else None
        exact_task_id = _source(row).get("task_id") or _source(exact_script or {}).get("task_id")
        owner_key = (("task", row.get("task_id", row.get("id"))) if kind == "task" else
                     ("script", row["id"]) if kind == "script" else ("production", row["id"]))
        placed = (variants.get("owners") or {}).get(owner_key) if variants is not None else None
        if placed and not direction:
            direction = placed["direction"][1]
        place = (product_key(row.get("product") or context.get("product") or ""), direction)
        owners = None if variants is None else placed["indexes"] if placed and placed["direction"] == place else set()
        title, subtitle = content_heading(row, kind, direction=direction, script=exact_script,
                                          task=by_task.get(exact_task_id), owners=owners,
                                          labels=(variants or {}).get("labels", {}).get(place, ()))
        result.append({"id": identifier, "title": title, "subtitle": subtitle, "source_title": row.get("title") or "",
                       "timeline_name": timeline_label(row) if kind == "video" else "",
                       "created_at": task_created_at(row) if kind == "task" else row.get("created_at") or "",
                       "product": row.get("product") or context.get("product") or "", "direction_id": direction,
                       "kind": kind, "task_action": row.get("action", "variants") if kind == "task" else "",
                       "kind_label": {"review": "审稿"}.get(row.get("action"), "协作候选") if kind == "task" else "",
                       "stage": stage, "stage_label": label, "next_label": action,
                       "next_hash": destination, "updated_at": _stamp(sources), "archived": archived,
                       "source_archived": source_archived, "history": bool(archived or completed),
                       "busy": busy, "needs_attention": attention, "task_ids": list(dict.fromkeys(related_tasks)),
                       "progress_text": _progress_text(row, kind) if busy else "",
                       "script_ids": [script["id"] for script in related_scripts], "production_ids": production_ids,
                       "links": record_links, "management": {"archived": archived},
                       "management_revision": revision, "version_count": len(related_scripts),
                       # Posted but not yet on the publish calendar; records saved before the mark existed stay untracked.
                       "calendar_pending": bool(posted and not archived and posted.get("calendar_uploaded") is False),
                       # True/False once posted; None when unposted or saved before the mark existed.
                       "calendar_uploaded": _calendar_state(posted, own, kind),
                       "post_url": _tiktok_url(posted, kind),
                       "posts": _bound_posts(row, kind) if posted else [],
                       "published_on": (posted or {}).get("published_on") or "",
                       "finished": _finished(kind, stage, (posted or {}).get("published_on"), archived),
                       "skip_reason": str(own.get("skip_reason") or "") if stage == "skipped" else "",
                       **_workflow(row, kind)})

    def related(row, include_family=False):
        script = by_script.get(row.get("script_id"))
        rows = families.get(script.get("family_id") or script["id"], [script]) if script and include_family else [script] if script else []
        pairs = [_provenance(row), *(_provenance(entry) for entry in rows)]
        return rows, list(dict.fromkeys(pair[0] for pair in pairs if pair))

    for row in job_rows:
        related_scripts, related_tasks = related(row)
        make(f"video:{row['id']}", row, "video", related_scripts, related_tasks, [row["id"]], _video_stage(row))
    by_content_id = {row["id"]: row for row in result}
    for family, versions in families.items():
        # A newer unproduced version stays available; old versions used by a
        # production order are still visible through that exact order's links.
        latest = next((row for row in reversed(versions) if not row.get("archived") and row.get("status") != "archived"), versions[-1])
        if latest["id"] in used_script_ids:
            continue
        related_tasks = list(dict.fromkeys(pair[0] for entry in versions if (pair := _provenance(entry))))
        status = latest.get("lifecycle_status") or latest.get("status") or "adopted"
        state = {
            "published": ("published", "已发布", "查看脚本与发布关联", "", False, True),
            "reviewed": ("reviewed", "已复盘", "查看复盘关联", "", False, True),
            "filmed": ("filmed", "已拍摄", "继续制作", "", True, False),
            "archived": ("archived", "已归档", "查看历史版本", "", False, True),
        }.get(status, ("adopted", "待制作", "开始制作", "", True, False))
        make(f"script:{family}", latest, "script", versions, related_tasks, [], state)
        # Never merged or settled: a script sharing a title with existing orders only says so, for the user to judge.
        # Orders made from an earlier version of this same script are its own history, not another script.
        title, family_ids = str(latest.get("title") or "").strip(), {entry["id"] for entry in versions}
        if title and not result[-1]["history"]:
            result[-1]["same_title_orders"] = [
                {"id": row["id"], "stage_label": by_content_id[f"video:{row['id']}"]["stage_label"]}
                for row in job_rows
                if row.get("script_id") not in family_ids and str(row.get("title") or "").strip() == title
                and not by_content_id[f"video:{row['id']}"]["archived"]]
    settled = ai_task_resolutions(task_rows, job_rows)
    for row in task_rows:
        task_id = row.get("task_id", row.get("id"))
        # A retrospective studies a posted video; it is worked from 待复盘 and the video's chain, not as content.
        if not task_id or row.get("action") == "retrospective":
            continue
        status, disposition = row.get("status"), row.get("management_state", "pending_selection")
        resolution = settled.get(task_id) if disposition not in {"needs_revision", "adopted", "abandoned"} else None
        writing = (row.get("action") or "variants") == "variants"
        result_kind = "审稿" if row.get("action") == "review" else "协作"
        rounds = row.get("rounds") or []
        latest_round = (rounds[-1].get("round_id") if rounds and isinstance(rounds[-1], dict) else
                        row.get("latest_round_id") or row.get("round_id"))
        linked = any(pair[0] == task_id for pair in adopted_pairs)
        new_candidate = bool(latest_round and (task_id, latest_round) not in adopted_pairs)
        if linked and not row.get("archived") and disposition != "needs_revision" and status not in {"running", "queued", "failed", "interrupted"} and not new_candidate:
            continue
        if not writing:
            if status in {"running", "queued"}:
                state = (status, f"{result_kind}中" if status == "running" else "排队中", f"查看{result_kind}进度", "", False, False)
            elif disposition in {"adopted", "abandoned"}:
                state = (disposition, f"已确认{result_kind}" if disposition == "adopted" else "已放弃", f"查看{result_kind}记录", "", False, True)
            elif disposition == "needs_revision":
                state = ("needs_revision", f"待继续{result_kind}", f"继续{result_kind}", "", True, False)
            elif status == "completed":
                state = ("pending_confirmation", f"待确认{result_kind}", f"查看{result_kind}结论", "", True, False)
            else:
                state = (status or "unknown", {"failed": f"{result_kind}失败", "interrupted": f"{result_kind}已中断", "cancelled": f"{result_kind}已停止"}.get(status, "待处理"), f"查看并继续{result_kind}", "", True, False)
        elif status in {"running", "queued"}:
            state = (status, "生成中" if status == "running" else "排队中", "查看生成进度", "", False, False)
        elif disposition in {"adopted", "abandoned"}:
            state = (disposition, "已采纳" if disposition == "adopted" else "已放弃", "查看协作记录", "", False, True)
        elif disposition == "needs_revision":
            state = ("needs_revision", "待继续修改", "继续修改候选", "", True, False)
        elif resolution and resolution["kind"] in {"rerun", "refilled"}:
            # Settled by a later run: kept in 全部 for the record, out of 待我处理.
            state = ("superseded", "已重跑完成", "查看协作记录", "", False, True)
        elif resolution and resolution["kind"] == "written_back":
            # A fill's result is already on its 制作单; the candidate can still be adopted from the task page.
            state = ("written_back", "已写回制作单", "查看协作记录", "", False, True)
        elif status == "completed":
            state = ("pending_selection", "未写回" if resolution and resolution["kind"] == "not_written" else "待选稿",
                     "查看并选择候选", "", True, False)
        else:
            state = (status or "unknown", {"failed": "生成失败", "interrupted": "生成已中断", "cancelled": "生成已停止"}.get(status, "待处理"), "查看并继续任务", "", True, False)
        make(f"task:{task_id}", row, "task", [], [task_id], [], state)
        if resolution and resolution.get("by_job"):
            result[-1]["links"].append(_link("视频制作单", _hash("production", resolution["by_job"])))
    label_task_starts(result)
    result.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
    return result
