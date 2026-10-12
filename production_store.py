"""Durable production records: voice library, reading table and per-script production jobs.

runtime/production is business data. A job freezes the adopted script lines and meaning-group table;
voiceover takes are appended and never overwritten. Audio files live beside the catalog.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
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
from elevenlabs_client import DEFAULT_MODEL, VOICE_ID, VoiceError, clean_settings, model
from production_operations import file_lock
from production_validation import MANUAL_SCOPE, shot_signature

JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")
TAKE_ID = re.compile(r"TAK-[0-9a-f]{12}")
JOB_STATUSES = {"draft", "voiced", "timeline", "editing", "awaiting_review", "approved", "needs_material", "needs_repair",
                "verification_failed", "unverified", "failed", "cancelled", "interrupted"}
# Finished voiceovers and timelines are appended whatever else changed while they were being made.
REVISION_FREE = {"take", "timeline", "edit_start", "edit_finish", "edit_recheck", "edit_approve", "edit_baseline", "edit_delete", "job_delete", "carousel_delete", "carousel_search_delete", "carousel_tidy", "carousel_search_start", "carousel_search_finish", "carousel_import",
                 "carousel_generation_start", "carousel_generation_finish", "carousel_generation_stage", "carousel_output", "carousel_publication",
                 "carousel_import_finished", "carousel_review_start", "carousel_review_finish", "carousel_collection"}
EDIT_ID = re.compile(r"EDT-[0-9a-f]{12}")
CAROUSEL_ID = re.compile(r"CAR-[0-9a-f]{12}")
SEARCH_ID = re.compile(r"STS-[0-9a-f]{12}")
CANDIDATE_ID = re.compile(r"CND-[0-9a-f]{12}")
GENERATION_ID = re.compile(r"IMG-[0-9a-f]{12}")
OUTPUT_ID = re.compile(r"OUT-[0-9a-f]{12}")
REVIEW_ID = re.compile(r"REV-[0-9a-f]{12}")
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


def job_status(job):
    """A past timeline for another take cannot certify the currently selected version."""
    take = job.get("selected_take")
    if not take:
        return "draft"
    targets = {row.get("unique_id"): row for row in job.get("timelines", []) if row.get("take_id") == take}
    if not targets:
        return "voiced"
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
            if isinstance((latest.get("report") or {}).get("gaps"), list) and latest["report"]["gaps"]:
                return "needs_material"
            return "approved" if latest.get("approval") else "awaiting_review"
        return "unverified"
    if state == "awaiting_review" and latest.get("approval"):
        return "approved"
    return state if state in JOB_STATUSES else "verification_failed"


def current_edit(job, edit):
    """Only the latest attempt on a timeline of the selected voiceover decides the job, so only it can be completed."""
    timeline = next((row for row in job.get("timelines", []) if row.get("unique_id") == edit.get("timeline_uid")), None)
    latest = next((item for item in reversed(job.get("edits", [])) if item.get("timeline_uid") == edit.get("timeline_uid")), None)
    return bool(timeline and job.get("selected_take") and timeline.get("take_id") == job["selected_take"] and latest is edit)


def public_job(job, result_root=None):
    result = copy.deepcopy(job)
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
PRODUCT_REQUIREMENT_KEYS = ("mode", "editable_regions", "protected_product", "protected_evidence", "hands_authorized",
                            "aspect", "style", "source", "postprocess")


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
    # Optional by design: adding a default would change signatures of already
    # imported, reviewed image groups that never requested a caption preset.
    if "caption_style" in source:
        if not isinstance(source["caption_style"], str) or source["caption_style"] not in {"plain", "rugged"}:
            raise VoiceError(400, "请选择普通文字或粗犷花字排版。")
        value["caption_style"] = source["caption_style"]
    if slide is not None:
        if "authorized_hand_slides" in source:
            value["hands_authorized"] = slide["number"] in source["authorized_hand_slides"]
        if slide["number"] in source.get("no_hand_slides", []):
            value["hands_authorized"] = False
        value["locked_copy"] = {"number": slide["number"], "title_lines": copy.deepcopy(slide.get("title_lines", [])),
                                "body": slide.get("body", "")}
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
    return fingerprint(values)


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


def carousel_confirmation_current(item, slide, output):
    """Keep manual confirmation bound to the selected file and current inputs."""
    if not carousel_output_current(item, slide, output) or output.get("approved") is not True:
        return False
    # Before manual receipts existed, an old review stored the same file identity.
    # Its findings are optional; only the bytes identity remains relevant here.
    approved_hash = output.get("approved_sha256") or (output.get("review") or {}).get("file_sha256")
    approved_input = output.get("approved_input_signature") or output.get("input_signature")
    return bool((not approved_hash or approved_hash == output.get("sha256")) and
                approved_input == output.get("input_signature"))


class ProductionStore:
    def __init__(self, runtime, lock=None):
        self.root = Path(runtime) / "production"
        self.path = self.root / "catalog.json"
        self.audio = self.root / "audio"
        self.lock = lock or threading.RLock()
        self._catalog_owner = threading.local()

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
        if not self.path.exists():
            return self.empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("schema_version") != 1 or
                    any(not isinstance(value.get(key), list) for key in ("voices", "readings", "jobs")) or
                    any(not isinstance(value.get(key), dict) for key in ("defaults", "requests"))):
                raise ValueError("invalid production catalog")
            value.setdefault("settings", {"creators": [], "products": {}})  # catalogs written before phase two
            value.setdefault("carousels", [])  # catalogs written before photo carousels
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise VoiceError(500, "制作资料无法读取，请保留原文件并从备份恢复。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

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
        encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        if len(encoded) > 64 * 1024 * 1024:
            raise VoiceError(413, "制作资料超过容量限制，本次未截断或覆盖记录。")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".production-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def public(self, product=None):
        with self.lock:
            data = self.read()
            result = {"voices": copy.deepcopy(data["voices"]), "defaults": copy.deepcopy(data["defaults"]),
                      "settings": copy.deepcopy(data["settings"]), "carousels": copy.deepcopy(data["carousels"]),
                      "readings": copy.deepcopy(data["readings"]), "jobs": [public_job(job, self.root / "edits") for job in data["jobs"]],
                      "revision": self.revision(data)}
            if product and product != "all":
                key = product_key(product)
                result["product_settings"] = self.product_settings(data, key)
                result["readings"] = [item for item in result["readings"] if item["product"] == key]
                result["jobs"] = [item for item in result["jobs"] if item["product"] == key]
                result["carousels"] = [item for item in result["carousels"] if item["product"] == key]
            return result

    def job(self, identifier):
        with self.lock:
            found = next((item for item in self.read()["jobs"] if item["id"] == identifier), None)
            if not found:
                raise VoiceError(404, "这张制作单不存在。")
            return public_job(found, self.root / "edits")

    def carousel(self, identifier):
        with self.lock:
            found = next((item for item in self.read()["carousels"] if item["id"] == identifier), None)
            if not found:
                raise VoiceError(404, "这张图文制作单不存在。")
            return copy.deepcopy(found)

    def readings(self, product):
        with self.lock:
            return [copy.deepcopy(item) for item in self.read()["readings"] if item["product"] == product_key(product)]

    def voice(self, voice_id):
        with self.lock:
            return copy.deepcopy(next((item for item in self.read()["voices"] if item["voice_id"] == voice_id), None))

    def replay(self, kind, body):
        with self.lock:
            prior = self.read()["requests"].get(body.get("request_id")) if isinstance(body, dict) else None
            if prior:
                if prior["hash"] != fingerprint([kind, body]):
                    raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                return copy.deepcopy(prior["result"])
            return None

    def mutate(self, kind, body, extra=None):
        if not isinstance(body, dict):
            raise VoiceError(400, "制作保存参数必须是对象。")
        key = body.get("request_id")
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
            raise VoiceError(400, "缺少保存标识，请刷新后重试。")
        request_hash = fingerprint([kind, body])
        with self.lock, self.catalog_transaction():
            data = self.read()
            prior = data["requests"].get(key)
            if prior:
                if prior["hash"] != request_hash:
                    raise VoiceError(409, "保存标识已用于另一项修改，请重新提交。")
                return copy.deepcopy(prior["result"])
            if kind not in REVISION_FREE and body.get("revision") != self.revision(data):
                raise VoiceError(409, "制作资料已更新，请读取最新记录后重新保存。")
            handlers = {"voice": self._voice, "reading": self._reading, "job": self._job, "job_lines": self._job_lines,
                        "job_archive": self._job_archive, "take": self._take, "select_take": self._select_take,
                        "settings": self._settings, "timeline": self._timeline,
                        "edit_start": self._edit_start, "edit_finish": self._edit_finish, "edit_recheck": self._edit_recheck,
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
                        "carousel_output": self._carousel_output, "carousel_approval": self._carousel_approval,
                        "carousel_delivery": self._carousel_delivery, "carousel_collection": self._carousel_collection,
                        "carousel_publication": self._carousel_publication}
            if kind not in handlers:
                raise VoiceError(400, "不支持这项制作操作。")
            result = handlers[kind](data, body, extra)
            data["requests"][key] = {"hash": request_hash, "result": copy.deepcopy(result)}
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
        job["updated_at"] = now()
        job["history"].append({"at": job["updated_at"], "event": "lines", "note": "修改意群表画面要求"})
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
        job["status"] = job_status(job)
        job["history"].append({"at": record["built_at"], "event": "timeline", "note": f"在达芬奇建时间线 {record['name']}"})
        return {"job": copy.deepcopy(job), "timeline": copy.deepcopy(record)}

    def _edit_start(self, data, body, record):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(record, dict) or not EDIT_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "剪辑任务未完成核对，未启动。")
        job.setdefault("edits", []).append(copy.deepcopy(record))
        job["status"] = job_status(job)
        job["updated_at"] = record["started_at"]
        job["history"].append({"at": record["started_at"], "event": "edit", "note": f"开始 AI 剪辑 {record['id']}（{record['timeline_name']}）"})
        product = data["settings"]["products"].setdefault(job["product"], {})
        product["material_timelines"] = list(record["materials"])
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(record)}

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
                  "needs_repair": "待修正", "verification_failed": "核验未完成", "unverified": "历史结果待核验"}
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
            "validation": copy.deepcopy(edit.get("validation")), "readback_error": edit.get("readback_error", "")})
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
                if (edit.get("status") not in {"completed", "awaiting_review"} or
                        edit.get("execution_status") != "completed" or (edit.get("report") or {}).get("saved") is not True or
                        (isinstance((edit.get("report") or {}).get("gaps"), list) and edit["report"]["gaps"])):
                    raise VoiceError(409, "这次剪辑尚未完成保存交付，请先处理已列明的问题。")
                job["updated_at"] = now()
                edit["approval"] = {"at": job["updated_at"], "note": str(body.get("note") or ""),
                                    "reviewed": list(MANUAL_SCOPE), "basis": "human_review"}
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
            data = self.read()
            changed = False
            for job in data["jobs"]:
                for edit in job.get("edits", []):
                    if edit.get("status") == "running":
                        edit.update(status="interrupted", execution_status="interrupted", finished_at=now(),
                                    error="工作台重启时这次剪辑还在进行，已中断；达芬奇里可能留有做到一半的结果，请先核对。")
                        changed = True
            for item in data["carousels"]:
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
        stamp = now()
        item = {"id": "CAR-" + uuid4().hex[:12], **copy.deepcopy(resolved), "status": "draft", "archived": False, "searches": [], "generations": [],
                "history": [{"at": stamp, "event": "created", "note": "按图文技能生成的逐张表创建制作单" if (resolved.get("source") or {}).get("table_id") else "按已采纳的图文版创建制作单"}],
                "created_at": stamp, "updated_at": stamp}
        for slide in item["slides"]:
            slide.update(base=None, status="pending", candidates=[], outputs=[], output_id=None)
        defaults = data["settings"]["products"].get(product_key(item.get("product", "")), {}).get("carousel_requirements")
        if isinstance(defaults, dict):
            try:
                item["requirements"] = carousel_requirements({**item, "requirements": defaults})
                item["image_aspect"] = item["requirements"]["aspect"]
                item["history"].append({"at": stamp, "event": "requirements", "note": "沿用该产品的默认出图设置"})
            except VoiceError:
                pass
        data["carousels"].append(item)
        return {"carousel": copy.deepcopy(item)}

    def _carousel_slides(self, data, body, extra):
        """Only how each slide is made can change; its words belong to the adopted script version."""
        item = self._find_carousel(data, body.get("carousel_id"))
        values = body.get("slides")
        if not isinstance(values, list) or len(values) != len(item["slides"]):
            raise VoiceError(400, "须逐张提交，张数与制作单一致。")
        for slide, value in zip(item["slides"], values):
            if not isinstance(value, dict) or value.get("number") != slide["number"]:
                raise VoiceError(400, "图文编号与制作单不一致，请刷新后重试。")
            for key, label in (("role", "作用"), ("source", "素材来源"), ("forbidden", "禁写")):
                updated = text(value.get(key, slide[key]), label, False, 2000)
                if updated != slide[key]:
                    self._clear_carousel_approval(slide)
                slide[key] = updated
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "slides", "note": "修改逐张作用、素材来源或禁写"})
        return {"carousel": copy.deepcopy(item)}

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
            key: copy.deepcopy(item["requirements"][key]) for key in PRODUCT_REQUIREMENT_KEYS}
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
        if any(entry.get("status") == "running" for key in ("searches", "generations") for entry in item.get(key, [])):
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
            # One base image per slide: the other candidates go once a base is chosen; the caller removes their files.
            removed = [{"id": entry["id"], "file": entry.get("file"), "thumb": entry.get("thumb")}
                       for entry in slide.get("candidates", []) if entry["id"] != chosen]
            slide["candidates"] = [entry for entry in slide.get("candidates", []) if entry["id"] == chosen]
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "base",
                                "note": f"第 {slide['number']} 张" + (f"选用底图 {chosen}，移除其余 {len(removed)} 张候选" if chosen else "取消底图")})
        return {"carousel": copy.deepcopy(item), "removed_candidates": removed}

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
        slide = next((entry for entry in item["slides"] if type(body.get("slide")) is int and entry["number"] == body["slide"]), None)
        output = next((entry for entry in slide.get("outputs", []) if entry["id"] == body.get("output_id")), None) if slide else None
        if not output:
            raise VoiceError(404, "这张成图不属于这一张。")
        approved = body.get("approved")
        if type(approved) is not bool:
            raise VoiceError(400, "确认参数无效。")
        if approved and not carousel_output_current(item, slide, output):
            raise VoiceError(409, "底图或制作要求已变化，请按当前资料重新出图。")
        note = text(body.get("note", ""), "确认备注", False, 2000)
        if approved:
            slide["output_id"] = output["id"]
        actor = body.get("actor", "human")
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
        item["status"] = self._carousel_status(item)
        item["updated_at"] = now()
        item["history"].append({"at": item["updated_at"], "event": "approval", "note": f"第 {slide['number']} 张" + ("工作台确认" if approved else "撤销确认") + f" {output['id']}", "actor": actor})
        return {"carousel": copy.deepcopy(item)}

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
            return {"carousel": copy.deepcopy(item), "delivery": copy.deepcopy(existing)}
        item.setdefault("deliveries", []).append(copy.deepcopy(record))
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
        item["updated_at"] = record["created_at"]
        item["history"].append({"at": item["updated_at"], "event": "delivery", "note": f"整理成品包 {record['id']}，共 {len(record['slides'])} 张"})
        return {"carousel": copy.deepcopy(item), "delivery": copy.deepcopy(record)}

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
        if not package or package.get("sha256") != record.get("delivery_sha256"):
            raise VoiceError(409, "实际发布的成品包不存在或已变化，请重新核对。")
        if any(record.get(key) != package.get(key) for key in ("script_id", "script_version", "selection_signature")):
            raise VoiceError(409, "发布登记的文案或成图版本与实际领取包不一致，请重新核对。")
        for other in data["carousels"]:
            published = other.get("publication") or {}
            if other["id"] != item["id"] and published and (published.get("post_id") == record.get("post_id") or other["script_id"] == item["script_id"]):
                raise VoiceError(409, "帖子或脚本已登记到另一制作单，请先核对原登记。")
        saved = copy.deepcopy(record)
        saved.pop("expected_publication", None)
        item["publication"] = saved
        item.setdefault("publication_history", []).append(copy.deepcopy(saved))
        item["updated_at"] = saved["updated_at"]
        item["history"].append({"at": saved["updated_at"], "event": "publication", "note": f"登记图文帖子 {saved['post_id']}，成品包 {saved['delivery_id']}"})
        return {"carousel": copy.deepcopy(item), "publication": copy.deepcopy(saved)}
