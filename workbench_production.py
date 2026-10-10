"""Workbench production API: pick a voiced variant, keep a voice library and generate timed voiceovers.

Network calls happen only on explicit requests and never while the workbench lock is held. Each
voiceover take stores its audio, the exact text sent and the timing of every meaning group.
"""
from __future__ import annotations

import copy
from datetime import datetime
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4

from creation_store import CreationError
from direction_store import now, product_key
from elevenlabs_client import DEFAULT_MODEL, MODELS, VOICE_ID, VoiceError, clean_settings, model
from production_store import FRAME_RATES, JOB_ID, TAKE_ID, apply_readings
from variant_parse import parse_variants, same_lines, spoken_lines

MAX_LINES = 80
MAX_LINE_CHARS = 1000
MIN_COVERAGE = 0.6
MARKER_COLOR = "Blue"  # Red stays reserved for the editing skill's missing-footage markers.
VOICE_TRACK = 2  # Existing timelines keep source audio on A1 (stereo) and the voiceover on A2 (mono).


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


def meaning_markers(job, take, fps):
    """One range marker per meaning group, tiling the voiceover: [this group's start, next group's start).

    The first marker starts at frame 0 so the opening shot is covered; pauses after a line belong to that line.
    """
    numerator, denominator = FRAME_RATES[fps]
    rate = numerator / denominator
    groups = take["groups"]
    starts = [int(round(group["start"] * rate)) for group in groups]
    starts[0] = 0
    for index in range(1, len(starts)):
        starts[index] = max(starts[index], starts[index - 1] + 1)
    seconds = take.get("duration") or max(group["end"] for group in groups)
    ends = starts[1:] + [max(int(math.floor(seconds * rate)), starts[-1] + 1)]
    markers = []
    for line, group, start, end in zip(job["lines"], groups, starts, ends):
        note = [f"口播：{line['spoken']}", f"中文：{line['zh']}" if line.get("zh") else "",
                f"画面要证明什么：{line['proof']}" if line.get("proof") else "画面要证明什么：待补",
                f"素材标签：{line['tags']}" if line.get("tags") else "", f"证据：{line['evidence']}" if line.get("evidence") else "",
                f"配音：{group['start']:.2f}–{group['end']:.2f} 秒；标记 [{start}, {end}) 帧，含句后停顿"]
        markers.append({"number": line["number"], "frame": start, "duration": end - start,
                        "name": f"#{line['number']} {_short(line['spoken'], 36)}", "note": "\n".join(item for item in note if item),
                        "custom": f"workbench-group:{job['id']}:{line['number']}"})
    return markers


