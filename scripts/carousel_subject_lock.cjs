'use strict';
// Sharp owns all raster edits; Python only prepares paths, hashes and receipts.
const fs = require('fs');
const crypto = require('crypto');
const sharp = require(process.argv[2]);
const DARK = {r: 24, g: 27, b: 29, alpha: 1};
const SIZES = {'3:4': [1536, 2048], '9:16': [1152, 2048]};
// The photograph fills the whole canvas width. Its top edge sits TITLE_BAND below the canvas top; the strip above is
// the title area: no photographed pixels live there, so the title and its dark fade never touch a protected pixel.
const TITLE_BAND = 224;
// The dark fade that carries the title reaches this far below the strip, over unprotected pixels only.
const TITLE_FADE = 150;
const TITLE_DARKNESS = .88;
// Photos stacked in one base are told apart by a thin separator the layout step drew. It is at most this wide;
// it keeps its own pixels, so two photographs still read as two photographs instead of one cut scene.
const MAX_SEPARATOR = 48;
// How far from a separator the plate's own seam is looked for when the image model drifted off it.
const SEAM_SEARCH = 160;
// Vision-drawn polygons are approximate: preserve a generous exterior ring so
// knife edges, small knobs and narrow gaps cannot fall outside the source lock.
const MASK_MARGIN = 12;
const FEATHER_PIXELS = 2;
const fail = code => { const error = new Error(code); error.safeCode = code; throw error; };
const rawImage = path => sharp(path, {limitInputPixels: 100000000}).rotate().toColourspace('srgb').ensureAlpha().raw().toBuffer({resolveWithObject: true});

function flatTop(data, info) {
  // Only remove a genuinely flat color band. A photograph with texture is retained.
  const {width, height, channels} = info;
  const color = Array.from(data.subarray(0, 3));
  let rows = 0;
  for (let y = 0; y < Math.ceil(height * .35); y++) {
    let flat = true;
    for (let x = 0; x < width; x++) {
      const at = (y * width + x) * channels;
      if (Math.abs(data[at] - color[0]) > 3 || Math.abs(data[at + 1] - color[1]) > 3 ||
          Math.abs(data[at + 2] - color[2]) > 3 || data[at + 3] !== 255) { flat = false; break; }
    }
    if (!flat) break;
    rows++;
  }
  return rows >= Math.ceil(height * .05) && rows < height * .35 ? rows : 0;
}

const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

async function prepared(base, aspect, hint = {}) {
  if (!SIZES[aspect]) fail('invalid_aspect');
  const [width, height] = SIZES[aspect];
  const original = await rawImage(base);
  const trimTop = flatTop(original.data, original.info);
  const sourceWidth = original.info.width, sourceHeight = original.info.height;
  const keptHeight = sourceHeight - trimTop;
  const areaHeight = height - TITLE_BAND;
  // Cover, never contain: the photo spans the full width and only its top or bottom (or, for a very wide frame,
  // its sides) is cropped. The window moves along the one axis that has slack; hints come from the fine mask.
  const scale = Math.max(width / sourceWidth, areaHeight / keptHeight);
  const cropWidth = Math.min(sourceWidth, Math.round(width / scale));
  const cropHeight = Math.min(keptHeight, Math.round(areaHeight / scale));
  const slackX = sourceWidth - cropWidth, slackY = keptHeight - cropHeight;
  const cropLeft = clamp(Number.isFinite(hint.crop_left) ? Math.round(hint.crop_left) : Math.floor(slackX / 2), 0, slackX);
  const cropTop = trimTop + clamp(Number.isFinite(hint.crop_top) ? Math.round(hint.crop_top) - trimTop : 0, 0, slackY);
  const photo = await sharp(original.data, {raw: original.info})
    .extract({left: cropLeft, top: cropTop, width: cropWidth, height: cropHeight})
    .resize(width, areaHeight, {fit: 'fill', kernel: 'lanczos3'}).png().toBuffer();
  const result = await sharp({create: {width, height, channels: 4, background: DARK}})
    .composite([{input: photo, left: 0, top: TITLE_BAND}]).png().toBuffer();
  return {buffer: result, transform: {version: 2, aspect, source_width: sourceWidth,
    source_height: sourceHeight, trim_top: trimTop, trim_height: keptHeight, crop_left: cropLeft, crop_top: cropTop,
    crop_width: cropWidth, crop_height: cropHeight, scale: width / cropWidth, scale_y: areaHeight / cropHeight,
    placed_x: 0, placed_y: TITLE_BAND, placed_width: width, placed_height: areaHeight,
    width, height, header_height: TITLE_BAND, fit: 'cover_below_title_band', auto_orientation: true}};
}

