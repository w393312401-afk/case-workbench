"""Independent selling points and test directions; never rewrites case snapshots.

runtime/directions/catalog.json is authoritative business data, not a cache. Back
up the entire directions directory alongside cases and other runtime records.
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
from datetime import datetime, timezone


class DirectionError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def product_key(value):
    text = str(value or "").strip()
    codes = set(re.findall(r"(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])", text.upper()))
    return next(iter(codes)) if len(codes) == 1 else text


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# Execution variants must differ in how the script progresses, not only in the first line.
PROGRESSIONS = ("问题-解决", "结果倒叙", "逐件对照", "回应质疑", "过程仪式", "身份认同", "清单演示")
CHAIN_KEYS = ("main_difference", "concern", "closing", "variant_type", "execution_change", "script_version", "conflict")
CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])([SAB][1-9]\d*)(?!\d)")


def progression_value(value):
    """One of PROGRESSIONS, or "其他：说明" with a short description."""
    value = value.strip() if isinstance(value, str) else ""
    if value in PROGRESSIONS:
        return value
    match = re.fullmatch(r"其他[:：]\s*(.{2,60})", value)
    if match:
        return "其他：" + match.group(1).strip()
    raise DirectionError(400, "请选择这条变体的推进结构；选“其他”时写明结构。")


def chain_issues(points, *parts):
    """Codes that are not in this product's table, and links written as unnumbered."""
    codes = {point["code"] for point in points}
    joined = " → ".join(part for part in parts if isinstance(part, str))
    issues = []
    unknown = sorted({code for code in CODE_PATTERN.findall(joined) if code not in codes})
    if unknown:
        issues.append("卖点表里没有 " + "、".join(unknown))
    if "未编号" in joined:
        issues.append("有未编号的环节，请改用卖点编号或删去")
    return issues


def grading_rows(content):
    """Rows of the first "编号 | 卖点 | …" table in a product grading file."""
    rows, in_table = [], False
    for line in content.splitlines():
        if not in_table:
            in_table = bool(re.match(r"\|\s*编号\s*\|\s*卖点\s*\|", line))
            continue
        if not line.strip().startswith("|"):
            break
        columns = [part.strip() for part in line.strip().strip("|").split("|")]
        if len(columns) == 4 and re.fullmatch(r"[SAB][1-9]\d*", columns[0]):
            rows.append(columns)
    return rows


def text(value, label, required=True, limit=12000):
    if not isinstance(value, str) or (required and not value.strip()) or len(value) > limit:
        raise DirectionError(400, f"{label}格式无效。")
    return value.strip()


