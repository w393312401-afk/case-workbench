"""Shared saved content filters, kept with the workbench's creation data.

Legacy browser imports are additive. Remembered IDs survive deletion so an old
browser cannot restore a view which another device has already removed.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading

from creation_store import CreationError
from direction_store import fingerprint, product_key
from elevenlabs_client import VoiceError
from production_operations import durable_json, file_lock


MAX_VIEWS = 30
MAX_NAME_LENGTH = 40
VIEW_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}\Z")
PRESET_NAMES = frozenset({"待处理", "进行中", "待发布", "全部"})


def view_product(value):
    if not isinstance(value, str) or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise CreationError(400, "视图所属产品无效。")
    return product_key(value)


def view_id(value):
    if not isinstance(value, str) or not VIEW_ID.fullmatch(value):
        raise CreationError(400, "视图编号无效。")
    return value


class ContentViewsStore:
    def __init__(self, runtime_root, lock=None, *, filter_validator):
        self.root = Path(runtime_root) / "creation"
        self.path = self.root / "content-views.json"
        self.lock = lock or threading.RLock()
        # The content endpoint owns the filter vocabulary; inject its parser to
        # validate both requests and restored files without a circular import.
        self.filter_validator = filter_validator

    @staticmethod
    def empty():
        return {"schema_version": 1, "products": {}}

    @staticmethod
    def empty_product():
        return {"views": [], "known_ids": []}

    def validate_view(self, value):
        if not isinstance(value, dict) or set(value) != {"id", "name", "filter", "q"}:
            raise CreationError(400, "保存的视图格式无效。")
        identifier = view_id(value["id"])
        name, query = value["name"], value["q"]
        if (not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_NAME_LENGTH or
                any(ord(char) < 32 for char in name)):
            raise CreationError(400, "视图名称须为 1–40 个字符。")
        if not isinstance(query, str) or len(query) > 200:
            raise CreationError(400, "视图搜索词不能超过 200 个字符。")
        return {"id": identifier, "name": name.strip(), "filter": self.filter_validator(value["filter"]),
                "q": query.strip()}

    def read(self):
        if not self.path.exists():
            return self.empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if (not isinstance(data, dict) or set(data) != {"schema_version", "products"} or
                    data["schema_version"] != 1 or not isinstance(data["products"], dict)):
                raise ValueError()
            for product, entry in data["products"].items():
                if (view_product(product) != product or not isinstance(entry, dict) or
                        set(entry) != {"views", "known_ids"} or not isinstance(entry["views"], list) or
                        len(entry["views"]) > MAX_VIEWS or not isinstance(entry["known_ids"], list)):
                    raise ValueError()
                records = [self.validate_view(row) for row in entry["views"]]
                identifiers = [row["id"] for row in records]
                known = [view_id(identifier) for identifier in entry["known_ids"]]
                names = [row["name"].casefold() for row in records]
                if (records != entry["views"] or len(set(identifiers)) != len(identifiers) or
                        len(set(known)) != len(known) or not set(identifiers).issubset(known) or
                        len(set(names)) != len(names) or set(names) & PRESET_NAMES):
                    raise ValueError()
            return data
        except (OSError, ValueError, TypeError, RecursionError) as exc:
            raise CreationError(500, "保存的内容视图无法读取，请保留原文件并核对备份。") from exc

    @staticmethod
    def _public(product, entry):
        return {"product": product, "views": copy.deepcopy(entry["views"]), "revision": fingerprint(entry)}

    def public(self, product=""):
        product = view_product(product)
        with self.lock:
            entry = self.read()["products"].get(product, self.empty_product())
            return self._public(product, entry)

    @staticmethod
    def _name_available(name, records, identifier=None):
        return name not in PRESET_NAMES and all(row["id"] == identifier or
            row["name"].casefold() != name.casefold() for row in records)

    def _import_name(self, name, records):
        if self._name_available(name, records):
            return name
        index = 1
        while True:
            suffix = "（导入）" if index == 1 else f"（导入 {index}）"
            candidate = name[:MAX_NAME_LENGTH - len(suffix)] + suffix
            if self._name_available(candidate, records):
                return candidate
            index += 1

    def mutate(self, body):
        if not isinstance(body, dict) or not isinstance(body.get("action"), str):
            raise CreationError(400, "内容视图保存参数无效。")
        action = body["action"]
        required = {"save": {"product", "action", "view", "revision"},
                    "delete": {"product", "action", "id", "revision"},
                    "import": {"product", "action", "views"}}.get(action)
        if required is None or set(body) != required:
            raise CreationError(400, "内容视图保存参数无效。")
        product = view_product(body["product"])
        records = []
        if action == "save":
            record = self.validate_view(body["view"])
            identifier = record["id"]
        elif action == "delete":
            identifier = view_id(body["id"])
        else:
            if not isinstance(body["views"], list) or len(body["views"]) > MAX_VIEWS:
                raise CreationError(400, "每个产品最多保存 30 个视图；浏览器中的旧视图尚未迁移，请先整理后重试。")
            records = [self.validate_view(row) for row in body["views"]]
            if len({row["id"] for row in records}) != len(records):
                raise CreationError(400, "浏览器中的视图编号重复，旧视图尚未迁移。")
        if action != "import" and not isinstance(body["revision"], str):
            raise CreationError(400, "缺少视图版本，请刷新后重试。")
        with self.lock:
            try:
                with file_lock(self.root / "content-views.lock", "内容视图正在保存，请稍后重试。"):
                    data = self.read()
                    previous = data["products"].get(product, self.empty_product())
                    entry = copy.deepcopy(previous)
                    views, known = entry["views"], set(entry["known_ids"])
                    if action == "import":
                        additions = [row for row in records if row["id"] not in known]
                        if len(views) + len(additions) > MAX_VIEWS:
                            raise CreationError(400, "合并后超过 30 个视图，浏览器中的旧视图尚未迁移；请先删除不再使用的工作台视图再重试。")
                        for row in additions:
                            row["name"] = self._import_name(row["name"], views)
                            views.append(row)
                            known.add(row["id"])
                    else:
                        existing = next((row for row in views if row["id"] == identifier), None)
                        # A timed-out response can be retried safely with the same
                        # ID and payload, even if the caller kept its old revision.
                        if action == "save" and existing == record:
                            return self._public(product, previous)
                        if action == "delete" and existing is None and identifier in known:
                            return self._public(product, previous)
                        if body["revision"] != fingerprint(previous):
                            raise CreationError(409, "视图已在其他浏览器或设备更新，请刷新视图后重试。")
                        if action == "save":
                            if existing is None and identifier in known:
                                raise CreationError(409, "这条视图已被删除，请另存为新视图。")
                            if not self._name_available(record["name"], views, identifier):
                                raise CreationError(400, "这个视图名称已被使用，请换一个名称。")
                            if existing is None and len(views) >= MAX_VIEWS:
                                raise CreationError(400, "每个产品最多保存 30 个视图，请先删除不再使用的视图。")
                            if existing is None:
                                views.append(record)
                            else:
                                views[views.index(existing)] = record
                            known.add(identifier)
                        else:
                            if existing is None:
                                raise CreationError(404, "这条视图不存在，请刷新后重试。")
                            views.remove(existing)
                            known.add(identifier)
                    entry["known_ids"] = sorted(known)
                    if entry != previous:
                        data["products"][product] = entry
                        durable_json(self.path, data)
                    return self._public(product, entry)
            except VoiceError as exc:
                raise CreationError(exc.status, str(exc)) from exc
