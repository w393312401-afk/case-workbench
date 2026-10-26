"""Read-only pixel checks for the independent Node/Sharp caption module."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
from PIL import Image

from carousel_subject_lock import _runtime


class SubjectCaptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="subject-caption-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.node, self.sharp = _runtime()
        self.module = Path(__file__).resolve().parents[1] / "scripts" / "carousel_subject_caption.cjs"

    def render(self, titles, body="", width=1152, style="rugged"):
        # All raster creation/editing stays in Node. Pillow only reads the result.
        code = r"""const fs=require('fs'), sharp=require(process.argv[1]);
const {caption}=require(process.argv[2]); const p=JSON.parse(fs.readFileSync(0,'utf8'));
(async()=>{const h=560, b=Buffer.alloc(p.width*h*4);
for(let y=0;y<h;y++) for(let x=0;x<p.width;x++) {
 const i=(y*p.width+x)*4; b[i]=(x*3+y)%256; b[i+1]=(x+y*7)%256;
 b[i+2]=(x*5+y*11)%256; b[i+3]=y<320?255:128+(x+y)%128; }
const source=await sharp(b,{raw:{width:p.width,height:h,channels:4}}).png().toBuffer();
fs.writeFileSync(p.source,source); fs.writeFileSync(p.output,await caption(sharp,source,p.width,h,
 {title_lines:p.titles,body:p.body,...(p.style == null ? {} : {caption_style:p.style})}));
})().catch(error=>{console.error(error);process.exit(1)});"""
        source, output = self.root / "source.png", self.root / "caption.png"
        result = subprocess.run([str(self.node), "-e", code, str(self.sharp), str(self.module)],
            input=json.dumps({"source": str(source), "output": str(output), "width": width,
                              "titles": titles, "body": body, "style": style}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return source, output

    BAND = 224

    def test_caption_inks_only_the_title_strip_and_preserves_every_lower_pixel_including_alpha(self):
        source, output = self.render(["Other side?", "Flip, then retighten."], "Keep the same knife.")
        with Image.open(source) as original, Image.open(output) as finished:
            self.assertEqual(finished.size, original.size)
            self.assertEqual(finished.format, "PNG")
            self.assertEqual(finished.crop((0, self.BAND, 1152, 560)).tobytes(),
                             original.crop((0, self.BAND, 1152, 560)).tobytes())
            self.assertNotEqual(finished.crop((0, 0, 1152, self.BAND)).tobytes(),
                                original.crop((0, 0, 1152, self.BAND)).tobytes())
            colors = set(finished.crop((0, 0, 1152, self.BAND)).getdata())
            self.assertIn((255, 245, 230, 255), colors)
            self.assertIn((217, 154, 108, 255), colors)
            # Ink goes on top of whatever the compositor already put in the strip; it never repaints a flat slab.
            self.assertEqual(finished.getpixel((0, 0)), original.getpixel((0, 0)))
            self.assertEqual(finished.getpixel((1151, self.BAND - 1)), original.getpixel((1151, self.BAND - 1)))

    def test_long_exact_two_lines_fit_inside_the_strip_and_markup_is_literal(self):
        titles = ["<Sharp & steady> " + "regular knives, " * 8,
                  '"Keep every part" ' + "flip, then retighten; " * 7]
        _, output = self.render(titles)
        with Image.open(output) as finished:
            strip = finished.crop((0, 0, 1152, self.BAND)).convert("RGB")
            # Test ink extent independently of layout internals. Neither line
            # touches an edge, and long copy stays on exactly two visual lines.
            ink_points = [(x, y) for y in range(self.BAND) for x in range(1152)
                          if strip.getpixel((x, y)) in {(255, 245, 230), (217, 154, 108)}]
            self.assertTrue(ink_points)
            bounds = (min(x for x, _ in ink_points), min(y for _, y in ink_points),
                      max(x for x, _ in ink_points) + 1, max(y for _, y in ink_points) + 1)
            self.assertGreater(bounds[0], 40)
            self.assertLess(bounds[2], 1112)
            self.assertGreater(bounds[1], 12)
            self.assertLess(bounds[3], self.BAND - 12)
            active_rows = [any(strip.getpixel((x, y)) == (255, 245, 230)
                               for x in range(1152)) for y in range(self.BAND)]
            bands = sum(active and (y == 0 or not active_rows[y - 1])
                        for y, active in enumerate(active_rows))
            self.assertEqual(bands, 2, "Each supplied line stays intact without wrapping or clipping.")

    def test_a_custom_strip_height_moves_the_limit(self):
        code = r"""const fs=require('fs'), sharp=require(process.argv[1]);
