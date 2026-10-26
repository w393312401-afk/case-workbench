"""Reusable review lessons with immutable provenance and versioned human edits.

runtime/creation/experiences is business data, included in the existing full-data
backup. Selection copies records into a task; it never turns lessons into rules.
"""
from __future__ import annotations

import copy
from datetime import date, datetime
import json
import re
from uuid import uuid4

from creation_store import CreationStore, CreationError, text
from direction_store import fingerprint, now, product_key
from review_followups import REFERENCE_VALUES


EXPERIENCE_ID = re.compile(r"EXP-[0-9a-f]{12}")
KINDS = {"success", "failure"}
CONFIDENCE = {"insufficient", "preliminary", "supported"}
SCOPES = {"direction", "product"}
STATUSES = {"active", "retired"}
FIELDS = ("product", "kind", "dimension", "practice", "conditions", "evidence_note",
          "confidence", "scope", "direction_id", "status")
TASK_FIELDS = ("id", "version", *FIELDS, "source", "created_at", "updated_at")


def _product(value):
    result = product_key(text(value, "产品", limit=200))
    if result.lower() == "all":
        raise CreationError(400, "请指定经验所属的具体产品。")
    return result


def _same_product(left, right):
    return product_key(left).casefold() == product_key(right).casefold()


def _choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise CreationError(400, f"{label}无效。")
    return value


