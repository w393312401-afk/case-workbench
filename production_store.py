"""Durable production records: voice library, reading table and per-script production jobs.

runtime/production is business data. A job freezes the adopted script lines and meaning-group table;
voiceover takes are appended and never overwritten. Audio files live beside the catalog.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import time
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
from uuid import uuid4

from direction_store import fingerprint, now, product_key
from file_identity import file_identity
from elevenlabs_client import DEFAULT_MODEL, VOICE_ID, VoiceError, clean_settings, model
from production_operations import file_lock
from production_naming import production_title
from production_validation import MANUAL_SCOPE, delivery_gap_status, shot_signature

JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")
TAKE_ID = re.compile(r"TAK-[0-9a-f]{12}")
JOB_STATUSES = {"draft", "voiced", "timeline", "editing", "awaiting_review", "approved", "needs_material", "needs_repair",
                "needs_verification", "verification_failed", "unverified", "failed", "cancelled", "interrupted"}
TIMELINE_TASK_STATUSES = {"pending", "in_progress", "completed"}
# Finished voiceovers and timelines are appended whatever else changed while they were being made.
REVISION_FREE = {"take", "job_fill_start", "job_fill_finish", "timeline", "timeline_location", "timeline_subtitles", "timeline_hook", "timeline_campaign", "job_completion", "edit_start", "edit_bind_timeline", "edit_finish", "edit_recheck", "edit_audio_repair", "edit_approve", "edit_baseline", "edit_delete", "timeline_delete", "job_delete"}
EDIT_ID = re.compile(r"EDT-[0-9a-f]{12}")
PUBLICATION_ID = re.compile(r"PUB-[0-9a-f]{12}")
# Timeline frame rates as exact ratios; names are what DaVinci Resolve shows.
FRAME_RATES = {"23.976": (24000, 1001), "24": (24, 1), "25": (25, 1), "29.97": (30000, 1001), "30": (30, 1),
               "50": (50, 1), "59.94": (60000, 1001), "60": (60, 1)}
CREATOR = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,23}")


def take_quality(take, expected_groups=None):
    """Only trustworthy, ordered, finite word timing may drive timeline markers."""
    groups = take.get("groups") if isinstance(take, dict) else None
    if not isinstance(groups, list) or not groups or (expected_groups is not None and len(groups) != expected_groups):
        return False
    previous = 0.0
    for number, group in enumerate(groups, 1):
        if not isinstance(group, dict) or group.get("number") != number:
            return False
        values = [group.get(key) for key in ("start", "end", "coverage")]
        if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in values):
            return False
        start, end, coverage = values
        if start < previous or end <= start or not 0.6 <= coverage <= 1:
            return False
        previous = end
    duration = take.get("duration")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, (float, int)) or
                                 not math.isfinite(duration) or previous > duration + 0.1):
        return False
    return True


def current_timeline(job):
    """The latest built timeline of the selected voiceover is the manual completion target."""
    take_id = job.get("selected_take")
    return next((row for row in reversed(job.get("timelines", []))
                 if take_id and row.get("take_id") == take_id and not row.get("archived")), None)


def manual_receipts(job):
    """Every manual completion receipt of the order, by timeline.

    Each timeline of the selected voiceover is completed on its own. An order saved before that held one receipt under
    manual_completion; it is read as the receipt of its timeline and moved into manual_completions on the next write."""
    found = {}
    legacy = job.get("manual_completion")
    if isinstance(legacy, dict) and legacy.get("timeline_uid"):
        found[legacy["timeline_uid"]] = legacy
    for uid, receipt in (job.get("manual_completions") or {}).items():
        if isinstance(receipt, dict):
            found[uid] = receipt
    return found


def active_timelines(job):
    """Timelines that can be completed and published: not archived, built for the selected voiceover."""
    take_id = job.get("selected_take")
    return [row for row in job.get("timelines", []) if take_id and row.get("take_id") == take_id and not row.get("archived")]


def timeline_edits(job, timeline):
    """Every AI edit run made on this timeline, including a queued one that never got bound to it."""
    uid, key = timeline.get("unique_id"), timeline.get("operation_key")
    return [edit for edit in job.get("edits", [])
            if edit.get("timeline_uid") == uid or (key and edit.get("timeline_operation_key") == key)]


def timeline_running(job, timeline):
    """An AI edit is still running on this very timeline (or on the build that will become it)."""
    uid = timeline.get("unique_id")
    return any(edit.get("status") == "running" and (edit.get("timeline_uid") == uid or
               (edit.get("waiting_timeline") and timeline.get("operation_key") and
                edit.get("timeline_operation_key") == timeline["operation_key"]))
               for edit in job.get("edits", []))


def timeline_manual_receipt(job, timeline):
    """The receipt that certifies this timeline: its own voiceover is selected, it is saved, and it is not being edited again."""
    # A public view has no manual_completions; each of its timelines carries the receipt that applies to it.
    receipt = manual_receipts(job).get((timeline or {}).get("unique_id")) or (timeline or {}).get("manual_completion")
    if (receipt and receipt.get("at") and not timeline.get("archived") and timeline.get("saved") is not False and
            receipt.get("take_id") == job.get("selected_take") == timeline.get("take_id") and not timeline_running(job, timeline)):
        return receipt
    return None


def manual_completion_current(job):
    """Whether any timeline of the selected voiceover has a valid manual completion."""
    return any(timeline_manual_receipt(job, row) for row in active_timelines(job))


def current_manual_receipt(job):
    """One receipt that stands for the order: the current timeline's, else the latest one completed."""
    current = current_timeline(job)
    first = timeline_manual_receipt(job, current) if current else None
    if first:
        return first
    found = [receipt for row in active_timelines(job) if (receipt := timeline_manual_receipt(job, row))]
    return max(found, key=lambda receipt: receipt["at"]) if found else None


def timeline_task_status(job, timeline):
    """Progress is independent of AI execution and verification; infer only legacy records."""
    explicit = timeline.get("task_status")
    if isinstance(explicit, str) and explicit in TIMELINE_TASK_STATUSES:
        return explicit
    uid = timeline.get("unique_id")
    receipt = manual_receipts(job).get(uid) or {}
    if receipt.get("at") and receipt.get("take_id") == timeline.get("take_id"):
        return "completed"
    latest = next((edit for edit in reversed(job.get("edits", [])) if edit.get("timeline_uid") == uid), None)
    if (latest and latest.get("approval") and not latest.get("result_pending") and
            (skill_delivery_reviewable(latest) if latest.get("completion_policy") == "skill_delivery" else
             latest.get("status") == "awaiting_review")):
        return "completed"
    return "in_progress" if latest else "pending"


def skill_delivery_reviewable(edit):
    """Use the latest explicit coverage check when present; keep the AI report as history."""
    # Older saved skill deliveries recorded completion only in status.
    execution = edit.get("execution_status", "completed" if edit.get("status") == "completed" else None)
    if (edit.get("status") not in {"completed", "awaiting_review"} or execution != "completed" or
            edit.get("result_pending")):
        return False
    validation = edit.get("validation")
    if validation:
        return bool(validation.get("scope") == "current_coverage" and validation.get("status") == "awaiting_review" and
                    validation.get("saved") is True and isinstance(edit.get("summary"), dict) and
                    isinstance(edit["summary"].get("picture_gaps"), list) and not edit["summary"]["picture_gaps"])
    return (edit.get("report") or {}).get("saved") is True and delivery_gap_status(edit.get("report")) is None


def job_status(job):
    """A past timeline for another take cannot certify the currently selected version."""
    take = job.get("selected_take")
    if not take:
        return "draft"
    archived = [row for row in job.get("timelines", []) if row.get("archived")]
    if any(edit.get("status") == "running" and edit.get("waiting_timeline") and edit.get("take_id") == take and
           not any(row.get("unique_id") == edit.get("timeline_uid") or
                   (row.get("operation_key") and row.get("operation_key") == edit.get("timeline_operation_key"))
                   for row in archived)
           for edit in job.get("edits", [])):
        return "editing"
    targets = {row.get("unique_id"): row for row in job.get("timelines", [])
               if row.get("take_id") == take and not row.get("archived")}
    if not targets:
        return "voiced"
    if manual_completion_current(job):
        return "approved"
    latest = next((edit for edit in reversed(job.get("edits", [])) if edit.get("timeline_uid") in targets), None)
    if not latest:
        current = list(targets.values())[-1]
        return "verification_failed" if current.get("saved") is False else "timeline"
    state = latest.get("status")
    if state == "running":
        return "editing"
    if state == "completed":
        if latest.get("completion_policy") == "skill_delivery":
            if (latest.get("report") or {}).get("saved") is not True:
                return "needs_repair"
            gap_state = delivery_gap_status(latest.get("report"))
            if gap_state:
                return gap_state
            return "approved" if latest.get("approval") else "awaiting_review"
        return "unverified"
    if state == "awaiting_review" and latest.get("approval"):
        return "approved"
    return state if state in JOB_STATUSES else "verification_failed"


