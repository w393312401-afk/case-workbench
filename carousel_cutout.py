"""Fine subject masks for the subject lock, made from transparent-background cutouts of the real photo.

Each photo panel of the base is cut out on its own (a whole composite makes the image model reframe it), the
returned alpha is aligned back onto the real pixels, and an inset picture is protected whole with its frame. The
mask only says which real pixels to restore; the final image still takes those pixels from the photo itself and the
existing coverage review and pixel verification still decide whether it may be used.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from codex_bridge import BridgeCancelled, BridgeError
from cockpit_image_bridge import CUTOUT_SIZES

MIN_CORRELATION = 0.3       # ECC score of the model's redraw against the real crop
MAX_RESIDUAL = 30.0         # mean RGB difference inside the kept area after alignment
MAX_SCALE_DRIFT = 0.05
MAX_SHIFT_RATIO = 0.03
# Only pixels the cutout holds mostly opaque are kept: a soft edge that is mostly old background would show as a halo,
# while taking whole pixels off the edge would leave a real blade edge outside the lock.
KEEP_ALPHA = 192
# Small background islands fully ringed by the subject (the gap between fingers and a clamp) are kept with it:
# product seen through them is too small for the cutout to name, and old background there is barely visible.
MAX_HOLE_SHARE = 0.002
INSET_MARGIN = 12           # the composer's inset frame is at most this wide at 1536 px
MAX_PARALLEL = 5
DEFAULT_PARTS = ["the product itself with every attached part", "every accessory, stone, rod and clamp",
                 "any knife or tool", "hands and fingers touching the product"]


def cutout_prompt(parts):
    listed = "; ".join(str(part).strip() for part in parts if str(part).strip()) or "; ".join(DEFAULT_PARTS)
    return ("Remove the background completely and return a transparent PNG. Keep ONLY these physical objects, each one "
            f"complete to its outermost edge: {listed}. Also keep any hands and fingers and anything they hold. "
            "Do not redraw, move, resize, crop, zoom, reframe or extend anything: every edge, printed letter, logo, scratch "
            "and reflection stays exactly where it is, on the same canvas as the input. The table, floor, grass, wall and "
            "everything behind the objects become fully transparent.")


def _size_for(width, height):
    ratio = width / height
    return min(CUTOUT_SIZES, key=lambda size: abs(int(size.split("x")[0]) / int(size.split("x")[1]) - ratio))


def align(model_rgb, real_rgb, model_alpha):
    """The affine warp mapping the model's redraw onto the real crop, and its correlation score.

    Only the kept pixels are compared: a transparent pixel carries no meaningful colour."""
    real = cv2.cvtColor(real_rgb, cv2.COLOR_RGB2GRAY).astype("float32") / 255
    model = cv2.cvtColor(model_rgb, cv2.COLOR_RGB2GRAY).astype("float32") / 255
    warp = np.eye(2, 3, dtype="float32")
    try:
        score, warp = cv2.findTransformECC(real, model, warp, cv2.MOTION_AFFINE,
                                           (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 200, 1e-6),
                                           (model_alpha > 128).astype("uint8"), 5)
    except cv2.error:
        return warp, 0.0
    return warp, float(score)


HOLE_MAX_LUMA = 100         # a hole this dark is product seen through a gap; a bright one is the table, not product
TINY_HOLE_SHARE = 0.0003    # specks this small are filled whatever their colour


def fill_small_holes(keep, rgb=None):
    """Close background islands enclosed by the subject that are too small to be a real view of the background.

    With the photo given, only dark islands (plastic and steel seen through a gap) are closed: a bright island is table
    seen through the product, and keeping its old pixels leaves a patch of the wrong wood in the new background."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats((~keep).astype("uint8"), connectivity=4)
    height, width = keep.shape
    limit = MAX_HOLE_SHARE * width * height
    luma = None if rgb is None else cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    filled = keep.copy()
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area <= limit and x > 0 and y > 0 and x + w < width and y + h < height:
            inside = labels == label
            if luma is None or area <= TINY_HOLE_SHARE * width * height or float(luma[inside].mean()) < HOLE_MAX_LUMA:
                filled[inside] = True
    return filled