async function prepare(args) {
  const source = await prepared(args.base, args.aspect, {crop_left: args.crop_left, crop_top: args.crop_top});
  await sharp(source.buffer).png().toFile(args.target);
  const t = source.transform;
  fs.writeFileSync(args.target + '.bounds.json', JSON.stringify({version: 1,
    source_sha256: crypto.createHash('sha256').update(fs.readFileSync(args.target)).digest('hex'),
    width: t.width, height: t.height, placed_x: t.placed_x, placed_y: t.placed_y,
    placed_width: t.placed_width, placed_height: t.placed_height}));
  return {width: source.transform.width, height: source.transform.height, transform: source.transform};
}

function sourceBounds(source, width, height) {
  let bounds;
  try {
    const path = source + '.bounds.json';
    if (fs.statSync(path).size > 8192) fail('invalid_source_bounds');
    bounds = JSON.parse(fs.readFileSync(path, 'utf8'));
  } catch (_) { fail('invalid_source_bounds'); }
  if (!bounds || bounds.version !== 1 ||
      ['width', 'height', 'placed_x', 'placed_y', 'placed_width', 'placed_height'].some(key => !Number.isSafeInteger(bounds[key])) ||
      bounds.width !== width || bounds.height !== height || bounds.placed_x < 0 || bounds.placed_y < 0 ||
      bounds.placed_width <= 0 || bounds.placed_height <= 0 ||
      bounds.placed_x + bounds.placed_width > width || bounds.placed_y + bounds.placed_height > height ||
      bounds.source_sha256 !== crypto.createHash('sha256').update(fs.readFileSync(source)).digest('hex')) fail('invalid_source_bounds');
  return bounds;
}

const insideSource = (x, y, bounds) => x >= bounds.placed_x && y >= bounds.placed_y &&
  x < bounds.placed_x + bounds.placed_width && y < bounds.placed_y + bounds.placed_height;

function convexHull(points) {
  const unique = [...new Map(points.map(point => [point.join(','), point])).values()]
    .sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (unique.length < 3) fail('invalid_regions');
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const point of unique) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], point) <= 0) lower.pop();
    lower.push(point);
  }
  for (const point of [...unique].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], point) <= 0) upper.pop();
    upper.push(point);
  }
  const hull = lower.slice(0, -1).concat(upper.slice(0, -1));
  if (hull.length < 3) fail('invalid_regions');
  return hull;
}

