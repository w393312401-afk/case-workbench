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
from carousel_plan import empty_plan, normalize_plan, slide_fields
from carousel_lineage import lineage_context, slide_lineage
from carousel_layout import CAPTION_PRESETS

JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")
TAKE_ID = re.compile(r"TAK-[0-9a-f]{12}")
JOB_STATUSES = {"draft", "voiced", "timeline", "editing", "awaiting_review", "approved", "needs_material", "needs_repair",
                "needs_verification", "verification_failed", "unverified", "failed", "cancelled", "interrupted"}
TIMELINE_TASK_STATUSES = {"pending", "in_progress", "completed"}
# Finished voiceovers and timelines are appended whatever else changed while they were being made.
REVISION_FREE = {"take", "job_fill_start", "job_fill_finish", "timeline", "job_completion", "edit_start", "edit_bind_timeline", "edit_finish", "edit_recheck", "edit_approve", "edit_baseline", "edit_delete", "job_delete", "carousel_delete", "carousel_search_delete", "carousel_tidy", "carousel_search_start", "carousel_search_finish", "carousel_import",
                 "carousel_generation_start", "carousel_generation_finish", "carousel_generation_stage", "carousel_output", "carousel_publication", "carousel_calendar",
                 "carousel_import_finished", "carousel_review_start", "carousel_review_finish", "carousel_collection",
                 "carousel_autopilot_update", "carousel_group_review_start", "carousel_group_review_finish"}
EDIT_ID = re.compile(r"EDT-[0-9a-f]{12}")
CAROUSEL_ID = re.compile(r"CAR-[0-9a-f]{12}")
SEARCH_ID = re.compile(r"STS-[0-9a-f]{12}")
CANDIDATE_ID = re.compile(r"CND-[0-9a-f]{12}")
GENERATION_ID = re.compile(r"IMG-[0-9a-f]{12}")
AUTOPILOT_ID = re.compile(r"AUT-[0-9a-f]{12}")
OUTPUT_ID = re.compile(r"OUT-[0-9a-f]{12}")
REVIEW_ID = re.compile(r"REV-[0-9a-f]{12}")
GROUP_REVIEW_ID = re.compile(r"GRV-[0-9a-f]{12}")
MAX_GROUP_REVIEWS = 20
FINISHED_IMPORT_ID = re.compile(r"IMP-[0-9a-f]{12}")
DELIVERY_ID = re.compile(r"DLV-[0-9a-f]{12}")
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


def timeline_task_status(job, timeline):
    """Progress is independent of AI execution and verification; infer only legacy records."""
    explicit = timeline.get("task_status")
    if isinstance(explicit, str) and explicit in TIMELINE_TASK_STATUSES:
        return explicit
    uid = timeline.get("unique_id")
    receipt = job.get("manual_completion") or {}
    if (receipt.get("at") and receipt.get("timeline_uid") == uid and
            receipt.get("take_id") == timeline.get("take_id")):
        return "completed"
    latest = next((edit for edit in reversed(job.get("edits", [])) if edit.get("timeline_uid") == uid), None)
    if (latest and latest.get("approval") and not latest.get("result_pending") and
            (skill_delivery_reviewable(latest) if latest.get("completion_policy") == "skill_delivery" else
             latest.get("status") == "awaiting_review")):
        return "completed"
    return "in_progress" if latest else "pending"


def manual_completion_current(job):
    """A manual receipt certifies only its voiceover and latest built timeline."""
    receipt = job.get("manual_completion")
    timeline = current_timeline(job)
    return bool(isinstance(receipt, dict) and receipt.get("at") and timeline and timeline.get("saved") is not False and
                receipt.get("timeline_uid") == timeline.get("unique_id") and
                receipt.get("take_id") == job.get("selected_take") and
                not any(edit.get("status") == "running" for edit in job.get("edits", [])))


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


# Settings that describe how a product is shot and edited; saving them on one carousel makes them the default for the next.
SUBJECT_RENDER_CONTRACT = "segmented_background_v4"
NEW_CAROUSEL_REQUIREMENTS = {
    "mode": "layout_only", "workflow": "review_first", "editable_regions": ["text"],
    "protected_product": True, "protected_evidence": True, "hands_authorized": False,
    "caption_style": "yellow_impact", "overlay_only": True, "postprocess": "layout_only",
    "caption_layer": "behind_subject", "dense_layout_policy": "edge_whitespace",
    "source": "选定底图（拼版、裁切和文字带在选底图时已定）；保留原背景、原手和产品像素，仅加锁定英文花字。",
    "style": "花字放底图文字带；没有文字带时放画面边缘细节最少处；不抠图、不加黑边，不改写英文。",
}
LAYOUT_COLLABORATION_MESSAGE = "已保留制作单和原文案，待分层排版协作：保留完整原图、背景和手，让主体在英文花字前面；密集画面使用排版边缘留白。可导入完成的分层成品，再独立核对。"


def carousel_requires_layered_layout(requirements):
    return requirements.get("mode") == "layout_only" and requirements.get("caption_layer") == "behind_subject"


PRODUCT_REQUIREMENT_KEYS = ("mode", "editable_regions", "protected_product", "protected_evidence", "hands_authorized",
                            "aspect", "style", "source", "postprocess", "hand_look", "caption_style", "overlay_only", "workflow",
                            "render_contract", "caption_layer", "dense_layout_policy")


def carousel_requirements(item, slide=None, aspect=None):
    """One structured contract for making, reviewing and displaying a slide."""
    source = copy.deepcopy(item.get("requirements") or {})
    if not isinstance(source, dict):
        raise VoiceError(400, "制作要求须是完整的资料。")
    mode = source.get("mode", "photo_preserving")
    if mode not in {"generated_visual", "photo_preserving", "layout_only"}:
        raise VoiceError(400, "请选择生成视觉、实拍保真或只排文字。")
    value = {"mode": mode, "editable_regions": source.get("editable_regions", ["background", "text"]),
             "protected_product": source.get("protected_product", True),
             "protected_evidence": source.get("protected_evidence", True),
             "hands_authorized": source.get("hands_authorized", source.get("hand_edit_allowed", False)),
             "aspect": aspect or source.get("aspect") or item.get("image_aspect", "3:4"),
             "style": source.get("style", ""), "source": source.get("source", "codex_builtin"),
             "instruction": source.get("instruction", ""), "postprocess": source.get("postprocess", "layout_only")}
    # Optional so historic image/review signatures stay valid until the user
    # explicitly changes the workflow for this carousel.
    if "workflow" in source:
        if not isinstance(source["workflow"], str) or source["workflow"] not in {"subject_locked", "generation_first", "review_first"}:
            raise VoiceError(400, "请选择保护商品图生图、先生成图片或逐张核对的制作流程。")
        value["workflow"] = source["workflow"]
        if value["workflow"] == "generation_first":
            value["mode"] = "generated_visual"
        elif value["workflow"] == "subject_locked":
            value.update(mode="photo_preserving", protected_product=True, protected_evidence=True,
                         editable_regions=["background", "text", "hands"], hands_authorized=True,
                         render_contract=SUBJECT_RENDER_CONTRACT)
    if value["aspect"] not in {"3:4", "9:16"} or type(value["hands_authorized"]) is not bool:
        raise VoiceError(400, "图片比例或手部授权格式无效。")
    if not isinstance(value["editable_regions"], list) or len(value["editable_regions"]) > 30 or any(
            not isinstance(part, str) or len(part) > 200 for part in value["editable_regions"]):
        raise VoiceError(400, "可修改区域须逐项填写。")
    for key in ("protected_product", "protected_evidence"):
        if type(value[key]) is not bool and (not isinstance(value[key], list) or len(value[key]) > 30 or any(
                not isinstance(part, str) or len(part) > 500 for part in value[key])):
            raise VoiceError(400, "受保护的商品和证据格式无效。")
    for key in ("style", "source", "instruction", "postprocess"):
        value[key] = text(value[key], "制作要求", False, 4000)
    for key in ("authorized_hand_slides", "no_hand_slides", "style_reference", "caption_area"):
        if key in source:
            value[key] = copy.deepcopy(source[key])
    for key in ("authorized_hand_slides", "no_hand_slides"):
        if key in value and (not isinstance(value[key], list) or any(type(n) is not int or n < 1 for n in value[key])):
            raise VoiceError(400, "逐张手部授权格式无效。")
    if "caption_area" in value and value["caption_area"] not in {"top", "bottom", ""}:
        raise VoiceError(400, "请选择已授权的文字留白区域。")
    for key, choices in (("caption_layer", {"front", "behind_subject"}), ("dense_layout_policy", {"edge_whitespace"})):
        if key in source:
            if not isinstance(source[key], str) or source[key] not in choices:
                raise VoiceError(400, "文字层级或密集画面的排版方式无效。")
            value[key] = source[key]
    # Slides that must keep their real photo pixels (real product redrawn by an image model failed the check). Only the group-level
    # record carries the list; each slide's own contract below changes just for the slides named, so other slides keep their signatures.
    preserve = source.get("preserve_slides", [])
    if not isinstance(preserve, list) or any(type(n) is not int or n < 1 for n in preserve):
        raise VoiceError(400, "逐张实拍保真格式无效。")
    if carousel_requires_layered_layout(value) or value.get("workflow") in {"generation_first", "subject_locked"}:
        preserve = []
    if slide is None and preserve:
        value["preserve_slides"] = sorted(set(preserve))
    # Optional by design: adding a default would change signatures of already
    # imported, reviewed image groups that never requested a caption preset.
    if "caption_style" in source:
        if not isinstance(source["caption_style"], str) or source["caption_style"] not in CAPTION_PRESETS:
            raise VoiceError(400, "请选择有效的花字或普通文字样式。")
        value["caption_style"] = source["caption_style"]
    # Optional too: the hand look used when hands may change; absent means the built-in default look.
    if "hand_look" in source:
        look = text(source["hand_look"], "手部外观", False, 500)
        if look:
            value["hand_look"] = look
    # Also optional: only a group that asked to lay out the overlay alone carries the key.
    if "overlay_only" in source:
        if type(source["overlay_only"]) is not bool:
            raise VoiceError(400, "「只排叠字」须是开或关。")
        if source["overlay_only"]:
            value["overlay_only"] = True
    if slide is not None:
        if slide["number"] in preserve:
            value.update(mode="photo_preserving", editable_regions=["background", "text"], hands_authorized=False,
                         protected_product=True, protected_evidence=True)
        if "authorized_hand_slides" in source and slide["number"] not in preserve and value.get("workflow") != "subject_locked":
            value["hands_authorized"] = slide["number"] in source["authorized_hand_slides"]
        if slide["number"] in source.get("no_hand_slides", []) and value.get("workflow") != "subject_locked":
            value["hands_authorized"] = False
        value["locked_copy"] = {"number": slide["number"], "title_lines": copy.deepcopy(slide.get("title_lines", [])),
                                "body": "" if value.get("overlay_only") else slide.get("body", "")}
        value["product_reference"] = {"base_id": (slide.get("base") or {}).get("id"),
                                      "source": slide.get("source", "")}
    return value


