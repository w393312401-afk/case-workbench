"""The real comment a video opens with, ready for the sticker template (2026-11-08, 方案 A).

「带证据写稿」 adds the skeleton opening's comment ids to the writing request, and a writing round also
freezes the comments in its material basket and those linked to its direction. From a production order
the chain script -> AI task finds them again, so the script format and the writing skill stay as they are. The words on the card are the commenter's own: only characters the template font
cannot draw are dropped, and an over-long comment is cut with an ellipsis, never rewritten.
"""
from __future__ import annotations

from pathlib import Path
import re
import textwrap

MARKER = "开头引用评论编号（成片贴纸用，不要写进稿子）："
LINE_WIDTH = 42  # Characters that fit the card at the template's text size (measured in Resolve 21.2, 2026-11-08).
MAX_LINES = 4
# The template's avatar circle on a 1080x1920 frame. The picture is the clip the comp sits on, and a clip
# smaller than the frame would make Fusion render the whole card at that small size, hence the full-frame canvas.
CANVAS = (1080, 1920)
AVATAR_CENTER = (235, 691)
AVATAR_SIZE = 84
INITIAL_FILL = (158, 171, 184, 255)
# Code points the template's Arial draws left to right: Latin, Greek, Cyrillic, punctuation, currency and the trademark sign.
DRAWABLE = ((0x20, 0x24F), (0x370, 0x4FF), (0x1E00, 0x1EFF), (0x2000, 0x206F), (0x20A0, 0x20BF), (0x2122, 0x2122))
EMOJI = re.compile("[\U00010000-\U0010ffff☀-➿️‍]")


SHIPPED_TEMPLATE = Path(__file__).resolve().parent / "templates" / "comment-sticker.comp"
SYNCED_NAME = "comment-sticker-synced.comp"
TEMPLATE_TIMELINE = "TikTok Comment Sticker - Editable"


class TemplateError(ValueError):
    pass


def _span(text, name):
    """Where one tool's block sits in a .comp file, or None."""
    found = re.search(r"\n\t\t" + re.escape(name) + r" = \w+ \{", text)
    if not found:
        return None
    position, depth = found.end(), 1
    while depth:
        depth += (text[position] == "{") - (text[position] == "}")
        position += 1
    return found.start(), position + (text[position] == ",")


def _without(text, name):
    span = _span(text, name)
    return text[:span[0]] + text[span[1]:] if span else text


def _merge_fed_by(text, source):
    """The Merge whose foreground is `source`, as (name, block)."""
    for name in re.findall(r"\n\t\t(\w+) = Merge \{", text):
        start, end = _span(text, name)
        if re.search(r'Foreground = Input \{\s*SourceOp = "' + source + '"', text[start:end]):
            return name, text[start:end]
    return None, ""


def adapt_template(text):
    """Turn the sticker as designed in Resolve (grey circle and initial) into the workbench's template, where the clip itself is the avatar."""
    missing = [name for name in ("CardShape", "AvatarCircle", "Username", "Comment", "MediaOut1") if not _span(text, name)]
    if missing:
        raise TemplateError(f"贴纸缺少这些节点：{'、'.join(missing)}。节点名称不能改。")
    text = _without(text, "MediaIn1")
    if 'SourceOp = "MediaIn1"' not in text:
        name, block = _merge_fed_by(text, "AvatarColor")
        if not name:
            raise TemplateError("贴纸里找不到头像节点 AvatarColor，节点名称不能改。")
        fed = re.sub(r'(Foreground = Input \{\s*SourceOp = )"AvatarColor"', r'\1"MediaIn1"', block)
        mask = '\n\t\t\t\tEffectMask = Input {\n\t\t\t\t\tSourceOp = "AvatarCircle",\n\t\t\t\t\tSource = "Mask",\n\t\t\t\t},'
        text = text.replace(block, fed.replace("Inputs = {", "Inputs = {" + mask, 1), 1)
        name, block = _merge_fed_by(text, "AvatarInitial")
        if name:
            below = re.search(r'Background = Input \{\s*SourceOp = "(\w+)"', block).group(1)
            text = _without(text, name).replace(f'SourceOp = "{name}"', f'SourceOp = "{below}"')
        text = _without(_without(text, "AvatarColor"), "AvatarInitial")
    frames = text.find("\n\tFrames = {")  # Window layout and this computer's cache paths.
    if frames >= 0:
        text = text[:frames] + "\n}\n"
    text = re.sub(r"\tCustomData = \{[^{}]*\},\n", "", text, count=1)
    tools = set(re.findall(r"\n\t\t(\w+) = \w+ \{", text)) | {"MediaIn1"}
    broken = sorted(set(re.findall(r'SourceOp = "(\w+)"', text)) - tools)
    if broken:
        raise TemplateError(f"贴纸里的节点连接不完整：{'、'.join(broken)}。")
    return text


def _input(text, tool, name, default):
    span = _span(text, tool)
    found = re.search(r"\b" + name + r" = Input \{ Value = (-?[\d.]+)", text[span[0]:span[1]]) if span else None
    return float(found.group(1)) if found else default


def _point(text, tool, name, default):
    span = _span(text, tool)
    found = re.search(r"\b" + name + r" = Input \{ Value = \{ (-?[\d.]+), (-?[\d.]+)", text[span[0]:span[1]]) if span else None
    return (float(found.group(1)), float(found.group(2))) if found else default


