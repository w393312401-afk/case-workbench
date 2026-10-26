"""Deterministic multi-image composition for one carousel slide.

An AI reads the slide's row of the 逐张表 and the candidate pictures and returns a small layout plan; this module only
validates the plan and pastes the real pixels into a 3:4 (or 9:16) canvas. It never generates, retouches or relabels
anything, so the evidence in each picture stays the real frame.
"""
from __future__ import annotations

import json
import re

from PIL import Image, ImageFilter, ImageOps

CANVAS = {"3:4": (1536, 2048), "9:16": (1152, 2048)}
LAYOUTS = {"single": (1, 1), "stack": (2, 3), "side_by_side": (2, 2), "main_inset": (2, 3), "grid": (3, 4)}
LAYOUT_LABELS = {"single": "单张", "stack": "上下拼", "side_by_side": "左右并排", "main_inset": "主图加小图", "grid": "宫格"}
CORNERS = ("top_left", "top_right", "bottom_left", "bottom_right")
DIVIDER = (255, 255, 255)
BAND = (17, 19, 21)


def _number(value, low, high, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value)) if value == value else default


def extract_plan(text):
    """The last ```json block of the answer, or its last {...} object."""
    if not isinstance(text, str):
        raise ValueError("AI 没有返回合成方案。")
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = blocks[::-1] or [text[text.find("{"):text.rfind("}") + 1]]
    for chunk in candidates:
        try:
            value = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("AI 的合成方案不是有效的 JSON。")


def normalize_plan(raw, files):
    """Check an AI plan against the files it was shown; returns a clean plan or raises ValueError (Chinese message)."""
    if not isinstance(raw, dict):
        raise ValueError("合成方案格式无效。")
    layout = raw.get("layout")
    if layout not in LAYOUTS:
        raise ValueError(f"合成方案的版式「{layout}」不在可用范围内。")
    panels, seen = [], set()
    for entry in raw.get("panels") or []:
        if not isinstance(entry, dict) or entry.get("file") not in files or entry["file"] in seen:
            continue
        seen.add(entry["file"])
        panels.append({"file": entry["file"], "fit": "contain" if entry.get("fit") == "contain" else "cover",
                       "focus_x": round(_number(entry.get("focus_x"), 0, 1, .5), 3),
                       "focus_y": round(_number(entry.get("focus_y"), 0, 1, .5), 3),
                       "zoom": round(_number(entry.get("zoom"), 1, 3, 1), 3),
                       "weight": round(_number(entry.get("weight"), .2, .8, .5), 3),
                       "note": str(entry.get("note") or "")[:200]})
    low, high = LAYOUTS[layout]
    if not low <= len(panels) <= high:
        raise ValueError(f"版式「{LAYOUT_LABELS[layout]}」需要 {low}–{high} 张图，方案给了 {len(panels)} 张有效图。")
    corner = raw.get("inset_corner") if raw.get("inset_corner") in CORNERS else "bottom_right"
    # Photos always fill the canvas: the caption is laid later into empty space the photo itself has, so an older
    # plan's request for a dark caption band is ignored.
    return {"layout": layout, "panels": panels, "inset_corner": corner, "caption_space": "none",
            "reason": str(raw.get("reason") or "")[:600]}


