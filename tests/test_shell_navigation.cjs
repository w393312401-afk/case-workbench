'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/shell.js'), 'utf8');

function harness(hash = '#/202H/scripts', session) {
  const sessionValues = new Map(session === undefined ? [] : [['workbench-navigation-v1', session]]);
  const location = {hash}, breadcrumb = {innerHTML:''};
  const mounted = {page:null}, snapshots = new Map();
  const window = {scrollY:0,dispatchEvent() {}};
  for (const [page, name] of Object.entries({scripts:'CaseScripts',ai:'CaseAI',production:'CaseProduction',videos:'CasePerformance',reviews:'CaseReviews',home:'WorkbenchHome'})) {
    window[name] = {captureListState:() => mounted.page === page ? snapshots.get(page) || null : null};
  }
  const context = vm.createContext({URLSearchParams,Map,Set,console,location,window,
    state:{cases:[],libraryHash:'#/202H/library'},
    history:{replaceState(_state,_title,next) {location.hash = next;}},
    localStorage:{getItem:() => null,setItem() {}},
    sessionStorage:{getItem:key => sessionValues.get(key) ?? null,setItem:(key,value) => sessionValues.set(key,value)},
    document:{querySelector:selector => selector === '#breadcrumb' ? breadcrumb : null,querySelectorAll:() => []},
    MutationObserver:class {observe() {}},
    CustomEvent:class {constructor(type,options) {this.type = type;this.detail = options.detail;}},
    esc:value => String(value).replace(/[&<>"']/g,char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
    api:async () => ({products:[]}),
  });
  vm.runInContext(source,context);
  const shell = window.WorkbenchShell;
  shell.initialize();
  function go(next, mountedPage = null, snapshot) {
    location.hash = next;
    const incoming = shell.parse();
    shell.beginNavigation(incoming);
    shell.canonicalize();
    mounted.page = mountedPage;
    if (snapshot) snapshots.set(mountedPage,{product:incoming.product,...snapshot});
    const restored = shell.listState(incoming.page,incoming.product);
    const scroll = shell.finishNavigation();
    return {restored,scroll};
  }
  return {shell,go,location,window,context,mounted,snapshots,sessionValues};
}

test('all-products source survives concrete details, reload, and list-state restoration', () => {
  const h = harness('#/all/scripts');
  h.go('#/all/scripts','scripts',{q:'fixture',status:'adopted'});
  h.window.scrollY = 642;
  h.go('#/202H/scripts/SCR-fixture');
  assert.equal(h.shell.parentURL(),'#/all/scripts');
  const reloaded = harness('#/202H/scripts/SCR-fixture',h.sessionValues.get('workbench-navigation-v1'));
  reloaded.go('#/202H/scripts/SCR-fixture');
  assert.equal(reloaded.shell.parentURL(),'#/all/scripts');
  const result = reloaded.go('#/all/scripts','scripts');
  assert.equal(result.restored.q,'fixture');
  assert.equal(result.restored.status,'adopted');
  assert.equal(result.scroll,642);
  assert.equal(reloaded.shell.listState('scripts',''),null);
});

test('specific product contexts remain isolated while module switches restore their controls', () => {
  const h = harness();
  h.go('#/202H/scripts','scripts',{q:'202H only',status:'adopted'});
  h.window.scrollY = 430;
  h.go('#/129N/scripts','scripts',{q:'129N only',status:'draft'});
  h.window.scrollY = 240;
  const first = h.go('#/202H/scripts','scripts');
  assert.equal(first.restored.q,'202H only');
  assert.equal(first.scroll,430);
  h.snapshots.set('scripts',first.restored);
  h.window.scrollY = first.scroll;
  const second = h.go('#/129N/scripts','scripts');
  assert.equal(second.restored.q,'129N only');
  assert.equal(second.scroll,240);
});

test('an explicit filtered URL does not inherit an incompatible saved list snapshot', () => {
  const h = harness('#/202H/reviews?q=old');
  h.go('#/202H/reviews?q=old','reviews',{filters:{q:'old'},hash:'#/202H/reviews?q=old'});
  h.window.scrollY = 310;
  h.go('#/202H/home');
  const explicit = h.go('#/202H/reviews?q=new','reviews');
  assert.equal(h.location.hash,'#/202H/reviews?q=new');
  assert.equal(explicit.restored,null);
  assert.equal(explicit.scroll,null);
});

test('captured current list URL takes precedence over the original linked URL', () => {
  const h = harness('#/202H/reviews?q=old');
  h.go('#/202H/reviews?q=old','reviews',{filters:{q:'current'},hash:'#/202H/reviews?q=current'});
  h.window.scrollY = 250;
  h.go('#/202H/home');
  assert.equal(h.shell.listURL('reviews'),'#/202H/reviews?q=current');
  const result = h.go('#/202H/reviews?q=current','reviews');
  assert.equal(result.restored.filters.q,'current');
  assert.equal(result.scroll,250);
});

test('detail refreshes and a fast transition through an unmounted list do not replace source scroll', () => {
  const h = harness();
  h.go('#/202H/scripts','scripts',{q:'fixture',status:'adopted'});
  h.window.scrollY = 702;
  h.go('#/202H/scripts/SCR-fixture');
  h.window.scrollY = 85;
  h.go('#/202H/scripts/SCR-fixture');
  h.location.hash = '#/202H/scripts';
  h.shell.beginNavigation(h.shell.parse());
  h.mounted.page = null;
  h.location.hash = '#/202H/scripts/SCR-fixture';
  h.shell.beginNavigation(h.shell.parse());
  assert.equal(h.shell.finishNavigation(),null);
  assert.equal(h.shell.parentURL(),'#/202H/scripts');
  const result = h.go('#/202H/scripts','scripts');
  assert.equal(result.scroll,702);
  assert.equal(result.restored.q,'fixture');
});

test('invalid session URLs, mismatched products, and external case parent URLs fall back safely', () => {
  const serialized = JSON.stringify({version:2,lists:[
    ['scripts:202H',{hash:'https://example.com',scroll:30,state:{q:'external'}}],
    ['scripts:129N',{hash:'#/202H/scripts',scroll:40,state:{q:'wrong product'}}],
  ],details:[
    ['scripts:202H:SCR-fixture',{hash:'#/129N/scripts',scroll:50,state:{q:'wrong source'}}],
    ['scripts:202H:SCR-external',{hash:'javascript:alert(1)',scroll:30}],
  ]});
  const h = harness('#/202H/scripts/SCR-fixture',serialized);
  h.go('#/202H/scripts/SCR-fixture');
  assert.equal(h.shell.parentURL(),'#/202H/scripts');
  assert.equal(h.shell.listURL('scripts','129N'),'#/129N/scripts');
  h.context.state.libraryHash = 'https://example.com';
  assert.equal(h.shell.parentURL(h.shell.parse('#/202H/case/PVR-0123456789ab')),'#/202H/library');
});

test('stale mounted-product controls are not captured for a newly requested product', () => {
  const h = harness();
  h.go('#/202H/scripts','scripts',{q:'202H only',status:'adopted'});
  h.window.scrollY = 510;
  h.go('#/129N/scripts','scripts');
  // The old product's controls remain visible while the new list is still loading.
  h.window.scrollY = 90;
  h.go('#/202H/home');
  const result = h.go('#/129N/scripts','scripts');
  assert.equal(result.restored,null);
  assert.equal(result.scroll,null);
});

test('related detail versions inherit the all-products source list', () => {
  const h = harness('#/all/scripts');
  h.go('#/all/scripts','scripts',{q:'fixture',status:'adopted'});
  h.window.scrollY = 615;
  h.go('#/202H/scripts/SCR-newer');
  h.go('#/202H/scripts/SCR-older');
  assert.equal(h.shell.parentURL(),'#/all/scripts');
  const result = h.go('#/all/scripts','scripts');
  assert.equal(result.restored.q,'fixture');
  assert.equal(result.scroll,615);
});

test('legacy direction sessions migrate the all-products source into current detail context', () => {
  const serialized = JSON.stringify([['202H',{hash:'#/all/directions?tier=S',scroll:320}]]);
  const h = harness('#/202H/directions/202H-S1',serialized);
  h.go('#/202H/directions/202H-S1');
  assert.equal(h.shell.parentURL(),'#/all/directions?tier=S');
  const result = h.go('#/all/directions?tier=S');
  assert.equal(result.scroll,320);
});
