// The guard must reject forbidden traffic before it reaches the browser fetch,
// strip active media before parsing markup, and preserve text browsing markup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class FakeNode {
  appendChild(node) { return node; }
  insertBefore(node) { return node; }
  replaceChild(node) { return node; }
}
FakeNode.TEXT_NODE = 3;
class FakeElement extends FakeNode {
  set innerHTML(value) { this.markup = value; }
  get innerHTML() { return this.markup; }
  set outerHTML(value) { this.markup = value; }
  get outerHTML() { return this.markup; }
  insertAdjacentHTML(position, value) { this.markup = value; }
  setAttribute(name, value) { this[name] = value; }
  matches() { return false; }
  querySelectorAll() { return []; }
}
let requests = 0, intervals = 0, reloads = 0, timerSequence = 0;
const timers = new Map(), storage = new Map(), network = [], documentEvents = new Map();
let versionResponse = {version: 'version-1', exported_at: '2026-10-08T12:00:00Z'};
const search = {id: 'case-search', value: 'clamp', type: 'search', disabled: false,
  matches: selector => selector.includes('#case-search'), closest: () => null};
const document = {
  readyState: 'loading', hidden: false, activeElement: search,
  addEventListener(name, callback) { documentEvents.set(name, callback); },
  querySelector(selector) {
    if (selector === "meta[name='public-exported-at']") return {content: '2026-10-08T12:00:00Z'};
    return null;
  },
  querySelectorAll() { return [search]; },
};
const location = {href: 'https://example.test/', origin: 'https://example.test', hash: '#library?q=clamp',
  reload() { reloads++; }};
const window = {
  addEventListener() {},
  setInterval() { return ++intervals; },
  clearInterval() {},
  setTimeout(callback, delay) { const id = ++timerSequence; timers.set(id, {callback, delay}); return id; },
  clearTimeout(id) { timers.delete(id); },
  scrollX: 0, scrollY: 620,
  sessionStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  EventSource: class {},
  fetch: async (url, options) => {
    requests++; network.push({url, options});
    return new Response(JSON.stringify(url === '/api/public-version' ? versionResponse : {ok: true}),
      {headers: {'Content-Type': 'application/json'}});
  },
};
const context = vm.createContext({
  window, Element: FakeElement, Node: FakeNode, URL, Response, Intl, Date, AbortController,
  location, document,
  history: {replaceState() {}},
});
vm.runInContext(fs.readFileSync(path.join(__dirname, '../deployment/text-only/public-mode.js'), 'utf8'), context);

(async () => {
  const markup = '<article><h2>文字稿</h2><video src="/media/a.mp4" preload="auto"><source src="/media/b.mp4"></video><picture><img src="/media/a.png"></picture><audio src="/media/a.mp3"></audio><iframe src="https://example.org/embed"></iframe><button id="copy-transcript">复制全文</button><input id="case-search"><svg><path d="M1 2"/></svg></article>';
  const clean = window.PublicTextMode.textOnlyMarkup(markup);
  assert(!/<(?:video|img|audio|source|picture|iframe)\b/i.test(clean));
  assert(clean.includes('<h2>文字稿</h2>'));
  assert(clean.includes('id="copy-transcript"'));
  assert(clean.includes('id="case-search"'));
  assert(clean.includes('<svg>'));
  const target = new FakeElement();
  target.innerHTML = markup;
  assert.equal(target.markup, clean);
  target.insertAdjacentHTML('beforeend', markup);
  assert.equal(target.markup, clean);

  for (const [url, options] of [
    ['/api/cases', {method: 'POST'}],
    ['/api/cases/PVR-example', {method: 'DELETE'}],
    ['/media/PVR-example/video', {}],
    ['/api/ai/tasks/AIT-example/stream', {}],
    ['https://outside.example/api/state', {}],
  ]) {
    const result = await window.fetch(url, options);
    assert.equal(result.status, 403, url);
  }
  assert.equal(requests, 0);
  assert.equal((await window.fetch('/api/state')).status, 200);
  assert.equal((await window.fetch('/api/reviews?q=clamp')).status, 200);
  assert.equal(requests, 2);
  assert.equal(window.EventSource, undefined);
  assert.equal(window.setInterval(() => refresh(true), 5000), 0);
  assert.equal(window.setInterval(() => { pollTask(); loadTaskList(); }, 2500), 0);
  assert.equal(window.setInterval(() => repaintChart(), 1000), 1);
  assert.equal(intervals, 1);

  // Update detection only reads the tiny version endpoint. The loaded timestamp
  // remains attached to the text actually displayed until the page reloads.
  const versionStart = requests;
  assert.equal(await window.PublicTextMode.checkVersion(), false);
  assert.equal(reloads, 0);
  document.hidden = true;
  assert.equal(await window.PublicTextMode.checkVersion(), false);
  assert.equal(requests, versionStart + 1, 'hidden pages do not probe versions');
  document.hidden = false;
  assert.equal(await window.PublicTextMode.checkVersion(), false);
  assert.equal(reloads, 0, 'unchanged published content does not reload');
  versionResponse = {version: 'version-2', exported_at: '2026-10-08T12:01:00Z'};
  assert.equal(await window.PublicTextMode.checkVersion(), true);
  function runRefreshTimer() {
    const entry = [...timers].find(([, timer]) => timer.delay === 250);
    assert(entry, 'changed content schedules an automatic refresh');
    timers.delete(entry[0]); entry[1].callback();
  }
  document.hidden = true;
  runRefreshTimer();
  assert.equal(reloads, 0, 'a tab hidden during the refresh delay stays paused');
  document.hidden = false;
  assert.equal(await window.PublicTextMode.checkVersion(), true);
  runRefreshTimer();
  assert.equal(reloads, 1);
  assert.equal(location.hash, '#library?q=clamp', 'reload preserves the current hash');
  const saved = JSON.parse(storage.get('public-text-reading-state-v1'));
  assert.equal(saved.hash, location.hash);
  assert.equal(saved.y, 620);
  assert.equal(saved.controls[0].selector, '[id="case-search"]');
  assert.equal(saved.controls[0].value, 'clamp');
  assert.equal(saved.focus, '[id="case-search"]');
  for (const request of network.slice(versionStart)) {
    assert.equal(request.url, '/api/public-version');
    assert.equal(request.options.cache, 'no-store');
    assert(!request.options.method || request.options.method === 'GET');
  }
  console.log('Public text guard: media/writes blocked; version detection, hidden-tab pause, and reading-state refresh checks passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
