'use strict';

// The title lives in the strip above the photograph's top edge (no photographed pixel is there). The strip's own
// dark fade comes from the compositor; the caption only adds ink, and only inside the strip.
const DEFAULT_BAND = 224;
const INK = '#FFF5E6';
const SHADOW = '#D99A6C';
const FONT_FILE = 'C:/Windows/Fonts/impact.ttf';
const STROKE = '#05080A';
// Equivalent to the four public carousel_layout.py presets. Legacy plain/rugged
// rendering remains in legacyCaption so old saved choices retain their appearance.
const CAPTION_PRESETS = {
  yellow_impact: {face: '#FFF9E5', emphasis: '#FFE044', body: '#FFF9E5',
    stroke: .055, outer: .012, outerColor: '#FFF9E5', depth: '#05080A', offset: [.030, .045]},
  orange_depth: {face: '#FFFCF6', emphasis: '#FFFCF6', body: '#FFFCF6',
    stroke: .050, outer: 0, outerColor: STROKE, depth: '#FF751C', offset: [.095, .105]},
  red_comic: {face: '#FFF9E5', emphasis: '#FF453D', body: '#FFF9E5',
    stroke: .055, outer: .018, outerColor: '#FFFFFF', depth: '#05080A', offset: [.028, .045], shear: .20},
  lime_pop: {face: '#FFFFFF', emphasis: '#D8FF36', body: '#FFFFFF',
    stroke: .050, outer: 0, outerColor: STROKE, depth: '#26DFDC', offset: [.035, .045]},
};
const escape = value => value.replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&apos;');

function invalid(reason) {
  const error = new Error(reason);
  error.safeCode = 'invalid_caption';
  throw error;
}

function line(value) {
  if (typeof value !== 'string' || !value.trim() || value.length > 2048 || /[\r\n\u0000-\u0008\u000b-\u001f]/.test(value)) {
    invalid('Caption lines must be nonempty single-line text.');
  }
  return value;
}

async function glyphs(sharp, value, size, color = INK, font = 'Impact') {
  // No width/height is supplied to Pango: it must neither wrap nor truncate copy.
  return sharp({text: {
    text: `<span foreground="${color}">${escape(value)}</span>`,
    font: `${font} ${size}`, fontfile: font === 'Impact' ? FONT_FILE : 'C:/Windows/Fonts/arial.ttf', dpi: 72, rgba: true,
  }}).png().toBuffer({resolveWithObject: true});
}

async function legacyCaption(sharp, imageBuffer, width, height, {title_lines, body = '', caption_style, band = DEFAULT_BAND} = {}) {
  if (!Number.isSafeInteger(band) || band < 96 || !Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 96 || height < band * 2 ||
      width * height > 100000000 || !Buffer.isBuffer(imageBuffer)) invalid('Invalid caption image.');
  if (!Array.isArray(title_lines) || title_lines.length < 1 || title_lines.length > 2) {
    invalid('Supply one or two exact title lines.');
  }
  const titles = title_lines.map(line);
  const copy = body === '' || body == null ? '' : line(body);
  const decoded = await sharp(imageBuffer, {limitInputPixels: 100000000}).ensureAlpha().raw()
    .toBuffer({resolveWithObject: true});
  if (decoded.info.width !== width || decoded.info.height !== height || decoded.info.channels !== 4) {
    invalid('Caption dimensions must match the input image.');
  }

  const marginX = Math.max(20, Math.round(width * .055));
  const marginY = Math.max(10, Math.round(band * .1));
  const offset = caption_style === 'plain' ? 2 : 4;
  const availableWidth = width - 2 * marginX - offset;
  const availableHeight = band - 2 * marginY - offset;
  const titleGap = 16, bodyGap = 22;
  const measure = async size => {
    const title = await Promise.all(titles.map(value => glyphs(sharp, value, size)));
    const bodySize = Math.max(4, Math.min(34, Math.round(size * .32)));
    const small = copy ? await glyphs(sharp, copy, bodySize) : null;
    const totalHeight = title.reduce((sum, item) => sum + item.info.height, 0) +
      titleGap * (title.length - 1) + (small ? bodyGap + small.info.height : 0);
    return {title, small, size, bodySize, totalHeight,
      fits: [...title, ...(small ? [small] : [])].every(item => item.info.width <= availableWidth) &&
        totalHeight <= availableHeight};
  };

  // Starting small also bounds temporary Pango rasters for unusually long copy.
  let low = 4, high = 134;
  let fitted = await measure(low);
  if (!fitted.fits) invalid('Caption text cannot fit without clipping.');
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    const candidate = await measure(middle);
    if (candidate.fits) { low = middle; fitted = candidate; }
    else high = middle - 1;
  }

  const layers = [];
  let top = Math.floor((band - fitted.totalHeight - offset) / 2);
  const addLine = async (render, value, size) => {
    const left = Math.floor((width - render.info.width - offset) / 2);
    const shadow = await glyphs(sharp, value, size, SHADOW);
    layers.push({input: shadow.data, left: left + offset, top: top + offset});
    layers.push({input: render.data, left, top});
    top += render.info.height;
  };
  for (let index = 0; index < fitted.title.length; index++) {
    if (index) top += titleGap;
    await addLine(fitted.title[index], titles[index], fitted.size);
  }
  if (fitted.small) {
    top += bodyGap;
    await addLine(fitted.small, copy, fitted.bodySize);
  }
  const header = await sharp(decoded.data, {raw: {width, height, channels: 4}})
    .extract({left: 0, top: 0, width, height: band}).composite(layers).raw().toBuffer();
  // Copy the strip only. Whole-image compositing can round translucent pixels elsewhere;
  // retaining the decoded lower bytes avoids that loss.
  const result = Buffer.from(decoded.data);
  header.copy(result, 0);
  return sharp(result, {raw: {width, height, channels: 4}}).png().toBuffer();
}

