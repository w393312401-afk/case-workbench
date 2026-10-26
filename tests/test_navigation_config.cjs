'use strict';

// Pure Node regressions: execute the shipped scripts with small DOM/storage
// adapters, never opening a browser or the user's case library.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const storageKey = 'case-library-navigation-v1';
const readScript = name => fs.readFileSync(path.join(__dirname, '../static', name), 'utf8');
const navigationSource = readScript('navigation.js');
const librarySource = readScript('library.js');
const appSource = readScript('app.js');
const plain = value => JSON.parse(JSON.stringify(value));
const ids = rows => Array.from(rows, row => row.case_id);

function appFunction(name) {
  let start = appSource.indexOf(`function ${name}(`);
  const end = appSource.indexOf('\n}', start) + 2;
  assert.ok(start >= 0 && end > start, `Load shipped ${name} implementation`);
  if (appSource.slice(start - 6, start) === 'async ') start -= 6;
  return appSource.slice(start, end);
}

class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.dataset = {};
    this.children = [];
    this.listeners = new Map();
    this.queries = new Map();
    this.innerHTML = '';
    this.textContent = '';
    this.open = false;
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, listener) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(listener);
  }
  emit(name, event) {
    for (const listener of this.listeners.get(name) || []) listener(event);
  }
  querySelector(selector) {
    if (!this.queries.has(selector)) this.queries.set(selector, new Element());
    return this.queries.get(selector);
  }
  querySelectorAll(selector) {
    if (selector === '[data-close]') {
      if (!this.closeButtons) this.closeButtons = [new Element('button'), new Element('button')];
      return this.closeButtons;
    }
    return [];
  }
  showModal() { this.open = true; }
  close() { this.open = false; }
  scrollIntoView() {}
  focus() {}
  select() {}
}