async function mask(args) {
  const source = await rawImage(args.source);
  const {width, height} = source.info;
  const bounds = sourceBounds(args.source, width, height);
  if (!Array.isArray(args.regions) || !args.regions.length || args.regions.length > 80) fail('invalid_regions');
  const points = args.regions.flatMap(region => {
    if (!Array.isArray(region) || region.length < 3 || region.length > 160) fail('invalid_regions');
    return region.map(point => {
      if (!Array.isArray(point) || point.length !== 2 || point.some(v => !Number.isFinite(v) || v < 0 || v > 1000)) fail('invalid_regions');
      return point;
    });
  });
  // Keep disconnected outlines separate: a global hull also retains huge pieces
  // of the old environment and creates visible geometric paste boundaries.
  const hull = convexHull(points);
  const polygon = args.regions.map(region => `<polygon points="${region.map(point => `${point[0] * (width - 1) / 1000},${point[1] * (height - 1) / 1000}`).join(' ')}" fill="white" stroke="white" stroke-width="${MASK_MARGIN * 2}" stroke-linejoin="round"/>`).join('');
  const svg = Buffer.from(`<svg width="${width}" height="${height}" xmlns="http://www.w3.org/2000/svg">${polygon}</svg>`);
  let alpha = await sharp(svg).ensureAlpha().extractChannel(3).raw().toBuffer();
  let rectangles = [{left: bounds.placed_x, top: bounds.placed_y, width: bounds.placed_width, height: bounds.placed_height}];
  let frames = [];
  if (args.semantic_path) {
    const t = args.transform, semantic = await rawImage(args.semantic_path);
    if (!t || t.version !== 2 || semantic.info.width !== t.source_width || semantic.info.height !== t.source_height ||
        t.width !== width || t.height !== height) fail('invalid_semantic_mask');
    // Only the visible window of the photo is mapped; whatever the crop removed simply is not on the canvas.
    const patch = await sharp(semantic.data, {raw: semantic.info})
      .extract({left: t.crop_left, top: t.crop_top, width: t.crop_width, height: t.crop_height})
      .resize(t.placed_width, t.placed_height, {kernel: 'nearest'}).png().toBuffer();
    alpha = await sharp({create: {width, height, channels: 4, background: {r: 0, g: 0, b: 0, alpha: 0}}})
      .composite([{input: patch, left: t.placed_x, top: t.placed_y}]).extractChannel(3).raw().toBuffer();
    if (!Array.isArray(args.photo_rectangles) || !args.photo_rectangles.length) fail('invalid_semantic_mask');
    const toCanvasX = x => t.placed_x + Math.round((x - t.crop_left) * t.scale);
    const toCanvasY = y => t.placed_y + Math.round((y - t.crop_top) * t.scale_y);
    const clipX = x => clamp(x, t.placed_x, t.placed_x + t.placed_width), clipY = y => clamp(y, t.placed_y, t.placed_y + t.placed_height);
    const kept = new Map();   // index in the base's own panel list -> index among the windows still on the canvas
    rectangles = [];
    args.photo_rectangles.forEach((rect, index) => {
      if (!rect || ['left', 'top', 'width', 'height'].some(key => !Number.isSafeInteger(rect[key])) ||
          rect.left < 0 || rect.top < t.trim_top || rect.width < 1 || rect.height < 1 ||
          rect.left + rect.width > t.source_width || rect.top + rect.height > t.source_height) fail('invalid_semantic_mask');
      const left = clipX(toCanvasX(rect.left)), top = clipY(toCanvasY(rect.top));
      const right = clipX(toCanvasX(rect.left + rect.width)), bottom = clipY(toCanvasY(rect.top + rect.height));
      if (right - left < 1 || bottom - top < 1) return;
      kept.set(index, rectangles.length);
      rectangles.push({left, top, width: right - left, height: bottom - top});
    });
    if (!rectangles.length) fail('invalid_semantic_mask');
    // A frame is kept only whole: one the crop cuts would paint a white border onto the canvas edge.
    frames = (args.photo_frames || []).flatMap(frame => {
      const {left, top, width: fw, height: fh, border} = frame;
      if ([left, top, fw, fh, border].some(v => !Number.isSafeInteger(v)) || left < 0 || top < t.trim_top ||
          fw < 1 || fh < 1 || border < 1 || border * 2 >= Math.min(fw, fh) || left + fw > t.source_width || top + fh > t.source_height ||
          !Number.isSafeInteger(frame.photo_index) || frame.photo_index < 0 || frame.photo_index >= args.photo_rectangles.length) fail('invalid_semantic_mask');
      const inside = left >= t.crop_left && top >= t.crop_top && left + fw <= t.crop_left + t.crop_width && top + fh <= t.crop_top + t.crop_height;
      if (!inside || !kept.has(frame.photo_index)) return [];
      return [{left: toCanvasX(left), top: toCanvasY(top), width: Math.round(fw * t.scale), height: Math.round(fh * t.scale_y),
        border: Math.max(1, Math.round(border * t.scale)), photo_index: kept.get(frame.photo_index)}];
    });
  }
  let count = 0;
  const rgba = Buffer.alloc(width * height * 4);
  const preview = Buffer.from(source.data);
  for (let pixel = 0; pixel < alpha.length; pixel++) {
    // Geometric clipping removes only artificial canvas padding. Never segment
    // by color: genuine black product/background pixels inside the photo remain.
    const x = pixel % width, y = Math.floor(pixel / width);
    if (!insideSource(x, y, bounds) || (args.semantic_path && !rectangles.some(r =>
      x >= r.left && y >= r.top && x < r.left + r.width && y < r.top + r.height))) alpha[pixel] = 0;
    const a = alpha[pixel];
    rgba[pixel * 4 + 3] = a;
    if (a === 255) count++;
    if (a) {
      const opacity = .38 * a / 255;
      preview[pixel * 4] = Math.round(preview[pixel * 4] * (1 - opacity) + 255 * opacity);
      preview[pixel * 4 + 1] = Math.round(preview[pixel * 4 + 1] * (1 - opacity));
      preview[pixel * 4 + 2] = Math.round(preview[pixel * 4 + 2] * (1 - opacity));
    }
  }
  if (!count || count > width * height * .95) fail('invalid_mask_coverage');
  await sharp(rgba, {raw: {width, height, channels: 4}}).png().toFile(args.target);
  await sharp(preview, {raw: source.info}).png().toFile(args.preview);
  fs.writeFileSync(args.target + '.layout.json', JSON.stringify({source_sha256: bounds.source_sha256,
    mask_sha256: crypto.createHash('sha256').update(fs.readFileSync(args.target)).digest('hex'), rectangles, frames}));
  return {width, height, protected_pixel_count: count, protected_fraction: count / (width * height),
    outer_stroke_pixels: args.semantic_path ? 0 : MASK_MARGIN, mask_mode: args.semantic_path ? 'refined_segmentation' : 'individual_regions', source_bounds: bounds};
}

