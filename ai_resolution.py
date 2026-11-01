"""Which AI task rows are already settled by something that happened afterwards.

Everything here is derived on read from list_tasks() rows and production jobs: no task, job or
management state is changed. Content rows and the task panel both ask this one function, so the
rules live in one place:

A  refilled      a failed fill whose 制作单 was filled again afterwards
B  rerun         a failed task rerun later with the same complete instruction on the same subject
C  written_back  a completed fill whose result is on the 制作单 (not_written when the write failed)
D  abandoned     the user gave the task up (task panel only; content rows already treat it as history)

Jobs are matched by number (ai_fill.task_id, context.source_production_id), never by title.
"""

from __future__ import annotations

from datetime import datetime

FAILED = {"failed", "interrupted"}


def local_stamp(value):
    """'10/19 14:03' in local time; stored times are UTC ISO strings."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return f"{moment.month}/{moment.day} {moment:%H:%M}"


def _after(later, earlier):
    try:
        return bool(later and earlier and datetime.fromisoformat(str(later).replace("Z", "+00:00")) >
                    datetime.fromisoformat(str(earlier).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return False


def _source_job(task):
    return ((task.get("context") or {}).get("source_production_id") or "") if isinstance(task.get("context"), dict) else ""


def ai_task_resolutions(tasks, jobs):
    """Return {task_id: {kind, by_task, by_job, at, note}} for the tasks something later settled."""
    rows = [task for task in tasks or [] if isinstance(task, dict) and task.get("task_id")]
    by_id = {task["task_id"]: task for task in rows}
    fills = [job for job in jobs or [] if isinstance(job, dict) and isinstance(job.get("ai_fill"), dict)]
    result = {}
    for task in rows:
        task_id, status = task["task_id"], task.get("status")
        source = _source_job(task)
        fill_task = bool(source or task.get("fill_request"))
        if status in FAILED and fill_task:
            for job in fills:
                fill = job["ai_fill"]
                if fill.get("status") != "completed" or fill.get("task_id") in {None, task_id}:
                    continue
                if not _after(fill.get("started_at"), task.get("latest_round_finished_at")):
                    continue
                refill = by_id.get(fill["task_id"]) or {}
                owned = (source == job.get("id")) if source else bool(
                    refill and task.get("instruction_sha256") and refill.get("instruction_sha256") == task["instruction_sha256"])
                if owned:
                    at = fill.get("finished_at") or refill.get("latest_round_finished_at") or ""
                    result[task_id] = {"kind": "refilled", "by_task": fill["task_id"], "by_job": job["id"], "at": at,
                                       "note": f"同一制作单 {local_stamp(at)} 已重新补全".replace("  ", " ")}
                    break
        elif status in FAILED and task.get("instruction_sha256"):
            for other in rows:
                if (other is task or other.get("status") != "completed" or other.get("fill_request") or _source_job(other)
                        or other.get("product") != task.get("product") or other.get("action") != task.get("action")
                        or other.get("subject_key") != task.get("subject_key")
                        or other.get("instruction_sha256") != task["instruction_sha256"]
                        or not _after(other.get("created_at"), task.get("latest_round_finished_at"))):
                    continue
                at = other.get("latest_round_finished_at") or ""
                result[task_id] = {"kind": "rerun", "by_task": other["task_id"], "by_job": "", "at": at,
                                   "note": f"{local_stamp(at)} 重跑已完成".strip()}
                break
        elif status == "completed" and fill_task:
            for job in fills:
                fill = job["ai_fill"]
                if fill.get("task_id") == task_id:
                    if fill.get("status") == "completed":
                        result[task_id] = {"kind": "written_back", "by_task": "", "by_job": job["id"],
                                           "at": fill.get("finished_at") or "", "note": "已写回制作单"}
                    elif fill.get("status") == "failed":
                        result[task_id] = {"kind": "not_written", "by_task": "", "by_job": job["id"],
                                           "at": fill.get("finished_at") or "", "note": "未写回：" + (fill.get("error") or "原因未记录")}
                    break
                # A fill a newer one replaced on the same job counts only when it named that job itself.
                if source and source == job.get("id") and fill.get("status") == "completed" and fill.get("task_id") != task_id:
                    result[task_id] = {"kind": "written_back", "by_task": fill["task_id"], "by_job": job["id"],
                                       "at": "", "note": "已写回制作单"}
                    break
        if task_id not in result and task.get("management_state") == "abandoned":
            result[task_id] = {"kind": "abandoned", "by_task": "", "by_job": "", "at": "", "note": "已放弃"}
    return result