function harness(options = {}) {
  const storage = options.storage || new Map();
  if (Object.hasOwn(options, 'stored')) storage.set(storageKey, options.stored);
  const nav = new Element('nav'), sidebar = new Element(), body = new Element('body');
  const created = [], window = new Element('window');
  const controls = { failWrite: false, failRequest: null, writes: 0, renders: 0, refreshes: 0, requests: [], toasts: [] };
  const serverNavigation = { ...options.navigation };
  const serverCases = structuredClone(options.cases || []);
  const state = {
    view: 'all', navigation: { ...serverNavigation }, cases: structuredClone(serverCases), routeKind: '',
    query: '', product: '', libraryProduct: '', archiveCategory: 'all', sourceType: 'all',
    sort: 'updated', sortDir: 'desc', selection: new Set(),
  };
  const context = vm.createContext({
    state, structuredClone, URLSearchParams, console, mutationSequence: 0,
    crypto: { randomUUID: () => '00000000-0000-4000-8000-000000000001' },
    location: { hash: '#library' }, window,
    document: {
      body,
      createElement(tag) { const element = new Element(tag); created.push(element); return element; },
    },
    localStorage: {
      getItem(key) { return storage.has(key) ? storage.get(key) : null; },
      setItem(key, value) {
        if (controls.failWrite) throw new Error('QuotaExceededError');
        controls.writes++;
        storage.set(key, String(value));
      },
    },
    $(selector) {
      if (selector === 'nav[aria-label="案例分类"]') return nav;
      if (selector === '.sidebar-bottom') return sidebar;
      return created.find(element => selector === `#${element.id}`) || null;
    },
    $$(selector, root) { return root?.querySelectorAll(selector) || []; },
    esc: value => String(value ?? '').replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    })[c]),
    icon: () => '<svg></svg>', updateNav() {},
    toast(message, error = false) { controls.toasts.push({ message, error }); },
    async api(url, request = {}) {
      controls.requests.push({ url, ...plain(request) });
      if (controls.failRequest === url) throw new Error('server write failed');
      if (url === '/api/navigation/move') {
        const { case_id: id, target, previous } = request.body;
        assert.equal(previous, serverNavigation[id] ?? null, 'move checks the previous server assignment');
        serverNavigation[id] = target;
        return { target, navigation: { ...serverNavigation } };
      }
      const match = url.match(/^\/api\/cases\/([^/]+)(\/archive)?$/);
      assert.ok(match, `Unexpected request: ${url}`);
      const item = serverCases.find(row => row.case_id === match[1]);
      assert.ok(item, `Unknown case: ${match[1]}`);
      if (match[2]) {
        item.archived = request.body.archived;
        item.revision = 'revision-after-archive';
        return { case: structuredClone(item) };
      }
      return { revision: 'revision-before-move', ...structuredClone(item) };
    },
    async refresh() {
      controls.refreshes++;
      state.navigation = { ...serverNavigation };
      state.cases = structuredClone(serverCases);
    },
    libraryHash: () => '#library', renderDetail() {},
  });
  vm.runInContext(['group', 'sourceType', 'productFilterGroup', 'shortTitle', 'numeric', 'moveCaseToView', 'inNavigation', 'libraryProductMatches', 'scopedCases'].map(appFunction).join('\n'), context);
  vm.runInContext(navigationSource, context, { filename: 'navigation.js' });
  vm.runInContext(librarySource, context, { filename: 'library.js' });
  const api = vm.runInContext(`({
    initializeNavigation, normalizeNavigation, navigationDefaults, navigationMatches,
    navigationContent, navigationLabel, navigationDescription, isNavigationView,
    navigationMoveChoices, saveNavigation, navigationAssignment, navigationCollectionCaseIds, filteredCases,
    moveControl, moveCaseToView, group, get items() { return navigationItems; },
    get baseline() { return navigationStoredValue; }
  })`, context);
  api.initializeNavigation();
  const dialog = created.find(element => element.id === 'navigation-dialog');
  const button = created.find(element => element.id === 'navigation-settings');
  return {
    api, state, storage, controls, nav, dialog, window, serverNavigation, serverCases,
    error: () => dialog.querySelector('.navigation-error').textContent,
    open: () => button.onclick(),
    cancel: () => dialog.querySelectorAll('[data-close]')[1].onclick(),
    submit: () => dialog.querySelector('form').onsubmit({ preventDefault() {} }),
    input(id, attr, value, checked = false) {
      const row = new Element(); row.dataset.id = id;
      const target = new Element('input');
      Object.assign(target, { value, checked });
      target.dataset.caseId = value;
      target.closest = selector => selector === '[data-id]' ? row : null;
      target.matches = selector => selector === `[data-${attr}]`;
      dialog.emit('input', { target });
    },
    external(value, notify = true) {
      storage.set(storageKey, JSON.stringify(value));
      if (notify) window.emit('storage', { key: storageKey });
    },
  };
}

function column(id, content, overrides = {}) {
  return { id, content, label: id, visible: true, ...overrides };
}
function record(number, overrides = {}) {
  return {
    case_id: `PVR-${number.toString(16).padStart(12, '0')}`,
    product: 'SHARPAL 202H', search_text: 'Sharp EDGE',
    updated_at: '2026-09-30T00:00:00Z', archived: false,
    current: { title: 'Sharpener fixture', category: '待验证与整理' },
    ...overrides,
  };
}

test('old preferences retain names, ordering, visibility and deletion while gaining default content', () => {
  const legacy = [
    { id: 'native', label: '达人内容参考', icon: 'file', visible: true },
    { id: 'legacy', label: '旧素材', icon: 'folder', deleted: true },
    { id: 'positive', label: '自制内容参考', icon: 'spark', visible: false },
  ];
  const h = harness({ stored: JSON.stringify(legacy) });
  const items = plain(h.api.items);
  assert.deepEqual(items.slice(0, 3).map(item => item.id), ['native', 'legacy', 'positive']);
  assert.equal(items[0].label, '达人内容参考');
  assert.equal(items[0].content, 'native', 'renaming must not infer the maker');
  assert.equal(items[0].icon, 'file');
  assert.equal(items[1].deleted, true);
  assert.equal(items[2].visible, false);
  assert.equal(items[2].content, 'positive');
  assert.deepEqual(items[0].caseIds, []);
  assert.equal(items.length, 7);
  assert.equal(h.controls.writes, 0, 'merely loading does not rewrite preferences');
});