def carousel_input_signature(item, slide, aspect):
    """Bind a render to its adopted words, chosen base and current making notes."""
    values = {"script": [item["script_id"], item["script_version"]], "aspect": aspect,
                        "slide": {key: slide.get(key) for key in ("number", "title_lines", "body", "role", "source", "forbidden")},
                        "base_id": (slide.get("base") or {}).get("id")}
    # Preserve signatures of historic jobs that did not yet have a structured contract.
    if item.get("requirements") is not None:
        values["requirements"] = carousel_requirements(item, slide, aspect)
    lineage = slide_lineage(slide)
    if lineage:
        values["lineage"] = lineage
    context = lineage_context(item, readonly=True)
    if context:
        values["script_context"] = context
    return fingerprint(values)


def validate_carousel_lineage(item):
    """Validate submitted mappings against the immutable source snapshot, when available."""
    context = lineage_context(item)
    known = {row.get("number") for row in context.get("records", []) if isinstance(row, dict)
             and type(row.get("number")) is int and row["number"] > 0}
    previous = 0
    for slide in item.get("slides", []):
        lineage = slide_lineage(slide, strict=True)
        lines = lineage.get("source_lines") or []
        if known and any(number not in known for number in lines):
            raise VoiceError(400, f"第 {slide['number']} 张引用了原稿中不存在的意群编号。")
        if lines and lines[0] < previous:
            raise VoiceError(400, "逐张原稿意群须沿原视频推进排列，不能将后面的购买理由提前。")
        if lines:
            previous = lines[-1]
        slide.update(lineage)


def carousel_review_current(item, slide, output):
    """Review eligibility follows the exact bytes and input contract it read."""
    review = output.get("review") or {}
    if review.get("status") not in {"pass", "uncertain"}:
        return False
    if output.get("requirements_signature") and review.get("requirements_signature") != output["requirements_signature"]:
        return False
    if output.get("review_runs") or output.get("origin") == "external_generated_layout" or review.get("file_sha256"):
        return (review.get("execution_status") == "completed" and isinstance(review.get("scope"), list) and
                {"visual", "text", "product", "evidence"}.issubset(set(review["scope"])) and
                review.get("file_sha256") == output.get("sha256") and
                review.get("input_signature") == carousel_input_signature(item, slide, output["aspect"]) and
                review.get("review_id") == output.get("latest_review_id", review.get("review_id")))
    # Existing records were independently reviewed inside their generation attempt.
    return bool(output.get("generation_id"))


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
RECEIPT_RECORDS = {"carousel": "carousels", "job": "jobs"}


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


def carousel_output_current(item, slide, output):
    """A person can choose current image bytes without an AI review report."""
    if not output or output.get("input_changed") or output.get("stale"):
        return False
    aspect = output.get("aspect")
    return bool(aspect in {"3:4", "9:16"} and aspect == item.get("image_aspect", aspect) and
                (slide.get("base") or output.get("origin") == "external_generated_layout") and
                output.get("input_signature") == carousel_input_signature(item, slide, aspect) and
                (not output.get("requirements_snapshot") or
                 output.get("requirements_signature") == fingerprint(output["requirements_snapshot"])))


def carousel_subject_preservation_current(output):
    """Check receipt identity here; file and pixel verification belongs to the image worker."""
    if not isinstance(output, dict) or output.get("workflow") != "subject_locked" or output.get("ai_generated") is not True:
        return False
    receipt = output.get("subject_preservation")
    if not isinstance(receipt, dict):
        return False
    return bool(receipt.get("status") == "passed" and receipt.get("method") == "masked_source_pixels" and
                receipt.get("render_contract") == SUBJECT_RENDER_CONTRACT and
                output.get("sha256") and receipt.get("final_sha256") == output["sha256"] and
                output.get("base_sha256") and receipt.get("base_sha256") == output["base_sha256"] and
                output.get("input_signature") and receipt.get("input_signature") == output["input_signature"] and
                type(receipt.get("protected_pixel_count")) is int and receipt["protected_pixel_count"] > 0 and
                type(receipt.get("mismatch_pixels")) is int and receipt["mismatch_pixels"] == 0)


def carousel_confirmation_current(item, slide, output):
    """Keep manual confirmation bound to the selected file and current inputs."""
    if not carousel_output_current(item, slide, output) or output.get("approved") is not True:
        return False
    if ((item.get("requirements") or {}).get("workflow") == "subject_locked" or
            output.get("workflow") == "subject_locked") and not carousel_subject_preservation_current(output):
        return False
    # Before manual receipts existed, an old review stored the same file identity.
    # Its findings are optional; only the bytes identity remains relevant here.
    approved_hash = output.get("approved_sha256") or (output.get("review") or {}).get("file_sha256")
    approved_input = output.get("approved_input_signature") or output.get("input_signature")
    return bool((not approved_hash or approved_hash == output.get("sha256")) and
                approved_input == output.get("input_signature"))