def current_edit(job, edit):
    """Only the latest attempt on a timeline of the selected voiceover decides the job, so only it can be completed."""
    timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == edit.get("timeline_uid")), None)
    latest = next((item for item in reversed(job.get("edits", [])) if item.get("timeline_uid") == edit.get("timeline_uid")), None)
    return bool(timeline and not timeline.get("archived") and job.get("selected_take") and
                timeline.get("take_id") == job["selected_take"] and latest is edit)


def public_job(job, result_root=None):
    result = copy.deepcopy(job)
    result["display_title"] = production_title(job)
    for timeline in result.get("timelines", []):
        timeline["archived"] = bool(timeline.get("archived"))
        timeline["task_status"] = timeline_task_status(job, timeline)
        # Each timeline carries its own completion; a running edit blocks only the timeline it edits.
        timeline["manual_completion"] = copy.deepcopy(timeline_manual_receipt(job, timeline))
        timeline["manual_completion_blocked"] = ("这条时间线的剪辑记录尚未结束登记，请先结束剪辑或恢复结果，再修改成功标记。"
                                                  if timeline_running(job, timeline) else "")
    # The order-level field keeps the single-receipt meaning for readers that have no timeline in hand.
    # A receipt that is not valid right now (its timeline archived, say) stays as stored: restoring revives it.
    standing = current_manual_receipt(job)
    if standing:
        result["manual_completion"] = copy.deepcopy(standing)
    result.pop("manual_completions", None)
    # Readback may expose an ended-but-unsaved worker as interrupted or needing
    # recovery. Its running catalog entry still blocks completion mutations.
    result["manual_completion_blocked"] = (
        "剪辑记录尚未结束登记，请先结束剪辑或恢复结果，再修改成功标记。"
        if any(edit.get("status") == "running" for edit in job.get("edits", [])) else "")
    for edit in result.get("edits", []):
        if (edit.get("status") == "completed" and not edit.get("validation") and
                edit.get("completion_policy") != "skill_delivery"):
            edit["execution_status"] = "completed"
            edit["status"] = "unverified"
        if result_root is not None and edit.get("status") == "running" and EDIT_ID.fullmatch(str(edit.get("id"))):
            path = Path(result_root) / edit["id"] / "result.json"
            if path.is_file():
                try:
                    pending = json.loads(path.read_text(encoding="utf-8"))
                    if pending.get("edit_id") == edit["id"] and pending.get("job_id") == job.get("id") and not pending.get("persisted_at"):
                        if edit.get("completion_policy") == "skill_delivery":
                            edit.update(status="needs_repair", result_pending=True, validation=None,
                                error="剪辑已结束，结果已保留但制作资料尚未登记；结果待恢复。")
                        else:
                            edit.update(status="verification_failed", result_pending=True,
                                validation={"status": "verification_failed", "issues": [{"code": "result_pending", "message":
                                    "剪辑已结束，结果已保留但制作资料尚未登记；点击重新核验恢复记录。"}]})
                except (OSError, ValueError, TypeError):
                    if edit.get("completion_policy") == "skill_delivery":
                        edit.update(status="needs_repair", result_pending=True, validation=None,
                            error="剪辑结果恢复记录无法读取，请保留原文件并核实；结果待恢复。")
                    else:
                        edit.update(status="verification_failed", result_pending=True,
                            validation={"status": "verification_failed", "issues": [{"code": "result_unreadable", "message":
                                "剪辑结果恢复记录无法读取，请保留原文件并核实。"}]})
    result["status"] = job_status(result)
    return result


def text(value, label, required=True, limit=12000):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise VoiceError(400, f"{label}格式无效。")
    return value.strip()


SUPPLEMENT_FIELDS = (("hook_visual", "开头画面", 1000), ("hook_text", "开头屏显", 300), ("hook_text_zh", "开头屏显中文", 300),
                     ("pickups", "补拍清单", 4000))
# Problems a job gets when its script came without a 意群表; they go away once every line has its 画面要求.
MISSING_TABLE = "在制作单里补填"


def supplement_markdown(job):
    """The 原稿全文 for a job whose script carried no writing-skill delivery, from what was filled in by hand, in the
    writing skill's order: 开头画面、开头屏显、口播、对应中文、补拍清单、发布文案."""
    extra = job.get("supplement") or {}
    block = lambda values: "```text\n" + "\n".join(values) + "\n```"
    parts = []
    if extra.get("hook_visual"):
        parts.append(f"**开头画面：** {extra['hook_visual']}")
    if extra.get("hook_text"):
        parts.append(f"**开头屏显：** `{extra['hook_text']}`" + (f"（{extra['hook_text_zh']}）" if extra.get("hook_text_zh") else ""))
    parts.append("**口播（配音用）**\n\n" + block([line["spoken"] for line in job["lines"]]))
    if any(line.get("zh") for line in job["lines"]):
        parts.append("**对应中文**\n\n" + block([line.get("zh") or "" for line in job["lines"]]))
    if extra.get("pickups"):
        parts.append(f"**补拍清单：** {extra['pickups']}")
    if extra.get("captions"):
        parts.append("**发布文案**\n\n" + "\n\n".join(block([caption]) for caption in extra["captions"]))
    return "\n\n".join(parts)


def reading_pattern(written):
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(written) + r"(?![A-Za-z0-9])")


def apply_readings(lines, readings):
    """Replace written forms with how they should be read; returns (lines, substitutions)."""
    ordered = sorted(readings, key=lambda item: len(item["written"]), reverse=True)
    result, count = [], 0
    for line in lines:
        for item in ordered:
            line, hits = reading_pattern(item["written"]).subn(item["spoken"], line)
            count += hits
        result.append(line)
    return result, count


# Large immutable results live outside the catalog; small result stubs remain
# sufficient for the existing deletion and age-based compaction policies.
RECEIPT_FULL_HOURS = 24
RECEIPT_DAYS = 30
RECEIPT_INLINE_BYTES = 16 * 1024
RECEIPT_MAX_BYTES = 64 * 1024 * 1024
CATALOG_MAX_BYTES = 64 * 1024 * 1024
# Every save keeps the previous catalog; the newest ones and one per recent day are enough to recover from.
BACKUP_KEEP = 50
BACKUP_DAYS = 30
RECEIPT_RECORDS = {"job": "jobs"}


def compact_receipt_result(result):
    if not isinstance(result, dict):
        return result
    compact = {}
    for key, value in result.items():
        if isinstance(value, dict) and "id" in value:
            compact[key] = {name: value[name] for name in ("id", "job_id") if name in value}
        elif len(json.dumps(value, ensure_ascii=False, default=str)) <= 2000:
            compact[key] = copy.deepcopy(value)
    return compact