test('normalization rejects unsafe IDs and prototype names, deduplicates values and bounds text', () => {
  const h = harness();
  const validId = record(1).case_id;
  const result = plain(h.api.normalizeNavigation([
    null, 12, 'wrong', { id: '__proto__' }, { id: 'constructor' },
    { id: 'custom-"><script>alert(1)</script>' }, { id: 'custom-short' },
    { id: 'custom-valid123', label: '  ' + '名'.repeat(40), icon: '__proto__', content: 'constructor',
      description: 'd'.repeat(300), query: 'q'.repeat(300), product: 'p'.repeat(200),
      caseIds: [validId, validId, 'PVR-invalid', null, { case_id: validId }] },
    { id: 'custom-valid123', label: 'must not replace first entry' },
  ]));
  assert.equal(result.length, 8);
  assert.equal(result[0].id, 'custom-valid123');
  assert.equal(result[0].label, '名'.repeat(24));
  assert.equal(result[0].icon, 'folder');
  assert.equal(result[0].content, 'custom');
  assert.equal(result[0].description.length, 200);
  assert.equal(result[0].query.length, 200);
  assert.equal(result[0].product.length, 120);
  assert.deepEqual(result[0].caseIds, [validId]);
  assert.ok(result.every(item => item.id !== '__proto__'));
  const malformedFields = JSON.parse('[{"id":"custom-badfield1","icon":{"toString":null,"valueOf":null},"content":{"toString":null,"valueOf":null}},{"id":"positive","label":"保留名称"}]');
  const recovered = plain(h.api.normalizeNavigation(malformedFields));
  assert.equal(recovered[0].icon, 'folder');
  assert.equal(recovered[0].content, 'custom');
  assert.equal(recovered.find(item => item.id === 'positive').label, '保留名称', 'one malformed entry must not reset valid preferences');
  const hidden = h.api.normalizeNavigation(h.api.navigationDefaults().map(item => ({ ...item, visible: false, deleted: true })));
  assert.equal(hidden.filter(item => item.visible && !item.deleted).length, 1);
});

test('user labels are escaped in the actual sidebar and move-menu renderers', () => {
  const label = '<img src=x onerror=x>';
  const h = harness({ stored: JSON.stringify([column('custom-safety12', 'custom', { label })]) });
  const sidebar = h.nav.children.find(node => node.dataset.view === 'custom-safety12').innerHTML;
  const menu = h.api.moveControl(record(1));
  assert.match(sidebar, /&lt;img src=x onerror=x&gt;/);
  assert.doesNotMatch(sidebar, /<img/);
  assert.match(menu, />&lt;img src=x onerror=x&gt;<\/option>/);
  assert.doesNotMatch(menu, /收纳到：/);
  assert.doesNotMatch(menu, /<img/);
});