class DirectionStore:
    def __init__(self, runtime, skill_root, lock=None):
        self.root = Path(runtime) / "directions"
        self.path = self.root / "catalog.json"
        self.skill_root = Path(skill_root)
        self.lock = lock or threading.RLock()

    @staticmethod
    def empty():
        return {"schema_version": 1, "selling_points": [], "directions": [], "video_links": [],
                "migration": {"sources": {}, "cases": {}, "conflicts": []}, "requests": {}}

    def read(self):
        if not self.path.exists():
            return self.empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if value.get("schema_version") != 1 or any(not isinstance(value.get(key), list)
                    for key in ("selling_points", "directions", "video_links")) or not isinstance(value.get("requests"), dict):
                raise ValueError("invalid catalog schema")
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise DirectionError(500, "方向目录无法读取；请保留原文件并从备份恢复。") from exc

    @staticmethod
    def revision(data):
        return fingerprint({key: value for key, value in data.items() if key != "requests"})

    def write(self, data, *, migration=False, backup=True):
        self.root.mkdir(parents=True, exist_ok=True)
        if backup and self.path.exists():
            backup = self.root / "backups"
            backup.mkdir(exist_ok=True)
            # A content-addressed backup keeps every prior committed catalog,
            # including the pre-migration version, without repeated copies.
            raw = self.path.read_bytes()
            destination = backup / (hashlib.sha256(raw).hexdigest() + ".json")
            if not destination.exists():
                with destination.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        encoded = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".catalog-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def point_id(product, code):
        return "SP-" + fingerprint([product, code])[:12]

    @staticmethod
    def find_direction(data, product, identifier):
        return next((item for item in data["directions"]
                     if item["product"] == product_key(product) and item["direction_id"] == identifier), None)

    @staticmethod
    def find_point(data, identifier):
        return next((item for item in data["selling_points"] if item["id"] == identifier), None)

    @staticmethod
    def find_link(data, product, video):
        return next((item for item in data["video_links"]
                     if item["product"] == product_key(product) and item["video_id"] == video), None)

    def _import_points(self, data):
        folder = self.skill_root / "references" / "products"
        for path in sorted(folder.glob("*-selling-point-grading.md")):
            content = path.read_text(encoding="utf-8-sig")
            digest = hashlib.sha256(content.encode()).hexdigest()
            key = str(path)
            if data["migration"]["sources"].get(key) == digest:
                continue
            product = product_key(path.name.removesuffix("-selling-point-grading.md"))
            for columns in grading_rows(content):
                code, title, rationale, evidence = columns
                identifier = self.point_id(product, code)
                if not self.find_point(data, identifier):
                    data["selling_points"].append({"id": identifier, "product": product, "code": code,
                        "tier": code[0], "title": title, "rationale": rationale, "evidence": evidence,
                        "source": {"kind": "markdown", "path": key, "digest": digest, "table": "卖点编号",
                                   "row": columns, "imported_at": now()}})
                # Source edits never silently replace a user's catalog edits.
            data["migration"]["sources"][key] = digest

    def _point_for_legacy(self, data, product, direction):
        match = re.search(r"-([SAB][1-9]\d*)$", direction["direction_id"])
        return self.find_point(data, self.point_id(product, match.group(1))) if match else None

    def compatible(self, data, entity, direction, point_id=None):
        if point_id and entity.get("selling_point_id") != point_id:
            return False
        main = direction["main_difference"].strip()
        accepted = {entity["main_difference"], *(variant["main_difference"] for variant in entity["variants"]
                    if not variant.get("conflict"))}
        if main in accepted:
            return True
        point = self.find_point(data, entity.get("selling_point_id"))
        if point:
            code = re.match(r"^([SAB][1-9]\d*)(?!\d)", main)
            return bool(code and code.group(1) == point["code"]) or main == point["title"]
        return False

    @staticmethod
    def add_variant(entity, direction, source, conflict=False, meta=None):
        fields = {key: direction[key] for key in ("main_difference", "concern", "closing", "variant_type", "execution_change", "script_version") if direction.get(key)}
        fields["conflict"] = conflict
        # Progression and baseline are catalog annotations; a case snapshot never carries them.
        variant = next((entry for entry in entity["variants"] if {key: value for key, value in entry.items() if key in CHAIN_KEYS} == fields), None)
        if variant is None:
            variant = {**fields, **(meta or {}), "sources": []}
            entity["variants"].append(variant)
        if source not in variant["sources"]:
            variant["sources"].append(source)

    def sync(self, cases):
        """Import new current records only; repeated reads never rewrite events."""
        with self.lock:
            data = self.read()
            before = self.revision(data)
            self._import_points(data)
            for case in cases:
                product = product_key(case.get("product"))
                current = case.get("current") or {}
                direction = current.get("test_direction")
                video = str((current.get("metrics") or {}).get("video_id") or "")
                marker = fingerprint([product, case.get("current_event_id"), direction, video])
                if data["migration"]["cases"].get(case["case_id"]) == marker:
                    continue
                data["migration"]["cases"][case["case_id"]] = marker
                # Case-derived assignments follow that case's current video.
                # An explicit manual assignment or unlink remains independent.
                for old_link in data["video_links"]:
                    if (old_link["source"].get("kind") == "case" and old_link["source"].get("case_id") == case["case_id"]
                            and (old_link["product"] != product or old_link["video_id"] != video or not direction)):
                        old_link["direction_id"] = ""
                if not direction or not product:
                    link = self.find_link(data, product, video)
                    if link and link["source"].get("kind") == "case" and link["source"].get("case_id") == case["case_id"]:
                        link["direction_id"] = ""
                    continue
                source = {"kind": "case", "case_id": case["case_id"], "event_id": case.get("current_event_id"),
                          "product_label": case.get("product")}
                entity = self.find_direction(data, product, direction["direction_id"])
                if entity is None:
                    point = self._point_for_legacy(data, product, direction)
                    entity = {"product": product, "direction_id": direction["direction_id"],
                        "selling_point_id": point["id"] if point else None,
                        **{key: direction[key] for key in ("main_difference", "concern", "closing")},
                        "status": "testing", "notes": "", "variants": [], "source": [source]}
                    data["directions"].append(entity)
                conflict = not self.compatible(data, entity, direction)
                if source not in entity["source"]:
                    entity["source"].append(source)
                self.add_variant(entity, direction, source, conflict)
                if conflict:
                    entity["definition_conflict"] = True
                    record = {"product": product, "direction_id": direction["direction_id"], "case_id": case["case_id"],
                              "event_id": case.get("current_event_id"), "reason": "同一方向编号出现不同主打；原快照保留。"}
                    if record not in data["migration"]["conflicts"]:
                        data["migration"]["conflicts"].append(record)
                if re.fullmatch(r"\d{6,25}", video):
                    link = self.find_link(data, product, video)
                    if link is None:
                        data["video_links"].append({"product": product, "video_id": video,
                                                   "direction_id": direction["direction_id"], "source": source})
                    elif link["source"].get("kind") == "case" and (link["source"].get("case_id") == case["case_id"] or not link["direction_id"]):
                        link.update(direction_id=direction["direction_id"], source=source)
            if self.revision(data) != before:
                self.write(data, migration=True)
            return data

    def grading_table(self):
        """Current grading-file rows by (product, code); the catalog never silently takes them over."""
        rows = {}
        for path in sorted((self.skill_root / "references" / "products").glob("*-selling-point-grading.md")):
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError):
                continue
            product = product_key(path.name.removesuffix("-selling-point-grading.md"))
            for code, title, rationale, evidence in grading_rows(content):
                rows[(product, code)] = {"title": title, "rationale": rationale, "evidence": evidence, "path": str(path)}
        return rows

    def public(self, data, product=None):
        product = product_key(product)
        result = {"revision": self.revision(data), **{key: copy.deepcopy([item for item in data[key]
                    if not product or item["product"] == product]) for key in ("selling_points", "directions", "video_links")},
                "migration": copy.deepcopy(data["migration"])}
        table = self.grading_table()
        for point in result["selling_points"]:
            row = table.get((point["product"], point["code"]))
            point["grading_row"] = row
            point["grading_mismatch"] = bool(row) and any(point.get(key, "") != row[key] for key in ("title", "rationale", "evidence"))
        for entity in result["directions"]:
            points = [item for item in data["selling_points"] if item["product"] == entity["product"]]
            lead = next((item for item in points if item["id"] == entity.get("selling_point_id")), None)
            entity["lead_tier"] = lead["tier"] if lead else None
            entity["chain_issues"] = chain_issues(points, entity["concern"], entity["closing"])
            for variant in entity["variants"]:
                variant["chain_issues"] = chain_issues(points, variant.get("concern"), variant.get("closing"))
        return result

    def mutate(self, kind, body):
        with self.lock:
            data = self.read()
            key = text(body.get("request_id"), "保存标识", limit=100)
            if not re.fullmatch(r"[\w-]{10,100}", key):
                raise DirectionError(400, "缺少保存标识，请刷新后重试。")
            digest = fingerprint([kind, body])
            previous = data["requests"].get(key)
            if previous:
                if previous["fingerprint"] != digest:
                    raise DirectionError(409, "保存标识已用于其他修改，请重新提交。")
                return {**copy.deepcopy(previous["result"]), "catalog": self.public(data)}
            if body.get("revision") != self.revision(data):
                raise DirectionError(409, "卖点与方向已在其他位置更新。请刷新目录后确认，你的填写仍保留。")
            if kind == "point":
                result = self._save_point(data, body.get("point"))
            elif kind == "direction":
                result = self._save_direction(data, body.get("direction"), body.get("operation", "add_variant"))
            elif kind == "video":
                result = self._save_video(data, body)
            else:
                raise DirectionError(400, "方向操作不存在。")
            result = {**result, "revision": self.revision(data)}
            data["requests"][key] = {"fingerprint": digest, "result": copy.deepcopy(result)}
            self.write(data)
            return {**result, "catalog": self.public(data)}

    def _save_point(self, data, value):
        if not isinstance(value, dict):
            raise DirectionError(400, "请填写卖点。")
        product = product_key(text(value.get("product"), "产品", limit=200))
        code = text(value.get("code"), "卖点编号", limit=30).upper()
        tier = text(value.get("tier", code[:1]), "卖点等级", limit=1)
        if not re.fullmatch(r"[SAB][1-9]\d*", code) or tier != code[0]:
            raise DirectionError(400, "卖点编号与 S/A/B 等级不一致。")
        identifier = self.point_id(product, code)
        if value.get("id") and value["id"] != identifier:
            raise DirectionError(409, "卖点的产品和编号不能更换；请新建另一个卖点。")
        old = self.find_point(data, identifier)
        if old and not value.get("id"):
            raise DirectionError(409, "这个卖点编号已使用，请编辑已有卖点，或选择新的编号。")
        point = {"id": identifier, "product": product, "code": code, "tier": tier,
                 "title": text(value.get("title"), "卖点内容"),
                 "rationale": text(value.get("rationale", ""), "分级用途", False),
                 "evidence": text(value.get("evidence", ""), "依据状态", False),
                 "source": copy.deepcopy(old["source"]) if old else {"kind": "manual", "created_at": now()}}
        if old:
            old.update(point)
        else:
            data["selling_points"].append(point)
        return {"point": copy.deepcopy(point)}

    def _save_direction(self, data, value, operation="add_variant"):
        if operation not in {"add_variant", "update", "variant_meta"}:
            raise DirectionError(400, "请选择新增执行变体或编辑方向。")
        if not isinstance(value, dict):
            raise DirectionError(400, "请填写方向。")
        product = product_key(text(value.get("product"), "产品", limit=200))
        if operation == "variant_meta":
            return self._save_variant_meta(data, product, value)
        point = self.find_point(data, value.get("selling_point_id")) if value.get("selling_point_id") else None
        if value.get("selling_point_id") and (not point or point["product"] != product):
            raise DirectionError(400, "请选择当前产品的卖点。")
        identifier = text(value.get("direction_id") or (f"{product}-{point['code']}" if point else ""), "方向编号", limit=200)
        if any(char in identifier for char in "/\\?#"):
            raise DirectionError(400, "方向编号不能包含路径字符。")
        main = text(value.get("main_difference") or (f"{point['code']} {point['title']}" if point else ""), "主打卖点")
        if point:
            lead = re.match(r"^([SAB][1-9]\d*)(?!\d)", main)
            if (lead and lead.group(1) != point["code"]) or (not lead and main != point["title"]):
                raise DirectionError(400, "主打卖点须与选择的卖点一致。")
            suffix = re.search(r"-([SAB][1-9]\d*)$", identifier)
            if suffix and suffix.group(1) != point["code"]:
                raise DirectionError(400, "方向编号须与主打卖点编号一致。")
        chain = {"main_difference": main, "concern": text(value.get("concern"), "顾虑链"),
                 "closing": text(value.get("closing"), "收口"), "variant_type": "execution"}
        if re.fullmatch(r"[\d\s\W_]*", chain["concern"]) or re.fullmatch(r"[\d\s\W_]*", chain["closing"]):
            raise DirectionError(400, "顾虑链和收口要写成观众顾虑与回应，不能只有数字或符号。")
        points = [item for item in data["selling_points"] if item["product"] == product]
        issues = chain_issues(points, chain["concern"], chain["closing"])
        if issues:
            raise DirectionError(400, "方向链需要修改：" + "；".join(issues) + "。")
        status = value.get("status", "testing")
        if status not in {"testing", "active", "paused", "archived"}:
            raise DirectionError(400, "方向状态无效。")
        notes = text(value.get("notes", ""), "方向备注", False)
        old = self.find_direction(data, product, identifier)
        source = {"kind": "manual", "created_at": now()}
        if old:
            if not self.compatible(data, old, chain, point["id"] if point else None):
                raise DirectionError(409, "同一方向编号不能更换主打卖点，请为新的主打创建方向。")
            if operation == "update":
                # The previous definition moves to the direction's history, never into its variants.
                previous = {key: old[key] for key in ("main_difference", "concern", "closing")}
                if previous != {key: chain[key] for key in previous} or old.get("status") != status or old.get("notes", "") != notes:
                    old.setdefault("history", []).append({**previous, "status": old.get("status"), "notes": old.get("notes", ""), "replaced_at": now()})
                old.update({key: chain[key] for key in ("main_difference", "concern", "closing")}, status=status, notes=notes)
                return {"direction": copy.deepcopy(old), "existing": True}
            progression = progression_value(value.get("progression"))
            change = text(value.get("execution_change"), "执行改动", limit=4000)
            same = next((index for index, item in enumerate(old["variants"]) if item.get("progression") == progression and not item.get("conflict")), None)
            if same is not None:
                raise DirectionError(409, f"这个方向第 {same + 1} 条已经是“{progression}”推进，只换开头不算新变体。请换一种推进结构。")
            if any({key: item.get(key) for key in ("concern", "closing")} == {key: chain[key] for key in ("concern", "closing")} for item in old["variants"]):
                raise DirectionError(409, "这条链和已有记录完全相同，不算新变体。")
            self.add_variant(old, {**chain, "execution_change": change}, source, meta={"progression": progression})
            return {"direction": copy.deepcopy(old), "existing": True}
        if operation == "update":
            raise DirectionError(404, "要编辑的方向不存在；产品和方向编号不能更换。")
        if point and point["tier"] != "S":
            reason = text(value.get("lead_reason", ""), "非 S 主打理由", False, 2000)
            if not reason:
                raise DirectionError(400, f"{point['code']} 是 {point['tier']} 级卖点。方向的主打应是 S 级；确实要用它主打，请写明理由。")
            notes = ("非 S 主打理由：" + reason + ("\n" + notes if notes else ""))
        if point:
            duplicate = next((item for item in data["directions"] if item["product"] == product and item.get("selling_point_id") == point["id"]), None)
            if duplicate:
                raise DirectionError(409, f"这个主打已有方向 {duplicate['direction_id']}；改变顾虑或收口请作为该方向的执行变体。")
        entity = {"product": product, "direction_id": identifier, "selling_point_id": point["id"] if point else None,
                  **{key: chain[key] for key in ("main_difference", "concern", "closing")},
                  "status": status, "notes": notes, "variants": [], "source": [source]}
        chain["variant_type"] = "direction"
        meta = {"baseline": True}
        if value.get("progression"):
            meta["progression"] = progression_value(value.get("progression"))
        self.add_variant(entity, chain, source, meta=meta)
        data["directions"].append(entity)
        return {"direction": copy.deepcopy(entity), "existing": False}

    def _save_variant_meta(self, data, product, value):
        """Mark the baseline or name the progression of an existing variant; the chain itself stays as recorded."""
        entity = self.find_direction(data, product, text(value.get("direction_id"), "方向编号", limit=200))
        if entity is None:
            raise DirectionError(404, "这条方向不存在。")
        index = value.get("index")
        if not isinstance(index, int) or not 0 <= index < len(entity["variants"]):
            raise DirectionError(400, "要标注的变体不存在，请刷新后重试。")
        variant = entity["variants"][index]
        if "progression" in value:
            progression = progression_value(value["progression"])
            same = next((i for i, item in enumerate(entity["variants"]) if i != index and item.get("progression") == progression and not item.get("conflict")), None)
            if same is not None:
                raise DirectionError(409, f"第 {same + 1} 条已经是“{progression}”推进。两条推进相同，应合并为一条变体。")
            variant["progression"] = progression
        if value.get("baseline") is True:
            for item in entity["variants"]:
                item.pop("baseline", None)
            variant["baseline"] = True
        return {"direction": copy.deepcopy(entity), "existing": True}

    def _save_video(self, data, body):
        product = product_key(text(body.get("product"), "产品", limit=200))
        video = text(body.get("video_id"), "视频 ID", limit=25)
        if not re.fullmatch(r"\d{6,25}", video):
            raise DirectionError(400, "视频 ID 格式无效。")
        identifier = text(body.get("direction_id"), "方向编号", False, 200)
        if identifier and not self.find_direction(data, product, identifier):
            raise DirectionError(404, "所选产品中不存在这条方向。")
        link = self.find_link(data, product, video)
        if link is None:
            link = {"product": product, "video_id": video}
            data["video_links"].append(link)
        link.update(direction_id=identifier, source={"kind": "manual", "updated_at": now()})
        return {"link": copy.deepcopy(link)}

    def prepare_case(self, current, product, previous=None, *, allow_inherit=False):
        """Validate/resolve a proposed new snapshot, without mutating the catalog."""
        data = self.read()
        direction = current.get("test_direction")
        prior = (previous or {}).get("test_direction")
        video = str((current.get("metrics") or {}).get("video_id") or "")
        link = self.find_link(data, product, video)
        if allow_inherit and direction is None and link and link["direction_id"]:
            entity = self.find_direction(data, product, link["direction_id"])
            if entity:
                direction = {key: entity[key] for key in ("direction_id", "main_difference", "concern", "closing")}
                direction["variant_type"] = "direction"
                current["test_direction"] = direction
        if direction is not None and direction != prior:
            entity = self.find_direction(data, product, direction["direction_id"])
            if entity and not self.compatible(data, entity, direction):
                raise DirectionError(409, "同一方向编号不能更换主打卖点；请选择对应的独立方向。")
        if direction and link and link["direction_id"] and link["source"].get("kind") == "manual" and link["direction_id"] != direction["direction_id"]:
            raise DirectionError(409, "视频已关联另一条方向。请先核对视频方向，再保存案例。")
        return current

    def forget_case(self, identifier):
        """Remove case-owned provenance, including internal catalog backups."""
        def cleaned(data):
            data = copy.deepcopy(data)
            data["migration"]["cases"].pop(identifier, None)
            data["migration"]["conflicts"] = [item for item in data["migration"]["conflicts"] if item.get("case_id") != identifier]
            for entity in data["directions"]:
                entity["source"] = [source for source in entity["source"] if source.get("case_id") != identifier]
                kept = []
                for variant in entity["variants"]:
                    variant["sources"] = [source for source in variant["sources"] if source.get("case_id") != identifier]
                    if variant["sources"]:
                        kept.append(variant)
                entity["variants"] = kept
                entity["definition_conflict"] = any(variant.get("conflict") for variant in kept)
            for link in data["video_links"]:
                if link["source"].get("case_id") == identifier:
                    link["source"] = {"kind": "retained_video_assignment"}
            data["requests"] = {key: receipt for key, receipt in data["requests"].items()
                                if identifier not in json.dumps(receipt, ensure_ascii=False)}
            return data
        with self.lock:
            if self.path.exists():
                original = self.read()
                data = cleaned(original)
                if data != original:
                    self.write(data, backup=False)
            for path in (self.root / "backups").glob("*.json"):
                original = json.loads(path.read_text(encoding="utf-8-sig"))
                data = cleaned(original)
                if data != original:
                    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
                    destination = path.with_name(hashlib.sha256(raw).hexdigest() + ".json")
                    if not destination.exists():
                        with destination.open("xb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fsync(stream.fileno())
                    path.unlink()
