'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const read = file => fs.readFileSync(path.join(__dirname,'../static',file),'utf8');
const escape = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function harness(selected='202H') {
  let chosen=selected;
  const shell={product:()=>chosen,matchesProduct:value=>!chosen||String(value).replace('SHARPAL ','')===chosen,url:(hash)=>hash};
  const context=vm.createContext({console,Date,Map,Set,URLSearchParams,JSON,CustomEvent:class{constructor(type){this.type=type;}},crypto:{randomUUID:()=> 'request-1'},esc:escape,icon:()=>'',state:{cases:[]},window:{WorkbenchShell:shell,dispatchEvent:()=>true},document:{querySelector:()=>null,querySelectorAll:()=>[]}});
  return {context,setProduct:value=>chosen=value};
}
test('home calendar week handles Sunday and crossing year without using export dates',()=>{
  const {context}=harness();vm.runInContext(read('home.js'),context);
  const home=context.window.WorkbenchHome;
  assert.equal(home.currentWeek(new Date(2026,9,11)).start,'2026-10-05');
  assert.equal(home.currentWeek(new Date(2027,0,1)).start,'2026-12-28');
  assert.equal(home.currentWeek(new Date(2027,0,1)).end,'2027-01-03');
});
test('cases ask references only for script and breakdown, and a known video counts as published',()=>{
  const {context}=harness();vm.runInContext(read('home.js'),context);
  const home=context.window.WorkbenchHome, base={transcript:{text:'Words'},highlights:['hook']};
  const labels=(current,published)=>home.missingCase({current:{...base,...current}},published).map(([label])=>label).join(',');
  assert.equal(labels({content_origin:'overseas_creator'}),'');
  assert.equal(labels({source_case:{tier:'A'}}),'');
  assert.equal(labels({content_origin:'self_produced',metrics:{video_id:'7001'}}),'方向,发布');
  assert.equal(labels({content_origin:'self_produced',metrics:{video_id:'7001'},test_direction:{direction_id:'202H-S1'}},new Set(['7001'])),'');
  assert.equal(labels({}),'方向,发布');
});
test('home due videos retain overdue windows and respect current product',()=>{
  const {context,setProduct}=harness();vm.runInContext(read('home.js'),context);
  const videos=[{video_id:'1',product:'202H',window_end:'2026-10-11'},{video_id:'2',product:'129N',window_end:'2026-10-11'},{video_id:'3',product:'202H',window_end:'2026-09-27'}];
  const week={start:'2026-10-05',end:'2026-10-11'};
  assert.equal(context.window.WorkbenchHome.dueVideos(videos,week).map(v=>v.video_id).join(','),'3,1');
  setProduct('');assert.equal(context.window.WorkbenchHome.dueVideos(videos,week).length,3);
});

