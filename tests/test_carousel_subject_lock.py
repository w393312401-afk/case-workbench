"""Local Node/Sharp subject protection; no AI or gateway calls."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from PIL import Image, ImageChops

from carousel_subject_lock import (prepare_subject_canvas, build_subject_mask, compose_subject_locked,
                                  verify_subject_lock, create_subject_review_preview, _runtime, _digest, _run)
from codex_bridge import BridgeError


class SubjectLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="subject-lock-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "carousel"
        self.folder = self.root / "generations" / "IMG-test" / "01"
        self.folder.mkdir(parents=True)
        self.base = self.folder / "base.png"
        self.node, self.sharp = _runtime()
        self.fixture(self.base, 120, 160)

    def fixture(self, path, width, height, *, flat_top=0, solid=None):
        # Test images are also produced with Node, keeping Pillow strictly read-only.
        code = """const fs=require('fs'), sharp=require(process.argv[1]);
const p=JSON.parse(fs.readFileSync(0,'utf8')); const b=Buffer.alloc(p.width*p.height*3);
for(let y=0;y<p.height;y++) for(let x=0;x<p.width;x++) {
 const i=(y*p.width+x)*3; const c=p.solid || (y<p.flat_top ? [245,245,245] : [30+x%200,40+y%180,70+(x+y)%150]);
 b[i]=c[0]; b[i+1]=c[1]; b[i+2]=c[2]; }
sharp(b,{raw:{width:p.width,height:p.height,channels:3}}).png().toFile(p.path).catch(()=>process.exit(1));"""
        result = subprocess.run([str(self.node), "-e", code, str(self.sharp)], input=json.dumps({
            "path": str(path), "width": width, "height": height, "flat_top": flat_top, "solid": solid}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0)

    def prepared(self, aspect="3:4"):
        prepared = prepare_subject_canvas(self.base, self.folder, aspect)
        mask = build_subject_mask(self.folder, [[[200, 400], [800, 400], [800, 800], [200, 800]]])
        generated = self.folder / "generated.png"
        self.fixture(generated, prepared["width"], prepared["height"], solid=[10, 80, 220])
        report = self.folder / "coverage-report.json"
        report.write_text(json.dumps({"slides": [{"number": 1, "covered": True, "issues": []}]}), encoding="utf-8")
        coverage = {"status": "passed", "number": 1, "file": report.relative_to(self.root).as_posix(),
                    "sha256": _digest(report), "source_sha256": prepared["sha256"], "mask_sha256": mask["sha256"],
                    "preview_sha256": _digest(self.folder / mask["preview_file"])}
        snapshot = {"aspect": aspect, "base_sha256": _digest(self.base), "input_signature": "locked-input",
                    "slide": {"number": 1}, "subject_lock": {**prepared, "mask_file": mask["file"], "coverage": coverage}}
        return prepared, mask, generated, snapshot

    def composed(self):
        prepared, mask, generated, snapshot = self.prepared()
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        output = {"file": final.relative_to(self.root).as_posix(), "sha256": receipt["final_sha256"],
                  "input_signature": snapshot["input_signature"], "base_sha256": snapshot["base_sha256"],
                  "subject_preservation": receipt}
        return final, receipt, output

    def semantic_prepared(self, *, photo_frames=None, rectangles=None):
        prepared = prepare_subject_canvas(self.base, self.folder, "3:4")
        base_hash = _digest(self.base)
        cache = self.root / "subject-masks" / base_hash
        cache.mkdir(parents=True)
        semantic = cache / "semantic.png"
        # Two product cores are separated by an artificial photo gutter. The
        # intentionally excessive mask in that gutter must be geometrically cut.
        code = """const fs=require('fs'), sharp=require(process.argv[1]);
const p=JSON.parse(fs.readFileSync(0,'utf8')), b=Buffer.alloc(120*160*4);
for(let y=0;y<160;y++) for(let x=0;x<120;x++) {
 const at=(y*120+x)*4;
 const opaque=(x>=25&&x<75&&((y>=15&&y<55)||(y>=105&&y<145)))||(y>=75&&y<85);
 b[at]=b[at+1]=b[at+2]=255; b[at+3]=opaque?255:0; }
