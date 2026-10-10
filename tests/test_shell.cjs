'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/shell.js'), 'utf8');
function harness(hash = '#library', stored) {
  const values = new Map(stored === undefined ? [] : [['workbench-product-v1', stored]]);
  const location = { hash };
  const events = [];
  const context = vm.createContext({ URLSearchParams, Map, Set, console, location,
    history: { replaceState(a,b,value) { location.hash = value; } },
    localStorage: { getItem: key => values.get(key) ?? null, setItem: (key,value) => values.set(key,value) },
    document: { querySelector: () => null },
    state: { cases: [{case_id: 'PVR-0123456789ab', product: 'SHARPAL 129N'}] },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    window: { dispatchEvent: e => events.push(e) },
  });
  vm.runInContext(source, context);
  return { shell: context.window.WorkbenchShell, location, context, values, events };
}
test('legacy pages canonicalize without losing bookmark filters or detail tabs', () => {
  const {shell} = harness();
  for (const [before,after] of [
    ['#library?q=sword&view=native', '#/202H/library?q=sword&view=native'],
    ['#native', '#/202H/library?view=native'],
    ['#files', '#/202H/settings'],
    ['#videos?video=7654572167790611743', '#/202H/videos?video=7654572167790611743'],
    ['#reviews?q=wobble&sku=Full+Set', '#/202H/reviews?q=wobble&sku=Full+Set'],
    ['#directions?product=SHARPAL+202H&direction=202H-S1&archive=all', '#/202H/directions?direction=202H-S1&archive=all'],
    ['#ai/task-123', '#/202H/ai/task-123'],
    ['#case/PVR-0123456789ab/history', '#/129N/case/PVR-0123456789ab/history'],
    ['#case/PVR-0123456789ab/analysis', '#/129N/case/PVR-0123456789ab/script'],
    ['#/129N/case/PVR-0123456789ab/analysis', '#/129N/case/PVR-0123456789ab/script'],
    ['#case/PVR-0123456789ab/data', '#/129N/case/PVR-0123456789ab/data'],
    ['#/202H/case/PVR-0123456789ab/history', '#/129N/case/PVR-0123456789ab/history'],
    ['#/all/case/PVR-0123456789ab/history', '#/all/case/PVR-0123456789ab/history'],
  ]) assert.equal(shell.url(before), after);
});
test('explicit canonical product wins over stale query and is restored after reload', () => {
  const h = harness('#/129N/reviews?product=202H&sku=Refill', '202H');
  h.shell.canonicalize();
  assert.equal(h.location.hash, '#/129N/reviews?sku=Refill');
  assert.equal(h.shell.product(), '129N');
  assert.equal(h.context.state.product, '129N');
  assert.equal(h.values.get('workbench-product-v1'), '129N');
  assert.equal(h.shell.legacyHash(), '#reviews?sku=Refill&product=129N');
  assert.equal(h.shell.legacyHash('#/129N/ai/task-123'), '#ai/task-123');
});
test('all-products selection is explicit and does not fall back to 202H', () => {
  const h = harness('#/all/library');
  h.shell.canonicalize();
  assert.equal(h.shell.product(), '');
  assert.equal(h.shell.url('#videos'), '#/all/videos');
  assert.equal(h.shell.matchesProduct({product:'Other'}), true);
  assert.equal(h.shell.legacyHash(), '#library');
});
test('product aliases match but longer model codes and missing products do not', () => {
  const {shell} = harness();
  assert.equal(shell.matchesProduct({product:'SHARPAL 202H Precision Knife Sharpening System'}), true);
  assert.equal(shell.matchesProduct('202h'), true);
  assert.equal(shell.matchesProduct('1202H'), false);
  assert.equal(shell.matchesProduct('202H2'), false);
  assert.equal(shell.matchesProduct({product:''}), false);
  assert.equal(shell.matchesProduct('129N'), false);
});
test('product switches leave detail pages and clear product-specific filters only', () => {
  const h = harness('#/202H/reviews?q=wobble&video=7654572167790611743&sku=Set&nature=question');
  h.shell.setProduct('129N');
  assert.equal(h.location.hash, '#/129N/reviews?q=wobble&nature=question');
  h.location.hash='#/202H/case/PVR-0123456789ab/history';
  h.shell.setProduct('129N');
  assert.equal(h.location.hash, '#/129N/library');
  h.location.hash='#/202H/directions/202H-S1?point=202H-S1&archive=all';
  h.shell.setProduct('129N');
  assert.equal(h.location.hash, '#/129N/directions?archive=all');
});
test('malformed URLs recover safely and UTF-8 product names round trip', () => {
  const {shell} = harness();
  assert.equal(shell.url('#/%zz/reviews?q=a'), '#/202H/reviews?q=a');
  assert.equal(shell.url('#unknown'), '#/202H/library');
  assert.equal(shell.parse(shell.url('#library', '厨房工具')).product, '厨房工具');
});

test('switching product detaches saved filters before clearing its direction', () => {
  const h = harness('#/202H/library?view=custom-saved123&category=positive&direction=202H-S1&q=clamp');
  h.context.navigationItem = () => ({ filters: { baseView: 'reference-all', direction: '202H-S1', product: '202H' } });
  h.shell.setProduct('129N');
  assert.equal(h.location.hash, '#/129N/library?category=positive&q=clamp');
});

test('startup discovers products with one inventory request', async () => {
  const h = harness(), calls = [];
  h.context.document.querySelectorAll = () => [];
  h.context.api = async path => {calls.push(path); return {products: ['202H', '129N']};};
  h.shell.initialize();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['/api/products']);
});

test('an older running service keeps product discovery until it can restart', async () => {
  const h = harness(), calls = [];
  h.context.document.querySelectorAll = () => [];
  h.context.api = async path => {
    calls.push(path);
    if (path === '/api/products') throw Object.assign(new Error('old service'), {status: 404});
    return {};
  };
  h.shell.initialize();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(calls, ['/api/products', '/api/performance/videos', '/api/reviews/summary', '/api/direction-catalog', '/api/creation']);
});
