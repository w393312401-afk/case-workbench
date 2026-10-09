'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const {createRangeController, nativeActiveIndex, parseCues} = require('../static/transcript-sync.js');

function player(duration = 38) {
  return {
    duration, currentTime: 0, paused: true, ended: false, plays: 0, pauses: 0,
    play() { this.paused = false; this.ended = false; this.plays++; return Promise.resolve(); },
    pause() { this.paused = true; this.pauses++; },
  };
}

test('loop repeats exactly the requested range, replacing the previous range', () => {
  const video = player(), controller = createRangeController(video);
  assert.equal(controller.set(3.56, 8.32, true), true);
  assert.equal(video.currentTime, 3.56);
  video.currentTime = 7;
  controller.tick();
  assert.equal(video.currentTime, 7);
  video.currentTime = 8.32;
  controller.tick();
  assert.equal(video.currentTime, 3.56);
  controller.set(11, 14, true);
  video.currentTime = 14;
  controller.tick();
  assert.equal(video.currentTime, 11);
  assert.equal(video.pauses, 0);
});

test('play once pauses at the end and leaves later playback unrestricted', () => {
  const video = player(), controller = createRangeController(video);
  controller.set(3.56, 8.32, false);
  video.currentTime = 8.45;
  controller.tick();
  assert.equal(video.currentTime, 8.32);
  assert.equal(video.paused, true);
  assert.equal(controller.range, null);
  video.play();
  video.currentTime = 15;
  controller.tick();
  assert.equal(video.currentTime, 15);
  assert.equal(video.paused, false);
});

test('range ending at the media duration can loop after the ended event', () => {
  const video = player(10), controller = createRangeController(video);
  controller.set(8, 12, true);
  assert.equal(controller.range.end, 10);
  video.currentTime = 10;
  video.ended = true;
  video.paused = true;
  controller.tick();
  assert.equal(video.currentTime, 8);
  assert.equal(video.paused, false);
  assert.equal(video.plays, 2);
});

test('manual seek outside a range and cancel both release segment playback', () => {
  const video = player(), controller = createRangeController(video);
  controller.set(4, 8, true);
  video.currentTime = 6;
  controller.seeking();
  assert.ok(controller.range);
  video.currentTime = 20;
  controller.seeking();
  controller.tick();
  assert.equal(controller.range, null);
  assert.equal(video.currentTime, 20);
  controller.set(4, 8, true);
  controller.clear();
  video.currentTime = 12;
  controller.tick();
  assert.equal(video.currentTime, 12);
});

test('invalid, reversed and out-of-video timestamps never start playback', () => {
  const video = player(10), controller = createRangeController(video);
  for (const [start, end] of [[-1, 3], [3, 3], [7, 5], [NaN, 5], [5, Infinity], [12, 16]]) {
    assert.equal(controller.set(start, end, true), false);
  }
  assert.equal(video.plays, 0);
  assert.equal(controller.range, null);
});

test('native highlight respects real range ends and gaps while legacy parsing remains conservative', () => {
  const cues = [{start: 3.56, end: 8.32}, {start: 12, end: 15}];
  assert.equal(nativeActiveIndex(cues, 2), -1);
  assert.equal(nativeActiveIndex(cues, 4), 0);
  assert.equal(nativeActiveIndex(cues, 10), -1);
  assert.equal(nativeActiveIndex(cues, 12), 1);
  assert.equal(nativeActiveIndex(cues, 15), -1);
  assert.deepEqual(parseCues('[0s - 1s] One.\nAn untimed sentence.'), []);
});

const escapeHTML = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
const context = {esc: escapeHTML};
vm.createContext(context);
vm.runInContext(fs.readFileSync(require.resolve('../static/native.js'), 'utf8'), context);

function nativeFixture() {
  return {current: {native_reference: {
    language: 'en', market: '实际发布市场与说话者地区未核验', scene: 'A scene.', speaker: 'A speaker.', audience: 'A viewer.',
    entries: [{id: 'one', start_sec: 3.56, end_sec: 8.32, function: '开头', channel: 'speech', original: 'Original <quote>.', meaning_zh: '原句。', context: 'Context.', analysis: 'Analysis.', adaptation: 'Rewritten <example>.', boundary: 'Boundary.'}],
  }}};
}

test('native presentation keeps original and rewrite distinct without mutating stored data', () => {
  const item = nativeFixture(), before = JSON.stringify(item);
  const html = context.nativeDetail(item);
  assert.match(html, /<blockquote class="native-original" lang="en">Original &lt;quote&gt;\.<\/blockquote>/);
  assert.match(html, /class="native-note native-adaptation"[^]*class="rewrite-badge">改写 · 非原话/);
  assert.match(html, /Rewritten &lt;example&gt;\./);
  assert.match(html, /class="native-note native-boundary"/);
  assert.match(html, /市场未核验/);
  assert.equal(JSON.stringify(item), before);
});

test('native timestamps carry both endpoints and the explicit loop and once actions', () => {
  const html = context.nativeDetail(nativeFixture());
  assert.match(html, /class="native-entry" data-native-start="3\.56" data-native-end="8\.32"/);
  assert.match(html, /class="timestamp-chip" data-native-seek="3\.56" data-native-end="8\.32" data-native-loop="true"/);
  assert.match(html, /data-native-loop="false">▶ 播放此段/);
  assert.match(html, /data-native-loop="true" aria-pressed="false">↻ 循环此段/);
  assert.doesNotMatch(html, /听原句/);
});

test('missing native timestamps are shown as unverified, without seek controls', () => {
  const item = nativeFixture();
  delete item.current.native_reference.entries[0].start_sec;
  const html = context.nativeDetail(item);
  assert.match(html, /时间待核实/);
  assert.doesNotMatch(html, /data-native-seek=/);
});
