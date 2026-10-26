'use strict';
const test = require('node:test'), assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const source = fs.readFileSync(path.join(__dirname,'../static/review-followups.js'),'utf8');
const week = {today:'2026-10-25',start:'2026-10-19',end:'2026-10-25'};
const base = {product:'202H',posted_on:'2026-10-01',window_end:'2026-10-11',last_data:'2026-10-24',status:'follow',window_views:4000};
const video = (id,extra={}) => ({...base,video_id:String(id),...extra});
function setup(snapshot={},read) {
  const data = {videos:[],entries:[],scripts:[],tasks:[],...snapshot}, calls=[];
  let weekReads=0;
  const context=vm.createContext({URLSearchParams,window:{WorkbenchHome:{currentWeek:() => {weekReads++;return week;}},WorkbenchShell:{product:() => '202H'}},
    api:async url => {
      calls.push(url);
      if (read) return read(url,data);
      if (url.startsWith('/api/performance/videos?')) return {videos:data.videos};
      if (url.startsWith('/api/review-followups?')) return {entries:data.entries};
      if (url.startsWith('/api/creation?')) return {scripts:data.scripts};
      if (url === '/api/ai/tasks') return {tasks:data.tasks};
      throw new Error('unexpected URL '+url);
    }});
  vm.runInContext(source,context);
  return {subject:context.window.ReviewFollowups,data,calls,weekReads:() => weekReads};
}
const identity = row => `${row.product}:${row.video_id}:${row.window_end}`;

test('next candidate and reason exactly match the full home ready/order policy, without the five row page',async () => {
  const videos=[video('current'),video('own'),video('signal',{orders:2}),video('late',{flags:[{key:'late'}]}),
    video('due'),video('ai'),...Array.from({length:6},(_,i) => video('tail-'+i))];
  const entries=[{...videos[4],state:'reviewed',at:'2026-10-10T12:00:00+08:00'}];
  const scripts=[{product:'202H',video_id:'own',published_on:base.posted_on}];
  const tasks=[{product:'202H',action:'retrospective',status:'completed',context:{review_video:{product:'202H',video_id:'ai',window_end:base.window_end}}}];
  const {subject,calls,weekReads}=setup({videos,entries,scripts,tasks});
  const eligible=subject.ready(videos.filter(row => row.video_id !== 'current'),entries,week,scripts);
  const expected=subject.order(eligible,scripts,{entries,tasks,today:week.today});
  const result=await subject.nextReady({product:'202H',exclude:videos[0]});
  assert.strictEqual(result.video,expected[0]);
  assert.equal(result.video.video_id,'ai');
  assert.equal(result.remaining,11);
  assert.equal(result.reason,subject.priority(expected[0],entries,tasks,week.today,scripts).reason);
  assert.equal(weekReads(),1);
  assert.equal(calls.length,4);
  assert.equal(new URL(calls[0],'https://local').searchParams.get('projection'),'home');
  assert.ok(calls.every(url => !/page|search|campaign|period/.test(url)));
});

test('every call reads afresh and respects work another page completed',async () => {
  const {subject,data,calls,weekReads}=setup({videos:[video('1'),video('2')]});
  assert.equal((await subject.nextReady({product:'202H',week})).video.video_id,'1');
  data.entries.push({...data.videos[0],state:'reviewed',at:'2026-10-25T02:00:00Z'});
  const next=await subject.nextReady({product:'202H',week});
  assert.equal(next.video.video_id,'2');assert.equal(next.remaining,1);
  data.entries.push({...data.videos[1],state:'reviewed',at:'2026-10-25T02:00:00Z'});
  const empty=await subject.nextReady({product:'202H',week});
  assert.equal(empty.video,null);assert.equal(empty.remaining,0);assert.equal(empty.reason,'');
  assert.equal(calls.length,12);assert.equal(weekReads(),0,'injected home week avoids a second date policy');
});

test('explicit all-product scope ignores the selected shell product and specific scopes are strict',async () => {
  const {subject,calls}=setup({videos:[video('1'),video('2',{product:'129N',flags:[{key:'late'}]}),video('3',{product:'Custom / Product'})]});
  const all=await subject.nextReady({product:'',week});
  assert.equal(all.remaining,3);assert.equal(all.video.product,'129N');
  assert.equal(new URL(calls[0],'https://local').searchParams.has('product'),false);
  const own=await subject.nextReady({product:'SHARPAL 202H',week});
  assert.equal(own.remaining,1);assert.equal(own.video.product,'202H');
  const custom=await subject.nextReady({product:'custom / product',week});
  assert.equal(custom.remaining,1);assert.equal(custom.video.product,'Custom / Product');
  assert.ok(calls.some(url => url.includes('custom%20%2F%20product')));
  assert.equal((await subject.nextReady({product:'unknown',week})).video,null);
});