test('self-produced and creator views use actual origin independently of manual placement', () => {
  const own = record(1, { current: { category: '待验证与整理', content_origin: 'self_produced' } });
  const creator = record(2, { current: { category: '转化正向参考', content_origin: 'overseas_creator' } });
  const unknown = record(3, { current: { category: '待验证与整理', native_reference: { entries: [] }, source_case: { file_path: '/own_products/demo.md' } } });
  const archived = { ...creator, case_id: record(4).case_id, archived: true };
  const h = harness({ cases: [own, creator, unknown, archived], stored: JSON.stringify([
    column('positive', 'self_produced'), column('native', 'overseas_creator'),
  ]) });
  h.state.navigation = { [own.case_id]: 'native', [creator.case_id]: 'positive', [unknown.case_id]: 'native' };
  assert.deepEqual(ids(h.state.cases.filter(item => h.api.navigationMatches(item, 'positive'))), [own.case_id]);
  assert.deepEqual(ids(h.state.cases.filter(item => h.api.navigationMatches(item, 'native'))), [creator.case_id]);
  h.state.view = 'native';
  assert.deepEqual(ids(h.api.filteredCases()), [creator.case_id], 'library must not re-filter by the column ID');
});

test('custom collections exclude archived cases, while archived aliases still support group filters', () => {
  const active = record(1), archived = record(2, { archived: true });
  const other = record(3);
  const h = harness({ cases: [active, archived, other], stored: JSON.stringify([
    column('custom-collection1', 'custom', { caseIds: [active.case_id, archived.case_id] }),
    column('custom-archive12', 'archived'),
  ]) });
  h.state.navigation[active.case_id] = 'native';
  h.state.view = 'custom-collection1';
  assert.deepEqual(ids(h.api.filteredCases()), [active.case_id]);
  h.state.view = 'custom-archive12';
  assert.deepEqual(ids(h.api.filteredCases()), [archived.case_id]);
  h.state.archiveCategory = 'positive';
  assert.deepEqual(ids(h.api.filteredCases()), []);
  h.state.archiveCategory = 'pending';
  assert.deepEqual(ids(h.api.filteredCases()), [archived.case_id]);
  h.state.cases.find(item => item.case_id === archived.case_id).archived = false;
  h.state.view = 'custom-collection1';
  assert.deepEqual(ids(h.api.filteredCases()).sort(), [active.case_id, archived.case_id].sort());
});

test('saved product and keyword scope combines with current library filters', () => {
  const match = record(1), otherProduct = record(2, { product: 'SHARPAL 129N' });
  const otherText = record(3, { search_text: 'Kitchen scissors' });
  const legacy = record(4, { current: { category: '待验证与整理', source_case: { case_type: 'video_analysis' } } });
  const h = harness({ cases: [match, otherProduct, otherText, legacy], stored: JSON.stringify([
    column('custom-filters12', 'all', { product: 'SHARPAL 202H', query: 'edge' }),
  ]) });
  h.state.view = 'custom-filters12';
  assert.deepEqual(ids(h.api.filteredCases()).sort(), [match.case_id, legacy.case_id].sort());
  h.state.sourceType = 'video_analysis';
  assert.deepEqual(ids(h.api.filteredCases()), [legacy.case_id]);
  h.state.query = 'absent';
  assert.deepEqual(ids(h.api.filteredCases()), []);
  h.state.query = '';
  h.state.libraryProduct = 'SHARPAL 129N';
  assert.deepEqual(ids(h.api.filteredCases()), []);
});

test('move choices omit source filters, extra filters and unavailable columns', () => {
  const h = harness({ stored: JSON.stringify([
    column('positive', 'self_produced'), column('native', 'overseas_creator'),
    column('mixed', 'mixed', { product: 'SHARPAL 202H' }),
    column('pending', 'pending', { query: 'knife' }),
    column('legacy', 'legacy', { visible: false }),
    column('custom-deleted1', 'custom', { deleted: true }),
    column('custom-available1', 'custom'), column('custom-archive12', 'archived'),
  ]) });
  const choices = Array.from(h.api.navigationMoveChoices(), item => item.id);
  assert.deepEqual(choices.sort(), ['all', 'archived', 'custom-available1', 'custom-archive12'].sort());
  const activeMenu = h.api.moveControl(record(1));
  assert.match(activeMenu, /value="custom-archive12"/);
  const archivedMenu = h.api.moveControl(record(1, { archived: true }));
  assert.doesNotMatch(archivedMenu, /value="(?:archived|custom-archive12)"/);
  assert.match(archivedMenu, />custom-available1<\/option>/);
});