test('home sends new posts ending next week and missing dates to the observation queue',async()=>{
  const {context}=harness(), root={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null}, breadcrumb={innerHTML:''};
  context.$=selector=>({'#home-page':root,'#breadcrumb':breadcrumb}[selector] || null);
  context.window.WorkbenchShell.parse=()=>({page:'home'});
  const videos=[{video_id:'new',product:'202H',posted_on:'2099-01-01',window_end:'2099-01-11'},{video_id:'missing-date',product:'202H'},{video_id:'other',product:'129N'}];
  let mounted=[];context.window.ReviewFollowups={ready:()=>[],mount:(_host,options)=>{mounted=options.videos;}};
  context.api=async path=>path.includes('/performance/videos')?{videos}:{scripts:[],tasks:[],entries:[],items:[],counts:{}};
  vm.runInContext(read('home.js'),context);await context.window.WorkbenchHome.route();
  assert.deepEqual(Array.from(mounted,video=>video.video_id),['new','missing-date']);
});
test('home loads only work data and keeps unavailable categories distinct from empty ones',async()=>{
  const {context}=harness();
  const root={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null}, breadcrumb={innerHTML:''}, requests=[];
  context.$=selector=>({'#home-page':root,'#breadcrumb':breadcrumb}[selector] || null);
  context.window.WorkbenchShell.parse=()=>({page:'home'});
  context.api=async path=>{requests.push(path);if(path.startsWith('/api/creation'))throw Error('offline');return path.includes('videos')?{videos:[]}:{tasks:[]};};
  vm.runInContext(read('home.js'),context);
  await context.window.WorkbenchHome.route();
  assert.deepEqual(requests,['/api/performance/videos?projection=home&product=202H','/api/ai/tasks','/api/creation?product=202H','/api/review-followups?product=202H','/api/content?view=attention&page_size=8&product=202H']);
  assert.ok(root.innerHTML.includes('<h1>工作概览</h1>'));
  assert.ok(root.innerHTML.includes('待写计划暂时无法读取。'));
  assert.ok(root.innerHTML.includes('data-home-retry'));
  assert.match(root.innerHTML,/data-home-tab="plans">下一轮待写<span class="home-tab-count">—</);
  assert.equal((root.innerHTML.match(/role="tab"/g)||[]).length,4);
  assert.ok(!root.innerHTML.includes('home-kpi'));
  assert.ok(!root.innerHTML.includes('home-weekly'));
  assert.ok(root.innerHTML.includes('#videos?tab=board'));
});

test('home lists posted videos that are not on the publish calendar yet and opens the publish step',async()=>{
  const {context}=harness();
  const root={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null}, breadcrumb={innerHTML:''};
  context.$=selector=>({'#home-page':root,'#breadcrumb':breadcrumb}[selector] || null);
  context.window.WorkbenchShell.parse=()=>({page:'home'});
  const row={id:'video:PRD-111111111111',kind:'video',title:'Zack 202',product:'202H',direction_id:'202H-S1',published_on:'2026-10-22',next_hash:'#production/PRD-111111111111?step=publish'};
  const photo={...row,id:'carousel:CAR-111111111111',kind:'carousel',title:'Zack 202 图文',next_hash:'#production/CAR-111111111111?step=delivery'};
  context.api=async path=>path.startsWith('/api/content')?{items:[],counts:{attention:0,publish:0},calendar_pending:{total:3,items:[row,photo]}}:path.includes('videos')?{videos:[]}:{tasks:[]};
  vm.runInContext(read('home.js'),context);
  await context.window.WorkbenchHome.route();
  assert.ok(root.innerHTML.includes('3 条已发布、还没上传到发布日历'));
  assert.ok(root.innerHTML.includes('#production/PRD-111111111111?step=publish'));
  assert.ok(root.innerHTML.includes('发布于 2026-10-22'));
  assert.ok(root.innerHTML.includes('#production/CAR-111111111111?step=delivery'));
  assert.ok(root.innerHTML.includes('图文制作单'));
  assert.ok(root.innerHTML.includes('另有 1 条'));
  assert.ok(!root.innerHTML.includes('现在没有等你处理的内容'));
  // Without any, the overview stays as before.
  context.api=async path=>path.startsWith('/api/content')?{items:[],counts:{attention:0,publish:0},calendar_pending:{total:0,items:[]}}:path.includes('videos')?{videos:[]}:{tasks:[]};
  await context.window.WorkbenchHome.route();
  assert.ok(!root.innerHTML.includes('发布日历'));
});

