'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const context=vm.createContext({window:{}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/review-followups.js'),'utf8'),context);
const subject=context.window.ReviewFollowups;
test('reviewed stays closed across weeks, deferral expires, and new windows reopen',()=>{
  const video={video_id:'123456',product:'202H',window_end:'2026-09-13'};
  assert.equal(subject.status(video,[{...video,state:'reviewed'}],'2026-10-13').state,'reviewed');
  assert.equal(subject.status(video,[{...video,state:'deferred',defer_until:'2026-10-14'}],'2026-10-13').state,'deferred');
  assert.equal(subject.status(video,[{...video,state:'deferred',defer_until:'2026-10-13'}],'2026-10-13').state,'pending');
  assert.equal(subject.status({...video,window_end:'2026-09-20'},[{...video,state:'reviewed'}],'2026-10-13').state,'pending');
  const script={video_id:'123456',status:'reviewed',history:[{status:'reviewed',published_on:'2026-08-31'}]};
  assert.equal(subject.status({...video,posted_on:'2026-08-31'},[],'2026-10-13',[script]).state,'reviewed');
  assert.equal(subject.status({...video,posted_on:'2026-09-07'},[],'2026-10-13',[script]).state,'pending');
  assert.equal(subject.status(video,[{...video,state:'pending'}],'2026-10-13',[{video_id:'123456',status:'reviewed'}]).state,'pending');
});
test('posts the workbench registered come first, then the most recently due',()=>{
  const videos=[{video_id:'1',window_end:'2026-04-12'},{video_id:'2',window_end:'2026-04-19'},{video_id:'3',window_end:'2026-10-25'},{video_id:'4',window_end:'2026-10-25'},{video_id:'5',window_end:'2026-10-18'}];
  const scripts=[{video_id:'3',published_on:'2026-10-17'},{video_id:'2',status:'adopted'},{video_id:'',published_on:'2026-10-01'}];
  assert.deepEqual(Array.from(subject.order(videos,scripts),video=>video.video_id),['3','4','5','2','1']);
  assert.deepEqual(videos.map(video=>video.video_id),['1','2','3','4','5']);
  assert.deepEqual(Array.from(subject.order(videos),video=>video.video_id),['3','4','5','2','1']);
});
test('only videos aged fourteen days with covered figures count as ready to review',()=>{
  const week={today:'2026-10-13',end:'2026-10-18'};
  const videos=[{video_id:'1',product:'202H',window_end:'2026-10-11',status:'focus'},{video_id:'2',product:'202H',window_end:'2026-10-11',status:'incomplete'},
    {video_id:'3',product:'202H',window_end:'2026-10-11',status:'pending'},{video_id:'4',product:'202H',window_end:'2026-10-18',status:'pending'},
    {video_id:'5',product:'202H',window_end:'2026-10-04',status:'drop'}];
  const entries=[{video_id:'5',product:'202H',window_end:'2026-10-04',state:'reviewed'}];
  videos.forEach(video=>Object.assign(video,{posted_on:'2026-09-28',last_data:'2026-10-12',window_views:1000}));
  videos[3].posted_on='2026-10-10';
  assert.deepEqual(subject.ready(videos,entries,week).map(video=>video.video_id),['1']);
  assert.equal(subject.pending(videos,entries,week).length,4);
});

test('fourteen day boundary ignores the shorter calendar report window and missing dates stay unknown',()=>{
  const video={video_id:'one',product:'202H',posted_on:'2026-10-11',window_end:'2026-10-18',last_data:'2026-10-24',status:'focus',window_views:12000};
  assert.equal(subject.bucket(video,[],'2026-10-24'),'observing');
  assert.equal(subject.context(video,'2026-10-24').observation.ends_on,'2026-10-25');
  assert.equal(subject.bucket(video,[],'2026-10-25'),'ready');
  assert.equal(subject.bucket({...video,posted_on:null},[],'2026-10-25'),'missing');
  assert.equal(subject.bucket({...video,posted_on:'2026-99-99'},[],'2026-10-25'),'missing');
  assert.equal(subject.bucket({...video,posted_on:'2026-10-26'},[],'2026-10-25'),'missing');
  assert.equal(subject.bucket({...video,last_data:'2026-10-18'},[],'2026-10-25'),'missing');
});