test('moving from a default view to a custom column changes ownership without copying or changing case content', async () => {
  const item = record(1, { current: { title: 'Example', category: '待验证与整理', native_reference: {}, content_origin: 'self_produced' } });
  const h = harness({ cases: [item], stored: JSON.stringify([
    column('custom-target12', 'custom'), column('positive', 'self_produced'),
  ]) });
  const storedBefore = h.storage.get(storageKey), contentBefore = JSON.stringify(h.state.cases);
  assert.equal(h.api.navigationMatches(item, 'pending'), true);
  assert.equal(h.api.navigationMatches(item, 'native'), true);
  assert.equal(h.api.navigationMatches(item, 'positive'), true);
  await h.api.moveCaseToView(item.case_id, 'custom-target12');
  assert.equal(h.state.navigation[item.case_id], 'column:custom-target12');
  assert.equal(h.api.navigationAssignment(item.case_id), 'custom-target12');
  for (const view of ['pending', 'native', 'positive']) assert.equal(h.api.navigationMatches(item, view), false, `Source ${view} must no longer show moved case`);
  assert.equal(h.api.navigationMatches(item, 'custom-target12'), true);
  assert.equal(h.api.navigationMatches(item, 'all'), true, 'all remains an overview rather than a second ownership');
  assert.equal(JSON.stringify(h.state.cases), contentBefore);
  assert.equal(h.storage.get(storageKey), storedBefore);
  assert.equal(h.controls.writes, 0, 'moving uses the server, not browser collection copies');
  assert.equal(h.controls.refreshes, 1);
  assert.equal(h.controls.toasts.at(-1).error, false);
  const request = h.controls.requests.find(request => request.url === '/api/navigation/move');
  assert.deepEqual(request.body, { case_id: item.case_id, target: 'column:custom-target12', revision: 'revision-before-move', previous: null });
  const reloaded = harness({ cases: h.serverCases, storage: h.storage, navigation: h.serverNavigation });
  assert.equal(reloaded.api.navigationMatches(item, 'custom-target12'), true);
  assert.equal(reloaded.api.navigationMatches(item, 'pending'), false, 'ownership survives reloading');
});

test('moving between custom and built-in columns suppresses every stale collection membership', async () => {
  const item = record(1);
  const h = harness({ cases: [item], stored: JSON.stringify([
    column('custom-first123', 'custom', { caseIds: [item.case_id] }),
    column('custom-second12', 'custom', { caseIds: [item.case_id] }),
    column('custom-third123', 'custom', { caseIds: [item.case_id] }),
  ]) });
  h.state.view = 'custom-first123';
  const bytesBefore = h.storage.get(storageKey);
  await h.api.moveCaseToView(item.case_id, 'custom-second12', '拖动');
  assert.equal(h.api.navigationMatches(item, 'custom-first123'), false);
  assert.equal(h.api.navigationMatches(item, 'custom-second12'), true);
  assert.equal(h.api.navigationMatches(item, 'custom-third123'), false);
  assert.deepEqual(ids(h.api.filteredCases()), [], 'source view immediately loses the moved case');
  await h.api.moveCaseToView(item.case_id, 'native');
  assert.equal(h.api.navigationMatches(item, 'native'), true);
  assert.equal(h.api.navigationMatches(item, 'pending'), false);
  for (const view of ['custom-first123', 'custom-second12', 'custom-third123']) {
    assert.equal(h.api.navigationMatches(item, view), false);
    const collection = h.api.items.find(column => column.id === view);
    assert.deepEqual(plain(h.api.navigationCollectionCaseIds(collection)), [], 'picker counts also exclude stale copies');
  }
  assert.equal(h.api.group(item), 'pending', 'placement does not replace the performance category');
  assert.equal(h.storage.get(storageKey), bytesBefore, 'legacy collections remain recoverable but cannot override new ownership');
  assert.deepEqual(h.controls.requests.filter(request => request.url === '/api/navigation/move').map(request => request.body.previous), [null, 'column:custom-second12']);
});

