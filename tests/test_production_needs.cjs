const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');
const context = {window:{WorkbenchShell:{url:(hash,product)=>`#/${product}/${hash.slice(1)}`}},URLSearchParams};
vm.runInNewContext(fs.readFileSync(require('node:path').join(__dirname,'../static/production-needs.js'),'utf8'),context);
const collect = (record,kind='video') => JSON.parse(JSON.stringify(context.window.ProductionNeeds.collect(record,kind)));
const job = () => ({id:'PRD-0123456789ab',product:'202H',takes:[{id:'TAK-current',groups:[{number:1,start:0,end:3,coverage:1}]}],selected_take:'TAK-current',timelines:[{unique_id:'current',take_id:'TAK-current',fps:'30'}],edits:[]});

test('only latest edits of the selected voiceover contribute unresolved gaps',()=>{
  const record=job();record.timelines.push({unique_id:'old',take_id:'TAK-old',fps:'30'});
  record.edits=[{id:'old',timeline_uid:'old',status:'needs_material',report:{gaps:[{start:0,end:30,need:'historical'}]}},{id:'superseded',timeline_uid:'current',status:'needs_material',report:{gaps:[{start:0,end:30,need:'superseded'}]}},{id:'latest',timeline_uid:'current',status:'needs_material',report:{gaps:[{start:30,end:60,need:'actual missing evidence'}]}}];
  const needs=collect(record);assert.equal(needs.length,1);assert.equal(needs[0].detail,'actual missing evidence');
  assert.match(needs[0].label,/第 1 句/);assert.match(needs[0].href,/run=latest&gap=0&start=30&end=60/);
});
test('current coverage supersedes old reported gaps and completed review removes blockers',()=>{
  const record=job();record.edits=[{id:'edit',timeline_uid:'current',status:'awaiting_review',report:{gaps:[{start:0,end:30,need:'old'}]},summary:{gaps:[]}}];
  assert.equal(collect(record)[0].key,'review-edit');record.edits[0].approval={at:'2026-10-14'};record.edits[0].validation={status:'awaiting_review'};assert.equal(collect(record).length,0);
});
test('stale approval on a failed or revised edit cannot hide its current gaps',()=>{
  const record=job();record.edits=[{id:'edit',timeline_uid:'current',status:'needs_repair',approval:{at:'old'},report:{gaps:[{start:0,end:30,need:'still missing'}]}}];
  assert.equal(collect(record)[0].key,'gap-edit-0');assert.equal(collect(record).length,2);
});
test('existing selected image works without a base and a stale image requires that specific slide',()=>{
  const record={id:'CAR-0123456789ab',product:'202H',slides:[{number:1,output_id:'one',outputs:[{id:'one',approved:true,stale:false}]},{number:2,base:{id:'base'},output_id:'two',outputs:[{id:'two',approved:true,stale:true}]},{number:3,outputs:[]}]};
  const needs=collect(record,'carousel');assert.equal(needs.length,2);assert.match(needs[0].label,/第 2 张.*失效/);assert.match(needs[0].href,/step=images&slide=2/);assert.match(needs[1].href,/step=pick&slide=3/);
});
test('publication is the next step, never listed again as a gap',()=>{
  const record=job();record.status='approved';record.manual_completion={take_id:'TAK-current',timeline_uid:'current'};
  assert.equal(collect(record).length,0);record.current_publication=null;assert.equal(collect(record).length,0);record.current_publication={post_id:'7650000000000000000'};assert.equal(collect(record).length,0);
});
test('archived work has no active blockers and route remains scoped to its product',()=>{
  const record=job();record.product='129N';record.selected_take=null;assert.match(collect(record)[0].href,/^#\/129N\/production\//);record.archived=true;assert.equal(collect(record).length,0);
});

test('archived timelines stop contributing AI gaps and can be restored',()=>{
  const record=job();record.timelines.push({unique_id:'old',take_id:'TAK-current',archived:true});
  record.timelines[0].task_status='completed';
  record.edits=[{id:'old-edit',timeline_uid:'old',status:'needs_material',report:{gaps:[{start:0,end:30,need:'archived gap'}]}}];
  assert.equal(collect(record).length,0);
  record.timelines[1].archived=false;
  assert.equal(collect(record)[0].detail,'archived gap');
  record.timelines.forEach(timeline=>timeline.archived=true);
  assert.equal(collect(record)[0].key,'timeline');assert.match(collect(record)[0].detail,/恢复/);
});

test('manual task completion avoids asking for AI but preserves actual unresolved gaps',()=>{
  const record=job();record.timelines[0].task_status='completed';
  assert.equal(collect(record).length,0);
  record.edits=[{id:'edit',timeline_uid:'current',status:'needs_material',report:{gaps:[{start:0,end:30,need:'still unverified'}]}}];
  assert.equal(collect(record)[0].detail,'still unverified');
});

test('pending build leads directly to editing and a submitted continuation needs no manual action',()=>{
  const record=job();record.timelines=[];record.timeline_building={request_id:'build-one',take_id:'TAK-current',group_markers:true};
  assert.equal(collect(record)[0].key,'pending-edit');assert.match(collect(record)[0].href,/step=edit/);
  record.edits=[{id:'edit-one',timeline_request_id:'build-one',take_id:'TAK-current',waiting_timeline:true,status:'running'}];
  assert.equal(collect(record).length,0);
  record.edits[0].status='cancelled';record.edits[0].error='建线已取消';
  assert.equal(collect(record)[0].detail,'建线已取消');assert.match(collect(record)[0].href,/run=edit-one/);
});
