'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

// Run the actual page modules with isolated in-memory data; no server or real records are used.
function environment(product = '202H', legacy = '#library') {
  const nodes = new Map(), storage = new Map();
  const location = {hash: '#/202H/library'};
  const env = {product, legacy, calls: [], nodes, storage};
  function node(selector) {
    if (!nodes.has(selector)) nodes.set(selector, {
      innerHTML: '', textContent: '', value: '', hidden: false, open: false,
      classList: {toggle() {}, add() {}, remove() {}}, style: {}, dataset: {},
      addEventListener() {}, setAttribute() {}, removeAttribute() {}, focus() {},
      showModal() {this.open = true;}, close() {this.open = false;},
    });
    return nodes.get(selector);
  }
  const shell = {
    product: () => env.product,
    legacyHash: () => env.legacy + (env.product && !/^#(?:ai|case)(?:\/|$)/.test(env.legacy) ? `${env.legacy.includes('?') ? '&' : '?'}product=${encodeURIComponent(env.product)}` : ''),
    url: hash => `#/${encodeURIComponent(env.product || 'all')}/${hash.slice(1)}`,
    matchesProduct: value => !env.product || new RegExp(`(^|[^A-Za-z0-9])${env.product}([^A-Za-z0-9]|$)`, 'i').test(String(value || '')),
    registerProducts() {},
  };
  const context = {
    window: {WorkbenchShell: shell, addEventListener() {}, scrollTo() {}}, location,
    document: {hidden: false, addEventListener() {}, getElementById: node},
    history: {replaceState(_state, _title, hash) {location.hash = hash;}},
    sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    state: {cases: [], token: 'test-token'}, $: node, $$: () => [], URLSearchParams,
    api: async (url, options) => {env.calls.push({url, options});return env.api ? env.api(url, options) : {tasks: []};},
    icon: () => '', esc: value => String(value ?? ''), skeletonMarkup: () => '<p>Loading</p>',
    toast() {}, setInterval() {}, setTimeout, clearTimeout,
    addCalendarDays(value, days) {const date = new Date(`${value}T00:00:00Z`);date.setUTCDate(date.getUTCDate() + days);return date.toISOString().slice(0, 10);},
    readableDate: value => value || '',
  };
  env.context = vm.createContext(context);
  env.node = node;
  return env;
}

function load(env, file, marker, expose) {
  let source = fs.readFileSync(path.join(__dirname, '../static', file), 'utf8');
  if (marker) {
    assert.ok(source.includes(marker));
    source = source.replace(marker, `window.__pageTest = {${expose}};\n${marker}`);
  }
  vm.runInContext(source, env.context, {filename: file});
  return env.context.window.__pageTest;
}

test('video list and dashboard use current product instead of a saved product', () => {
  const env = environment();
  env.storage.set('video-data-filters', JSON.stringify({product: '999H', account: 'one', range: 'all'}));
  const page = load(env, 'performance.js', '  window.CasePerformance =', 'perf,filtered,boardContext,bind,reviewMaterial');
  page.perf.videos = [
    {video_id: 'a', product: '202H', account: 'one'},
    {video_id: 'b', product: 'SHARPAL 202H', account: 'one'},
    {video_id: 'c', product: '1202H', account: 'one'},
    {video_id: 'd', product: '202H', account: 'two'},
  ];
  assert.equal(page.filtered().map(item => item.video_id).join(','), 'a,b');
  assert.equal(page.boardContext().filters.product, '202H');
  env.product = '';
  assert.equal(page.filtered().length, 3);
  env.product = 'missing';
  assert.equal(page.filtered().length, 0);
  assert.equal(page.boardContext().filters.product, 'missing');
});

test('dashboard includes all aliases even when an exact product label exists', () => {
  const env = environment();
  const page = load(env, 'perf-dashboard.js', '  window.PerfDashboard =', 'scope');
  const data = page.scope({filters: {product: '202H', account: '', period: 'all'},
    summary: {weeks: [{week: '2026-10-05', complete: true}]},
    analytics: {videos: [['a', 'one', '202H'], ['b', 'one', 'SHARPAL 202H'], ['c', 'one', '1202H']],
      weekly: [[0, '2026-10-05', 1, 2, 3, 4, 5, 6], [1, '2026-10-05', 2, 3, 4, 5, 6, 7], [2, '2026-10-05', 9, 9, 9, 9, 9, 9]], monthly: [], shop_daily: []}});
  assert.equal(data.total.gmv, 13);
  assert.equal(data.total.videos, 2);
});

test('review product is independent of purchased SKU and does not clear other filters', () => {
  const env = environment('missing-product', '#reviews');
  env.storage.set('review-library-filters', JSON.stringify({q: 'clamp', sku: '202H kit', rating: 'low'}));
  const page = load(env, 'reviews.js', '  window.CaseReviews =', 'rev,params,filtersFromHash,writeHash');
  const params = page.params();
  assert.equal(params.get('product'), 'missing-product');
  assert.equal(params.get('sku'), '202H kit');
  assert.equal(params.get('q'), 'clamp');
  assert.equal(params.get('rating_max'), '3');
  assert.equal(page.filtersFromHash(), null);
  page.writeHash();
  assert.match(env.context.location.hash, /^#\/missing-product\/reviews\?/);
  assert.match(env.context.location.hash, /sku=202H\+kit/);
  env.product = '';
  assert.equal(page.params().has('product'), false);
});

test('old review responses cannot replace data after switching product', async () => {
  const env = environment('202H', '#reviews');
  const page = load(env, 'reviews.js', '  window.CaseReviews =', 'rev,loadResults');
  let finish;
  env.api = () => new Promise(resolve => {finish = resolve;});
  const pending = page.loadResults();
  env.product = '999H';
  finish({reviews: [{id: 'old'}], facets: {}});
  assert.equal(await pending, false);
  assert.equal(page.rev.data, null);
});

test('directions retain unavailable product context and separate filters on canonical routes', async () => {
  const env = environment('missing-product', '#directions?direction=D1&archive=all');
  env.api = async () => ({filters: {product: ''}, products: ['202H'], groups: [], case_count: 0});
  load(env, 'directions.js');
  await env.context.window.CaseDirections.route();
  const request = new URLSearchParams(env.calls.find(call => call.url.startsWith('/api/directions?')).url.split('?')[1]);
  assert.equal(request.get('product'), 'missing-product');
  assert.equal(request.get('archive'), 'all');
  const html = env.node('#content').innerHTML;
  assert.match(html, /卖点与方向/);
  assert.doesNotMatch(html, /select name="product"/);
  assert.match(html, /name="direction"/);
  assert.match(html, /value="D1"/);
  assert.match(html, /name="archive"/);
  assert.equal(env.product, 'missing-product');
});

test('writing selection and deletion candidates stay within the current product', async () => {
  const env = environment();
  env.context.state.cases = [{case_id: 'C1', product: '202H'}, {case_id: 'C2', product: '999H'}];
  const page = load(env, 'ai.js', '  return {mountCaseActions,startWriting,', 'ai,scopedTasks,selectableTasks,updateListSelection');
  await Promise.resolve();
  page.ai.taskList = {tasks: [{task_id: 'T1', case_id: 'C1', status: 'completed'}, {task_id: 'T2', case_id: 'C2', status: 'completed'}, {task_id: 'T3', product: '202H', status: 'running'}]};
  page.ai.selected = new Set(['T1', 'T2']);
  assert.equal(page.scopedTasks().length, 2);
  assert.equal(page.selectableTasks().map(task => task.task_id).join(','), 'T1');
  page.updateListSelection();
  assert.equal([...page.ai.selected].join(','), 'T1');
  env.product = 'missing-product';
  page.updateListSelection();
  assert.equal(page.ai.selected.size, 0);
});

test('choosing a writing case opens the existing variant dialog without posting a task', async () => {
  const env = environment();
  env.api = async url => url.startsWith('/api/cases/') ? {case_id: 'C1', product: '202H', revision: 'v1', title: 'Test case'} : url === '/api/ai/status' ? {available: true} : {tasks: [], models: [], skills: []};
  load(env, 'ai.js');
  assert.equal(await env.context.window.CaseAI.startWriting('C1'), true);
  assert.equal(env.node('#ai-create-dialog').open, true);
  assert.equal(env.node('#ai-create-title').textContent, '开始写稿');
  assert.equal(env.node('#ai-create-instruction').value, '');
  assert.equal(env.calls.some(call => call.options?.method === 'POST'), false);
});

test('old and incorrectly scoped writing detail links follow the actual task product', async () => {
  for (const initialHash of ['#ai/T202', '#/129N/ai/T202']) {
    const env = environment('129N');
    env.context.location.hash = '#/129N/library';
    env.context.localStorage = {getItem: () => '129N', setItem() {}};
    env.context.CustomEvent = class {};
    env.context.window.dispatchEvent = () => {};
    env.context.document.querySelector = () => null;
    env.context.state.cases = [{case_id: 'C202', product: 'SHARPAL 202H'}];
    load(env, 'shell.js');
    const page = load(env, 'ai.js', '  return {mountCaseActions,startWriting,', 'ai,alignTaskProduct');
    await new Promise(resolve => setImmediate(resolve));
    env.context.location.hash = initialHash;
    env.context.window.WorkbenchShell.canonicalize();
    env.api = async url => url.startsWith('/api/ai/tasks/') ? {task: {task_id: 'T202', case_id: 'C202', status: 'completed'}} : url === '/api/ai/status' ? {available: true} : {models: [], skills: []};
    await env.context.window.CaseAI.route();
    assert.equal(env.context.location.hash, '#/202H/ai/T202', initialHash);
    // Hash navigation returns before rendering an incorrectly labelled task.
    assert.equal(page.ai.task, null);
    env.context.window.WorkbenchShell.canonicalize();
    assert.equal(env.context.window.WorkbenchShell.product(), '202H');
  }
});

test('cached writing details align their payload product while all-products and unknown tasks stay unchanged', async () => {
  const env = environment('129N');
  env.context.localStorage = {getItem: () => '129N', setItem() {}};
  env.context.CustomEvent = class {};
  env.context.window.dispatchEvent = () => {};
  env.context.document.querySelector = () => null;
  env.context.location.hash = '#/129N/library';
  load(env, 'shell.js');
  const page = load(env, 'ai.js', '  return {mountCaseActions,startWriting,', 'ai,alignTaskProduct');
  await new Promise(resolve => setImmediate(resolve));
  page.ai.task = {task_id: 'T202', product: 'SHARPAL 202H', status: 'completed'};
  page.ai.page = 'T202';
  env.context.location.hash = '#/129N/ai/T202';
  env.context.window.WorkbenchShell.canonicalize();
  await env.context.window.CaseAI.route();
  assert.equal(env.context.location.hash, '#/202H/ai/T202');
  env.context.location.hash = '#/all/ai/T202';
  env.context.window.WorkbenchShell.canonicalize();
  assert.equal(page.alignTaskProduct(page.ai.task), false);
  assert.equal(env.context.location.hash, '#/all/ai/T202');
  env.context.location.hash = '#/129N/ai/UNKNOWN';
  env.context.window.WorkbenchShell.canonicalize();
  assert.equal(page.alignTaskProduct({task_id: 'UNKNOWN', case_id: 'unknown'}), false);
  assert.equal(env.context.location.hash, '#/129N/ai/UNKNOWN');
});

test('real shell product switches clear old review SKU/video without restoring them from page memory', async () => {
  for (const query of ['sku=202H+kit', 'video=7664986913497009439', 'sku=202H+kit&video=7664986913497009439&q=clamp&rating=low']) {
    const env = environment('');
    env.context.location.hash = `#/all/reviews?${query}`;
    env.context.localStorage = {getItem: () => '', setItem() {}};
    env.context.CustomEvent = class {};
    env.context.window.dispatchEvent = () => {};
    env.context.document.querySelector = () => null;
    load(env, 'shell.js');
    const shell = env.context.window.WorkbenchShell;
    shell.canonicalize();
    env.api = async url => url === '/api/reviews/summary' ? {labels: {platform: {}, nature: {}, topic: {}, competitor: {}, role: {}}, skus: ['202H kit']} : {reviews: [], facets: {}, total: 0};
    // Keep the real router and API/filter logic; rendering needs no browser in this unit test.
    const source = fs.readFileSync(path.join(__dirname, '../static/reviews.js'), 'utf8').replace('  window.CaseReviews =', '  render = () => {};\n  window.CaseReviews =');
    vm.runInContext(source, env.context);
    await env.context.window.CaseReviews.route();
    shell.setProduct('129N');
    shell.canonicalize();
    await env.context.window.CaseReviews.route();
    const queryAfter = new URLSearchParams(env.context.location.hash.split('?')[1] || '');
    assert.equal(queryAfter.has('sku'), false, query);
    assert.equal(queryAfter.has('video'), false, query);
    const lastRequest = env.calls.filter(call => call.url.startsWith('/api/reviews?')).at(-1);
    const params = new URLSearchParams(lastRequest.url.split('?')[1]);
    assert.equal(params.get('product'), '129N');
    assert.equal(params.has('sku'), false);
    assert.equal(params.has('video'), false);
    if (query.includes('q=clamp')) {
      assert.equal(params.get('q'), 'clamp');
      assert.equal(params.get('rating_max'), '3');
    }
  }
});