test('exclude identifies one product, video and window rather than hiding other windows or products',async () => {
  const current=video('same'),otherWindow=video('same',{window_end:'2026-10-18'}),otherProduct=video('same',{product:'129N'});
  const {subject}=setup({videos:[current,otherWindow,otherProduct]});
  const result=await subject.nextReady({product:'',exclude:{...current,product:'SHARPAL 202H'},week});
  assert.equal(result.remaining,2);assert.equal(identity(result.video),identity(otherWindow));
  const wrongProduct=await subject.nextReady({product:'202H',exclude:otherProduct,week});
  assert.equal(wrongProduct.remaining,2);
});

test('ready policy skips completed, deferred, fourteen-day observation and missing data while allowing due reviews',async () => {
  const videos=[video('done'),video('deferred'),video('young',{posted_on:'2026-10-20'}),video('missing',{status:'incomplete'}),
    video('legacy'),video('expired'),video('due'),video('window-changed',{window_end:'2026-10-18'})];
  const entries=[{...videos[0],state:'reviewed',at:'2026-10-25T02:00:00Z'},
    {...videos[1],state:'deferred',defer_until:'2026-10-26'},
    {...videos[5],state:'deferred',defer_until:'2026-10-25'},
    {...videos[6],state:'reviewed',at:'2026-10-10T12:00:00+08:00'},
    {...videos[7],window_end:'2026-10-11',state:'reviewed',at:'2026-10-25T02:00:00Z'}];
  const scripts=[{product:'202H',video_id:'legacy',status:'reviewed',history:[{status:'reviewed',published_on:base.posted_on}]}];
  const {subject}=setup({videos,entries,scripts});
  const result=await subject.nextReady({product:'202H',week});
  assert.equal(result.remaining,3);assert.equal(result.video.video_id,'due');
  assert.equal(result.reason,'已满14天，重新核对发布初期的阶段结论');
});

test('reviews, legacy scripts, own-post priority and AI evidence never cross explicit product identities',async () => {
  const videos=[video('1'),video('2')];
  const foreign={product:'129N',video_id:'2'};
  const entries=[{...foreign,window_end:base.window_end,state:'reviewed'}];
  const scripts=[{...foreign,published_on:base.posted_on,status:'reviewed',history:[{status:'reviewed',published_on:base.posted_on}]}];
  const tasks=[{product:'129N',action:'retrospective',status:'completed',context:{review_video:{...foreign,window_end:base.window_end}}}];
  const {subject}=setup({videos,entries,scripts,tasks});
  const result=await subject.nextReady({product:'',week});
  assert.equal(result.remaining,2);assert.equal(result.video.video_id,'1');
  assert.equal(subject.priority(videos[1],entries,tasks,week.today,scripts).rank,5);
});

test('all four independent reads start before any result and every read must complete',async () => {
  const waits=[], {subject,calls}=setup({},url => new Promise(resolve => waits.push({url,resolve})));
  let settled=false;
  const promise=subject.nextReady({product:'',week}).then(result => {settled=true;return result;});
  assert.equal(calls.length,4);
  waits[0].resolve({videos:[video('1')]});waits[1].resolve({entries:[]});waits[2].resolve({scripts:[]});
  await Promise.resolve();assert.equal(settled,false);
  waits[3].resolve({tasks:[]});
  assert.equal((await promise).video.video_id,'1');
});

test('a failed or malformed read rejects instead of claiming the queue is finished',async () => {
  for (const failed of ['performance/videos','review-followups','creation','ai/tasks']) {
    const {subject,calls}=setup({},async url => {
      if (url.includes(failed)) throw new Error('read failed: '+failed);
      return {videos:[],entries:[],scripts:[],tasks:[]};
    });
    await assert.rejects(subject.nextReady({product:'',week}),/read failed/);
    assert.equal(calls.length,4);
    const malformed=setup({},async url => url.includes(failed) ? {} : {videos:[],entries:[],scripts:[],tasks:[]});
    await assert.rejects(malformed.subject.nextReady({product:'',week}),/未完整读取/);
  }
});
