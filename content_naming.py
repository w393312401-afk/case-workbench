"""Read-only list names shared by the home page and content workflow."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import re


_SEPARATORS = " \t\r\n｜|·:：—–-、，,"
_VARIANT = re.compile(
    r"^(?:(?:执行变体|变体|方案|版本)\s*[#＃]?\s*(\d+|[A-Za-z])|第\s*(\d+)\s*条)"
    r"(?=$|[\s｜|·:：—–\-、，,])", re.IGNORECASE)
_TOPIC_LINE = re.compile(r"^(?:制作单|文案标题|内容主题|写稿主题|主题|标题)\s*[：:]\s*(.+)$")
_GENERIC = {"空白创作", "空白写稿", "未命名内容", "未命名制作单", "创作任务", "候选稿", "稿件", "视频", "图文", "脚本"}
_FORMS = {"video": "视频", "carousel": "图文", "task": "候选稿", "script": "脚本"}
_BEIJING = timezone(timedelta(hours=8))


def _text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def _first_line(value):
    if not isinstance(value, str):
        return ""
    return next((_text(line.strip().strip("*`# ")) for line in value.splitlines() if line.strip()), "")


def _clean(value, direction=""):
    """Remove only this direction's exact prefix and explicit variant labels."""
    topic, variant = _text(value), ""
    if direction and topic.startswith(direction):
        suffix = topic[len(direction):]
        if not suffix or suffix[0] in _SEPARATORS:
            topic = suffix.lstrip(_SEPARATORS)
    while match := _VARIANT.match(topic):
        variant = variant or match.group(1) or match.group(2)
        topic = topic[match.end():].lstrip(_SEPARATORS)
    # Existing manual names sometimes already carry their media label.
    suffix = re.search(r"\s*[｜|·]\s*(?:视频|图文|候选稿|脚本|复盘结果|审稿结果)(?:方案\s*(\d+|[A-Za-z]))?\s*$", topic)
    if suffix:
        variant = variant or suffix.group(1) or ""
        topic = topic[:suffix.start()]
    return ("" if topic in _GENERIC else topic), variant


def task_topic_hint(instruction):
    """Extract an explicit title from the first full request, never its boilerplate."""
    if not isinstance(instruction, str):
        return ""
    for line in instruction.splitlines():
        line = re.sub(r"^[\s#>*\-]+", "", line).replace("**", "").strip()
        match = _TOPIC_LINE.match(line)
        if match:
            return _text(match.group(1).strip("`* "))[:500]
    return ""


def _opening(row, kind):
    if kind == "carousel":
        slides = row.get("slides") or []
        slide = next((value for value in slides if isinstance(value, dict)), {})
        titles = slide.get("title_lines") or []
        english = " ".join(value for value in titles if isinstance(value, str)) if isinstance(titles, list) else ""
        return _text(slide.get("title_zh")) or _text(slide.get("body_zh")) or _text(english) or _text(slide.get("body"))
    if kind == "video":
        lines = [value for value in row.get("lines") or [] if isinstance(value, dict)]
        return next((_text(line.get("zh")) for line in lines if _text(line.get("zh"))), "") or next(
            (_text(line.get("spoken")) for line in lines if _text(line.get("spoken"))), "")
    if kind == "script":
        return _first_line(row.get("translation_zh")) or _first_line(row.get("text"))
    return ""


