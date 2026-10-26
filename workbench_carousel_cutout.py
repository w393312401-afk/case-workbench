"""Fine subject masks for subject-locked image runs, made through the image channel's transparent cutout."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import re

from codex_bridge import BridgeCancelled
from carousel_cutout import best_crop, canvas_boxes_to_base, lock_boxes, refine_subject_mask, refit_old_boxes

TABLE_COMPOSE = re.compile(r"(TBL-[0-9a-f]{12})-(\d+)")
COMPOSE_ID = re.compile(r"CMP-[0-9a-f]{12}")


class CarouselCutoutMixin:
    """Needs the carousel image mixin (progress, carousel folder) and self.production from the workbench."""

    def _base_layout(self, identifier, base):
        """The panel layout a composed base was pasted with; None for a plain photo or an unreadable receipt."""
        if isinstance(base.get("compose_layout"), dict):
            return base["compose_layout"]
        compose_id = str(base.get("compose_id") or "")
        path, key = None, None
        if COMPOSE_ID.fullmatch(compose_id):
            path = self._carousel_dir(identifier) / "compositions" / compose_id / "receipt.json"
        elif (match := TABLE_COMPOSE.fullmatch(compose_id)):
            path, key = self.production.root / "carousel_tables" / match[1] / "compositions" / f"slide{int(match[2]):02d}.json", "receipt"
        if path is None:
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value[key] if key else value
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _refine_subject_masks(self, identifier, generation_id, snapshots, plan_by_number, cancel):
        """Cut out every base that has no fine mask yet; a slide whose cutout fails keeps the AI polygon outline."""
        bridge = getattr(self, "image_generator", None)
        if not getattr(bridge, "supports_cutout", False):
            return {}
        item = self.production.carousel(identifier)
        slides = {slide["number"]: slide for slide in item["slides"]}
        root = self._carousel_dir(identifier)
        jobs, results = [], {}
        for snapshot, work, base in snapshots:
            number = snapshot["slide"]["number"]
            cache = root / "subject-masks" / snapshot["base_sha256"]
            if (cache / "metadata.json").is_file():
                try:
                    refit_old_boxes(cache, base)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                results[number] = {"status": "cached"}
                continue
            parts = plan_by_number.get(number, {}).get("subject_parts")
            parts = [str(part)[:200] for part in parts if str(part).strip()][:20] if isinstance(parts, list) else []
            layout = self._base_layout(identifier, (slides.get(number) or {}).get("base") or {})
            jobs.append((number, base, cache, layout, int(snapshot["subject_lock"]["transform"].get("trim_top", 0)), parts))
        if not jobs:
            return results
        self._image_progress(generation_id, f"正在用抠图为 {len(jobs)} 张底图生成精细主体保护区。")

        def run(job):
            number, base, cache, layout, trim_top, parts = job
            return refine_subject_mask(bridge, base, cache, layout=layout, trim_top=trim_top, parts=parts, cancel=cancel,
                                       progress=lambda text: self._image_progress(generation_id, f"第 {number} 张抠图 · {text}"))

        with ThreadPoolExecutor(max_workers=min(5, len(jobs))) as pool:
            futures = {job[0]: pool.submit(run, job) for job in jobs}
            for number, future in futures.items():
                try:
                    results[number] = {"status": "refined", "protected_share": future.result()["refinement"]["protected_share"]}
                except BridgeCancelled:
                    raise
                except Exception as exc:
                    results[number] = {"status": "polygon", "error": str(exc)[:300]}
                    self._image_progress(generation_id, f"第 {number} 张抠图没有通过对位检查，改用 AI 轮廓保护区：{str(exc)[:120]}")
        if cancel.is_set():
            raise BridgeCancelled("抠图已停止。")
        return results

    def _lock_missed_boxes(self, identifier, generation_id, snapshots, failed, plan_by_number=None):
        """Expand protection at the reviewer's misses; a rebuilt mask still needs the same coverage check."""
        root = self._carousel_dir(identifier)
        by_number = {snapshot["slide"]["number"]: (snapshot, base) for snapshot, _, base in snapshots}
        locked = []
        for row in failed:
            snapshot, base = by_number.get(row.get("number"), (None, None))
            if not snapshot:
                continue
            cache = root / "subject-masks" / snapshot["base_sha256"]
            rects = canvas_boxes_to_base(row.get("missing_boxes"), snapshot["subject_lock"]["transform"])
            if (cache / "metadata.json").is_file():
                changed = rects and lock_boxes(cache, rects, base)
            else:
                # A failed cutout keeps the planner's polygons, so there is no fine-mask cache to patch.
                # Append conservative boxes on that same canvas rather than silently ignoring the misses.
                entry = (plan_by_number or {}).get(row["number"])
                regions = entry.get("protected_regions") if isinstance(entry, dict) else None
                if not isinstance(regions, list):
                    continue
                t = snapshot["subject_lock"]["transform"]
                canvas_x = lambda value: min(1000, max(0, (t["placed_x"] + (value - t["crop_left"]) * t["scale"]) * 1000 / (t["width"] - 1)))
                canvas_y = lambda value: min(1000, max(0, (t["placed_y"] + (value - t["crop_top"]) * t["scale_y"]) * 1000 / (t["height"] - 1)))
                changed = False
                for x, y, width, height in rects:
                    x0, y0, x1, y1 = canvas_x(x), canvas_y(y), canvas_x(x + width), canvas_y(y + height)
                    polygon = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
                    if x0 < x1 and y0 < y1 and polygon not in regions and len(regions) < 80:
                        regions.append(polygon)
                        changed = True
            if changed:
                locked.append(row["number"])
        if locked:
            self._image_progress(generation_id, f"按核对标出的位置，为第 {'、'.join(map(str, locked))} 张补锁原像素，再核对一次。")
        return locked

    def _refit_subject_canvases(self, identifier, generation_id, snapshots):
        """Slide each cover-fit window to where the fine mask loses the least; returns the slides moved.

        Runs after the cutouts exist and before the protected area is built, so everything downstream (coverage
        check, background plate, composition) sees the final canvas. A base without a fine mask keeps its window."""
        from carousel_subject_lock import prepare_subject_canvas
        root = self._carousel_dir(identifier)
        moved = []
        for snapshot, work, base in snapshots:
            lock = snapshot["subject_lock"]
            cache = root / "subject-masks" / snapshot["base_sha256"]
            if not (cache / "metadata.json").is_file() or lock["transform"].get("version") != 2:
                continue
            crop = best_crop(cache, lock["transform"], base=base)
            if not crop or (crop["crop_left"], crop["crop_top"]) == (lock["transform"]["crop_left"], lock["transform"]["crop_top"]):
                continue
            snapshot["subject_lock"] = prepare_subject_canvas(base, work, snapshot["aspect"], crop)
            (work / "input.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            moved.append(snapshot["slide"]["number"])
        if moved:
            self._image_progress(generation_id, f"按主体位置调整第 {'、'.join(map(str, moved))} 张的画面取景，尽量不裁到商品。")
        return moved
