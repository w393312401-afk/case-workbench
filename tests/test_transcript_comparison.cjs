'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const app = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const start = app.indexOf('function currentTranslation(item) {');
const end = app.indexOf('\nfunction renderTranscriptComparison(', start);
assert.ok(start >= 0 && end > start, 'Load the actual frontend mapping function');
const context = {};
vm.runInNewContext(`${app.slice(start, end)}\nthis.compare = transcriptComparison;`, context);
const compare = context.compare;
const rows = [
  ['0.00s - 1.26s', 'I did something kind of ridiculous.', '我干了件有点离谱的事。'],
  ['1.90s - 4.32s', 'An outdoor knife does not need a mirror edge.', '户外刀其实不需要镜面刃。'],
  ['4.76s - 5.92s', 'It just needs to cut.', '能切就行。'],
  ['6.48s - 8.86s', 'But once I realized I could make an edge look like this at home,', '但当我发现，在家就能把刀刃做成这样，'],
  ['9.20s - 11.52s', 'I started doing it to every knife I cared about.', '我就开始给每把我在意的刀都这么处理。'],
  ['12.12s - 14.90s', 'The 202H clamps the blade, lets me set a repeatable angle,', '202H能夹住刀身，让我设置可以重复使用的角度，'],
  ['15.30s - 17.66s', 'then work from sharpening plates to the leather strop', '然后从磨板一路做到皮革荡刀板收尾，'],
  ['17.66s - 19.22s', 'without guessing freehand.', '不用徒手凭感觉猜角度。'],
  ['19.72s - 20.30s', 'Is it overkill?', '是不是有点过头？'],
  ['20.90s - 21.28s', 'Probably.', '大概是。'],
  ['21.98s - 23.44s', 'But that clean reflection is addictive.', '但那种干净的反光，确实让人上瘾。'],
  ['24.10s - 25.46s', 'Check out the 202H below.', '去下方看看202H。'],
];
const original = rows.map(([time, english]) => `[${time}] ${english}`).join('\n');
const english = rows.map(row => row[1]).join(' ');
const sentences = [...new Intl.Segmenter('en', {granularity:'sentence'}).segment(english)].map(row => row.segment.trim());
const current = sentences.join('\n\n');
const block = `\`\`\`text\n${original}\n\`\`\`\n\n`;
const table = '| 时间 | 中文语义还原 |\n|---|---|\n' + rows.map(([time, , chinese]) => `| ${time} | ${chinese} |`).join('\n');
const source = `## 5. 口播原文完整转写\n\n${block}${table}\n\n## 6. 视觉节拍\n`;
const item = (text = current, analysis = source) => ({current:{transcript:{text}}, source_analysis:{text:analysis}});

test('current sentences merge their original ASR fragments and translations', () => {
  const result = compare(item());
  assert.equal(result.length, 9);
  assert.equal(JSON.stringify(result.map(row => row.original)), JSON.stringify(sentences));
  assert.equal(result[3].chinese, rows[3][2] + rows[4][2]);
  assert.equal(result[4].chinese, rows[5][2] + rows[6][2] + rows[7][2]);
  assert.ok(result.every(row => row.chinese));
  assert.ok(result.every(row => !/^\[/.test(row.original)));
});

test('legacy timestamped transcripts keep all segment-level pairs', () => {
  const result = compare(item(original));
  assert.equal(result.length, rows.length);
  assert.equal(result[4].original, `[${rows[4][0]}] ${rows[4][1]}`);
  assert.equal(result[4].chinese, rows[4][2]);
});

test('word or punctuation changes cannot borrow the older translation', () => {
  assert.equal(compare(item(current.replace('does not need', 'needs'))).length, 0);
  assert.equal(compare(item(current.replace('ridiculous.', 'ridiculous!'))).length, 0);
  assert.equal(compare(item(original.replace('ridiculous.', 'unnecessary.'))).length, 0);
});

test('whitespace changes preserve the English match and sentence boundaries', () => {
  const result = compare(item(current.replaceAll('\n\n', '\r\n').replace('I realized', 'I   realized')));
  assert.equal(result.length, 9);
  assert.equal(result[3].chinese, rows[3][2] + rows[4][2]);
});

test('translations cannot be taken from another heading or code block', () => {
  assert.equal(compare(item(current, block + '## Another case\n' + table)).length, 0);
  assert.equal(compare(item(current, block + '```text\nAnother script.\n```\n' + table)).length, 0);
});

test('a missing fragment translation leaves the entire merged sentence untranslated', () => {
  const result = compare(item(current, source.replace(`| ${rows[4][0]} | ${rows[4][2]} |`, `| ${rows[4][0]} | |`)));
  assert.equal(result.length, 9);
  assert.equal(result[3].chinese, '');
  assert.equal(result[2].chinese, rows[2][2]);
});

test('ambiguous sentence-spanning translations are not split or duplicated', () => {
  const combined = '```text\n[0.0s - 2.0s] First sentence. Second sentence.\n```\n\n| 时间 | 中文 |\n|---|---|\n| 0.0–2.0s | 第一。第二。 |';
  assert.equal(compare(item('First sentence.\n\nSecond sentence.', combined)).length, 0);
});

test('conflicting translations for one timestamp cannot be silently selected', () => {
  const conflicted = source.replace('|---|---|\n', '|---|---|\n| 0.00–1.26s | 不同的译文。 |\n');
  assert.equal(compare(item(current, conflicted)).length, 0);
});

test('clock timestamps and seconds refer to the same translation interval', () => {
  const clockSource = '```text\n[00:01.00 - 00:02.50] A complete sentence.\n```\n\n| 时间 | 中文 |\n|---|---|\n| 1.00–2.50s | 一个完整句子。 |';
  assert.equal(compare(item('A complete sentence.', clockSource))[0].chinese, '一个完整句子。');
});

test('mixed timestamped and plain current fragments are not guessed', () => {
  assert.equal(compare(item('[0.00s - 1.26s] ' + current)).length, 0);
});

test('current saved translation takes priority and aligns matching lines', () => {
  const saved = item('First line.\nSecond line.');
  saved.current.translation_zh = {
    source_text: saved.current.transcript.text,
    text: '第一句。\n第二句。',
    channel: 'transcript',
  };
  const result = compare(saved);
  assert.equal(JSON.stringify(result), JSON.stringify([
    {original: 'First line.', chinese: '第一句。'},
    {original: 'Second line.', chinese: '第二句。'},
  ]));
});

test('screen text stays distinct from a complete spoken transcript', () => {
  const saved = {current: {translation_zh: {
    source_text: 'Sharp in seconds.', text: '几秒就磨锋利了。', channel: 'on_screen',
  }}};
  assert.equal(compare(saved)[0].chinese, '几秒就磨锋利了。');
});

test('screen translation is hidden when a revised current source no longer contains it', () => {
  const saved = {current: {
    transcript: {text: 'Sharp in seconds.'},
    translation_zh: {
      source_text: 'Sharp in seconds.', text: '几秒就磨锋利了。', channel: 'on_screen',
    },
  }};
  assert.equal(compare(saved)[0].chinese, '几秒就磨锋利了。');
  saved.current.transcript.text = 'A different screen caption.';
  assert.equal(compare(saved).length, 0);
});

test('mismatched line counts show one complete source and translation pair', () => {
  const saved = item('First line.\nSecond line.');
  saved.current.translation_zh = {
    source_text: saved.current.transcript.text,
    text: '第一句和第二句。',
    channel: 'transcript',
  };
  assert.equal(compare(saved).length, 1);
  assert.equal(compare(saved)[0].original, saved.current.transcript.text);
});

test('stale saved transcript translation is ignored', () => {
  const saved = item('A new transcript.');
  saved.current.translation_zh = {
    source_text: 'An old transcript.', text: '旧译文。', channel: 'transcript',
  };
  assert.equal(compare(saved).length, 0);
});
