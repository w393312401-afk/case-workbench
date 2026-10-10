"""Durable creation records. Case events and the review pick list remain their own sources.

runtime/creation is business data, including explicitly adopted text and its frozen
provenance. Script text is immutable: editing it creates a new version.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from uuid import uuid4

from direction_store import fingerprint, now, product_key


class CreationError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


STATUSES = {"draft", "adopted", "filmed", "published", "reviewed", "archived"}
PLAN_ACTIONS = {"scale", "execution", "stop"}
PLAN_STATUSES = {"pending", "working", "done", "cancelled"}
# Case expressions and video hooks the basket holds; reviews have their own limit in review_store.MAX_PICKS.
MAX_MATERIALS = 1000
# Receipts and finished journals only answer retries of recent requests.
RECEIPT_DAYS = 30
# Every write keeps the previous catalog; the newest ones and one per recent day are enough to recover from.
BACKUP_KEEP = 50
BACKUP_DAYS = 30


def text(value, label, required=True, limit=12000):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise CreationError(400, f"{label}格式无效。")
    return value.strip()


class CreationStore:
    def __init__(self, runtime, lock=None):
        self.root = Path(runtime) / "creation"
        self.path = self.root / "catalog.json"
        self.lock = lock or threading.RLock()
        # Counts catalog writes in this process, so readers can cache without trusting file times alone.
        self.version = 0

    @staticmethod
    def empty():
        return {"schema_version": 1, "scripts": [], "materials": [], "theme_links": [], "plans": [], "requests": {}}

    def read(self):
        if not self.path.exists():
            return self.empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(value, dict) or value.get("schema_version") != 1 or
                    any(not isinstance(value.get(key), list) for key in ("scripts", "materials", "theme_links", "plans")) or
                    not isinstance(value.get("requests"), dict)):
                raise ValueError("invalid creation catalog")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise CreationError(500, "创作资料无法读取，请保留原文件并从备份恢复。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

    def _replace(self, path, data):
        encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        if len(encoded) > 64 * 1024 * 1024:
            raise CreationError(413, "创作资料超过容量限制，本次未截断或覆盖记录。")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".creation-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if path == self.path:
                self.version += 1
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def write(self, data, *, backup=True):
        if backup and self.path.exists():
            raw = self.path.read_bytes()
            target = self.root / "backups" / (hashlib.sha256(raw).hexdigest() + ".json")
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                self._prune_backups()
        self._replace(self.path, data)

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

    def prune_operations(self):
        """Drop finished operation journals older than RECEIPT_DAYS. Unfinished ones stay for recovery, and the small
        markers left by case deletion stay so a stale retry still cannot write a deleted case back."""
        cutoff = time.time() - RECEIPT_DAYS * 86400
        for path in (self.root / "operations").glob("*.json"):
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                if json.loads(path.read_text(encoding="utf-8")).get("complete"):
                    path.unlink()
            except (OSError, ValueError):
                continue

    @staticmethod
    def _prune_requests(data):
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RECEIPT_DAYS)).isoformat(timespec="microseconds")
        stamp = now()
        for key, receipt in list(data["requests"].items()):
            if not receipt.get("at"):
                # Receipts from before this field start their month now.
                receipt["at"] = stamp
            elif receipt["at"] < cutoff:
                del data["requests"][key]

    def public(self, product=None):
        with self.lock:
            data = self.read()
            result = {key: copy.deepcopy(value) for key, value in data.items() if key not in {"requests", "schema_version"}}
            if product and product != "all":
                for key in result:
                    result[key] = [item for item in result[key] if item.get("product") == product_key(product)]
            result["revision"] = self.revision(data)
            return result

    def get_script(self, identifier):
        with self.lock:
            item = next((item for item in self.read()["scripts"] if item["id"] == identifier), None)
            if not item:
                raise CreationError(404, "这条文案版本不存在。")
            return copy.deepcopy(item)

    def replay(self, kind, body):
        """Return a durable receipt before the caller resolves a now-changed source."""
        with self.lock:
            prior = self.read()["requests"].get(body.get("request_id")) if isinstance(body, dict) else None
            if prior:
                if prior["hash"] != fingerprint([kind, body]):
                    raise CreationError(409, "保存标识已用于另一项修改，请重新提交。")
                return copy.deepcopy(prior["result"])
            return None

    def mutate(self, kind, body, source=None):
        if not isinstance(body, dict):
            raise CreationError(400, "创作保存参数必须是对象。")
        key = body.get("request_id")
        if not isinstance(key, str) or not re.fullmatch(r"[\w-]{10,100}", key):
            raise CreationError(400, "缺少保存标识，请刷新后重试。")
        request_hash = fingerprint([kind, body])
        with self.lock:
            data = self.read()
            prior = data["requests"].get(key)
            if prior:
                if prior["hash"] != request_hash:
                    raise CreationError(409, "保存标识已用于另一项修改，请重新提交。")
                return copy.deepcopy(prior["result"])
            if body.get("revision") != self.revision(data):
                raise CreationError(409, "创作资料已更新，请读取最新记录后重新保存。")
            handlers = {"material": self._material, "material_batch": self._material_batch, "theme_link": self._theme_link, "plan": self._plan,
                        "script": lambda d, b: self._script(d, b, source), "script_status": self._script_status}
            if kind not in handlers:
                raise CreationError(400, "不支持这项创作操作。")
            result = handlers[kind](data, body)
            data["requests"][key] = {"hash": request_hash, "result": copy.deepcopy(result), "at": now()}
            self._prune_requests(data)
            self.write(data)
            return result

    @staticmethod
    def _value(body, key):
        value = body.get(key)
        if not isinstance(value, dict):
            raise CreationError(400, f"{key}格式无效。")
        return value

    @staticmethod
    def _product(value):
        return product_key(text(value, "产品", limit=200))

    def _material(self, data, body):
        value = self._value(body, "material")
        identifier = value.get("id")
        old = next((item for item in data["materials"] if item["id"] == identifier), None)
        if body.get("operation") == "remove":
            if not old:
                raise CreationError(404, "素材已经移出。")
            data["materials"].remove(old)
            return {"removed": identifier}
        if identifier and not old:
            raise CreationError(404, "素材不存在。")
        if value.get("kind") not in {"case", "video", "note", "selling_point", "direction", "script"}:
            raise CreationError(400, "素材类型无效；评论继续使用原摘录夹。")
        product = self._product(value.get("product"))
        if old and old["product"] != product:
            raise CreationError(409, "素材不能改到另一产品。")
        item = {"id": identifier or "MAT-" + uuid4().hex[:12], "kind": value["kind"], "product": product,
                "title": text(value.get("title"), "素材标题", limit=500),
                "text": text(value.get("text", ""), "素材内容", False, 100000),
                "ref_id": text(value.get("ref_id", ""), "来源编号", False, 200),
                "note": text(value.get("note", ""), "备注", False, 5000),
                "created_at": old["created_at"] if old else now(), "updated_at": now()}
        for key in ("type", "source_id", "source_label", "source_url"):
            if key in value:
                item[key] = text(value[key], key, False, 2000)
        if old:
            data["materials"][data["materials"].index(old)] = item
        else:
            duplicate = next((entry for entry in data["materials"] if item["ref_id"] and
                              (entry["product"], entry["kind"], entry["ref_id"]) == (product, item["kind"], item["ref_id"])), None)
            if duplicate:
                return {"material": copy.deepcopy(duplicate), "existing": True}
            if len(data["materials"]) >= MAX_MATERIALS:
                raise CreationError(400, f"素材篮里的案例表达和视频钩子最多 {MAX_MATERIALS} 条。")
            data["materials"].append(item)
        return {"material": copy.deepcopy(item)}

    @staticmethod
    def _material_batch(data, body):
        """Take listed items out, put earlier items back exactly, or set several notes; absent items are skipped."""
        if "notes" in body:
            notes = body["notes"]
            if not isinstance(notes, dict) or any(not isinstance(value, str) for value in notes.values()):
                raise CreationError(400, "备注格式无效。")
            changed = []
            for item in data["materials"]:
                if item["id"] in notes:
                    item.update(note=text(notes[item["id"]], "备注", False, 5000), updated_at=now())
                    changed.append(item["id"])
            return {"noted": changed}
        if "remove" in body:
            ids = body["remove"]
            if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
                raise CreationError(400, "要移出的素材编号格式无效。")
            wanted = set(ids)
            removed = [item["id"] for item in data["materials"] if item["id"] in wanted]
            data["materials"] = [item for item in data["materials"] if item["id"] not in wanted]
            return {"removed": removed}
        items = body.get("restore")
        if not isinstance(items, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in items):
            raise CreationError(400, "要恢复的素材格式无效。")
        present = {item["id"] for item in data["materials"]}
        sources = {(item["product"], item["kind"], item["ref_id"]) for item in data["materials"] if item.get("ref_id")}
        restored = []
        for item in items:
            if item["id"] in present or item.get("ref_id") and (item.get("product"), item.get("kind"), item["ref_id"]) in sources:
                continue
            if len(data["materials"]) >= MAX_MATERIALS:
                raise CreationError(400, f"素材篮里的案例表达和视频钩子最多 {MAX_MATERIALS} 条。")
            data["materials"].append(copy.deepcopy(item))
            restored.append(item["id"])
        return {"restored": restored}

    def _theme_link(self, data, body):
        value = self._value(body, "theme_link")
        identifier = value.get("id")
        old = next((item for item in data["theme_links"] if item["id"] == identifier), None)
        if not identifier and value.get("review_id") and value.get("role") and value.get("target_id"):
            old = next((item for item in data["theme_links"] if (item.get("review_id"), item.get("role"), item.get("target_id")) ==
                        (value["review_id"], value["role"], value["target_id"])), None)
            identifier = old["id"] if old else None
        if body.get("operation") == "remove":
            if not old:
                raise CreationError(404, "主题关联不存在。")
            data["theme_links"].remove(old)
            return {"removed": identifier}
        if identifier and not old:
            raise CreationError(404, "主题关联不存在。")
        item = {"id": identifier or "THM-" + uuid4().hex[:12], "product": self._product(value.get("product")),
                "theme": text(value.get("theme"), "评论主题", limit=500),
                "selling_point_id": text(value.get("selling_point_id", ""), "卖点编号", False, 200),
                "direction_id": text(value.get("direction_id", ""), "方向编号", False, 200),
                "concern": text(value.get("concern", ""), "用户顾虑", False, 5000),
                "note": text(value.get("note", ""), "关联说明", False, 5000), "updated_at": now()}
        for key in ("review_id", "role", "target_id", "target_label", "source_quote"):
            if key in value:
                item[key] = text(value[key], key, False, 12000)
        if not item["selling_point_id"] and not item["concern"]:
            raise CreationError(400, "请关联一个卖点或填写顾虑。")
        if old:
            if old["product"] != item["product"]:
                raise CreationError(409, "主题关联不能改到另一产品。")
            data["theme_links"][data["theme_links"].index(old)] = item
        else:
            data["theme_links"].append(item)
        return {"theme_link": copy.deepcopy(item)}

    def _plan(self, data, body):
        value = self._value(body, "plan")
        identifier = value.get("id")
        old = next((item for item in data["plans"] if item["id"] == identifier), None)
        if identifier and not old:
            raise CreationError(404, "待写计划不存在。")
        merged = {**(old or {}), **value}
        action, status = merged.get("action"), merged.get("status", "pending")
        if action not in PLAN_ACTIONS or status not in PLAN_STATUSES:
            raise CreationError(400, "计划动作或状态无效。")
        item = {"id": identifier or "PLN-" + uuid4().hex[:12], "product": self._product(merged.get("product")),
                "direction_id": text(merged.get("direction_id"), "方向编号", limit=200),
                "action": action, "status": status, "note": text(merged.get("note", ""), "计划说明", False, 12000),
                "task_id": text(merged.get("task_id", ""), "任务编号", False, 200),
                "script_id": text(merged.get("script_id", ""), "文案编号", False, 200),
                "created_at": old["created_at"] if old else now(), "updated_at": now()}
        if old:
            if (item["product"], item["direction_id"], item["action"]) != (old["product"], old["direction_id"], old["action"]):
                raise CreationError(409, "现有计划的产品、方向与动作不能改写，请另建计划。")
            data["plans"][data["plans"].index(old)] = item
        else:
            data["plans"].append(item)
        return {"plan": copy.deepcopy(item)}

    def _script(self, data, body, source):
        value = self._value(body, "script")
        if value.get("id"):
            raise CreationError(400, "已存文案正文不能覆盖，请用 parent_id 保存新版本。")
        if value.get("confirmed") is not True:
            raise CreationError(400, "请先核对正文及译文，再明确确认保存。")
        product = self._product(value.get("product"))
        parent = None
        if value.get("parent_id"):
            parent = next((item for item in data["scripts"] if item["id"] == value["parent_id"]), None)
            if not parent:
                raise CreationError(404, "原文案版本不存在。")
            if parent["product"] != product:
                raise CreationError(409, "新版本不能更换产品。")
        if source is not None:
            if not isinstance(source, dict) or not source.get("task_id") or not source.get("round_id") or not source.get("candidate_text"):
                raise CreationError(400, "候选来源未完成核验。")
            if product_key(source.get("product")) != product:
                raise CreationError(409, "候选与文案所属产品不一致。")
        elif value.get("task_id") or value.get("round_id"):
            raise CreationError(400, "候选来源必须由服务端核验。")
        identifier = "SCR-" + uuid4().hex[:12]
        family = parent["family_id"] if parent else identifier
        version = max((item["version"] for item in data["scripts"] if item["family_id"] == family), default=0) + 1
        provenance = copy.deepcopy(source if source is not None else parent.get("source") if parent else None)
        item = {"id": identifier, "family_id": family, "version": version, "parent_id": parent["id"] if parent else None,
                "product": product, "direction_id": text(value.get("direction_id", ""), "方向编号", False, 200),
                "title": text(value.get("title"), "文案标题", limit=500),
                "text": text(value.get("text"), "文案正文", limit=500000),
                "translation_zh": text(value.get("translation_zh", ""), "中文译文", False, 500000),
                "status": "adopted", "source": provenance,
                "video_id": "", "published_on": "", "case_id": "", "created_at": now(), "updated_at": now(),
                "history": [{"status": "adopted", "at": now(), "note": "用户核对并保存这一版本"}]}
        data["scripts"].append(item)
        return {"script": copy.deepcopy(item)}

    def _script_status(self, data, body):
        value = body.get("script_status", body)
        if not isinstance(value, dict):
            raise CreationError(400, "文案状态参数无效。")
        identifier = value.get("script_id") or value.get("id")
        item = next((item for item in data["scripts"] if item["id"] == identifier), None)
        if not item:
            raise CreationError(404, "这条文案版本不存在。")
        status = value.get("status", item["status"])
        if status not in STATUSES:
            raise CreationError(400, "文案状态无效。")
        video = text(value.get("video_id", item["video_id"]), "视频编号", False, 25)
        if video and not re.fullmatch(r"\d{6,25}", video):
            raise CreationError(400, "视频编号须为 6–25 位数字。")
        published = text(value.get("published_on", item["published_on"]), "发布日期", False, 10)
        if published:
            try:
                date.fromisoformat(published)
            except ValueError as exc:
                raise CreationError(400, "发布日期须为 YYYY-MM-DD。") from exc
        case_id = text(value.get("case_id", item["case_id"]), "案例编号", False, 100)
        if case_id and not re.fullmatch(r"PVR-[0-9a-f]{12}", case_id):
            raise CreationError(400, "案例编号无效。")
        if status in {"published", "reviewed"} and (not video or not published):
            raise CreationError(400, "登记已发布或已复盘前，请填写视频编号和发布日期。")
        note = text(value.get("note", ""), "状态说明", False, 12000)
        event = {"status": status, "at": now(), "note": note, "video_id": video, "published_on": published, "case_id": case_id}
        item.update(status=status, video_id=video, published_on=published, case_id=case_id, updated_at=event["at"])
        item["history"].append(event)
        return {"script": copy.deepcopy(item)}

    def forget_case(self, identifier):
        """Erase owned snapshots after permanent case deletion, including internal backups."""
        def scrub_script(item):
            source = item.get("source") or {}
            if source.get("case_id") == identifier:
                for key in ("snapshot", "candidate_text", "case_id"):
                    source.pop(key, None)
                source.update(deleted_case_id=identifier, source_deleted=True)
            else:
                context = (source.get("snapshot") or {}).get("creation_context") or {}
                if context.get("materials"):
                    context["materials"] = [material for material in context["materials"] if not
                        any(str(material.get(key) or "").startswith(identifier) for key in ("source_id", "ref_id", "case_id"))]
            if item.get("case_id") == identifier:
                item["case_id"] = ""
            for event in item.get("history", []):
                if event.get("case_id") == identifier:
                    event["case_id"] = ""

        def clean(data):
            before = fingerprint(data)
            for item in data["scripts"]:
                scrub_script(item)
            data["materials"] = [item for item in data["materials"] if not
                                 (item["kind"] == "case" and item["ref_id"].startswith(identifier))]
            # Keep unrelated and adopted-work receipts valid, while erasing deleted source shadows.
            for receipt in data["requests"].values():
                result = receipt.get("result") or {}
                if result.get("script"):
                    scrub_script(result["script"])
                material = result.get("material") or {}
                if material.get("kind") == "case" and str(material.get("ref_id") or "").startswith(identifier):
                    receipt["result"] = {"removed": material.get("id"), "source_deleted": True}
            return fingerprint(data) != before
        with self.lock:
            if self.path.exists():
                data = self.read()
                if clean(data):
                    self.write(data, backup=False)
            for path in (self.root / "backups").glob("*.json"):
                data = json.loads(path.read_text(encoding="utf-8"))
                if clean(data):
                    self._replace(path, data)
