"""反面素材库: our own videos that did badly, kept so new content can be checked against them.

Two kinds, both read from the per-video sales exports (performance_store.videos_readonly):
- low:  low play — exported views below `low_views`.
- weak: high play, low conversion — views at or above `high_views` with few orders: at most `max_orders`, or at most
        `orders_per_10k` per ten thousand views when that allows more (a very large video with 1 order still counts).

Only videos whose data has had time to settle count: posted at least `min_age_days` before the shop's last exported
day, and (unless `recent_days` is 0) no more than `recent_days` before that. Every threshold is a saved setting.

Each automatic entry carries a suspected reason worked out from its own figures (click-through and click-to-order
against the medians of comparable videos, or its product's median views). A person can write their own reason, take
an entry out (kept as hidden, can be put back), pin a video that the thresholds miss, or add a video by hand that the
exports do not have. All of that is saved in the runtime folder at `performance/negative-library.json`, which the
restart backup already covers; the sales database itself is never changed.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import statistics
import threading
from uuid import uuid4

from creation_store import CreationError
from production_operations import durable_json

KINDS = {"low": "低播放", "weak": "高播放低转化"}
DEFAULTS = {"low_views": 2500, "high_views": 5000, "max_orders": 2, "orders_per_10k": 1.0, "recent_days": 90, "min_age_days": 14}
# (min, max, integer) for each setting.
LIMITS = {"low_views": (0, 10_000_000, True), "high_views": (0, 100_000_000, True), "max_orders": (0, 100_000, True),
          "orders_per_10k": (0, 1000, False), "recent_days": (0, 3650, True), "min_age_days": (0, 365, True)}
MAX_MANUAL = 500
# A rate below this share of the comparable median is named as the suspected weak step.
WEAK_RATE = 0.85
MAX_REASON = 1000
DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value else None


def _median(values):
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def _day(text):
    try:
        return date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


class NegativeLibrary:
    def __init__(self, runtime_root):
        self.path = Path(runtime_root) / "performance" / "negative-library.json"
        self.lock = threading.RLock()

    # ---- storage -------------------------------------------------------------
    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as error:
            raise CreationError(500, f"反面素材库文件读不了：{error}") from None
        data = data if isinstance(data, dict) else {}
        settings = {**DEFAULTS, **{k: v for k, v in (data.get("settings") or {}).items() if k in DEFAULTS and _number(v) is not None}}
        notes = {str(k): v for k, v in (data.get("notes") or {}).items() if isinstance(v, dict)}
        manual = [row for row in data.get("manual") or [] if isinstance(row, dict) and row.get("id")]
        return {"settings": settings, "notes": notes, "manual": manual}

    def _write(self, data):
        durable_json(self.path, data)

    # ---- view ----------------------------------------------------------------
    def public(self, videos, shop=None):
        """The library for one shop (None: every shop) from the sales rows. `videos` is read, never changed."""
        with self.lock:
            data = self._read()
        return build(videos, data, shop)

    # ---- changes -------------------------------------------------------------
    def mutate(self, body, known=None):
        """Apply one change. `known(ids)` returns the video IDs the sales exports have: a hand-added entry naming one
        pins that video instead, so its figures keep coming from the data."""
        if not isinstance(body, dict):
            raise CreationError(400, "请求格式不对。")
        action = body.get("action")
        with self.lock:
            data = self._read()
            if action == "settings":
                data["settings"] = clean_settings(body.get("settings"))
            elif action == "reset_settings":
                data["settings"] = dict(DEFAULTS)
            elif action == "note":
                video_id = str(body.get("video_id") or "").strip()
                if not re.fullmatch(r"\d{6,25}", video_id):
                    raise CreationError(400, "视频 ID 不对。")
                note = dict(data["notes"].get(video_id) or {})
                if "reason" in body:
                    note["reason"] = _text(body.get("reason"), MAX_REASON, "原因")
                if "hidden" in body:
                    note["hidden"] = bool(body.get("hidden"))
                if "pinned" in body:
                    pinned = body.get("pinned")
                    if pinned not in (None, "", *KINDS):
                        raise CreationError(400, "类别只能是低播放或高播放低转化。")
                    note["pinned"] = pinned or None
                note = {k: v for k, v in note.items() if v not in (None, "", False)}
                if note:
                    data["notes"][video_id] = note | {"updated_at": _now()}
                else:
                    data["notes"].pop(video_id, None)
            elif action == "add_manual":
                if len(data["manual"]) >= MAX_MANUAL:
                    raise CreationError(400, f"手动条目最多 {MAX_MANUAL} 条。")
                entry = clean_manual(body.get("entry"))
                found = re.search(r"(?<!\d)(\d{15,25})(?!\d)", entry["link"])
                if found and known and found.group(1) in known({found.group(1)}):
                    video_id = found.group(1)
                    note = dict(data["notes"].get(video_id) or {})
                    note.update(pinned=entry["kind"], hidden=False, updated_at=_now())
                    if entry["reason"]:
                        note["reason"] = entry["reason"]
                    data["notes"][video_id] = {k: v for k, v in note.items() if v not in (None, "", False)}
                    self._write(data)
                    return data
                entry.update(id="NEG-" + uuid4().hex[:10], added_at=_now())
                data["manual"].append(entry)
            elif action == "update_manual":
                index = next((i for i, row in enumerate(data["manual"]) if row["id"] == body.get("id")), None)
                if index is None:
                    raise CreationError(404, "找不到这条手动条目。")
                old = data["manual"][index]
                data["manual"][index] = clean_manual(body.get("entry")) | {"id": old["id"], "added_at": old.get("added_at"), "updated_at": _now()}
            elif action == "delete_manual":
                before = len(data["manual"])
                data["manual"] = [row for row in data["manual"] if row["id"] != body.get("id")]
                if len(data["manual"]) == before:
                    raise CreationError(404, "找不到这条手动条目。")
            else:
                raise CreationError(400, "不认识的操作。")
            self._write(data)
            return data


def _text(value, limit, label):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CreationError(400, f"{label}必须是文字。")
    value = value.strip()
    if len(value) > limit:
        raise CreationError(400, f"{label}太长了（最多 {limit} 字）。")
    return value


def clean_settings(raw):
    if not isinstance(raw, dict):
        raise CreationError(400, "门槛格式不对。")
    result = {}
    for key, default in DEFAULTS.items():
        value = raw.get(key, default)
        if isinstance(value, str) and value.strip():
            try:
                value = float(value)
            except ValueError:
                value = None
        value = _number(value)
        low, high, integer = LIMITS[key]
        if value is None or not low <= value <= high:
            raise CreationError(400, f"门槛 {key} 要在 {low} 到 {high} 之间。")
        result[key] = int(round(value)) if integer else round(float(value), 2)
    if result["low_views"] > result["high_views"]:
        raise CreationError(400, "低播放门槛不能高于高播放门槛。")
    return result


def clean_manual(raw):
    if not isinstance(raw, dict):
        raise CreationError(400, "条目格式不对。")
    kind = raw.get("kind")
    if kind not in KINDS:
        raise CreationError(400, "请选择类别：低播放或高播放低转化。")
    entry = {"kind": kind, "product": _text(raw.get("product"), 40, "产品"), "title": _text(raw.get("title"), 200, "标题"),
             "link": _text(raw.get("link"), 500, "链接或视频 ID"), "reason": _text(raw.get("reason"), MAX_REASON, "原因"),
             "posted_on": _text(raw.get("posted_on"), 10, "发布日期")}
    if entry["posted_on"] and not (DAY.match(entry["posted_on"]) and _day(entry["posted_on"])):
        raise CreationError(400, "发布日期要写成 2026-11-01 这样。")
    for key, label in (("views", "播放"), ("orders", "出单数")):
        value = raw.get(key)
        if value in (None, ""):
            entry[key] = None
            continue
        try:
            value = float(str(value).replace(",", "").strip())
        except ValueError:
            raise CreationError(400, f"{label}要填数字。") from None
        if not 0 <= value < 1e10:
            raise CreationError(400, f"{label}不能是负数。")
        entry[key] = int(value)
    if not (entry["title"] or entry["link"]):
        raise CreationError(400, "至少填标题或链接/视频 ID。")
    return entry


# ---- classification -----------------------------------------------------------
def classify(video, settings):
    """'low', 'weak' or None for one in-scope video. Videos without views (or, for weak, without orders) are None."""
    views, orders = _number(video.get("views")), _number(video.get("orders"))
    if views is None:
        return None
    if views < settings["low_views"]:
        return "low"
    if views >= settings["high_views"] and orders is not None:
        if orders <= max(settings["max_orders"], views / 10000 * settings["orders_per_10k"]):
            return "weak"
    return None


def suspect(video, kind, medians, product_views, settings):
    """A suspected reason from this video's own figures; a person's note replaces it."""
    if kind == "low":
        median = product_views.get(video.get("product"))
        if median is None:
            return "播放低；同产品没有可比视频，原因待人工判断。"
        if median < settings["low_views"]:
            return f"同产品近期视频普遍播放低（中位 {median:,.0f}），更可能是产品或流量问题，不全是这条内容。"
        return f"同产品中位播放 {median:,.0f}，这条明显偏低：多半是开头没留住人，或选题不吸引。"
    if not (video.get("products") or "").strip():
        return "没挂商品。"
    ctr, ctor = _number(video.get("ctr_pct")), _number(video.get("ctor_pct"))
    if ctr is None:
        return "播放高但出单少；缺商品点击数据，原因待人工判断。"
    # Each rate as a share of the comparable videos' median; only one clearly below it (WEAK_RATE) is named.
    ratios = {key: value / medians[key] for key, value in (("ctr", ctr), ("ctor", ctor))
              if value is not None and medians[key]}
    worst = min(ratios, key=ratios.get) if ratios else None
    if worst == "ctr" and ratios["ctr"] < WEAK_RATE:
        return (f"看的人多但很少点商品（点击率 {ctr:.2f}%，同类中位 {medians['ctr']:.2f}%）："
                "内容和产品关系弱，或没给购买理由。")
    if worst == "ctor" and ratios["ctor"] < WEAK_RATE:
        return (f"有人点商品但不买（点进下单 {ctor:.2f}%，同类中位 {medians['ctor']:.2f}%）："
                "价格、详情页或视频里的承诺没接上。")
    return ("点击率和点进下单都接近同类，出单少主要是播放还不够大；"
            "如果仍算反面，多半是吸引来的人群不对，待人工判断。")


def build(videos, data, shop=None):
    settings = data["settings"]
    rows = [v for v in videos if shop is None or v.get("shop") == shop]
    data_until = {}
    for v in rows:
        if v.get("last_data"):
            data_until[v["shop"]] = max(data_until.get(v["shop"], ""), v["last_data"])
    scoped, counts = [], {"no_date": 0, "too_new": 0, "older": 0, "no_views": 0}
    for v in rows:
        posted, until = _day(v.get("posted_on") or v.get("posted_at")), _day(data_until.get(v.get("shop")))
        if not posted or not until:
            counts["no_date"] += 1
            continue
        age = (until - posted).days
        if age < settings["min_age_days"]:
            counts["too_new"] += 1
            continue
        if settings["recent_days"] and age > settings["recent_days"] + settings["min_age_days"]:
            counts["older"] += 1
            continue
        if _number(v.get("views")) is None:
            counts["no_views"] += 1
        scoped.append(v)
    high = [v for v in scoped if (_number(v.get("views")) or 0) >= settings["high_views"]]
    medians = {"ctr": _median([_number(v.get("ctr_pct")) for v in high]),
               "ctor": _median([_number(v.get("ctor_pct")) for v in high])}
    by_product = {}
    for v in scoped:
        if _number(v.get("views")) is not None:
            by_product.setdefault(v.get("product"), []).append(v["views"])
    product_views = {key: statistics.median(values) for key, values in by_product.items()}

    notes, entries, hidden = data["notes"], {"low": [], "weak": []}, []
    in_scope = {v["video_id"] for v in scoped}
    by_id = {v["video_id"]: v for v in rows}
    for v in scoped:
        kind = classify(v, settings)
        note = notes.get(v["video_id"]) or {}
        source = "auto"
        if note.get("pinned"):
            kind, source = note["pinned"], "pinned"
        if not kind:
            continue
        row = _row(v, kind, source, note, medians, product_views, settings)
        (hidden if note.get("hidden") else entries[kind]).append(row)
    # A pinned video outside the date window still shows: a person chose it.
    for video_id, note in notes.items():
        if note.get("pinned") and video_id not in in_scope and video_id in by_id:
            v = by_id[video_id]
            row = _row(v, note["pinned"], "pinned", note, medians, product_views, settings)
            (hidden if note.get("hidden") else entries[note["pinned"]]).append(row)
    for kind in entries:
        entries[kind].sort(key=lambda r: (r["posted_on"] or "", r["video_id"]), reverse=True)
    manual = sorted(copy.deepcopy(data["manual"]), key=lambda r: (r.get("posted_on") or "", r.get("added_at") or ""), reverse=True)
    for row in manual:
        row["kind_label"] = KINDS.get(row.get("kind"), "")
    return {
        "settings": settings, "defaults": DEFAULTS, "kinds": KINDS,
        "data_until": max(data_until.values()) if data_until else None,
        "low": entries["low"], "weak": entries["weak"], "hidden": hidden, "manual": manual,
        "medians": medians, "counts": counts | {"videos": len(rows), "in_scope": len(scoped)},
    }


def _row(v, kind, source, note, medians, product_views, settings):
    return {
        "video_id": v["video_id"], "kind": kind, "kind_label": KINDS[kind], "source": source, "shop": v.get("shop"),
        "product": v.get("product"), "account": v.get("account"), "headline": v.get("headline") or v.get("title"),
        "posted_on": v.get("posted_on"), "views": v.get("views"), "orders": v.get("orders"), "gmv": v.get("gmv"),
        "clicks": v.get("clicks"), "ctr_pct": v.get("ctr_pct"), "ctor_pct": v.get("ctor_pct"),
        "auto_reason": suspect(v, kind, medians, product_views, settings), "reason": note.get("reason") or "",
        "hidden": bool(note.get("hidden")),
    }
