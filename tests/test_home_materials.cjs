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
test('home due videos use both natural week and current product',()=>{
  const {context,setProduct}=harness();vm.runInContext(read('home.js'),context);
  const videos=[{video_id:'1',product:'202H',window_end:'2026-10-11'},{video_id:'2',product:'129N',window_end:'2026-10-11'},{video_id:'3',product:'202H',window_end:'2026-09-27'}];
  const week={start:'2026-10-05',end:'2026-10-11'};
  assert.equal(context.window.WorkbenchHome.dueVideos(videos,week).map(v=>v.video_id).join(','),'1');
  setProduct('');assert.equal(context.window.WorkbenchHome.dueVideos(videos,week).length,2);
});
test('home four charts preserve product filter and leave incomplete data empty',()=>{
  const {context}=harness();
  context.addCalendarDays=(iso,n)=>{const d=new Date(iso+'T00:00:00Z');d.setUTCDate(d.getUTCDate()+n);return d.toISOString().slice(0,10);};
  vm.runInContext(read('perf-dashboard.js'),context);
  const data={filters:{product:'202H',account:'',period:'8'},summary:{coverage:{last:'2026-10-04',gaps:[]},weeks:[{week:'2026-09-28',complete:true},{week:'2026-10-05',complete:false}]},analytics:{videos:[['1','one','202H','2026-09-28'],['2','two','129N','2026-09-28']],weekly:[[0,'2026-09-28',100,200,20,2,3,41],[1,'2026-09-28',900,1800,100,10,30,9999],[0,'2026-10-05',1,2,1,1,1,12345]],monthly:[],shop_daily:[]},videos:[]};
  const html=context.window.PerfDashboard.homeMarkup(data);
  assert.equal((html.match(/class="perf-mini"/g)||[]).length,4);
  for(const title of ['每周归因 GMV','每周售出件数','每周播放量','每周新发布视频']) assert.ok(html.includes(title));
  assert.ok(!html.includes('$9,999'));
  assert.ok(!html.includes('$12,345'));
  assert.ok(html.includes('$41'));
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