def template_metrics(text):
    """How much text fits and where the avatar goes, scaled from what was measured on the first template."""
    card_x, text_x = _point(text, "CardShape", "Center", (0.5, 0.5))[0], _point(text, "Comment", "Center", (0.5, 0.5))[0]
    room = card_x + _input(text, "CardShape", "Width", 0.5) / 2 - text_x
    x, y = _point(text, "AvatarCircle", "Center", (0.5, 0.5))
    return {"line_width": max(12, int(LINE_WIDTH * (0.034 / _input(text, "Comment", "Size", 0.1)) * (room / 0.565))),
            "avatar_center": (round(x * CANVAS[0]), round((1 - y) * CANVAS[1])),
            "avatar_size": round(_input(text, "AvatarCircle", "Width", 0.5) * CANVAS[0]) + 5}


def marker(skeleton):
    """The line carried in the writing request, or "" when the opening cites no comment."""
    ids = list(dict.fromkeys(str(quote) for row in skeleton.get("opening") or [] for quote in row.get("quotes") or []))
    return MARKER + "、".join(ids) if ids else ""


def review_ids(rounds):
    """Comment ids named by a task's writing requests, in order."""
    found = []
    for turn in rounds or []:
        for line in str(turn.get("instruction") or "").splitlines():
            if line.strip().startswith(MARKER):
                found += [value.strip() for value in line.strip()[len(MARKER):].split("、") if value.strip()]
    return list(dict.fromkeys(found))


def username(review):
    """The public handle or nickname, as the platform shows it (a shop's masked M**o stays masked)."""
    value = _clean(review.get("author_handle")).lstrip("@") or _clean(review.get("author_name"))
    return f"@{value}" if value and " " not in value else value


def _clean(text):
    return re.sub(r"\s+", " ", EMOJI.sub("", str(text or ""))).strip()


def comment_lines(text, width=LINE_WIDTH):
    """The comment broken for the card. Too long: whole sentences if they nearly fill it, else as much as fits, cut at a word."""
    clean = _clean(text)
    wrap = lambda value: textwrap.wrap(value, width)
    lines = wrap(clean)
    if len(lines) <= MAX_LINES:
        return lines
    kept = ""
    for sentence in re.split(r"(?<=[.!?])\s+", clean):
        candidate = f"{kept} {sentence}".strip()
        if len(wrap(candidate + "...")) > MAX_LINES:
            break
        kept = candidate
    if kept and len(wrap(kept + "...")) >= MAX_LINES - 1:
        return wrap(kept.rstrip(".") + "...")
    head, last = lines[:MAX_LINES], lines[MAX_LINES - 1]
    head[-1] = (last if len(last) + 3 <= width else last[:width - 3].rsplit(" ", 1)[0]).rstrip(" ,;:.") + "..."
    return head


def drawable(row):
    """Whether the template font can draw the name and the comment; other scripts come out as empty boxes."""
    return all(any(low <= ord(char) <= high for low, high in DRAWABLE) for char in username(row) + _clean(row.get("text")))


def _usable(rows):
    """The comments that can fill the card, ones with a saved avatar first."""
    fit = [row for row in rows if row and username(row) and comment_lines(row.get("text")) and drawable(row)]
    return sorted(fit, key=lambda row: not row.get("avatar"))


def pick(rows):
    return next(iter(_usable(rows)), None)


def attached_ids(context):
    """Comments given to the writing round besides the opening's: the material basket, then those linked to the direction, concerns first."""
    basket = [row.get("source_id") for row in context.get("materials") or [] if row.get("type") == "review"]
    linked = sorted(context.get("linked_reviews") or [], key=lambda row: row.get("linked_as") != "方向顾虑")
    return [str(value) for value in (*basket, *(row.get("review_id") for row in linked)) if value]


def options(app, job):
    """The comments the order's script was written with, the one it opens on first."""
    if not getattr(app, "ai", None) or not getattr(app, "reviews", None):
        return []
    # Opening the publish panel must not depend on the AI task or the comment database still being readable.
    try:
        task_id = (app.creation.get_script(job["script_id"]).get("source") or {}).get("task_id")
        if not task_id:
            return []
        task = app.ai.get(task_id)
        opening = review_ids(task.get("rounds"))
        others = [value for value in dict.fromkeys(attached_ids(task.get("context") or {})) if value not in opening]
        return _usable([app.reviews.review(value) for value in opening]) + _usable([app.reviews.review(value) for value in others])
    except Exception:
        return []


def source(app, job):
    return next(iter(options(app, job)), None)


def typed(name, text):
    """A comment entered by hand, shaped like a saved one. No avatar, so the card shows the name's initial."""
    return {"id": None, "author_handle": name, "author_name": "", "text": text, "avatar": None}


def avatar_canvas(source, target, name, metrics=None):
    """A transparent full-frame PNG with the avatar where the template's circle cuts it, or the name's initial on grey."""
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    side, (x, y) = (metrics or {}).get("avatar_size", AVATAR_SIZE), (metrics or {}).get("avatar_center", AVATAR_CENTER)
    size = (side, side)
    try:
        if not source:
            raise ValueError("no avatar")
        with Image.open(source) as picture:
            face = ImageOps.fit(picture.convert("RGBA"), size, Image.Resampling.LANCZOS)
    except (OSError, ValueError):
        face = Image.new("RGBA", size, INITIAL_FILL)
        letter = name.lstrip("@")[:1].upper()
        ImageDraw.Draw(face).text((side / 2, side / 2), letter, fill="white", anchor="mm", font=ImageFont.load_default(round(side * 0.45)))
    canvas = Image.new("RGBA", CANVAS, (0, 0, 0, 0))
    canvas.paste(face, (x - side // 2, y - side // 2))
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target)
    return target