test('moving to all removes appearances in other columns while keeping one overview entry', async () => {
  const item = record(1);
  const h = harness({ cases: [item], navigation: { [item.case_id]: 'column:custom-source12' }, stored: JSON.stringify([
    column('custom-source12', 'custom', { caseIds: [item.case_id] }),
    column('custom-legacy12', 'custom', { caseIds: [item.case_id] }),
  ]) });
  await h.api.moveCaseToView(item.case_id, 'all');
  assert.equal(h.state.navigation[item.case_id], 'column:all');
  for (const view of ['custom-source12', 'custom-legacy12', 'pending']) assert.equal(h.api.navigationMatches(item, view), false);
  h.state.view = 'all';
  assert.deepEqual(ids(h.api.filteredCases()), [item.case_id]);
});

test('archived cases are restored before moving to a custom column and use the new revision', async () => {
  const item = record(1, { archived: true });
  const h = harness({ cases: [item], navigation: { [item.case_id]: 'column:pending' }, stored: JSON.stringify([column('custom-restored1', 'custom')]) });
  await h.api.moveCaseToView(item.case_id, 'custom-restored1');
  assert.deepEqual(h.controls.requests.map(request => request.url), [`/api/cases/${item.case_id}`, `/api/cases/${item.case_id}/archive`, '/api/navigation/move']);
  assert.equal(h.controls.requests[1].body.archived, false);
  assert.equal(h.controls.requests[2].body.revision, 'revision-after-archive');
  const active = h.state.cases[0];
  assert.equal(active.archived, false);
  assert.equal(h.api.navigationMatches(active, 'custom-restored1'), true);
  assert.equal(h.api.navigationMatches(active, 'pending'), false);
  assert.equal(h.api.navigationMatches(active, 'archived'), false);
});

test('moving to archive changes lifecycle only and restores the existing column when unarchived', async () => {
  const item = record(1);
  const h = harness({ cases: [item], navigation: { [item.case_id]: 'column:custom-source12' }, stored: JSON.stringify([column('custom-source12', 'custom')]) });
  await h.api.moveCaseToView(item.case_id, 'archived');
  assert.equal(h.controls.requests.some(request => request.url === '/api/navigation/move'), false);
  assert.equal(h.state.navigation[item.case_id], 'column:custom-source12');
  const archived = h.state.cases[0];
  assert.equal(h.api.navigationMatches(archived, 'archived'), true);
  assert.equal(h.api.navigationMatches(archived, 'custom-source12'), false);
  assert.equal(h.api.navigationMatches(archived, 'all'), false);
  archived.archived = false;
  assert.equal(h.api.navigationMatches(archived, 'custom-source12'), true);
  assert.equal(h.api.navigationMatches(archived, 'pending'), false);
});

test('failed server movement leaves ownership and browser collections unchanged and reports no success', async () => {
  const item = record(1);
  const h = harness({ cases: [item], navigation: { [item.case_id]: 'column:custom-source12' }, stored: JSON.stringify([
    column('custom-source12', 'custom', { caseIds: [item.case_id] }), column('custom-target12', 'custom'),
  ]) });
  const before = h.storage.get(storageKey), itemsBefore = JSON.stringify(h.api.items);
  h.controls.failRequest = '/api/navigation/move';
  await h.api.moveCaseToView(item.case_id, 'custom-target12');
  assert.equal(h.state.navigation[item.case_id], 'column:custom-source12');
  assert.equal(h.api.navigationMatches(item, 'custom-source12'), true);
  assert.equal(h.api.navigationMatches(item, 'custom-target12'), false);
  assert.equal(h.storage.get(storageKey), before);
  assert.equal(JSON.stringify(h.api.items), itemsBefore);
  assert.equal(h.controls.writes, 0);
  assert.equal(h.controls.refreshes, 1);
  assert.equal(h.state.moving, false, 'failed request does not leave movement locked');
  assert.equal(h.controls.toasts.length, 1);
  assert.equal(h.controls.toasts[0].error, true);
  assert.match(h.controls.toasts[0].message, /移动未完成：server write failed/);
});

