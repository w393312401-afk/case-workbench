"""Fine subject masks from transparent cutouts: alignment, panels, whole insets, edge inset; no live image channel."""
import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image, ImageDraw

from carousel_cutout import (BOX_MARGIN, INSET_MARGIN, best_crop, box_shape, refit_old_boxes, canvas_boxes_to_base, cutout_prompt, fill_small_holes,
                            layout_panels, lock_boxes, refine_subject_mask)
from cockpit_image_bridge import CockpitImageBridge
from codex_bridge import BridgeError
from workbench_carousel_cutout import CarouselCutoutMixin


def textured(size, seed=1):
    """Smooth, photo-like texture: resampling keeps it recognizable, as it does a real photo."""
    import cv2
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 255, (size[1], size[0], 3), dtype="uint8")
    smooth = cv2.GaussianBlur(noise, (0, 0), 6).astype(float)
    smooth = (smooth - smooth.min()) / (smooth.max() - smooth.min())
    return (70 + smooth * 140).astype("uint8")


def draw_product(array, box):
    """A dark product with its own texture, so the cutout and the alignment have something real to hold on to."""
    x0, y0, x1, y1 = box
    array[y0:y1, x0:x1] = (textured((x1 - x0, y1 - y0), seed=7).astype(int) - 70) * 45 // 140
    return array


class CutoutBridge:
    """Answers like the image channel: the input redrawn at another size, with alpha on the dark product pixels."""
    supports_cutout = True

    def __init__(self, *, shift=0, scale=.75):
        self.calls, self.shift, self.scale = [], shift, scale

    def run(self, prompt, *, cwd, images, cancel_event, on_progress, timeout, cutout):
        source = np.array(Image.open(images[0]).convert("RGB"))
        self.calls.append({"prompt": prompt, "cutout": cutout, "source": source.copy()})
        on_progress("正在抠图")
        alpha = np.where(source.max(axis=2) < 55, 255, 0).astype("uint8")
        if self.shift:
            source, alpha = np.roll(source, self.shift, axis=1), np.roll(alpha, self.shift, axis=1)
        rgba = np.dstack([source, alpha])
        image = Image.fromarray(rgba, "RGBA")
        image = image.resize((max(1, int(image.width * self.scale)), max(1, int(image.height * self.scale))), Image.LANCZOS)
        image.save(Path(cwd) / "render.png")
        return json.dumps({"file": "render.png", "summary": ""})


class RefineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def base(self, size, boxes):
        array = textured(size)
        for box in boxes:
            draw_product(array, box)
        path = self.root / "base.png"
        Image.fromarray(array).save(path)
        return path

    def refine(self, bridge, base, layout=None, trim_top=0, parts=("the knife sharpener", "the open case lid")):
        return refine_subject_mask(bridge, base, self.root / "cache", layout=layout, trim_top=trim_top, parts=list(parts),
                                   cancel=threading.Event(), progress=lambda text: None)

    def alpha(self):
        return np.array(Image.open(self.root / "cache" / "mask.png"))[..., 3]

    def test_a_plain_photo_is_cut_out_aligned_up_to_its_real_edge(self):
        base = self.base((300, 400), [(100, 120, 200, 300)])
        bridge = CutoutBridge()
        record = self.refine(bridge, base)
        self.assertEqual(len(bridge.calls), 1)
        self.assertIn("the open case lid", bridge.calls[0]["prompt"])
        self.assertEqual(bridge.calls[0]["cutout"], {"size": "1024x1536", "quality": "high"})
        alpha = self.alpha()
        self.assertEqual(set(np.unique(alpha)), {0, 255})
        self.assertEqual(alpha[200, 150], 255)
        self.assertEqual(alpha[200, 101], 255, "a real edge stays inside the lock")
        self.assertEqual(alpha[200, 96], 0)
        self.assertEqual(alpha[50, 50], 0)
        self.assertEqual(record["base_sha256"], hashlib.sha256(base.read_bytes()).hexdigest())
        self.assertEqual(record["mask_sha256"], hashlib.sha256((self.root / "cache" / "mask.png").read_bytes()).hexdigest())
        self.assertEqual(record["photo_rectangles"], [{"left": 0, "top": 0, "width": 300, "height": 400}])
        self.assertEqual(json.loads((self.root / "cache" / "metadata.json").read_text(encoding="utf-8")), record)
        self.assertLess(record["refinement"]["panels"][0]["residual"], 10)

    def test_an_inset_is_kept_whole_with_its_frame_and_hidden_from_the_main_cutout(self):
        base = self.base((300, 400), [(20, 40, 120, 200)])
        layout = {"size": [300, 400], "layout": "main_inset",
                  "panels": [{"file": "F1", "rect": [0, 0, 300, 320]}, {"file": "F2", "rect": [180, 200, 100, 100]}]}
        bridge = CutoutBridge()
        record = self.refine(bridge, base, layout)
        self.assertEqual(len(bridge.calls), 1, "only the main photo is cut out")
        self.assertTrue((bridge.calls[0]["source"][200:300, 180:280] == 128).all())
        alpha = self.alpha()
        frame = (180 - INSET_MARGIN, 200 - INSET_MARGIN, 100 + 2 * INSET_MARGIN, 100 + 2 * INSET_MARGIN)
        self.assertTrue((alpha[frame[1]:frame[1] + frame[3], frame[0]:frame[0] + frame[2]] == 255).all())
        self.assertEqual(alpha[100, 70], 255)
        self.assertEqual(alpha[360, 150], 0, "the caption band outside every photo stays editable")
        self.assertEqual(record["photo_rectangles"][1], dict(zip(("left", "top", "width", "height"), frame)))

    def test_every_stacked_photo_is_cut_out_on_its_own(self):
        base = self.base((300, 400), [(50, 40, 150, 150), (120, 240, 250, 360)])
        layout = {"size": [300, 400], "layout": "stack",
                  "panels": [{"file": "F1", "rect": [0, 0, 300, 195]}, {"file": "F2", "rect": [0, 205, 300, 195]}]}
        bridge = CutoutBridge()
        record = self.refine(bridge, base, layout, trim_top=10)
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(sorted(call["source"].shape[:2] for call in bridge.calls), [(195, 300), (195, 300)])
        alpha = self.alpha()
        self.assertEqual((alpha[100, 100], alpha[300, 180], alpha[200, 100]), (255, 255, 0))
        self.assertEqual(record["photo_rectangles"][0]["top"], 10, "photo windows start below a trimmed flat top")

    def test_a_reframed_cutout_is_refused_and_no_cache_is_written(self):
        base = self.base((300, 400), [(100, 120, 200, 300)])
        with self.assertRaises(BridgeError) as caught:
            self.refine(CutoutBridge(shift=45), base)
        self.assertTrue(any(word in str(caught.exception) for word in ("挪动", "对不上")), str(caught.exception))
        self.assertFalse((self.root / "cache" / "metadata.json").exists())

    def test_small_enclosed_gaps_are_kept_but_open_background_is_not(self):
        keep = np.zeros((100, 100), dtype=bool)
        keep[20:80, 20:80] = True
        keep[40:44, 40:44] = False          # a gap between fingers, ringed by the subject (16 px, under 0.2%)
        keep[20:80, 60:62] = False          # a slit open to the edge of the subject is real background
        keep[0:10, 0:10] = False
        filled = fill_small_holes(keep)
        self.assertTrue(filled[40:44, 40:44].all())
        self.assertFalse(filled[50, 60])
        self.assertFalse(filled[90, 90])
        big = np.ones((100, 100), dtype=bool)
        big[30:70, 30:70] = False           # a window far larger than a gap stays editable
        self.assertFalse(fill_small_holes(big)[50, 50])

    def test_layouts_that_do_not_match_the_base_fall_back_to_one_photo(self):
        self.assertEqual(layout_panels({"size": [10, 10], "panels": [{"rect": [0, 0, 5, 5]}]}, (300, 400)),
                         [{"rect": (0, 0, 300, 400), "inset": False}])
        self.assertEqual(layout_panels({"size": [300, 400], "panels": [{"rect": [0, 0, 500, 5]}]}, (300, 400)),
                         [{"rect": (0, 0, 300, 400), "inset": False}])
        self.assertIn("hands", cutout_prompt([]))

    def test_the_cache_drives_the_refined_subject_mask(self):
        from carousel_subject_lock import _runtime, build_subject_mask, prepare_subject_canvas
        try:
            _runtime()
        except BridgeError:
            self.skipTest("本机图像组件不可用")
        car = self.root / "CAR"
        work = car / "generations" / "IMG-000000000000" / "01"
        work.mkdir(parents=True)
        base = work / "base.png"
        array = draw_product(textured((600, 800)), (200, 250, 400, 600))
        Image.fromarray(array).save(base)
        lock = prepare_subject_canvas(base, work, "3:4")
        digest = hashlib.sha256(base.read_bytes()).hexdigest()
        (work / "input.json").write_text(json.dumps({"base_sha256": digest, "subject_lock": lock}), encoding="utf-8")
        refine_subject_mask(CutoutBridge(), base, car / "subject-masks" / digest, layout=None,
                            trim_top=lock["transform"]["trim_top"], parts=[], cancel=threading.Event(), progress=lambda text: None)
        mask = build_subject_mask(work, [[[0, 0], [10, 0], [10, 10]]])
        self.assertEqual(mask["mask_mode"], "refined_segmentation")
        t = lock["transform"]
        with Image.open(work / mask["file"]) as protection:
            inside = (t["placed_x"] + round(300 * t["scale"]), t["placed_y"] + round(420 * t["scale"]))
            outside = (t["placed_x"] + round(50 * t["scale"]), t["placed_y"] + round(50 * t["scale"]))
            self.assertEqual((protection.getpixel(inside)[3], protection.getpixel(outside)[3]), (255, 0))


class BridgeCutoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.job = root / "job"
        self.job.mkdir()
        parameters = root / "parameters.json"
        parameters.write_text(json.dumps({"通道": {"Cockpit本机API": {"基址": "http://127.0.0.1:12359/v1",
            "参考图编辑": {"method": "POST", "path": "/images/edits"}}},
            "可复用请求形式": {"本机参考图编辑JSON": {"model": "gpt-image-2.5", "size": "1024x1536", "quality": "low",
            "background": "opaque", "output_format": "webp", "output_compression": 85}}}, ensure_ascii=False), encoding="utf-8")
        self.base = self.job / "source.png"
        Image.new("RGB", (30, 40), "red").save(self.base)
        buffer = io.BytesIO()
        Image.new("RGBA", (30, 40), (0, 0, 0, 0)).save(buffer, format="PNG")
        self.calls = []
        response = json.dumps({"data": [{"b64_json": base64.b64encode(buffer.getvalue()).decode()}]}).encode()
        self.bridge = CockpitImageBridge(parameters_file=parameters, key_provider=lambda: "key", retry_delays=(0, 0),
                                         transport=lambda method, url, headers, payload, timeout: (
                                             self.calls.append(json.loads(payload)) or (200, {}, response)))

    def run_cutout(self, **kwargs):
        return self.bridge.run("cut out", cwd=self.job, images=[self.base], cancel_event=threading.Event(),
                               on_progress=lambda text: None, **kwargs)

    def test_a_cutout_asks_for_a_transparent_png_at_the_chosen_size(self):
        self.assertEqual(json.loads(self.run_cutout(cutout={"size": "2048x2048", "quality": "high"}))["file"], "render.png")
        body = self.calls[-1]
        self.assertEqual((body["background"], body["output_format"], body["size"], body["quality"]),
                         ("transparent", "png", "2048x2048", "high"))
        self.assertNotIn("output_compression", body)

    def test_invalid_cutout_options_are_refused_before_any_request(self):
        for options in ({"size": "999x999"}, {"size": "2048x2048", "quality": "ultra"}):
            with self.subTest(options=options), self.assertRaises(BridgeError):
                self.run_cutout(cutout=options)
        self.assertEqual(self.calls, [])


