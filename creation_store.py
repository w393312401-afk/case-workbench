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
from file_identity import file_identity


class CreationError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


STATUSES = {"draft", "adopted", "filmed", "published", "reviewed", "archived"}
PLAN_ACTIONS = {"scale", "execution", "stop"}
PLAN_STATUSES = {"pending", "working", "done", "cancelled"}
EXPERIMENT_METRICS = {"views", "clicks", "orders", "gmv", "ctr_pct", "ctor_pct", "gpm"}
EXPERIMENT_DECISIONS = {"observe", "scale", "execution", "stop"}
# Frozen context for an explicitly chosen direction variant. The index is only
# its position at selection time; readers also match the snapshot before crediting work.
PLAN_VARIANT_SNAPSHOT_KEYS = ("progression", "baseline", "variant_type", "main_difference",
                              "concern", "closing", "execution_change")
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
        self._catalog_signature = None
        self._catalog_value = None
        self._catalog_revision = None

    @staticmethod
    def empty():
        return {"schema_version": 1, "scripts": [], "materials": [], "theme_links": [], "plans": [], "variant_links": [], "requests": {}}

    def read(self):
        with self.lock:
            return copy.deepcopy(self._read_catalog())

    def clear_cache(self):
        """Discard snapshots after a runtime restore or an in-process replacement."""
        with self.lock:
            self._catalog_signature = self._catalog_value = self._catalog_revision = None

    def _signature(self):
        return file_identity(self.path)

    def _read_catalog(self):
        """Internal read-only snapshot; public readers always copy the data they return."""
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
                    any(not isinstance(value.get(key), list) for key in ("scripts", "materials", "theme_links", "plans")) or
                    not isinstance(value.get("requests"), dict) or
                    not isinstance(value.get("variant_links", []), list)):
                raise ValueError("invalid creation catalog")
            value.setdefault("variant_links", [])
            # Never pin a read that raced another process's catalog replacement.
            if signature == self._signature():
                self._catalog_signature, self._catalog_value = signature, value
                self._catalog_revision = None
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise CreationError(500, "创作资料无法读取，请保留原文件并从备份恢复。") from exc

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
        """Return the current catalog revision without copying saved request receipts."""
        with self.lock:
            return self._snapshot_revision(self._read_catalog())

    def product_inventory(self):
        """Read only product identifiers, without materializing public records or receipts."""
        with self.lock:
            data = self._read_catalog()
            return sorted({product_key(item.get("product"))
                           for group in ("scripts", "materials", "theme_links", "plans", "variant_links")
                           for item in data.get(group, [])} - {"", "all"})

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
                self.clear_cache()
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
            data = self._read_catalog()
            selected = product_key(product) if product and product != "all" else None
            result = {}
            for key, value in data.items():
                if key in {"requests", "schema_version"}:
                    continue
                rows = value if selected is None else [item for item in value if item.get("product") == selected]
                result[key] = ([self.script_lifecycle(item) for item in rows] if key == "scripts"
                               else copy.deepcopy(rows))
            result["revision"] = self._snapshot_revision(data)
            return result

    @staticmethod
    def script_lifecycle(item):
        """Project legacy archive statuses without silently migrating business data on read."""
        item = copy.deepcopy(item)
        status = item.get("status", "draft")
        if status == "archived":
            status = next((entry["status"] for entry in reversed(item.get("history", []))
                           if entry.get("status") in STATUSES - {"archived"}), "draft")
            # Old archive rows may have no earlier progress or a damaged release binding.
            # Showing them in history must not invent evidence that the script was published.
            if status in {"published", "reviewed"} and (not item.get("video_id") or not item.get("published_on")):
                status = "draft"
        item["archived"] = item.get("archived") is True or item.get("status") == "archived"
        item["lifecycle_status"] = status
        return item

    def get_script(self, identifier):
        with self.lock:
            item = next((item for item in self._read_catalog()["scripts"] if item["id"] == identifier), None)
            if not item:
                raise CreationError(404, "这条文案版本不存在。")
            return self.script_lifecycle(item)

    @staticmethod
    def _candidate_association(candidate_id, product, direction_id, variant_ref, *, script_id=None):
        """Validate the server-checked association separately from frozen script provenance."""
        if not isinstance(candidate_id, str) or not re.fullmatch(r"TKC-[0-9a-f]{12}", candidate_id):
            raise CreationError(400, "插件候选编号格式无效。")
        product = CreationStore._product(product)
        if product == "all":
            raise CreationError(400, "请先选择具体产品。")
        association = {"candidate_id": candidate_id,
                       "direction_id": text(direction_id, "方向编号", limit=200),
                       "variant_ref": CreationStore._plan_variant_ref(variant_ref)}
        if script_id is not None:
            association["script_id"] = text(script_id, "文案编号", limit=200)
        return product, association

    @staticmethod
    def _same_variant_identity(left, right):
        """Indices and presentation annotations may change without changing the tested chain."""
        try:
            left = CreationStore._plan_variant_ref(left)["snapshot"]
            right = CreationStore._plan_variant_ref(right)["snapshot"]
        except CreationError:
            return False
        return all(left.get(key, "") == right.get(key, "") for key in
                   ("main_difference", "concern", "closing", "variant_type", "execution_change"))

    @staticmethod
    def _same_candidate_association(left, right):
        return (isinstance(left, dict) and
                all(left.get(key) == right.get(key) for key in ("candidate_id", "direction_id", "script_id")) and
                CreationStore._same_variant_identity(left.get("variant_ref"), right.get("variant_ref")))

    def associate_candidate(self, candidate_id, *, script_id, product, direction_id, variant_ref, reference_url,
                            dry_run=False):
        """Fill reviewed direction metadata and a standard link, preserving the adopted version."""
        product, association = self._candidate_association(candidate_id, product, direction_id, variant_ref)
        script_id = text(script_id, "文案编号", limit=200)
        reference_url = text(reference_url, "参考视频来源", False, 1000)
        with self.lock:
            data = self.read()
            script = next((item for item in data["scripts"] if item["id"] == script_id), None)
            if not script:
                raise CreationError(404, "这条文案版本不存在。")
            if script.get("product") != product:
                raise CreationError(409, "候选与文案所属产品不一致，未补关联。")
            if script.get("direction_id") and script["direction_id"] != association["direction_id"]:
                raise CreationError(409, "文案已有其他方向，不能覆盖。")
            previous = script.get("candidate_direction")
            if previous is not None and not self._same_candidate_association(previous, association):
                raise CreationError(409, "文案已有其他候选方向或变体关联，不能覆盖。")
            frozen_ref = (script.get("source") or {}).get("variant_ref")
            if frozen_ref is not None and not self._same_variant_identity(frozen_ref, association["variant_ref"]):
                raise CreationError(409, "文案原始来源已指定其他变体，不能覆盖关联。")
            links = [row for row in data["variant_links"]
                     if row.get("kind") == "script" and row.get("record_id") == script_id]
            if len(links) > 1:
                raise CreationError(409, "文案存在重复变体关联，请先核对原记录。")
            if links and (links[0].get("product") != product or
                          links[0].get("direction_id") != association["direction_id"] or
                          not self._same_variant_identity(links[0].get("variant_ref"), association["variant_ref"])):
                raise CreationError(409, "文案已有其他方向或变体关联，不能覆盖。")
            changed = not script.get("direction_id") or previous is None or not links
            if not links:
                self._variant_link(data, {"link": {"kind": "script", "record_id": script_id, "product": product,
                    "direction_id": association["direction_id"], "variant_ref": association["variant_ref"],
                    "evidence": {"note": "按已核对的插件候选自动归入对应方向。",
                                 "refs": [candidate_id] + ([reference_url] if reference_url else [])}}})
            script["direction_id"] = association["direction_id"]
            if previous is None:
                script["candidate_direction"] = copy.deepcopy(association)
            history = script.setdefault("history", [])
            if not any(row.get("event") == "candidate_direction" and row.get("candidate_id") == candidate_id
                       for row in history):
                changed = True
                history.append({"at": now(), "status": script.get("status", "adopted"),
                    "event": "candidate_direction", "note": "按插件候选自动归入对应方向；正文和版本编号保持不变。",
                    **copy.deepcopy(association)})
            if changed:
                script["updated_at"] = now()
                if not dry_run:
                    self.write(data)
            return copy.deepcopy(script)

    def replay(self, kind, body):
        """Return a durable receipt before the caller resolves a now-changed source."""
        with self.lock:
            prior = self._read_catalog()["requests"].get(body.get("request_id")) if isinstance(body, dict) else None
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
                        "script": lambda d, b: self._script(d, b, source), "script_status": self._script_status,
                        "script_sources": lambda d, b: self._script_sources(d, b, source),
                        "variant_link": self._variant_link, "script_delete": self._script_delete}
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
        # Case expressions and video hooks only; reviews stay in the review store's picks.
        if value.get("kind") not in {"case", "video"}:
            raise CreationError(400, "素材类型无效；素材篮只收案例表达和视频钩子，评论继续使用原摘录夹。")
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

    def _variant_link(self, data, body):
        """A reviewed association is separate from immutable creation provenance."""
        value = self._value(body, "link")
        kind = value.get("kind")
        if not isinstance(kind, str) or kind not in {"script", "production", "video"}:
            raise CreationError(400, "历史记录类型无效。")
        record_id = text(value.get("record_id"), "历史记录编号", limit=200)
        product = self._product(value.get("product"))
        if product == "all":
            raise CreationError(400, "请先选择具体产品。")
        evidence = value.get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"note", "refs"}:
            raise CreationError(400, "请填写关联核对依据与来源。")
        refs = evidence.get("refs")
        if not isinstance(refs, list) or not refs or len(refs) > 50:
            raise CreationError(400, "请保留至少一项关联核对来源。")
        evidence = {"note": text(evidence.get("note"), "关联核对依据", limit=6000),
                    "refs": list(dict.fromkeys(text(ref, "关联核对来源", limit=1000) for ref in refs))}
        old = next((row for row in data["variant_links"]
                    if row["kind"] == kind and row["record_id"] == record_id), None)
        if old and old["product"] != product:
            raise CreationError(409, "历史记录的变体关联不能改到另一产品。")
        item = {"id": old["id"] if old else "VLK-" + uuid4().hex[:12], "kind": kind,
                "record_id": record_id, "product": product,
                "direction_id": text(value.get("direction_id"), "方向编号", limit=200),
                "variant_ref": self._plan_variant_ref(value.get("variant_ref")), "evidence": evidence,
                "created_at": old["created_at"] if old else now(), "updated_at": now(),
                "history": copy.deepcopy(old.get("history", [])) if old else []}
        if old:
            item["history"].append({key: copy.deepcopy(entry) for key, entry in old.items() if key != "history"})
            data["variant_links"][data["variant_links"].index(old)] = item
        else:
            data["variant_links"].append(item)
        return {"link": copy.deepcopy(item)}

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
        if old and old.get("source_review") is not None:
            if "source_review" in value and value["source_review"] != old["source_review"]:
                raise CreationError(409, "已有计划的复盘来源不能改写或清空，请另建计划。")
            item["source_review"] = copy.deepcopy(old["source_review"])
        elif "source_review" in merged:
            item["source_review"] = self._plan_review_source(merged["source_review"])
        if old and old.get("variant_ref") is not None:
            if "variant_ref" in value and value["variant_ref"] != old["variant_ref"]:
                raise CreationError(409, "已有计划的变体归属不能改写或清空，请另建计划。")
            item["variant_ref"] = copy.deepcopy(old["variant_ref"])
        elif "variant_ref" in merged:
            item["variant_ref"] = self._plan_variant_ref(merged["variant_ref"])
        if merged.get("experiment") is not None:
            experiment = self._experiment(merged["experiment"])
            item["experiment"] = experiment
            item["experiment_history"] = copy.deepcopy((old or {}).get("experiment_history", []))
            if experiment != (old or {}).get("experiment"):
                item["experiment_history"].append({"at": item["updated_at"], "experiment": copy.deepcopy(experiment)})
        elif old and old.get("experiment"):
            raise CreationError(400, "已有测试卡不能移除，请编辑内容或取消计划。")
        if old:
            if (item["product"], item["direction_id"], item["action"]) != (old["product"], old["direction_id"], old["action"]):
                raise CreationError(409, "现有计划的产品、方向与动作不能改写，请另建计划。")
            data["plans"][data["plans"].index(old)] = item
        else:
            data["plans"].append(item)
        return {"plan": copy.deepcopy(item)}

    @staticmethod
    def _plan_review_source(value):
        if not isinstance(value, dict) or set(value) != {"product", "video_id", "window_end", "reviewed_at", "note"}:
            raise CreationError(400, "复盘来源格式无效，请重新读取复盘记录。")
        source = {"product": product_key(text(value.get("product"), "复盘产品", limit=200)),
                  "video_id": text(value.get("video_id"), "复盘视频编号", limit=25),
                  "window_end": text(value.get("window_end"), "复盘观察窗", limit=10),
                  "reviewed_at": text(value.get("reviewed_at"), "复盘记录时间", limit=100),
                  "note": text(value.get("note"), "复盘结论", limit=4000)}
        if source["product"] == "all" or not re.fullmatch(r"\d{6,25}", source["video_id"]):
            raise CreationError(400, "复盘产品或视频编号无效。")
        try:
            if date.fromisoformat(source["window_end"]).isoformat() != source["window_end"]:
                raise ValueError()
        except ValueError as exc:
            raise CreationError(400, "复盘观察窗须为 YYYY-MM-DD。") from exc
        return source

    @staticmethod
    def _plan_variant_ref(value):
        """Check shape here; the workbench validates against the live product/direction."""
        if (not isinstance(value, dict) or set(value) != {"index", "snapshot"} or
                type(value.get("index")) is not int or value["index"] < 0):
            raise CreationError(400, "测试变体归属格式无效，请重新选择变体。")
        snapshot = value.get("snapshot")
        if (not isinstance(snapshot, dict) or not snapshot or
                set(snapshot) - set(PLAN_VARIANT_SNAPSHOT_KEYS) or
                any(not isinstance(snapshot.get(key), str) or not snapshot[key].strip()
                    for key in ("main_difference", "concern", "closing", "variant_type"))):
            raise CreationError(400, "测试变体快照格式无效，请重新选择变体。")
        for key, item in snapshot.items():
            if key == "baseline":
                if type(item) is not bool:
                    raise CreationError(400, "测试变体快照格式无效，请重新选择变体。")
            elif not isinstance(item, str) or len(item) > 12000:
                raise CreationError(400, "测试变体快照格式无效，请重新选择变体。")
        return copy.deepcopy(value)

    @staticmethod
    def _experiment(value):
        if not isinstance(value, dict):
            raise CreationError(400, "测试卡格式无效。")
        item = {"hypothesis": text(value.get("hypothesis"), "测试假设", limit=3000),
                "change": text(value.get("change"), "主要改动", limit=3000),
                "metric": value.get("metric", "ctor_pct"),
                "decision": value.get("decision", "observe"),
                "decision_note": text(value.get("decision_note", ""), "决定依据", False, 6000)}
        if (not isinstance(item["metric"], str) or item["metric"] not in EXPERIMENT_METRICS or
                not isinstance(item["decision"], str) or item["decision"] not in EXPERIMENT_DECISIONS):
            raise CreationError(400, "测试指标或决定无效。")
        if item["decision"] != "observe" and not item["decision_note"]:
            raise CreationError(400, "请填写这次决定的依据。")
        for key in ("baseline_video_id", "test_video_id"):
            item[key] = text(value.get(key, ""), "测试视频编号", False, 25)
            if item[key] and not re.fullmatch(r"\d{6,25}", item[key]):
                raise CreationError(400, "测试视频编号须为 6–25 位数字。")
        if item["baseline_video_id"] and item["baseline_video_id"] == item["test_video_id"]:
            raise CreationError(400, "基准视频与测试视频须为不同视频。")
        for key in ("window_start", "window_end"):
            item[key] = text(value.get(key, ""), "比较日期", False, 10)
            if item[key]:
                try:
                    date.fromisoformat(item[key])
                except ValueError as exc:
                    raise CreationError(400, "比较日期须为 YYYY-MM-DD。") from exc
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item[key]):
                    raise CreationError(400, "比较日期须为 YYYY-MM-DD。")
        if bool(item["window_start"]) != bool(item["window_end"]):
            raise CreationError(400, "请同时填写比较开始与结束日期，或同时留空。")
        if item["window_start"] and (item["window_end"] < item["window_start"] or
                (date.fromisoformat(item["window_end"]) - date.fromisoformat(item["window_start"])).days > 366):
            raise CreationError(400, "比较日期须按先后顺序，且不超过 367 天。")
        return item

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
        if source is not None and parent is None:
            # Adopting the same candidate text again (a retried 制作图文 or 制作视频) keeps the version
            # already adopted, so one variant never becomes several identical scripts.
            body_text = text(value.get("text"), "文案正文", limit=500000)
            translation = text(value.get("translation_zh", ""), "中文译文", False, 500000)
            same = next((item for item in reversed(data["scripts"])
                         if not item.get("archived") and item.get("status") != "archived" and item["product"] == product
                         and (item.get("source") or {}).get("task_id") == source["task_id"]
                         and (item.get("source") or {}).get("round_id") == source["round_id"]
                         and item["text"] == body_text and item.get("translation_zh", "") == translation), None)
            if same:
                return {"script": copy.deepcopy(same), "reused": True}
        identifier = "SCR-" + uuid4().hex[:12]
        family = (parent.get("family_id") or parent["id"]) if parent else identifier
        version = max((item.get("version", 1) for item in data["scripts"] if (item.get("family_id") or item["id"]) == family), default=0) + 1
        provenance = copy.deepcopy(source if source is not None else parent.get("source") if parent else None)
        item = {"id": identifier, "family_id": family, "version": version, "parent_id": parent["id"] if parent else None,
                "product": product, "direction_id": text(value.get("direction_id", ""), "方向编号", False, 200),
                "title": text(value.get("title"), "文案标题", limit=500),
                "text": text(value.get("text"), "文案正文", limit=500000),
                "translation_zh": text(value.get("translation_zh", ""), "中文译文", False, 500000),
                "status": "adopted", "archived": False, "source": provenance,
                "video_id": "", "published_on": "", "case_id": "", "created_at": now(), "updated_at": now(),
                "history": [{"status": "adopted", "at": now(), "note": "用户核对并保存这一版本"}]}
        data["scripts"].append(item)
        return {"script": copy.deepcopy(item)}

    def _script_sources(self, data, body, sources):
        """Attach checked candidate provenance without changing adopted text or version IDs."""
        if not isinstance(sources, list) or not sources:
            raise CreationError(400, "缺少已核对的脚本来源。")
        identifiers = [entry.get("script_id") for entry in sources if isinstance(entry, dict)]
        if (len(identifiers) != len(sources) or any(not isinstance(item, str) for item in identifiers)
                or len(set(identifiers)) != len(identifiers)):
            raise CreationError(400, "脚本来源关联有重复或无效条目。")
        changed = []
        for entry in sources:
            script = next((item for item in data["scripts"] if item["id"] == entry["script_id"]), None)
            provenance = entry.get("source")
            if script is None:
                raise CreationError(404, "要补关联的脚本版本不存在。")
            if (not isinstance(provenance, dict) or not provenance.get("task_id")
                    or not provenance.get("round_id") or not provenance.get("candidate_text")
                    or type(provenance.get("variant_index")) is not int):
                raise CreationError(400, "候选来源未完成核验。")
            if (script["product"] != product_key(provenance.get("product"))
                    or script.get("direction_id", "") != provenance.get("direction_id", "")):
                raise CreationError(409, "候选与脚本的产品或方向不同，未补关联。")
            if script["text"] != entry.get("text") or script.get("translation_zh", "") != entry.get("translation_zh"):
                raise CreationError(409, "脚本正文或译文已变化，请重新核对来源。")
            if script.get("source"):
                if script["source"] != provenance:
                    raise CreationError(409, "脚本已有其他候选来源，不能覆盖。")
                changed.append(copy.deepcopy(script))
                continue
            stamp = now()
            script.update(source=copy.deepcopy(provenance), updated_at=stamp)
            script.setdefault("history", []).append({"status": script["status"], "at": stamp,
                "event": "source_import", "note": "补关联已完成的对话导入稿及意群表；正文和版本编号保持不变。",
                "task_id": provenance["task_id"], "round_id": provenance["round_id"],
                "variant_index": provenance["variant_index"]})
            changed.append(copy.deepcopy(script))
        return {"scripts": changed}

    @staticmethod
    def family_blocker(data, family):
        """Why a script family must not be deleted for good, or "": a case, a posted video or a plan uses it."""
        rows = [item for item in data["scripts"] if (item.get("family_id") or item["id"]) == family]
        if not rows:
            return "这组脚本已不存在。"
        ids = {item["id"] for item in rows}
        if any(item.get("case_id") for item in rows):
            return "有版本已转成案例，保留。"
        if any(item.get("video_id") or item.get("published_on") for item in rows):
            return "有版本已登记视频 ID，保留。"
        if any(plan.get("script_id") in ids for plan in data["plans"]):
            return "被写稿计划引用，保留。"
        return ""

    def _script_delete(self, data, body):
        """Remove one script family for good with its reviewed variant links; the caller has checked orders."""
        family = body.get("family_id")
        problem = self.family_blocker(data, family) if isinstance(family, str) else "这组脚本已不存在。"
        if problem:
            raise CreationError(404 if problem == "这组脚本已不存在。" else 409, problem)
        ids = {item["id"] for item in data["scripts"] if (item.get("family_id") or item["id"]) == family}
        data["scripts"] = [item for item in data["scripts"] if item["id"] not in ids]
        data["variant_links"] = [row for row in data["variant_links"]
                                 if not (row.get("kind") == "script" and row.get("record_id") in ids)]
        return {"family_id": family, "script_ids": sorted(ids)}

    def _script_status(self, data, body):
        value = body.get("script_status", body)
        if not isinstance(value, dict):
            raise CreationError(400, "文案状态参数无效。")
        identifier = value.get("script_id") or value.get("id")
        item = next((item for item in data["scripts"] if item["id"] == identifier), None)
        if not item:
            raise CreationError(404, "这条文案版本不存在。")
        lifecycle = self.script_lifecycle(item)
        status = value.get("status", lifecycle["lifecycle_status"])
        if status not in STATUSES:
            raise CreationError(400, "文案状态无效。")
        if "archived" in value and type(value["archived"]) is not bool:
            raise CreationError(400, "文案归档开关须为布尔值。")
        archived = value.get("archived", lifecycle["archived"])
        if status == "archived":
            # Keep the legacy status action usable while making archive independent of progress.
            archived, status = True, lifecycle["lifecycle_status"]
        elif item["status"] == "archived" and "status" in value and "archived" not in value:
            archived = False
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
        if archived != lifecycle["archived"]:
            event.update(event="archive" if archived else "restore", archived=archived)
        item.update(status=status, archived=archived, video_id=video, published_on=published, case_id=case_id, updated_at=event["at"])
        item.setdefault("history", []).append(event)
        return {"script": self.script_lifecycle(item)}

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