def layout_panels(layout, size):
    """Photo panels and insets of a composed base in its own pixels; a plain photo is one panel."""
    width, height = size
    if not isinstance(layout, dict) or list(layout.get("size") or []) != [width, height]:
        return [{"rect": (0, 0, width, height), "inset": False}]
    panels = []
    for index, panel in enumerate(layout.get("panels") or []):
        rect = panel.get("rect") if isinstance(panel, dict) else None
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4 or any(type(v) is not int for v in rect) or
                rect[0] < 0 or rect[1] < 0 or rect[2] < 1 or rect[3] < 1 or rect[0] + rect[2] > width or rect[1] + rect[3] > height):
            return [{"rect": (0, 0, width, height), "inset": False}]
        panels.append({"rect": tuple(rect), "inset": layout.get("layout") == "main_inset" and index > 0})
    return panels or [{"rect": (0, 0, width, height), "inset": False}]


def _grown(rect, size, margin):
    x, y, w, h = rect
    left, top = max(0, x - margin), max(0, y - margin)
    return left, top, min(size[0], x + w + margin) - left, min(size[1], y + h + margin) - top


def cutout_panel(bridge, crop, folder, parts, cancel, progress):
    """One panel's kept area as a boolean array of the crop's size, with the alignment receipt."""
    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "source.png"
    Image.fromarray(crop).save(source)
    height, width = crop.shape[:2]
    size = _size_for(width, height)
    answer = bridge.run(cutout_prompt(parts), cwd=str(folder), images=[str(source)], cancel_event=cancel,
                        on_progress=progress, timeout=900, cutout={"size": size, "quality": "high"})
    if cancel.is_set():
        raise BridgeCancelled("抠图已停止。")
    with Image.open(folder / json.loads(answer)["file"]) as image:
        returned = image.size
        if "A" not in image.getbands():
            raise BridgeError("抠图没有返回透明背景。")
        rgba = np.array(image.convert("RGBA").resize((width, height), Image.LANCZOS))
    warp, score = align(rgba[..., :3], crop, rgba[..., 3])
    flags = cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP
    rgb = cv2.warpAffine(rgba[..., :3], warp, (width, height), flags=flags, borderMode=cv2.BORDER_REPLICATE)
    keep = fill_small_holes(cv2.warpAffine(rgba[..., 3], warp, (width, height), flags=flags, borderValue=0) > KEEP_ALPHA, crop)
    share = float(keep.mean())
    residual = float(np.abs(crop.astype(int) - rgb.astype(int)).mean(axis=2)[keep].mean()) if keep.any() else 255.0
    scale = (abs(float(warp[0, 0]) - 1), abs(float(warp[1, 1]) - 1))
    shift = (abs(float(warp[0, 2])) / width, abs(float(warp[1, 2])) / height)
    receipt = {"requested_size": size, "returned_size": list(returned), "correlation": round(score, 4),
               "residual": round(residual, 2), "kept_share": round(share, 4), "warp": np.round(warp, 4).tolist()}
    (folder / "alignment.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    problems = []
    if score < MIN_CORRELATION:
        problems.append("抠图和原图对不上位置")
    if max(scale) > MAX_SCALE_DRIFT or max(shift) > MAX_SHIFT_RATIO:
        problems.append("抠图被模型缩放或挪动了")
    if residual > MAX_RESIDUAL:
        problems.append("抠图把主体重画了")
    if not .01 <= share <= .95:
        problems.append("抠图保留的范围不合理")
    if problems:
        raise BridgeError("、".join(problems) + "。")
    return keep, receipt


def refine_subject_mask(bridge, base, cache, *, layout, trim_top, parts, cancel, progress):
    """Write the fine mask cache for one base (bound to its SHA-256) and return its metadata record."""
    base = Path(base)
    digest = hashlib.sha256(base.read_bytes()).hexdigest()
    with Image.open(base) as image:
        original = np.array(image.convert("RGB"))
    size = (original.shape[1], original.shape[0])
    panels = layout_panels(layout, size)
    insets = [_grown(panel["rect"], size, INSET_MARGIN) for panel in panels if panel["inset"]]
    work = Path(cache) / "work"
    keep = np.zeros(original.shape[:2], dtype=bool)

    def run(index, panel):
        x, y, w, h = panel["rect"]
        crop = original[y:y + h, x:x + w].copy()
        for ix, iy, iw, ih in insets:
            # The model sees one photograph only: an inset on top of it is greyed out and kept whole afterwards.
            crop[max(0, iy - y):max(0, iy + ih - y), max(0, ix - x):max(0, ix + iw - x)] = (128, 128, 128)
        return cutout_panel(bridge, crop, work / f"panel{index}", parts, cancel,
                            lambda text: progress(f"第 {index + 1} 格：{text}"))

    photos = [(index, panel) for index, panel in enumerate(panels) if not panel["inset"]]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL, len(photos))) as pool:
        results = [pool.submit(run, index, panel) for index, panel in photos]
        outcomes = [future.result() for future in results]
    receipts = []
    for (index, panel), (mask, receipt) in zip(photos, outcomes):
        x, y, w, h = panel["rect"]
        keep[y:y + h, x:x + w] |= mask
        receipts.append({"panel": index, "rect": list(panel["rect"]), **receipt})
    for ix, iy, iw, ih in insets:
        keep[iy:iy + ih, ix:ix + iw] = True
    rectangles = [panel["rect"] for panel in panels if not panel["inset"]] + insets
    windows = []
    for x, y, w, h in rectangles:
        top = max(y, int(trim_top))
        if y + h - top >= 1:
            windows.append({"left": x, "top": top, "width": w, "height": y + h - top})
    if not windows:
        raise BridgeError("底图没有可用的照片区域。")
    rgba = np.zeros((size[1], size[0], 4), dtype="uint8")
    rgba[..., 3] = np.where(keep, 255, 0)
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba, "RGBA").save(cache / "mask.png")
    record = {"base_sha256": digest, "mask_file": "mask.png",
              "mask_sha256": hashlib.sha256((cache / "mask.png").read_bytes()).hexdigest(),
              "photo_rectangles": windows, "photo_frames": [],
              "refinement": {"method": "cockpit_transparent_cutout", "parts": list(parts), "keep_alpha": KEEP_ALPHA,
                             "max_hole_share": MAX_HOLE_SHARE,
                             "insets_protected": [list(rect) for rect in insets], "panels": receipts,
                             "protected_share": round(float(keep.mean()), 4)}}
    (cache / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


BOX_MARGIN = 6              # pixels added around a reviewer's box, in base pixels
MAX_BOX_SHARE = 0.03        # one box may lock at most this share of the photo; larger ones are not small misses
MAX_BOXES = 8
MAX_FITTED_SHARE = 0.08     # a box whose fitted subject part is at most this share of the photo is still a miss, not a redo


def canvas_boxes_to_base(boxes, transform):
    """Reviewer boxes (0–1000 on the prepared canvas) as base-pixel rectangles; unusable boxes are left out."""
    t = transform
    rects = []
    for box in (boxes or [])[:MAX_BOXES]:
        try:
            x0, y0, x1, y1 = (float(box[key]) for key in ("x0", "y0", "x1", "y1"))
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
            continue
        corners = []
        for x, y in ((x0, y0), (x1, y1)):
            cx, cy = x * (t["width"] - 1) / 1000, y * (t["height"] - 1) / 1000
            corners.append(((cx - t["placed_x"]) / t["scale"] + t["crop_left"],
                            (cy - t["placed_y"]) / t["scale_y"] + t["crop_top"]))
        # A box drawn over the title strip or past the photo only reaches as far as the visible photo does.
        window_left, window_top = t["crop_left"], max(int(t["trim_top"]), t["crop_top"])
        window_right, window_bottom = t["crop_left"] + t["crop_width"], t["crop_top"] + t["crop_height"]
        left = max(window_left, int(corners[0][0]) - BOX_MARGIN)
        top = max(window_top, int(corners[0][1]) - BOX_MARGIN)
        right = min(window_right, int(corners[1][0] + 1) + BOX_MARGIN)
        bottom = min(window_bottom, int(corners[1][1] + 1) + BOX_MARGIN)
        if right - left >= 1 and bottom - top >= 1:
            rects.append((left, top, right - left, bottom - top))
    return rects


BOX_FIT_VERSION = "grabcut2"   # bumped when the fit changes, so older caches are refitted once
BOX_CONTEXT = 32            # pixels of surrounding mask the shape fit may see around a box
MIN_SHAPE_SHARE, MAX_SHAPE_SHARE = 0.03, 0.97


def _grab(roi_bgr, labels):
    background, foreground = np.zeros((1, 65)), np.zeros((1, 65))
    try:
        cv2.grabCut(roi_bgr, labels, None, background, foreground, 4, cv2.GC_INIT_WITH_MASK)
    except cv2.error:
        return None
    return (labels == cv2.GC_FGD) | (labels == cv2.GC_PR_FGD)


def box_shape(image_rgb, mask, rect):
    """The part of a reviewer's box that really is subject, as a boolean array of the box's size; None if unsure.

    A rectangle of real pixels would drag a patch of old table into the picture (a visible rectangular scrap), so the
    box is fitted instead: the mask around the box says what subject and background look like, GrabCut decides inside.
    Where the mask around the box shows no subject (a part of the product the cutout missed entirely) the box itself is
    the only hint: what differs from the ring around it is the part."""
    x, y, w, h = rect
    height, width = mask.shape
    x0, y0, x1, y1 = max(0, x - BOX_CONTEXT), max(0, y - BOX_CONTEXT), min(width, x + w + BOX_CONTEXT), min(height, y + h + BOX_CONTEXT)
    roi = np.ascontiguousarray(image_rgb[y0:y1, x0:x1][..., ::-1])
    labels = np.where(mask[y0:y1, x0:x1], cv2.GC_FGD, cv2.GC_BGD).astype("uint8")
    labels[y - y0:y - y0 + h, x - x0:x - x0 + w] = cv2.GC_PR_FGD
    known = labels[(labels == cv2.GC_FGD) | (labels == cv2.GC_BGD)]
    shape = None
    if (known == cv2.GC_FGD).any() and (known == cv2.GC_BGD).any():
        kept = _grab(roi, labels)
        if kept is not None:
            shape = kept[y - y0:y - y0 + h, x - x0:x - x0 + w]
    if shape is None or not MIN_SHAPE_SHARE <= float(shape.mean()) <= MAX_SHAPE_SHARE:
        ring = np.full(labels.shape, cv2.GC_BGD, dtype="uint8")
        ring[y - y0:y - y0 + h, x - x0:x - x0 + w] = cv2.GC_PR_FGD
        ring.flat[0] = cv2.GC_BGD
        kept = _grab(roi, ring)
        shape = None if kept is None else kept[y - y0:y - y0 + h, x - x0:x - x0 + w]
    if shape is None or not MIN_SHAPE_SHARE <= float(shape.mean()) <= MAX_SHAPE_SHARE:
        return None
    return cv2.dilate(shape.astype("uint8"), np.ones((3, 3), "uint8")).astype(bool)


def lock_boxes(cache, rects, base=None):
    """Add the reviewer's boxes, inside the photo windows only, to a cached fine mask; returns the boxes used.

    With the base photo given, only the subject part of each box is locked; without it (or when the fit is unsure)
    the whole box is."""
    cache = Path(cache)
    record = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
    mask = np.array(Image.open(cache / record["mask_file"]).convert("RGBA"))
    height, width = mask.shape[:2]
    image = None
    if base is not None:
        with Image.open(base) as source:
            image = np.array(source.convert("RGB"))
        if image.shape[:2] != (height, width):
            image = None
    windows = np.zeros((height, width), dtype=bool)
    for window in record["photo_rectangles"]:
        windows[window["top"]:window["top"] + window["height"], window["left"]:window["left"] + window["width"]] = True
    photo_area = max(1, int(windows.sum()))
    used = []
    for x, y, w, h in rects:
        area = np.zeros((height, width), dtype=bool)
        area[y:y + h, x:x + w] = True
        area &= windows
        if not area.any():
            continue
        shape = box_shape(image, mask[..., 3] == 255, (x, y, w, h)) if image is not None else None
        if shape is not None:
            fitted = np.zeros((height, width), dtype=bool)
            fitted[y:y + h, x:x + w] = shape
            area &= fitted
            if area.sum() > MAX_FITTED_SHARE * photo_area:
                continue
        elif area.sum() > MAX_BOX_SHARE * photo_area:
            continue
        mask[..., 3][area] = 255
        used.append([x, y, w, h])
    if not used:
        return []
    Image.fromarray(mask, "RGBA").save(cache / record["mask_file"])
    record["mask_sha256"] = hashlib.sha256((cache / record["mask_file"]).read_bytes()).hexdigest()
    record["refinement"]["reviewer_boxes"] = [*record["refinement"].get("reviewer_boxes", []), *used]
    record["refinement"]["protected_share"] = round(float((mask[..., 3] == 255).mean()), 4)
    if image is not None:
        record["refinement"]["box_fit"] = BOX_FIT_VERSION
    (cache / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return used


def refit_old_boxes(cache, base):
    """Caches written before boxes were fitted hold plain rectangles of old table; fit them now. True if changed."""
    cache = Path(cache)
    record = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
    boxes = record.get("refinement", {}).get("reviewer_boxes") or []
    if not boxes or record["refinement"].get("box_fit") == BOX_FIT_VERSION:
        return False
    mask = np.array(Image.open(cache / record["mask_file"]).convert("RGBA"))
    with Image.open(base) as source:
        image = np.array(source.convert("RGB"))
    if image.shape[:2] != mask.shape[:2]:
        return False
    changed = False
    for x, y, w, h in boxes:
        shape = box_shape(image, mask[..., 3] == 255, (x, y, w, h))
        if shape is None:
            continue
        mask[y:y + h, x:x + w, 3] = np.where(shape, 255, 0)
        changed = True
    record["refinement"]["box_fit"] = BOX_FIT_VERSION
    if changed:
        Image.fromarray(mask, "RGBA").save(cache / record["mask_file"])
        record["mask_sha256"] = hashlib.sha256((cache / record["mask_file"]).read_bytes()).hexdigest()
        record["refinement"]["protected_share"] = round(float((mask[..., 3] == 255).mean()), 4)
    (cache / "metadata.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return changed


SKIN_WEIGHT = 0.15          # hands and arms leave the frame at its edge naturally; the product must not
CUT_TOLERANCE = 6         # protected non-skin pixels a window's top rows may cross and still count as clean
TOP_CROP_WEIGHT = 2.5       # cropping the top moves the photo's edge under the title, where a cut shows


def _skin(image_rgb):
    ycc = cv2.cvtColor(image_rgb, cv2.COLOR_RGB2YCrCb)
    return (ycc[..., 1] >= 133) & (ycc[..., 1] <= 173) & (ycc[..., 2] >= 77) & (ycc[..., 2] <= 127)


def best_crop(cache, transform, step=4, base=None):
    """Where the cover-fit window should sit: the offset that crops away the least that matters.

    Only the one axis with slack matters. Protected pixels count fully, except skin (SKIN_WEIGHT) when the base photo
    is given, and a crop at the top costs more than one at the bottom. Ties centre the subject in the window (clamped,
    so a top-heavy subject stays under the title strip). Returns {"crop_left", "crop_top"} in base pixels, or None when
    the window cannot move or the mask is unreadable."""
    t = transform
    try:
        record = json.loads((Path(cache) / "metadata.json").read_text(encoding="utf-8"))
        with Image.open(Path(cache) / record["mask_file"]) as image:
            alpha = np.array(image.convert("RGBA"))[..., 3] == 255
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if alpha.shape != (t["source_height"], t["source_width"]) or not alpha.any():
        return None
    weight = alpha.astype("float64")
    rgb = None
    if base is not None:
        try:
            with Image.open(base) as photo:
                rgb = np.array(photo.convert("RGB"))
        except OSError:
            rgb = None
        if rgb is not None and rgb.shape[:2] != alpha.shape:
            rgb = None
        if rgb is not None:
            weight[_skin(rgb)] *= SKIN_WEIGHT
    slack_y = t["trim_height"] - t["crop_height"]
    slack_x = t["source_width"] - t["crop_width"]
    if slack_y > 0 and slack_x <= 0:
        rows = weight[t["trim_top"]:].sum(axis=1)
        total = np.concatenate([[0], np.cumsum(rows)])
        lost = lambda offset: TOP_CROP_WEIGHT * total[offset] + total[-1] - total[offset + t["crop_height"]]
        # Equal losses (the subject fits) put it in the middle of the window; with no room to centre it, at the top.
        used = np.nonzero(alpha[t["trim_top"]:].any(axis=1))[0]
        ideal = int((used[0] + used[-1] + 1 - t["crop_height"]) / 2)
        # A window whose top edge slices through product (a rod, a handle) leaves a cut right under the title. If some
        # window avoids that, only those are considered; when the product leaves the photo at its top there is none.
        solid = weight * (~_skin(rgb) if rgb is not None else 1)
        crossing = lambda offset: float(solid[t["trim_top"] + offset:t["trim_top"] + offset + 2].sum())
        offsets = list(range(0, slack_y + 1, step))
        clean = [offset for offset in offsets if crossing(offset) <= CUT_TOLERANCE]
        best = min(clean or offsets, key=lambda offset: (lost(offset), abs(offset - ideal)))
        return {"crop_left": t["crop_left"], "crop_top": t["trim_top"] + best}
    if slack_x > 0 and slack_y <= 0:
        cols = weight[t["trim_top"]:].sum(axis=0)
        total = np.concatenate([[0], np.cumsum(cols)])
        lost = lambda offset: total[offset] + total[-1] - total[offset + t["crop_width"]]
        used = np.nonzero(alpha[t["trim_top"]:].any(axis=0))[0]
        ideal = int((used[0] + used[-1] + 1 - t["crop_width"]) / 2)
        best = min(range(0, slack_x + 1, step), key=lambda offset: (lost(offset), abs(offset - ideal)))
        return {"crop_left": best, "crop_top": t["crop_top"]}
    return None
