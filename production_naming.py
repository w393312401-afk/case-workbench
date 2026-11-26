"""Shared, read-only names for video production orders."""
from __future__ import annotations

import re


_SEPARATORS = " \t\r\n｜|·:：—–-、，,。"
# 「1:1 复刻」 says how the order was made, not what it is about; the header's replica link already says so.
_GENERIC_PREFIX = re.compile(
    r"^(?:(?:执行变体|变体|版本)\s*[#＃]?\s*(?:\d+|[A-Za-z])|第\s*\d+\s*条|1:1\s*复刻)"
    r"(?=$|[\s｜|·:：—–\-、，,。])", re.IGNORECASE)
# A variant's note on its direction chain (「方向链：S1 定角不用猜 → 每次补磨」) describes the plan, not this script.
_DIRECTION_NOTE = re.compile(r"^方向链\s*[:：]")
_CHINESE = re.compile(r"[\u3400-\u9fff]")


def _compact(value):
    return " ".join(str(value or "").split())


def _brief(value):
    value = _compact(value)
    limit = 36 if _CHINESE.search(value) else 72
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def timeline_label(item):
    """Latest active named timeline for the chosen voice, otherwise the latest active one."""
    timelines = [row for row in item.get("timelines") or []
                 if isinstance(row, dict) and not row.get("archived") and str(row.get("name") or "").strip()]
    take_id = item.get("selected_take")
    row = next((row for row in reversed(timelines) if take_id and row.get("take_id") == take_id), None)
    row = row or (timelines[-1] if timelines else None)
    return str(row["name"]).strip() if row else ""


def production_topic(item):
    """Use this order's concrete title or its own opening line as a brief topic."""
    original = _compact(item.get("title"))
    topic, code = original, _compact(item.get("direction_id"))
    if code and topic.startswith(code):
        suffix = topic[len(code):]
        if not suffix or suffix[0] in _SEPARATORS:
            topic = suffix.lstrip(_SEPARATORS)
    while match := _GENERIC_PREFIX.match(topic):
        topic = topic[match.end():].lstrip(_SEPARATORS)
    if _DIRECTION_NOTE.match(topic):
        topic = ""
    if not topic:
        lines = item.get("lines") or []
        opening = lines[0] if lines and isinstance(lines[0], dict) else {}
        topic = _compact(opening.get("zh")) or _compact(opening.get("spoken"))
    return _brief(topic or original or "未命名制作单")


def production_title(item):
    """One display rule; never rewrite the adopted title or script facts."""
    return "｜".join(part for part in (timeline_label(item), production_topic(item)) if part)