function effects(size, preset) {
  return {stroke: Math.max(1, Math.round(size * preset.stroke)),
    outer: preset.outer ? Math.max(1, Math.round(size * preset.outer)) : 0,
    dx: Math.max(1, Math.round(size * preset.offset[0])),
    dy: Math.max(1, Math.round(size * preset.offset[1]))};
}

function lineSize(render, size, preset) {
  const effect = effects(size, preset), pad = effect.stroke + effect.outer;
  return {width: render.info.width + Math.ceil(render.info.height * (preset.shear || 0)) + 2 * pad + effect.dx,
    height: render.info.height + 2 * pad + effect.dy, ...effect};
}

function colorMask(alpha, color) {
  const rgb = [1, 3, 5].map(start => parseInt(color.slice(start, start + 2), 16));
  const rgba = Buffer.alloc(alpha.length * 4);
  for (let pixel = 0; pixel < alpha.length; pixel++) {
    const at = pixel * 4;
    rgba[at] = rgb[0]; rgba[at + 1] = rgb[1]; rgba[at + 2] = rgb[2]; rgba[at + 3] = alpha[pixel];
  }
  return rgba;
}

async function styledLine(sharp, render, size, preset, color) {
  const geometry = lineSize(render, size, preset), pad = geometry.stroke + geometry.outer;
  const glyphAlpha = await sharp(render.data).extractChannel('alpha').raw().toBuffer();
  const skew = Math.ceil(render.info.height * (preset.shear || 0));
  const maskWidth = render.info.width + skew + 2 * pad, maskHeight = render.info.height + 2 * pad;
  const face = Buffer.alloc(maskWidth * maskHeight);
  for (let y = 0; y < render.info.height; y++) {
    // Shift rows instead of rotating the baseline: comic text leans forward,
    // while its exact supplied lines and their complete glyphs remain intact.
    const shift = Math.round((render.info.height - 1 - y) * (preset.shear || 0));
    glyphAlpha.copy(face, (y + pad) * maskWidth + pad + shift, y * render.info.width, (y + 1) * render.info.width);
  }
  const expand = radius => sharp(face, {raw: {width: maskWidth, height: maskHeight, channels: 1}})
    .dilate(radius).raw().toBuffer();
  const border = await expand(geometry.stroke);
  const layers = [], raw = {width: maskWidth, height: maskHeight, channels: 4};
  const depth = colorMask(border, preset.depth), steps = Math.max(geometry.dx, geometry.dy);
  for (let step = steps; step > 0; step--) {
    layers.push({input: depth, raw, left: Math.round(geometry.dx * step / steps), top: Math.round(geometry.dy * step / steps)});
  }
  if (geometry.outer) layers.push({input: colorMask(await expand(pad), preset.outerColor), raw, left: 0, top: 0});
  layers.push({input: colorMask(border, STROKE), raw, left: 0, top: 0});
  layers.push({input: colorMask(face, color), raw, left: 0, top: 0});
  return sharp({create: {width: geometry.width, height: geometry.height, channels: 4, background: '#00000000'}})
    .composite(layers).png().toBuffer();
}

