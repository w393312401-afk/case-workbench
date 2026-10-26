"""拆解定位：一条案例视频赢在哪一环、拿哪几条没卖出去的视频对照、观众评论、结构化拆解字段。

Everything here is read-only over the sales data, review library and case library, except the
structured tags, which live in a workbench sidecar (runtime/evidence/case-tags.json) because the
case snapshot schema in the skill's case_store has a fixed field list.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import threading

MIN_VIEWS = 3000          # peers below this are too small to set a benchmark
MIN_CLICKS = 30           # a conversion rate on fewer clicks is noise
TAG_FIELDS = ("hook_types", "skeleton_id", "lead_code", "progression", "carrier", "duration_sec", "opening", "funnel")
FUNNEL_LABELS = {
    "both": "双强：流量和转化都好", "traffic": "流量型：赢在播放，转化一般", "conversion": "转化型：播放一般，买的人多",
    "views_no_orders": "有播放没出单", "plain": "流量和转化都不突出", "unknown": "数据不够，暂不判断",
}


def _num(value):
    return value if isinstance(value, (int, float)) and math.isfinite(value) else None


def _ctor(video):
    clicks, orders = _num(video.get("clicks")), _num(video.get("orders"))
    return orders / clicks if clicks and clicks >= MIN_CLICKS and orders is not None else None


def _shop_views(video):
    """The export's own views: rates and peer thresholds stay on it where public-page views replaced the display."""
    return _num(video.get("views_shop", video.get("views")))


def _ctr(video):
    clicks, views = _num(video.get("clicks")), _shop_views(video)
    return clicks / views if views and clicks is not None else None


def _per_k(video):
    views, orders = _shop_views(video), _num(video.get("orders"))
    return orders * 1000 / views if views and orders is not None else None


def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * q
    low, high = math.floor(index), math.ceil(index)
    return values[low] + (values[high] - values[low]) * (index - low)


def _peers(video, videos):
    product = video.get("product")
    return [v for v in videos if v.get("product") == product and (_shop_views(v) or 0) >= MIN_VIEWS]


def funnel(video, videos):
    """Which stage this video wins at, against videos of the same product with at least MIN_VIEWS views."""
    peers = _peers(video, videos)
    views = _shop_views(video) or 0
    ctrs = [x for x in map(_ctr, peers) if x is not None]
    ctors = [x for x in map(_ctor, peers) if x is not None]
    bench = {"peers": len(peers), "views_p75": _quantile([_shop_views(v) for v in peers], 0.75),
             "ctr_median": statistics.median(ctrs) if ctrs else None, "ctor_median": statistics.median(ctors) if ctors else None}
    mine = {"views": views, "orders": _num(video.get("orders")), "clicks": _num(video.get("clicks")),
            "ctr": _ctr(video), "ctor": _ctor(video), "orders_per_1k_views": _per_k(video)}
    if len(peers) < 10 or views < MIN_VIEWS or mine["ctor"] is None or bench["ctor_median"] is None:
        key = "views_no_orders" if views >= (bench["views_p75"] or math.inf) and not mine["orders"] else "unknown"
    else:
        traffic = views >= bench["views_p75"]
        conversion = mine["ctor"] >= bench["ctor_median"] * 1.5
        if traffic and not mine["orders"]:
            key = "views_no_orders"
        else:
            key = {(True, True): "both", (True, False): "traffic", (False, True): "conversion"}.get((traffic, conversion), "plain")
    reasons = []
    if bench["views_p75"]:
        reasons.append(f"播放 {views:,.0f}，同产品前 25% 的门槛是 {bench['views_p75']:,.0f}")
    if mine["ctor"] is not None and bench["ctor_median"]:
        reasons.append(f"点击转化率 {mine['ctor']:.2%}，同产品中位数 {bench['ctor_median']:.2%}")
    if mine["ctr"] is not None and bench["ctr_median"]:
        reasons.append(f"点击率 {mine['ctr']:.2%}，中位数 {bench['ctr_median']:.2%}")
    focus = {"both": "开头和购买理由都值得拆", "traffic": "重点拆开头、节奏和完播，购买理由不要照搬",
             "conversion": "重点拆购买理由、证明画面和收口，开头不一定有借鉴价值",
             "views_no_orders": "拆它为什么没卖出去：购买理由缺在哪", "plain": "不建议当正面样板",
             "unknown": "先补数据再判断"}[key]
    return {"key": key, "label": FUNNEL_LABELS[key], "focus": focus, "reasons": reasons, "video": mine, "benchmark": bench}


def contrasts(video, videos, limit=2):
    """Same-product videos with similar reach that sold far less, posted close in time."""
    orders = _num(video.get("orders")) or 0
    views = _shop_views(video) or 0
    if views < MIN_VIEWS:
        return []
    try:
        posted = date.fromisoformat(str(video.get("posted_on") or "")[:10])
    except ValueError:
        posted = None
    ceiling = max(1, orders * 0.25)
    picks = []
    for other in _peers(video, videos):
        if other.get("video_id") == video.get("video_id"):
            continue
        other_views, other_orders = _shop_views(other) or 0, _num(other.get("orders")) or 0
        if not 0.5 <= other_views / views <= 2 or other_orders > ceiling or other_orders >= orders:
            continue
        gap = 0
        if posted:
            try:
                gap = abs((date.fromisoformat(str(other.get("posted_on") or "")[:10]) - posted).days)
            except ValueError:
                gap = 999
            if gap > 60:
                continue
        picks.append((abs(math.log(other_views / views)) + gap / 60, other))
    picks.sort(key=lambda item: item[0])
    return [{"video_id": v["video_id"], "headline": v.get("headline") or "", "views": v.get("views"), "orders": v.get("orders"),
             "ctr": _ctr(v), "ctor": _ctor(v), "posted_on": v.get("posted_on"), "account": v.get("account"),
             "case_id": v.get("case_id")} for _, v in picks[:limit]]


