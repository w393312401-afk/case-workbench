"""TK 看板 cards and their automatic catalog/production associations.

Direction membership is independent of whether a card is waiting, in production,
or dismissed. The original card and reference remain available for provenance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from direction_store import product_key

STATUSES = ("new", "adopted", "dismissed", "writing", "produced")
MAX_PER_PUSH = 8
MAX_CANDIDATES = 2000
COUNTS = ("views", "likes", "comments", "shares", "saves")
LEAD_CODE = re.compile(r"(?<![A-Za-z0-9])([SAB][1-9]\d*)(?!\d)")


class CandidateError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _text(value, limit):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x09\x0b-\x1f\x7f]+", " ", value).strip()[:limit]


def _pair(value):
    if isinstance(value, str):
        return {"en": _text(value, 600), "zh": ""}
    if not isinstance(value, dict):
        return {"en": "", "zh": ""}
    return {"en": _text(value.get("en"), 600), "zh": _text(value.get("zh"), 600)}


def _list(value, limit, size):
    return [item for item in (_text(entry, size) for entry in (value if isinstance(value, list) else [])[:limit]) if item]


def clean_direction(raw):
    """One direction card from the extension, reduced to plain bounded text; raises CandidateError when empty."""
    if not isinstance(raw, dict):
        raise CandidateError(400, "方向格式无效。")
    lines = [pair for pair in (_pair(item) for item in (raw.get("lines") if isinstance(raw.get("lines"), list) else [])[:30])
             if pair["en"] or pair["zh"]]
    direction = {
        "code": _text(raw.get("id"), 80), "kind": _text(raw.get("kind"), 40), "variant_of": _text(raw.get("variantOf"), 400),
        "name": _text(raw.get("name"), 200), "audience": _text(raw.get("audience"), 400), "compare": _text(raw.get("compare"), 300),
        "chain": _list(raw.get("chain"), 12, 300), "why": _text(raw.get("why"), 2000), "hook_visual": _text(raw.get("hookVisual"), 600),
        "hook_text": _pair(raw.get("hookText")), "lines": lines, "cta": _pair(raw.get("cta")), "payback": _text(raw.get("payback"), 1000),
        "check": _list(raw.get("check"), 10, 300),
    }
    if not direction["chain"] and not lines:
        raise CandidateError(400, "方向缺少方向链和口播。")
    return direction


def clean_reference(raw):
    """The reference video as the extension saw it: ID, account, title and public counts."""
    if not isinstance(raw, dict):
        raise CandidateError(400, "缺少参考视频。")
    video_id = str(raw.get("id") or "")
    if not re.fullmatch(r"\d{6,25}", video_id):
        raise CandidateError(400, "参考视频 ID 无效。")
    author = str(raw.get("author") or "").lstrip("@")
    author = author if re.fullmatch(r"[\w.\-]{1,100}", author) else ""
    counts = {}
    for name in COUNTS:
        value = raw.get(name)
        counts[name] = int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 10 ** 13 else None
    published = raw.get("publishedAt") if isinstance(raw.get("publishedAt"), str) else ""
    return {"video_id": video_id, "author": author, "title": _text(raw.get("title"), 300), **counts,
            "published_at": published[:40] if re.fullmatch(r"\d{4}-\d{2}-\d{2}T[\d:.]+Z?", published[:40]) else "",
            "url": f"https://www.tiktok.com/@{author or '_'}/video/{video_id}"}


def suggestion(direction):
    """Catalog fields already supplied by the card; no extra creative choices."""
    chain = list(direction["chain"])
    code_source = " ".join([direction["code"], chain[0] if chain else ""])
    match = LEAD_CODE.search(direction["code"].split("-")[-1]) or LEAD_CODE.search(code_source)
    closing = ""
    if chain and re.match(r"^\s*收口", chain[-1]):
        closing = re.sub(r"^\s*收口\s*[:：]?\s*", "", chain.pop()).strip()
    if chain and match and LEAD_CODE.match(chain[0].lstrip()) and LEAD_CODE.match(chain[0].lstrip()).group(1) == match.group(1):
        chain = chain[1:]
    return {"code": match.group(1) if match else "", "concern": " → ".join(chain), "closing": closing or direction["cta"]["zh"] or direction["cta"]["en"],
            "audience": direction["audience"], "execution_change": direction["variant_of"] or direction["name"]}


def voiceover(direction):
    """The lines to voice: every English line, then the closing line unless it repeats the last one. Chinese comes
    along only when every voiced line has it, so the two stay matched line by line."""
    rows = [line for line in direction["lines"] if line["en"]]
    cta = direction["cta"]
    if cta["en"] and (not rows or rows[-1]["en"] != cta["en"]):
        rows.append(cta)
    spoken = [row["en"] for row in rows]
    return spoken, [row["zh"] for row in rows] if rows and all(row["zh"] for row in rows) else []


def candidate_instruction(item):
    """The 写稿 request for one candidate: the card in full, and the writing skill's delivery for exactly one script."""
    d, ref = item["direction"], item["reference"]
    pair = lambda value: value["en"] + (f"（{value['zh']}）" if value["zh"] else "")
    lines = [
        "按写稿技能的口播稿交付格式，把下面这张 TK 看板方向卡写成 1 条可以直接进制作单的口播稿：开头画面、开头屏显、"
        "口播（配音用）代码块、读法（需要时）、对应中文、意群表（# | 口播 | 画面要证明什么 | 素材标签 | 证据，结尾 CTA 行注明）、"
        "补拍清单、3 行发布文案，以及发布前核对。只写这 1 条，不出多稿。",
        "保留方向卡的主打卖点、顾虑链和收口手法；逐句口播是方向卡的草稿，按技能规则修顺、核对事实，资料里没有的说法收窄或列入发布前核对。",
        "", "方向卡：",
        f"方向编号：{d['code'] or '未编号'}{'｜' + d['kind'] if d['kind'] else ''}｜{d['name']}",
    ]
    lines += [text for text in (
        d["variant_of"] and "变化：" + d["variant_of"],
        d["audience"] and "拍给：" + d["audience"] + (f"｜比较对象：{d['compare']}" if d["compare"] else ""),
        d["chain"] and "方向链：" + " → ".join(d["chain"]),
        d["hook_visual"] and "开头画面（草稿）：" + d["hook_visual"],
        (d["hook_text"]["en"] or d["hook_text"]["zh"]) and "开头屏显（草稿）：" + pair(d["hook_text"]),
    ) if text]
    if d["lines"]:
        lines.append("逐句口播（草稿）：")
        lines += [f"{number}. {pair(line)}" for number, line in enumerate(d["lines"], 1)]
    lines += [text for text in (
        (d["cta"]["en"] or d["cta"]["zh"]) and "收口句（草稿）：" + pair(d["cta"]),
        d["payback"] and "收口思路：" + d["payback"],
        d["why"] and "为什么这样拍：" + d["why"],
        d["check"] and "发布前要核对：" + "；".join(d["check"]),
        "参考视频：" + ref["url"] + (f"（{ref['title']}）" if ref["title"] else ""),
        item.get("ask") and "生成方向时的要求：" + item["ask"],
    ) if text]
    return "\n".join(lines)[:19000]


