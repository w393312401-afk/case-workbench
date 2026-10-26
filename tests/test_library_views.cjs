'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

function fixture(items, views = []) {
  const state = {cases:items,navigation:{},view:'all',query:'',product:'',libraryProduct:'',direction:'',variantType:'all',archiveCategory:'all',sourceType:'all',contentOrigin:'all',archiveStatus:'active',sort:'updated',sortDir:'desc'};
  const context=vm.createContext({window:{},state,structuredClone,URLSearchParams,console,
    salesMetrics:item=>item.sales_data?.metrics || {}, numeric:Number,
    productFilterGroup:item=>item.product, sourceType:()=> 'review',
  });
  for(const name of ['navigation.js','library.js']) vm.runInContext(fs.readFileSync(path.join(__dirname,'../static',name),'utf8'),context);
  context.views=views;
  vm.runInContext('navigationItems=normalizeNavigation(views); function inNavigation(item,view){return navigationMatches(item,view);}',context);
  const appSource=fs.readFileSync(path.join(__dirname,'../static/app.js'),'utf8');
  vm.runInContext(appSource.slice(appSource.indexOf('function scopedCases()'),appSource.indexOf('function shortTitle(')),context);
  return {state,context,api:vm.runInContext('({normalizeNavigation,libraryViewFilters,libraryPerformance,navigationMatches,filteredCases,views:window.LibraryViews})',context)};
}
const record=(id,current={},archived=false)=>({case_id:'PVR-'+String(id).padStart(12,'0'),product:'SHARPAL 202H',updated_at:String(id),archived,current:{category:'转化正向参考',...current},search_text:'knife reference'});

test('saved filter schema preserves legacy names and members without mutating them',()=>{
  const legacy={id:'custom-original12',label:'原有收藏',content:'custom',caseIds:['PVR-000000000001'],product:'202H',query:'knife'};
  const saved={id:'custom-filter123',label:'新视图',filters:{baseView:'../bad',query:'knife',archiveStatus:'all',contentOrigin:'overseas_creator',sort:'evil',sortDir:'asc',unexpected:'ignored'}};
  const original=JSON.stringify([legacy,saved]);
  const {api}=fixture([], [legacy,saved]);
  const normalized=api.normalizeNavigation([legacy,saved]);
  assert.equal(JSON.stringify([legacy,saved]),original);
  assert.equal(normalized[0].label,'原有收藏');
  assert.deepEqual(Array.from(normalized[0].caseIds),legacy.caseIds);
  assert.equal(normalized[1].filters.baseView,'all');
  assert.equal(normalized[1].filters.archiveStatus,'all');
  assert.equal(normalized[1].filters.sort,'updated');
  assert.equal('unexpected' in normalized[1].filters,false);
});

test('factual filters ignore legacy placement while old collection bookmarks retain their members',()=>{
  const own=record(1,{content_origin:'self_produced'}), creator=record(2,{content_origin:'overseas_creator',native_reference:{}}), archived=record(3,{category:'高播放低转化',content_origin:'overseas_creator',native_reference:{}},true);
  const legacy={id:'custom-original12',label:'旧收纳',content:'custom',caseIds:[own.case_id]};
  const saved={id:'custom-filter123',label:'达人正向',content:'all',filters:{baseView:'all',archiveCategory:'positive',contentOrigin:'overseas_creator',archiveStatus:'all'}};
  const {api,state}=fixture([own,creator,archived],[legacy,saved]);
  state.navigation[creator.case_id]='column:custom-original12';
  assert.equal(api.navigationMatches(own,legacy.id),true);
  assert.equal(api.navigationMatches(creator,legacy.id),true);
  state.view=saved.id;api.views.applyRouteFilters();
  assert.deepEqual(Array.from(api.filteredCases(),item=>item.case_id),[creator.case_id]);
  state.view='reference-native';state.archiveCategory='all';
  assert.deepEqual(Array.from(api.filteredCases(),item=>item.case_id).sort(),[creator.case_id,archived.case_id].sort());
  assert.equal(state.navigation[creator.case_id],'column:custom-original12');
  assert.equal(api.libraryPerformance(creator),'positive');
});

test('the universal scope survives hidden or deleted legacy all collections without changing their membership',()=>{
  const first=record(1),second=record(2),archived=record(3,{},true);
  for(const flags of [{visible:true},{visible:false},{deleted:true}]) {
    const oldAll={id:'all',label:'旧的精选收纳',content:'custom',caseIds:[first.case_id],...flags};
    const {api,state}=fixture([first,second,archived],[oldAll]);
    assert.equal(api.navigationMatches(first,'all'),true);
    assert.equal(api.navigationMatches(second,'all'),false);
    state.view='reference-all';
    assert.deepEqual(Array.from(api.filteredCases(),item=>item.case_id).sort(),[first.case_id,second.case_id].sort());
    state.archiveStatus='all';
    assert.equal(api.filteredCases().length,3);
    assert.deepEqual(Array.from(api.normalizeNavigation([oldAll])[0].caseIds),[first.case_id]);
  }
});