async function caption(sharp, imageBuffer, width, height, {title_lines, body = '', caption_style = 'yellow_impact', band = DEFAULT_BAND} = {}) {
  if (caption_style === 'plain' || caption_style === 'rugged') {
    return legacyCaption(sharp, imageBuffer, width, height, {title_lines, body, caption_style, band});
  }
  if (typeof caption_style !== 'string' || !Object.hasOwn(CAPTION_PRESETS, caption_style)) invalid('Unknown caption style.');
  const preset = CAPTION_PRESETS[caption_style];
  if (!Number.isSafeInteger(band) || band < 96 || !Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width < 96 || height < band * 2 ||
      width * height > 100000000 || !Buffer.isBuffer(imageBuffer)) invalid('Invalid caption image.');
  if (!Array.isArray(title_lines) || title_lines.length < 1 || title_lines.length > 2) invalid('Supply one or two exact title lines.');
  const titles = title_lines.map(line), copy = body === '' || body == null ? '' : line(body);
  const decoded = await sharp(imageBuffer, {limitInputPixels: 100000000}).ensureAlpha().raw().toBuffer({resolveWithObject: true});
  if (decoded.info.width !== width || decoded.info.height !== height || decoded.info.channels !== 4) invalid('Caption dimensions must match the input image.');
  const marginX = Math.max(20, Math.round(width * .055)), marginY = Math.max(10, Math.round(band * .1));
  const availableWidth = width - 2 * marginX, availableHeight = band - 2 * marginY;
  const measure = async size => {
    const title = await Promise.all(titles.map(value => glyphs(sharp, value, size, '#FFFFFF')));
    const metrics = title.map(render => lineSize(render, size, preset));
    const bodySize = Math.max(4, Math.round(size * .53));
    const small = copy ? await glyphs(sharp, copy, bodySize, preset.body, 'Arial') : null;
    const titleGap = Math.max(8, Math.round(size * .18)), bodyGap = Math.max(10, Math.round(size * .20));
    const totalHeight = metrics.reduce((sum, metric) => sum + metric.height, 0) + titleGap * (title.length - 1) +
      (small ? bodyGap + small.info.height : 0);
    return {title, metrics, small, size, totalHeight, titleGap, bodyGap,
      fits: metrics.every(metric => metric.width <= availableWidth) && (!small || small.info.width <= availableWidth) && totalHeight <= availableHeight};
  };
  // Bound temporary Pango rasters even when a caller supplies unusually wide input.
  let low = 4, high = Math.min(256, Math.max(8, Math.round(width * .105))), fitted = await measure(low);
  if (!fitted.fits) invalid('Caption text cannot fit without clipping.');
  while (low < high) {
    const middle = Math.ceil((low + high) / 2), candidate = await measure(middle);
    if (candidate.fits) { low = middle; fitted = candidate; } else high = middle - 1;
  }
  const layers = [];
  let top = Math.floor((band - fitted.totalHeight) / 2);
  for (let index = 0; index < fitted.title.length; index++) {
    if (index) top += fitted.titleGap;
    const color = index || titles.length === 1 ? preset.emphasis : preset.face;
    layers.push({input: await styledLine(sharp, fitted.title[index], fitted.size, preset, color),
      left: Math.floor((width - fitted.metrics[index].width) / 2), top});
    top += fitted.metrics[index].height;
  }
  if (fitted.small) layers.push({input: fitted.small.data, left: Math.floor((width - fitted.small.info.width) / 2), top: top + fitted.bodyGap});
  const header = await sharp(decoded.data, {raw: {width, height, channels: 4}})
    .extract({left: 0, top: 0, width, height: band}).composite(layers).raw().toBuffer();
  const result = Buffer.from(decoded.data);
  header.copy(result, 0);
  return sharp(result, {raw: {width, height, channels: 4}}).png().toBuffer();
}

module.exports = {caption, CAPTION_PRESETS};