def scrub_retired_delivery_data(data, carousel_id, old_ids):
    """Scrub one task's retired package identities from an in-memory catalog; never read or write files.

    Backups may predate the surviving package, so their delivery list is allowed
    to become empty. Callers persist the cleaned catalog and remove returned
    unshared receipt refs only after checking references across all catalogs.
    """
    if (not isinstance(data, dict) or not isinstance(carousel_id, str) or not CAROUSEL_ID.fullmatch(carousel_id) or
            not isinstance(old_ids, (list, tuple)) or any(not isinstance(value, str) or not DELIVERY_ID.fullmatch(value)
                                                       for value in old_ids)):
        raise VoiceError(400, "历史成品包资料清理参数无效。")
    item = next((entry for entry in data.get("carousels", []) if entry.get("id") == carousel_id), None)
    # A backup may contain only an old operation receipt after the task itself
    # was removed. Its replay snapshot must be retired too.
    scope = item if item is not None else {"id": carousel_id, "deliveries": []}
    return ProductionStore._prune_carousel_deliveries(data, scope, "", list(old_ids))


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
        return {"schema_version": 1, "voices": [], "defaults": {}, "readings": [], "jobs": [], "carousels": [], "requests": {},
                "settings": {"creators": [], "products": {}}}

    @staticmethod
    def product_settings(data, product):
        """Saved DaVinci settings for a product, completed with the defaults used by the existing projects."""
        key = product_key(product)
        saved = data["settings"]["products"].get(key, {})
        return {"project": key, "code": re.sub(r"\D", "", key) or key, "fps": "29.97", "width": 1080, "height": 1920,
                "audio_bin": "音频字幕", "timeline_bin": "时间线", **saved}

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
            value.setdefault("carousels", [])  # catalogs written before photo carousels
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
                      "settings": copy.deepcopy(data["settings"]), "carousels": copy.deepcopy(selected(data["carousels"])),
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
            for collection, kind in (("jobs", "video"), ("carousels", "carousel")):
                for item in data[collection]:
                    if selected is not None and item.get("product") != selected:
                        continue
                    row = {key: copy.deepcopy(item.get(key)) for key in ("id", "script_id", "archived")}
                    row.update(kind=kind, status=job_status(item) if kind == "video" else item.get("status"))
                    if kind == "carousel":
                        row["publication"] = copy.deepcopy(item.get("publication"))
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

    def carousel(self, identifier):
        with self.lock:
            found = next((item for item in self._read_catalog()["carousels"] if item["id"] == identifier), None)
            if not found:
                raise VoiceError(404, "这张图文制作单不存在。")
            return copy.deepcopy(found)

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
        # A receipt must never restore the download identity of a retired package.
        if isinstance(result, dict) and isinstance(result.get("delivery"), dict):
            carousel_id = (result.get("carousel") or {}).get("id")
            current = next((item for item in data.get("carousels", []) if item.get("id") == carousel_id), None)
            if current is not None and not any(entry.get("id") == result["delivery"].get("id")
                                               for entry in current.get("deliveries", [])):
                raise VoiceError(409, "这份历史成品包已清理，请领取当前最新成品包。")
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
                        "settings": self._settings, "timeline": self._timeline, "job_completion": self._job_completion,
                        "timeline_archive": self._timeline_archive, "timeline_task_status": self._timeline_task_status,
                        "edit_start": self._edit_start, "edit_bind_timeline": self._edit_bind_timeline,
                        "edit_finish": self._edit_finish, "edit_recheck": self._edit_recheck,
                        "edit_approve": self._edit_approve, "edit_baseline": self._edit_baseline,
                        "edit_delete": self._edit_delete,
                        "carousel": self._carousel, "carousel_slides": self._carousel_slides, "carousel_archive": self._carousel_archive,
                        "carousel_search_start": self._carousel_search_start, "carousel_search_delete": self._carousel_search_delete, "carousel_tidy": self._carousel_tidy,
                        "job_delete": self._job_delete, "carousel_delete": self._carousel_delete, "carousel_search_finish": self._carousel_search_finish,
                        "carousel_import": self._carousel_import, "carousel_base": self._carousel_base,
                        "carousel_import_finished": self._carousel_import_finished, "carousel_requirements": self._carousel_requirements,
                        "carousel_review_start": self._carousel_review_start, "carousel_review_finish": self._carousel_review_finish,
                        "carousel_select_output": self._carousel_select_output, "carousel_generation_stage": self._carousel_generation_stage,
                        "carousel_generation_start": self._carousel_generation_start, "carousel_generation_finish": self._carousel_generation_finish,
                        "carousel_autopilot_start": self._carousel_autopilot_start, "carousel_autopilot_update": self._carousel_autopilot_update,
                        "carousel_output": self._carousel_output, "carousel_approval": self._carousel_approval,
                        "carousel_approval_batch": self._carousel_approval_batch,
                        "carousel_delivery": self._carousel_delivery, "carousel_delivery_prune": self._carousel_delivery_prune,
                        "carousel_collection": self._carousel_collection,
                        "carousel_publication": self._carousel_publication, "carousel_calendar": self._carousel_calendar,
                        "carousel_plan": self._carousel_plan,
                        "carousel_group_review_start": self._carousel_group_review_start,
                        "carousel_group_review_finish": self._carousel_group_review_finish}
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
        if job.get("manual_completion") and not manual_completion_current(job):
            self._clear_manual_completion(job, "选用配音已变化")
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
                **({"carousel_requirements": previous["carousel_requirements"]} if "carousel_requirements" in previous else {}),
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
        if job.get("manual_completion") and not manual_completion_current(job):
            self._clear_manual_completion(job, "当前时间线已变化")
        job["status"] = job_status(job)
        job["history"].append({"at": record["built_at"], "event": "timeline", "note": f"在达芬奇建时间线 {record['name']}"})
        return {"job": copy.deepcopy(job), "timeline": copy.deepcopy(record)}

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
    def _clear_manual_completion(job, reason):
        receipt = job.pop("manual_completion", None)
        if receipt:
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completion_cleared",
                                   "note": f"{reason}，已撤销 {receipt.get('timeline_name', '')} 的手动成功标记"})

    def _job_completion(self, data, body, extra):
        job = self._find_job(data, body.get("job_id"))
        completed = body.get("completed")
        if type(completed) is not bool:
            raise VoiceError(400, "请说明是手动标记成功还是撤销标记。")
        note = text(body.get("note", ""), "完成备注", False, 2000)
        if job.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再修改成功标记。")
        if any(edit.get("status") == "running" for edit in job.get("edits", [])):
            raise VoiceError(409, "剪辑仍在运行，请结束后再修改成功标记。")
        timeline = current_timeline(job)
        if completed:
            timeline_uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
            if not timeline or timeline.get("saved") is False:
                raise VoiceError(409, "请先为当前选用配音建立并保存时间线，再手动标记成功。")
            if timeline.get("unique_id") != timeline_uid:
                raise VoiceError(409, "当前时间线已变化，请刷新后再标记成功。")
            timeline["task_status"] = "completed"
            if manual_completion_current(job):
                return {"job": copy.deepcopy(job), "completed": True,
                        "manual_completion": copy.deepcopy(job["manual_completion"])}
            job["updated_at"] = now()
            job["manual_completion"] = {"at": job["updated_at"], "note": note, "timeline_uid": timeline["unique_id"],
                                        "timeline_name": timeline.get("name", ""), "take_id": job["selected_take"],
                                        "basis": "human_review"}
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completed",
                                   "note": f"人工手动标记成功（{timeline.get('name', '')}）" + (f"：{note}" if note else "")})
        else:
            timeline_uid = text(body.get("timeline_uid"), "时间线标识", limit=500)
            completion_at = text(body.get("completion_at"), "成功标记时间", limit=100)
            receipt = job.get("manual_completion")
            if not receipt:
                raise VoiceError(409, "这张制作单还没有手动成功标记。")
            if timeline_uid != receipt.get("timeline_uid") or completion_at != receipt.get("at"):
                raise VoiceError(409, "成功标记已变化，请刷新后再撤销。")
            job.pop("manual_completion")
            target = next((row for row in job.get("timelines", []) if row.get("unique_id") == timeline_uid), None)
            if target:
                target.pop("task_status", None)
                target["task_status"] = timeline_task_status(job, target)
            job["updated_at"] = now()
            job["history"].append({"at": job["updated_at"], "event": "job_manual_completion_revoked",
                                   "note": f"撤销 {receipt.get('timeline_name', '')} 的手动成功标记" + (f"：{note}" if note else "")})
        job["status"] = job_status(job)
        return {"job": copy.deepcopy(job), "completed": manual_completion_current(job),
                "manual_completion": copy.deepcopy(job.get("manual_completion"))}

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
        self._clear_manual_completion(job, "已发起新的 AI 剪辑")
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
            for item in data["carousels"]:
                for run in item.get("autopilot_runs", []):
                    if run.get("status") == "running":
                        stamp = now()
                        message = "工作台重启时 AI 全程制作还在进行，已中断；已保存的底图、成图和核对结果仍保留，可继续制作。"
                        run.update(status="interrupted", phase="制作已中断", message=message, error=message,
                                   finished_at=stamp, updated_at=stamp)
                        item["updated_at"] = stamp
                        item.setdefault("history", []).append({"at": stamp, "event": "autopilot_interrupted",
                            "note": f"AI 全程制作已中断 {run['id']}"})
                        changed = True
                for run in item.get("group_reviews", []):
                    if run.get("status") == "running":
                        run.update(status="interrupted", finished_at=now(), error="工作台重启时整组验收还在进行，已中断，可重新验收。")
                        changed = True
                for search in item.get("searches", []):
                    if search.get("status") == "running":
                        search.update(status="interrupted", finished_at=now(),
                                      error="工作台重启时这次找底图还在进行，已中断；达芬奇可能停在别的时间线上，请切回你要的时间线。")
                        changed = True
                for generation in item.get("generations", []):
                    if generation.get("status") == "running":
                        generation.update(status="interrupted", finished_at=now(),
                                          error="工作台重启时这次出图还在进行，已中断；已保存的版本仍保留，可以重新出图。")
                        for stages in generation.get("stages", {}).values():
                            for stage in stages.values():
                                if stage.get("status") == "running":
                                    stage.update(status="interrupted", finished_at=generation["finished_at"])
                        changed = True
                for slide in item.get("slides", []):
                    for output in slide.get("outputs", []):
                        for review in output.get("review_runs", []):
                            if review.get("status") == "running":
                                review.update(status="interrupted", finished_at=now(), error="核对进程已中断，成图仍保留，可单独重新核对。")
                                if output.get("latest_review_id") == review["id"]:
                                    output["review"] = {"status": "review_failed", "execution_status": "interrupted",
                                        "summary": review["error"], "review_id": review["id"], "file_sha256": review.get("file_sha256"),
                                        "input_signature": review.get("input_signature"), "scope": copy.deepcopy(review.get("scope", [])),
                                        "finished_at": review["finished_at"], "checks": [], "issues": []}
                                    output.setdefault("stages", {})["review"] = {"status": "interrupted", "started_at": review.get("started_at", ""),
                                                                               "finished_at": review["finished_at"], "review_id": review["id"]}
                                changed = True
            if changed:
                self.write(data)

    @staticmethod
    def _find_carousel(data, identifier):
        found = next((item for item in data["carousels"] if item["id"] == identifier), None)
        if not found:
            raise VoiceError(404, "这张图文制作单不存在。")
        return found

    @staticmethod
    def _carousel(data, body, resolved):
        if not isinstance(resolved, dict) or not resolved.get("slides"):
            raise VoiceError(400, "图文制作单缺少已核对的逐张内容。")
        table_id = (resolved.get("source") or {}).get("table_id")
        if table_id:
            existing = next((item for item in data["carousels"]
                             if item.get("script_id") == resolved.get("script_id")
                             and item.get("script_version") == resolved.get("script_version")
                             and (item.get("source") or {}).get("table_id") == table_id), None)
            if existing:
                return {"carousel": copy.deepcopy(existing), "existing": True}
        stamp = now()
        item = {"id": "CAR-" + uuid4().hex[:12], **copy.deepcopy(resolved), "status": "draft", "archived": False, "searches": [], "generations": [],
                "history": [{"at": stamp, "event": "created", "note": "按图文技能生成的逐张表创建制作单" if (resolved.get("source") or {}).get("table_id") else "按已采纳的图文版创建制作单"}],
                "created_at": stamp, "updated_at": stamp}
        validate_carousel_lineage(item)
        item.setdefault("plan", empty_plan())
        item["group_reviews"] = []
        for slide in item["slides"]:
            slide.update(base=None, status="pending", candidates=[], outputs=[], output_id=None)
            slide.setdefault("stage", "")
            slide.setdefault("shot", "")
        defaults = data["settings"]["products"].get(product_key(item.get("product", "")), {}).get("carousel_requirements")
        requirements = {**(copy.deepcopy(defaults) if isinstance(defaults, dict) else {}), **copy.deepcopy(NEW_CAROUSEL_REQUIREMENTS)}
        if isinstance(defaults, dict) and defaults.get("mode") == "layout_only" and not defaults.get("hands_authorized"):
            for key in ("style", "source"):
                if key in defaults:
                    requirements[key] = copy.deepcopy(defaults[key])
        for key in ("authorized_hand_slides", "no_hand_slides", "hand_look", "hand_edit_allowed", "preserve_slides",
                    "render_contract", "caption_area", "instruction", "style_reference"):
            requirements.pop(key, None)
        if "requirements" in body:
            if not isinstance(body["requirements"], dict):
                raise VoiceError(400, "制作要求格式无效。")
            requested = copy.deepcopy(body["requirements"])
            explicit_generation = requested.get("mode") in ("generated_visual", "photo_preserving") or requested.get("workflow") in ("generation_first", "subject_locked")
            requirements = requested if explicit_generation else {**requirements, **requested}
        requirements.setdefault("caption_style", NEW_CAROUSEL_REQUIREMENTS["caption_style"])
        item["requirements"] = carousel_requirements({**item, "requirements": requirements})
        item["image_aspect"] = item["requirements"]["aspect"]
        item["history"].append({"at": stamp, "event": "requirements", "note": "保存本次图文制作要求"})
        data["carousels"].append(item)
        return {"carousel": copy.deepcopy(item)}

    def _carousel_slides(self, data, body, extra):
        """Only how each slide is made can change; its words belong to the adopted script version."""
        item = self._find_carousel(data, body.get("carousel_id"))
        values = body.get("slides")
        if not isinstance(values, list) or len(values) != len(item["slides"]):
            raise VoiceError(400, "须逐张提交，张数与制作单一致。")
        context = extra.get("script_context") if isinstance(extra, dict) else None
        if not item.get("script_context") and context:
            if any(context.get(key) != item.get(key) for key in ("script_id", "script_version")):
                raise VoiceError(409, "冻结原稿与本单版本不一致。")
            item["script_context"] = copy.deepcopy(context)
            item["history"].append({"at": now(), "event": "script_context", "note": "从原逐张表恢复冻结原稿，供逐张对应与验收"})
        for slide, value in zip(item["slides"], values):
            if not isinstance(value, dict) or value.get("number") != slide["number"]:
                raise VoiceError(400, "图文编号与制作单不一致，请刷新后重试。")
            for key, label in (("role", "作用"), ("source", "素材来源"), ("forbidden", "禁写")):
                updated = text(value.get(key, slide[key]), label, False, 2000)
                if updated != slide[key]:
                    self._clear_carousel_approval(slide)
                slide[key] = updated
            for key, updated in slide_lineage(value, strict=True).items():
                if updated != slide.get(key):
                    self._clear_carousel_approval(slide)
                slide[key] = updated
        validate_carousel_lineage(item)
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "slides", "note": "修改逐张任务、原稿对应、画面证据、素材来源或禁写"})
        return {"carousel": copy.deepcopy(item)}

    def _carousel_plan(self, data, body, extra):
        """The group plan and each slide's stage and shot; none of it changes a slide's words or image contract."""
        item = self._find_carousel(data, body.get("carousel_id"))
        plan, _ = normalize_plan(body.get("plan"), strict=True)
        values = body.get("slides")
        if not isinstance(values, list) or len(values) != len(item["slides"]):
            raise VoiceError(400, "须逐张提交环节和景别，张数与制作单一致。")
        fields = []
        for slide, value in zip(item["slides"], values):
            if not isinstance(value, dict) or value.get("number") != slide["number"]:
                raise VoiceError(400, "图文编号与制作单不一致，请刷新后重试。")
            fields.append(slide_fields(value, strict=True)[0])
        item["plan"] = plan
        for slide, value in zip(item["slides"], fields):
            slide.update(value)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "plan", "note": "修改整组方案（路线、卖点标签、环节、景别、音乐方向）"})
        return {"carousel": copy.deepcopy(item)}

    def _carousel_group_review_start(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if not isinstance(record, dict) or not GROUP_REVIEW_ID.fullmatch(str(record.get("id"))) or record.get("status") != "running":
            raise VoiceError(500, "整组验收尚未准备好，未启动。")
        if any(run.get("status") == "running" for run in item.get("group_reviews", [])):
            raise VoiceError(409, "这组图文正在整组验收，请等待本次结果。")
        runs = item.setdefault("group_reviews", [])
        runs.append(copy.deepcopy(record))
        if len(runs) > MAX_GROUP_REVIEWS:
            del runs[:len(runs) - MAX_GROUP_REVIEWS]
        item["updated_at"] = record["started_at"]
        return {"carousel": copy.deepcopy(item), "group_review": copy.deepcopy(record)}

    def _carousel_group_review_finish(self, data, body, result):
        item = self._find_carousel(data, body.get("carousel_id"))
        run = next((entry for entry in item.get("group_reviews", []) if entry["id"] == body.get("group_review_id")), None)
        if not run:
            raise VoiceError(404, "这次整组验收记录不存在。")
        if run.get("status") != "running":
            return {"carousel": copy.deepcopy(item), "group_review": copy.deepcopy(run)}
        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            raise VoiceError(500, "整组验收结果格式无效。")
        run.update({key: copy.deepcopy(result.get(key)) for key in ("status", "finished_at", "error", "review")})
        item["updated_at"] = run["finished_at"] or now()
        if run["status"] == "completed":
            outcome = (run.get("review") or {}).get("status")
            item["history"].append({"at": item["updated_at"], "event": "group_review",
                                    "note": "整组验收" + {"pass": "全部通过", "fail": "有未通过项"}.get(outcome, "有待确认项")})
        return {"carousel": copy.deepcopy(item), "group_review": copy.deepcopy(run)}

    def _carousel_archive(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        if not isinstance(body.get("archived"), bool):
            raise VoiceError(400, "归档参数无效。")
        item.update(archived=body["archived"], updated_at=now())
        item["history"].append({"at": item["updated_at"], "event": "archived" if item["archived"] else "restored", "note": ""})
        return {"carousel": copy.deepcopy(item)}

    @staticmethod
    def _carousel_status(item):
        slides = item["slides"]
        selected = []
        for slide in slides:
            output = next((entry for entry in slide.get("outputs", []) if entry["id"] == slide.get("output_id")), None)
            valid = carousel_output_current(item, slide, output)
            selected.append(output if valid else None)
            slide["status"] = "approved" if valid and carousel_confirmation_current(item, slide, output) else "generated" if valid else "awaiting_review" if slide.get("outputs") else "picked" if slide.get("base") else "pending"
        if slides and all(output and carousel_confirmation_current(item, slide, output) for slide, output in zip(slides, selected)):
            return "approved"
        if any(selected):
            return "reviewing"
        if any(slide.get("outputs") for slide in slides):
            return "reviewing"
        if slides and all(slide.get("base") for slide in slides):
            return "ready"
        return "picking" if any(slide.get("base") or slide.get("candidates") for slide in slides) else "draft"

    def _carousel_search_start(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if not isinstance(record, dict) or not SEARCH_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "找底图任务未完成核对，未启动。")
        item.setdefault("searches", []).append(copy.deepcopy(record))
        item["updated_at"] = record["started_at"]
        item["history"].append({"at": record["started_at"], "event": "search", "note": f"开始从达芬奇找底图 {record['id']}"})
        return {"carousel": copy.deepcopy(item), "search": copy.deepcopy(record)}

    def _carousel_search_delete(self, data, body, extra):
        """Drop one finished search record and the candidates only it produced; the one chosen as a base stays.

        The caller removes the search folder and the returned candidate files after the record is gone."""
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再删除找底图记录。")
        search = next((entry for entry in item.get("searches", []) if entry["id"] == body.get("search_id")), None)
        if not search:
            raise VoiceError(404, "这次找底图记录不存在。")
        if search.get("status") == "running":
            raise VoiceError(409, "这次找底图还在运行，请先停止或等它结束。")
        item["searches"] = [entry for entry in item["searches"] if entry["id"] != search["id"]]
        removed, kept_as_base = [], 0
        for slide in item["slides"]:
            if "candidates" not in slide:
                continue
            base_id = (slide.get("base") or {}).get("id")
            kept = []
            for candidate in slide["candidates"]:
                if candidate.get("search_id") != search["id"]:
                    kept.append(candidate)
                elif candidate["id"] == base_id:
                    kept.append(candidate)
                    kept_as_base += 1
                else:
                    removed.append({"id": candidate["id"], "file": candidate.get("file"), "thumb": candidate.get("thumb")})
            slide["candidates"] = kept
        item["updated_at"] = now()
        item["status"] = self._carousel_status(item)
        item["history"].append({"at": item["updated_at"], "event": "search_deleted",
                                "note": f"删除找底图记录 {search['id']}，移除 {len(removed)} 张未选用的候选"
                                        + (f"，保留 {kept_as_base} 张已选底图" if kept_as_base else "")})
        for key in [key for key, saved in data["requests"].items()
                    if isinstance(saved.get("result"), dict) and (saved["result"].get("search") or {}).get("id") == search["id"]]:
            del data["requests"][key]
        return {"carousel": copy.deepcopy(item), "deleted_search": search["id"], "removed_candidates": removed, "kept_as_base": kept_as_base}

    def _carousel_search_finish(self, data, body, result):
        item = self._find_carousel(data, body.get("carousel_id"))
        search = next((entry for entry in item.get("searches", []) if entry["id"] == body.get("search_id")), None)
        if not search:
            raise VoiceError(404, "这次找底图记录不存在。")
        if search.get("status") != "running":
            return {"carousel": copy.deepcopy(item), "search": copy.deepcopy(search)}
        candidates = result.pop("candidates", [])
        search.update(copy.deepcopy(result), added=len(candidates))
        by_number = {slide["number"]: slide for slide in item["slides"]}
        for candidate in candidates:
            by_number[candidate["slide"]].setdefault("candidates", []).append(copy.deepcopy(candidate))
        item["status"] = self._carousel_status(item)
        item["updated_at"] = search["finished_at"]
        labels = {"completed": "找底图完成", "failed": "找底图失败", "cancelled": "找底图已停止"}
        item["history"].append({"at": search["finished_at"], "event": "search_" + search["status"],
                                "note": f"{labels.get(search['status'], search['status'])} {search['id']}，新增 {len(candidates)} 张候选"})
        return {"carousel": copy.deepcopy(item), "search": copy.deepcopy(search)}

    def _carousel_import(self, data, body, candidate):
        item = self._find_carousel(data, body.get("carousel_id"))
        slide = next((entry for entry in item["slides"] if entry["number"] == body.get("slide")), None)
        if not slide or not isinstance(candidate, dict) or not CANDIDATE_ID.fullmatch(str(candidate.get("id"))):
            raise VoiceError(400, "要添加到哪一张无效。")
        slide.setdefault("candidates", []).append(copy.deepcopy(candidate))
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "import", "note": f"第 {slide['number']} 张添加已有图片 {candidate.get('name', '')}"})
        return {"carousel": copy.deepcopy(item), "candidate": copy.deepcopy(candidate)}

    def _carousel_import_finished(self, data, body, prepared):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item.get("archived"):
            raise VoiceError(409, "制作单已归档，请恢复后再导入成品。")
        if not isinstance(prepared, dict) or not FINISHED_IMPORT_ID.fullmatch(str(prepared.get("import", {}).get("id"))):
            raise VoiceError(500, "现有成品尚未完成文件核对，未登记。")
        record = copy.deepcopy(prepared["import"])
        existing = next((entry for entry in item.get("finished_imports", []) if
                         entry.get("content_signature") == record.get("content_signature")), None)
        if existing:
            ids = set(existing.get("output_ids", []))
            return {"carousel": copy.deepcopy(item), "import": copy.deepcopy(existing), "duplicate": True,
                    "outputs": [copy.deepcopy(output) for slide in item["slides"] for output in slide.get("outputs", []) if output["id"] in ids]}
        if prepared.get("expected_inputs") != fingerprint({"script_id": item["script_id"], "script_version": item["script_version"],
                "slides": [{key: slide.get(key) for key in ("number", "title_lines", "body", "title_zh", "body_zh")} for slide in item["slides"]],
                "caption_en": item.get("caption_en", ""), "caption_zh": item.get("caption_zh", "")}):
            raise VoiceError(409, "文案版本在导入时已变化，请保留文件并按最新制作单重新导入。")
        if prepared.get("expected_requirements") != fingerprint(item.get("requirements")):
            raise VoiceError(409, "制作要求在导入期间已被修改，本次未覆盖；请保留文件并按最新要求重新导入。")
        outputs = prepared.get("outputs")
        if not isinstance(outputs, list) or not outputs or len({entry.get("slide") for entry in outputs}) != len(outputs):
            raise VoiceError(500, "导入成品的张号无效，未登记。")
        candidate_item = copy.deepcopy(item)
        # Import requirements describe already finished outputs. Their snapshots
        # stay with those outputs, without replacing future making instructions.
        by_number = {slide["number"]: slide for slide in candidate_item["slides"]}
        for entry in outputs:
            slide = by_number.get(entry.get("slide"))
            if not slide or not OUTPUT_ID.fullmatch(str(entry.get("id"))) or entry.get("generation_id"):
                raise VoiceError(500, "导入成品未完成逐张核对，未登记。")
            if entry.get("input_signature") != carousel_input_signature(candidate_item, slide, entry.get("aspect")):
                raise VoiceError(409, "制作要求在导入时已变化，请按最新制作单重新导入。")
            entry = copy.deepcopy(entry)
            entry.update(approved=False, approved_at="", note="", origin="external_generated_layout", generation_id=None)
            entry["review"] = {"status": "pending", "summary": "现有成品已保存，等待独立核对。", "issues": [], "checks": []}
            slide.setdefault("outputs", []).append(entry)
        # New candidates never take another version's confirmation or selection.
        item.update(candidate_item)
        record["output_ids"] = [entry["id"] for entry in outputs]
        item.setdefault("finished_imports", []).append(record)
        item["updated_at"] = record["imported_at"]
        item["status"] = self._carousel_status(item)
        item["history"].append({"at": item["updated_at"], "event": "import_finished",
                                "note": f"导入现有成品 {record['id']}，{len(outputs)} 张待核对，保留已有选版"})
        return {"carousel": copy.deepcopy(item), "import": copy.deepcopy(record), "outputs": copy.deepcopy(outputs), "duplicate": False}

    def _carousel_requirements(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        if not isinstance(body.get("requirements"), dict):
            raise VoiceError(400, "请填写结构化制作要求。")
        item["requirements"] = carousel_requirements({**item, "requirements": body["requirements"]})
        item["image_aspect"] = item["requirements"]["aspect"]
        data["settings"]["products"].setdefault(product_key(item["product"]), {})["carousel_requirements"] = {
            key: copy.deepcopy(item["requirements"][key]) for key in PRODUCT_REQUIREMENT_KEYS if key in item["requirements"]}
        item["updated_at"] = now()
        item["status"] = self._carousel_status(item)
        item["history"].append({"at": item["updated_at"], "event": "requirements", "note": "保存制作模式、修改授权与受保护内容"})
        return {"carousel": copy.deepcopy(item)}

    def _carousel_review_start(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        slide, output = self._carousel_bound_output(item, body)
        if not isinstance(record, dict) or not REVIEW_ID.fullmatch(str(record.get("id"))) or record.get("status") != "running":
            raise VoiceError(500, "重新核对尚未准备好，未启动。")
        if not isinstance(record.get("scope"), list) or not record["scope"] or any(
                not isinstance(scope, str) or scope not in {"visual", "text", "product", "evidence"} for scope in record["scope"]):
            raise VoiceError(400, "请选择明确的视觉、文字、商品对照或实拍证据核对范围。")
        if record.get("file_sha256") != output.get("sha256") or record.get("input_signature") != carousel_input_signature(item, slide, output["aspect"]):
            raise VoiceError(409, "成图或制作要求已变化，请刷新后核对当前版本。")
        if output.get("requirements_signature") and record.get("requirements_signature") != output["requirements_signature"]:
            raise VoiceError(409, "核对要求与这张候选的制作授权不一致，请按对应版本重新核对。")
        output.setdefault("review_runs", []).append(copy.deepcopy(record))
        output["latest_review_id"] = record["id"]
        output.setdefault("stages", {})["review"] = {"status": "running", "started_at": record["started_at"], "finished_at": "", "review_id": record["id"]}
        item["updated_at"] = record["started_at"]
        return {"carousel": copy.deepcopy(item), "review": copy.deepcopy(record), "output": copy.deepcopy(output)}

    def _carousel_review_finish(self, data, body, result):
        item = self._find_carousel(data, body.get("carousel_id"))
        slide, output = self._carousel_bound_output(item, body)
        run = next((entry for entry in output.get("review_runs", []) if entry["id"] == body.get("review_id")), None)
        if not run:
            raise VoiceError(404, "这次核对记录不存在。")
        if run.get("status") != "running":
            return {"carousel": copy.deepcopy(item), "review": copy.deepcopy(run), "output": copy.deepcopy(output)}
        if not isinstance(result, dict) or result.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            raise VoiceError(500, "核对结果格式无效，已保留成图。")
        result = copy.deepcopy(result)
        bound = (output.get("latest_review_id") == run["id"] and run.get("file_sha256") == output.get("sha256") == result.get("file_sha256") and
                 run.get("input_signature") == result.get("input_signature") == carousel_input_signature(item, slide, output["aspect"]) and
                 ("scope" not in result or result["scope"] == run.get("scope")) and
                 ("requirements_signature" not in result or result["requirements_signature"] == run.get("requirements_signature")))
        # Keep the original read contract even if the returned result is for different bytes.
        run.update({key: value for key, value in result.items() if key not in {"file_sha256", "input_signature", "scope", "requirements_signature"}}, stale=not bound)
        run["result_file_sha256"] = result.get("file_sha256")
        run["result_input_signature"] = result.get("input_signature")
        if bound:
            review = copy.deepcopy(result.get("review") or {"status": "review_failed", "summary": result.get("error") or "独立核对未完成，成图已保留，可重试。", "checks": [], "issues": []})
            review.update(file_sha256=run["file_sha256"], input_signature=run["input_signature"], scope=copy.deepcopy(run.get("scope", [])),
                          review_id=run["id"], finished_at=run.get("finished_at"), execution_status=run["status"],
                          requirements_signature=run.get("requirements_signature"))
            if run["status"] != "completed":
                review["status"] = "review_failed"
            output["review"] = review
            if ("review_deferred" in output and run["status"] == "completed" and review.get("completed") and
                    set(run.get("scope", [])) == {"visual", "text", "product", "evidence"}):
                output["review_deferred"] = False
            output.setdefault("stages", {})["review"] = {"status": run["status"], "started_at": run.get("started_at", ""),
                                                        "finished_at": run.get("finished_at", ""), "review_id": run["id"]}
        item["updated_at"] = run.get("finished_at") or now()
        item["status"] = self._carousel_status(item)
        item["history"].append({"at": item["updated_at"], "event": "review_" + run["status"], "note": f"第 {slide['number']} 张独立核对 {run['id']}，成图保留"})
        return {"carousel": copy.deepcopy(item), "review": copy.deepcopy(run), "output": copy.deepcopy(output)}

    @staticmethod
    def _carousel_bound_output(item, body):
        slide = next((entry for entry in item["slides"] if type(body.get("slide")) is int and entry["number"] == body["slide"]), None)
        output = next((entry for entry in (slide or {}).get("outputs", []) if entry["id"] == body.get("output_id")), None)
        if not output:
            raise VoiceError(404, "这张成图不属于这一张。")
        return slide, output

    def _carousel_select_output(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        slide, output = self._carousel_bound_output(item, body)
        if not carousel_output_current(item, slide, output):
            raise VoiceError(409, "这个版本对应的底图或制作要求已变化，请按当前资料重新出图。")
        slide["output_id"] = output["id"]
        item["updated_at"] = now()
        item["status"] = self._carousel_status(item)
        return {"carousel": copy.deepcopy(item), "output": copy.deepcopy(output)}

    def _carousel_generation_stage(self, data, body, stage):
        item = self._find_carousel(data, body.get("carousel_id"))
        run = next((entry for entry in item.get("generations", []) if entry["id"] == body.get("generation_id")), None)
        if not run or body.get("slide") not in run.get("slides", []) or body.get("stage") not in {"prepare", "generation", "layout", "review", "confirmation", "package", "preparation", "generate", "confirm", "packaging"}:
            raise VoiceError(400, "制作阶段不属于这次任务。")
        if run.get("status") != "running":
            return {"carousel": copy.deepcopy(item), "generation": copy.deepcopy(run)}
        if not isinstance(stage, dict) or stage.get("status") not in {"pending", "running", "completed", "failed", "skipped", "cancelled", "interrupted"}:
            raise VoiceError(400, "制作阶段状态无效。")
        stages = run.setdefault("stages", {}).setdefault(str(body["slide"]), {})
        stages[body["stage"]] = {**stages.get(body["stage"], {}), **copy.deepcopy(stage)}
        return {"carousel": copy.deepcopy(item), "generation": copy.deepcopy(run)}

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

    def _carousel_delete(self, data, body, extra):
        """Remove a carousel order; its folder is removed by the caller."""
        item = self._find_carousel(data, body.get("carousel_id"))
        if any(entry.get("status") == "running" for key in ("searches", "generations", "autopilot_runs") for entry in item.get(key, [])):
            raise VoiceError(409, "这张图文制作单还有任务在运行。")
        data["carousels"] = [entry for entry in data["carousels"] if entry["id"] != item["id"]]
        for key in [key for key, saved in data["requests"].items() if isinstance(saved.get("result"), dict) and
                    ((saved["result"].get("carousel") or {}).get("id") == item["id"])]:
            del data["requests"][key]
        return {"deleted_carousel": item["id"], "title": item.get("title", "")}

    def _carousel_tidy(self, data, body, extra):
        """Every slide that has a base keeps only that image; the other candidate records are dropped (files: the caller)."""
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再整理候选图。")
        removed = []
        for slide in item["slides"]:
            base_id = (slide.get("base") or {}).get("id")
            if not base_id or "candidates" not in slide:
                continue
            for entry in slide["candidates"]:
                if entry["id"] != base_id:
                    removed.append({"id": entry["id"], "file": entry.get("file"), "thumb": entry.get("thumb")})
            slide["candidates"] = [entry for entry in slide["candidates"] if entry["id"] == base_id]
        if removed:
            item["updated_at"] = now()
            item["history"].append({"at": item["updated_at"], "event": "tidy", "note": f"整理候选图：每张只保留底图，移除 {len(removed)} 张多余候选"})
        return {"carousel": copy.deepcopy(item), "removed_candidates": removed}

    def _carousel_base(self, data, body, extra):
        removed = []
        item = self._find_carousel(data, body.get("carousel_id"))
        preserve = body.get("preserve_candidates", False)
        if type(preserve) is not bool:
            raise VoiceError(400, "保留候选图参数无效。")
        slide = next((entry for entry in item["slides"] if entry["number"] == body.get("slide")), None)
        if not slide:
            raise VoiceError(400, "这一张不存在。")
        chosen = body.get("candidate")
        if chosen != (slide.get("base") or {}).get("id"):
            self._clear_carousel_approval(slide)
        if chosen is None:
            slide.update(base=None, status="pending")
        else:
            candidate = next((entry for entry in slide.get("candidates", []) if entry["id"] == chosen), None)
            if not candidate:
                raise VoiceError(404, "这张候选图不属于这一张。")
            slide.update(base=copy.deepcopy(candidate), status="picked")
            # Manual selection keeps its existing tidy behavior; AI runs can retain
            # their alternatives so selecting a base remains reversible.
            if not preserve:
                removed = [{"id": entry["id"], "file": entry.get("file"), "thumb": entry.get("thumb")}
                           for entry in slide.get("candidates", []) if entry["id"] != chosen]
                slide["candidates"] = [entry for entry in slide.get("candidates", []) if entry["id"] == chosen]
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "base",
                                "note": f"第 {slide['number']} 张" + ((f"选用底图 {chosen}，保留全部候选" if preserve else
                                    f"选用底图 {chosen}，移除其余 {len(removed)} 张候选") if chosen else "取消底图")})
        return {"carousel": copy.deepcopy(item), "removed_candidates": removed}

    def _carousel_autopilot_start(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再交给 AI 制作。")
        if any(run.get("status") == "running" for run in item.get("autopilot_runs", [])):
            raise VoiceError(409, "这张图文制作单已有 AI 全程制作在运行。")
        if (not isinstance(record, dict) or not AUTOPILOT_ID.fullmatch(str(record.get("id"))) or
                record.get("status") != "running" or not isinstance(record.get("started_at"), str) or not record["started_at"]):
            raise VoiceError(500, "AI 全程制作任务未完成核对，未启动。")
        if any(run.get("id") == record["id"] for run in item.get("autopilot_runs", [])):
            raise VoiceError(409, "这次 AI 全程制作记录已存在，请使用新的运行标识。")
        saved = copy.deepcopy(record)
        saved.update(request_id=body["request_id"], finished_at="", updated_at=record["started_at"])
        saved["phase"] = text(record.get("phase", "准备制作"), "制作阶段", False, 300)
        saved["message"] = text(record.get("message", ""), "制作进度", False, 2000)
        item.setdefault("autopilot_runs", []).append(saved)
        item["updated_at"] = saved["started_at"]
        item.setdefault("history", []).append({"at": saved["started_at"], "event": "autopilot_start",
            "note": f"开始 AI 全程制作 {saved['id']}"})
        return {"carousel": copy.deepcopy(item), "run": copy.deepcopy(saved)}

    def _carousel_autopilot_update(self, data, body, fields):
        item = self._find_carousel(data, body.get("carousel_id"))
        run = next((entry for entry in item.get("autopilot_runs", []) if entry.get("id") == body.get("run_id")), None)
        if not run:
            raise VoiceError(404, "这次 AI 全程制作记录不存在。")
        # Final records are immutable. A delayed worker cannot reopen a cancelled
        # or interrupted run, or replace its result after the next run starts.
        if run.get("status") != "running":
            return {"carousel": copy.deepcopy(item), "run": copy.deepcopy(run)}
        allowed = {"status", "phase", "message", "error", "problems", "finished_at", "updated_at",
                   "generation_id", "review_id", "delivery_id", "slide", "attempt", "attempts",
                   "completed_slides", "stages", "result"}
        if not isinstance(fields, dict) or set(fields) - allowed:
            raise VoiceError(400, "AI 制作进度参数无效，不能修改任务身份、来源或授权记录。")
        status = fields.get("status", "running")
        if not isinstance(status, str) or status not in {"running", "completed", "failed", "cancelled", "interrupted", "needs_attention"}:
            raise VoiceError(400, "AI 制作状态无效。")
        updated = copy.deepcopy(fields)
        for key, label, limit in (("phase", "制作阶段", 300), ("message", "制作进度", 2000), ("error", "制作错误", 2000)):
            if key in updated:
                updated[key] = text(updated[key], label, False, limit)
        if "problems" in updated and (not isinstance(updated["problems"], list) or
                len(updated["problems"]) > 100 or any(not isinstance(entry, str) or len(entry) > 2000 for entry in updated["problems"])):
            raise VoiceError(400, "AI 制作问题记录无效。")
        stamp = now()
        updated.update(status=status, updated_at=stamp)
        updated["finished_at"] = text(updated.get("finished_at") or stamp, "结束时间", False, 100) if status != "running" else ""
        run.update(updated)
        item["updated_at"] = stamp
        if status != "running":
            item.setdefault("history", []).append({"at": stamp, "event": "autopilot_" + status,
                "note": f"AI 全程制作结束 {run['id']}：{run.get('message') or status}"})
        return {"carousel": copy.deepcopy(item), "run": copy.deepcopy(run)}

    @staticmethod
    def _clear_carousel_approval(slide):
        for output in slide.get("outputs", []):
            output.update(approved=False, approved_at="")

    def _carousel_generation_start(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再出图。")
        if not isinstance(record, dict) or not GENERATION_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "出图任务未完成核对，未启动。")
        if item.get("image_aspect", "3:4") != record["aspect"]:
            for slide in item["slides"]:
                self._clear_carousel_approval(slide)
        item["image_aspect"] = record["aspect"]
        item["status"] = self._carousel_status(item)
        item.setdefault("generations", []).append(copy.deepcopy(record))
        item["updated_at"] = record["started_at"]
        item["history"].append({"at": item["updated_at"], "event": "generate", "note": f"开始出图 {record['id']}"})
        return {"carousel": copy.deepcopy(item), "generation": copy.deepcopy(record)}

    def _carousel_output(self, data, body, output):
        item = self._find_carousel(data, body.get("carousel_id"))
        run = next((entry for entry in item.get("generations", []) if entry["id"] == body.get("generation_id")), None)
        slide = next((entry for entry in item["slides"] if entry["number"] == body.get("slide")), None)
        if not run or not slide or slide["number"] not in run["slides"] or not isinstance(output, dict) or not OUTPUT_ID.fullmatch(str(output.get("id"))):
            raise VoiceError(500, "成图未完成核对，未保存。")
        if run["status"] != "running":
            raise VoiceError(409, "这次出图已结束，迟到的结果未替换当前版本。")
        output = copy.deepcopy(output)
        if output.get("requirements_snapshot"):
            output["requirements_signature"] = fingerprint(output["requirements_snapshot"])
        output["input_changed"] = bool(output.get("input_changed") or
            output["input_signature"] != carousel_input_signature(item, slide, output["aspect"]))
        output.update(approved=False, approved_at="", note="")
        slide.setdefault("outputs", []).append(output)
        selected = next((entry for entry in slide.get("outputs", []) if entry["id"] == slide.get("output_id")), None)
        if not output["input_changed"] and not selected:
            slide["output_id"] = output["id"]
        run["added"] = run.get("added", 0) + 1
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "output", "note": f"第 {slide['number']} 张保存成图 {output['id']}"})
        return {"carousel": copy.deepcopy(item), "output": copy.deepcopy(output)}

    def _carousel_generation_finish(self, data, body, result):
        item = self._find_carousel(data, body.get("carousel_id"))
        run = next((entry for entry in item.get("generations", []) if entry["id"] == body.get("generation_id")), None)
        if not run:
            raise VoiceError(404, "这次出图记录不存在。")
        if run["status"] != "running":
            return {"carousel": copy.deepcopy(item), "generation": copy.deepcopy(run)}
        run.update(copy.deepcopy(result))
        item["updated_at"] = run["finished_at"]
        item["status"] = self._carousel_status(item)
        label = {"completed": "完成", "failed": "失败", "cancelled": "已停止", "interrupted": "已中断"}.get(run["status"], "结束")
        item["history"].append({"at": item["updated_at"], "event": "generation_" + run["status"],
                                "note": f"出图{label} {run['id']}，保存 {run.get('added', 0)} 张"})
        return {"carousel": copy.deepcopy(item), "generation": copy.deepcopy(run)}

    def _carousel_approval(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再确认。")
        self._approve_output(item, body.get("slide"), body.get("output_id"), body.get("approved"),
                             body.get("note", ""), body.get("actor", "human"))
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        return {"carousel": copy.deepcopy(item)}

    def _carousel_approval_batch(self, data, body, extra):
        """Confirm several slides in one save: all of them pass the same checks or none is confirmed."""
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "图文制作单已归档，请恢复后再确认。")
        picks = body.get("selections")
        if (not isinstance(picks, list) or not picks or len(picks) > len(item["slides"])
                or any(not isinstance(pick, dict) for pick in picks)
                or len({pick.get("slide") for pick in picks}) != len(picks)):
            raise VoiceError(400, "请选择要确认的成图，每张只选一版。")
        for pick in picks:
            self._approve_output(item, pick.get("slide"), pick.get("output_id"), True, "", "human")
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        return {"carousel": copy.deepcopy(item), "approved": [pick["slide"] for pick in picks]}

    def _approve_output(self, item, number, output_id, approved, note, actor):
        slide = next((entry for entry in item["slides"] if type(number) is int and entry["number"] == number), None)
        output = next((entry for entry in slide.get("outputs", []) if entry["id"] == output_id), None) if slide else None
        if not output:
            raise VoiceError(404, "这张成图不属于这一张。")
        if type(approved) is not bool:
            raise VoiceError(400, "确认参数无效。")
        if approved and not carousel_output_current(item, slide, output):
            raise VoiceError(409, f"第 {slide['number']} 张的底图或制作要求已变化，请按当前资料重新出图。")
        if approved and ((item.get("requirements") or {}).get("workflow") == "subject_locked" or
                output.get("workflow") == "subject_locked") and not carousel_subject_preservation_current(output):
            raise VoiceError(409, f"第 {slide['number']} 张尚未通过商品主体保护验证，不能选用或打包。")
        note = text(note, "确认备注", False, 2000)
        if approved:
            slide["output_id"] = output["id"]
        if actor not in {"human", "assistant_on_user_authorization"}:
            raise VoiceError(400, "确认来源无效。")
        output.update(approved=approved, approved_at=now() if approved else "", note=note, approval_actor=actor)
        output.update(approved_sha256=output["sha256"] if approved else "",
                      approved_input_signature=output["input_signature"] if approved else "")
        output.setdefault("stages", {})["confirmation"] = {"status": "completed" if approved else "pending",
            "started_at": output["approved_at"], "finished_at": output["approved_at"], "actor": actor}
        if approved and output.get("generation_id"):
            generation = next((entry for entry in item.get("generations", []) if entry["id"] == output["generation_id"]), None)
            if generation:
                generation.setdefault("stages", {}).setdefault(str(slide["number"]), {})["confirmation"] = {
                    **copy.deepcopy(output["stages"]["confirmation"]), "output_id": output["id"]}
        item["history"].append({"at": output["approved_at"] or now(), "event": "approval", "note": f"第 {slide['number']} 张" + ("工作台确认" if approved else "撤销确认") + f" {output['id']}", "actor": actor})

    @staticmethod
    def _delivery_snapshot(record):
        """Keep publication provenance without retaining a retired package's download paths."""
        keys = ("id", "created_at", "sha256", "script_id", "script_version", "selection_signature", "aspect",
                "caption_en", "caption_zh", "source", "workflow", "review_deferred")
        snapshot = {key: copy.deepcopy(record[key]) for key in keys if key in record}
        snapshot["slides"] = [{key: copy.deepcopy(slide[key]) for key in ("number", "output_id", "sha256") if key in slide}
                              for slide in record.get("slides", [])]
        return snapshot

    @staticmethod
    def _prune_carousel_deliveries(data, item, keep_id, old_ids):
        """Drop obsolete catalog identities; the caller removes files after this transaction commits."""
        retired = set(old_ids)
        records = {entry["id"]: entry for entry in item.get("deliveries", []) if entry.get("id") in retired}
        changed = bool(records)
        for publication in [item.get("publication"), *item.get("publication_history", [])]:
            if isinstance(publication, dict) and publication.get("delivery_id") in records:
                publication.setdefault("delivery_snapshot", ProductionStore._delivery_snapshot(records[publication["delivery_id"]]))
        item["deliveries"] = [entry for entry in item.get("deliveries", []) if entry.get("id") not in retired]
        for slide in item.get("slides", []):
            for output in slide.get("outputs", []):
                stages = output.get("stages") or {}
                if (stages.get("package") or {}).get("delivery_id") in retired:
                    stages.pop("package", None)
                    changed = True
        for generation in item.get("generations", []):
            for stages in (generation.get("stages") or {}).values():
                if isinstance(stages, dict) and (stages.get("package") or {}).get("delivery_id") in retired:
                    stages.pop("package", None)
                    changed = True
        for run in item.get("autopilot_runs", []):
            for scope in (run, run.get("result")):
                if not isinstance(scope, dict):
                    continue
                if scope.get("delivery_id") in retired:
                    scope.pop("delivery_id", None)
                    scope["delivery_retired"] = True
                    changed = True
                if isinstance(scope.get("delivery"), dict) and scope["delivery"].get("id") in retired:
                    scope.pop("delivery", None)
                    scope["delivery_retired"] = True
                    changed = True
        removed_requests, refs = [], set()
        if retired:
            for key, receipt in list(data.get("requests", {}).items()):
                result = receipt.get("result") if isinstance(receipt, dict) else None
                if not isinstance(result, dict):
                    continue
                package = result.get("delivery") or {}
                carousel = result.get("carousel") or {}
                has_package = isinstance(package, dict) and package.get("id") in retired
                # Sidecar stubs cannot show which historical package their full
                # snapshot carried; remove that task's full sidecar receipts too.
                has_snapshot = (isinstance(carousel, dict) and carousel.get("id") == item["id"] and
                                ("result_ref" in receipt or any(entry.get("id") in retired
                                 for entry in carousel.get("deliveries", []) if isinstance(entry, dict))))
                if has_package or has_snapshot:
                    removed_requests.append(key)
                    ref = receipt.get("result_ref") or {}
                    if isinstance(ref, dict) and isinstance(ref.get("file"), str):
                        refs.add(ref["file"])
                    del data["requests"][key]
                    changed = True
        live_refs = {(receipt.get("result_ref") or {}).get("file") for receipt in data.get("requests", {}).values()
                     if isinstance(receipt, dict) and isinstance(receipt.get("result_ref"), dict)}
        return {"changed": changed, "removed_ids": list(dict.fromkeys(old_ids)), "removed_request_ids": removed_requests,
                "receipt_refs": sorted(refs - live_refs)}

    def _carousel_delivery_prune(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        keep_id, old_ids = body.get("keep_id"), body.get("old_ids")
        if (not isinstance(keep_id, str) or not DELIVERY_ID.fullmatch(keep_id) or
                not isinstance(old_ids, list) or any(not isinstance(value, str) or not DELIVERY_ID.fullmatch(value)
                                                    for value in old_ids) or keep_id in old_ids):
            raise VoiceError(400, "成品包清理参数无效，不能删除需保留的最新成品包。")
        deliveries = item.get("deliveries", [])
        if not deliveries or deliveries[-1].get("id") != keep_id:
            raise VoiceError(409, "最新成品包已变化，请读取当前记录后重新清理。")
        removed_record = any(entry.get("id") in set(old_ids) for entry in deliveries)
        cleanup = self._prune_carousel_deliveries(data, item, keep_id, old_ids)
        if removed_record or cleanup["removed_request_ids"]:
            item["updated_at"] = now()
            item.setdefault("history", []).append({"at": item["updated_at"], "event": "delivery_pruned",
                "note": f"保留最新成品包 {keep_id}，清理 {len(cleanup['removed_ids'])} 份历史成品包及领取记录"})
        return {"carousel": copy.deepcopy(item), "delivery": copy.deepcopy(item["deliveries"][-1]), **cleanup}

    def _carousel_delivery(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "制作单已归档，请恢复后再整理成品。")
        if not isinstance(record, dict) or not DELIVERY_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "成品包未完成核验，未保存。")
        from workbench_carousel_delivery import carousel_selection_signature
        if self._carousel_status(item) != "approved" or record.get("selection_signature") != carousel_selection_signature(item):
            raise VoiceError(409, "选中的成图或确认已变化，请逐张核对后重新整理。")
        existing = next((entry for entry in item.get("deliveries", []) if entry["id"] == record["id"]), None)
        if existing:
            if existing.get("sha256") != record.get("sha256"):
                raise VoiceError(409, "已有成品包不能覆盖，请重新整理为新版本。")
            record = existing
        else:
            item.setdefault("deliveries", []).append(copy.deepcopy(record))
        cleanup = self._prune_carousel_deliveries(data, item, record["id"],
            [entry["id"] for entry in item["deliveries"] if entry["id"] != record["id"]])
        packaging = record.get("packaging") or {}
        if packaging.get("status") == "completed":
            selected_ids = {slide["output_id"] for slide in record["slides"]}
            for slide in item["slides"]:
                for output in slide.get("outputs", []):
                    if output["id"] in selected_ids:
                        output.setdefault("stages", {})["package"] = {**copy.deepcopy(packaging), "delivery_id": record["id"]}
                        generation = next((entry for entry in item.get("generations", []) if entry["id"] == output.get("generation_id")), None)
                        if generation:
                            generation.setdefault("stages", {}).setdefault(str(slide["number"]), {})["package"] = {
                                **copy.deepcopy(output["stages"]["package"]), "output_id": output["id"]}
        if not existing:
            item["updated_at"] = record["created_at"]
            item["history"].append({"at": item["updated_at"], "event": "delivery", "note": f"整理成品包 {record['id']}，共 {len(record['slides'])} 张"})
        elif cleanup["changed"]:
            item["updated_at"] = now()
            item["history"].append({"at": item["updated_at"], "event": "delivery_pruned",
                "note": f"沿用成品包 {record['id']}，清理其他历史成品包及领取记录"})
        return {"carousel": copy.deepcopy(item), "delivery": copy.deepcopy(record), **cleanup}

    def _carousel_collection(self, data, body, extra):
        item = self._find_carousel(data, body.get("carousel_id"))
        package = next((entry for entry in item.get("deliveries", []) if entry["id"] == body.get("delivery_id")), None)
        if not package or not isinstance(extra, dict) or not extra.get("collected_at"):
            raise VoiceError(404, "实际领取的成品包不存在。")
        package["collected_at"] = extra["collected_at"]
        package["download_count"] = package.get("download_count", 0) + 1
        package.setdefault("collection_history", []).append({"at": extra["collected_at"]})
        return {"carousel": copy.deepcopy(item), "delivery": copy.deepcopy(package)}

    def _carousel_publication(self, data, body, record):
        item = self._find_carousel(data, body.get("carousel_id"))
        if item["archived"]:
            raise VoiceError(409, "制作单已归档，请恢复后再登记发布。")
        if not isinstance(record, dict) or not PUBLICATION_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "发布信息未完成核验，未保存。")
        if record.get("expected_publication") != fingerprint(item.get("publication")):
            raise VoiceError(409, "发布记录已被另一操作修改，本次未覆盖，请核对最新登记。")
        package = next((entry for entry in item.get("deliveries", []) if entry["id"] == record.get("delivery_id")), None)
        previous = item.get("publication") or {}
        frozen = previous.get("delivery_snapshot") or {}
        if (not package and previous.get("delivery_id") == record.get("delivery_id") and
                frozen.get("id") == previous.get("delivery_id") and
                frozen.get("sha256") == previous.get("delivery_sha256")):
            package = frozen
        if not package or package.get("sha256") != record.get("delivery_sha256"):
            raise VoiceError(409, "实际发布的成品包不存在或已变化，请重新核对。")
        if any(record.get(key) != package.get(key) for key in ("script_id", "script_version", "selection_signature")):
            raise VoiceError(409, "发布登记的文案或成图版本与实际领取包不一致，请重新核对。")
        for other in data["carousels"]:
            if other["id"] != item["id"] and any(entry.get("post_id") == record.get("post_id") for entry in (other.get("publication"), *other.get("publication_history", [])) if entry):
                raise VoiceError(409, "帖子已绑定到另一制作单，请先核对原登记。")
        saved = copy.deepcopy(record)
        saved.pop("expected_publication", None)
        saved["delivery_snapshot"] = self._delivery_snapshot(package)
        item["publication"] = saved
        item.setdefault("publication_history", []).append(copy.deepcopy(saved))
        item["updated_at"] = saved["updated_at"]
        item["history"].append({"at": saved["updated_at"], "event": "publication", "note": f"登记图文帖子 {saved['post_id']}，成品包 {saved['delivery_id']}"})
        return {"carousel": copy.deepcopy(item), "publication": copy.deepcopy(saved)}

    def _carousel_calendar(self, data, body, extra):
        """Flip the publish-calendar mark of the current registration in place; no new registration is made."""
        item = self._find_carousel(data, body.get("carousel_id"))
        flag = body.get("calendar_uploaded")
        if type(flag) is not bool:
            raise VoiceError(400, "发布日历状态须为已上传或未上传。")
        publication = item.get("publication")
        if not publication or body.get("publication_id") != publication.get("id"):
            raise VoiceError(409, "发布记录已变化，请刷新后再标记发布日历。")
        if publication.get("calendar_uploaded") is not flag:
            stamp = now()
            for record in (publication, *(entry for entry in item.get("publication_history", []) if entry.get("id") == publication["id"])):
                record.update(calendar_uploaded=flag, calendar_updated_at=stamp)
        return {"carousel_id": item["id"], "publication_id": publication["id"], "calendar_uploaded": flag}
