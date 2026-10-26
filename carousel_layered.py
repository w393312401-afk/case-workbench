"""Exact English captions on the chosen base, nothing else.

The layout (panels, crop and any caption band) was settled when the base was chosen. This module only adds the
lettering: into the base's caption band when it has one, otherwise into the empty region a vision check picked on the
photo (table, wall, sky; never the product, hands or an inset). Without such a region it falls back to the calmest
stretch near the top or bottom edge. The photo is never redrawn, padded or cut out. A base of another aspect (picked by
hand before bases were composed) is cropped to the requested aspect around its centre.
"""
from __future__ import annotations

from itertools import combinations
from pathlib import Path

import numpy as np
from PIL import Image

from carousel_cutout import layout_panels
from carousel_layout import fit_caption, paint_caption

ASPECTS = {"3:4": 3 / 4, "9:16": 9 / 16}
STEP = 8
INK_ALPHA = 32
EDGE_WEIGHT = .5            # captions belong near the top or bottom edge, not across the middle of the photo
MIN_REGION_FONT = .025      # a picked region that only fits smaller type than this share of the width is too small
REWRAP_BELOW = .045         # below this size a narrow region may take the same words on more lines
MAX_LINES = 4
ALIGNS = {"left", "center", "right"}


class NeedsPlacement(Exception):
    """The base has no caption band: someone has to look at the photo and pick the empty region."""


def _crop_box(size, aspect):
    """The largest centred box of the requested aspect inside the frame: (left, top, width, height)."""
    width, height = size
    ratio = ASPECTS[aspect]
    if abs(width / height - ratio) / ratio <= .025:
        return 0, 0, width, height
    if width / height > ratio:
        w = round(height * ratio)
        return (width - w) // 2, 0, w, height
    h = round(width / ratio)
    return 0, (height - h) // 2, width, h


def _detail(image):
    """Per-pixel detail (gradient strength) of the photo, relative to its own average."""
    gray = np.asarray(image.convert("L"), dtype="float32")
    detail = np.zeros_like(gray)
    detail[:, 1:] += np.abs(np.diff(gray, axis=1))
    detail[1:, :] += np.abs(np.diff(gray, axis=0))
    return detail / max(float(detail.mean()), 1e-6)


def _ink(width, fit):
    """The caption alone on a transparent layer as tall as its block, and its ink mask."""
    layer = Image.new("RGBA", (width, fit["used"]), (0, 0, 0, 0))
    paint_caption(layer, fit, 0)
    return layer, np.array(layer.getchannel("A")) > INK_ALPHA