test('a failed move after restoring reports the partial outcome without inventing target ownership', async () => {
  const item = record(1, { archived: true });
  const h = harness({ cases: [item], navigation: { [item.case_id]: 'column:pending' }, stored: JSON.stringify([column('custom-target12', 'custom')]) });
  h.controls.failRequest = '/api/navigation/move';
  await h.api.moveCaseToView(item.case_id, 'custom-target12');
  assert.equal(h.state.cases[0].archived, false);
  assert.equal(h.state.navigation[item.case_id], 'column:pending');
  assert.equal(h.api.navigationMatches(h.state.cases[0], 'custom-target12'), false);
  assert.equal(h.controls.toasts.length, 1);
  assert.equal(h.controls.toasts[0].error, true);
  assert.match(h.controls.toasts[0].message, /案例已恢复，但栏目未调整：/);
});

test('collection picker includes new ownership, excludes stale copies, and cannot override a move', () => {
  const assignedHere = record(1), assignedAway = record(2), legacy = record(3), unassigned = record(4);
  const h = harness({ cases: [assignedHere, assignedAway, legacy, unassigned], navigation: {
    [assignedHere.case_id]: 'column:custom-picker12', [assignedAway.case_id]: 'column:native',
  }, stored: JSON.stringify([column('custom-picker12', 'custom', { caseIds: [assignedAway.case_id, legacy.case_id] })]) });
  const collection = h.api.items.find(item => item.id === 'custom-picker12');
  assert.deepEqual(plain(h.api.navigationCollectionCaseIds(collection)).sort(), [assignedHere.case_id, legacy.case_id].sort());
  h.open();
  const picker = h.dialog.querySelector('[data-id="custom-picker12"] [data-case-picker]');
  const inputFor = id => picker.innerHTML.match(new RegExp(`<input[^>]+data-case-id="${id}"[^>]*>`))?.[0];
  assert.match(inputFor(assignedHere.case_id), /checked/);
  assert.match(inputFor(assignedHere.case_id), /disabled/);
  assert.doesNotMatch(inputFor(assignedAway.case_id), /checked/);
  assert.match(inputFor(assignedAway.case_id), /disabled/);
  assert.match(inputFor(legacy.case_id), /checked/);
  assert.doesNotMatch(inputFor(legacy.case_id), /disabled/);
  assert.doesNotMatch(inputFor(unassigned.case_id), /checked|disabled/);
  const before = plain(collection.caseIds);
  h.input('custom-picker12', 'case-id', assignedHere.case_id, false);
  h.input('custom-picker12', 'case-id', assignedAway.case_id, true);
  h.submit();
  assert.deepEqual(plain(h.api.items.find(item => item.id === 'custom-picker12').caseIds), before);
  assert.equal(h.api.navigationMatches(assignedHere, 'custom-picker12'), true);
  assert.equal(h.api.navigationMatches(assignedAway, 'custom-picker12'), false);
});

