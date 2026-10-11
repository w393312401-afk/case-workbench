"""Reusable deterministic caption export; never invents a background or cuts a subject."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CAPTION_PRESETS = {
    "plain": {"title_color": (255, 255, 255, 255), "body_color": (255, 255, 255, 255),
              "panel_color": (17, 19, 21, 215), "title_font": "arial_bold", "effects": False},
    "rugged": {"title_color": (244, 237, 216, 255), "body_color": (244, 237, 216, 255),
               "panel_color": (20, 24, 27, 235), "title_font": "impact", "effects": True},
}


def validate_mask(path, size):
    """A future local repair must use an explicit, correctly sized grayscale mask."""
    with Image.open(path) as mask:
        mask.load()
        if mask.mode != "L" or mask.size != tuple(size) or len(mask.getbands()) != 1:
            raise ValueError("蒙版必须是与成图等大的单通道灰度图。")
        if len(mask.tobytes()) != size[0] * size[1]:
            raise ValueError("蒙版的字节数与图片尺寸不一致。")
        return mask.copy()


def _font(size, bold=False, *, impact=False):
    choices = ([Path("C:/Windows/Fonts/impact.ttf")] if impact else []) + [
               Path("C:/Windows/Fonts") / ("arialbd.ttf" if bold else "arial.ttf"),
               Path("/usr/share/fonts/truetype/dejavu") / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")]
    for path in choices:
        if path.is_file():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def _effects(font_size, enabled):
    if not enabled:
        return {"stroke": 0, "orange": (0, 0), "depth": (0, 0)}
    return {"stroke": max(1, round(font_size * .045)),
            "orange": (max(1, round(font_size * .028)), max(1, round(font_size * .028))),
            "depth": (0, max(2, round(font_size * .070)))}


def _bounds(draw, text, font, effects):
    box = draw.textbbox((0, 0), text, font=font, stroke_width=effects["stroke"])
    return (box[0], box[1], box[2] + max(effects["orange"][0], effects["depth"][0]),
            box[3] + max(effects["orange"][1], effects["depth"][1]))


def export_caption(source, target, title_lines, body="", *, area="top", style=None):
    """Preserve the full frame and lay out exact text in an explicitly selected band.

    Callers must choose a band that does not cover product or evidence. This module
    does not remove burned-in captions, segment hands, crop, or replace any product.
    """
    if area not in {"top", "bottom"}:
        raise ValueError("请选择顶部或底部留白区。")
    preset_name = "plain" if style is None else style
    if not isinstance(preset_name, str) or preset_name not in CAPTION_PRESETS:
        raise ValueError("请选择普通文字或粗犷花字排版。")
    preset = CAPTION_PRESETS[preset_name]
    if not isinstance(title_lines, list) or not all(isinstance(line, str) for line in title_lines):
        raise ValueError("标题行格式无效。")
    if not isinstance(body, str):
        raise ValueError("正文格式无效。")
    with Image.open(source) as opened:
        opened.load()
        image = opened.convert("RGB")
    width, height = image.size
    margin = max(8, round(width * .045))
    limit = max(1, round(height * .30))
    lines = [line for line in title_lines if line]
    font_size = max(10, round(width * .063))
    body_lines = []
    draw = ImageDraw.Draw(image)
    while font_size >= 8:
        title_font = _font(font_size, True, impact=preset["title_font"] == "impact")
        body_font = _font(max(8, round(font_size * .53)))
        title_effects, body_effects = _effects(font_size, preset["effects"]), _effects(font_size, False)
        body_lines, pending = [], ""
        for word in body.split():
            candidate = (pending + " " + word).strip()
            if pending and draw.textbbox((0, 0), candidate, font=body_font)[2] > width - 2 * margin:
                body_lines.append(pending)
                pending = word
            else:
                pending = candidate
        if pending:
            body_lines.append(pending)
        rows = [(line, title_font, title_effects, preset["title_color"]) for line in lines] + [
                (line, body_font, body_effects, preset["body_color"]) for line in body_lines]
        metrics = [_bounds(draw, line, font, effects) for line, font, effects, _ in rows]
        gap = max(3, round(font_size * .18))
        used = sum(box[3] - box[1] + gap for box in metrics) + 2 * margin
        if used <= limit and all(box[2] - box[0] <= width - 2 * margin for box in metrics):
            break
        font_size -= 1
    else:
        raise ValueError("文字放不进留白区，请调整断行或选择留白更多的图片。")
    if not rows:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="PNG")
        return {"width": width, "height": height, "caption_area": area, "caption_style": preset_name,
                "title_lines": list(title_lines), "body": body, "text": [], "preserves_full_frame": True}
    top = 0 if area == "top" else height - used
    panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(panel).rectangle((0, top, width - 1, top + used - 1), fill=preset["panel_color"])
    image = Image.alpha_composite(image.convert("RGBA"), panel)
    draw = ImageDraw.Draw(image)
    y = top + margin
    for (line, font, effects, color), box in zip(rows, metrics):
        x, baseline = margin - box[0], y - box[1]
        stroke = effects["stroke"]
        if stroke:
            # Controlled shallow depth and a small warm rim; the wording itself
            # remains exact and the body uses a clean, undistressed font.
            dx, dy = effects["depth"]
            draw.text((x + dx, baseline + dy), line, font=font, fill=(47, 53, 57, 255),
                      stroke_width=stroke, stroke_fill=(5, 8, 10, 255))
            dx, dy = effects["orange"]
            draw.text((x + dx, baseline + dy), line, font=font, fill=(182, 91, 42, 255),
                      stroke_width=stroke, stroke_fill=(182, 91, 42, 255))
        draw.text((x, baseline), line, font=font, fill=color,
                  stroke_width=stroke, stroke_fill=(5, 8, 10, 255))
        y += box[3] - box[1] + gap
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(target, format="PNG")
    return {"width": width, "height": height, "caption_area": area, "caption_style": preset_name,
            "caption_bounds": [0, top, width, top + used], "title_font": title_font.getname()[0],
            "title_lines": list(title_lines), "body": body, "text": [line for line, _, _, _ in rows],
            "preserves_full_frame": True}