async function readMask(path, width, height) {
  const metadata = await sharp(path).metadata();
  if (metadata.format !== 'png' || metadata.channels !== 4 || metadata.width !== width || metadata.height !== height) fail('invalid_mask');
  const alpha = await sharp(path).extractChannel(3).raw().toBuffer();
  let count = 0;
  for (const value of alpha) if (value === 255) count++;
  if (!count || count > width * height * .95 || !alpha.includes(0)) fail('invalid_mask_coverage');
  return {alpha, count};
}

function countMismatches(left, right, alpha) {
  let mismatches = 0;
  for (let pixel = 0; pixel < alpha.length; pixel++) {
    if (alpha[pixel] !== 255) continue;
    const at = pixel * 4;
    if (left[at] !== right[at] || left[at + 1] !== right[at + 1] || left[at + 2] !== right[at + 2] || left[at + 3] !== right[at + 3]) mismatches++;
  }
  return mismatches;
}

// Windows stacked with only a thin gap between them, as {top, bottom, left, right} pairs of the gap itself.
function separatorsOf(rectangles) {
  const found = [];
  for (const a of rectangles) for (const b of rectangles) {
    const gap = b.top - (a.top + a.height);
    const overlap = Math.min(a.left + a.width, b.left + b.width) - Math.max(a.left, b.left);
    if (gap >= 1 && gap <= MAX_SEPARATOR && overlap > Math.min(a.width, b.width) * .5 &&
        !found.some(f => f.top === a.top + a.height && f.bottom === b.top)) {
      found.push({top: a.top + a.height, bottom: b.top, left: Math.max(a.left, b.left), right: Math.min(a.left + a.width, b.left + b.width)});
    }
  }
  return found.sort((x, y) => x.top - y.top);
}

// The plate is drawn by an image model that keeps panel positions only roughly. Where the plate has its own seam
// (a full-width jump between two scenes) near a separator, stretch the plate vertically so the two coincide.
function alignPlateSeams(plate, width, height, separators) {
  const luma = y => {
    const row = new Float32Array(width);
    for (let x = 0; x < width; x++) { const at = (y * width + x) * 4; row[x] = .299 * plate[at] + .587 * plate[at + 1] + .114 * plate[at + 2]; }
    return row;
  };
  const anchors = [[0, 0]];
  for (const separator of separators) {
    const expected = Math.round((separator.top + separator.bottom) / 2);
    const from = Math.max(anchors[anchors.length - 1][0] + 40, expected - SEAM_SEARCH), to = Math.min(height - 41, expected + SEAM_SEARCH);
    if (to <= from) continue;
    const jumps = [];
    let previous = luma(from - 1);
    for (let y = from; y <= to; y++) {
      const row = luma(y);
      let sum = 0;
      for (let x = separator.left; x < separator.right; x++) sum += Math.abs(row[x] - previous[x]);
      jumps.push(sum / Math.max(1, separator.right - separator.left));
      previous = row;
    }
    const sorted = [...jumps].sort((a, b) => a - b), median = sorted[Math.floor(sorted.length / 2)];
    const peak = jumps.indexOf(Math.max(...jumps)), strength = jumps[peak];
    // A real seam stands far above the plank edges around it; anything else is left alone.
    if (strength < Math.max(8, median * 3)) continue;
    anchors.push([expected, from + peak]);
  }
  anchors.push([height - 1, height - 1]);
  if (anchors.length === 2 || anchors.every(([out, src]) => out === src)) return plate;
  const result = Buffer.alloc(plate.length);
  let segment = 0;
  for (let y = 0; y < height; y++) {
    while (segment < anchors.length - 2 && y > anchors[segment + 1][0]) segment++;
    const [out0, src0] = anchors[segment], [out1, src1] = anchors[segment + 1];
    const position = clamp(src0 + (y - out0) * (src1 - src0) / Math.max(1, out1 - out0), 0, height - 1);
    const low = Math.floor(position), high = Math.min(height - 1, low + 1), t = position - low;
    for (let x = 0; x < width * 4; x++) result[y * width * 4 + x] = Math.round(plate[low * width * 4 + x] * (1 - t) + plate[high * width * 4 + x] * t);
  }
  return result;
}

