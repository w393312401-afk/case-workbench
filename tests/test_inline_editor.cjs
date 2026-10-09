'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function scope() {
  const saved = new Map(), context = {window: {addEventListener() {}}, state: {cases: []}, structuredClone,
    localStorage: {getItem: key => saved.get(key), setItem: (key, value) => saved.set(key, value)},
    daysSincePublish: () => null, $: () => null,
    assetLabels: {video: '视频', screenshot: '截图', transcript: '转写', source_manifest: '来源清单'},
  };
  vm.createContext(context);
  for (const file of ['editor.js', 'directions.js', 'inline-editor.js']) {
    let text = fs.readFileSync(path.join(__dirname, '../static', file), 'utf8');
    if (file === 'inline-editor.js') text = text.replace('return {open, save, close, beforeNavigate, handleDeleted}', 'return {open, save, close, beforeNavigate, handleDeleted, collect}');
    vm.runInContext(text, context, {filename: file});
  }
  return context;
}
function original() {return {
  title: 'Original', category: '待验证与整理', conclusion: 'Original conclusion', kind: 'editorial',
  assessments: [{dimension: 'conversion', polarity: 'unknown', reason: 'waiting'}],
  transcript: {text: 'original script', status: 'checked', source: 'original file'},
  translation_zh: {channel: 'transcript', source_text: 'original script', text: '原中文'},
  metrics: {video_id: '123456', creator_handle: '@old', items_sold: 8, views_display: '10K'},
  assets: {video: 'C:/video.mp4', screenshot: 'C:/historic.png', source_manifest: 'C:/manifest.json'},
  publish: {published_on: '2026-10-01'}, production: {script_version: 'V1', voiceover: 'voice.mp3'},
  evidence: {status: 'unverified', window: 'first week'}, highlights: ['old'], limitations: ['old limit'],
  source_event_ids: ['EV1'], native_reference: {entries: [{original: 'native words'}]},
};}
function collect(context, sectionId, entries) {
  const current = original(), editor = {sectionId, current, item: {revision: 'REV1'}};
  const before = JSON.stringify(current), payload = context.window.CaseInlineEditor.collect(editor, new Map(Object.entries(entries)));
  assert.equal(JSON.stringify(current), before, 'The original revision remains unchanged');
  assert.equal(payload.revision, 'REV1');
  assert.ok(payload.note);
  return {current, result: payload.current};
}
test('editing insights preserves every unrelated current field', () => {
  const {current, result} = collect(scope(), 'insights', {highlights: 'first\nsecond', limitations: 'new limit'});
  assert.equal(result.highlights.join(','), 'first,second');
  delete result.highlights; delete result.limitations; delete current.highlights; delete current.limitations;
  assert.equal(JSON.stringify(result), JSON.stringify(current));
});
test('changing metrics identity keeps the historical numbers, screenshots and all other sections', () => {
  const {current, result} = collect(scope(), 'metrics', {metric_video_id: '654321', metric_creator_handle: '@new', metric_published_title: 'New title'});
  assert.equal(result.metrics.items_sold, 8); assert.equal(result.metrics.views_display, '10K');
  assert.equal(result.assets.screenshot, 'C:/historic.png');
  delete current.metrics; delete result.metrics; assert.equal(JSON.stringify(result), JSON.stringify(current));
});
test('transcript changes remove stale translations without touching other sections', () => {
  const {current, result} = collect(scope(), 'transcript', {transcript_text: 'new script', transcript_status: 'checked', transcript_source: 'new source', translation_channel: 'transcript', translation_text: '原中文'});
  assert.equal(result.transcript.text, 'new script'); assert.equal(result.translation_zh, undefined);
  delete current.transcript; delete current.translation_zh; delete result.transcript;
  assert.equal(JSON.stringify(result), JSON.stringify(current));
});
test('release editing preserves other production associations and unrelated sections', () => {
  const {current, result} = collect(scope(), 'release', {publish_published_on: '2026-10-02', production_script_version: 'V2', production_voiceover: 'voice.mp3'});
  assert.equal(result.production.voiceover, 'voice.mp3'); assert.equal(result.production.script_version, 'V2');
  delete current.production; delete current.publish; delete result.production; delete result.publish;
  assert.equal(JSON.stringify(result), JSON.stringify(current));
});
test('new cases inherit global product or last-created/first product without a fourth required field', () => {
  const context = scope(); context.window.WorkbenchShell = {product: () => '129N'};
  assert.equal(context.defaultCaseProduct(), '129N');
  context.window.WorkbenchShell.product = () => '';
  context.state.cases = [{product: 'SHARPAL 202H'}];
  assert.equal(context.defaultCaseProduct(), 'SHARPAL 202H');
  context.localStorage.setItem('workbench-last-created-product', '777H');
  assert.equal(context.defaultCaseProduct(), '777H');
});
test('quick creation accepts a source URL or local video and preserves import metadata', () => {
  const context = scope(), values = new Map(Object.entries({title: 'New case', conclusion: 'Worth saving', product: '202H', url: 'https://example.com/video', content_origin: 'self_produced', metric_video_id: '654321', publish_published_on: '2026-10-08'}));
  context.FormData = class {get(name) {return values.get(name);}};
  context.state.editor = {mode: 'create', current: {category: '待验证与整理', kind: 'editorial', assessments: [], evidence: {status: 'unverified'}}};
  const payload = context.collectQuickCurrent();
  assert.equal(payload.current.content_origin, 'self_produced'); assert.equal(payload.current.metrics.video_id, '654321');
  assert.equal(payload.current.publish.published_on, '2026-10-08'); assert.equal(payload.note, '首次收录案例');
  values.delete('url'); assert.throws(() => context.collectQuickCurrent(), /链接/);
  values.set('asset_video', 'C:/imported.mp4'); assert.equal(context.collectQuickCurrent().current.assets.video, 'C:/imported.mp4');
});