test('legacy manual observation dates no longer delay mature videos and automatic observation still applies',()=>{
  const video={video_id:'one',product:'202H',posted_on:'2026-10-01',window_end:'2026-10-11',last_data:'2026-10-24',status:'follow',window_views:4000};
  const entry={...video,state:'observing',decision:'observe',next_review_on:'2026-10-25',observation_reason:'等新增订单'};
  assert.equal(subject.bucket(video,[entry],'2026-10-24'),'ready');
  assert.equal(subject.bucket(video,[{...entry,next_review_on:'2027-10-25'}],'2026-10-24'),'ready');
  assert.equal(subject.bucket(video,[entry],'2026-10-25'),'ready');
  assert.equal(subject.status(video,[entry],'2026-10-25').review_due,undefined);
  assert.equal(entry.state,'observing');
  assert.equal(subject.bucket({...video,last_data:null},[entry],'2026-10-25'),'missing');
  assert.equal(subject.bucket({...video,posted_on:'2026-10-20'},[entry],'2026-10-25'),'observing');
  assert.equal(subject.bucket(video,[{...entry,state:'reviewed',decision:'none'}],'2026-10-25'),'reviewed');
});

test('review priorities lead with confirmation then matured early reviews, anomalies, and replication signals',()=>{
  const base={product:'202H',window_end:'2026-10-18',posted_on:'2026-10-01',last_data:'2026-10-24',status:'follow',window_views:4000};
  const videos=[{...base,video_id:'own'},{...base,video_id:'copy',orders:2},{...base,video_id:'late',flags:[{key:'late'}]},{...base,video_id:'due'},{...base,video_id:'ai'}];
  const entries=[{...base,video_id:'due',state:'reviewed',decision:'stop',at:'2026-10-10T12:00:00+08:00'}];
  const tasks=[{task_id:'ai-1',action:'retrospective',status:'completed',context:{review_video:{video_id:'ai',window_end:base.window_end}}}];
  assert.deepEqual(Array.from(subject.order(videos,[{video_id:'own',published_on:'2026-10-01'}],{entries,tasks,today:'2026-10-25'}),video=>video.video_id),['ai','due','late','copy','own']);
  assert.equal(subject.priority(videos[4],entries,tasks,'2026-10-25').reason,'AI结论已准备好，等待核对');
});

test('recent replication candidates precede historical zero-order backlog while matured early reviews stay urgent',()=>{
  const base={product:'202H',window_end:'2026-10-18',last_data:'2026-10-24',status:'focus',window_views:12000};
  const old={...base,video_id:'old',posted_on:'2026-08-01',orders:0,flags:[{key:'no_orders'}]};
  const boundary={...base,video_id:'boundary',posted_on:'2026-09-10',orders:0,flags:[{key:'no_orders'}]};
  const copy={...base,video_id:'copy',posted_on:'2026-10-01',orders:29,window_views:157326};
  const due={...old,video_id:'due'};
  const entries=[{...due,state:'reviewed',decision:'stop',at:'2026-08-10T12:00:00+08:00'}];
  const videos=[old,copy,boundary,due];
  assert.deepEqual(Array.from(subject.order(videos,[],{entries,today:'2026-10-25'}),video=>video.video_id),['due','boundary','copy','old']);
  assert.equal(subject.priority(old,entries,[],'2026-10-25').rank,4);
  assert.equal(subject.priority(old,entries,[],'2026-10-25').reason,'历史视频有播放未成交，可集中复核');
  assert.equal(subject.priority(boundary,entries,[],'2026-10-25').rank,2);
  assert.equal(subject.priority({...old,posted_on:null},entries,[],'2026-10-25').rank,2,'unknown age must not be guessed as historical');
  assert.equal(subject.priority({...old,review_context:{observation:{age_days:46}}},entries,[],'2026-10-25').rank,4);
  assert.equal(subject.priority({...old,flags:[{key:'late'}]},entries,[],'2026-10-25').reason,'有后起信号，值得重新检查表现');
});