class BaseLayoutTests(unittest.TestCase):
    def test_compose_receipts_are_found_from_either_kind_of_composite(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bench = CarouselCutoutMixin()
            bench._carousel_dir = lambda identifier: root / "carousels" / identifier
            bench.production = SimpleNamespace(root=root)
            receipt = {"size": [10, 10], "layout": "single", "panels": [{"rect": [0, 0, 10, 10]}]}
            cmp = root / "carousels" / "CAR-1" / "compositions" / "CMP-0123456789ab"
            cmp.mkdir(parents=True)
            (cmp / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
            table = root / "carousel_tables" / "TBL-0123456789ab" / "compositions"
            table.mkdir(parents=True)
            (table / "slide05.json").write_text(json.dumps({"plan": {}, "receipt": receipt}), encoding="utf-8")
            self.assertEqual(bench._base_layout("CAR-1", {"compose_id": "CMP-0123456789ab"}), receipt)
            self.assertEqual(bench._base_layout("CAR-1", {"compose_id": "TBL-0123456789ab-5"}), receipt)
            self.assertEqual(bench._base_layout("CAR-1", {"compose_layout": receipt, "compose_id": "x"}), receipt)
            self.assertIsNone(bench._base_layout("CAR-1", {"compose_id": "TBL-0123456789ab-6"}))
            self.assertIsNone(bench._base_layout("CAR-1", {}))


class CachedCutoutTests(unittest.TestCase):
    def test_an_earlier_cut_of_the_same_base_is_reused_without_another_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            car = root / "carousels" / "CAR-1"
            work = car / "generations" / "IMG-1" / "02"
            work.mkdir(parents=True)
            base = work / "base.png"
            Image.fromarray(draw_product(textured((300, 400)), (100, 120, 200, 300))).save(base)
            digest = hashlib.sha256(base.read_bytes()).hexdigest()
            bench = CarouselCutoutMixin()
            bench._carousel_dir = lambda identifier: car
            bench.production = SimpleNamespace(root=root, carousel=lambda identifier: {"slides": [{"number": 2, "base": {}}]})
            bench.image_generator = CutoutBridge()
            bench._image_progress = lambda generation_id, text: None
            snapshots = [({"slide": {"number": 2}, "base_sha256": digest, "subject_lock": {"transform": {"trim_top": 0}}}, work, base)]
            first = bench._refine_subject_masks("CAR-1", "IMG-1", snapshots, {2: {"subject_parts": ["the sharpener"]}}, threading.Event())
            self.assertEqual(first[2]["status"], "refined")
            self.assertIn("the sharpener", bench.image_generator.calls[-1]["prompt"])
            self.assertEqual(bench._refine_subject_masks("CAR-1", "IMG-1", snapshots, {}, threading.Event()), {2: {"status": "cached"}})
            self.assertEqual(len(bench.image_generator.calls), 1)


class MissedBoxTests(unittest.TestCase):
    # A cover-fit window: base rows 40..40+crop_height land below the 224 px title strip, scaled 2x, full width.
    transform = {"version": 2, "width": 1536, "height": 2048, "placed_x": 0, "placed_y": 224, "placed_width": 1536,
                 "placed_height": 1824, "scale": 2.0, "scale_y": 2.0, "trim_top": 10, "crop_left": 0, "crop_top": 40,
                 "crop_width": 768, "crop_height": 912, "source_width": 768, "source_height": 1000, "trim_height": 990}

    def test_canvas_boxes_map_back_to_base_pixels_with_a_margin(self):
        x = lambda base_x: (base_x * 2) * 1000 / 1535
        y = lambda base_y: (224 + (base_y - 40) * 2) * 1000 / 2047
        rects = canvas_boxes_to_base([{"x0": x(100), "y0": y(200), "x1": x(150), "y1": y(260)},
                                      {"x0": 10, "y0": 10, "x1": 5, "y1": 20}, {"x0": "a"}], self.transform)
        self.assertEqual(len(rects), 1)
        left, top, width, height = rects[0]
        self.assertAlmostEqual(left, 100 - BOX_MARGIN, delta=1)
        self.assertAlmostEqual(top, 200 - BOX_MARGIN, delta=1)
        self.assertAlmostEqual(width, 50 + 2 * BOX_MARGIN, delta=2)
        self.assertAlmostEqual(height, 60 + 2 * BOX_MARGIN, delta=2)

    def test_boxes_over_the_title_strip_stay_inside_the_visible_photo(self):
        t = self.transform
        straddling = canvas_boxes_to_base([{"x0": 100, "y0": 50, "x1": 200, "y1": 150}], t)
        self.assertEqual(len(straddling), 1)
        self.assertEqual(straddling[0][1], t["crop_top"], "a box that starts over the title strip starts at the photo's top edge")
        self.assertEqual(canvas_boxes_to_base([{"x0": 100, "y0": 0, "x1": 200, "y1": 60}], t), [],
                         "a box entirely over the title strip has no photographed pixels to lock")

    def cache(self, root):
        cache = root / "cache"
        cache.mkdir()
        Image.fromarray(np.zeros((400, 300, 4), dtype="uint8"), "RGBA").save(cache / "mask.png")
        record = {"base_sha256": "x", "mask_file": "mask.png", "mask_sha256": "", "photo_frames": [],
                  "photo_rectangles": [{"left": 0, "top": 0, "width": 300, "height": 300}], "refinement": {}}
        (cache / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
        return cache

    def test_boxes_lock_only_inside_photo_windows_and_large_boxes_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = self.cache(Path(folder))
            self.assertEqual(lock_boxes(cache, [(0, 0, 300, 300)]), [], "a box over most of the photo is not a small miss")
            used = lock_boxes(cache, [(20, 290, 10, 20), (100, 100, 10, 10)])
            self.assertEqual(used, [[20, 290, 10, 20], [100, 100, 10, 10]])
            alpha = np.array(Image.open(cache / "mask.png"))[..., 3]
            self.assertEqual((alpha[105, 105], alpha[295, 25], alpha[305, 25], alpha[50, 50]), (255, 255, 0, 0))
            record = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(record["mask_sha256"], hashlib.sha256((cache / "mask.png").read_bytes()).hexdigest())
            self.assertEqual(record["refinement"]["reviewer_boxes"], used)

    def test_only_slides_with_a_cached_mask_and_usable_boxes_are_changed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            car = root / "CAR-1"
            (car / "subject-masks").mkdir(parents=True)
            self.cache(car / "subject-masks").rename(car / "subject-masks" / "h2")
            bench = CarouselCutoutMixin()
            bench._carousel_dir = lambda identifier: car
            messages = []
            bench._image_progress = lambda generation_id, text: messages.append(text)
            t = {"version": 2, "width": 300, "height": 400, "placed_x": 0, "placed_y": 0, "placed_width": 300,
                 "placed_height": 400, "scale": 1.0, "scale_y": 1.0, "trim_top": 0, "trim_height": 400, "crop_left": 0,
                 "crop_top": 0, "crop_width": 300, "crop_height": 400, "source_width": 300, "source_height": 400}
            snapshots = [({"slide": {"number": n}, "base_sha256": f"h{n}", "subject_lock": {"transform": t}}, None, None) for n in (2, 3)]
            failed = [{"number": 2, "missing_boxes": [{"x0": 300, "y0": 300, "x1": 340, "y1": 330}]},
                      {"number": 3, "missing_boxes": [{"x0": 300, "y0": 300, "x1": 340, "y1": 330}]},
                      {"number": 2, "missing_boxes": []}]
            self.assertEqual(bench._lock_missed_boxes("CAR-1", "IMG-1", snapshots, failed), [2])
            self.assertIn("第 2 张", messages[-1])


class BoxFitTests(unittest.TestCase):
    """A reviewer's box locks the missed part itself, not the rectangle of table around it."""

    def scene(self):
        rng = np.random.default_rng(3)
        image = np.zeros((300, 300, 3), dtype="uint8")
        image[:] = (205, 170, 120)                                     # light table
        image = np.clip(image + rng.integers(-6, 7, image.shape), 0, 255).astype("uint8")
        image[100:200, 40:260] = (35, 36, 40)                          # a dark bar the cutout already holds
        image[150:215, 150:200] = (35, 36, 40)                         # a dark lug the cutout missed
        mask = np.zeros((300, 300), dtype=bool)
        mask[100:200, 40:260] = True
        return image, mask

    box = (145, 190, 60, 35)    # rows 190..224: a strip of the bar, the lug below it, and plain table under the lug

    def test_fit_keeps_the_part_and_drops_the_table_inside_the_box(self):
        image, mask = self.scene()
        shape = box_shape(image, mask, self.box)
        self.assertIsNotNone(shape)
        self.assertGreater(shape[12:24, 8:50].mean(), .9, "the lug below the bar is kept")
        self.assertLess(shape[27:35, 0:50].mean(), .1, "the plain table under it is not")

    def test_a_part_with_no_masked_subject_nearby_is_fitted_from_the_box_alone(self):
        image, _ = self.scene()
        image[:] = (205, 170, 120)
        image[200:222, 150:200] = (35, 36, 40)                         # a missed part and nothing else in reach
        shape = box_shape(image, np.zeros((300, 300), dtype=bool), self.box)
        self.assertIsNotNone(shape, "the box is the hint when the mask around it knows no subject")
        self.assertGreater(shape[12:24, 8:50].mean(), .85)
        self.assertLess(shape[0:8, :].mean(), .1, "the table above the part is not locked")
        self.assertLess(shape.mean(), .75)

    def test_a_featureless_box_is_left_to_the_caller(self):
        image = np.full((300, 300, 3), (205, 170, 120), dtype="uint8")
        self.assertIsNone(box_shape(image, np.ones((300, 300), dtype=bool), self.box), "nothing to tell part from table")

    def cache(self, folder, image, mask, boxes, fitted=False):
        cache = Path(folder) / "cache"
        cache.mkdir()
        alpha = np.where(mask, 255, 0).astype("uint8")
        for x, y, w, h in boxes:
            alpha[y:y + h, x:x + w] = 255
        Image.fromarray(np.dstack([np.zeros((300, 300, 3), dtype="uint8"), alpha]), "RGBA").save(cache / "mask.png")
        refinement = {"reviewer_boxes": [list(box) for box in boxes]}
        if fitted:
            refinement["box_fit"] = "grabcut"
        record = {"base_sha256": "x", "mask_file": "mask.png", "mask_sha256": "", "photo_frames": [],
                  "photo_rectangles": [{"left": 0, "top": 0, "width": 300, "height": 300}], "refinement": refinement}
        (cache / "metadata.json").write_text(json.dumps(record), encoding="utf-8")
        base = Path(folder) / "base.png"
        Image.fromarray(image).save(base)
        return cache, base

    def test_lock_boxes_with_the_photo_locks_only_the_part_and_records_it(self):
        image, mask = self.scene()
        with tempfile.TemporaryDirectory() as folder:
            cache, base = self.cache(folder, image, mask, [])
            self.assertEqual(lock_boxes(cache, [self.box], base), [list(self.box)])
            alpha = np.array(Image.open(cache / "mask.png"))[..., 3]
            self.assertEqual(alpha[208, 175], 255)
            self.assertEqual(alpha[222, 160], 0, "table under the part stays editable")
            self.assertEqual(json.loads((cache / "metadata.json").read_text(encoding="utf-8"))["refinement"]["box_fit"], "grabcut2")

    def test_old_rectangle_locks_are_refitted_once(self):
        image, mask = self.scene()
        with tempfile.TemporaryDirectory() as folder:
            cache, base = self.cache(folder, image, mask, [self.box])
            self.assertEqual(np.array(Image.open(cache / "mask.png"))[222, 160, 3], 255, "starts as a plain rectangle")
            self.assertTrue(refit_old_boxes(cache, base))
            alpha = np.array(Image.open(cache / "mask.png"))[..., 3]
            self.assertEqual((alpha[208, 175], alpha[222, 160]), (255, 0))
            self.assertFalse(refit_old_boxes(cache, base), "already refitted")
            record = json.loads((cache / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(record["mask_sha256"], hashlib.sha256((cache / "mask.png").read_bytes()).hexdigest())


class HoleTests(unittest.TestCase):
    def ring(self):
        keep = np.zeros((200, 200), dtype=bool)
        keep[40:160, 40:160] = True
        keep[92:100, 92:100] = False                       # a 64 px island ringed by the subject
        return keep

    def test_a_dark_island_is_product_seen_through_a_gap_and_a_bright_one_is_table(self):
        keep = self.ring()
        dark = np.full((200, 200, 3), 20, dtype="uint8")
        bright = np.full((200, 200, 3), 20, dtype="uint8")
        bright[92:100, 92:100] = (215, 180, 130)
        self.assertTrue(fill_small_holes(keep, dark)[95, 95], "dark gaps close")
        self.assertFalse(fill_small_holes(keep, bright)[95, 95], "table seen through the jig stays background")
        self.assertTrue(fill_small_holes(keep)[95, 95], "without the photo the old rule applies")

    def test_specks_close_whatever_their_colour(self):
        keep = np.zeros((200, 200), dtype=bool)
        keep[40:160, 40:160] = True
        keep[95, 95] = False
        bright = np.full((200, 200, 3), 230, dtype="uint8")
        self.assertTrue(fill_small_holes(keep, bright)[95, 95])


class CropWindowTests(unittest.TestCase):
    """The cover-fit window slides to where it crops away the fewest protected pixels."""

    def transform(self, source=(600, 800), crop_height=700, crop_top=0, trim_top=0):
        return {"version": 2, "source_width": source[0], "source_height": source[1], "trim_top": trim_top,
                "trim_height": source[1] - trim_top, "crop_left": 0, "crop_top": crop_top, "crop_width": source[0],
                "crop_height": crop_height}

    def cache(self, root, protected):
        cache = Path(root) / "cache"
        cache.mkdir()
        alpha = np.zeros((800, 600), dtype="uint8")
        for x, y, w, h in protected:
            alpha[y:y + h, x:x + w] = 255
        Image.fromarray(np.dstack([np.zeros((800, 600, 3), dtype="uint8"), alpha]), "RGBA").save(cache / "mask.png")
        (cache / "metadata.json").write_text(json.dumps({"mask_file": "mask.png"}), encoding="utf-8")
        return cache

    def test_window_keeps_the_subject_and_crops_the_empty_end(self):
        with tempfile.TemporaryDirectory() as folder:
            low = self.cache(folder, [(100, 300, 300, 480)])
            # The window can move 0..100 rows; rows 300..779 fit from 80 on, and centring them pushes it to the end.
            self.assertEqual(best_crop(low, self.transform())["crop_top"], 100, "subject sits low: the empty top is cropped")
        with tempfile.TemporaryDirectory() as folder:
            high = self.cache(folder, [(100, 20, 300, 480)])
            self.assertEqual(best_crop(high, self.transform())["crop_top"], 0, "ties and top-heavy subjects stay under the title")

    def test_a_window_that_would_slice_a_thin_rod_at_its_top_edge_is_avoided_when_another_is_clean(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / "cache"
            cache.mkdir()
            alpha = np.zeros((800, 600), dtype="uint8")
            alpha[150:300, 300:306] = 255                  # a thin rod from row 150 to 300
            alpha[300:790, 100:500] = 255                  # the body below it
            Image.fromarray(np.dstack([np.zeros((800, 600, 3), dtype="uint8"), alpha]), "RGBA").save(cache / "mask.png")
            (cache / "metadata.json").write_text(json.dumps({"mask_file": "mask.png"}), encoding="utf-8")
            base = Path(folder) / "base.png"
            Image.fromarray(np.full((800, 600, 3), (60, 60, 60), dtype="uint8")).save(base)
            # Pixel loss alone would drop the rod's tip (cheap) by sliding down; the clean window keeps it whole.
            crop = best_crop(cache, self.transform(crop_height=640), base=base)["crop_top"]
            self.assertLessEqual(crop, 148, "the window starts above the rod's tip, not through the rod")

    def test_no_slack_unreadable_or_empty_masks_leave_the_window_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = self.cache(folder, [(100, 100, 100, 100)])
            self.assertIsNone(best_crop(cache, self.transform(crop_height=800)))
            self.assertIsNone(best_crop(Path(folder) / "missing", self.transform()))
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(best_crop(self.cache(folder, []), self.transform()))

    def test_the_refit_step_reprepares_only_slides_whose_window_moves(self):
        from carousel_subject_lock import _runtime
        try:
            _runtime()
        except BridgeError:
            self.skipTest("本机图像组件不可用")
        from carousel_subject_lock import prepare_subject_canvas
        with tempfile.TemporaryDirectory() as folder:
            car = Path(folder) / "CAR"
            work = car / "generations" / "IMG-1" / "01"
            work.mkdir(parents=True)
            base = work / "base.png"
            Image.fromarray(draw_product(textured((600, 1000)), (150, 600, 450, 980))).save(base)   # tall photo, subject low
            digest = hashlib.sha256(base.read_bytes()).hexdigest()
            lock = prepare_subject_canvas(base, work, "3:4")
            self.assertEqual(lock["transform"]["crop_top"], 0)
            cache = car / "subject-masks" / digest
            cache.mkdir(parents=True)
            alpha = np.zeros((1000, 600), dtype="uint8")
            alpha[600:980, 150:450] = 255
            Image.fromarray(np.dstack([np.zeros((1000, 600, 3), dtype="uint8"), alpha]), "RGBA").save(cache / "mask.png")
            (cache / "metadata.json").write_text(json.dumps({"mask_file": "mask.png"}), encoding="utf-8")
            snapshot = {"slide": {"number": 1}, "aspect": "3:4", "base_sha256": digest, "subject_lock": lock}
            bench = CarouselCutoutMixin()
            bench._carousel_dir = lambda identifier: car
            bench._image_progress = lambda generation_id, text: None
            self.assertEqual(bench._refit_subject_canvases("CAR", "IMG-1", [(snapshot, work, base)]), [1])
            moved = snapshot["subject_lock"]["transform"]
            self.assertGreater(moved["crop_top"], 0)
            self.assertEqual(json.loads((work / "input.json").read_text(encoding="utf-8"))["subject_lock"]["transform"], moved)
            self.assertEqual(bench._refit_subject_canvases("CAR", "IMG-1", [(snapshot, work, base)]), [], "already in place")


if __name__ == "__main__":
    unittest.main()