sharp(b,{raw:{width:120,height:160,channels:4}}).png().toFile(p.path).catch(()=>process.exit(1));"""
        result = subprocess.run([str(self.node), "-e", code, str(self.sharp)],
            input=json.dumps({"path": str(semantic)}).encode(), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        metadata = {"base_sha256": base_hash, "mask_file": semantic.name, "mask_sha256": _digest(semantic),
            "photo_rectangles": [{"left": 0, "top": 0, "width": 120, "height": 70},
                                 {"left": 0, "top": 90, "width": 120, "height": 70}]}
        if rectangles is not None:
            metadata["photo_rectangles"] = rectangles
        if photo_frames is not None:
            metadata["photo_frames"] = photo_frames
        (cache / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        snapshot = {"aspect": "3:4", "base_sha256": base_hash, "input_signature": "semantic-input",
            "requirements": {"render_contract": "segmented_background_v4", "caption_style": "rugged"},
            "slide": {"number": 1, "title_lines": ["Other side?", "Flip, then retighten."], "body": "Keep the same knife."},
            "subject_lock": prepared}
        (self.folder / "input.json").write_text(json.dumps(snapshot), encoding="utf-8")
        mask = build_subject_mask(self.folder, [[[0, 0], [1000, 0], [1000, 1000], [0, 1000]]])
        report = self.folder / "coverage-report.json"
        report.write_text(json.dumps({"slides": [{"number": 1, "covered": True, "issues": []}]}), encoding="utf-8")
        snapshot["subject_lock"].update(mask_file=mask["file"], coverage={"status": "passed", "number": 1,
            "file": report.relative_to(self.root).as_posix(), "sha256": _digest(report),
            "source_sha256": prepared["sha256"], "mask_sha256": mask["sha256"]})
        generated = self.folder / "generated.png"
        self.fixture(generated, prepared["width"], prepared["height"], solid=[10, 80, 220])
        return prepared, mask, generated, snapshot, metadata

    def test_photo_covers_the_full_width_below_the_title_strip_and_only_trims_flat_top_band(self):
        self.fixture(self.base, 120, 160, flat_top=24)
        prepared = prepare_subject_canvas(self.base, self.folder, "3:4")
        transform = prepared["transform"]
        self.assertEqual((prepared["width"], prepared["height"]), (1536, 2048))
        self.assertEqual((transform["trim_top"], transform["trim_height"]), (24, 136))
        # No side margins: the photo spans the canvas width (this tall-for-its-width frame loses a sliver of its sides).
        self.assertEqual((transform["placed_x"], transform["placed_width"]), (0, 1536))
        self.assertEqual((transform["placed_y"], transform["placed_height"]), (224, 2048 - 224))
        self.assertLessEqual(transform["crop_width"], 120)
        self.assertEqual((transform["crop_top"], transform["crop_height"]), (24, 136))
        self.assertAlmostEqual(transform["scale"] * transform["crop_width"], 1536, delta=1)
        self.assertAlmostEqual(transform["scale_y"] * transform["crop_height"], 2048 - 224, delta=1)
        with Image.open(self.folder / prepared["file"]) as image:
            self.assertEqual(image.getpixel((0, 0))[:3], (24, 27, 29), "the strip above the photo is plain dark")
            self.assertNotEqual(image.getpixel((0, 224))[:3], (24, 27, 29))
            self.assertNotEqual(image.getpixel((1535, 2047))[:3], (24, 27, 29), "the photo reaches both lower corners")
        self.fixture(self.base, 120, 160, solid=[200, 200, 200])
        solid = prepare_subject_canvas(self.base, self.folder, "9:16")
        self.assertEqual(solid["transform"]["trim_top"], 0, "A whole solid image is not a removable caption band.")
        self.assertEqual((solid["width"], solid["height"]), (1152, 2048))
        self.fixture(self.base, 120, 160)
        photo = prepare_subject_canvas(self.base, self.folder, "3:4")
        self.assertEqual(photo["transform"]["trim_top"], 0)

    def test_tall_photo_crops_only_its_vertical_ends_and_the_window_follows_the_hint(self):
        self.fixture(self.base, 108, 192)   # 9:16 photo on a 3:4 canvas
        first = prepare_subject_canvas(self.base, self.folder, "3:4")["transform"]
        self.assertEqual((first["crop_left"], first["crop_width"]), (0, 108), "the full width is always shown")
        self.assertLess(first["crop_height"], 192)
        self.assertEqual(first["crop_top"], 0, "without a hint the window starts at the top")
        moved = prepare_subject_canvas(self.base, self.folder, "3:4", {"crop_left": 0, "crop_top": 30})["transform"]
        self.assertEqual(moved["crop_top"], 30)
        clamped = prepare_subject_canvas(self.base, self.folder, "3:4", {"crop_top": 999})["transform"]
        self.assertEqual(clamped["crop_top"] + clamped["crop_height"], 192, "the window never leaves the photo")

    def test_moved_window_survives_compose_and_verification(self):
        self.fixture(self.base, 108, 192)
        prepared = prepare_subject_canvas(self.base, self.folder, "3:4", {"crop_top": 24})
        mask = build_subject_mask(self.folder, [[[200, 400], [800, 400], [800, 800], [200, 800]]])
        generated = self.folder / "generated.png"
        self.fixture(generated, prepared["width"], prepared["height"], solid=[10, 80, 220])
        report = self.folder / "coverage-report.json"
        report.write_text(json.dumps({"slides": [{"number": 1, "covered": True, "issues": []}]}), encoding="utf-8")
        coverage = {"status": "passed", "number": 1, "file": report.relative_to(self.root).as_posix(),
                    "sha256": _digest(report), "source_sha256": prepared["sha256"], "mask_sha256": mask["sha256"]}
        snapshot = {"aspect": "3:4", "base_sha256": _digest(self.base), "input_signature": "moved",
                    "slide": {"number": 1, "title_lines": ["A moved window"]},
                    "subject_lock": {**prepared, "mask_file": mask["file"], "coverage": coverage}}
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        output = {"file": final.relative_to(self.root).as_posix(), "sha256": receipt["final_sha256"],
                  "input_signature": "moved", "base_sha256": snapshot["base_sha256"], "subject_preservation": receipt}
        self.assertEqual(verify_subject_lock(self.root, output)["transform"]["crop_top"], 24)
        altered = copy.deepcopy(output)
        altered["subject_preservation"]["transform"]["crop_top"] = 0
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, altered)

    def test_full_resolution_review_jpeg_keeps_original_unchanged_and_rejects_bad_targets(self):
        prepared = prepare_subject_canvas(self.base, self.folder, "3:4")
        source = self.folder / prepared["file"]
        before = source.read_bytes()
        target = self.folder / "review.jpg"
        result = create_subject_review_preview(source, target)
        with Image.open(target) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (1536, 2048)))
            self.assertEqual(image.layer[0][1:3], (1, 1), "JPEG uses 4:4:4 chroma sampling.")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual((result["quality"], result["chroma_subsampling"]), (92, "4:4:4"))
        self.assertEqual(result["bytes"], target.stat().st_size)
        for invalid in (source, self.folder / "bad.png", Path("relative.jpg")):
            with self.assertRaises(BridgeError):
                create_subject_review_preview(source, invalid)

    def test_mask_alpha_has_protected_polygon_margin_and_inspectable_preview(self):
        prepare_subject_canvas(self.base, self.folder, "3:4")
        mask = build_subject_mask(self.folder, [[[300, 400], [700, 400], [700, 700], [300, 700]]])
        with Image.open(self.folder / mask["file"]) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((768, 1024))[3], 255)
            self.assertEqual(image.getpixel((0, 0))[3], 0)
            self.assertEqual(image.getpixel((453, 1024))[3], 255, "A small exterior margin protects approximate polygon edges.")
            self.assertEqual(image.getpixel((430, 1024))[3], 0, "Background beyond the 12 px margin remains editable.")
        self.assertTrue((self.folder / mask["preview_file"]).is_file())
        self.assertGreater(mask["protected_pixel_count"], 0)
        self.assertEqual(mask["outer_stroke_pixels"], 12)
        self.assertEqual(mask["mask_mode"], "individual_regions")
        self.assertLess(mask["protected_fraction"], .95)
        for regions in ([], [[[0, 0], [9999, 0], [0, 3]]]):
            with self.subTest(regions=regions), self.assertRaises(BridgeError):
                build_subject_mask(self.folder, regions)

    def test_mask_and_feather_never_restore_the_title_strip_even_when_regions_cross_it(self):
        prepared = prepare_subject_canvas(self.base, self.folder, "3:4")
        t = prepared["transform"]
        mask = build_subject_mask(self.folder, [[[0, 0], [1000, 0], [1000, 1000], [0, 1000]]])
        with Image.open(self.folder / mask["file"]) as image:
            alpha = image.getchannel("A")
            self.assertEqual(alpha.getpixel((0, t["placed_y"])), 255, "the photo reaches the left canvas edge")
            self.assertEqual(alpha.getpixel((1535, t["placed_y"])), 255, "and the right one")
            self.assertEqual(alpha.getpixel((768, t["placed_y"] - 1)), 0)
            self.assertEqual(alpha.getpixel((768, t["placed_y"])), 255)
            self.assertEqual(alpha.crop((0, 0, image.width, t["placed_y"])).getextrema(), (0, 0))
        generated, final = self.folder / "padding-test-generated.png", self.folder / "padding-test-final.png"
        self.fixture(generated, prepared["width"], prepared["height"], solid=[10, 80, 220])
        _run("compose", source=self.folder / prepared["file"], mask=self.folder / mask["file"],
             generated=generated, target=final)
        with Image.open(final) as image, Image.open(self.folder / prepared["file"]) as source:
            self.assertNotEqual(image.getpixel((768, t["placed_y"] - 1)), source.getpixel((768, t["placed_y"] - 1)),
                                "The artificial strip above the photo is never restored.")
            self.assertGreater(image.getpixel((768, 0))[2], 40, "The strip is the AI scene under a dark fade, not the flat slab.")
            self.assertEqual(image.getpixel((0, t["placed_y"] + 100)), source.getpixel((0, t["placed_y"] + 100)))
            self.assertEqual(image.getpixel((1535, t["placed_y"] + 100)), source.getpixel((1535, t["placed_y"] + 100)))
        # Dark photographed pixels also remain locked; clipping uses only geometry.
        self.fixture(self.base, 120, 160, solid=[24, 27, 29])
        prepare_subject_canvas(self.base, self.folder, "3:4")
        dark = build_subject_mask(self.folder, [[[0, 0], [1000, 0], [1000, 1000], [0, 1000]]])
        with Image.open(self.folder / dark["file"]) as image:
            self.assertEqual(image.getpixel((768, 1000))[3], 255)

    def test_source_bounds_reject_invalid_canvas_fractional_or_outside_geometry(self):
        prepare_subject_canvas(self.base, self.folder, "3:4")
        sidecar = self.folder / "subject-source.png.bounds.json"
        original = json.loads(sidecar.read_text())
        for changed in ({**original, "width": 1}, {**original, "placed_x": 1.5},
                        {**original, "placed_width": 99999}, {**original, "placed_y": -1},
                        {**original, "source_sha256": "other"}):
            sidecar.write_text(json.dumps(changed))
            with self.assertRaises(BridgeError):
                build_subject_mask(self.folder, [[[200, 400], [800, 400], [800, 800], [200, 800]]])
        sidecar.unlink()
        with self.assertRaises(BridgeError):
            build_subject_mask(self.folder, [[[200, 400], [800, 400], [800, 800], [200, 800]]])

    def test_individual_regions_leave_gaps_and_concavities_editable(self):
        prepare_subject_canvas(self.base, self.folder, "3:4")
        mask = build_subject_mask(self.folder, [
            [[200, 400], [350, 400], [350, 500], [250, 500], [250, 700], [200, 700]],
            [[650, 400], [800, 400], [800, 700], [650, 700]],
        ])
        self.assertEqual(mask["mask_mode"], "individual_regions")
        with Image.open(self.folder / mask["file"]) as image:
            self.assertEqual(image.getpixel((768, 1126))[3], 0, "Separated product outlines do not retain a hull of old background.")
            self.assertEqual(image.getpixel((460, 1228))[3], 0, "Concave cutouts stay open beyond the small margin.")
            self.assertEqual(image.getpixel((350, 1126))[3], 255, "The actual outlined part remains protected.")
            self.assertEqual(image.getpixel((150, 1126))[3], 0, "Outside background stays editable.")
            self.assertEqual(image.getpixel((768, 200))[3], 0, "The title strip stays editable.")

    def test_compose_restores_all_protected_pixels_and_receipt_can_verify_copied_output(self):
        final, receipt, output = self.composed()
        self.assertEqual((receipt["method"], receipt["status"], receipt["mismatch_pixels"]),
                         ("masked_source_pixels", "passed", 0))
        self.assertEqual(receipt["feather_pixels"], 2)
        self.assertEqual(receipt["render_contract"], "segmented_background_v4")
        self.assertEqual(receipt["mask_coverage"]["status"], "passed")
        self.assertEqual(receipt["mask_coverage"]["source_sha256"], receipt["source_sha256"])
        self.assertTrue((self.folder / "subject-lock-receipt.json").is_file())
        with Image.open(self.folder / "subject-source.png") as source, Image.open(final) as image:
            self.assertEqual(image.getpixel((768, 1100)), source.getpixel((768, 1100)))
            self.assertNotEqual(image.getpixel((0, 0)), source.getpixel((0, 0)))
            self.assertLess(sum(image.getpixel((0, 0))[:3]), sum((10, 80, 220)), "The title strip is the AI scene under a dark fade.")
        destination = self.root / "outputs" / "current.png"
        destination.parent.mkdir()
        shutil.copyfile(final, destination)
        output["file"] = destination.relative_to(self.root).as_posix()
        self.assertEqual(verify_subject_lock(self.root, output), receipt)
        sorted_output = json.loads(json.dumps(output, sort_keys=True))
        self.assertEqual(verify_subject_lock(self.root, sorted_output), receipt)

    def test_two_pixel_feather_blends_just_outside_core_without_wide_old_background_halo(self):
        final, receipt, _ = self.composed()
        with Image.open(self.folder / "subject-mask.png") as mask, \
                Image.open(self.folder / "subject-source.png") as source, Image.open(final) as image:
            edge = next(y for y in range(mask.height) if mask.getpixel((768, y))[3] == 255)
            point = (768, edge - 3)
            self.assertEqual(mask.getpixel(point)[3], 0)
            self.assertGreater(image.getpixel(point)[0], 10)
            self.assertLess(image.getpixel(point)[0], source.getpixel(point)[0])
            self.assertEqual(image.getpixel((768, edge - 15))[:3], (10, 80, 220))
            self.assertEqual(image.getpixel((768, 1100)), source.getpixel((768, 1100)))
        self.assertEqual(receipt["mismatch_pixels"], 0)
        self.assertEqual(receipt["feather_pixels"], 2)

    def test_semantic_masks_use_individual_photo_windows_preserve_cores_and_caption_only_title_strip(self):
        prepared, mask, generated, snapshot, metadata = self.semantic_prepared()
        self.assertEqual((mask["mask_mode"], mask["outer_stroke_pixels"]), ("refined_segmentation", 0))
        self.assertEqual(_digest(self.folder / "refined-subject-mask.png"), metadata["mask_sha256"])
        t = prepared["transform"]

        def point(x, y):
            return (round((x - t["crop_left"]) * t["scale"]), t["placed_y"] + round((y - t["crop_top"]) * t["scale_y"]))

        layout_path = self.folder / (mask["file"] + ".layout.json")
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        self.assertEqual((layout["source_sha256"], layout["mask_sha256"]), (prepared["sha256"], mask["sha256"]))
        self.assertEqual(len(layout["rectangles"]), 2)
        self.assertLess(layout["rectangles"][0]["top"] + layout["rectangles"][0]["height"], layout["rectangles"][1]["top"])
        with Image.open(self.folder / mask["file"]) as protection:
            for core in (point(50, 30), point(50, 120)):
                self.assertEqual(protection.getpixel(core)[3], 255)
            for editable in (point(100, 30), point(50, 80), (t["placed_x"] - 1, t["placed_y"] + 100), (768, 100)):
                self.assertEqual(protection.getpixel(editable)[3], 0, "Only product pixels within real photo windows are protected.")
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        output = {"file": final.relative_to(self.root).as_posix(), "sha256": receipt["final_sha256"],
                  "base_sha256": snapshot["base_sha256"], "input_signature": snapshot["input_signature"],
                  "subject_preservation": receipt}
        self.assertEqual(verify_subject_lock(self.root, output), receipt)
        self.assertEqual(receipt["layout_file"], layout_path.relative_to(self.root).as_posix())
        self.assertEqual(receipt["layout_sha256"], _digest(layout_path))
        self.assertEqual((receipt["feather_pixels"], receipt["mismatch_pixels"]), (2, 0))
        plain = self.folder / "without-caption.png"
        _run("compose", source=self.folder / prepared["file"], mask=self.folder / mask["file"], generated=generated, target=plain)
        with Image.open(final) as finished, Image.open(plain) as uncaptioned, \
                Image.open(self.folder / prepared["file"]) as source, Image.open(self.folder / mask["file"]) as protection:
            core = protection.getchannel("A").point(lambda alpha: 255 if alpha == 255 else 0)
            self.assertTrue(all(ImageChops.multiply(channel, core).getbbox() is None
                                for channel in ImageChops.difference(finished, source).split()))
            self.assertEqual(finished.getpixel(point(100, 30))[:3], (10, 80, 220), "Editable photo area uses the actual AI plate.")
            gutter = finished.getpixel(point(50, 80))
            self.assertNotEqual(gutter, source.getpixel(point(50, 80)), "Old artificial gutter must not be restored.")
            self.assertEqual(gutter, finished.getpixel((0, 500)), "Separate photo windows share the one AI plate; no blurred backdrop.")
            band = t["placed_y"]
            self.assertEqual(finished.crop((0, band, finished.width, finished.height)).tobytes(),
                             uncaptioned.crop((0, band, finished.width, finished.height)).tobytes(),
                             "The caption only touches the title strip above the photo.")
            self.assertNotEqual(finished.crop((0, 0, finished.width, band)).tobytes(),
                                uncaptioned.crop((0, 0, finished.width, band)).tobytes())
            self.assertIn((255, 245, 230, 255), finished.crop((0, 0, finished.width, band)).getdata())
            self.assertLess(sum(finished.getpixel((0, 0))[:3]), sum(finished.getpixel((0, band + 200))[:3]),
                            "The strip is darkest at the very top and the fade runs out into the photo.")

    def test_photo_layout_changes_or_missing_file_invalidate_the_preservation_receipt(self):
        _, mask, generated, snapshot, _ = self.semantic_prepared()
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        output = {"file": final.relative_to(self.root).as_posix(), "sha256": receipt["final_sha256"],
                  "base_sha256": snapshot["base_sha256"], "input_signature": snapshot["input_signature"],
                  "subject_preservation": receipt}
        layout_path = self.folder / (mask["file"] + ".layout.json")
        original = layout_path.read_bytes()
        layout = json.loads(original)
        self.assertEqual(receipt["layout_sha256"], _digest(layout_path))
        changed = copy.deepcopy(layout)
        changed["rectangles"][0]["height"] += 1
        layout_path.write_text(json.dumps(changed), encoding="utf-8")
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)
        layout_path.write_bytes(original)
        invalid = copy.deepcopy(output)
        invalid["subject_preservation"]["layout_file"] = "../outside.json"
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, invalid)
        changed = {**layout, "source_sha256": "changed"}
        layout_path.write_text(json.dumps(changed), encoding="utf-8")
        invalid = copy.deepcopy(output)
        invalid["subject_preservation"]["layout_sha256"] = _digest(layout_path)
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, invalid)
        layout_path.unlink()
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)

    def test_photo_keeps_its_new_background_right_up_to_the_title_band(self):
        prepared, _, generated, snapshot = self.prepared()
        t = prepared["transform"]
        band = t["placed_y"]
        # The outline crosses the photo's top edge: only its photographed part (below the strip) is locked.
        mask = build_subject_mask(self.folder, [[[400, 40], [600, 40], [600, 250], [400, 250]]])
        snapshot["subject_lock"]["coverage"]["mask_sha256"] = mask["sha256"]
        snapshot["slide"].update(title_lines=["Keep the original product"], body="")
        plain = self.folder / "clean-gutter-without-caption.png"
        _run("compose", source=self.folder / prepared["file"], mask=self.folder / mask["file"], generated=generated, target=plain)
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        with Image.open(final) as finished, Image.open(plain) as uncaptioned, \
                Image.open(self.folder / prepared["file"]) as source, Image.open(self.folder / mask["file"]) as protection:
            for y in (band - 1, band, band + 1, band + 40, band + 200):
                self.assertEqual(protection.getpixel((200, y))[3], 0)
            # No seam where the photo's top edge was: the plate runs through it and only the fade changes the tone.
            above, below = finished.getpixel((200, band - 1)), finished.getpixel((200, band))
            self.assertLessEqual(max(abs(a - b) for a, b in zip(above[:3], below[:3])), 4,
                                 "A step at the photo's top edge reads as a pasted photo on a banner.")
            self.assertGreater(finished.getpixel((200, band + 400))[2], finished.getpixel((200, band))[2],
                               "The fade lightens steadily down into the photo.")
            for y in (band, band + 40, band + 90):
                self.assertEqual(protection.getpixel((768, y))[3], 255)
                self.assertEqual(finished.getpixel((768, y)), source.getpixel((768, y)),
                                 "The original subject keeps its pixels right through the dark fade.")
            self.assertEqual(finished.crop((0, band, finished.width, finished.height)).tobytes(),
                             uncaptioned.crop((0, band, finished.width, finished.height)).tobytes())
            self.assertNotEqual(finished.crop((0, 0, finished.width, band)).tobytes(),
                                uncaptioned.crop((0, 0, finished.width, band)).tobytes())
        self.assertEqual(receipt["mismatch_pixels"], 0)

    def test_photo_frame_binding_keeps_only_its_photo_clear_and_white_edges_off_protected_pixels(self):
        frame = {"left": 30, "top": 10, "width": 80, "height": 130, "border": 2, "photo_index": 0}
        prepared, mask, generated, snapshot, metadata = self.semantic_prepared(photo_frames=[frame])
        t = prepared["transform"]

        def point(x, y):
            return (round((x - t["crop_left"]) * t["scale"]), t["placed_y"] + round((y - t["crop_top"]) * t["scale_y"]))

        layout_path = self.folder / (mask["file"] + ".layout.json")
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        self.assertEqual((layout["source_sha256"], layout["mask_sha256"]), (prepared["sha256"], mask["sha256"]))
        self.assertEqual(len(layout["frames"]), 1)
        mapped = layout["frames"][0]
        self.assertEqual((mapped["left"], mapped["top"]), point(frame["left"], frame["top"]))
        self.assertEqual((mapped["width"], mapped["height"], mapped["border"], mapped["photo_index"]),
                         (round(80 * t["scale"]), round(130 * t["scale_y"]), round(2 * t["scale"]), 0))
        refinement = json.loads((self.folder / "refinement.json").read_text(encoding="utf-8"))
        self.assertEqual(refinement["photo_frames"], [frame])
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        with Image.open(final) as finished, Image.open(self.folder / prepared["file"]) as source, \
                Image.open(self.folder / mask["file"]) as protection:
            clear, other_photo = point(100, 30), point(100, 120)
            self.assertEqual((protection.getpixel(clear)[3], protection.getpixel(other_photo)[3]), (0, 0))
            self.assertEqual(finished.getpixel(clear), (10, 80, 220, 255))
            self.assertEqual(finished.getpixel(other_photo), finished.getpixel(point(100, 80)),
                             "A different photo inside this frame is not framed; it shows the same plate.")
            white_edge, crossed_subject = point(31, 65), point(31, 30)
            self.assertEqual(protection.getpixel(white_edge)[3], 0)
            self.assertEqual(finished.getpixel(white_edge), (255, 255, 255, 255))
            self.assertEqual(protection.getpixel(crossed_subject)[3], 255)
            self.assertEqual(finished.getpixel(crossed_subject), source.getpixel(crossed_subject),
                             "The frame border never paints over the photographed product.")
            core = protection.getchannel("A").point(lambda alpha: 255 if alpha == 255 else 0)
            self.assertTrue(all(ImageChops.multiply(channel, core).getbbox() is None
                                for channel in ImageChops.difference(finished, source).split()))
        self.assertEqual(receipt["mismatch_pixels"], 0)
        self.assertEqual(receipt["layout_sha256"], _digest(layout_path))
        output = {"file": final.relative_to(self.root).as_posix(), "sha256": receipt["final_sha256"],
                  "base_sha256": snapshot["base_sha256"], "input_signature": snapshot["input_signature"],
                  "subject_preservation": receipt}
        self.assertEqual(verify_subject_lock(self.root, output), receipt)
        layout["frames"][0]["photo_index"] = 1
        layout_path.write_text(json.dumps(layout), encoding="utf-8")
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)
        metadata["photo_frames"][0]["photo_index"] = 2
        cache = self.root / "subject-masks" / snapshot["base_sha256"]
        (cache / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(BridgeError):
            build_subject_mask(self.folder, [[[0, 0], [1000, 0], [1000, 1000], [0, 1000]]])

    def thin_gap_layout(self):
        # Two photographs 2 base pixels apart: about 25 canvas pixels, a separator rather than open background.
        return [{"left": 0, "top": 0, "width": 120, "height": 79}, {"left": 0, "top": 81, "width": 120, "height": 79}]

    def test_a_thin_separator_between_stacked_photos_keeps_the_layouts_own_pixels(self):
        prepared, mask, generated, snapshot, _ = self.semantic_prepared(rectangles=self.thin_gap_layout())
        t = prepared["transform"]
        final, receipt = compose_subject_locked(self.base, self.folder, generated, snapshot)
        gap_y = t["placed_y"] + round(80 * t["scale_y"])
        with Image.open(final) as finished, Image.open(self.folder / prepared["file"]) as source:
            self.assertEqual(finished.getpixel((10, gap_y)), source.getpixel((10, gap_y)), "the separator is the layout's own")
            self.assertEqual(finished.getpixel((10, gap_y - 200)), (10, 80, 220, 255), "photos themselves show the plate")
            self.assertEqual(finished.getpixel((10, gap_y + 200)), (10, 80, 220, 255))
        self.assertEqual(receipt["mismatch_pixels"], 0)

    def test_a_plate_seam_the_model_drew_off_position_is_moved_onto_the_separator(self):
        prepared, mask, generated, snapshot, _ = self.semantic_prepared(rectangles=self.thin_gap_layout())
        t = prepared["transform"]
        gap_y = t["placed_y"] + round(80 * t["scale_y"])
        drifted = gap_y - 90
        upper, lower = (210, 60, 40), (40, 160, 90)
        plate = Image.new("RGB", (prepared["width"], prepared["height"]), lower)
        plate.paste(upper, (0, 0, prepared["width"], drifted))
        plate.save(generated)
        final, _ = compose_subject_locked(self.base, self.folder, generated, snapshot)
        near = lambda a, b: all(abs(x - y) <= 12 for x, y in zip(a[:3], b))
        with Image.open(final) as finished:
            # Rows just above the separator were the lower scene on the drifted plate (the seam sat 90 px higher).
            self.assertTrue(near(finished.getpixel((10, gap_y - 74)), upper), "above the separator is the upper scene")
            self.assertTrue(near(finished.getpixel((10, gap_y + 80)), lower), "below it is the lower scene")
        # A plate with no seam anywhere is left exactly alone.
        Image.new("RGB", (prepared["width"], prepared["height"]), (10, 80, 220)).save(generated)
        calm, _ = compose_subject_locked(self.base, self.folder, generated, snapshot)
        with Image.open(calm) as finished:
            self.assertEqual(finished.getpixel((10, gap_y - 200)), (10, 80, 220, 255))

    def test_a_frame_cut_by_the_crop_is_dropped_rather_than_painted_onto_the_canvas_edge(self):
        cut = {"left": 30, "top": 10, "width": 80, "height": 140, "border": 2, "photo_index": 0}   # runs past the visible rows
        _, mask, _, _, _ = self.semantic_prepared(photo_frames=[cut])
        layout = json.loads((self.folder / (mask["file"] + ".layout.json")).read_text(encoding="utf-8"))
        self.assertEqual(layout["frames"], [])

    def test_forged_hashes_cannot_hide_changed_protected_pixels_or_source(self):
        final, receipt, output = self.composed()
        self.fixture(final, receipt["width"], receipt["height"], solid=[0, 255, 0])
        receipt["final_sha256"] = output["sha256"] = _digest(final)
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)
        source = self.root / receipt["source_file"]
        self.fixture(source, receipt["width"], receipt["height"], solid=[0, 255, 0])
        receipt["source_sha256"] = _digest(source)
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)

    def test_binding_path_mask_and_coverage_changes_fail_verification(self):
        _, receipt, output = self.composed()
        for changed in ({**output, "input_signature": "other"}, {**output, "base_sha256": "other"},
                        {**output, "file": "../outside.png"}):
            with self.subTest(changed=changed), self.assertRaises(BridgeError):
                verify_subject_lock(self.root, changed)
        altered = copy.deepcopy(output)
        altered["subject_preservation"]["mask_coverage"]["status"] = "failed"
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, altered)
        altered = copy.deepcopy(output)
        altered["subject_preservation"]["transform"]["placed_x"] += 1
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, altered)
        (self.root / receipt["mask_file"]).write_bytes(b"changed")
        with self.assertRaises(BridgeError):
            verify_subject_lock(self.root, output)

    def test_generation_with_wrong_aspect_is_not_stretched_or_receipted(self):
        _, _, generated, snapshot = self.prepared()
        self.fixture(generated, 800, 800, solid=[0, 0, 255])
        with self.assertRaisesRegex(BridgeError, "比例"):
            compose_subject_locked(self.base, self.folder, generated, snapshot)
        self.assertFalse((self.folder / "subject-lock-receipt.json").exists())

    def test_coverage_proof_is_mandatory_and_bound_to_source_mask_and_positive_report(self):
        _, receipt, output = self.composed()
        variants = []
        missing = copy.deepcopy(output)
        missing["subject_preservation"].pop("mask_coverage")
        variants.append(missing)
        for field in ("source_sha256", "mask_sha256", "sha256"):
            changed = copy.deepcopy(output)
            changed["subject_preservation"]["mask_coverage"][field] = "other"
            variants.append(changed)
        escaped = copy.deepcopy(output)
        escaped["subject_preservation"]["mask_coverage"]["file"] = "../outside.json"
        variants.append(escaped)
        for changed in variants:
            with self.assertRaises(BridgeError):
                verify_subject_lock(self.root, changed)
        report = self.root / receipt["mask_coverage"]["file"]
        for entry in ({"number": 1, "covered": False, "issues": []},
                      {"number": 1, "covered": True, "issues": ["Finger missing"]},
                      {"number": 2, "covered": True, "issues": []}):
            report.write_text(json.dumps({"slides": [entry]}), encoding="utf-8")
            receipt["mask_coverage"]["sha256"] = _digest(report)
            with self.assertRaises(BridgeError):
                verify_subject_lock(self.root, output)


if __name__ == "__main__":
    unittest.main()