def _choose(ink, detail, area, used, height):
    """The caption's top edge: inside the caption band when there is one, else the calmest edge-near stretch."""
    tops = sorted({*range(0, height - used + 1, STEP), height - used})
    clear = [top for top in tops if not (ink & area[top:top + used]).any()]
    if clear:
        # Off the photo entirely: the middle of the first clear run keeps the caption off the photo's edge.
        run = [clear[0]]
        for top in clear[1:]:
            if top - run[-1] > STEP:
                break
            run.append(top)
        return run[len(run) // 2], True, 0.0
    total = max(1, int(ink.sum()))
    best = None
    for top in tops:
        busy = float(detail[top:top + used][ink].sum()) / total
        score = busy + EDGE_WEIGHT * min(top, height - used - top) / (height / 2)
        if best is None or score < best[0]:
            best = (score, top, busy)
    return best[1], False, best[2]


def plate(base, aspect):
    """The base as it will be captioned (cropped to the aspect when it differs) and its crop box."""
    with Image.open(base) as opened:
        opened.load()
        photo = opened.convert("RGB")
    left, top, width, height = _crop_box(photo.size, aspect)
    return photo.crop((left, top, left + width, top + height)), [left, top, width, height], photo.size


def caption_need(width, height, title_lines, body="", *, style=None):
    """The block the caption needs at its normal size, as a share of the frame (width, height)."""
    fit = fit_caption(width, height, title_lines, body, style=style)
    lines = max((box[2] - box[0] for box in fit["metrics"]), default=0) + 2 * fit["margin"]
    return round(lines / width, 3), round(fit["used"] / height, 3)


def _wraps(title_lines):
    """The given lines, then each line split further at its own word gaps (never joined with the next), most even first."""
    given = [line.split() for line in title_lines if line.split()]
    yield list(title_lines)
    # Word gaps inside each given line: (line index, gap index); the given line breaks always stay.
    gaps = [(row, cut) for row, words in enumerate(given) for cut in range(1, len(words))]
    for extra in range(1, MAX_LINES - len(given) + 1):
        best = None
        for chosen in combinations(gaps, extra):
            lines = []
            for row, words in enumerate(given):
                cuts = [cut for line, cut in chosen if line == row]
                lines += [" ".join(words[a:b]) for a, b in zip((0, *cuts), (*cuts, len(words)))]
            longest = max(len(line) for line in lines)
            if best is None or longest < best[0]:
                best = (longest, lines)
        if best:
            yield best[1]


def _fit_region(rw, rh, width, title_lines, body, style):
    """The largest caption for the region, re-breaking lines (never words) when the given ones would be too small."""
    best = None
    for lines in _wraps(title_lines):
        try:
            fit = fit_caption(rw, rh, lines, body, style=style, limit=1.0, scale=width, margin=max(4, round(width * .015)))
        except ValueError:
            continue
        if best is None or fit["font_size"] > best[1]["font_size"]:
            best = (lines, fit)
        if best[1]["font_size"] >= REWRAP_BELOW * width:
            break
    return best


def _region(box, width, height):
    x0, y0, x1, y1 = (min(1000, max(0, float(value))) for value in box)
    x0, x1 = sorted((round(x0 * width / 1000), round(x1 * width / 1000)))
    y0, y1 = sorted((round(y0 * height / 1000), round(y1 * height / 1000)))
    return x0, y0, x1 - x0, y1 - y0


def render_caption(base, target, title_lines, body="", *, style=None, aspect="3:4", layout=None, region=None):
    """Write the slide and return its receipt. Every pixel outside the caption's ink box is the (cropped) base's own.

    `region` is {"box": [x0, y0, x1, y1] in 0-1000 of the captioned plate, "align": left/center/right} from a vision
    check, or "edge" for the calmest top or bottom stretch. Without a caption band and with no region, NeedsPlacement."""
    image, crop, size = plate(base, aspect)
    left, top, width, height = crop
    photo_area = np.zeros((size[1], size[0]), dtype=bool)
    for panel in layout_panels(layout, size):
        x, y, w, h = panel["rect"]
        photo_area[y:y + h, x:x + w] = True
    area = photo_area[top:top + height, left:left + width]
    fit = fit_caption(width, height, title_lines, body, style=style)
    layer, ink = _ink(width, fit)
    placement, at, x, align, note, busy = "band", 0, 0, "left", "", 0.0
    if fit["rows"]:
        tops = sorted({*range(0, height - fit["used"] + 1, STEP), height - fit["used"]})
        clear = [value for value in tops if not (ink & area[value:value + fit["used"]]).any()]
        if clear:
            at, _, _ = _choose(ink, None, area, fit["used"], height)
        elif region is None:
            raise NeedsPlacement()
        else:
            placement = "edge"
            if isinstance(region, dict):
                rx, ry, rw, rh = _region(region.get("box") or [0, 0, 0, 0], width, height)
                align = region.get("align") if region.get("align") in ALIGNS else "left"
                found = _fit_region(rw, rh, width, title_lines, body, style) if rw > 0 and rh > 0 else None
                boxed = found[1] if found else None
                if boxed and boxed["font_size"] >= MIN_REGION_FONT * width:
                    fit, placement, at, x = boxed, "region", ry + (rh - boxed["used"]) // 2, rx
                    layer = Image.new("RGBA", (rw, boxed["used"]), (0, 0, 0, 0))
                    paint_caption(layer, boxed, 0, align=align)
                else:
                    note = "选出的空白区域太小，改放画面边缘细节最少处。"
            if placement == "edge":
                at, _, busy = _choose(ink, _detail(image), area, fit["used"], height)
    out = image.convert("RGBA")
    out.alpha_composite(layer, (x, at))
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    out.convert("RGB").save(target, format="PNG")
    return {"method": "caption_on_base", "width": width, "height": height, "aspect": aspect,
            "caption_style": fit["preset"], "caption_placement": placement, "caption_align": align,
            "caption_region": region.get("box") if isinstance(region, dict) else None,
            "caption_region_reason": str(region.get("reason", ""))[:300] if isinstance(region, dict) else "",
            "caption_note": note, "caption_bounds": [x, at, x + layer.width, at + fit["used"]],
            "caption_detail": round(busy, 3), "crop_box": crop, "cropped": (width, height) != size,
            "title_font": fit["title_font"].getname()[0], "title_font_size": fit["font_size"],
            "title_lines": list(title_lines), "body": body, "text": [row[0] for row in fit["rows"]],
            "preserves_full_frame": (width, height) == size}
