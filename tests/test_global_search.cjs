'use strict';

// Exercise the shipped controller, including async reads and actual key handlers.
// Requests are fixtures; this suite never modifies or loads the user's library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../static/global-search.js'), 'utf8');
const shellSource = fs.readFileSync(path.join(__dirname, '../static/shell.js'), 'utf8');
const decode = value => value.replaceAll('&amp;', '&').replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&#39;', "'");
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const productKey = value => /\b202h\b/i.test(value || '') ? '202H' : String(value || '');
const flush = async () => { await new Promise(resolve => setImmediate(resolve)); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.listeners = new Map(); this.children = [];
    this.queries = new Map(); this.attributes = {}; this.dataset = {}; this.open = false;
    this.value = ''; this.textContent = ''; this.isConnected = true; this.focused = 0;
  }
  set innerHTML(value) {
    this.html = value;
    this.options = [...value.matchAll(/<a\s+([^>]+)>/g)].map(match => {
      const node = new Element('a');
      for (const [, key, val] of match[1].matchAll(/([\w-]+)="([^"]*)"/g)) node.setAttribute(key, decode(val));
      node.dataset.searchIndex = node.attributes['data-search-index'];
      node.href = node.attributes.href;
      return node;
    });
  }
  get innerHTML() { return this.html || ''; }
  append(...children) { this.children.push(...children); }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(fn);
  }
  emit(name, values = {}) {
    const event = { target: this, key: '', defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...values };
    for (const fn of this.listeners.get(name) || []) fn(event);
    return event;
  }
  querySelector(selector) {
    if (!this.queries.has(selector)) this.queries.set(selector, new Element(selector.includes('input') ? 'input' : 'div'));
    return this.queries.get(selector);
  }
  querySelectorAll(selector) { return selector === '[data-search-index]' ? this.options || [] : []; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  removeAttribute(key) { delete this.attributes[key]; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  showModal() { this.open = true; }
  close() { this.open = false; this.emit('close'); }
  focus() { this.focused += 1; }
  select() { this.selected = true; }
  scrollIntoView() { this.scrolled = true; }
  closest(selector) {
    if (selector === '[data-search-index]') return this.dataset.searchIndex != null ? this : null;
    if (selector.startsWith('#')) return this.id === selector.slice(1) ? this : null;
    return ['INPUT', 'TEXTAREA', 'SELECT'].includes(this.tagName) || this.isContentEditable ? this : null;
  }
  getBoundingClientRect() { return { left: 10, right: 100, top: 10, bottom: 100 }; }
}

function harness(options = {}) {
  const window = new Element('window'), document = new Element('document'), body = new Element('body');
  const previousFocus = new Element('button');
  document.body = body; document.readyState = 'complete'; document.activeElement = previousFocus;
  document.createElement = tag => new Element(tag);
  const location = { hash: '#/202H/home' }, controls = { product: options.product ?? '202H', requests: [], timers: new Map(), timerId: 0 };
  const state = { cases: options.cases || [] };
  window.WorkbenchShell = {
    product: () => controls.product,
    matchesProduct: item => !controls.product || productKey(typeof item === 'string' ? item : item.product) === controls.product,
    url: legacy => '#/' + encodeURIComponent(controls.product || 'all') + '/' + legacy.slice(1),
  };
  const context = vm.createContext({
    window, document, location, state, esc, icon: name => `<svg data-icon="${name}" aria-hidden="true"></svg>`,
    URLSearchParams, AbortController, Date, localStorage: { getItem: () => controls.product },
    setTimeout(fn) { const id = ++controls.timerId; controls.timers.set(id, fn); return id; },
    clearTimeout(id) { controls.timers.delete(id); },
    async api(url, config) {
      controls.requests.push({ url, config });
      assert.ok(!config.method || config.method === 'GET', 'search must stay read-only');
      if (url.startsWith('/api/direction-catalog') && !options.catalogRequest) return { directions: options.directions || [], selling_points: options.points || [] };
      if (url.startsWith('/api/creation')) return { scripts: options.scripts || [] };
      if (url.startsWith('/api/ai/tasks')) return { tasks: options.tasks || [] };
      if (url.startsWith('/api/production')) return { jobs: options.jobs || [], carousels: options.carousels || [] };
      if (options.catalogRequest && url.startsWith('/api/direction-catalog')) return options.catalogRequest(url, config, controls);
      if (options.request) return options.request(url, config, controls);
      return url.startsWith('/api/performance') ? { videos: options.videos || [] } : { reviews: options.reviews || [], total: (options.reviews || []).length };
    },
  });
  if (options.realShell) vm.runInContext(shellSource, context, { filename: 'shell.js' });
  vm.runInContext(source, context, { filename: 'global-search.js' });
  const dialog = body.children[0], input = dialog.querySelector('#workbench-search-input');
  const list = dialog.querySelector('#workbench-search-list'), status = dialog.querySelector('#workbench-search-status');
  const feedback = dialog.querySelector('#workbench-search-feedback');
  const run = async () => {
    const callbacks = [...controls.timers.values()]; controls.timers.clear(); callbacks.forEach(fn => void fn()); await flush();
  };
  const search = async value => { input.value = value; input.emit('input'); await run(); };
  window.WorkbenchSearch.open();
  return { window, document, body, previousFocus, input, list, dialog, status, feedback, controls, location, state, run, search, api: window.WorkbenchSearch };
}

test('all four sources obey the current product and result Enter jumps retain it', async () => {
  const h = harness({
    cases: [
      { case_id: 'PVR-aaaaaaaaaaaa', title: 'Shared case', product: 'SHARPAL 202H', current: { test_direction: { direction_id: '202H-shared', main_difference: 'Shared angle' } } },
      { case_id: 'PVR-bbbbbbbbbbbb', title: 'Shared wrong product', product: '152N', current: { test_direction: { direction_id: '152N-shared' } } },
    ],
    videos: [
      { video_id: '123456789', title: 'Shared video', product: '202H' },
      { video_id: '987654321', title: 'Shared wrong video', product: '152N' },
    ],
    reviews: [
      { id: 'a'.repeat(16), product: '202H', platform: 'tiktok_video', source_ref: '123456789', text: 'Shared useful comment' },
      { id: 'b'.repeat(16), product: '152N', text: 'Shared wrong comment' },
    ],
  });
  await h.search('shared');
  const routes = h.list.options.map(node => node.href);
  assert.equal(routes.length, 4);
  assert.deepEqual(routes.map(route => route.split('?')[0]), ['#/202H/case/PVR-aaaaaaaaaaaa', '#/202H/videos', '#/202H/directions/202H-shared', '#/202H/reviews']);
  assert.doesNotMatch(h.list.innerHTML, /wrong/);
  assert.match(routes[2], /directions\/202H-shared/);
  assert.match(routes[3], /q=shared&video=123456789/);
  assert.match(h.list.innerHTML, /查看该视频的匹配评论/);
  assert.equal(new URLSearchParams(h.controls.requests.find(r => r.url.startsWith('/api/reviews')).url.split('?')[1]).get('product'), '202H');
  h.input.emit('keydown', { key: 'ArrowDown' });
  assert.equal(h.input.getAttribute('aria-activedescendant'), 'workbench-search-result-1');
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/202H/videos?video=123456789');
  assert.equal(h.dialog.open, false);
  assert.equal(h.previousFocus.focused, 1);
});

test('all-products mode includes other products and matches exact video IDs', async () => {
  const h = harness({ product: '', videos: [{ video_id: '987654321', title: 'Different name', product: '152N' }] });
  await h.search('987654321');
  assert.equal(h.list.options.length, 1);
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/all/videos?video=987654321');
  assert.equal(new URLSearchParams(h.controls.requests.find(r => r.url.startsWith('/api/reviews')).url.split('?')[1]).has('product'), false);
});

test('case and direction links activate through the actual keyboard handler', async () => {
  const h = harness({ cases: [{ case_id: 'PVR-cccccccccccc', title: 'Unique case', product: '202H', archived: true, current: { test_direction: { direction_id: '202H-S1', main_difference: 'angle' } } }] });
  await h.search('Unique case');
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/202H/case/PVR-cccccccccccc');
  h.api.open();
  await h.search('202H-S1');
  assert.match(h.list.innerHTML, /1 条案例/);
  h.input.emit('keydown', { key: 'Enter' });
  const params = new URLSearchParams(h.location.hash.split('?')[1]);
  assert.equal(h.location.hash.split('?')[0], '#/202H/directions/202H-S1');
  assert.equal(params.get('archive'), 'all');
});

test('a late previous query cannot overwrite newer results or loading state', async () => {
  const old = deferred(), latest = deferred();
  const h = harness({ request: url => url.startsWith('/api/performance') ? { videos: [] } : (url.includes('q=old') ? old.promise : latest.promise) });
  await h.search('old');
  const oldRequest = h.controls.requests.find(r => r.url.includes('q=old'));
  await h.search('new');
  assert.equal(oldRequest.config.signal.aborted, true);
  latest.resolve({ reviews: [{ id: 'n', text: 'new result', product: '202H' }], total: 1 });
  await flush();
  assert.match(h.list.innerHTML, /new/);
  assert.equal(h.list.getAttribute('aria-busy'), 'false');
  old.resolve({ reviews: [{ id: 'o', text: 'old result', product: '202H' }], total: 1 });
  await flush();
  assert.doesNotMatch(h.list.innerHTML, /old result/);
  assert.match(h.list.innerHTML, /new/);
  assert.equal(h.list.getAttribute('aria-busy'), 'false');
});

test('changing product aborts pending search and reissues a scoped query', async () => {
  const old = deferred();
  const h = harness({
    cases: [{ case_id: 'one', title: 'shared first', product: '202H' }, { case_id: 'two', title: 'shared second', product: '152N' }],
    request: url => url.startsWith('/api/performance') ? { videos: [] } : url.includes('product=202H') ? old.promise : { reviews: [{ id: 'new', text: 'shared new product', product: '152N' }], total: 1 },
  });
  await h.search('shared');
  h.controls.product = '152N';
  h.window.emit('workbench:productchange');
  await h.run();
  old.resolve({ reviews: [{ id: 'old', text: 'shared stale product', product: '202H' }], total: 1 });
  await flush();
  assert.match(h.list.innerHTML.replace(/<[^>]+>/g, ''), /shared second/);
  assert.doesNotMatch(h.list.innerHTML.replace(/<[^>]+>/g, ''), /shared first|stale product/);
  assert.ok(h.list.options.every(node => node.href.startsWith('#/152N/')));
  assert.match(h.dialog.querySelector('#workbench-search-scope').textContent, /152N/);
});

test('clearing or closing the search invalidates requests even if the API ignores abort', async () => {
  for (const action of ['clear', 'close']) {
    const pending = deferred();
    const h = harness({ request: url => url.startsWith('/api/performance') ? { videos: [] } : pending.promise });
    await h.search('late');
    if (action === 'clear') await h.search(''); else h.api.close();
    pending.resolve({ reviews: [{ id: 'late', text: 'late entry', product: '202H' }], total: 1 });
    await flush();
    assert.doesNotMatch(h.list.innerHTML, /late entry/);
    if (action === 'clear') assert.match(h.status.textContent, /输入案例名称/);
  }
});

test('review filtering pages past another product on a server without product filtering', async () => {
  const h = harness({ request: url => {
    if (url.startsWith('/api/performance')) return { videos: [] };
    const p = new URLSearchParams(url.split('?')[1]);
    return p.get('offset') === '0'
      ? { reviews: Array.from({ length: 200 }, (_, i) => ({ id: `wrong-${i}`, product: '152N', text: 'shared other' })), total: 201 }
      : { reviews: [{ id: 'wanted', product: '202H', text: 'shared found later', platform: 'amazon' }], total: 201 };
  } });
  await h.search('shared');
  assert.equal(h.controls.requests.filter(r => r.url.startsWith('/api/reviews')).length, 2);
  assert.equal(h.list.options.length, 1);
  assert.match(h.list.innerHTML, /found later/);
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/202H/reviews?q=shared');
});

test('partial API failure preserves local results and retry recovers', async () => {
  let failing = true;
  const h = harness({ cases: [{ case_id: 'local', title: 'needle case', product: '202H' }], request: url => {
    if (url.startsWith('/api/performance')) throw new Error('unavailable');
    if (failing) throw new Error('unavailable');
    return { reviews: [{ id: 'fixed', text: 'needle restored comment', product: '202H' }], total: 1 };
  } });
  await h.search('needle');
  assert.equal(h.feedback.hidden, false);
  assert.match(h.feedback.innerHTML, /视频、评论暂时无法搜索/);
  assert.match(h.list.innerHTML, /case/);
  assert.equal(h.list.getAttribute('aria-busy'), 'false');
  failing = false;
  const retry = new Element('button'); retry.id = 'workbench-search-retry';
  h.dialog.emit('click', { target: retry });
  await h.run();
  assert.match(h.list.innerHTML, /restored comment/);
  assert.doesNotMatch(h.feedback.innerHTML, /评论暂时/);
});

test('shortcuts respect typing, composition and handled keys; Ctrl/Cmd K work', () => {
  const h = harness(); h.api.close();
  for (const target of [new Element('input'), new Element('textarea'), new Element('select'), Object.assign(new Element(), { isContentEditable: true })]) {
    assert.equal(h.document.emit('keydown', { key: '/', target }).defaultPrevented, false);
    assert.equal(h.dialog.open, false);
  }
  h.document.emit('keydown', { key: '/', target: h.body, isComposing: true });
  h.document.emit('keydown', { key: '/', target: h.body, defaultPrevented: true });
  assert.equal(h.dialog.open, false);
  assert.equal(h.document.emit('keydown', { key: '/', target: h.body }).defaultPrevented, true);
  assert.equal(h.dialog.open, true);
  h.api.close();
  assert.equal(h.document.emit('keydown', { key: 'k', ctrlKey: true, target: h.input }).defaultPrevented, true);
  assert.equal(h.dialog.open, true);
  h.api.close();
  h.document.emit('keydown', { key: 'K', metaKey: true, target: h.input });
  assert.equal(h.dialog.open, true);
});

test('combobox selection wraps, has valid ARIA targets, and Escape closes via native dialog', async () => {
  const h = harness({ cases: [{ case_id: 'a', title: 'needle first', product: '202H' }, { case_id: 'b', title: 'needle second', product: '202H' }] });
  await h.search('needle');
  assert.match(h.dialog.innerHTML, /role="combobox"/);
  assert.match(h.dialog.innerHTML, /aria-controls="workbench-search-list"/);
  assert.equal(h.dialog.getAttribute('aria-labelledby'), 'workbench-search-title');
  h.input.emit('keydown', { key: 'ArrowUp' });
  assert.equal(h.input.getAttribute('aria-activedescendant'), h.list.options[1].getAttribute('id'));
  assert.deepEqual(h.list.options.map(node => node.getAttribute('aria-selected')), ['false', 'true']);
  h.input.emit('keydown', { key: 'ArrowDown' });
  assert.equal(h.input.getAttribute('aria-activedescendant'), h.list.options[0].getAttribute('id'));
  // The dialog keeps its native cancel behavior; input Escape closes immediately,
  // including when a search input would otherwise clear its text first.
  assert.equal(h.dialog.emit('cancel').defaultPrevented, false);
  assert.equal(h.input.emit('keydown', { key: 'Escape' }).defaultPrevented, true);
  assert.equal(h.dialog.open, false);
  assert.equal(h.input.getAttribute('aria-activedescendant'), null);
  assert.equal(h.previousFocus.focused, 1);
  h.api.initialize();
  assert.equal(h.body.children.length, 1);
});

test('result text is escaped and IME composition does not issue intermediate requests', async () => {
  const h = harness({ cases: [{ case_id: 'safe', title: 'needle <img src=x onerror=bad()>', product: '202H' }] });
  h.input.value = 'needle'; h.input.emit('input', { isComposing: true }); await h.run();
  assert.equal(h.controls.requests.length, 0);
  h.input.emit('compositionend'); await h.run();
  assert.match(h.list.innerHTML, /&lt;img/);
  assert.doesNotMatch(h.list.innerHTML, /<img/);
  assert.match(h.list.innerHTML, /<mark>needle<\/mark>/);
});

test('production shell canonicalizes direction products and merges their known aliases', async () => {
  const h = harness({ product: '', realShell: true, cases: [
    { case_id: 'first', title: 'one', product: 'SHARPAL 202H', current: { test_direction: { direction_id: '202H-S1' } } },
    { case_id: 'second', title: 'two', product: '202H', current: { test_direction: { direction_id: '202H-S1' } } },
  ] });
  await h.search('202H-S1');
  assert.equal(h.list.options.length, 1);
  assert.match(h.list.innerHTML, /2 条案例/);
  assert.equal(h.list.options[0].href, '#/202H/directions/202H-S1?archive=all');
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/202H/directions/202H-S1?archive=all');
});

test('deferred loading waits for app globals even while document is interactive', () => {
  const document = new Element('document'), window = new Element('window');
  document.readyState = 'interactive';
  const context = vm.createContext({ window, document });
  assert.doesNotThrow(() => vm.runInContext(source, context));
  assert.equal(document.listeners.get('DOMContentLoaded').length, 1);
  assert.equal(typeof window.WorkbenchSearch.initialize, 'function');
});

test('standalone directions without cases are searchable and product-scoped', async () => {
  const h = harness({ product: '', realShell: true, directions: [
    {product: '202H', direction_id: '202H-S3', main_difference: '选角自由', concern: '多把刀'},
    {product: '129N', direction_id: '129N-S3', main_difference: '选角另一个产品'},
  ] });
  await h.search('202H-S3');
  assert.equal(h.list.options.length, 1);
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(h.location.hash, '#/202H/directions/202H-S3?archive=all');
  const scoped = harness({ product: '202H', directions: [
    {product: '202H', direction_id: '202H-S3', main_difference: '选角自由'},
    {product: '129N', direction_id: '129N-S3', main_difference: '选角另一个产品'},
  ] });
  await scoped.search('选角');
  assert.doesNotMatch(scoped.list.innerHTML, /另一个产品/);
});

test('catalog failure keeps matching case directions and reports the missing source', async () => {
  const h = harness({ cases: [{case_id: 'legacy', product: '202H', current: {test_direction: {direction_id: '202H-S1'}}}],
    catalogRequest() { throw new Error('offline'); } });
  await h.search('202H-S1');
  assert.equal(h.list.options.length, 1);
  assert.match(h.feedback.innerHTML, /方向、卖点暂时无法搜索/);
  assert.equal(h.list.getAttribute('aria-busy'), 'false');
});

test('scripts, tasks, production orders and selling points are searchable within the current product', async () => {
  const h = harness({
    scripts: [{ id: 'SCR-aaaaaaaaaaaa', title: 'Paper slice script', product: '202H', direction_id: '202H-S1', version: 2 }, { id: 'SCR-bbbbbbbbbbbb', title: 'Paper other product', product: '152N' }],
    tasks: [{ task_id: 'AIT-aaaaaaaaaaaa', product: '202H', case_title: 'Paper case', instruction: 'write a paper hook' }],
    jobs: [{ id: 'PRD-aaaaaaaaaaaa', title: 'Paper voice order', product: '202H' }],
    carousels: [{ id: 'CAR-aaaaaaaaaaaa', title: 'Paper carousel', product: '202H', archived: true }],
    points: [{ code: 'S1', title: 'Paper thin edge', product: '202H' }, { code: 'S2', title: 'Paper elsewhere', product: '152N' }],
  });
  await h.search('paper');
  const routes = h.list.options.map(node => node.href);
  assert.deepEqual(routes, [
    '#/202H/directions?q=Paper+thin+edge&product=202H', '#/202H/scripts/SCR-aaaaaaaaaaaa', '#/202H/ai/AIT-aaaaaaaaaaaa',
    '#/202H/production/CAR-aaaaaaaaaaaa', '#/202H/production/PRD-aaaaaaaaaaaa',
  ]);
  assert.doesNotMatch(h.list.innerHTML, /other product|elsewhere/);
  assert.match(h.list.innerHTML, /第 2 版/);
  assert.match(h.list.innerHTML, /图文制作单 · 已归档/);
  assert.ok(['/api/creation?product=202H', '/api/ai/tasks?product=202H', '/api/production?product=202H'].every(url => h.controls.requests.some(r => r.url === url)));
});

test('empty search offers pages and actions; an action runs instead of navigating', async () => {
  const h = harness();
  assert.match(h.list.innerHTML, /工作概览/);
  assert.match(h.list.innerHTML, /<button type="button" class="global-search-result"[^>]*>.*开始写稿/);
  let started = 0;
  h.window.CaseAI = { startBlank() { started += 1; } };
  await h.search('写稿');
  assert.match(h.list.innerHTML, /开始<mark>写稿<\/mark>/);
  const pageRows = h.list.options.length;
  for (let step = 0; step < pageRows; step += 1) h.input.emit('keydown', { key: 'ArrowDown' });
  h.input.emit('keydown', { key: 'Enter' });
  assert.equal(started, 1);
  assert.equal(h.dialog.open, false);
  assert.equal(h.location.hash, '#/202H/home');
});