test('coverage permits ending a low sample round without converting it into a failure verdict',()=>{
  const video={posted_on:'2026-10-01',last_data:'2026-10-24',status:'drop',window_views:120,orders:0};
  const info=subject.context(video,'2026-10-25');
  assert.equal(info.evidence.key,'insufficient');assert.equal(info.closing_allowed,true);
  assert.ok(!info.performance.traffic.includes('放弃'));
});

test('early reviewed videos keep observing without changing history and reopen only an evidenced early decision',()=>{
  const video={video_id:'early',product:'202H',posted_on:'2026-10-11',window_end:'2026-10-18',last_data:'2026-10-24',status:'focus',window_views:12000};
  const entry={...video,state:'reviewed',decision:'none',at:'2026-10-20T12:00:00+08:00',note:'阶段观察',history:[{state:'reviewed'}]};
  assert.equal(subject.bucket(video,[entry],'2026-10-24'),'observing');
  assert.equal(subject.status(video,[entry],'2026-10-24').stage_review,true);
  assert.equal(subject.status(video,[entry],'2026-10-24').original_state,'reviewed');
  assert.equal(subject.bucket(video,[entry],'2026-10-25'),'ready');
  assert.equal(subject.status(video,[entry],'2026-10-25').review_due,true);
  assert.equal(entry.state,'reviewed');assert.equal(entry.history.length,1);
  assert.equal(subject.bucket(video,[{...entry,at:undefined}],'2026-10-24'),'observing');
  assert.equal(subject.bucket(video,[{...entry,at:undefined}],'2026-10-25'),'reviewed');
  assert.equal(subject.bucket(video,[{...entry,at:'2026-10-25T12:00:00+08:00'}],'2026-10-26'),'reviewed');
  assert.equal(subject.bucket(video,[{...entry,state:'deferred',defer_until:'2026-10-30'}],'2026-10-24'),'deferred');
});

test('stop saved after Shanghai midnight on day fourteen stays closed across UTC dates',()=>{
  const video={video_id:'midnight',product:'202H',posted_on:'2026-10-11',window_end:'2026-10-18',last_data:'2026-10-24',status:'follow',window_views:12000,orders:3};
  const entry={...video,state:'reviewed',decision:'stop',note:'内容反例，结束本条。'};
  for (const at of ['2026-10-24T16:00:00Z','2026-10-24T20:00:00.123456+00:00','2026-10-25T00:00:00+08:00','2026-10-24T09:00:00-07:00']) {
    const saved={...entry,at};
    assert.equal(subject.bucket(video,[saved],'2026-10-25'),'reviewed',at);
    assert.equal(subject.ready([video],[saved],{today:'2026-10-25'}).length,0,at);
    assert.equal(subject.status(video,[saved],'2026-10-25').decision,'stop',at);
    assert.equal(subject.bucket(video,[saved],'2026-10-26'),'reviewed',at);
  }
});

test('Shanghai dates still reopen genuinely early conclusions but unknown timestamps stay conservative',()=>{
  const video={video_id:'early-midnight',product:'202H',posted_on:'2026-10-11',window_end:'2026-10-18',last_data:'2026-10-24',status:'follow',window_views:12000};
  const entry={...video,state:'reviewed',decision:'stop',note:'原阶段结论'};
  for (const at of ['2026-10-24T15:59:59Z','2026-10-24T23:59:59+08:00','2026-10-25T01:00:00+14:00']) {
    assert.equal(subject.bucket(video,[{...entry,at}],'2026-10-25'),'ready',at);
    assert.equal(subject.status(video,[{...entry,at}],'2026-10-25').stage_review,true,at);
  }
  for (const at of [undefined,null,'','invalid','2026-02-30T00:00:00Z','2026-10-24T99:00:00Z','2026-10-24T12:00:00','2026-10-24garbage']) {
    assert.equal(subject.bucket(video,[{...entry,at}],'2026-10-25'),'reviewed',String(at));
    assert.equal(subject.bucket(video,[{...entry,at}],'2026-10-24'),'observing',String(at));
  }
});