def comments(reviews, video_id, limit=40):
    """Top comments of this video by likes, with tag counts; empty when the library has none."""
    try:
        found = reviews.search({"video": str(video_id)}, limit=limit, sort="likes")
    except Exception:
        return {"total": 0, "items": [], "topics": {}, "nature": {}}
    items = [{"text": str(r.get("text") or "")[:300], "likes": r.get("likes"), "topics": (r.get("tags") or {}).get("topic", []),
              "nature": (r.get("tags") or {}).get("nature", []), "is_reply": bool(r.get("is_reply"))} for r in found.get("reviews", [])]
    facets = found.get("facets") or {}
    return {"total": found.get("total", 0), "items": items,
            "topics": {k: v for k, v in (facets.get("topic") or {}).items() if k != "none"},
            "nature": {k: v for k, v in (facets.get("nature") or {}).items() if k != "none"}}


# ---- structured tags ---------------------------------------------------------------------------

def _frontmatter(path):
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return {}
    match = re.match(r"---\s*\n(.*?)\n---", text, re.S)
    if not match:
        return {}
    result = {}
    for line in match.group(1).splitlines():
        found = re.match(r"([a-z_]+):\s*(.*?)\s*(?:#.*)?$", line)
        if not found:
            continue
        key, raw = found.groups()
        if key == "hook_types":
            result[key] = [item.strip().strip("'\"") for item in raw.strip("[]").split(",") if item.strip()]
        elif key == "skeleton_id" and raw not in {"", "null"}:
            result[key] = raw.strip("'\"")
        elif key == "duration_sec" and raw not in {"", "null"}:
            try:
                result[key] = float(raw)
            except ValueError:
                pass
    return result


def resolve_source(skill_root, file_path):
    """Map a stored (possibly Windows) source path onto the local skill folder."""
    raw = str(file_path or "").replace("\\", "/")
    match = re.search(r"(sources/.+)$", raw)
    if not match:
        return None
    path = Path(skill_root) / match.group(1)
    return path if path.is_file() else None


class CaseTags:
    def __init__(self, runtime):
        self.path = Path(runtime) / "evidence" / "case-tags.json"
        self.lock = threading.Lock()

    def read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, case_id):
        return self.read().get(case_id) or {}

    def update(self, case_id, fields, source):
        clean = {key: value for key, value in (fields or {}).items() if key in TAG_FIELDS and value not in (None, "", [])}
        if not clean:
            return self.get(case_id)
        with self.lock:
            data = self.read()
            entry = data.get(case_id) or {}
            sources = entry.get("sources") or {}
            for key, value in clean.items():
                # Something the analysis or a person set is not overwritten by a later frontmatter backfill.
                if source == "frontmatter" and sources.get(key) not in (None, "frontmatter"):
                    continue
                entry[key] = value
                sources[key] = source
            entry["sources"] = sources
            entry["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            data[case_id] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
            return entry

    def derive(self, case, skill_root):
        """Tags that can be read from the case itself and its source document, without any model."""
        current = case.get("current") or {}
        found = {}
        source = current.get("source_case") or {}
        path = resolve_source(skill_root, source.get("file_path"))
        if path:
            found.update(_frontmatter(path))
        direction = str((current.get("test_direction") or {}).get("direction_id") or "")
        code = re.search(r"-([SAB]\d+)$", direction)
        if code:
            found["lead_code"] = code.group(1)
        transcript = current.get("transcript") or {}
        native = current.get("native_reference") or {}
        channels = {entry.get("channel") for entry in native.get("entries") or [] if entry.get("channel")}
        if transcript.get("text"):
            found["carrier"] = "口播"
        elif "on_screen" in channels:
            found["carrier"] = "屏显"
        return found


def transcript_gate(case):
    """Whether original lines may be quoted as verbatim reusable wording."""
    transcript = (case.get("current") or {}).get("transcript") or {}
    status = str(transcript.get("status") or "")
    checked = bool(re.search(r"人工(核对|听校|听审|核验)|^checked$|用户核对", status)) and not re.search(r"未(经)?人工|未逐", status)
    return {"has_transcript": bool(transcript.get("text")), "status": status, "verbatim_ok": checked,
            "rule": "原句已人工核对，可作逐字参考。" if checked else "转写未经人工逐字核对，只能当大意参考，不能当逐字可复用原句。"}


def tag_stats(store, tags, performance_videos, product):
    """Per hook type and skeleton: how many cases, and their orders per 1,000 views, within one product."""
    by_video = {v["video_id"]: v for v in performance_videos}
    groups = {"hook_types": {}, "skeleton_id": {}, "lead_code": {}, "funnel": {}}
    for entry in store.read_index()["cases"]:
        if product and (entry.get("product") or "").upper().find(product.upper()) < 0:
            continue
        video = by_video.get(str(((entry.get("current") or {}).get("metrics") or {}).get("video_id") or ""))
        per_k = _per_k(video) if video else None
        entry_tags = tags.get(entry["case_id"]) or {}
        for field in groups:
            values = entry_tags.get(field)
            for value in (values if isinstance(values, list) else [values] if values else []):
                bucket = groups[field].setdefault(str(value), {"cases": 0, "with_sales": 0, "orders_per_1k": []})
                bucket["cases"] += 1
                if per_k is not None:
                    bucket["with_sales"] += 1
                    bucket["orders_per_1k"].append(per_k)
    for field in groups.values():
        for bucket in field.values():
            values = bucket.pop("orders_per_1k")
            bucket["orders_per_1k_median"] = round(statistics.median(values), 2) if values else None
    return groups
