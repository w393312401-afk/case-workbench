"""Workbench production API: pick a voiced variant, keep a voice library and generate timed voiceovers.

Network calls happen only on explicit requests and never while the workbench lock is held. Each
voiceover take stores its audio, the exact text sent and the timing of every meaning group.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from creation_store import CreationError
from direction_store import fingerprint, now, product_key
from elevenlabs_client import DEFAULT_MODEL, MODELS, VOICE_ID, VoiceError, clean_settings, model
from production_operations import ProductionOperations, durable_json
from production_store import FRAME_RATES, JOB_ID, TAKE_ID, apply_readings, supplement_markdown, take_quality, job_status
from task_activity import activity_label, record_activity, safe_error, settle_resolved_activity
from variant_parse import parse_variants, same_lines, spoken_lines, variant_script

MAX_LINES = 80
MAX_LINE_CHARS = 1000
MIN_COVERAGE = 0.6
MARKER_COLOR = "Blue"  # Red stays reserved for the editing skill's missing-footage markers.
VOICE_TRACK = 2  # Existing timelines keep source audio on A1 (stereo) and the voiceover on A2 (mono).
MAX_LEAD_SECONDS = 10
QUEUE_GRACE = 5  # Brief DaVinci holds (status reads, clip reveals) end before a build is reported as queued.
TIMELINE_WAIT = 150 * 60  # As long as an AI edit may hold DaVinci.
OPTION_KEYS = {"code", "name", "fps", "width", "height", "lead_seconds", "markers"}


class QueueCancelled(VoiceError):
    """A queued build the user withdrew before DaVinci was touched."""


def timeline_location(record):
    """Where a built timeline's DaVinci project lives: its creator's folder and the project id, when they were recorded.

    The same project name exists once per channel folder, so the name alone cannot say which project holds a timeline."""
    return {key: record[key] for key in ("creator", "folder", "project_id") if record.get(key)}


def creator_project_base(project, creator):
    """Keep a creator-prefixed rename in the same project family as its previous name."""
    name, prefix = str(project or ""), str(creator or "") + "-"
    return name[len(prefix):] if creator and name.casefold().startswith(prefix.casefold()) else name


def same_creator_project(first, second, creator):
    return creator_project_base(first, creator).casefold() == creator_project_base(second, creator).casefold()


def known_location(data, creator, project):
    """The folder and project id of the newest timeline built for this creator in a project of this name."""
    built = [item for job in data["jobs"] for item in job.get("timelines", [])
             if item.get("creator") == creator and same_creator_project(item.get("project"), project, creator) and item.get("project_id")]
    newest = max(built, key=lambda item: item.get("built_at", ""), default=None)
    return {key: newest[key] for key in ("folder", "project_id") if newest and newest.get(key)}


def timeline_options(value, config):
    """One build's overrides of the product's DaVinci settings; anything left out keeps the product setting."""
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - OPTION_KEYS:
        raise VoiceError(400, "建线选项格式无效。")
    code = value.get("code", config["code"])
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9]{1,20}", code):
        raise VoiceError(400, "时间线编号只能是 1–20 位字母或数字。")
    name = value.get("name", "")
    if not isinstance(name, str) or len(name.strip()) > 120 or any(ord(char) < 32 for char in name):
        raise VoiceError(400, "自定义时间线名称须是 120 字以内的一行文字。")
    fps = value.get("fps", config["fps"])
    if fps not in FRAME_RATES:
        raise VoiceError(400, "帧率无效。")
    size = [value.get(key, config[key]) for key in ("width", "height")]
    if any(type(item) is not int or not 16 <= item <= 8192 for item in size):
        raise VoiceError(400, "分辨率须是 16–8192 之间的整数。")
    lead = value.get("lead_seconds", 0)
    if type(lead) not in (int, float) or not math.isfinite(lead) or not 0 <= lead <= MAX_LEAD_SECONDS:
        raise VoiceError(400, f"配音前留白须在 0–{MAX_LEAD_SECONDS} 秒之间。")
    markers = value.get("markers", True)
    if type(markers) is not bool:
        raise VoiceError(400, "意群标记选项无效。")
    numerator, denominator = FRAME_RATES[fps]
    return {"code": code, "name": name.strip(), "fps": fps, "width": size[0], "height": size[1],
            "lead_frames": int(round(lead * numerator / denominator)), "markers": markers}


def _triples_tts(alignment):
    if not isinstance(alignment, dict):
        return []
    chars, starts, ends = (alignment.get(key) for key in ("characters", "character_start_times_seconds", "character_end_times_seconds"))
    if not all(isinstance(value, list) for value in (chars, starts, ends)) or not len(chars) == len(starts) == len(ends):
        return []
    return [(str(char), float(start), float(end)) for char, start, end in zip(chars, starts, ends)]


def _triples_forced(body):
    items = body.get("characters") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return []
    return [(str(item.get("text", "")), float(item.get("start", 0)), float(item.get("end", 0))) for item in items if isinstance(item, dict)]


def map_groups(lines, triples):
    """Assign each line the time span of its characters, matched in order and ignoring spaces and case."""
    target = [(char.lower(), number) for number, line in enumerate(lines) for char in line if not char.isspace()]
    source = [(char.lower(), start, end) for text, start, end in triples for char in text if not char.isspace()]
    matcher = difflib.SequenceMatcher(None, [item[0] for item in target], [item[0] for item in source], autojunk=False)
    spans = [[None, None, 0] for _ in lines]
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            number = target[block.a + offset][1]
            _, start, end = source[block.b + offset]
            span = spans[number]
            span[0] = start if span[0] is None else min(span[0], start)
            span[1] = end if span[1] is None else max(span[1], end)
            span[2] += 1
    totals = [sum(1 for char in line if not char.isspace()) for line in lines]
    return [{"number": number + 1, "start": None if span[0] is None else round(span[0], 3),
             "end": None if span[1] is None else round(span[1], 3),
             "coverage": round(span[2] / totals[number], 3) if totals[number] else 0.0}
            for number, span in enumerate(spans)]


def _short(value, limit):
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def meaning_markers(job, take, fps, legacy=False, lead_frames=0):
    """One range marker per meaning group, tiling the voiceover: [this group's start, next group's start).

    The first marker starts at frame 0 so the opening shot is covered, including any `lead_frames` of silence
    placed before the voiceover; pauses after a line belong to that line.
    The note carries only the bare Chinese line and what the shot must prove; legacy=True rebuilds the fuller note
    that timelines built before 2026-10-12 carry.
    """
    if not take_quality(take, len(job.get("lines") or [])):
        raise VoiceError(409, "这版配音的意群对齐未通过核验，请先核对配音后再建时间线。")
    numerator, denominator = FRAME_RATES[fps]
    rate = numerator / denominator
    groups = take["groups"]
    starts = [int(round(group["start"] * rate)) + lead_frames for group in groups]
    starts[0] = 0
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1] + 1)
    seconds = take.get("duration") or max(group["end"] for group in groups)
    ends = starts[1:] + [max(lead_frames + int(math.floor(seconds * rate)), starts[-1] + 1)]
    markers = []
    for line, group, start, end in zip(job["lines"], groups, starts, ends):
        proof = line.get("proof") or "待补"
        note = ([f"口播：{line['spoken']}", f"中文：{line['zh']}" if line.get("zh") else "", f"画面要证明什么：{proof}",
                 f"素材标签：{line['tags']}" if line.get("tags") else "", f"证据：{line['evidence']}" if line.get("evidence") else "",
                 f"配音：{group['start']:.2f}–{group['end']:.2f} 秒；标记 [{start}, {end}) 帧，含句后停顿"] if legacy
                else [line.get("zh") or "", f"画面表达：{proof}"])
        markers.append({"number": line["number"], "frame": start, "duration": end - start,
                        "name": f"#{line['number']} {_short(line['spoken'], 36)}", "note": ("\n" if legacy else " ").join(item for item in note if item),
                        "custom": f"workbench-group:{job['id']}:{line['number']}"})
    return markers


def _complete(groups):
    return take_quality({"groups": groups}, len(groups))


def _media_tool(name):
    found = shutil.which(name)
    if found:
        return found
    ffmpeg = shutil.which("ffmpeg")
    return str(Path(ffmpeg).with_name(name + Path(ffmpeg).suffix)) if ffmpeg and Path(ffmpeg).with_name(name + Path(ffmpeg).suffix).is_file() else None


def convert_wav(source, target):
    """48 kHz 16-bit WAV for DaVinci Resolve; returns (written, duration seconds or None)."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    ffmpeg, ffprobe = _media_tool("ffmpeg"), _media_tool("ffprobe")
    written = False
    if ffmpeg:
        try:
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(source),
                            "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(target)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, creationflags=flags, check=True)
            written = Path(target).is_file() and Path(target).stat().st_size > 44
        except (OSError, subprocess.SubprocessError):
            written = False
    duration = None
    if ffprobe:
        try:
            probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(target if written else source)], capture_output=True, text=True, timeout=30, creationflags=flags)
            duration = round(float(probe.stdout.strip()), 3)
        except (OSError, ValueError, subprocess.SubprocessError):
            duration = None
    return written, duration