def _fit(image, width, height, panel):
    width, height = max(1, width), max(1, height)
    if panel["fit"] == "contain":
        inner = ImageOps.contain(image, (width, height), Image.LANCZOS)
        backdrop = ImageOps.fit(image, (width, height), Image.LANCZOS).filter(ImageFilter.GaussianBlur(max(4, width // 40)))
        backdrop = Image.blend(backdrop, Image.new("RGB", backdrop.size, BAND), .45)
        backdrop.paste(inner, ((width - inner.width) // 2, (height - inner.height) // 2))
        return backdrop
    source_w, source_h = image.size
    ratio = width / height
    crop_w, crop_h = (source_h * ratio, source_h) if source_w / source_h > ratio else (source_w, source_w / ratio)
    crop_w, crop_h = crop_w / panel["zoom"], crop_h / panel["zoom"]
    left = min(max(panel["focus_x"] * source_w - crop_w / 2, 0), source_w - crop_w)
    top = min(max(panel["focus_y"] * source_h - crop_h / 2, 0), source_h - crop_h)
    return image.resize((width, height), Image.LANCZOS, box=(left, top, left + crop_w, top + crop_h))


def _split(total, weights, gap):
    usable = total - gap * (len(weights) - 1)
    scale = sum(weights)
    sizes = [round(usable * weight / scale) for weight in weights]
    sizes[-1] = usable - sum(sizes[:-1])
    starts, position = [], 0
    for size in sizes:
        starts.append(position)
        position += size + gap
    return list(zip(starts, sizes))


def compose(plan, sources, target, aspect="3:4"):
    """Paste the plan's pictures (sources: plan file name -> local path) into one canvas saved at target (PNG).

    Returns a receipt with every panel's rectangle so the result can be audited."""
    width, height = CANVAS.get(aspect, CANVAS["3:4"])
    canvas = Image.new("RGB", (width, height), BAND)
    gap = max(6, round(width * .012))
    top, area_h = 0, height
    images = []
    for panel in plan["panels"]:
        with Image.open(sources[panel["file"]]) as opened:
            images.append(ImageOps.exif_transpose(opened).convert("RGB"))
    rects, layout, panels = [], plan["layout"], plan["panels"]
    if layout == "single":
        rects = [(0, top, width, area_h)]
    elif layout == "stack":
        rects = [(0, top + start, width, size) for start, size in _split(area_h, [p["weight"] for p in panels], gap)]
    elif layout == "side_by_side":
        rects = [(start, top, size, area_h) for start, size in _split(width, [p["weight"] for p in panels], gap)]
    elif layout == "grid":
        rows = _split(area_h, [1, 1], gap)
        if len(panels) == 3:
            rects = [(0, top + rows[0][0], width, rows[0][1])]
            rects += [(start, top + rows[1][0], size, rows[1][1]) for start, size in _split(width, [1, 1], gap)]
        else:
            columns = _split(width, [1, 1], gap)
            rects = [(x, top + y, w, h) for y, h in rows for x, w in columns]
    if layout != "main_inset":
        for image, panel, (x, y, w, h) in zip(images, panels, rects):
            canvas.paste(_fit(image, w, h, panel), (x, y))
    else:
        rects.append((0, top, width, area_h))
        canvas.paste(_fit(images[0], width, area_h, panels[0]), (0, top))
        margin, border = round(width * .04), max(4, round(width * .008))
        first = plan["inset_corner"]
        mirror = {"top_left": "top_right", "top_right": "top_left", "bottom_left": "bottom_right", "bottom_right": "bottom_left"}
        for image, panel, corner in zip(images[1:], panels[1:], (first, mirror[first])):
            inset_w = round(width * .38)
            inset_h = min(round(area_h * .42), round(inset_w * image.height / image.width)) if panel["fit"] == "contain" else round(inset_w * 4 / 3)
            inset_h = min(inset_h, round(area_h * .42))
            x = margin if corner.endswith("left") else width - margin - inset_w
            y = top + margin if corner.startswith("top") else top + area_h - margin - inset_h
            canvas.paste(Image.new("RGB", (inset_w + 2 * border, inset_h + 2 * border), DIVIDER), (x - border, y - border))
            canvas.paste(_fit(image, inset_w, inset_h, panel), (x, y))
            rects.append((x, y, inset_w, inset_h))
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "PNG")
    return {"size": [width, height], "aspect": aspect, "layout": layout,
            "panels": [{"file": panel["file"], "rect": list(rect)} for panel, rect in zip(panels, rects)]}
