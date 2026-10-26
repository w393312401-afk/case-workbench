'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/shell.js'), 'utf8');

function harness(hash = '#/202H/content', session, listMemory) {
  const sessionValues = new Map(session === undefined ? [] : [['workbench-navigation-v1', session]]);
  const localValues = new Map(listMemory === undefined ? [] : [['workbench-list-filters-v1',listMemory]]);
  const location = {hash}, breadcrumb = {innerHTML:''};
  const mounted = {page:null}, snapshots = new Map();
  const window = {scrollY:0,dispatchEvent() {},addEventListener() {}};
  for (const [page, name] of Object.entries({content:'ContentFlow',ai:'CaseAI',production:'CaseProduction',videos:'CasePerformance',reviews:'CaseReviews',home:'WorkbenchHome'})) {
    window[name] = {captureListState:() => mounted.page === page ? snapshots.get(page) || null : null};
  }
  const context = vm.createContext({URLSearchParams,Map,Set,console,location,window,
    state:{cases:[],libraryHash:'#/202H/library'},
    history:{replaceState(_state,_title,next) {location.hash = next;}},
    localStorage:{getItem:key => localValues.get(key) ?? null,setItem:(key,value) => localValues.set(key,value)},
    sessionStorage:{getItem:key => sessionValues.get(key) ?? null,setItem:(key,value) => sessionValues.set(key,value)},
    document:{querySelector:selector => selector === '#breadcrumb' ? breadcrumb : selector === '#case-search' && mounted.page === 'library' ? {} : null,querySelectorAll:() => [],addEventListener() {}},
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
    if (mountedPage === 'library') context.state.libraryHash = location.hash;
    if (snapshot) snapshots.set(mountedPage,{product:incoming.product,...snapshot});
    const restored = shell.listState(incoming.page,incoming.product);
    const scroll = shell.finishNavigation();
    return {restored,scroll};
  }
  return {shell,go,location,window,context,mounted,snapshots,sessionValues,localValues};
}

test('all-products source survives concrete details, reload, and list-state restoration', () => {
  const h = harness('#/all/content');
  h.go('#/all/content','content',{q:'fixture',status:'adopted'});
  h.window.scrollY = 642;
  h.go('#/202H/content/SCR-fixture');
  assert.equal(h.shell.parentURL(),'#/all/content');
  const reloaded = harness('#/202H/content/SCR-fixture',h.sessionValues.get('workbench-navigation-v1'));
  reloaded.go('#/202H/content/SCR-fixture');
  assert.equal(reloaded.shell.parentURL(),'#/all/content');
  const result = reloaded.go('#/all/content','content');
  assert.equal(result.restored.q,'fixture');
  assert.equal(result.restored.status,'adopted');
  assert.equal(result.scroll,642);
  assert.equal(reloaded.shell.listState('content',''),null);
});

test('independent library classification survives cross-product details and reload', () => {
  const list = '#/202H/library?caseProduct=129N&view=custom-saved123&direction=129N-S1&q=knife';
  const detail = '#/129N/case/PVR-0123456789ab';
  const h = harness(list);
  h.go(list,'library');
  h.window.scrollY = 642;
  h.go(detail);
  assert.equal(h.shell.parentURL(),list);
  const reloaded = harness(detail,h.sessionValues.get('workbench-navigation-v1'));
  reloaded.go(detail);
  assert.equal(reloaded.shell.parentURL(),list);
  const result = reloaded.go(list,'library');
  assert.equal(result.scroll,642);
  assert.equal(reloaded.shell.product(),'202H');
  assert.equal(reloaded.shell.parse().params.get('caseProduct'),'129N');
});

test('returning to the library from another global product restores the same classification', () => {
  const list = '#/202H/library?caseProduct=129N&q=knife&direction=129N-S1';
  const h = harness(list);
  h.go(list,'library');
  h.window.scrollY = 408;
  h.go('#/303X/reviews','reviews',{filters:{q:'review only'},hash:'#/303X/reviews?q=review+only'});
  const remembered = h.shell.listURL('library');
  assert.equal(remembered,'#/303X/library?caseProduct=129N&q=knife&direction=129N-S1');
  const result = h.go(remembered,'library');
  assert.equal(result.scroll,408);
  assert.equal(h.shell.product(),'303X');
  assert.equal(h.shell.parse().params.get('caseProduct'),'129N');
  assert.equal(h.shell.listURL('reviews','303X'),'#/303X/reviews?q=review+only');
  assert.equal(h.shell.listURL('reviews','202H'),'#/202H/reviews');
});

test('independent library filters persist across a fresh visit under a new global product', () => {
  const h = harness('#/202H/library?caseProduct=129N&q=knife');
  h.go(h.location.hash,'library');
  h.window.scrollY = 510;
  h.go('#/202H/home');
  const memory = h.localValues.get('workbench-list-filters-v1');
  assert.equal(JSON.parse(memory).lists[0][0],'library:');
  const reloaded = harness('#/303X/library',undefined,memory);
  reloaded.go('#/303X/library','library');
  assert.equal(reloaded.location.hash,'#/303X/library?caseProduct=129N&q=knife');
  assert.equal(reloaded.shell.product(),'303X');
  const explicit = reloaded.go('#/303X/library?caseProduct=202H','library');
  assert.equal(reloaded.location.hash,'#/303X/library?caseProduct=202H');
  assert.equal(explicit.scroll,null);
});

test('changing the global product in case details returns to its independent library source', () => {
  const list = '#/202H/library?caseProduct=129N&view=custom-saved123&direction=129N-S1';
  const h = harness(list);
  h.go(list,'library');
  h.go('#/129N/case/PVR-0123456789ab');
  h.shell.setProduct('303X');
  assert.equal(h.location.hash,'#/303X/library?caseProduct=129N&view=custom-saved123&direction=129N-S1');
});

test('specific product contexts remain isolated while module switches restore their controls', () => {
  const h = harness();
  h.go('#/202H/content','content',{q:'202H only',status:'adopted'});
  h.window.scrollY = 430;
  h.go('#/129N/content','content',{q:'129N only',status:'draft'});
  h.window.scrollY = 240;
  const first = h.go('#/202H/content','content');
  assert.equal(first.restored.q,'202H only');
  assert.equal(first.scroll,430);
  h.snapshots.set('content',first.restored);
  h.window.scrollY = first.scroll;
  const second = h.go('#/129N/content','content');
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
  h.go('#/202H/content','content',{q:'fixture',status:'adopted'});
  h.window.scrollY = 702;
  h.go('#/202H/content/SCR-fixture');
  h.window.scrollY = 85;
  h.go('#/202H/content/SCR-fixture');
  h.location.hash = '#/202H/content';
  h.shell.beginNavigation(h.shell.parse());
  h.mounted.page = null;
  h.location.hash = '#/202H/content/SCR-fixture';
  h.shell.beginNavigation(h.shell.parse());
  assert.equal(h.shell.finishNavigation(),null);
  assert.equal(h.shell.parentURL(),'#/202H/content');
  const result = h.go('#/202H/content','content');
  assert.equal(result.scroll,702);
  assert.equal(result.restored.q,'fixture');
});

test('invalid session URLs, mismatched products, and external case parent URLs fall back safely', () => {
  const serialized = JSON.stringify({version:2,lists:[
    ['content:202H',{hash:'https://example.com',scroll:30,state:{q:'external'}}],
    ['content:129N',{hash:'#/202H/content',scroll:40,state:{q:'wrong product'}}],
  ],details:[
    ['content:202H:SCR-fixture',{hash:'#/129N/content',scroll:50,state:{q:'wrong source'}}],
    ['content:202H:SCR-external',{hash:'javascript:alert(1)',scroll:30}],
  ]});
  const h = harness('#/202H/content/SCR-fixture',serialized);
  h.go('#/202H/content/SCR-fixture');
  assert.equal(h.shell.parentURL(),'#/202H/content');
  assert.equal(h.shell.listURL('content','129N'),'#/129N/content');
  h.context.state.libraryHash = 'https://example.com';
  assert.equal(h.shell.parentURL(h.shell.parse('#/202H/case/PVR-0123456789ab')),'#/202H/library');
});

test('stale mounted-product controls are not captured for a newly requested product', () => {
  const h = harness();
  h.go('#/202H/content','content',{q:'202H only',status:'adopted'});
  h.window.scrollY = 510;
  h.go('#/129N/content','content');
  // The old product's controls remain visible while the new list is still loading.
  h.window.scrollY = 90;
  h.go('#/202H/home');
  const result = h.go('#/129N/content','content');
  assert.equal(result.restored,null);
  assert.equal(result.scroll,null);
});

test('related detail versions inherit the all-products source list', () => {
  const h = harness('#/all/content');
  h.go('#/all/content','content',{q:'fixture',status:'adopted'});
  h.window.scrollY = 615;
  h.go('#/202H/content/SCR-newer');
  h.go('#/202H/content/SCR-older');
  assert.equal(h.shell.parentURL(),'#/all/content');
  const result = h.go('#/all/content','content');
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

test('a direct content detail returns to content and hides the duplicate top action', () => {
  const h = harness('#/202H/content/SCR-fixture');
  h.go('#/202H/content/SCR-fixture');
  assert.equal(h.shell.parentURL(), '#/202H/content');
  const button = {hidden:false};
  const query = h.context.document.querySelector;
  h.context.document.querySelector = selector => selector === '#add-top' ? button : query(selector);
  h.shell.update();
  assert.equal(button.hidden,true);
  assert.match(query('#breadcrumb').innerHTML,/采用稿/);
  assert.doesNotMatch(query('#breadcrumb').innerHTML,/脚本版本/);
});

test('production details return to the unified content list with its product and filters after reload', () => {
  const h = harness('#/all/content?view=all&kind=video&status=archived');
  h.go('#/all/content?view=all&kind=video&status=archived','content',{hash:'#/all/content?view=all&kind=video&status=archived'});
  h.window.scrollY = 524;
  h.go('#/202H/production/PRD-fixture');
  assert.equal(h.shell.parentURL(),'#/all/content?view=all&kind=video&status=archived');
  const reloaded = harness('#/202H/production/PRD-fixture',h.sessionValues.get('workbench-navigation-v1'));
  reloaded.go('#/202H/production/PRD-fixture');
  assert.equal(reloaded.shell.parentURL(),'#/all/content?view=all&kind=video&status=archived');
  const result = reloaded.go('#/all/content?view=all&kind=video&status=archived','content');
  assert.equal(result.scroll,524);
  assert.equal(result.restored.hash,'#/all/content?view=all&kind=video&status=archived');
  const direct = harness('#/129N/production/CAR-fixture');
  direct.go('#/129N/production/CAR-fixture');
  assert.equal(direct.shell.parentURL(),'#/129N/content');
});

test('collaboration records return to the list that opened them across reloads', () => {
  for (const parent of ['#/all/content?view=running','#/all/ai']) {
    const h = harness(parent), page = h.shell.parse().page;
    h.go(parent,page,{hash:parent,filter:'active'});
    h.go('#/202H/ai/AI-fixture');
    assert.equal(h.shell.parentURL(),parent);
    const reloaded = harness('#/202H/ai/AI-fixture',h.sessionValues.get('workbench-navigation-v1'));
    reloaded.go('#/202H/ai/AI-fixture');
    assert.equal(reloaded.shell.parentURL(),parent);
  }
});