def content_title(row, kind, *, direction="", script=None, task=None):
    """Describe this record's topic and form; exact provenance alone supplies fallbacks."""
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    review_video = context.get("review_video") if isinstance(context.get("review_video"), dict) else {}
    originals = ([review_video.get("headline"), review_video.get("title")] if kind == "task" and row.get("action") == "retrospective" else [])
    originals += [row.get("title"), context.get("title"), row.get("case_title")]
    parsed = [_clean(value, direction) for value in originals]
    topic = next((value for value, _ in parsed if value), "")
    variant = next((label for _, label in parsed if label), "")
    if not variant:
        # variant_index is a candidate array index, not a user-facing number.
        label = row.get("variant_label")
        if isinstance(label, str):
            _, variant = _clean(label, direction)
            if not variant and re.fullmatch(r"\d+|[A-Za-z]", label.strip()):
                variant = label.strip()
    if not topic:
        topic = _clean(_opening(row, kind), direction)[0]
    if not topic and kind == "task":
        topic = _clean(context.get("writing_topic"), direction)[0]
        if not topic:
            rounds = row.get("rounds") or []
            first = rounds[0] if rounds and isinstance(rounds[0], dict) else {}
            topic = _clean(task_topic_hint(first.get("instruction")), direction)[0]
    if not topic and script:
        topic = _clean(script.get("title"), direction)[0] or _clean(_opening(script, "script"), direction)[0]
    task_context = (task or {}).get("context") or context
    if not isinstance(task_context, dict):
        task_context = {}
    if not topic:
        topic = _clean(task_context.get("writing_topic"), direction)[0]
    if not topic:
        topic = _text(task_context.get("direction_title"))
        short_code = direction.rsplit("-", 1)[-1] if direction else ""
        topic = _clean(topic, short_code)[0]
    topic = topic or "未命名内容"
    limit = 36 if re.search(r"[\u3400-\u9fff]", topic) else 72
    if len(topic) > limit:
        topic = topic[:limit - 1].rstrip() + "…"
    form = ({"review": "审稿结果", "retrospective": "复盘结果"}.get(row.get("action"), _FORMS[kind])
            if kind == "task" else _FORMS[kind])
    form += f"方案 {variant}" if variant and kind != "task" else ""
    return f"{topic}｜{form}"


def task_created_at(row):
    """Creation is independent of later revisions and queue management."""
    context = row.get("context") if isinstance(row.get("context"), dict) else {}
    rounds = row.get("rounds") or []
    first = rounds[0] if rounds and isinstance(rounds[0], dict) else {}
    return row.get("created_at") or first.get("created_at") or context.get("captured_at") or ""


def task_display_metadata(row):
    """Pure presentation fields shared by task details, lists and search; never rewrite saved names."""
    snapshot = row.get("snapshot") if isinstance(row.get("snapshot"), dict) else {}
    frozen = snapshot.get("creation_context") if isinstance(snapshot.get("creation_context"), dict) else {}
    public = row.get("context") if isinstance(row.get("context"), dict) else {}
    context = {**frozen, **public}
    direction = context.get("direction") if isinstance(context.get("direction"), dict) else {}
    title = content_title({**row, "context": context}, "task", direction=row.get("direction_id") or context.get("direction_id") or direction.get("direction_id") or "")
    created_at = task_created_at(row)
    label = ""
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=_BEIJING) if stamp.tzinfo is None else stamp.astimezone(_BEIJING)
        label = stamp.strftime("%m/%d %H:%M") + " 发起"
    except (ValueError, TypeError, AttributeError):
        pass
    return {"display_title": title, "created_at": created_at, "task_started_label": label}


def label_task_starts(items):
    """Disambiguate same-product/direction tasks before filtering and pagination."""
    groups = {}
    for item in items:
        item["task_started_label"] = ""
        if item["kind"] == "task" and item.get("direction_id"):
            groups.setdefault((item.get("product"), item["direction_id"]), []).append(item)
    for rows in groups.values():
        if len(rows) < 2:
            continue
        stamps = {}
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                # Legacy timestamps without a zone were written as local time.
                stamp = stamp.replace(tzinfo=_BEIJING) if stamp.tzinfo is None else stamp.astimezone(_BEIJING)
            except (ValueError, TypeError, AttributeError):
                stamp = None
            stamps[row["id"]] = stamp
        minutes = Counter(stamp.strftime("%Y/%m/%d %H:%M") for stamp in stamps.values() if stamp)
        labels = {}
        for row in rows:
            stamp = stamps[row["id"]]
            if stamp:
                minute = stamp.strftime("%Y/%m/%d %H:%M")
                labels[row["id"]] = stamp.strftime("%Y/%m/%d %H:%M:%S") if minutes[minute] > 1 else minute
            else:
                labels[row["id"]] = "发起时间未记录"
        repeated = Counter(labels.values())
        for row in rows:
            label = labels[row["id"]]
            row["task_started_label"] = label + (" 发起" if stamps[row["id"]] else "")
            if repeated[label] > 1:
                row["task_started_label"] += f" · {row['id'].removeprefix('task:')}"
