"""Reusable deterministic caption export; never invents a background or cuts a subject."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CAPTION_PRESETS = {
    "plain": {"title_color": (255, 255, 255, 255), "body_color": (255, 255, 255, 255),
              "panel_color": (17, 19, 21, 215), "title_font": "arial_bold", "effects": False},
    "rugged": {"title_color": (244, 237, 216, 255), "body_color": (244, 237, 216, 255),
               "panel_color": None, "title_font": "impact", "effects": True},
    "yellow_impact": {"label": "黄黑冲击", "title_color": (255, 249, 229, 255),
                      "emphasis_color": (255, 224, 68, 255), "body_color": (255, 249, 229, 255),
                      "panel_color": None, "title_font": "impact", "effects": True, "font_scale": .105,
                      "stroke_ratio": .055, "outer_ratio": .012, "outer_color": (255, 249, 229, 255),
                      "depth_color": (5, 8, 10, 255), "depth_ratio": (.030, .045)},
    "orange_depth": {"label": "橙白立体", "title_color": (255, 252, 246, 255),
                     "emphasis_color": (255, 252, 246, 255), "body_color": (255, 252, 246, 255),
                     "panel_color": None, "title_font": "impact", "effects": True, "font_scale": .105,
                     "stroke_ratio": .050, "outer_ratio": 0, "outer_color": (5, 8, 10, 255),
                     "depth_color": (255, 117, 28, 255), "depth_ratio": (.095, .105)},
    "red_comic": {"label": "红白漫画", "title_color": (255, 249, 229, 255),
                  "emphasis_color": (255, 69, 61, 255), "body_color": (255, 249, 229, 255),
                  "panel_color": None, "title_font": "impact", "effects": True, "font_scale": .105,
                  "stroke_ratio": .055, "outer_ratio": .018, "outer_color": (255, 255, 255, 255),
                  "depth_color": (5, 8, 10, 255), "depth_ratio": (.028, .045), "shear": .20},
    "lime_pop": {"label": "荧光青绿", "title_color": (255, 255, 255, 255),
                 "emphasis_color": (216, 255, 54, 255), "body_color": (255, 255, 255, 255),
                 "panel_color": None, "title_font": "impact", "effects": True, "font_scale": .105,
                 "stroke_ratio": .050, "outer_ratio": 0, "outer_color": (5, 8, 10, 255),
                 "depth_color": (38, 223, 220, 255), "depth_ratio": (.035, .045)},
}
STROKE_COLOR, RIM_COLOR, DEPTH_COLOR = (5, 8, 10, 255), (182, 91, 42, 255), (47, 53, 57, 255)


def _hex(color):
    return "#%02X%02X%02X" % tuple(color[:3])


def caption_style_prompt(name):
    """The look export_caption draws, in words, so generated slides match laid-out ones."""
    preset = CAPTION_PRESETS[name]
    if not preset["effects"]:
        panel = (f"需要文字底板时用 {_hex(preset['panel_color'])}（约 {round(preset['panel_color'][3] / 255 * 100)}% 不透明）"
                 "的整条纯色底板，不用渐变、纹理或圆角卡片。")
        return (f"文字样式（普通文字）：标题用粗体无衬线英文字，标题与正文都是 {_hex(preset['title_color'])}，"
                f"不加描边或特效。{panel}整组字体、配色和层级一致。")
    if "label" in preset:
        face = (f"首行字面 {_hex(preset['title_color'])}，后续标题行用 {_hex(preset['emphasis_color'])} 强调；"
                f"只有一行时用 {_hex(preset['emphasis_color'])}。")
        dx, dy = preset["depth_ratio"]
        edge = (f"外加约字高 {preset['outer_ratio'] * 100:g}% 的 {_hex(preset['outer_color'])} 细外轮廓；"
                if preset["outer_ratio"] else "")
        slant = "字形向右前倾约 11 度，保持基线水平；" if preset.get("shear") else "字形直立；"
        return (f"文字样式（{preset['label']}）：Impact 类极粗、紧凑的无衬线英文字，{slant}"
                f"在已确认留白内优先使用醒目大字，字号起点约画宽 {preset['font_scale'] * 100:g}%，按锁定分行适配，不能遮挡主体或证据。{face}"
                f"{_hex(STROKE_COLOR)} 粗描边（约字高 {preset['stroke_ratio'] * 100:g}%）；{edge}"
                f"字后叠 {_hex(preset['depth_color'])} 实色立体厚度，向右约字高 {dx * 100:g}%、向下约 {dy * 100:g}%。"
                f"正文用干净常规无衬线字，字面 {_hex(preset['body_color'])}，不加花字效果。"
                "只画文字及紧贴字形的轮廓和厚度，不加通栏底板、光晕或新增口号；字形清晰完整，不做做旧破损。"
                "英文单词、大小写、标点和既定分行不变；字形以外保持原图。")
    return (f"文字样式（粗犷花字）：标题用 Impact 类粗重、略窄的无衬线英文字，字面 {_hex(preset['title_color'])}，"
            f"{_hex(STROKE_COLOR)} 细描边（约字高 4.5%）；字后叠一层向右下错开约字高 3% 的 {_hex(RIM_COLOR)} 暖橙描影，"
            f"再叠一层向下约字高 7% 的 {_hex(DEPTH_COLOR)} 深色立体厚度。正文用干净的常规无衬线字，同为 "
            f"{_hex(preset['body_color'])}，不加花字效果。只叠文字及字形描边、浅层阴影和暖色细边，不画文字底板或整条色带，"
            "字形效果以外保持原图。整组字体、配色、层级和效果一致；字形清晰完整，不做做旧破损。")


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


def _effects(font_size, enabled, *, preset=None):
    if not enabled:
        return {"stroke": 0, "orange": (0, 0), "depth": (0, 0)}
    if preset and "label" in preset:
        return {"styled": True, "stroke": max(1, round(font_size * preset["stroke_ratio"])),
                "outer": max(1, round(font_size * preset["outer_ratio"])) if preset["outer_ratio"] else 0,
                "outer_color": preset["outer_color"], "depth_color": preset["depth_color"],
                "depth": tuple(max(1, round(font_size * part)) for part in preset["depth_ratio"]),
                "shear": preset.get("shear", 0)}
    return {"stroke": max(1, round(font_size * .045)),
            "orange": (max(1, round(font_size * .028)), max(1, round(font_size * .028))),
            "depth": (0, max(2, round(font_size * .070)))}


def _bounds(draw, text, font, effects):
    if effects.get("styled"):
        box, width, height, skew = _styled_metrics(draw, text, font, effects)
        return 0, 0, width + skew, height
    box = draw.textbbox((0, 0), text, font=font, stroke_width=effects["stroke"])
    return (box[0], box[1], box[2] + max(effects["orange"][0], effects["depth"][0]),
            box[3] + max(effects["orange"][1], effects["depth"][1]))


def _styled_metrics(draw, text, font, effects):
    stroke = effects["stroke"] + effects["outer"]
    box = draw.textbbox((0, 0), text, font=font, stroke_width=stroke)
    width = max(1, box[2] - box[0] + effects["depth"][0])
    height = max(1, box[3] - box[1] + effects["depth"][1])
    return box, width, height, math.ceil(height * effects["shear"])


def _styled_line(text, font, effects, color):
    """A tight independent lettering layer, including the complete shadow and optional forward slant."""
    measure = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box, width, height, skew = _styled_metrics(measure, text, font, effects)
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw, stroke = ImageDraw.Draw(layer), effects["stroke"]
    x, y = -box[0], -box[1]
    dx, dy = effects["depth"]
    # Short, solid side faces keep the colored depth readable even at thumbnail size.
    for step in range(max(dx, dy), 0, -1):
        fraction = step / max(dx, dy)
        draw.text((x + round(dx * fraction), y + round(dy * fraction)), text, font=font,
                  fill=effects["depth_color"], stroke_width=stroke, stroke_fill=effects["depth_color"])
    if effects["outer"]:
        draw.text((x, y), text, font=font, fill=effects["outer_color"],
                  stroke_width=stroke + effects["outer"], stroke_fill=effects["outer_color"])
    draw.text((x, y), text, font=font, fill=color, stroke_width=stroke, stroke_fill=STROKE_COLOR)
    if skew:
        layer = layer.transform((width + skew, height), Image.Transform.AFFINE,
                                (1, effects["shear"], -skew, 0, 1, 0), resample=Image.Resampling.BICUBIC)
    return layer


def fit_caption(width, height, title_lines, body="", *, style=None, limit=.30, scale=None, margin=None):
    """The largest caption that fits the frame width and `limit` of its height: rows, sizes and the block height.

    `scale` is the frame width that sets the starting type size, so a caption fitted into a smaller region of the frame
    starts at the frame's own size; `margin` overrides the inner margin."""
    preset_name = "plain" if style is None else style
    if not isinstance(preset_name, str) or preset_name not in CAPTION_PRESETS:
        raise ValueError("请选择已有的文字或花字样式。")
    preset = CAPTION_PRESETS[preset_name]
    if not isinstance(title_lines, list) or not all(isinstance(line, str) for line in title_lines):
        raise ValueError("标题行格式无效。")
    if not isinstance(body, str):
        raise ValueError("正文格式无效。")
    margin = max(8, round(width * .045)) if margin is None else margin
    limit = max(1, round(height * limit))
    lines = [line for line in title_lines if line]
    font_size = max(10, round((scale or width) * preset.get("font_scale", .063)))
    draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    while font_size >= 8:
        title_font = _font(font_size, True, impact=preset["title_font"] == "impact")
        body_font = _font(max(8, round(font_size * .53)))
        title_effects, body_effects = _effects(font_size, preset["effects"], preset=preset), _effects(font_size, False)
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
        rows = [(line, title_font, title_effects,
                 preset.get("emphasis_color", preset["title_color"]) if index or len(lines) == 1 else preset["title_color"])
                for index, line in enumerate(lines)] + [
                (line, body_font, body_effects, preset["body_color"]) for line in body_lines]
        metrics = [_bounds(draw, line, font, effects) for line, font, effects, _ in rows]
        gap = max(3, round(font_size * .18))
        used = sum(box[3] - box[1] + gap for box in metrics) + 2 * margin
        if used <= limit and all(box[2] - box[0] <= width - 2 * margin for box in metrics):
            break
        font_size -= 1
    else:
        raise ValueError("文字放不进留白区，请调整断行或选择留白更多的图片。")
    return {"preset": preset_name, "rows": rows, "metrics": metrics, "gap": gap, "used": used, "margin": margin,
            "font_size": font_size, "title_font": title_font, "width": width}


