"""Durable production records: voice library, reading table and per-script production jobs.

runtime/production is business data. A job freezes the adopted script lines and meaning-group table;
voiceover takes are appended and never overwritten. Audio files live beside the catalog.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from uuid import uuid4

from direction_store import fingerprint, now, product_key
from elevenlabs_client import DEFAULT_MODEL, VOICE_ID, VoiceError, clean_settings, model

JOB_ID = re.compile(r"PRD-[0-9a-f]{12}")
TAKE_ID = re.compile(r"TAK-[0-9a-f]{12}")
JOB_STATUSES = {"draft", "voiced", "timeline"}
# Finished voiceovers and timelines are appended whatever else changed while they were being made.
REVISION_FREE = {"take", "timeline", "edit_start", "edit_finish"}
EDIT_ID = re.compile(r"EDT-[0-9a-f]{12}")
# Timeline frame rates as exact ratios; names are what DaVinci Resolve shows.
FRAME_RATES = {"23.976": (24000, 1001), "24": (24, 1), "25": (25, 1), "29.97": (30000, 1001), "30": (30, 1),
               "50": (50, 1), "59.94": (60000, 1001), "60": (60, 1)}
CREATOR = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,23}")


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


class ProductionStore:
    def __init__(self, runtime, lock=None):
        self.root = Path(runtime) / "production"
        self.path = self.root / "catalog.json"
        self.audio = self.root / "audio"
        self.lock = lock or threading.RLock()

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
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise VoiceError(500, "制作资料无法读取，请保留原文件并从备份恢复。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

    def write(self, data):
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
                      "settings": copy.deepcopy(data["settings"]),
                      "readings": copy.deepcopy(data["readings"]), "jobs": copy.deepcopy(data["jobs"]),
                      "revision": self.revision(data)}
            if product and product != "all":
                key = product_key(product)
                result["product_settings"] = self.product_settings(data, key)
                result["readings"] = [item for item in result["readings"] if item["product"] == key]
                result["jobs"] = [item for item in result["jobs"] if item["product"] == key]
            return result

    def job(self, identifier):
        with self.lock:
            found = next((item for item in self.read()["jobs"] if item["id"] == identifier), None)
            if not found:
                raise VoiceError(404, "这张制作单不存在。")
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
        with self.lock:
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
                        "edit_start": self._edit_start, "edit_finish": self._edit_finish}
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
        if not take.get("groups") or any(group.get("start") is None for group in take["groups"]):
            raise VoiceError(409, "这版配音没有完整的意群时间，不能选用；请重新生成。")
        job.update(selected_take=take_id, status="timeline" if job.get("timelines") else "voiced", updated_at=now())
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
            kept = data["settings"]["products"].get(key, {}).get("material_timelines", [])
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
        job.update(status="timeline", updated_at=record["built_at"])
        job["history"].append({"at": record["built_at"], "event": "timeline", "note": f"在达芬奇建时间线 {record['name']}"})
        return {"job": copy.deepcopy(job), "timeline": copy.deepcopy(record)}

    def _edit_start(self, data, body, record):
        job = self._find_job(data, body.get("job_id"))
        if not isinstance(record, dict) or not EDIT_ID.fullmatch(str(record.get("id"))):
            raise VoiceError(500, "剪辑任务未完成核对，未启动。")
        job.setdefault("edits", []).append(copy.deepcopy(record))
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
        job["updated_at"] = edit["finished_at"]
        labels = {"completed": "AI 剪辑完成", "failed": "AI 剪辑失败", "cancelled": "AI 剪辑已停止"}
        job["history"].append({"at": edit["finished_at"], "event": "edit_" + edit["status"], "note": f"{labels.get(edit['status'], edit['status'])} {edit['id']}"})
        return {"job": copy.deepcopy(job), "edit": copy.deepcopy(edit)}

    def interrupt_running(self):
        """A restarted workbench cannot reattach to an editing process; mark such runs as interrupted."""
        with self.lock:
            if not self.path.exists():
                return
            data = self.read()
            changed = False
            for job in data["jobs"]:
                for edit in job.get("edits", []):
                    if edit.get("status") == "running":
                        edit.update(status="interrupted", finished_at=now(),
                                    error="工作台重启时这次剪辑还在进行，已中断；达芬奇里可能留有做到一半的结果，请先核对。")
                        changed = True
            if changed:
                self.write(data)