const {caption}=require(process.argv[2]);
(async()=>{const w=800,h=900,b=Buffer.alloc(w*h*4,60); for(let i=3;i<b.length;i+=4) b[i]=255;
const source=await sharp(b,{raw:{width:w,height:h,channels:4}}).png().toBuffer();
fs.writeFileSync(process.argv[3], await caption(sharp,source,w,h,{title_lines:['Short line'],band:300}));})().catch(e=>{console.error(e);process.exit(1)});"""
        output = self.root / "band.png"
        result = subprocess.run([str(self.node), "-e", code, str(self.sharp), str(self.module), str(output)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, shell=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        with Image.open(output) as image:
            below = image.crop((0, 300, 800, 900)).convert("RGB")
            self.assertEqual(set(below.getdata()), {(60, 60, 60)}, "Nothing is drawn below the strip.")
            self.assertIn((255, 224, 68), set(image.crop((0, 0, 800, 300)).convert("RGB").getdata()))

    def test_four_styles_have_their_own_colors_and_preserve_every_pixel_below_the_existing_strip(self):
        from carousel_layout import CAPTION_PRESETS

        outputs = []
        for style in ("yellow_impact", "orange_depth", "red_comic", "lime_pop"):
            with self.subTest(style=style):
                source, output = self.render(["A HOLDER ISN'T", "THE WHOLE PATH"], style=style)
                with Image.open(source) as original, Image.open(output) as finished:
                    before, after = np.asarray(original), np.asarray(finished)
                    self.assertTrue(np.array_equal(before[self.BAND:], after[self.BAND:]),
                                    "The photographed area, including alpha, must stay byte-for-byte intact.")
                    changed = np.any(before[:self.BAND] != after[:self.BAND], axis=2)
                    ys, xs = np.nonzero(changed)
                    self.assertGreater(len(xs), 1000)
                    self.assertGreater(xs.min(), 40)
                    self.assertLess(xs.max(), 1112)
                    self.assertGreater(ys.min(), 12)
                    self.assertLess(ys.max(), self.BAND - 12)
                    active = np.any(changed, axis=1)
                    self.assertEqual(int(np.count_nonzero(active & ~np.r_[False, active[:-1]])), 2,
                                     "The two exact supplied title lines must not wrap or merge.")
                    colors = {tuple(pixel) for pixel in after[:self.BAND][changed]}
                    preset = CAPTION_PRESETS[style]
                    for role in ("title_color", "emphasis_color", "depth_color"):
                        self.assertIn(preset[role], colors, f"The visible {role} must match the Python preset.")
                    self.assertIn((5, 8, 10, 255), colors, "The title needs an opaque black outline.")
                    outputs.append(after[:self.BAND].tobytes())
        self.assertEqual(len(set(outputs)), 4, "Changing the style must change the final image.")

    def test_default_is_yellow_but_explicit_legacy_choices_keep_their_existing_colors(self):
        title = ["Same copy.", "Same punctuation!"]
        _, output = self.render(title, style=None)
        default = output.read_bytes()
        _, output = self.render(title, style="yellow_impact")
        self.assertEqual(output.read_bytes(), default)
        for style in ("plain", "rugged"):
            with self.subTest(style=style):
                source, output = self.render(title, style=style)
                with Image.open(source) as original, Image.open(output) as finished:
                    self.assertEqual(original.crop((0, self.BAND, 1152, 560)).tobytes(),
                                     finished.crop((0, self.BAND, 1152, 560)).tobytes())
                    colors = {tuple(pixel) for row in np.asarray(finished)[:self.BAND] for pixel in row}
                    self.assertIn((255, 245, 230, 255), colors)
                    self.assertIn((217, 154, 108, 255), colors)
                self.assertNotEqual(output.read_bytes(), default)

    def test_new_style_keeps_literal_markup_and_english_apostrophes(self):
        source, output = self.render(["<HOLDER> & CASE", "ISN'T THE SAME!"], style="red_comic")
        with Image.open(source) as original, Image.open(output) as finished:
            self.assertEqual(original.crop((0, self.BAND, 1152, 560)).tobytes(),
                             finished.crop((0, self.BAND, 1152, 560)).tobytes())
            exact = finished.tobytes()
        _, changed = self.render(["HOLDER & CASE", "ISNT THE SAME!"], style="red_comic")
        with Image.open(changed) as finished:
            self.assertNotEqual(finished.tobytes(), exact, "Literal punctuation must remain visible in the lettering.")


if __name__ == "__main__":
    unittest.main()