test('failed writes preserve both saved definitions and stored bytes, and the dialog can retry', () => {
  const h = harness({ stored: JSON.stringify([column('custom-retry123', 'custom')]) });
  const before = JSON.stringify(h.api.items), raw = h.storage.get(storageKey);
  h.controls.failWrite = true;
  assert.throws(() => h.api.saveNavigation(h.api.items), /QuotaExceededError/);
  assert.equal(JSON.stringify(h.api.items), before);
  assert.equal(h.storage.get(storageKey), raw);
  assert.equal(h.api.baseline, raw);
  h.open();
  h.input('custom-retry123', 'label', '重试后的栏目');
  h.submit();
  assert.equal(h.dialog.open, true);
  assert.match(h.error(), /浏览器未能保存/);
  assert.equal(JSON.stringify(h.api.items), before);
  h.controls.failWrite = false;
  h.submit();
  assert.equal(h.dialog.open, false);
  assert.equal(h.api.navigationLabel('custom-retry123'), '重试后的栏目');
});

test('cancel discards nested collection changes and text edits without persisting any draft', () => {
  const first = record(1), second = record(2);
  const h = harness({ cases: [first, second], stored: JSON.stringify([
    column('custom-draft123', 'custom', { description: '原说明', caseIds: [first.case_id] }),
  ]) });
  const raw = h.storage.get(storageKey), before = JSON.stringify(h.api.items);
  h.open();
  h.input('custom-draft123', 'description', '取消的说明');
  h.input('custom-draft123', 'case-id', first.case_id, false);
  h.input('custom-draft123', 'case-id', second.case_id, true);
  h.cancel();
  assert.equal(h.dialog.open, false);
  assert.equal(h.controls.writes, 0);
  assert.equal(h.storage.get(storageKey), raw);
  assert.equal(JSON.stringify(h.api.items), before);
  h.open();
  h.submit();
  assert.equal(h.api.navigationDescription('custom-draft123'), '原说明');
  assert.deepEqual(plain(h.api.items.find(item => item.id === 'custom-draft123').caseIds), [first.case_id]);
});

test('unnotified concurrent changes cannot be overwritten by collection or dialog saves', () => {
  const h = harness({ stored: JSON.stringify([column('custom-conflict1', 'custom')]) });
  h.open();
  h.input('custom-conflict1', 'label', '旧页面草稿');
  const external = [column('custom-conflict1', 'custom', { label: '另一页面已保存' })];
  h.external(external, false);
  const raw = h.storage.get(storageKey), before = JSON.stringify(h.api.items);
  assert.throws(() => h.api.saveNavigation(h.api.items), /其他页面/);
  h.submit();
  assert.equal(h.dialog.open, true);
  assert.match(h.error(), /其他页面/);
  assert.equal(h.controls.writes, 0);
  assert.equal(h.storage.get(storageKey), raw);
  assert.equal(JSON.stringify(h.api.items), before);
  h.cancel(); h.open(); h.submit();
  assert.equal(h.api.navigationLabel('custom-conflict1'), '另一页面已保存');
});

test('storage events preserve an open draft but require reopening against the fresh version', () => {
  const h = harness({ stored: JSON.stringify([column('custom-storage1', 'custom')]) });
  h.open();
  h.input('custom-storage1', 'label', '当前未保存');
  h.external([column('custom-storage1', 'custom', { label: '外部新名称', caseIds: [record(9).case_id] })]);
  assert.equal(h.dialog.open, true);
  assert.match(h.error(), /当前草稿已保留/);
  h.submit();
  assert.equal(h.controls.writes, 0);
  assert.match(h.error(), /重新打开/);
  assert.equal(h.api.navigationLabel('custom-storage1'), '外部新名称');
  h.cancel(); h.open(); h.submit();
  assert.deepEqual(plain(h.api.items.find(item => item.id === 'custom-storage1').caseIds), [record(9).case_id]);
  assert.equal(h.api.navigationLabel('custom-storage1'), '外部新名称');
});

test('malformed saved JSON recovers to defaults without writing until the user saves', () => {
  const h = harness({ stored: '{broken' });
  assert.equal(h.api.items.length, 7);
  assert.equal(h.controls.writes, 0);
  assert.equal(h.api.isNavigationView('all'), true);
  h.open(); h.submit();
  assert.equal(JSON.parse(h.storage.get(storageKey)).length, 7);
});