def paint_caption(image, fit, top, *, left=0, align="left"):
    """Draw a fitted caption onto an RGBA image with its block at (`left`, `top`); returns the line boxes drawn."""
    preset = CAPTION_PRESETS[fit["preset"]]
    draw = ImageDraw.Draw(image)
    y = top + fit["margin"]
    boxes = []
    for (line, font, effects, color), box in zip(fit["rows"], fit["metrics"]):
        room = fit["width"] - 2 * fit["margin"] - (box[2] - box[0])
        start = left + fit["margin"] + {"left": 0, "center": room // 2, "right": room}[align]
        x, baseline = start - box[0], y - box[1]
        stroke = effects["stroke"]
        if effects.get("styled"):
            image.alpha_composite(_styled_line(line, font, effects, color), (start, y))
            boxes.append((start, y, start + box[2] - box[0], y + box[3] - box[1]))
            y += box[3] - box[1] + fit["gap"]
            continue
        if stroke:
            # Controlled shallow depth and a small warm rim; the wording itself
            # remains exact and the body uses a clean, undistressed font.
            dx, dy = effects["depth"]
            draw.text((x + dx, baseline + dy), line, font=font, fill=DEPTH_COLOR,
                      stroke_width=stroke, stroke_fill=STROKE_COLOR)
            dx, dy = effects["orange"]
            draw.text((x + dx, baseline + dy), line, font=font, fill=RIM_COLOR,
                      stroke_width=stroke, stroke_fill=RIM_COLOR)
        draw.text((x, baseline), line, font=font, fill=color,
                  stroke_width=stroke, stroke_fill=STROKE_COLOR)
        boxes.append((start, y, start + box[2] - box[0], y + box[3] - box[1]))
        y += box[3] - box[1] + fit["gap"]
    return boxes


def export_caption(source, target, title_lines, body="", *, area="top", style=None):
    """Preserve the full frame and lay out exact text in an explicitly selected band.

    Callers must choose a band that does not cover product or evidence. This module
    does not remove burned-in captions, segment hands, crop, or replace any product.
    """
    if area not in {"top", "bottom"}:
        raise ValueError("请选择顶部或底部留白区。")
    with Image.open(source) as opened:
        opened.load()
        image = opened.convert("RGB")
    width, height = image.size
    fit = fit_caption(width, height, title_lines, body, style=style)
    preset_name, preset, rows, used = fit["preset"], CAPTION_PRESETS[fit["preset"]], fit["rows"], fit["used"]
    if not rows:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        image.save(target, format="PNG")
        return {"width": width, "height": height, "caption_area": area, "caption_style": preset_name,
                "title_lines": list(title_lines), "body": body, "text": [], "preserves_full_frame": True}
    top = 0 if area == "top" else height - used
    image = image.convert("RGBA")
    if preset["panel_color"] is not None:
        panel = Image.new("RGBA", image.size, (0, 0, 0, 0))
        ImageDraw.Draw(panel).rectangle((0, top, width - 1, top + used - 1), fill=preset["panel_color"])
        image = Image.alpha_composite(image, panel)
    paint_caption(image, fit, top)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(target, format="PNG")
    return {"width": width, "height": height, "caption_area": area, "caption_style": preset_name,
            "caption_bounds": [0, top, width, top + used], "title_font": fit["title_font"].getname()[0],
            "title_font_size": fit["font_size"], "background_panel": preset["panel_color"] is not None,
            "title_lines": list(title_lines), "body": body, "text": [line for line, _, _, _ in rows],
            "preserves_full_frame": True}