class ProductionStore:
    def __init__(self, runtime, lock=None):
        self.root = Path(runtime) / "production"
        self.path = self.root / "catalog.json"
        self.audio = self.root / "audio"
        self.lock = lock or threading.RLock()
        self._catalog_owner = threading.local()
        self._catalog_signature = None
        self._catalog_value = None
        self._catalog_revision = None

    @contextmanager
    def catalog_transaction(self):
        """Serialize the read/modify/replace transaction across restarted or parallel processes."""
        if getattr(self._catalog_owner, "held", False):
            yield
            return
        with file_lock(self.root / "catalog.lock", "制作资料正在由另一项操作保存，请用原请求恢复。"):
            self._catalog_owner.held = True
            try:
                yield
            finally:
                self._catalog_owner.held = False

    @staticmethod
    def empty():
        return {"schema_version": 1, "voices": [], "defaults": {}, "readings": [], "jobs": [], "requests": {},
                "settings": {"creators": [], "products": {}}}

    @staticmethod
    def product_settings(data, product):
        """Saved DaVinci settings for a product, completed with the defaults used by the existing projects."""
        key = product_key(product)
        saved = data["settings"]["products"].get(key, {})
        return {"project": key, "code": re.sub(r"\D", "", key) or key, "fps": "29.97", "width": 1080, "height": 1920,
                "audio_bin": "音频字幕", "timeline_bin": "时间线", **saved}

    @staticmethod
    def material_combos(data, product, limit=3):
        """Material timeline sets this product's AI edits used, most used first, ties broken by the latest use."""
        key = product_key(product)
        combos = {}
        for job in data.get("jobs", []):
            if job.get("product") != key:
                continue
            for edit in job.get("edits", []):
                names = [str(name).strip() for name in edit.get("materials") or [] if str(name).strip()]
                if not names:
                    continue
                entry = combos.setdefault(frozenset(names), {"materials": list(dict.fromkeys(names)), "count": 0, "last": ""})
                entry["count"] += 1
                entry["last"] = max(entry["last"], str(edit.get("started_at") or ""))
        ranked = sorted(combos.values(), key=lambda item: (item["count"], item["last"]), reverse=True)
        return [{"materials": item["materials"], "count": item["count"]} for item in ranked[:limit]]

    def read(self):
        with self.lock:
            data = copy.deepcopy(self._read_catalog())
            for receipt in data["requests"].values():
                if isinstance(receipt, dict) and "result_ref" in receipt:
                    receipt["result"] = self._receipt_result(receipt, self.root)
            return data

    def records(self):
        """A private copy of the business records without the replay receipts.

        Page reads never need the receipts, and cloning every saved one made each
        read take seconds under the shared lock.
        """
        with self.lock:
            return copy.deepcopy({key: value for key, value in self._read_catalog().items() if key != "requests"})

    def _mutation_snapshot(self):
        """Copy mutable business records without cloning immutable replay history.

        Handlers only add or remove entries in requests; existing receipts are
        never modified. Public reads and replay results still receive deep
        copies, so this internal sharing cannot expose a saved receipt.
        """
        snapshot = self._read_catalog()
        data = copy.deepcopy({key: value for key, value in snapshot.items() if key != "requests"})
        data["requests"] = dict(snapshot["requests"])
        return data

    def clear_cache(self):
        """Discard snapshots after a runtime restore or an in-process replacement."""
        with self.lock:
            self._catalog_signature = self._catalog_value = self._catalog_revision = None

    def _signature(self):
        return file_identity(self.path)

    def _read_catalog(self):
        """Internal read-only snapshot; callers must copy records before returning them."""
        try:
            try:
                signature = self._signature()
            except FileNotFoundError:
                self.clear_cache()
                return self.empty()
            if signature == self._catalog_signature:
                return self._catalog_value
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("schema_version") != 1 or
                    any(not isinstance(value.get(key), list) for key in ("voices", "readings", "jobs")) or
                    any(not isinstance(value.get(key), dict) for key in ("defaults", "requests"))):
                raise ValueError("invalid production catalog")
            value.setdefault("settings", {"creators": [], "products": {}})  # catalogs written before phase two
            if signature == self._signature():
                self._catalog_signature, self._catalog_value = signature, value
                self._catalog_revision = None
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise VoiceError(500, "制作资料无法读取，请保留原文件并从备份恢复。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

    def _snapshot_revision(self, data):
        if data is not self._catalog_value:
            return self.revision(data)
        if self._catalog_revision is None:
            self._catalog_revision = self.revision(data)
        return self._catalog_revision

    def current_revision(self):
        """Current business revision without copying the historical receipts."""
        with self.lock:
            return self._snapshot_revision(self._read_catalog())

    def write(self, data):
        with self.lock, self.catalog_transaction():
            return self._write(data)

    def _write(self, data):
        if self.path.exists():
            raw = self.path.read_bytes()
            target = self.root / "backups" / (hashlib.sha256(raw).hexdigest() + ".json")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._prune_backups()
        # Persist complete results before publishing their references. A failed
        # catalog replace may leave an unused blob, never a missing saved result.
        data = self._externalize_request_results(data)
        encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(encoded) > CATALOG_MAX_BYTES:
            raise VoiceError(413, "制作资料超过容量限制，本次未截断或覆盖记录。")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".production-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.clear_cache()
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _externalize_request_results(self, data):
        requests = {}
        for key, receipt in data["requests"].items():
            if not isinstance(receipt, dict) or "result" not in receipt:
                requests[key] = receipt
                continue
            if "result_ref" in receipt:
                # read() exposes full results for compatibility; writes retain
                # only the index while the immutable referenced bytes stay put.
                requests[key] = {**receipt, "result": compact_receipt_result(receipt["result"])}
                continue
            encoded = (json.dumps(receipt["result"], ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
            if len(encoded) <= RECEIPT_INLINE_BYTES:
                requests[key] = receipt
                continue
            if len(encoded) > RECEIPT_MAX_BYTES:
                raise VoiceError(413, "单项制作回执超过容量限制，本次未截断或覆盖记录。")
            digest = hashlib.sha256(encoded).hexdigest()
            target = self.root / "request-results" / (digest + ".json")
            if not target.parent.resolve().is_relative_to(self.root.resolve()):
                raise VoiceError(500, "制作回执目录不属于当前资料目录。")
            if target.exists():
                if target.is_symlink() or target.stat().st_size != len(encoded) or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                    raise VoiceError(500, "制作回执文件校验失败，请保留原文件并从备份恢复。")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.parent.resolve().is_relative_to(self.root.resolve()):
                    raise VoiceError(500, "制作回执目录不属于当前资料目录。")
                fd, temporary = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            requests[key] = {**receipt, "result": compact_receipt_result(receipt["result"]),
                "result_ref": {"file": target.relative_to(self.root).as_posix(), "sha256": digest, "bytes": len(encoded)}}
        return {**data, "requests": requests}

    @staticmethod
    def _receipt_result(receipt, root=None):
        if "result_ref" not in receipt:
            return copy.deepcopy(receipt["result"])
        try:
            ref = receipt["result_ref"]
            if (not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str) or
                    not re.fullmatch(r"[0-9a-f]{64}", ref["sha256"]) or type(ref.get("bytes")) is not int or
                    not 0 < ref["bytes"] <= RECEIPT_MAX_BYTES or root is None or
                    ref.get("file") != "request-results/" + ref["sha256"] + ".json"):
                raise ValueError()
            root = Path(root).resolve()
            path = root / ref["file"]
            if path.is_symlink() or not path.resolve().is_relative_to(root) or path.stat().st_size != ref["bytes"]:
                raise ValueError()
            encoded = path.read_bytes()
            if len(encoded) != ref["bytes"] or hashlib.sha256(encoded).hexdigest() != ref["sha256"]:
                raise ValueError()
            return json.loads(encoded)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise VoiceError(500, "制作回执文件缺失或校验失败，请保留原文件并从备份恢复；本次未重复执行操作。") from exc

    def _prune_backups(self):
        """Keep the newest BACKUP_KEEP backups and, for BACKUP_DAYS days, the newest one of each day."""
        files = []
        for path in (self.root / "backups").glob("*.json"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                continue
        cutoff, days = time.time() - BACKUP_DAYS * 86400, set()
        for index, (modified, path) in enumerate(sorted(files, reverse=True)):
            day = date.fromtimestamp(modified)
            if index < BACKUP_KEEP or modified >= cutoff and day not in days:
                days.add(day)
                continue
            try:
                path.unlink()
            except OSError:
                continue

    def public(self, product=None):
        with self.lock:
            data = self._read_catalog()
            key = product_key(product) if product and product != "all" else None
            def selected(rows):
                return rows if key is None else [item for item in rows if item["product"] == key]
            result = {"voices": copy.deepcopy(data["voices"]), "defaults": copy.deepcopy(data["defaults"]),
                      "settings": copy.deepcopy(data["settings"]),
                      "readings": copy.deepcopy(selected(data["readings"])),
                      "jobs": [public_job(job, self.root / "edits") for job in selected(data["jobs"])],
                      "revision": self._snapshot_revision(data)}
            if key is not None:
                result["product_settings"] = copy.deepcopy(self.product_settings(data, key))
            return result

    def script_usage(self, product=None):
        """Small provenance projection for script lists, without voice or edit readback."""
        with self.lock:
            data = self._read_catalog()
            selected = product_key(product) if product and product != "all" else None
            result = []
            for item in data["jobs"]:
                if selected is not None and item.get("product") != selected:
                    continue
                row = {key: copy.deepcopy(item.get(key)) for key in ("id", "script_id", "archived")}
                row.update(kind="video", status=job_status(item))
                result.append(row)
            return result

    def job(self, identifier):
        with self.lock:
            found = next((item for item in self._read_catalog()["jobs"] if item["id"] == identifier), None)
            if not found:
                raise VoiceError(404, "这张制作单不存在。")
            return public_job(found, self.root / "edits")

    def associate_candidate(self, candidate_id, *, job_id, script_id, product, direction_id, variant_ref,
                            dry_run=False):
        """Attach a checked candidate without replacing voice, edit or frozen script records."""
        from creation_store import CreationError, CreationStore

        try:
            product, association = CreationStore._candidate_association(
                candidate_id, product, direction_id, variant_ref, script_id=script_id)
        except CreationError as exc:
            raise VoiceError(exc.status, str(exc)) from exc
        with self.lock, self.catalog_transaction():
            data = self._mutation_snapshot()
            job = self._find_job(data, job_id)
            if job.get("product") != product:
                raise VoiceError(409, "候选与制作单所属产品不一致，未补关联。")
            if job.get("script_id") != association["script_id"]:
                raise VoiceError(409, "制作单属于另一文案版本，未补关联。")
            if job.get("direction_id") and job["direction_id"] != association["direction_id"]:
                raise VoiceError(409, "制作单已有其他方向，不能覆盖。")
            previous = job.get("candidate_direction")
            if previous is not None and not CreationStore._same_candidate_association(previous, association):
                raise VoiceError(409, "制作单已有其他候选方向或变体关联，不能覆盖。")
            frozen_ref = (job.get("source") or {}).get("variant_ref")
            if frozen_ref is not None and not CreationStore._same_variant_identity(frozen_ref, association["variant_ref"]):
                raise VoiceError(409, "制作单原始来源已指定其他变体，不能覆盖关联。")
            changed = not job.get("direction_id") or previous is None
            job["direction_id"] = association["direction_id"]
            if previous is None:
                job["candidate_direction"] = copy.deepcopy(association)
            history = job.setdefault("history", [])
            if not any(row.get("event") == "candidate_direction" and row.get("candidate_id") == candidate_id
                       for row in history):
                changed = True
                history.append({"at": now(), "event": "candidate_direction",
                    "note": "按插件候选自动归入对应方向；原口播、配音和剪辑记录保持不变。",
                    **copy.deepcopy(association)})
            if changed:
                job["updated_at"] = now()
                if not dry_run:
                    self.write(data)
            return copy.deepcopy(job)

    def readings(self, product):
        with self.lock:
            return [copy.deepcopy(item) for item in self._read_catalog()["readings"] if item["product"] == product_key(product)]

    def voice(self, voice_id):
        with self.lock:
            return copy.deepcopy(next((item for item in self._read_catalog()["voices"] if item["voice_id"] == voice_id), None))

    def replay(self, kind, body):
        with self.lock:
            prior = self._read_catalog()["requests"].get(body.get("request_id")) if isinstance(body, dict) else None
            if prior:
                if prior["hash"] != fingerprint([kind, body]):
                    raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                return self._replayed(self._read_catalog(), prior, self.root)
            return None

    @staticmethod
    def _replayed(data, receipt, root=None):
        result = ProductionStore._receipt_result(receipt, root)
        if receipt.get("compact") and isinstance(result, dict):
            for key, collection in RECEIPT_RECORDS.items():
                stub = result.get(key)
                found = next((item for item in data.get(collection, []) if isinstance(stub, dict) and item.get("id") == stub.get("id")), None)
                if found:
                    result[key] = copy.deepcopy(found)
        return result

    @staticmethod
    def _prune_requests(data):
        moment = datetime.now(timezone.utc)
        stamp = moment.isoformat(timespec="microseconds")
        full_cutoff = (moment - timedelta(hours=RECEIPT_FULL_HOURS)).isoformat(timespec="microseconds")
        cutoff = (moment - timedelta(days=RECEIPT_DAYS)).isoformat(timespec="microseconds")
        for key, receipt in list(data["requests"].items()):
            if not isinstance(receipt, dict):
                continue
            if not receipt.get("at"):
                receipt = data["requests"][key] = {**receipt, "at": stamp, "full_until": ""}
            if receipt["at"] < cutoff:
                del data["requests"][key]
            elif "result" in receipt and not receipt.get("compact") and receipt.get("full_until", receipt["at"]) < full_cutoff:
                data["requests"][key] = {**{name: value for name, value in receipt.items() if name not in {"full_until", "result_ref"}},
                                         "result": compact_receipt_result(receipt["result"]), "compact": True}

    def mutate(self, kind, body, extra=None):
        if not isinstance(body, dict):
            raise VoiceError(400, "制作保存参数必须是对象。")
        key = body.get("request_id")
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
            raise VoiceError(400, "缺少保存标识，请刷新后重试。")
        request_hash = fingerprint([kind, body])
        with self.lock, self.catalog_transaction():
            data = self._mutation_snapshot()
            prior = data["requests"].get(key)
            if prior:
                if prior["hash"] != request_hash:
                    raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                return self._replayed(data, prior, self.root)
            if kind not in REVISION_FREE and body.get("revision") != self.revision(data):
                raise VoiceError(409, "制作资料已更新，请读取最新记录后重新保存。")
            handlers = {"voice": self._voice, "reading": self._reading, "job": self._job, "job_lines": self._job_lines, "job_fill_start": self._job_fill_start, "job_fill_finish": self._job_fill_finish,
                        "job_archive": self._job_archive, "take": self._take, "select_take": self._select_take,
                        "settings": self._settings, "timeline": self._timeline, "timeline_location": self._timeline_location,
                        "timeline_subtitles": self._timeline_subtitles, "timeline_hook": self._timeline_hook, "timeline_campaign": self._timeline_campaign, "job_completion": self._job_completion,
                        "timeline_archive": self._timeline_archive, "timeline_delete": self._timeline_delete,
                        "timeline_task_status": self._timeline_task_status,
                        "edit_start": self._edit_start, "edit_bind_timeline": self._edit_bind_timeline,
                        "edit_finish": self._edit_finish, "edit_recheck": self._edit_recheck,
                        "edit_audio_repair": self._edit_audio_repair,
                        "edit_approve": self._edit_approve, "edit_baseline": self._edit_baseline,
                        "edit_delete": self._edit_delete,
                        "job_delete": self._job_delete}
            if kind not in handlers:
                raise VoiceError(400, "不支持这项制作操作。")
            result = handlers[kind](data, body, extra)
            data["requests"][key] = {"hash": request_hash, "result": copy.deepcopy(result), "at": now()}
            self._prune_requests(data)
            self.write(data)
            return result

    @staticmethod
    def _find_job(data, identifier):
        found = next((item for item in data["jobs"] if item["id"] == identifier), None)
        if not found:
            raise VoiceError(404, "这张制作单不存在。")
        return found

    @staticmethod
    def _touch_voice(data, voice_id, lookup=None):
        """Every voice used for a take is kept in the library, with its usage counted."""
        found = next((item for item in data["voices"] if item["voice_id"] == voice_id), None)
        stamp = now()
        if not found:
            found = {"voice_id": voice_id, "note": "", "name": "", "category": "", "labels": {}, "preview_url": "",
                     "model_id": DEFAULT_MODEL, "settings": {}, "disabled": False, "created_at": stamp,
                     "updated_at": stamp, "last_used_at": "", "use_count": 0}
            data["voices"].append(found)
        if lookup:
            found.update({key: lookup[key] for key in ("name", "category", "labels", "preview_url") if key in lookup})
        return found

    def _voice(self, data, body, lookup):
        value = body.get("voice")
        if not isinstance(value, dict):
            raise VoiceError(400, "音色参数格式无效。")
        voice_id = value.get("voice_id")
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise VoiceError(400, "音色 ID 格式无效。")
        operation = body.get("operation", "save")
        existing = next((item for item in data["voices"] if item["voice_id"] == voice_id), None)
        if operation == "default":
            product = product_key(text(body.get("product"), "产品", limit=200))
            if not existing or existing["disabled"]:
                raise VoiceError(409, "只能把音色库里启用中的音色设为默认。")
            data["defaults"][product] = voice_id
            return {"voice": copy.deepcopy(existing), "defaults": copy.deepcopy(data["defaults"])}
        if operation in {"disable", "enable"}:
            if not existing:
                raise VoiceError(404, "音色库里没有这个音色。")
            existing.update(disabled=operation == "disable", updated_at=now())
            if existing["disabled"]:
                data["defaults"] = {key: value for key, value in data["defaults"].items() if value != voice_id}
            return {"voice": copy.deepcopy(existing), "defaults": copy.deepcopy(data["defaults"])}
        if operation != "save":
            raise VoiceError(400, "音色操作无效。")
        if not existing and not lookup:
            raise VoiceError(400, "新音色需先向 ElevenLabs 核对 ID。")
        voice = self._touch_voice(data, voice_id, lookup)
        model_id = value.get("model_id", voice["model_id"])
        model(model_id)
        voice.update(note=text(value.get("note", voice["note"]), "音色备注", False, 500), model_id=model_id,
                     settings=clean_settings(model_id, value.get("settings", voice["settings"])), updated_at=now())
        return {"voice": copy.deepcopy(voice), "defaults": copy.deepcopy(data["defaults"])}

    @staticmethod
    def _reading(data, body, extra):
        value = body.get("reading")
        if not isinstance(value, dict):
            raise VoiceError(400, "读法参数格式无效。")
        identifier = value.get("id")
        old = next((item for item in data["readings"] if item["id"] == identifier), None)
        if identifier and not old:
            raise VoiceError(404, "这条读法不存在。")
        if body.get("operation") == "remove":
            if not old:
                raise VoiceError(404, "这条读法已经删除。")
            data["readings"].remove(old)
            return {"removed": identifier}
        item = {"id": identifier or "RDG-" + uuid4().hex[:12], "product": product_key(text(value.get("product"), "产品", limit=200)),
                "written": text(value.get("written"), "稿中写法", limit=100), "spoken": text(value.get("spoken"), "配音读法", limit=200),
                "note": text(value.get("note", ""), "读法备注", False, 500), "updated_at": now()}
        if old and old["product"] != item["product"]:
            raise VoiceError(409, "读法不能改到另一产品。")
        if any(entry is not old and (entry["product"], entry["written"].lower()) == (item["product"], item["written"].lower())
               for entry in data["readings"]):
            raise VoiceError(409, "这个写法已有读法，请直接修改原记录。")
        if old:
            data["readings"][data["readings"].index(old)] = item
        else:
            data["readings"].append(item)
        return {"reading": copy.deepcopy(item)}

    @staticmethod
    def _job(data, body, resolved):
        if not isinstance(resolved, dict) or not resolved.get("lines"):
            raise VoiceError(400, "制作单缺少已核对的口播行。")
        stamp = now()
        spoken = "\n".join(line["spoken"] for line in resolved["lines"])
        item = {"id": "PRD-" + uuid4().hex[:12], **copy.deepcopy(resolved),
                "text_sha256": hashlib.sha256(spoken.encode()).hexdigest(), "creator": "",
                "takes": [], "selected_take": "", "timelines": [], "edits": [], "status": "draft", "archived": False,
                "history": [{"at": stamp, "event": "created", "note": "按已采纳脚本版本创建制作单"}],
                "created_at": stamp, "updated_at": stamp}
        data["jobs"].append(item)
        return {"job": copy.deepcopy(item)}

    def _job_lines(self, data, body, extra):
        job = self._find_job(data, body.get("job_id"))
        values = body.get("lines")
        if not isinstance(values, list) or len(values) != len(job["lines"]):
            raise VoiceError(400, "意群表须逐行提交，行数与口播一致。")
        for line, value in zip(job["lines"], values):
            if not isinstance(value, dict) or value.get("number") != line["number"]:
                raise VoiceError(400, "意群编号与口播行不一致，请刷新后重试。")
            for key, label in (("proof", "画面要证明什么"), ("tags", "素材标签"), ("evidence", "证据")):
                line[key] = text(value.get(key, line[key]), label, False, 2000)
        note = "修改意群表画面要求"
        if "supplement" in body:
            # 开头画面、屏显、补拍清单和发布文案 for a script that came without them; the spoken lines never change here.
            raw = body["supplement"]
            if not isinstance(raw, dict) or set(raw) - {key for key, _, _ in SUPPLEMENT_FIELDS} - {"captions"}:
                raise VoiceError(400, "补全内容格式无效。")
            supplement = {key: text(raw.get(key, ""), label, False, limit) for key, label, limit in SUPPLEMENT_FIELDS}
            captions = raw.get("captions", [])
            if not isinstance(captions, list) or len(captions) > 3:
                raise VoiceError(400, "发布文案最多 3 行。")
            supplement["captions"] = [value for value in (text(item, "发布文案", False, 600) for item in captions) if value]
            job["supplement"] = {key: value for key, value in supplement.items() if value}
            note = "补全脚本信息"
        if all(line.get("proof") for line in job["lines"]):
            job["problems"] = [item for item in job.get("problems", []) if MISSING_TABLE not in item]
        job["updated_at"] = now()
        job["history"].append({"at": job["updated_at"], "event": "lines", "note": note})
        return {"job": copy.deepcopy(job)}

    def _job_fill_start(self, data, body, extra):
        """An AI 写稿 round is filling this job's script information; the job remembers which one."""
        job = self._find_job(data, body.get("job_id"))
        task_id = body.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise VoiceError(400, "补全任务编号无效。")
        job["ai_fill"] = {"task_id": task_id, "status": "running", "started_at": now(), "error": ""}
        job["updated_at"] = now()
        job["history"].append({"at": job["updated_at"], "event": "fill", "note": "AI 按写稿技能补全脚本信息"})
        return {"job": copy.deepcopy(job)}

    def _job_fill_finish(self, data, body, extra):
        """The round's result: 意群表 columns by row and the full script markdown. The spoken lines never change here."""
        job = self._find_job(data, body.get("job_id"))
        fill = job.get("ai_fill") or {}
        if fill.get("task_id") != body.get("task_id"):
            raise VoiceError(409, "这张制作单已换了补全任务。")
        if body.get("error"):
            fill.update(status="failed", error=text(str(body["error"])[:600], "补全失败原因", False, 600), finished_at=now())
        else:
            values = body.get("lines")
            if not isinstance(values, list) or len(values) != len(job["lines"]):
                raise VoiceError(400, "补全结果行数与口播不一致。")
            for line, value in zip(job["lines"], values):
                for key, label in (("proof", "画面要证明什么"), ("tags", "素材标签"), ("evidence", "证据")):
                    line[key] = text(str((value.get(key) if isinstance(value, dict) else "") or line[key] or "")[:2000], label, False, 2000)
                if not line.get("zh") and isinstance(value, dict) and value.get("zh"):
                    line["zh"] = str(value["zh"]).strip()[:2000]
            job["fill_markdown"] = text(str(body.get("markdown") or "")[:20000], "补全脚本", False, 20000)
            if all(line.get("proof") for line in job["lines"]):
                job["problems"] = [item for item in job.get("problems", []) if MISSING_TABLE not in item]
            changed = body.get("changed") or []
            if changed:
                note = f"AI 补全时改写了第 {'、'.join(str(n) for n in changed)} 句口播；制作单仍按原口播，画面要求按行对应，请核对。"
                job["problems"] = [item for item in job.get("problems", []) if "AI 补全时改写了" not in item] + [note]
            fill.update(status="completed", error="", finished_at=now())
        job["ai_fill"] = fill
        job["updated_at"] = now()
        job["history"].append({"at": job["updated_at"], "event": "fill", "note": "AI 补全完成" if fill["status"] == "completed" else "AI 补全未完成"})
        return {"job": copy.deepcopy(job)}

    def _job_archive(self, data, body, extra):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(body.get("archived"), bool):
            raise VoiceError(400, "归档参数无效。")
        job.update(archived=body["archived"], updated_at=now())
        job["history"].append({"at": job["updated_at"], "event": "archived" if job["archived"] else "restored", "note": ""})
        return {"job": copy.deepcopy(job)}

    def _take(self, data, body, take):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(take, dict) or not TAKE_ID.fullmatch(str(take.get("id"))):
            raise VoiceError(500, "配音结果未完成核对，未保存。")
        voice = self._touch_voice(data, take["voice_id"], take.get("lookup"))
        voice.update(use_count=voice["use_count"] + 1, last_used_at=take["created_at"], updated_at=take["created_at"])
        record = {key: value for key, value in take.items() if key != "lookup"}
        record["voice_name"] = voice["name"]
        job["takes"].append(record)
        job["updated_at"] = take["created_at"]
        job["history"].append({"at": take["created_at"], "event": "take", "note": f"生成配音 {record['id']}"})
        return {"job": copy.deepcopy(job), "take": copy.deepcopy(record)}

    def _select_take(self, data, body, extra):
        job = self._find_job(data, body.get("job_id"))
        take_id = body.get("take_id")
        take = next((item for item in job["takes"] if item["id"] == take_id), None)
        if not take:
            raise VoiceError(404, "这版配音不存在。")
        if not take_quality(take, len(job["lines"])):
            raise VoiceError(409, "这版配音的意群时间未通过核验，不能选用；请重新生成或核实对齐结果。")
        job.update(selected_take=take_id, updated_at=now())
        # A receipt certifies only the voiceover it was made on.
        stale = {uid for uid, receipt in manual_receipts(job).items() if receipt.get("take_id") != take_id}
        if stale:
            self._clear_manual_completion(job, "选用配音已变化", only=stale)
        job["status"] = job_status(job)
        job["history"].append({"at": job["updated_at"], "event": "select_take", "note": f"选用配音 {take_id}"})
        return {"job": copy.deepcopy(job)}

    def _settings(self, data, body, extra):
        value = body.get("settings")
        if not isinstance(value, dict):
            raise VoiceError(400, "设置参数格式无效。")
        if "creators" in value:
            creators = value["creators"]
            if not isinstance(creators, list) or len(creators) > 30 or any(not isinstance(item, str) or not CREATOR.fullmatch(item) for item in creators):
                raise VoiceError(400, "达人名须以英文字母开头，只含字母、数字或下划线，最多 24 个字符。")
            if len({item.lower() for item in creators}) != len(creators):
                raise VoiceError(400, "达人名有重复。")
            data["settings"]["creators"] = list(creators)
        if "product" in value:
            key = product_key(text(value.get("product"), "产品", limit=200))
            fps = value.get("fps", "29.97")
            if fps not in FRAME_RATES:
                raise VoiceError(400, "帧率无效。")
            size = [value.get(name) for name in ("width", "height")]
            if any(type(item) is not int or not 16 <= item <= 8192 for item in size):
                raise VoiceError(400, "分辨率须是 16–8192 之间的整数。")
            code = text(value.get("code"), "时间线编号", limit=20)
            if not re.fullmatch(r"[A-Za-z0-9]+", code):
                raise VoiceError(400, "时间线编号只能是字母或数字。")
            previous = data["settings"]["products"].get(key, {})
            kept = previous.get("material_timelines", [])
            data["settings"]["products"][key] = {"material_timelines": kept,
                "project": text(value.get("project"), "达芬奇项目名", limit=200), "code": code, "fps": fps,
                "width": size[0], "height": size[1],
                "audio_bin": text(value.get("audio_bin", "音频字幕"), "配音素材夹", limit=100),
                "timeline_bin": text(value.get("timeline_bin", "时间线"), "时间线素材夹", limit=100)}
        return {"settings": copy.deepcopy(data["settings"])}

    def _timeline(self, data, body, record):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(record, dict) or not record.get("unique_id"):
            raise VoiceError(500, "时间线结果未完成核对，未保存。")
        job.setdefault("timelines", []).append(copy.deepcopy(record))
        job.update(updated_at=record["built_at"])
        job["status"] = job_status(job)
        job["history"].append({"at": record["built_at"], "event": "timeline", "note": f"在达芬奇建时间线 {record['name']}"})
        return {"job": copy.deepcopy(job), "timeline": copy.deepcopy(record)}

    def _timeline_location(self, data, body, location):
        """Pin a timeline built before its project was recorded to the project DaVinci actually opened it in.

        A recorded location is never replaced: the worker already proved it when it opened the timeline."""
        job = self._find_job(data, body.get("job_id"))
        uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        if not isinstance(location, dict):
            raise VoiceError(500, "时间线位置未完成核对，未保存。")
        for key in ("project_id", "folder"):
            value = location.get(key)
            if isinstance(value, str) and value and not timeline.get(key):
                timeline[key] = text(value, "达芬奇项目位置", limit=500)
        return {"timeline": copy.deepcopy(timeline)}

    def _timeline_subtitles(self, data, body, check):
        """Keep the latest subtitle check on its timeline; the fixes it made also go in the order's history."""
        job = self._find_job(data, body.get("job_id"))
        uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        timeline["subtitle_check"] = copy.deepcopy(check)
        job["updated_at"] = check["at"]
        if check["changes"] and check["status"] in {"fixed", "needs_review", "failed"}:
            job["history"].append({"at": check["at"], "event": "timeline_subtitles",
                                   "note": f"核对时间线 {timeline.get('name', '')} 的字幕，自动修改 {len(check['changes'])} 处"})
        return self._timeline_management_result(job, timeline)

    def _timeline_hook(self, data, body, hook):
        """Remember what the timeline's on-screen hook last said; the order's history keeps each change."""
        job = self._find_job(data, body.get("job_id"))
        uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        timeline["hook_overlay"] = copy.deepcopy(hook)
        job["updated_at"] = hook["at"]
        job["history"].append({"at": hook["at"], "event": "timeline_hook", "note": f"给时间线 {timeline.get('name', '')} {'加' if hook['created'] else '更新'}屏显：{hook['text']}"})
        return self._timeline_management_result(job, timeline)

    def _timeline_campaign(self, data, body, card):
        """Remember the campaign end card a timeline carries (or that it was taken off); the order's history keeps each change."""
        job = self._find_job(data, body.get("job_id"))
        uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        timeline["campaign_overlay"] = copy.deepcopy(card)
        job["updated_at"] = card["at"]
        note = f"给时间线 {timeline.get('name', '')} 加活动屏显：{card['text']}" if card["status"] != "removed" else f"去掉时间线 {timeline.get('name', '')} 的活动屏显"
        job["history"].append({"at": card["at"], "event": "timeline_campaign", "note": note})
        return self._timeline_management_result(job, timeline)

    @staticmethod
    def _managed_timeline(data, body):
        job = ProductionStore._find_job(data, body.get("job_id"))
        uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == uid), None)
        if not timeline:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再管理时间线。")
        if any(edit.get("status") == "running" and (edit.get("timeline_uid") == uid or
               (edit.get("waiting_timeline") and timeline.get("operation_key") and
                edit.get("timeline_operation_key") == timeline["operation_key"]))
               for item in data["jobs"] for edit in item.get("edits", [])):
            raise VoiceError(409, "这条时间线的剪辑仍在运行，请结束后再归档或修改任务状态。")
        return job, timeline

    @staticmethod
    def _timeline_management_result(job, timeline):
        result = public_job(job)
        return {"job": result, "timeline": next(row for row in result["timelines"]
                                               if row["unique_id"] == timeline["unique_id"])}

    def _timeline_archive(self, data, body, extra):
        archived = body.get("archived")
        if type(archived) is not bool:
            raise VoiceError(400, "请说明是归档还是恢复时间线。")
        job, timeline = self._managed_timeline(data, body)
        if bool(timeline.get("archived")) != archived:
            # Freeze inferred legacy progress so later current-version changes do
            # not erase the history of a completed, archived timeline.
            timeline["task_status"] = timeline_task_status(job, timeline)
            timeline["archived"] = archived
            job["updated_at"] = now()
            job["status"] = job_status(job)
            job["history"].append({"at": job["updated_at"], "event": "timeline_archived" if archived else "timeline_restored",
                                   "note": f"{'归档' if archived else '恢复'}时间线 {timeline.get('name', '')}"})
        return self._timeline_management_result(job, timeline)

    def _timeline_delete(self, data, body, extra):
        """Drop one timeline with its AI edit runs, its completion receipt and every saved request that only describes
        them; files are removed by the caller, and the DaVinci timeline is never touched."""
        job, timeline = self._managed_timeline(data, body)
        uid = timeline["unique_id"]
        edit_ids = [edit["id"] for edit in timeline_edits(job, timeline)]
        receipts = manual_receipts(job)
        receipts.pop(uid, None)
        job["timelines"] = [row for row in job["timelines"] if row is not timeline]
        job["edits"] = [edit for edit in job.get("edits", []) if edit["id"] not in edit_ids]
        self._set_receipts(job, receipts)
        job["updated_at"] = now()
        job["status"] = job_status(job)
        job["history"].append({"at": job["updated_at"], "event": "timeline_deleted",
                               "note": f"删除时间线 {timeline.get('name', '')}"
                                       + (f"及 {len(edit_ids)} 次剪辑记录" if edit_ids else "") + "，连同本地文件"})
        for key in [key for key, saved in data["requests"].items() if isinstance(saved.get("result"), dict) and
                    ((saved["result"].get("timeline") or {}).get("unique_id") == uid or
                     (saved["result"].get("edit") or {}).get("id") in edit_ids)]:
            del data["requests"][key]
        return {"job": public_job(job), "deleted_timeline": uid, "timeline_name": timeline.get("name", ""),
                "edit_ids": edit_ids, "operation_key": timeline.get("operation_key") or ""}

    def _timeline_task_status(self, data, body, extra):
        status = body.get("task_status")
        if not isinstance(status, str) or status not in TIMELINE_TASK_STATUSES:
            raise VoiceError(400, "时间线任务状态无效。")
        job, timeline = self._managed_timeline(data, body)
        if timeline.get("archived"):
            raise VoiceError(409, "时间线已归档，请恢复后再修改任务状态。")
        if timeline.get("task_status") != status:
            timeline["task_status"] = status
            job["updated_at"] = now()
            labels = {"pending": "待剪辑", "in_progress": "剪辑中", "completed": "已完成"}
            job["history"].append({"at": job["updated_at"], "event": "timeline_task_status",
                                   "note": f"将时间线 {timeline.get('name', '')} 的任务状态改为{labels[status]}"})
        return self._timeline_management_result(job, timeline)

    @staticmethod
    def _set_receipts(job, receipts):
        """Save the receipts: the current timeline's (else the latest) stays in manual_completion, as every earlier reader
        of the order expects; the other timelines' go in manual_completions."""
        if not receipts:
            job.pop("manual_completion", None)
            job.pop("manual_completions", None)
            return
        current = current_timeline(job)
        standing = current["unique_id"] if current and current.get("unique_id") in receipts else max(receipts, key=lambda uid: receipts[uid].get("at", ""))
        job["manual_completion"] = receipts[standing]
        others = {uid: receipt for uid, receipt in receipts.items() if uid != standing}
        if others:
            job["manual_completions"] = others
        else:
            job.pop("manual_completions", None)

    @staticmethod
    def _clear_manual_completion(job, reason, only):
        """Withdraw the receipts of the timelines in `only` (their uids); every other timeline keeps its own."""
        receipts = manual_receipts(job)
        for uid in [uid for uid in receipts if uid in only]:
            receipt = receipts.pop(uid)
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completion_cleared",
                                   "note": f"{reason}，已撤销 {receipt.get('timeline_name', '')} 的手动成功标记"})
        ProductionStore._set_receipts(job, receipts)

    def _job_completion(self, data, body, extra):
        job = self._find_job(data, body.get("job_id"))
        completed = body.get("completed")
        if type(completed) is not bool:
            raise VoiceError(400, "请说明是手动标记成功还是撤销标记。")
        note = text(body.get("note", ""), "完成备注", False, 2000)
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再修改成功标记。")
        timeline_uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == timeline_uid), None)
        if timeline and timeline_running(job, timeline):
            raise VoiceError(409, "这条时间线的剪辑仍在运行，请结束后再修改成功标记。")
        receipts = manual_receipts(job)
        if completed:
            if not timeline or timeline.get("saved") is False:
                raise VoiceError(409, "请先为当前选用配音建立并保存时间线，再手动标记成功。")
            if timeline.get("archived") or timeline.get("take_id") != job.get("selected_take"):
                raise VoiceError(409, "这条时间线已变化或不属于当前选用的配音，请刷新后再标记成功。")
            timeline["task_status"] = "completed"
            receipt = timeline_manual_receipt(job, timeline)
            if receipt:
                return {"job": copy.deepcopy(job), "completed": True, "timeline_uid": timeline_uid,
                        "manual_completion": copy.deepcopy(receipt)}
            job["updated_at"] = now()
            receipts[timeline_uid] = {"at": job["updated_at"], "note": note, "timeline_uid": timeline_uid,
                                      "timeline_name": timeline.get("name", ""), "take_id": job["selected_take"],
                                      "basis": "human_review"}
            self._set_receipts(job, receipts)
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completed",
                                   "note": f"人工手动标记成功（{timeline.get('name', '')}）" + (f"：{note}" if note else "")})
        else:
            completion_at = text(body.get("completion_at"), "成功标记时间", limit=100)
            receipt = receipts.get(timeline_uid)
            if not receipt:
                raise VoiceError(409, "这条时间线还没有手动成功标记。" if receipts else "这张制作单还没有手动成功标记。")
            if completion_at != receipt.get("at"):
                raise VoiceError(409, "成功标记已变化，请刷新后再撤销。")
            receipts.pop(timeline_uid)
            self._set_receipts(job, receipts)
            if timeline:
                timeline.pop("task_status", None)
                timeline["task_status"] = timeline_task_status(job, timeline)
            job["updated_at"] = now()
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completion_revoked",
                                   "note": f"撤销 {receipt.get('timeline_name', '')} 的手动成功标记" + (f"：{note}" if note else "")})
        job["status"] = job_status(job)
        return {"job": copy.deepcopy(job), "timeline_uid": timeline_uid,
                "completed": bool(timeline and timeline_manual_receipt(job, timeline)),
                "manual_completion": copy.deepcopy(manual_receipts(job).get(timeline_uid))}

    def _edit_start(self, data, body, record):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(record, dict) or not EDIT_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "剪辑任务未完成核对，未启动。")
        target_uid = next((timeline["unique_id"] for timeline in job.get("timelines", []) if
                          record.get("waiting_timeline") and timeline.get("operation_key") == record.get("timeline_operation_key")),
                          record.get("timeline_uid"))
        target = next((timeline for timeline in job.get("timelines", []) if timeline.get("unique_id") == target_uid), None)
        if target and target.get("archived"):
            raise VoiceError(409, "时间线已归档，请恢复后再启动 AI 剪辑。")
        if any(edit.get("status") == "running" and (edit.get("timeline_uid") in {record.get("timeline_uid"), target_uid} or
                   ((record.get("waiting_timeline") or edit.get("waiting_timeline")) and record.get("timeline_operation_key") and
                    edit.get("timeline_operation_key") == record["timeline_operation_key"]))
               for item in data["jobs"] for edit in item.get("edits", [])):
            raise VoiceError(409, "这条目标时间线已有剪辑任务，请等它结束；其他时间线可以同时提交。")
        job["updated_at"] = record["started_at"]
        # Only the timeline being edited loses its completion; the others were not touched.
        if target:
            self._clear_manual_completion(job, "已发起新的 AI 剪辑", only={target["unique_id"]})
        job.setdefault("edits", []).append(copy.deepcopy(record))
        if target:
            target["task_status"] = "in_progress"
        job["status"] = job_status(job)
        job["updated_at"] = record["started_at"]
        job["history"].append({"at": record["started_at"], "event": "edit", "note": f"开始 AI 剪辑 {record['id']}（{record['timeline_name']}）"})
        product = data["settings"]["products"].setdefault(job["product"], {})
        product["material_timelines"] = list(record["materials"])
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(record)}

    def _edit_bind_timeline(self, data, body, extra):
        """Replace a queued build identity only with the timeline committed by that exact operation."""
        job = self._find_job(data, body.get("job_id"))
        edit = next((item for item in job.get("edits", []) if item["id"] == body.get("edit_id")), None)
        if not edit or edit.get("status") != "running" or not edit.get("waiting_timeline"):
            raise VoiceError(409, "这次待建线剪辑已结束或已绑定时间线，未重复启动。")
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，未启动 AI 剪辑。")
        key = body.get("operation_key")
        timeline = next((item for item in job.get("timelines", []) if item.get("unique_id") == body.get("timeline_uid") and
                         item.get("operation_key") == key), None)
        if (not key or key != edit.get("timeline_operation_key") or not timeline or
                timeline.get("take_id") != edit.get("take_id")):
            raise VoiceError(409, "建线结果与排队剪辑绑定的原请求不一致，未启动 AI 剪辑。")
        if timeline.get("archived"):
            raise VoiceError(409, "时间线已归档，请恢复后再启动 AI 剪辑。")
        if any(other.get("id") != edit["id"] and other.get("status") == "running" and
               other.get("timeline_uid") == timeline["unique_id"]
               for item in data["jobs"] for other in item.get("edits", [])):
            raise VoiceError(409, "这条目标时间线已有剪辑任务，未重复启动。")
        edit.update(timeline_uid=timeline["unique_id"], timeline_name=timeline["name"], waiting_timeline=False,
                    timeline_bound_at=now())
        timeline["task_status"] = "in_progress"
        job["status"] = job_status(job)
        job["updated_at"] = edit["timeline_bound_at"]
        job["history"].append({"at": job["updated_at"], "event": "edit_timeline_bound",
                               "note": f"排队剪辑 {edit['id']} 已接续时间线 {timeline['name']}"})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit), "timeline": copy.deepcopy(timeline)}

    def _edit_finish(self, data, body, result):
        job = self._find_job(data, body.get("job_id"))
        edit = next((item for item in job.get("edits", []) if item["id"] == body.get("edit_id")), None)
        if not edit:
            raise VoiceError(404, "这次剪辑记录不存在。")
        edit.update(copy.deepcopy(result))
        job["status"] = job_status(job)
        job["updated_at"] = edit["finished_at"]
        labels = {"completed": "AI 剪辑执行结束", "failed": "AI 剪辑失败", "cancelled": "AI 剪辑已停止",
                  "awaiting_review": "工程核验通过，待人工审查", "needs_material": "待补素材",
                  "needs_verification": "素材待核实", "needs_repair": "待修正", "verification_failed": "核验未完成", "unverified": "历史结果待核验"}
        if edit.get("completion_policy") == "skill_delivery" and edit["status"] == "completed":
            labels["completed"] = "按技能剪辑结束，交人工审查；工程核验未执行"
        job["history"].append({"at": edit["finished_at"], "event": "edit_" + edit["status"], "note": f"{labels.get(edit['status'], edit['status'])} {edit['id']}"})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit)}

    @staticmethod
    def _finished_edit(data, body):
        job = ProductionStore._find_job(data, body.get("job_id"))
        edit = next((item for item in job.get("edits", []) if item["id"] == body.get("edit_id")), None)
        if not edit:
            raise VoiceError(404, "这次剪辑记录不存在。")
        if edit.get("status") == "running":
            raise VoiceError(409, "剪辑仍在运行，请结束后再核验。")
        return job, edit

    @staticmethod
    def _apply_readback(job, edit, result):
        """Record a fresh readback; a completion confirmation survives only while the checked shots stay the same."""
        if not isinstance(result, dict) or not isinstance(result.get("validation"), dict):
            raise VoiceError(500, "核验结果无效，未覆盖原记录。")
        edit.setdefault("execution_status", edit.get("status"))
        edit.setdefault("verification_history", []).append({"at": now(), "status": edit.get("status"),
            "validation": copy.deepcopy(edit.get("validation")), "summary": copy.deepcopy(edit.get("summary")),
            "readback_error": edit.get("readback_error", "")})
        for key in ("status", "summary", "validation", "readback_error", "checked_at"):
            if key in result:
                edit[key] = copy.deepcopy(result[key])
        edit.pop("result_pending", None)
        edit.pop("persistence_error", None)
        job["updated_at"] = now()
        approval = edit.get("approval")
        if approval and (edit["status"] != "awaiting_review" or approval.get("shots") !=
                         shot_signature(edit["validation"].get("source_ranges") or [])):
            edit["approval"] = None
            job["history"].append({"at": job["updated_at"], "event": "edit_approval_cleared",
                                   "note": f"核验结果有变化，已撤销 {edit['id']} 的完成确认"})

    def _edit_recheck(self, data, body, result):
        job, edit = self._finished_edit(data, body)
        self._apply_readback(job, edit, result)
        job["status"] = job_status(job)
        job["history"].append({"at": job["updated_at"], "event": "edit_recheck", "note": f"重新核验 {edit['id']}：{edit['status']}"})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit)}

    def _edit_audio_repair(self, data, body, result):
        job, edit = self._finished_edit(data, body)
        if not isinstance(result, dict) or not isinstance(result.get("audio_inheritance"), dict):
            raise VoiceError(500, "原声处理结果无效，未覆盖原记录。")
        edit.setdefault("audio_repair_history", []).append({"at": now(), "status": edit.get("status"),
            "error": edit.get("error", ""), "audio_inheritance": copy.deepcopy(edit.get("audio_inheritance"))})
        for key in ("status", "error", "report", "audio_inheritance", "audio_repaired_at"):
            if key in result:
                edit[key] = copy.deepcopy(result[key])
        job["status"], job["updated_at"] = job_status(job), now()
        job["history"].append({"at": job["updated_at"], "event": "edit_audio_repair",
            "note": f"原声继承已补齐 {edit['id']}" if edit["audio_inheritance"].get("status") == "completed" else f"原声继承仍有待处理项 {edit['id']}"})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit)}

    def _edit_delete(self, data, body, extra):
        """Drop one finished edit record and every saved request that only describes it; files are removed by the caller."""
        job = self._find_job(data, body.get("job_id"))
        edit = next((item for item in job.get("edits", []) if item["id"] == body.get("edit_id")), None)
        if not edit:
            raise VoiceError(404, "这次剪辑记录不存在。")
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再删除剪辑。")
        if edit.get("status") == "running":
            raise VoiceError(409, "这次剪辑还在运行，请先取消或等它结束。")
        job["edits"] = [item for item in job["edits"] if item["id"] != edit["id"]]
        job["updated_at"] = now()
        job["status"] = job_status(job)
        job["history"].append({"at": job["updated_at"], "event": "edit_deleted", "note": f"删除 AI 剪辑 {edit['id']}（{edit.get('timeline_name', '')}）及本地文件"})
        for key in [key for key, saved in data["requests"].items()
                    if isinstance(saved.get("result"), dict) and (saved["result"].get("edit") or {}).get("id") == edit["id"]]:
            del data["requests"][key]
        return {"job": copy.deepcopy(job), "deleted_edit": edit["id"]}

    def _edit_approve(self, data, body, result):
        job, edit = self._finished_edit(data, body)
        if body.get("approved") is False:
            if not edit.get("approval"):
                raise VoiceError(409, "这次剪辑还没有确认完成。")
            edit["approval"] = None
            job["updated_at"] = now()
            job["history"].append({"at": job["updated_at"], "event": "edit_approval_revoked", "note": f"撤销 {edit['id']} 的完成确认"})
        else:
            if job.get("archived"):
                raise VoiceError(409, "制作单已归档，请恢复后再确认完成。")
            if not current_edit(job, edit):
                raise VoiceError(409, "这次剪辑已被更新的剪辑或配音取代，只能确认当前版本。")
            if edit.get("completion_policy") == "skill_delivery":
                if not skill_delivery_reviewable(edit):
                    raise VoiceError(409, "这次剪辑尚未完成保存交付，请先处理已列明的问题。")
                job["updated_at"] = now()
                edit["approval"] = {"at": job["updated_at"], "note": str(body.get("note") or ""),
                                    "reviewed": list(MANUAL_SCOPE), "basis": "human_review"}
                if (edit.get("validation") or {}).get("scope") == "current_coverage":
                    edit["approval"].update(checked_at=edit["validation"].get("checked_at", ""),
                                            shots=shot_signature(edit["validation"].get("source_ranges") or []))
                note = f"人工确认完成 {edit['id']}（{edit['timeline_name']}）；仅登记人工审查"
            else:
                self._apply_readback(job, edit, result)
                if edit["status"] == "awaiting_review":
                    edit["approval"] = {"at": job["updated_at"], "note": str(body.get("note") or ""), "reviewed": list(MANUAL_SCOPE),
                                        "checked_at": edit["validation"].get("checked_at", ""),
                                        "shots": shot_signature(edit["validation"].get("source_ranges") or [])}
                    note = f"人工确认完成 {edit['id']}（{edit['timeline_name']}）"
                else:
                    note = f"确认完成前重新核验 {edit['id']}：{edit['status']}，未确认完成"
            job["history"].append({"at": job["updated_at"], "event": "edit_approve", "note": note})
        timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == edit.get("timeline_uid")), None)
        latest = next((row for row in reversed(job.get("edits", [])) if row.get("timeline_uid") == edit.get("timeline_uid")), None)
        if timeline and latest is edit:
            timeline["task_status"] = "completed" if edit.get("approval") else "in_progress"
        job["status"] = job_status(job)
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit), "approved": bool(edit.get("approval"))}

    def _edit_baseline(self, data, body, result):
        job, edit = self._finished_edit(data, body)
        if edit.get("baseline"):
            raise VoiceError(409, "这次剪辑已有核验依据，不能覆盖。")
        if (not isinstance(result, dict) or not isinstance(result.get("baseline"), dict) or
                not isinstance(result.get("baseline_at"), str) or not result["baseline_at"]):
            raise VoiceError(500, "补齐的核验依据无效，未保存。")
        origin = result.get("baseline_origin", "adopted")
        if not isinstance(origin, str) or origin not in {"adopted", "historical_resume"}:
            raise VoiceError(500, "核验依据的来源无效，未保存。")
        if body.get("source") == "historical_resume" and origin != "historical_resume":
            raise VoiceError(500, "历史恢复未返回真实历史依据，未保存；不能采纳当前工程替代。")
        evidence = result.get("baseline_evidence")
        if origin == "historical_resume":
            from datetime import datetime
            sources = evidence.get("sources") if isinstance(evidence, dict) else None
            required_roles = {"resume_snapshot", "native_timeline_backup", "handoff", "pre_edit_catalog"}
            resumption = edit.get("resumption") if isinstance(edit.get("resumption"), dict) else {}
            tracks = result["baseline"].get("tracks")
            audio = tracks.get("audio") if isinstance(tracks, dict) else None
            voice_tracks = [track for track in audio if isinstance(track, dict) and track.get("index") == 2] if isinstance(audio, list) else []
            voice_items = voice_tracks[0].get("items") if len(voice_tracks) == 1 else None
            voice = voice_items[0] if isinstance(voice_items, list) and len(voice_items) == 1 else None
            voice_enabled = voice.get("enabled") if isinstance(voice, dict) else None
            voice_valid = isinstance(voice, dict) and "enabled" in voice and (voice_enabled is None or isinstance(voice_enabled, bool))
            valid = (isinstance(evidence, dict) and type(evidence.get("schema_version")) is int and
                     evidence["schema_version"] == 1 and evidence.get("kind") == "historical_resume" and
                     evidence.get("scope") == "Codex接续剪辑前" and
                     evidence.get("captured_at") == result["baseline_at"] == resumption.get("started_at") and
                     voice_valid and evidence.get("unrecorded_fields") == (["voice.clip_enabled"] if voice_enabled is None else []) and
                     evidence.get("configured_fields") == ["width", "height"] and
                     isinstance(sources, list) and len(sources) >= len(required_roles))
            roles, paths = [], set()
            if valid:
                for source in sources:
                    if (not isinstance(source, dict) or not isinstance(source.get("role"), str) or source["role"] not in required_roles or
                            not isinstance(source.get("path"), str) or "\x00" in source["path"] or
                            not Path(source["path"]).is_absolute() or source["path"].casefold() in paths or
                            not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])):
                        valid = False
                        break
                    roles.append(source["role"])
                    paths.add(source["path"].casefold())
                valid = valid and set(roles) == required_roles and all(
                    roles.count(role) == 1 for role in required_roles - {"pre_edit_catalog"})
            if valid:
                try:
                    captured = datetime.fromisoformat(evidence["captured_at"].replace("Z", "+00:00"))
                    restored = datetime.fromisoformat(evidence["restored_at"].replace("Z", "+00:00"))
                    valid = captured.tzinfo is not None and restored.tzinfo is not None and restored >= captured
                except (KeyError, AttributeError, TypeError, ValueError):
                    valid = False
            if not valid:
                raise VoiceError(500, "历史核验依据的来源或采集时间不完整，未保存；不能以当前工程替代过去的记录。")
        edit.update(baseline=copy.deepcopy(result["baseline"]), baseline_at=result["baseline_at"], baseline_origin=origin)
        if origin == "historical_resume":
            edit["baseline_evidence"] = copy.deepcopy(evidence)
        self._apply_readback(job, edit, result)
        job["status"] = job_status(job)
        note = (f"恢复 {edit['id']} 的历史核验依据（Codex接续剪辑前，原采集时间 {edit['baseline_at']}），重新核验：{edit['status']}"
                if origin == "historical_resume" else
                f"为 {edit['id']} 采纳当前保护基准，补齐核验依据，重新核验：{edit['status']}")
        job["history"].append({"at": job["updated_at"], "event": "edit_baseline",
                               "note": note})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit)}

    def interrupt_running(self):
        """A restarted workbench cannot reattach to an editing process; mark such runs as interrupted."""
        if not self.path.exists():
            return
        with self.lock, self.catalog_transaction():
            if not self.path.exists():
                return
            data = self._mutation_snapshot()
            changed = False
            for job in data["jobs"]:
                for edit in job.get("edits", []):
                    if edit.get("status") == "running":
                        edit.update(status="interrupted", execution_status="interrupted", finished_at=now(),
                                    error=("工作台重启时仍在等待原建线，排队已中断，AI 剪辑没有开始；请先核对原建线结果。" if edit.get("waiting_timeline") else
                                           "工作台重启时这次剪辑还在进行，已中断；达芬奇里可能留有做到一半的结果，请先核对。"))
                        changed = True
            if changed:
                self.write(data)

    def _job_delete(self, data, body, extra):
        """Remove a job and every saved request that only describes it; files are removed by the caller."""
        job = self._find_job(data, body.get("job_id"))
        if any(edit.get("status") == "running" for edit in job.get("edits", [])):
            raise VoiceError(409, "这张制作单还有 AI 剪辑在运行。")
        data["jobs"] = [entry for entry in data["jobs"] if entry["id"] != job["id"]]
        for key in [key for key, saved in data["requests"].items() if isinstance(saved.get("result"), dict) and
                    ((saved["result"].get("job") or {}).get("id") == job["id"] or
                     (saved["result"].get("edit") or {}).get("job_id") == job["id"])]:
            del data["requests"][key]
        return {"deleted_job": job["id"], "title": job.get("title", ""), "edit_ids": [edit["id"] for edit in job.get("edits", [])]}
