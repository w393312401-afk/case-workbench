'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { parseCues, parseTimedLine, activeIndex } = require('../static/transcript-sync.js');

const text = [
  '[0.00s - 3.60s] Honestly, I never needed a mirror edge to break down cardboard.',
  '',
  '[4.46s - 10.08s] Then I ran one blade up the plate progression.',
  '[1:05.5 - 1:08,25] Minutes and comma decimals work too.',
].join('\n');

test('parses timed lines and ignores blank lines', () => {
  const cues = parseCues(text);
  assert.equal(cues.length, 3);
  assert.deepEqual([cues[0].start, cues[0].end], [0, 3.6]);
  assert.deepEqual([cues[2].start, cues[2].end], [65.5, 68.25]);
  assert.match(cues[1].text, /^\[4\.46s - 10\.08s\] Then/);
});

test('plain, mixed or reversed timestamps are not synced', () => {
  assert.deepEqual(parseCues('Sharp in seconds.'), []);
  assert.deepEqual(parseCues('[0.00s - 1.00s] One.\nNo timestamp here.'), []);
  assert.equal(parseTimedLine('[5.00s - 1.00s] Backwards.'), null);
  assert.deepEqual(parseCues(''), []);
});

test('active cue follows playback time and holds through gaps', () => {
  const cues = parseCues(text);
  assert.equal(activeIndex(cues, 0), 0);
  assert.equal(activeIndex(cues, 3.6), 0);
  assert.equal(activeIndex(cues, 4.0), 0);
  assert.equal(activeIndex(cues, 4.46), 1);
  assert.equal(activeIndex(cues, 70), 2);
  assert.equal(activeIndex([{ start: 2, end: 3 }], 1), -1);
});