def _source(value, product):
    """The application resolves the source; request experience.source is never accepted."""
    if not isinstance(value, dict) or not isinstance(value.get("review"), dict):
        raise CreationError(400, "缺少已核对的来源复盘。")
    review = value["review"]
    if not _same_product(_product(review.get("product")), product):
        raise CreationError(400, "经验与来源复盘必须属于同一产品。")
    video_id = text(review.get("video_id"), "来源视频编号", limit=25)
    if not re.fullmatch(r"\d{6,25}", video_id):
        raise CreationError(400, "来源视频编号无效。")
    window = text(review.get("window_end"), "来源观察窗", limit=10)
    reviewed_at = text(review.get("reviewed_at"), "来源复盘时间", limit=64)
    try:
        if date.fromisoformat(window).isoformat() != window:
            raise ValueError()
        if datetime.fromisoformat(reviewed_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise CreationError(400, "来源观察窗或复盘时间无效。") from exc
    if not isinstance(value.get("video", {}), dict) or not isinstance(value.get("publications", []), list):
        raise CreationError(400, "来源视频与发布记录格式无效。")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise CreationError(413, "来源快照过大，未保存经验。")
    except (TypeError, ValueError, OverflowError) as exc:
        if isinstance(exc, CreationError):
            raise
        raise CreationError(400, "来源快照格式无效。") from exc
    result = copy.deepcopy(value)
    result["review"]["product"] = product_key(product)
    return result


def _fields(value):
    product = _product(value.get("product"))
    scope = _choice(value.get("scope"), SCOPES, "适用范围")
    direction = text(value.get("direction_id", ""), "适用方向", scope == "direction", 200)
    return {"product": product, "kind": _choice(value.get("kind"), KINDS, "经验类型"),
            "dimension": text(value.get("dimension", ""), "经验环节", False, 100),
            "practice": text(value.get("practice"), "具体做法", limit=3000),
            "conditions": text(value.get("conditions", ""), "适用条件", False, 3000),
            "evidence_note": text(value.get("evidence_note", ""), "证据与限制", False, 4000),
            "confidence": _choice(value.get("confidence"), CONFIDENCE, "证据状态"),
            "scope": scope, "direction_id": direction,
            "status": _choice(value.get("status", "active"), STATUSES, "经验状态")}


class ReviewExperienceStore(CreationStore):
    def __init__(self, runtime, lock=None):
        super().__init__(runtime, lock)
        self.root = self.root / "experiences"
        self.path = self.root / "catalog.json"

    @staticmethod
    def empty():
        return {"schema_version": 1, "items": [], "requests": {}}

    def _read_catalog(self):
        try:
            try:
                signature = self._signature()
            except FileNotFoundError:
                self.clear_cache()
                return self.empty()
            if signature == self._catalog_signature:
                return self._catalog_value
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(data, dict) or data.get("schema_version") != 1 or
                    not isinstance(data.get("items"), list) or not isinstance(data.get("requests"), dict)):
                raise ValueError()
            identifiers = set()
            for item in data["items"]:
                if (not isinstance(item, dict) or not EXPERIENCE_ID.fullmatch(str(item.get("id", ""))) or
                        item["id"] in identifiers or type(item.get("version")) is not int or item["version"] < 1 or
                        not isinstance(item.get("history"), list)):
                    raise ValueError()
                identifiers.add(item["id"])
                _fields(item)
                _source(item.get("source"), item["product"])
                for revision in item["history"]:
                    if (not isinstance(revision, dict) or revision.get("id") != item["id"] or
                            type(revision.get("version")) is not int or not 1 <= revision["version"] < item["version"]):
                        raise ValueError()
                    _fields(revision)
                    if revision.get("source") != item["source"]:
                        raise ValueError()
            if signature == self._signature():
                self._catalog_signature, self._catalog_value = signature, data
                self._catalog_revision = None
            return data
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise CreationError(500, "复盘经验无法读取，请保留原文件并从备份恢复。") from exc

    def product_inventory(self):
        with self.lock:
            return sorted({row["product"] for row in self._read_catalog()["items"]})

    def public(self, product=None, query="", kind="", status="active", direction_id=""):
        query = text(query, "检索词", False, 500).casefold()
        direction_id = text(direction_id, "适用方向筛选", False, 200)
        if kind:
            _choice(kind, KINDS, "经验类型筛选")
        _choice(status, {*STATUSES, "all"}, "经验状态筛选")
        selected = _product(product) if product and str(product).strip().lower() != "all" else None
        with self.lock:
            data = self._read_catalog()
            items = []
            for item in data["items"]:
                if (selected and not _same_product(item["product"], selected) or
                        kind and item["kind"] != kind or status != "all" and item["status"] != status or
                        direction_id and item["direction_id"] != direction_id):
                    continue
                source = item["source"]
                haystack = "\n".join(str(value or "") for value in (
                    item["practice"], item["conditions"], item["evidence_note"], item["dimension"],
                    item["direction_id"], source["review"].get("video_id"), source["review"].get("note"),
                    source.get("video", {}).get("title"), source.get("video", {}).get("headline")))
                if query and query not in haystack.casefold():
                    continue
                items.append(copy.deepcopy(item))
            items.sort(key=lambda row: (row.get("updated_at", ""), row["id"]), reverse=True)
            return {"items": items, "total": len(items), "revision": self._snapshot_revision(data)}

    @staticmethod
    def _dedup_key(item):
        review = item["source"]["review"]
        return (product_key(item["product"]).casefold(), review["video_id"], review["window_end"], review["reviewed_at"],
                item["kind"], item["dimension"].casefold(), item["practice"].casefold())

    def save(self, body, source=None):
        if (not isinstance(body, dict) or not isinstance(body.get("request_id"), str) or
                not re.fullmatch(r"[\w-]{10,100}", body["request_id"])):
            raise CreationError(400, "缺少保存标识，请刷新后重试。")
        with self.lock:
            prior = self.replay("experience", body)
            if prior:
                return prior
            data = self.read()
            if body.get("revision") != self.revision(data):
                raise CreationError(409, "复盘经验已更新，请重新读取后再保存。")
            value = body.get("experience")
            if not isinstance(value, dict) or set(value) - {*FIELDS, "id"}:
                raise CreationError(400, "经验字段无效；来源由已保存的复盘确定，不能直接填写或覆盖。")
            identifier = value.get("id")
            if identifier and (not isinstance(identifier, str) or not EXPERIENCE_ID.fullmatch(identifier)):
                raise CreationError(400, "经验编号无效。")
            old = next((row for row in data["items"] if row["id"] == identifier), None) if identifier else None
            if identifier and old is None:
                raise CreationError(404, "未找到这条复盘经验。")
            fields = _fields({**(old or {}), **value})
            stamp = now()
            duplicate = False
            if old:
                if not _same_product(fields["product"], old["product"]):
                    raise CreationError(400, "已有经验不能更换所属产品。")
                fields["product"] = old["product"]
                if source is not None and _source(source, old["product"]) != old["source"]:
                    raise CreationError(400, "已有经验不能更换来源复盘。")
                candidate = {**fields, "source": old["source"]}
                if any(row["id"] != identifier and self._dedup_key(row) == self._dedup_key(candidate) for row in data["items"]):
                    raise CreationError(409, "相同来源与做法已有经验，请修改原经验。")
                if all(fields[key] == old[key] for key in FIELDS):
                    item, duplicate = old, True
                else:
                    previous = {key: copy.deepcopy(val) for key, val in old.items() if key != "history"}
                    item = {**old, **fields, "version": old["version"] + 1, "updated_at": stamp,
                            "history": [*old["history"], previous]}
                    data["items"][data["items"].index(old)] = item
            else:
                frozen_source = _source(source, fields["product"])
                candidate = {**fields, "source": frozen_source}
                existing = next((row for row in data["items"] if self._dedup_key(row) == self._dedup_key(candidate)), None)
                if existing:
                    if any(fields[key] != existing[key] for key in FIELDS):
                        raise CreationError(409, "相同来源与做法已有经验，请编辑已有经验以更新条件或证据。")
                    item, duplicate = existing, True
                else:
                    item = {"id": "EXP-" + uuid4().hex[:12], "version": 1, **fields, "source": frozen_source,
                            "created_at": stamp, "updated_at": stamp, "history": []}
                    data["items"].append(item)
            result = {"item": copy.deepcopy(item), "revision": self.revision(data), "duplicate": duplicate}
            data["requests"][body["request_id"]] = {"hash": fingerprint(["experience", body]), "result": copy.deepcopy(result), "at": stamp}
            self._prune_requests(data)
            self.write(data)
            return result

    def selected_for_task(self, product, direction_id="", limit=8, source_filter=None, reference_value_for_item=None):
        selected = _product(product)
        direction_id = text(direction_id, "写稿或审稿方向", False, 200)
        if type(limit) is not int or not 0 <= limit <= 20:
            raise CreationError(400, "引用经验条数须为0至20。")
        if source_filter is not None and not callable(source_filter):
            raise CreationError(400, "经验来源校验无效。")
        if reference_value_for_item is not None and not callable(reference_value_for_item):
            raise CreationError(400, "经验参考价值读取无效。")
        with self.lock:
            candidates = [row for row in self._read_catalog()["items"] if row["status"] == "active" and _same_product(row["product"], selected)
                    and (row["scope"] == "product" or bool(direction_id) and row["direction_id"] == direction_id)
                    and (source_filter is None or source_filter(copy.deepcopy(row)))]
            rows = []
            for row in candidates:
                value = (reference_value_for_item(copy.deepcopy(row)) if reference_value_for_item is not None
                         else row["source"]["review"].get("reference_value", ""))
                _choice(value, REFERENCE_VALUES, "经验参考价值")
                if value != "none":
                    rows.append({**row, "reference_value": value})
            confidence = {"supported": 0, "preliminary": 1, "insufficient": 2}
            reference_rank = {"high": 0, "medium": 1, "weak": 2, "": 3}
            # Human reference value prioritizes reuse; it never upgrades evidence.
            rows.sort(key=lambda row: (row["updated_at"], row["id"]), reverse=True)
            rows.sort(key=lambda row: (reference_rank[row["reference_value"]], confidence[row["confidence"]], row["scope"] != "direction"))
            buckets = {kind: [row for row in rows if row["kind"] == kind] for kind in ("success", "failure")}
            chosen = []
            # Round-robin keeps a popular positive example from hiding all warnings.
            # Within each kind, reference value, evidence and exact direction lead.
            while len(chosen) < limit and any(buckets.values()):
                available = [kind for kind in ("success", "failure") if buckets[kind]]
                available.sort(key=lambda kind: (sum(row["kind"] == kind for row in chosen),
                                                  reference_rank[buckets[kind][0]["reference_value"]],
                                                  confidence[buckets[kind][0]["confidence"]],
                                                  buckets[kind][0]["scope"] != "direction", kind))
                chosen.append(buckets[available[0]].pop(0))
            return [{key: copy.deepcopy(row[key]) for key in (*TASK_FIELDS, "reference_value")} for row in chosen]