test('a late home response cannot overwrite another product or page',async()=>{
  const {context,setProduct}=harness();
  const root={innerHTML:'untouched',querySelectorAll:()=>[],querySelector:()=>null}, breadcrumb={innerHTML:''}, pending=[];
  context.$=selector=>({'#home-page':root,'#breadcrumb':breadcrumb}[selector] || null);
  let page='home';context.window.WorkbenchShell.parse=()=>({page});
  context.api=()=>new Promise(resolve=>pending.push(resolve));
  vm.runInContext(read('home.js'),context);
  // The frame shows at once; what matters is that answers arriving after a switch change nothing.
  const oldProduct=context.window.WorkbenchHome.route();
  let frame=root.innerHTML;assert.ok(frame.includes('正在读取'));
  setProduct('129N');pending.splice(0).forEach(resolve=>resolve({}));await oldProduct;
  assert.equal(root.innerHTML,frame);
  const oldPage=context.window.WorkbenchHome.route();
  frame=root.innerHTML;
  page='videos';pending.splice(0).forEach(resolve=>resolve({}));await oldPage;
  assert.equal(root.innerHTML,frame);
});

test('a product-scoped home read still recognizes publications in the full sales library',async()=>{
  const {context}=harness();
  const root={innerHTML:'',querySelectorAll:()=>[],querySelector:()=>null}, breadcrumb={innerHTML:''}, requests=[];
  context.$=selector=>({'#home-page':root,'#breadcrumb':breadcrumb}[selector] || null);
  context.window.WorkbenchShell.parse=()=>({page:'home'});
  context.state.signature='loaded';
  context.state.cases=[{case_id:'one',product:'202H',current:{transcript:{text:'Ready'},highlights:['Hook'],test_direction:{direction_id:'202H-S1'},metrics:{video_id:'other-product-video'}}}];
  context.api=async path=>{requests.push(path);return path.includes('/performance/videos')?{videos:[],published_video_ids:['other-product-video']}:{scripts:[],tasks:[]};};
  vm.runInContext(read('home.js'),context);
  await context.window.WorkbenchHome.route();
  assert.match(root.innerHTML,/data-home-tab="cases">待补案例<span class="home-tab-count">0</);
  assert.equal(requests[0],'/api/performance/videos?projection=home&product=202H');
});
test('material basket scopes server items and exports sources plus unverified boundary',async()=>{
  const {context,setProduct}=harness();
  context.api=async()=>({revision:'r1',items:[{id:'r:1',type:'review',product:'202H',source_id:'review-1',source_label:'TikTok 视频评论',text:'Wobbles',unverified:true,note:'use actual footage'},{id:'e:2',type:'expression',product:'129N',source_id:'case-2',text:'Another product'}]});
  vm.runInContext(read('material-basket.js'),context);
  await context.window.MaterialBasket.refresh();
  assert.equal(context.window.MaterialBasket.selection().length,1);
  const markdown=context.window.MaterialBasket.markdown();
  assert.ok(markdown.includes('review-1'));assert.ok(markdown.includes('未经核实'));assert.ok(markdown.includes('use actual footage'));assert.ok(!markdown.includes('Another product'));
  setProduct('129N');assert.equal(context.window.MaterialBasket.selection()[0].source_id,'case-2');
});
test('material basket tells source pages what is already in it and remembers unticked items',async()=>{
  const {context}=harness();
  context.localStorage={getItem:()=>JSON.stringify(['h:2','gone']),setItem:(key,value)=>{context.saved=value;}};
  context.api=async()=>({revision:'r1',items:[{id:'e:1',type:'expression',product:'202H',source_id:'case-1',text:'Keep it right there.'},{id:'h:1',type:'hook',product:'202H',source_id:'v1',text:'A'},{id:'h:2',type:'hook',product:'202H',source_id:'v1',text:'B'}]});
  vm.runInContext(read('material-basket.js'),context);
  const basket=context.window.MaterialBasket;
  await basket.refresh();
  assert.equal(basket.has({type:'expression',source_id:'case-1',text:'Keep it right there.'}),true);
  assert.equal(basket.has({type:'expression',source_id:'case-1',text:'Another line'}),false);
  assert.equal(basket.count({type:'hook',source_id:'v1'}),2);
  assert.deepEqual(basket.selection().map(item=>item.id),['e:1','h:1']);
  assert.equal(context.saved,JSON.stringify(['h:2']));
});
