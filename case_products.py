"""One product name per case, so cases filter and read the same way everywhere.

Original records keep whatever name they were collected with (long TikTok listing titles,
"198H", competitor descriptions). The workbench shows a unified name instead:
- a manual pick saved here (fly chooses it on the case page), else
- "SHARPAL <model>" when the original names one SHARPAL model (or is just the model code), else
- the original text unchanged (competitor or unidentified products, until someone picks one).

Picks live in runtime/evidence/case-products.json; the case records and their index are never rewritten.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

MODEL = re.compile(r"(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])")
CASE_ID = re.compile(r"PVR-[0-9a-f]{12}")


def canonical(raw):
    text = re.sub(r"\s+", " ", str(raw or "")).strip()
    codes = sorted(set(MODEL.findall(text.upper())))
    if len(codes) == 1 and (re.search(r"\bSHARPAL\b", text, re.I) or text.upper() == codes[0]):
        return f"SHARPAL {codes[0]}"
    return text


def clean(value, *, max_length=120):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("产品名称必须是文字。")
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return None
    if max_length is not None and len(text) > max_length:
        raise ValueError("产品名称不能超过 120 个字。")
    return canonical(text) if MODEL.fullmatch(text.upper()) else text


class ProductLabels:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache = (None, {})

    def key(self):
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def picks(self):
        key = self.key()
        with self._lock:
            if self._cache[0] == key:
                return self._cache[1]
            picks = {}
            if key is not None:
                try:
                    data = json.loads(self.path.read_text(encoding="utf-8"))
                    for case_id, entry in (data.get("cases") or {}).items():
                        product = entry.get("product") if isinstance(entry, dict) else None
                        if CASE_ID.fullmatch(str(case_id)) and isinstance(product, str) and product.strip():
                            picks[case_id] = product.strip()
                except (OSError, ValueError, AttributeError):
                    picks = {}
            self._cache = (key, picks)
            return picks

    def label(self, case_id, raw):
        return self.picks().get(case_id) or canonical(raw)

    def is_manual(self, case_id):
        return case_id in self.picks()

    def set(self, case_id, product, *, allow_existing=False):
        if not CASE_ID.fullmatch(str(case_id)):
            raise ValueError("案例编号无效。")
        cleaned = clean(product, max_length=None if allow_existing else 120)
        # The server only grants this for an exact visible catalog entry. Keep
        # its identifier intact instead of creating a whitespace-normalized twin.
        product = product.strip() if allow_existing and cleaned is not None else cleaned
        with self._lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            except (OSError, ValueError):
                data = {}
            cases = data.get("cases") if isinstance(data.get("cases"), dict) else {}
            if product is None:
                cases.pop(case_id, None)
            else:
                cases[case_id] = {"product": product, "set_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "cases": cases}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            self._cache = (None, {})
        return product


class ProductCatalog:
    """The product list in the top-left switcher: names found in the data, plus ones added by hand, minus removed ones.

    Removing hides a name; deleting also removes it from the hidden list. Both
    preserve cases, videos and scripts. Deleted keys remain as tombstones so
    discovering an unchanged source record cannot add the name back.
    """

    def __init__(self, path, key_of):
        self.path = Path(path)
        self.key_of = key_of
        self._lock = threading.Lock()

    def key(self):
        try:
            stat = self.path.stat()
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        lists = {}
        for name in ("added", "removed", "deleted"):
            values = data.get(name) if isinstance(data, dict) else None
            lists[name] = [value for value in values if isinstance(value, str) and value.strip()] if isinstance(values, list) else []
        return lists

    def apply(self, values):
        lists = self.read()
        deleted = set(lists["deleted"])
        removed = set(lists["removed"]) - deleted
        return sorted((set(values) | set(lists["added"])) - removed - deleted - {"", "all"}), sorted(removed)

    def change(self, action, product=None, *, known_products=()):
        if action not in {"add", "remove", "restore", "delete", "delete_hidden"}:
            raise ValueError("不支持的操作。")
        if action != "delete_hidden":
            # Source records can contain old listing titles longer than the new
            # product-name limit. Managing those existing entries is not adding
            # a new name and must not fail the add form's validation.
            name = clean(product, max_length=120 if action == "add" else None)
            if name is None:
                raise ValueError("请填写产品名称。")
            # Existing entries are identifiers: preserve internal whitespace so
            # even legacy names match the key returned by product discovery.
            key = self.key_of(name if action == "add" else product.strip())
            if key in {"", "all"}:
                raise ValueError("请填写有效的产品名称。")
        with self._lock:
            lists = self.read()
            added, removed, deleted = (set(lists[group]) for group in ("added", "removed", "deleted"))
            if action == "delete_hidden":
                result = sorted(removed - deleted)
                deleted.update(removed)
                added.difference_update(removed)
                removed.clear()
            else:
                if len(name) > 120 and key not in added | removed | deleted | set(known_products):
                    raise ValueError("只能操作已存在的产品；新增产品名称不能超过 120 个字。")
                if action in {"restore", "remove"} and key in deleted:
                    raise ValueError("这个产品已删除，请通过新增产品重新添加。")
                added.discard(key)
                removed.discard(key)
                if action in {"add", "restore"}:
                    # Hidden manual names may have no source records, including
                    # names hidden by older versions that discarded added.
                    added.add(key)
                    deleted.discard(key)
                elif action == "remove":
                    removed.add(key)
                elif action == "delete":
                    deleted.add(key)
                result = key
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "added": sorted(added), "removed": sorted(removed),
                                             "deleted": sorted(deleted)}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        return result
