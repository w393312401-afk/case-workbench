"""Photo carousel layout: one full-bleed photo per slide (or a 2/4-cell collage) with title, fact lines, page number and
(last slide) a call to action.

Pure rendering, no AI: the same slide spec always gives the same picture. Three type styles:
  label  – white rounded stickers, black heavy type, orange key words
  flower – default flower text straight on the photo (ivory/yellow face, black stroke, warm edge, shadow)
  bar    – tight black bars, white condensed type, orange key words
Key words are marked with *asterisks* in title and call-to-action lines.
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1440
FONTS = Path("C:/Windows/Fonts")
IMPACT = FONTS / "impact.ttf"
BLACK = FONTS / "ariblk.ttf"
FACT = FONTS / "segoeuib.ttf"
ORANGE = (242, 101, 34)
YELLOW = (255, 210, 31)
IVORY = (255, 246, 224)
INK = (20, 20, 20)
WARM = (232, 134, 42)
WHITE = (255, 255, 255)
STYLES = {"label": "白色标签贴", "flower": "满版花字", "bar": "黑色字条"}
POSITIONS = {"split": "标题在上、第二行在下", "top": "全部在上", "middle": "全部在中间", "bottom": "全部在下"}
# Cell rectangles as fractions of the canvas. Cells touch; only a thin seam line separates them (no margins or bands).
LAYOUTS = {
    "single": ("单图", [(0, 0, 1, 1)]),
    "lr": ("左右", [(0, 0, .5, 1), (.5, 0, 1, 1)]),
    "tb": ("上下", [(0, 0, 1, .5), (0, .5, 1, 1)]),
    "grid": ("四格", [(0, 0, .5, .5), (.5, 0, 1, .5), (0, .5, .5, 1), (.5, .5, 1, 1)]),
}
SEAM = 4
SIZES = {"l": ("大", 1.0), "m": ("中", 0.84), "s": ("小", 0.7)}
MAX_W = W - 110
TOP, BOTTOM = 40, H - 100
_fonts = {}
_probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))


class RenderError(ValueError):
    pass


def font(path, px):
    key = (str(path), int(px))
    if key not in _fonts:
        try:
            _fonts[key] = ImageFont.truetype(str(path), int(px))
        except OSError as exc:
            raise RenderError(f"缺少字体文件 {Path(path).name}，无法排版。") from exc
    return _fonts[key]


def text_w(s, f):
    box = _probe.textbbox((0, 0), s, font=f)
    return box[2] - box[0]


def words(line):
    """'STOP *ONE BY ONE.*' -> [('STOP', False), ('ONE', True), ('BY', True), ('ONE.', True)]"""
    out, hot = [], False
    for part in re.split(r"(\*)", str(line or "")):
        if part == "*":
            hot = not hot
            continue
        out.extend((word, hot) for word in part.split())
    return out


def plain(line):
    return str(line or "").replace("*", "").strip()


def crop_fit(image, crop_y=0.5, crop_x=0.5, w=W, h=H):
    """Scale to cover a w×h window (the whole 3:4 canvas by default), then place the crop window: crop_y 0 = keep the top,
    1 = keep the bottom; crop_x 0 = keep the left, 1 = keep the right. Same rule as CSS object-fit: cover with object-position in %."""
    scale = max(w / image.width, h / image.height)
    image = image.resize((max(w, round(image.width * scale)), max(h, round(image.height * scale))), Image.LANCZOS)
    x = int((image.width - w) * min(1.0, max(0.0, float(crop_x))))
    y = int((image.height - h) * min(1.0, max(0.0, float(crop_y))))
    return image.crop((x, y, x + w, y + h))


def layout_of(slide):
    return slide.get("layout") if slide.get("layout") in LAYOUTS else "single"


def cell_boxes(layout):
    return [(round(a * W), round(b * H), round(c * W), round(d * H)) for a, b, c, d in LAYOUTS[layout][1]]


def compose(slide, images):
    """The picture under the text: one photo, or one photo per cell. Cell 1 uses the slide's own crop_x/crop_y,
    the others use slide['cells'][i - 1]."""
    layout = layout_of(slide)
    boxes = cell_boxes(layout)
    if len(images) < len(boxes):
        raise RenderError("拼图的格子缺图，请给每一格选图。")
    crops = [slide] + list(slide.get("cells") or [])
    canvas = Image.new("RGB", (W, H), WHITE)
    for index, (box, image) in enumerate(zip(boxes, images)):
        crop = crops[index] if index < len(crops) else {}
        x0, y0, x1, y1 = box
        try:
            base = image if isinstance(image, Image.Image) else Image.open(image)
            canvas.paste(crop_fit(base.convert("RGB"), crop.get("crop_y", 0.5), crop.get("crop_x", 0.5), x1 - x0, y1 - y0), (x0, y0))
        except (OSError, ValueError) as exc:
            raise RenderError("这张底图读不出来，请换一张。") from exc
    if len(boxes) > 1:
        d = ImageDraw.Draw(canvas)
        for x0, y0, x1, y1 in boxes:
            if x0:
                d.rectangle((x0 - SEAM // 2, y0, x0 + SEAM // 2 - 1, y1), fill=WHITE)
            if y0:
                d.rectangle((x0, y0 - SEAM // 2, x1, y0 + SEAM // 2 - 1), fill=WHITE)
    return canvas


# ---------- boxed styles (label, bar) ----------
def _row_width(items, px, path, space):
    return sum(text_w(t, font(path, px)) for t, _ in items) + space * (len(items) - 1)


def boxed_row(d, y, items, px, path, face, hot, box, pad, r):
    while px > 30 and _row_width(items, px, path, text_w(" ", font(path, px))) > MAX_W - 60:
        px -= 2
    f = font(path, px)
    space = text_w(" ", f)
    total = _row_width(items, px, path, space)
    x = (W - total) // 2
    asc, desc = f.getmetrics()
    bottom = y + asc + desc // 3
    if d:
        d.rounded_rectangle((x - pad[0], y - pad[1], x + total + pad[0], bottom + pad[1]), r, fill=box)
        for text, hl in items:
            d.text((x, y), text, font=f, fill=hot if hl else face)
            x += text_w(text, f) + space
    return bottom + pad[1]


def balance(lines, too_wide):
    """A spoken second line is one sentence: when it is too wide for one row, break it into two rows of similar width
    (at most two rows in total) instead of shrinking the type until it is unreadable."""
    lines = [plain(line) for line in lines or [] if plain(line)]
    if len(lines) != 1 or not too_wide(lines[0]):
        return lines
    words_ = lines[0].split()
    if len(words_) < 2:
        return lines
    best = min(range(1, len(words_)), key=lambda i: max(len(" ".join(words_[:i])), len(" ".join(words_[i:]))))
    return [" ".join(words_[:best]), " ".join(words_[best:])]


def fact_rows(d, y, lines, px, face, box, pad=(20, 10), r=12, gap=10):
    lines = balance(lines, lambda line: text_w(line, font(FACT, px)) > W - 120)
    for line in lines:
        f = font(FACT, px)
        while text_w(line, f) > W - 120 and f.size > 20:
            f = font(FACT, f.size - 2)
        x = (W - text_w(line, f)) // 2
        top, bottom = y + f.getbbox(line)[1], y + f.getbbox(line)[3]
        if d:
            d.rounded_rectangle((x - pad[0], top - pad[1], x + text_w(line, f) + pad[0], bottom + pad[1]), r, fill=box)
            d.text((x, y), line, font=f, fill=face)
        y = bottom + pad[1] + gap
    return y


def page_box(d, n, total, face, box):
    f = font(FACT, 30)
    t = f"{n}/{total}"
    x, y = W - 36 - text_w(t, f), H - 64
    bb = d.textbbox((x, y), t, font=f)
    d.rounded_rectangle((bb[0] - 12, bb[1] - 7, bb[2] + 12, bb[3] + 7), 10, fill=box)
    d.text((x, y), t, font=f, fill=face)


BOXED = {
    # face, hot, box, title font, title px, radius, pad, cta box
    "label": (INK, ORANGE, (255, 255, 255, 235), BLACK, 96, 16, (26, 14), ORANGE + (255,), WHITE),
    "bar": (WHITE, ORANGE, (16, 16, 16, 230), IMPACT, 124, 6, (24, 10), (16, 16, 16, 230), ORANGE),
}


def boxed_block(d, y, slide, style, k=1.0):
    face, hot, box, path, px, r, pad, cta_box, cta_hot = BOXED[style]
    for line in slide.get("title_lines") or []:
        y = boxed_row(d, y, words(line), int(px * k), path, face, hot, box, pad, r) + 14
    if slide.get("body_lines"):
        y = fact_rows(d, y + 4, slide["body_lines"], int(42 * max(k, .8)), face, box, r=min(r, 12))
    if slide.get("cta"):
        y = boxed_row(d, y + 18, words(slide["cta"]), int(76 * k), path, WHITE, cta_hot, cta_box, pad, r)
    return y


# ---------- flower style ----------
def flower_row(layer, y, items, px, gap=24, x=None):
    while px > 30 and sum(text_w(t, font(IMPACT, int(px * (1.25 if h else 1)))) for t, h in items) + gap * (len(items) - 1) > MAX_W:
        px -= 2
    sized = [(t, h, font(IMPACT, int(px * (1.25 if h else 1)))) for t, h in items]
    total = sum(text_w(t, f) for t, _, f in sized) + gap * (len(sized) - 1)
    x = (W - total) // 2 if x is None else x
    base = y + int(px * 1.25)
    if layer is not None:
        d = ImageDraw.Draw(layer)
        for text, hl, f in sized:
            top = base - f.getmetrics()[0]
            sw = max(6, f.size // 14)
            d.text((x + 8, top + 9), text, font=f, fill=(0, 0, 0, 170), stroke_width=sw, stroke_fill=(0, 0, 0, 170))
            d.text((x, top), text, font=f, fill=WARM, stroke_width=sw + 4, stroke_fill=WARM)
            d.text((x, top), text, font=f, fill=YELLOW if hl else IVORY, stroke_width=sw, stroke_fill=INK)
            x += text_w(text, f) + gap
    return base + 20


def flower_block(layer, y, slide, k=1.0):
    for line in slide.get("title_lines") or []:
        y = flower_row(layer, y, words(line), int(120 * k)) + 10
    px = int(54 * max(k, .8))
    for line in balance(slide.get("body_lines"), lambda line: text_w(line.upper(), font(IMPACT, px)) > MAX_W):
        y = flower_row(layer, y - 14, [(line.upper(), False)], px, gap=0) - 6
    if slide.get("cta"):
        y = flower_row(layer, y + 4, words(slide["cta"]), int(80 * k))
    return y


# ---------- slide ----------
def render(slide, image, style, number, total):
    """slide: title_lines, body_lines, cta (optional), crop_y, crop_x, layout ('single' default, 'lr', 'tb', 'grid') with
    cells (crop_x/crop_y of cells 2…), text_position: 'split' (title top, facts bottom; default), 'top', 'middle' or
    'bottom' (everything together), text_size: 'l' (default), 'm', 's'. image: path or PIL image, or a list with one per cell."""
    if style not in STYLES:
        raise RenderError("排版风格无效。")
    base = compose(slide, list(image) if isinstance(image, (list, tuple)) else [image]).convert("RGBA")
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    position = slide.get("text_position") or "split"
    k = SIZES.get(slide.get("text_size") or "l", SIZES["l"])[1]
    head = {"title_lines": slide.get("title_lines") or []}
    tail = {"body_lines": slide.get("body_lines") or [], "cta": slide.get("cta") or ""}
    groups = [(slide, position)] if position in ("top", "middle", "bottom") else [(head, "top"), (tail, "bottom")]
    if style == "flower":
        for part, where in groups:
            height = flower_block(None, 0, part, k)
            y = TOP if where == "top" else max(TOP, (H - height) // 2) if where == "middle" else max(TOP, BOTTOM - 40 - height)
            flower_block(layer, y, part, k)
        flower_row(layer, H - 92, [(f"{number}/{total}", False)], 34, x=W - 120)
    else:
        for part, where in groups:
            height = boxed_block(None, 0, part, style, k)
            y = TOP + 30 if where == "top" else max(TOP, (H - height) // 2) if where == "middle" else max(TOP, BOTTOM - height)
            boxed_block(d, y, part, style, k)
        face, _, box, *_ = BOXED[style]
        page_box(d, number, total, face, box)
    return Image.alpha_composite(base, layer).convert("RGB")