def candidate_id(product, video_id, direction):
    digest = hashlib.sha256(json.dumps([product_key(product), video_id, direction["code"], direction["name"],
                                        [line["en"] for line in direction["lines"]]], ensure_ascii=False).encode()).hexdigest()
    return "TKC-" + digest[:12]


class CandidateStore:
    def __init__(self, runtime):
        self.path = Path(runtime) / "directions" / "tk-candidates.json"
        self.lock = threading.Lock()

    def read(self):
        if not self.path.exists():
            return {"schema_version": 1, "candidates": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if data.get("schema_version") != 1 or not isinstance(data.get("candidates"), list):
                raise ValueError("invalid schema")
            return data
        except (OSError, ValueError, TypeError) as exc:
            raise CandidateError(500, "插件候选方向文件无法读取；请保留原文件并从备份恢复。") from exc

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (json.dumps(data, ensure_ascii=False, indent=1) + "\n").encode()
        descriptor, temporary = tempfile.mkstemp(prefix=".tk-candidates-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def add(self, product, reference, directions, *, ask="", now=None):
        """Save cards from one reference video. A card saved before keeps its status and only refreshes the
        reference counts. Returns how many were new and the candidate behind every card, in order."""
        if not isinstance(directions, list) or not directions:
            raise CandidateError(400, "没有要保存的方向。")
        if len(directions) > MAX_PER_PUSH:
            raise CandidateError(400, f"一次最多保存 {MAX_PER_PUSH} 个方向。")
        reference = clean_reference(reference)
        cleaned = [clean_direction(item) for item in directions]
        stamp = now or _now()
        saved = duplicate = 0
        result = []
        with self.lock:
            data = self.read()
            by_id = {item["candidate_id"]: item for item in data["candidates"]}
            for direction in cleaned:
                identifier = candidate_id(product, reference["video_id"], direction)
                item = by_id.get(identifier)
                if item:
                    item["reference"] = reference
                    duplicate += 1
                else:
                    item = {"candidate_id": identifier, "product": product, "product_key": product_key(product),
                            "reference": reference, "direction": direction, "ask": _text(ask, 2000),
                            "suggestion": suggestion(direction), "status": "new", "direction_id": "",
                            "saved_at": stamp, "status_at": stamp}
                    data["candidates"].append(item)
                    by_id[identifier] = item
                    saved += 1
                result.append(self.brief(item))
            if len(data["candidates"]) > MAX_CANDIDATES:
                # Adopted candidates carry the link back to the reference video; the oldest other ones go first.
                removable = sorted((item for item in data["candidates"] if item["status"] not in ("adopted", "writing", "produced")), key=lambda item: item["saved_at"])
                drop = {item["candidate_id"] for item in removable[:len(data["candidates"]) - MAX_CANDIDATES]}
                data["candidates"] = [item for item in data["candidates"] if item["candidate_id"] not in drop]
            self.write(data)
        return {"saved": saved, "duplicate": duplicate, "candidates": result}

    @staticmethod
    def brief(item):
        return {"candidate_id": item["candidate_id"], "product": item["product"], "code": item["direction"]["code"],
                "name": item["direction"]["name"], "status": item["status"], "direction_id": item["direction_id"],
                "writing_task_id": item.get("writing_task_id", ""), "script_id": item.get("script_id", ""),
                "job_id": item.get("job_id", ""), "saved_at": item["saved_at"],
                "variant_ref": copy.deepcopy(item.get("variant_ref")), "direction_error": item.get("direction_error", ""),
                "production_error": item.get("production_error", "")}

    def remember_production_error(self, identifier, error=""):
        """Keep a specific automatic-production failure on the saved card."""
        with self.lock:
            data = self.read()
            item = next((entry for entry in data["candidates"] if entry["candidate_id"] == identifier), None)
            if item is None:
                raise CandidateError(404, "这条候选方向不存在，可能已被清理。")
            value = _text(error, 2000)
            if item.get("production_error", "") != value:
                item["production_error"] = value
                self.write(data)
            return copy.deepcopy(item)

    def remember_direction(self, identifier, *, direction_id=None, variant_ref=None, error="", complete=False):
        """Keep catalog membership without changing the card's production state.

        A completion marker is written last, after script and job associations.
        Retrying an unchanged result does not rewrite business data.
        """
        with self.lock:
            data = self.read()
            item = next((entry for entry in data["candidates"] if entry["candidate_id"] == identifier), None)
            if item is None:
                raise CandidateError(404, "这条候选方向不存在，可能已被清理。")
            before = copy.deepcopy(item)
            if direction_id is not None:
                item["direction_id"] = _text(direction_id, 200)
                item["variant_ref"] = copy.deepcopy(variant_ref)
            item["direction_error"] = _text(error, 2000)
            item["association_version"] = 1 if complete and not error else 0
            if before != item:
                self.write(data)
            return copy.deepcopy(item)

    def get(self, identifier):
        with self.lock:
            item = next((entry for entry in self.read()["candidates"] if entry["candidate_id"] == identifier), None)
        if item is None:
            raise CandidateError(404, "这条候选方向不存在，可能已被清理。")
        return item

    def link(self, identifier, *, writing_task_id=None, writing_direction_id=None, script_id=None, job_id=None, now=None):
        """Remember the 写稿 round, script version and 制作单 made from this candidate. A running round marks it writing
        and a job marks it produced, unless it was already adopted as a direction, which stays the stronger state."""
        with self.lock:
            data = self.read()
            item = next((entry for entry in data["candidates"] if entry["candidate_id"] == identifier), None)
            if item is None:
                raise CandidateError(404, "这条候选方向不存在，可能已被清理。")
            if writing_task_id is not None:
                # A new round replaces whatever an earlier, archived or deleted 制作单 was made from.
                item.update(writing_task_id=_text(writing_task_id, 80), writing_direction_id=_text(writing_direction_id or "", 200),
                            script_id="", job_id="", association_version=0)
                if item["status"] != "adopted":
                    item.update(status="writing", status_at=now or _now())
            if script_id is not None:
                if item.get("script_id") != _text(script_id, 80):
                    item["association_version"] = 0
                item["script_id"] = _text(script_id, 80)
            if job_id is not None:
                if item.get("job_id") != _text(job_id, 80):
                    item["association_version"] = 0
                item["job_id"] = _text(job_id, 80)
                if item["job_id"]:
                    item.pop("production_error", None)
                if item["status"] != "adopted":
                    item.update(status="produced", status_at=now or _now())
            self.write(data)
            return dict(item)

    def list(self, product=None, status=None):
        if status not in (None, "", "all", *STATUSES):
            raise CandidateError(400, "候选状态筛选无效。")
        key = product_key(product) if product else None
        with self.lock:
            items = self.read()["candidates"]
        items = [item for item in items if (not key or item["product_key"] == key) and (status in (None, "", "all") or item["status"] == status)]
        return sorted(items, key=lambda item: item["saved_at"], reverse=True)

    def counts(self, product=None):
        key = product_key(product) if product else None
        with self.lock:
            items = [item for item in self.read()["candidates"] if not key or item["product_key"] == key]
        return {name: sum(item["status"] == name for item in items) for name in STATUSES}

    def set_status(self, identifier, status, direction_id="", *, direction_exists=None, now=None):
        if status not in STATUSES:
            raise CandidateError(400, "候选状态无效。")
        direction_id = _text(direction_id, 200)
        if status == "adopted" and not direction_id:
            raise CandidateError(400, "采纳时要写明变成了哪个方向。")
        with self.lock:
            data = self.read()
            item = next((entry for entry in data["candidates"] if entry["candidate_id"] == identifier), None)
            if item is None:
                raise CandidateError(404, "这条候选方向不存在，可能已被清理。")
            if status == "writing" and not item.get("writing_task_id"):
                raise CandidateError(400, "这条候选还没有写稿任务。")
            if status == "produced" and not item.get("job_id"):
                raise CandidateError(400, "这条候选还没有制作单。")
            if status == "adopted" and direction_exists and not direction_exists(item["product"], direction_id):
                raise CandidateError(404, "工作台里没有这个方向编号。")
            if status == "adopted":
                if item.get("variant_ref") and item.get("direction_id") != direction_id:
                    raise CandidateError(409, "这条候选已自动归入对应方向，不能通过处理状态更换方向。")
                item["direction_id"] = direction_id
            item.update(status=status, status_at=now or _now())
            self.write(data)
            return dict(item)

    def for_reference(self, video_id):
        if not re.fullmatch(r"\d{6,25}", str(video_id or "")):
            raise CandidateError(400, "视频 ID 无效。")
        with self.lock:
            items = [item for item in self.read()["candidates"] if item["reference"]["video_id"] == video_id]
        return sorted(items, key=lambda item: item["saved_at"])