class ProductionWorkbenchMixin:
    """Needs self.production, self.voices, self.creation, self.ai and self.lock from the workbench."""

    def _voicing(self):
        if not hasattr(self, "_voicing_jobs"):
            self._voicing_jobs = set()
        return self._voicing_jobs

    def _voice_live(self):
        if not hasattr(self, "_voicing_progress"):
            self._voicing_progress = {}
        return self._voicing_progress

    def _timeline_live(self):
        """Job id → the build waiting for or driving DaVinci; one per job, as the job's file lock allows."""
        if not hasattr(self, "_timeline_building"):
            self._timeline_building = {}
        return self._timeline_building

    def _timeline_state(self, job, recovery, detail=False):
        """Call with self.lock held. Only a build that may have changed DaVinci is a recovery case: a queued one is
        shown as waiting, and one that ended before the worker journaled anything is the last unfinished build."""
        live = self._timeline_live().get(job["id"])
        job["timeline_building"] = {key: value for key, value in live.items() if key != "cancel"} if live else None
        recovery = [item for item in recovery if item["kind"] != "timeline" or
                    (item.get("has_journal") and not (live and item["request_id"] == live["request_id"]))]
        if not detail:
            return recovery
        failure = self._production_operations().latest(job["id"], "timeline")
        built = max((item.get("built_at") or "" for item in job.get("timelines", [])), default="")
        ended = failure and (failure.get("state") == "safe_failed" or (failure.get("state") != "committed" and
            not (live and failure["request_id"] == live["request_id"]) and
            not self._production_operations().recovery_file(failure).exists()))
        job["timeline_failure"] = ({"request_id": failure["request_id"], "at": failure["updated_at"],
                                    "error": failure.get("error", ""), "cancelled": bool(failure.get("cancelled"))}
                                   if ended and failure["updated_at"] > built else None)
        return recovery

    def _voice_progress(self, identifier, stage, message):
        with self.lock:
            current = self._voice_live().get(identifier)
            if current is not None:
                current.update(stage=stage, progress=message, updated_at=now())

    def _production_operations(self):
        operations = getattr(self, "_operations_reader", None)
        if operations is None:
            operations = self._operations_reader = ProductionOperations(self.production.root, on_saved=self._production_operation_activity)
        return operations

    def _production_operation_activity(self, record, *, pending=False):
        """Mirror only durable phase boundaries, never provider responses or prompts."""
        try:
            kind = "voice" if record["kind"] == "take" else "timeline"
            job = self.production.job(record["job_id"])
            run_id = f"{kind}:{job['id']}:{record['request_id']}"
            state = record["state"]
            phases = {"prepared": "已准备", "inflight": "正在生成配音" if kind == "voice" else "正在建立时间线",
                      "response_saved": "配音已返回，正在对齐", "result_ready": "结果已保存，正在登记",
                      "committed": "已登记", "safe_failed": "执行失败", "uncertain": "待核实外部结果"}
            status = "queued" if kind == "timeline" and state == "prepared" else "running"
            message = phases.get(state, "正在处理")
            if state == "committed":
                result = record.get("result") or {}
                attention = (result.get("quality", {}).get("status") != "passed" if kind == "voice" else
                             bool(result.get("warnings")) or result.get("saved") is not True)
                status = "needs_attention" if attention else "completed"
                message = ("配音已生成，意群对齐待核对" if attention else "配音已生成") if kind == "voice" else (
                          "时间线已建立，待核对工程" if attention else "时间线已建立")
            elif state == "safe_failed" and record.get("cancelled"):
                status, message = "cancelled", record.get("error") or "已取消，达芬奇里没有改动。"
            elif state == "safe_failed":
                status, message = "failed", ("配音请求未完成。" + safe_error(record.get("error", "")) if kind == "voice" else
                                             "建线没有完成，达芬奇里没有改动：" + _short(record.get("error") or "原因未知", 200))
            elif state == "uncertain":
                status, message = "needs_attention", "外部结果待核实，请恢复原请求"
            if pending:
                status, message = "needs_attention", "结果已取得，资料尚未登记；请恢复原请求"
            record_activity(self, run_id, kind=kind, owner_id=job["id"], product=job.get("product", ""),
                            case_id=job.get("case_id"), title=("生成配音" if kind == "voice" else "建立时间线") + " · " + activity_label(job),
                            source_id=record.get("take_id") or (record.get("result") or {}).get("unique_id") or record["request_id"],
                            step=kind, status=status, phase="已取消" if status == "cancelled" else phases.get(state, "处理中"), message=message,
                            result_count=1 if state == "committed" else 0,
                            event_key=f"{state}:{'pending' if pending else status}" if status not in {"queued", "running"} else None)
        except Exception:
            pass

    def recover_production_operation(self, identifier, body):
        operations = self._production_operations()
        original = operations.recovery_body(identifier, body)
        if body.get("decision") is not None:
            if body.get("decision") != "not_generated" or body.get("confirmed") is not True or body["kind"] != "take":
                raise VoiceError(400, "请先核实这次配音确实没有生成，并明确确认后再恢复。")
            with operations.guard("take", original) as record:
                if not record or record.get("state") not in {"inflight", "uncertain"} or record.get("result"):
                    raise VoiceError(409, "这项配音已有结果或不处于未知状态，请恢复现有结果，不能重新生成。")
                if operations.speech_response(record):
                    raise VoiceError(409, "已保存这次配音的返回结果，请恢复现有结果，不能重新生成。")
                folder = self.production.audio / identifier / record["take_id"]
                if any(path.is_file() and path.stat().st_size > 0 for path in
                       (folder / "voiceover.mp3", folder / "voiceover.wav")):
                    raise VoiceError(409, "已找到这次配音的音频，请先核对并恢复或导入该音频，不能按未生成重新发送。")
                history = list(record.get("manual_resolution_history") or [])
                history.append({"at": now(), "decision": "not_generated", "confirmed": True,
                                "original_state": record["state"], "original_error": record.get("error", "")})
                operations.update(record, "prepared", manual_resolution_history=history, error="")
        return (self.generate_take(identifier, original) if body["kind"] == "take" else
                self.build_timeline(identifier, original))

    def _commit_production_operation(self, operations, record):
        # Result is already durable. A catalog write failure leaves it available for a restart/retry.
        try:
            result = self.production.mutate(record["kind"], record["body"], record["result"])
            operations.update(record, "committed", error="")
        except OSError as exc:
            self._production_operation_activity(record, pending=True)
            raise VoiceError(503, "外部制作结果已经保存，资料登记未完成；请用原请求恢复，无需重新生成配音或另建时间线。") from exc
        return result

    def production_catalog(self, product=None):
        result = self.production.public(product)
        result["models"] = copy.deepcopy(MODELS)
        result["default_model"] = DEFAULT_MODEL
        recoveries = {}
        if result["jobs"]:
            for operation in self._production_operations().public():
                recoveries.setdefault(operation["job_id"], []).append(operation)
        for job in result["jobs"]:
            job["voicing"] = job["id"] in self._voicing()
            job["voicing_live"] = copy.deepcopy(self._voice_live().get(job["id"]))
            with self.lock:
                job["recovery_operations"] = self._timeline_state(job, recoveries.get(job["id"], []))
            self._production_edit_state(job)
        for item in result.get("carousels", []):
            with self.lock:
                item["clip_picking_live"] = copy.deepcopy(getattr(self, "_clip_ai_live", {}).get(item["id"]))
            if item.get("archived"):
                continue
            # Resolve persisted running records after a restart. Idle image and
            # package integrity is checked by the separate deliveries read, so
            # ordinary list reads need not scan every finished image twice.
            if hasattr(self, "carousel_live") and any(run.get("status") == "running" for run in item.get("searches", [])):
                self.carousel_live(item)
            running_images = any(run.get("status") == "running" for run in item.get("generations", []))
            running_reviews = any(run.get("status") == "running" for slide in item.get("slides", [])
                                  for output in slide.get("outputs", []) for run in output.get("review_runs", []))
            if hasattr(self, "carousel_images_live") and (running_images or running_reviews):
                self.carousel_images_live(item)
        return self.attach_content_publications(result) if hasattr(self, "attach_content_publications") else result

    def _production_edit_state(self, job):
        """Show ended-but-unsaved edits as recoverable, instead of an endless running task."""
        for edit in job.get("edits", []):
            pending = self.edit_pending(edit["id"]) if hasattr(self, "edit_pending") else None
            if pending:
                edit.update(pending)
                edit["result_pending"] = True
            if edit.get("status") == "running":
                live = self.edit_live(edit["id"]) if hasattr(self, "edit_live") else None
                if live:
                    edit["live"] = live
                else:
                    edit.update(status="interrupted", error=("等待原建线时任务已中断，AI 剪辑尚未开始；请先恢复原建线，再重新提交剪辑。"
                        if edit.get("waiting_timeline") else "这次剪辑的进程已不在运行，结果可能不完整；请先到达芬奇里核对并重新核验。"))
        job["status"] = job_status(job)

    def _tts(self, job, readings):
        lines, count = apply_readings([line["spoken"] for line in job["lines"]], readings)
        joined = "\n".join(lines)
        return {"lines": lines, "text": joined, "characters": len(joined), "substitutions": count}

    def production_job(self, identifier):
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(404, "这张制作单不存在。")
        self._settle_job_fill(identifier)
        with self.lock:
            catalog = self.production.public()
            job = next((item for item in catalog["jobs"] if item["id"] == identifier), None)
            if not job:
                raise VoiceError(404, "这张制作单不存在。")
            readings = [item for item in catalog["readings"] if item["product"] == job["product"]]
            try:
                full = self.creation.get_script(job["script_id"])
                script = {key: full.get(key) for key in ("id", "title", "status", "version", "product", "direction_id")}
                index = (job.get("source") or {}).get("variant_index")
                candidate = (full.get("source") or {}).get("candidate_text") or ""
                chosen = next((item for item in parse_variants(candidate) if type(index) is int and item["index"] == index), None)
                # A derived script keeps its candidate provenance, but the production order uses its own frozen words.
                matching = chosen and same_lines([line["spoken"] for line in chosen["lines"]],
                                                 [line["spoken"] for line in job["lines"]])
                markdown = job.get("fill_markdown") or (variant_script(candidate, index) if matching else "")
                manual = not markdown
                if manual and (job.get("supplement") or candidate):
                    markdown = supplement_markdown(job)
                variant = {"label": job.get("variant_label", ""), "markdown": markdown, "manual": manual} if markdown else None
            except CreationError:
                script, variant = None, None
            job["voicing"] = identifier in self._voicing()
            job["voicing_live"] = copy.deepcopy(self._voice_live().get(identifier))
            recovery = self._timeline_state(job, self._production_operations().public(identifier), detail=True)
            job["recovery_operations"] = recovery
            job.setdefault("timelines", [])
            self._production_edit_state(job)
            settle_resolved_activity(self, "job", job)
            return {"job": job, "revision": catalog["revision"], "readings": readings, "tts": self._tts(job, readings),
                    "historical_baseline_restore": True,
                    "recovery_operations": recovery,
                    "script": script, "variant": variant, "voices": catalog["voices"], "defaults": catalog["defaults"],
                    "models": copy.deepcopy(MODELS), "default_model": DEFAULT_MODEL, "creators": catalog["settings"]["creators"],
                    "product_settings": self.production.product_settings(catalog, job["product"]),
                    "today": datetime.now().strftime("%y%m%d")}

    def production_variants(self, task_id, round_id):
        task = self.ai.get(task_id)
        turn = next((item for item in task.get("rounds", []) if item.get("round_id") == round_id), None)
        if not turn:
            raise VoiceError(404, "指定候选轮次不存在。")
        if turn.get("status") != "completed" or not str(turn.get("output") or "").strip():
            raise VoiceError(409, "只有完整完成的候选才可以制作视频。")
        variants = parse_variants(turn["output"])
        if not variants:
            raise VoiceError(409, "这一轮候选里没有识别到可配音的口播：既没有「口播（配音用）」代码块，也没有可识别的 English/口播 段落。")
        return {"task_id": task_id, "round_id": round_id, "task_revision": task.get("revision"), "variants": variants}

    def eleven_status(self):
        try:
            return {"ok": True, **self.voices.subscription()}
        except VoiceError as exc:
            return {"ok": False, "error": str(exc)}

    def save_voice(self, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "音色参数格式无效。")
        prior = self.production.replay("voice", body)
        if prior:
            return {**prior, "catalog": self.production_catalog()}
        value = body.get("voice") if isinstance(body.get("voice"), dict) else {}
        lookup = None
        if body.get("operation", "save") == "save":
            voice_id = value.get("voice_id")
            if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
                raise VoiceError(400, "音色 ID 格式无效。")
            known = self.production.voice(voice_id)
            if not known or not known.get("name") or body.get("refresh") is True:
                lookup = self.voices.voice(voice_id)
        result = self.production.mutate("voice", body, lookup)
        return {**result, "catalog": self.production_catalog()}

    def save_reading(self, body):
        result = self.production.mutate("reading", body)
        return {**result, "catalog": self.production_catalog()}

    def create_production_job(self, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "制作单参数格式无效。")
        prior = self.production.replay("job", body)
        if prior:
            return prior
        value = body.get("job") if isinstance(body.get("job"), dict) else {}
        with self.lock:
            script = self.creation.get_script(str(value.get("script_id") or ""))
            lines = spoken_lines(script["text"])
            if not lines:
                raise VoiceError(400, "脚本正文没有可配音的口播行。")
            if len(lines) > MAX_LINES or any(len(line) > MAX_LINE_CHARS for line in lines):
                raise VoiceError(400, f"口播超过 {MAX_LINES} 行或单行超过 {MAX_LINE_CHARS} 字，请先核对正文。")
            source = script.get("source") or {}
            variants = parse_variants(source.get("candidate_text") or "")
            index = source["variant_index"] if "variant_index" in source else value.get("variant_index")
            hinted = next((item for item in variants if type(index) is int and item["index"] == index), None)
            matches = lambda item: same_lines([line["spoken"] for line in item["lines"]], lines)
            chosen = hinted if hinted and matches(hinted) else None
            if chosen is None and "variant_index" not in source:
                chosen = next((item for item in variants if matches(item)), None)
            chinese = [line.strip() for line in str(script.get("translation_zh") or "").splitlines() if line.strip()]
            if chosen:
                records, problems, label = copy.deepcopy(chosen["lines"]), chosen["problems"], chosen["label"]
                # New selected variants preserve an explicit empty translation; old blank scripts keep their candidate Chinese.
                if "variant_index" in source or chinese:
                    for position, record in enumerate(records):
                        record["zh"] = chinese[position] if len(chinese) == len(records) else ""
            else:
                records = [{"number": number, "spoken": line, "zh": chinese[number - 1] if len(chinese) == len(lines) else "",
                            "proof": "", "tags": "", "evidence": ""} for number, line in enumerate(lines, 1)]
                problems = ["口播行与所选变体的意群表不一致，未沿用原画面要求，请在制作单里补填。" if "variant_index" in source else
                            "来源候选里没有逐行一致的意群表，画面要求请在制作单里补填。" if variants else
                            "这条脚本没有附带意群表，画面要求请在制作单里补填。"]
                label = ""
            resolved = {"product": product_key(script["product"]), "direction_id": script.get("direction_id", ""),
                        "script_id": script["id"], "script_version": script["version"], "script_family": script["family_id"],
                        "title": script["title"], "variant_label": label, "lines": copy.deepcopy(records), "problems": problems,
                        "source": {"task_id": source.get("task_id", ""), "round_id": source.get("round_id", ""),
                                   "variant_index": source["variant_index"] if "variant_index" in source else chosen["index"] if chosen else None}}
            return self.production.mutate("job", body, resolved)

    def _settle_job_fill(self, identifier):
        """A fill round that ended while nobody was listening (e.g. across a restart) is written back on the next read."""
        try:
            fill = self.production.job(identifier).get("ai_fill") or {}
        except VoiceError:
            return
        if fill.get("status") != "running":
            return
        try:
            status = self.ai.get(fill["task_id"])["status"]
        except Exception:
            status = "missing"
        if status == "missing":
            self.production.mutate("job_fill_finish", {"request_id": "fill-" + uuid4().hex, "job_id": identifier,
                                                       "task_id": fill["task_id"], "error": "找不到这轮写稿任务，可以再点一次补全。"})
        elif status not in ("queued", "running"):
            self._job_fill_finished(fill["task_id"])

    def save_job_lines(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "意群表参数格式无效。")
        return self.production.mutate("job_lines", {**body, "job_id": identifier})

    def archive_job(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "归档参数格式无效。")
        result = self.production.mutate("job_archive", {**body, "job_id": identifier})
        activity = getattr(self, "activity", None)
        if activity is not None:
            activity.archive_owner(identifier, result["job"].get("archived", False))
        return result

    def select_take(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "选用参数格式无效。")
        return self.production.mutate("select_take", {**body, "job_id": identifier})

    def archive_timeline(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "时间线归档参数格式无效。")
        return self.production.mutate("timeline_archive", {**body, "job_id": identifier})

    def set_timeline_task_status(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "时间线任务状态参数格式无效。")
        return self.production.mutate("timeline_task_status", {**body, "job_id": identifier})

    def complete_production_job(self, identifier, body):
        """Record manual step-four success without launching or fabricating an AI edit."""
        if not isinstance(body, dict):
            raise VoiceError(400, "成功标记参数格式无效。")
        return self.production.mutate("job_completion", {**body, "job_id": identifier})

    def _forced_alignment(self, lines, audio_path):
        """Cache the external alignment response; an unknown attempt is never billed again."""
        audio = audio_path.read_bytes()
        request_hash = fingerprint(["forced_alignment", lines, hashlib.sha256(audio).hexdigest()])
        path = audio_path.parent / "forced_alignment.json"
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("request_hash") != request_hash:
                    raise ValueError("alignment input changed")
                if saved.get("state") == "completed" and isinstance(saved.get("response"), dict):
                    return saved["response"]
            except (OSError, ValueError, AttributeError) as exc:
                raise VoiceError(503, "已保存的强制对齐记录无法核对，未再次发送对齐请求，请先核实。") from exc
            raise VoiceError(503, "已有强制对齐请求的结果不确定，未再次发送以免重复计费，请先核实。")
        attempt = {"schema_version": 1, "request_hash": request_hash, "state": "inflight", "created_at": now()}
        durable_json(path, attempt)
        result = self.voices.align(audio, audio_path.name, "\n".join(lines))
        durable_json(path, {**attempt, "state": "completed", "response": result, "completed_at": now()})
        return result

    def _timing(self, lines, result, audio_path):
        warnings = []
        for source, key in (("tts", "alignment"), ("tts_normalized", "normalized_alignment")):
            try:
                triples = _triples_tts(result.get(key))
                if triples:
                    groups = map_groups(lines, triples)
                    if _complete(groups):
                        return groups, source, warnings
            except (TypeError, ValueError, OverflowError):
                continue
        try:
            groups = map_groups(lines, _triples_forced(self._forced_alignment(lines, audio_path)))
            if _complete(groups):
                return groups, "forced", warnings
            if any(value is not None and not math.isfinite(value)
                   for group in groups for value in (group["start"], group["end"], group["coverage"])):
                raise ValueError("意群时间包含无效数字")
            warnings.append("强制对齐后仍有意群没有对上，请试听核对。")
            return groups, "forced", warnings
        except (VoiceError, OSError, TypeError, ValueError, OverflowError) as exc:
            warnings.append(f"没有拿到意群时间：{exc}")
            return [{"number": number, "start": None, "end": None, "coverage": 0.0} for number in range(1, len(lines) + 1)], "", warnings

    def generate_take(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "配音参数格式无效。")
        body = {**body, "job_id": identifier}
        operations = self._production_operations()
        operations.validate("take", body)
        voice_id, model_id = body.get("voice_id"), body.get("model_id", DEFAULT_MODEL)
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise VoiceError(400, "音色 ID 格式无效。")
        settings = clean_settings(model_id, body.get("settings"))
        with operations.guard("take", body) as record:
            prior = self.production.replay("take", body)
            if prior:
                if record and record.get("state") != "committed":
                    operations.update(record, "committed", error="")
                return prior
            if record and record.get("state") in {"result_ready", "committed"} and record.get("result"):
                return self._commit_production_operation(operations, record)
            if not record:
                with self.lock:
                    job = self.production.job(identifier)
                    if job["archived"]:
                        raise VoiceError(409, "制作单已归档，请恢复后再生成配音。")
                    tts = self._tts(job, self.production.readings(job["product"]))
                    if tts["characters"] > model(model_id)["max_chars"]:
                        raise VoiceError(400, "口播超过所选模型单次可配音的字数。")
                    known = self.production.voice(voice_id)
                record = operations.prepare("take", body, {"tts": tts, "voice_id": voice_id, "model_id": model_id,
                    "settings": settings, "known_voice": known}, take_id="TAK-" + uuid4().hex[:12])
            snapshot = record["snapshot"]
            tts, known = snapshot["tts"], snapshot.get("known_voice")
            voice_id, model_id, settings = snapshot["voice_id"], snapshot["model_id"], snapshot["settings"]
            take_id = record["take_id"]
            folder = self.production.audio / identifier / take_id
            mp3 = folder / "voiceover.mp3"
            response = operations.speech_response(record)
            if not response and record.get("state") in {"inflight", "uncertain", "response_saved"}:
                record = operations.best_effort_uncertain(record, record.get("error") or "没有找到已持久保存的配音返回结果。")
                raise VoiceError(503, "这次配音的结果不确定，请先核对 ElevenLabs 的生成记录或已保存音频；工作台不会再次发送该请求，以免重复扣费。")
            with self.lock:
                self._voicing().add(identifier)
                self._voice_live()[identifier] = {"started_at": now(), "stage": "request", "progress": "正在请求配音", "updated_at": now()}
            try:
                if response:
                    self._production_operation_activity(record)
                if not response:
                    current = self.production.job(identifier)
                    if current["archived"]:
                        raise VoiceError(409, "制作单已归档，请恢复后再继续生成配音。")
                    try:
                        lookup = None if known and known.get("name") else self.voices.voice(voice_id)
                        # Validate credentials before the durable boundary which forbids resending speech.
                        if hasattr(self.voices, "key"):
                            self.voices.key()
                    except Exception as exc:
                        operations.update(record, "safe_failed", error=str(exc)[:1000])
                        raise
                    record = operations.update(record, "inflight", lookup=lookup, error="")
                    try:
                        result = self.voices.speech(voice_id, tts["text"], model_id, settings)
                    except VoiceError as exc:
                        # Only an explicit provider refusal or a pre-call credential failure permits replay.
                        if exc.retry_safe:
                            operations.update(record, "safe_failed", error=str(exc)[:1000])
                            raise
                        operations.best_effort_uncertain(record, exc)
                        raise VoiceError(503, "配音请求已发出，但没有拿到完整返回结果。请先核对 ElevenLabs 的生成记录；工作台不会自动重发，以免重复扣费。") from exc
                    except Exception as exc:
                        operations.best_effort_uncertain(record, exc)
                        raise VoiceError(503, "配音请求已发出，但没有拿到完整返回结果。请先核对 ElevenLabs 的生成记录；工作台不会自动重发，以免重复扣费。") from exc
                    try:
                        record = operations.save_speech_response(record, result, mp3)
                    except Exception as exc:
                        operations.best_effort_uncertain(record, exc)
                        raise VoiceError(503, "配音请求已发出，但没有完整保存返回结果。请先核对 ElevenLabs 的生成记录；工作台不会自动重发，以免重复扣费。") from exc
                    response = operations.speech_response(record)
                self._voice_progress(identifier, "alignment", "已取得音频，正在对齐意群")
                groups, alignment_source, warnings = self._timing(tts["lines"], response, mp3)
                self._voice_progress(identifier, "saving", "正在保存音频和配音时间")
                written, duration = convert_wav(mp3, folder / "voiceover.wav")
                if not written:
                    warnings.append("没有转出 WAV（需要本机 ffmpeg），只保存了 MP3。")
                if duration is None:
                    ends = [group["end"] for group in groups if group["end"] is not None and math.isfinite(group["end"])]
                    duration = max(ends) if ends else None
                durable_json(folder / "alignment.json", {"alignment": response.get("alignment"),
                    "normalized_alignment": response.get("normalized_alignment"), "groups": groups})
                take = {"id": take_id, "voice_id": voice_id, "model_id": model_id, "settings": settings, "tts_lines": tts["lines"],
                        "tts_sha256": hashlib.sha256(tts["text"].encode()).hexdigest(), "characters": tts["characters"],
                        "request_id": response["request_id"], "audio": f"{identifier}/{take_id}/voiceover.mp3",
                        "wav": f"{identifier}/{take_id}/voiceover.wav" if written else "", "duration": duration,
                        "alignment_source": alignment_source, "groups": groups, "warnings": warnings,
                        "created_at": now(), "lookup": record.get("lookup")}
                quality_passed = take_quality(take, len(tts["lines"]))
                take["quality"] = {"status": "passed" if quality_passed else "failed",
                                   "min_coverage": MIN_COVERAGE,
                                   "message": "意群对齐已通过核验。" if quality_passed else
                                              "意群对齐未通过核验，请试听核对；暂不能选用或建线。"}
                record = operations.update(record, "result_ready", result=take, error="")
                return self._commit_production_operation(operations, record)
            except Exception:
                try:
                    latest = operations.read("take", body) or record
                    self._production_operation_activity(latest, pending=latest.get("state") in {"response_saved", "result_ready"})
                except Exception:
                    pass
                raise
            finally:
                with self.lock:
                    self._voicing().discard(identifier)
                    self._voice_live().pop(identifier, None)

    def production_audio(self, identifier, take_id, kind):
        if not JOB_ID.fullmatch(identifier) or not TAKE_ID.fullmatch(take_id) or kind not in {"mp3", "wav"}:
            raise VoiceError(404, "配音文件不存在。")
        path = (self.production.audio / identifier / take_id / f"voiceover.{kind}").resolve()
        if not path.is_relative_to(self.production.audio.resolve()) or not path.is_file():
            raise VoiceError(404, "配音文件不存在。")
        return path

    def resolve_status(self):
        # A closed DaVinci answers at once; asking its scripting API would first wait several seconds.
        if self.resolve.closed():
            return {"ok": False, "busy": self.resolve.busy(), "error": "达芬奇没有打开：请先打开 DaVinci Resolve，再回来刷新。"}
        try:
            return {"ok": True, **{key: value for key, value in self.resolve.run("status", timeout=30).items() if key != "ok"}}
        except VoiceError as exc:
            return {"ok": False, "busy": self.resolve.busy(), "error": str(exc)}

    def save_production_settings(self, body):
        result = self.production.mutate("settings", body)
        return {**result, "catalog": self.production_catalog()}

    def build_timeline(self, identifier, body):
        """Build on a worker thread. The reply is the built timeline, or a queued notice when DaVinci stays busy;
        a queued build then waits its turn with AI edits and records its outcome in the task activity."""
        if not isinstance(body, dict):
            raise VoiceError(400, "时间线参数格式无效。")
        body = {**body, "job_id": identifier}
        self._production_operations().validate("timeline", body)
        outcome, reported = {}, threading.Event()

        def report(**value):
            if not reported.is_set():
                outcome.update(value)
                reported.set()

        def run():
            try:
                report(result=self._build_timeline(body, report))
            except BaseException as exc:
                report(error=exc)

        threading.Thread(target=run, name=f"timeline-{identifier}", daemon=True).start()
        reported.wait()
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]

    def cancel_timeline(self, identifier, body):
        """Withdraw a build still waiting for DaVinci; one already running is left to finish."""
        request_id = body.get("request_id") if isinstance(body, dict) else None
        with self.lock:
            live = self._timeline_live().get(identifier)
            if not live or (request_id is not None and request_id != live["request_id"]):
                raise VoiceError(404, "这张制作单没有排队中的建线。")
            if live["stage"] != "waiting":
                raise VoiceError(409, "建线已经开始，不能取消；建好后如不需要，可在达芬奇里删除这条时间线。")
            live["cancel"].set()
        return {"ok": True}

    @contextmanager
    def _timeline_turn(self, identifier, request_id, creator, report, snapshot=None):
        """Wait for DaVinci in the same first-come line as AI edits, then hold it for the build."""
        lock = getattr(self.resolve, "lock", None)
        cancel, ticket, acquired, queued = threading.Event(), object(), False, False
        live = {"request_id": request_id, "creator": creator, "stage": "waiting", "since": now(), "cancel": cancel}
        if snapshot:
            payload = snapshot["payload"]
            live.update(take_id=snapshot["take_id"], name=payload.get("name") or payload["prefix"] + "序号",
                        group_markers=bool(snapshot["markers"]), project=snapshot["config"]["project"],
                        materials=list(snapshot.get("materials") or snapshot["config"].get("material_timelines") or []))
        with self.lock:
            self._timeline_live()[identifier] = live
            if not hasattr(self, "_edit_resolve_waiters"):
                self._edit_resolve_waiters = []
            if lock is not None:
                self._edit_resolve_waiters.append(ticket)
        started = time.monotonic()
        try:
            while lock is not None:
                with self.lock:
                    if cancel.is_set():
                        raise QueueCancelled(409, "已取消排队，没有建时间线，达芬奇里没有改动。")
                    if self._edit_resolve_waiters[0] is ticket and lock.acquire(blocking=False):
                        acquired = True
                        self._edit_resolve_waiters.pop(0)
                        live["stage"] = "building"
                        break
                waited = time.monotonic() - started
                if waited >= TIMELINE_WAIT:
                    raise VoiceError(409, "等待达芬奇超时，没有建时间线；请在达芬奇空闲后重新建线。")
                if not queued and waited >= QUEUE_GRACE:
                    queued = True
                    message = "建线已排队，达芬奇空闲后自动开始；现在可以先提交剪辑，建好后自动接着剪。" if live.get("group_markers") else "建线已排队，达芬奇空闲后自动开始。"
                    record_activity(self, f"timeline:{identifier}:{request_id}", status="queued", phase="等待达芬奇", message=message)
                    report(result={"queued": True, "request_id": request_id, "message": message,
                                   "can_submit_edit": live.get("group_markers") is True})
                cancel.wait(.2)
            with self.lock:
                live["stage"] = "building"
            yield
        finally:
            if acquired:
                lock.release()
            with self.lock:
                if ticket in self._edit_resolve_waiters:
                    self._edit_resolve_waiters.remove(ticket)
                if self._timeline_live().get(identifier) is live:
                    self._timeline_live().pop(identifier)

    def _build_timeline(self, body, report):
        identifier = body["job_id"]
        operations = self._production_operations()
        with operations.guard("timeline", body) as operation:
            prior = self.production.replay("timeline", body)
            if prior:
                if operation and operation.get("state") != "committed":
                    operations.update(operation, "committed", error="")
                return prior
            if operation and operation.get("state") in {"result_ready", "committed"} and operation.get("result"):
                return self._commit_production_operation(operations, operation)
            if not operation:
                with self.lock:
                    data = self.production.records()
                    job = next((item for item in data["jobs"] if item["id"] == identifier), None)
                    if not job:
                        raise VoiceError(404, "这张制作单不存在。")
                    if job["archived"]:
                        raise VoiceError(409, "制作单已归档，请恢复后再建时间线。")
                    take = next((item for item in job["takes"] if item["id"] == job["selected_take"]), None)
                    if not take:
                        raise VoiceError(409, "请先选用一版配音，再建时间线。")
                    if not take_quality(take, len(job["lines"])):
                        raise VoiceError(409, "这版配音的意群对齐未通过核验，请先核对配音后再建时间线。")
                    creator = body.get("creator")
                    if creator not in data["settings"]["creators"]:
                        raise VoiceError(400, "请选择达人；达人名在「制作视频」页的达芬奇设置里维护。")
                    config = self.production.product_settings(data, job["product"])
                    options = timeline_options(body.get("options"), config)
                    audio = self.production.audio / (take["wav"] or take["audio"])
                    if not audio.is_file():
                        raise VoiceError(409, "配音文件不见了，请重新生成配音。")
                    markers = (meaning_markers(job, take, options["fps"], lead_frames=options["lead_frames"])
                               if options["markers"] else [])
                    prefix = f"{creator}{datetime.now().strftime('%y%m%d')}-{options['code']}-"
                    payload = {"project": config["project"], "creator": creator, **known_location(data, creator, config["project"]),
                        "audio": str(audio), "prefix": prefix, "clip_suffix": "_配音",
                        "audio_bin": config["audio_bin"], "timeline_bin": config["timeline_bin"], "fps": options["fps"],
                        "width": options["width"], "height": options["height"], "track_index": VOICE_TRACK, "track_name": "配音",
                        "lead_frames": options["lead_frames"], "name": options["name"],
                        "marker_color": MARKER_COLOR, "markers": markers}
                previous = max((item for record in data["jobs"] for item in record.get("timelines", [])
                                if item.get("creator") == creator and same_creator_project(item.get("project"), config["project"], creator) and item.get("materials")),
                               key=lambda item: item.get("built_at", ""), default=None)
                operation = operations.prepare("timeline", body, {"payload": payload, "config": config,
                    "creator": creator, "take_id": take["id"], "markers": markers,
                    "materials": list((previous or {}).get("materials") or config.get("material_timelines") or [])})
            snapshot = operation["snapshot"]
            config, markers, creator = snapshot["config"], snapshot["markers"], snapshot["creator"]
            payload = {**snapshot["payload"], "operation_key": operation["operation_key"],
                       "recovery_file": str(operations.recovery_file(operation).resolve())}
            started = False
            try:
                with self._timeline_turn(identifier, body["request_id"], creator, report, snapshot):
                    operation = operations.update(operation, "inflight", error="")
                    started = True
                    result = self.resolve.run("build", payload, held=True)
            except Exception as exc:
                # The worker journals its identity before it changes anything in DaVinci. Without that journal
                # nothing was built, so the request ends cleanly instead of blocking the job as uncertain.
                if not operations.recovery_file(operation).exists():
                    try:
                        operations.update(operation, "safe_failed", error=str(exc)[:1000],
                                          cancelled=isinstance(exc, QueueCancelled))
                    except OSError:
                        pass
                elif started:
                    operations.best_effort_uncertain(operation, exc)
                raise
            try:
                warnings = list(result.get("warnings") or [])
                if len(result.get("markers") or []) != len(markers):
                    warnings.append(f"读回 {len(result.get('markers') or [])} 个意群标记，应有 {len(markers)} 个，请在达芬奇里核对。")
                record = {"name": result["name"], "unique_id": result["unique_id"], "project": result.get("project") or config["project"], "creator": creator,
                          "take_id": snapshot["take_id"], "fps": payload["fps"], "width": payload["width"], "height": payload["height"],
                          "lead_frames": payload.get("lead_frames", 0), "group_markers": bool(markers),
                          "frames": result["end_frame"] - result["start_frame"],
                          "markers": len(result.get("markers") or []), "warnings": warnings, "built_at": now(),
                          "operation_key": operation["operation_key"], "saved": result.get("saved") is True,
                          "build_request_id": body["request_id"],
                          **{key: result[key] for key in ("folder", "project_id") if result.get(key)},
                          "materials": [name for name in result.get("materials") or [] if isinstance(name, str)]}
                operation = operations.update(operation, "result_ready", result=record, error="")
            except Exception as exc:
                operations.best_effort_uncertain(operation, exc)
                raise
            return self._commit_production_operation(operations, operation)

    def open_timeline(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        if "frame" in body and (type(body["frame"]) is not int or body["frame"] < 0):
            raise VoiceError(400, "帧位置必须是从第 0 帧算起的非负整数。")
        job = self.production.job(identifier)
        record = next((item for item in job.get("timelines", []) if item["unique_id"] == body.get("unique_id")), None)
        if not record:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        payload = {"project": record["project"], "unique_id": record["unique_id"], "name": record["name"], **timeline_location(record)}
        if "frame" in body:
            # Relative to the timeline start, matching edit report and engineering-summary gaps.
            # The worker checks the live duration; the saved build record may predate manual trimming.
            payload["frame"] = body["frame"]
        return {"ok": True, **{key: value for key, value in self.resolve.run(
            "open", payload, timeout=30).items() if key != "ok"}}