async function compose(args) {
  const source = await rawImage(args.source);
  const {width, height} = source.info;
  const bounds = sourceBounds(args.source, width, height);
  const locked = await readMask(args.mask, width, height);
  const generatedMeta = await sharp(args.generated).metadata();
  if (Math.abs((generatedMeta.width / generatedMeta.height) / (width / height) - 1) > .025) fail('generated_aspect_mismatch');
  let generated = await sharp(args.generated).rotate().resize(width, height, {fit: 'fill'})
    .toColourspace('srgb').ensureAlpha().raw().toBuffer();
  let rectangles = [{left: bounds.placed_x, top: bounds.placed_y, width: bounds.placed_width, height: bounds.placed_height}];
  let frames = [];
  if (fs.existsSync(args.mask + '.layout.json')) {
    const layout = JSON.parse(fs.readFileSync(args.mask + '.layout.json', 'utf8'));
    if (layout.source_sha256 !== bounds.source_sha256 || layout.mask_sha256 !== crypto.createHash('sha256').update(fs.readFileSync(args.mask)).digest('hex') ||
        !Array.isArray(layout.rectangles) || !layout.rectangles.length) fail('invalid_semantic_mask');
    rectangles = layout.rectangles;
    frames = layout.frames || [];
  }
  for (const r of rectangles) if (['left','top','width','height'].some(k => !Number.isSafeInteger(r[k])) ||
      r.left < 0 || r.top < 0 || r.width < 1 || r.height < 1 || r.left + r.width > width || r.top + r.height > height) fail('invalid_semantic_mask');
  for (const f of frames) if (['left','top','width','height','border','photo_index'].some(k => !Number.isSafeInteger(f[k])) ||
    f.left < 0 || f.top < 0 || f.width < 1 || f.height < 1 || f.left + f.width > width || f.top + f.height > height ||
    f.border < 1 || f.border * 2 >= Math.min(f.width, f.height) || !rectangles[f.photo_index]) fail('invalid_semantic_mask');
  const within = (x, y, r) => x >= r.left && y >= r.top && x < r.left + r.width && y < r.top + r.height;
  const separators = separatorsOf(rectangles);
  generated = alignPlateSeams(generated, width, height, separators);
  const insidePhoto = (x, y) => {
    const frame = frames.find(f => within(x, y, f));
    return frame ? within(x, y, rectangles[frame.photo_index]) : rectangles.some(r => within(x, y, r));
  };
  // The title strip holds no photograph. The AI plate may have kept a dark slab or drawn text there, so the strip
  // is rebuilt by mirroring the plate just below it: the scene runs on past the photo's top edge without a seam.
  const band = bounds.placed_y;
  if (band < 96 || band * 2 > height) fail('invalid_source_bounds');
  const rowBytes = width * 4;
  for (let y = 0; y < band; y++) generated.copy(generated, y * rowBytes, (2 * band - 1 - y) * rowBytes, (2 * band - y) * rowBytes);
  const blurred = await sharp(locked.alpha, {raw: {width, height, channels: 1}}).blur(FEATHER_PIXELS).greyscale().raw().toBuffer();
  const final = Buffer.alloc(source.data.length);
  // The title sits on a dark fade over the photo's top edge. The fade only darkens unprotected pixels: the
  // photographed subject keeps its own pixels, so it simply stands out against the darkened background.
  const fadeStart = Math.round(band * .6), fadeEnd = band + TITLE_FADE;
  const darkness = y => {
    if (y <= fadeStart) return TITLE_DARKNESS;
    if (y >= fadeEnd) return 0;
    const t = (y - fadeStart) / (fadeEnd - fadeStart);
    return TITLE_DARKNESS * (1 - t * t * (3 - 2 * t));
  };
  for (let pixel = 0; pixel < locked.alpha.length; pixel++) {
    const at = pixel * 4;
    // The feather must not reintroduce artificial dark padding either.
    const x = pixel % width, y = Math.floor(pixel / width), photo = insidePhoto(x, y);
    // A separator between stacked photographs is kept as the layout drew it; elsewhere outside a window is plate.
    const separator = !photo && separators.some(f => y >= f.top && y < f.bottom && x >= f.left && x < f.right);
    const a = separator ? 1 : insideSource(x, y, bounds) ? Math.max(locked.alpha[pixel], photo ? blurred[pixel] : 0) / 255 : 0;
    const dark = y < fadeEnd ? darkness(y) * (1 - locked.alpha[pixel] / 255) : 0;
    for (let channel = 0; channel < 4; channel++) {
      const value = source.data[at + channel] * a + generated[at + channel] * (1 - a);
      final[at + channel] = channel === 3 ? 255 : Math.round(value * (1 - dark) + [DARK.r, DARK.g, DARK.b][channel] * dark);
    }
    const frame = frames.find(f => within(x, y, f));
    if (frame && locked.alpha[pixel] === 0 && (x < frame.left + frame.border || x >= frame.left + frame.width - frame.border ||
      y < frame.top + frame.border || y >= frame.top + frame.height - frame.border)) {
      final[at] = 255; final[at + 1] = 255; final[at + 2] = 255; final[at + 3] = 255;
    }
  }
  let rendered = await sharp(final, {raw: source.info}).png().toBuffer();
  if (args.caption) {
    const {caption} = require('./carousel_subject_caption.cjs');
    rendered = await caption(sharp, rendered, width, height, {...args.caption, band});
  }
  const pixels = await sharp(rendered).ensureAlpha().raw().toBuffer();
  const mismatch = countMismatches(source.data, pixels, locked.alpha);
  if (mismatch) fail('protected_pixels_changed');
  await sharp(rendered).png().toFile(args.target);
  return {width, height, protected_pixel_count: locked.count, mismatch_pixels: mismatch, feather_pixels: FEATHER_PIXELS};
}