def _complete(groups):
    return bool(groups) and all(group["start"] is not None and group["coverage"] >= MIN_COVERAGE for group in groups)


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

    def production_catalog(self, product=None):
        result = self.production.public(product)
        result["models"] = copy.deepcopy(MODELS)
        result["default_model"] = DEFAULT_MODEL
        for job in result["jobs"]:
            job["voicing"] = job["id"] in self._voicing()
        return result

    def _tts(self, job, readings):
        lines, count = apply_readings([line["spoken"] for line in job["lines"]], readings)
        joined = "\n".join(lines)
        return {"lines": lines, "text": joined, "characters": len(joined), "substitutions": count}

    def production_job(self, identifier):
        if not JOB_ID.fullmatch(str(identifier)):
            raise VoiceError(404, "这张制作单不存在。")
        with self.lock:
            catalog = self.production.public()
            job = next((item for item in catalog["jobs"] if item["id"] == identifier), None)
            if not job:
                raise VoiceError(404, "这张制作单不存在。")
            readings = [item for item in catalog["readings"] if item["product"] == job["product"]]
            try:
                script = self.creation.get_script(job["script_id"])
                script = {key: script.get(key) for key in ("id", "title", "status", "version", "product", "direction_id")}
            except CreationError:
                script = None
            job["voicing"] = identifier in self._voicing()
            job.setdefault("timelines", [])
            for edit in job.setdefault("edits", []):
                if edit["status"] == "running":
                    live = self.edit_live(edit["id"]) if hasattr(self, "edit_live") else None
                    if live:
                        edit["live"] = live
                    else:
                        edit.update(status="interrupted", error="这次剪辑的进程已不在运行，结果可能不完整；请先到达芬奇里核对。")
            return {"job": job, "revision": catalog["revision"], "readings": readings, "tts": self._tts(job, readings),
                    "script": script, "voices": catalog["voices"], "defaults": catalog["defaults"],
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
            raise VoiceError(409, "这一轮候选里没有找到「口播（配音用）」代码块，无法拆出可配音的变体。")
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
            index = value.get("variant_index")
            chosen = variants[index] if type(index) is int and 0 <= index < len(variants) else None
            if not chosen or not same_lines([line["spoken"] for line in chosen["lines"]], lines):
                chosen = next((item for item in variants if same_lines([line["spoken"] for line in item["lines"]], lines)), None)
            if chosen:
                records, problems, label = chosen["lines"], chosen["problems"], chosen["label"]
            else:
                chinese = [line.strip() for line in str(script.get("translation_zh") or "").splitlines() if line.strip()]
                records = [{"number": number, "spoken": line, "zh": chinese[number - 1] if len(chinese) == len(lines) else "",
                            "proof": "", "tags": "", "evidence": ""} for number, line in enumerate(lines, 1)]
                problems = ["来源候选里没有逐行一致的意群表，画面要求请在制作单里补填。" if variants else
                            "这条脚本没有附带意群表，画面要求请在制作单里补填。"]
                label = ""
            resolved = {"product": product_key(script["product"]), "direction_id": script.get("direction_id", ""),
                        "script_id": script["id"], "script_version": script["version"], "script_family": script["family_id"],
                        "title": script["title"], "variant_label": label, "lines": copy.deepcopy(records), "problems": problems,
                        "source": {"task_id": source.get("task_id", ""), "round_id": source.get("round_id", ""),
                                   "variant_index": chosen["index"] if chosen else None}}
            return self.production.mutate("job", body, resolved)

    def save_job_lines(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "意群表参数格式无效。")
        return self.production.mutate("job_lines", {**body, "job_id": identifier})

    def archive_job(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "归档参数格式无效。")
        return self.production.mutate("job_archive", {**body, "job_id": identifier})

    def select_take(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "选用参数格式无效。")
        return self.production.mutate("select_take", {**body, "job_id": identifier})

    def _timing(self, lines, result, audio_path):
        warnings = []
        for source, triples in (("tts", _triples_tts(result.get("alignment"))),
                                ("tts_normalized", _triples_tts(result.get("normalized_alignment")))):
            if triples:
                groups = map_groups(lines, triples)
                if _complete(groups):
                    return groups, source, warnings
        try:
            groups = map_groups(lines, _triples_forced(self.voices.align(audio_path.read_bytes(), audio_path.name, "\n".join(lines))))
            if _complete(groups):
                return groups, "forced", warnings
            warnings.append("强制对齐后仍有意群没有对上，请试听核对。")
            return groups, "forced", warnings
        except VoiceError as exc:
            warnings.append(f"没有拿到意群时间：{exc}")
            return [{"number": number, "start": None, "end": None, "coverage": 0.0} for number in range(1, len(lines) + 1)], "", warnings

    def generate_take(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "配音参数格式无效。")
        body = {**body, "job_id": identifier}
        prior = self.production.replay("take", body)
        if prior:
            return prior
        voice_id, model_id = body.get("voice_id"), body.get("model_id", DEFAULT_MODEL)
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise VoiceError(400, "音色 ID 格式无效。")
        settings = clean_settings(model_id, body.get("settings"))
        with self.lock:
            job = self.production.job(identifier)
            if job["archived"]:
                raise VoiceError(409, "制作单已归档，请恢复后再生成配音。")
            tts = self._tts(job, self.production.readings(job["product"]))
            if tts["characters"] > model(model_id)["max_chars"]:
                raise VoiceError(400, "口播超过所选模型单次可配音的字数。")
            if identifier in self._voicing():
                raise VoiceError(409, "这张制作单正在生成配音，请等待完成。")
            known = self.production.voice(voice_id)
            self._voicing().add(identifier)
        try:
            lookup = None if known and known.get("name") else self.voices.voice(voice_id)
            result = self.voices.speech(voice_id, tts["text"], model_id, settings)
            take_id = "TAK-" + uuid4().hex[:12]
            folder = self.production.audio / identifier / take_id
            folder.mkdir(parents=True, exist_ok=False)
            mp3 = folder / "voiceover.mp3"
            mp3.write_bytes(result["audio"])
            groups, alignment_source, warnings = self._timing(tts["lines"], result, mp3)
            written, duration = convert_wav(mp3, folder / "voiceover.wav")
            if not written:
                warnings.append("没有转出 WAV（需要本机 ffmpeg），只保存了 MP3。")
            if duration is None:
                ends = [group["end"] for group in groups if group["end"] is not None]
                duration = max(ends) if ends else None
            (folder / "alignment.json").write_text(json.dumps({"alignment": result.get("alignment"),
                "normalized_alignment": result.get("normalized_alignment"), "groups": groups}, ensure_ascii=False), encoding="utf-8")
            take = {"id": take_id, "voice_id": voice_id, "model_id": model_id, "settings": settings, "tts_lines": tts["lines"],
                    "tts_sha256": hashlib.sha256(tts["text"].encode()).hexdigest(), "characters": tts["characters"],
                    "request_id": result["request_id"], "audio": f"{identifier}/{take_id}/voiceover.mp3",
                    "wav": f"{identifier}/{take_id}/voiceover.wav" if written else "", "duration": duration,
                    "alignment_source": alignment_source, "groups": groups, "warnings": warnings,
                    "created_at": now(), "lookup": lookup}
        finally:
            with self.lock:
                self._voicing().discard(identifier)
        return self.production.mutate("take", body, take)

    def production_audio(self, identifier, take_id, kind):
        if not JOB_ID.fullmatch(identifier) or not TAKE_ID.fullmatch(take_id) or kind not in {"mp3", "wav"}:
            raise VoiceError(404, "配音文件不存在。")
        path = (self.production.audio / identifier / take_id / f"voiceover.{kind}").resolve()
        if not path.is_relative_to(self.production.audio.resolve()) or not path.is_file():
            raise VoiceError(404, "配音文件不存在。")
        return path

    def resolve_status(self):
        try:
            return {"ok": True, **{key: value for key, value in self.resolve.run("status", timeout=30).items() if key != "ok"}}
        except VoiceError as exc:
            return {"ok": False, "busy": self.resolve.busy(), "error": str(exc)}

    def save_production_settings(self, body):
        result = self.production.mutate("settings", body)
        return {**result, "catalog": self.production_catalog()}

    def build_timeline(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "时间线参数格式无效。")
        body = {**body, "job_id": identifier}
        prior = self.production.replay("timeline", body)
        if prior:
            return prior
        with self.lock:
            data = self.production.read()
            job = next((item for item in data["jobs"] if item["id"] == identifier), None)
            if not job:
                raise VoiceError(404, "这张制作单不存在。")
            if job["archived"]:
                raise VoiceError(409, "制作单已归档，请恢复后再建时间线。")
            take = next((item for item in job["takes"] if item["id"] == job["selected_take"]), None)
            if not take:
                raise VoiceError(409, "请先选用一版配音，再建时间线。")
            creator = body.get("creator")
            if creator not in data["settings"]["creators"]:
                raise VoiceError(400, "请选择达人；达人名在「制作视频」页的达芬奇设置里维护。")
            config = self.production.product_settings(data, job["product"])
            audio = self.production.audio / (take["wav"] or take["audio"])
            if not audio.is_file():
                raise VoiceError(409, "配音文件不见了，请重新生成配音。")
            markers = meaning_markers(job, take, config["fps"])
            prefix = f"{creator}{datetime.now().strftime('%y%m%d')}-{config['code']}-"
        result = self.resolve.run("build", {
            "project": config["project"], "audio": str(audio), "prefix": prefix, "clip_suffix": "_配音",
            "audio_bin": config["audio_bin"], "timeline_bin": config["timeline_bin"], "fps": config["fps"],
            "width": config["width"], "height": config["height"], "track_index": VOICE_TRACK, "track_name": "配音",
            "marker_color": MARKER_COLOR, "markers": markers})
        warnings = list(result.get("warnings") or [])
        if len(result.get("markers") or []) != len(markers):
            warnings.append(f"读回 {len(result.get('markers') or [])} 个意群标记，应有 {len(markers)} 个，请在达芬奇里核对。")
        record = {"name": result["name"], "unique_id": result["unique_id"], "project": config["project"], "creator": creator,
                  "take_id": take["id"], "fps": config["fps"], "frames": result["end_frame"] - result["start_frame"],
                  "markers": len(result.get("markers") or []), "warnings": warnings, "built_at": now()}
        return self.production.mutate("timeline", body, record)

    def open_timeline(self, identifier, body):
        if not isinstance(body, dict):
            raise VoiceError(400, "参数格式无效。")
        job = self.production.job(identifier)
        record = next((item for item in job.get("timelines", []) if item["unique_id"] == body.get("unique_id")), None)
        if not record:
            raise VoiceError(404, "这张制作单没有这条时间线。")
        return {"ok": True, **{key: value for key, value in self.resolve.run(
            "open", {"project": record["project"], "unique_id": record["unique_id"], "name": record["name"]}, timeout=30).items() if key != "ok"}}