async function verify(args) {
  const source = await rawImage(args.source);
  const {width, height} = source.info;
  if (!SIZES[args.aspect] || SIZES[args.aspect][0] !== width || SIZES[args.aspect][1] !== height) fail('invalid_source_dimensions');
  const hint = args.transform && typeof args.transform === 'object' ? args.transform : {};
  const canonical = await prepared(args.base, args.aspect, {crop_left: hint.crop_left, crop_top: hint.crop_top});
  const rebuilt = await sharp(canonical.buffer).ensureAlpha().raw().toBuffer();
  if (!rebuilt.equals(source.data)) fail('source_binding_mismatch');
  if (!args.transform || Object.keys(canonical.transform).length !== Object.keys(args.transform).length ||
      Object.keys(canonical.transform).some(key => canonical.transform[key] !== args.transform[key])) fail('source_transform_mismatch');
  const locked = await readMask(args.mask, width, height);
  const finalMetadata = await sharp(args.final).metadata();
  if (finalMetadata.format !== 'png' || finalMetadata.width !== width || finalMetadata.height !== height) fail('invalid_final_dimensions');
  const final = await rawImage(args.final);
  const mismatch = countMismatches(source.data, final.data, locked.alpha);
  if (mismatch) fail('protected_pixels_changed');
  return {width, height, protected_pixel_count: locked.count, mismatch_pixels: mismatch};
}

async function preview(args) {
  const result = await sharp(args.source, {limitInputPixels: 100000000})
    .rotate().toColourspace('srgb').jpeg({quality: 92, chromaSubsampling: '4:4:4'}).toFile(args.target);
  return {width: result.width, height: result.height, format: 'jpeg', quality: 92,
    chroma_subsampling: '4:4:4', bytes: result.size};
}

(async () => {
  const args = JSON.parse(fs.readFileSync(0, 'utf8'));
  const handlers = {prepare, mask, compose, verify, preview};
  if (!handlers[args.operation]) fail('invalid_operation');
  process.stdout.write(JSON.stringify(await handlers[args.operation](args)));
})().catch(error => {
  process.stdout.write(JSON.stringify({error: error.safeCode || 'subject_image_processing_failed'}));
  process.exitCode = 1;
});
