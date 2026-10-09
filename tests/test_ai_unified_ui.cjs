'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/ai.js'), 'utf8');
const detailSource = fs.readFileSync(path.join(__dirname, '../static/detail.js'), 'utf8');
function declaration(name) {
  const line = source.split('\n').find(value => value.trimStart().startsWith(`const ${name} = `));
  assert.ok(line, `Load actual ${name} declaration`);
  return line.trim();
}
function implementation(name) {
  const start = source.indexOf(`  function ${name}(`);
  const end = source.indexOf('\n  function ', start + 1);
  assert.ok(start >= 0 && end > start, `Load actual ${name} function`);
  return source.slice(start, end);
}
function markdownImplementation(name) {
  const start = detailSource.indexOf(`function ${name}(`);
  const end = detailSource.indexOf('\nfunction ', start + 1);
  assert.ok(start >= 0 && end > start, `Load actual ${name} function`);
  return detailSource.slice(start, end);
}
const helpers = ['refreshAvailability','settingsFromRound','modelChoices','selectedModel','allowedEfforts','skillAvailable','settingsIssue','settingsMarkup','modelOptionGroups','claudeHint','rememberedSettings','modelLabel','titleLineGroups','stageMetadata','roundCopyText','reviewContextText'];
const scope = vm.runInNewContext(`
  ${declaration('skillNames')}
  ${declaration('legacySkillNames')}
  ${declaration('effortNames')}
  ${declaration('statuses')}
  ${declaration('active')}
  ${declaration('statusText')}
  const pageURL = hash => hash;
  const localStorage = {value:null,getItem() {return this.value;}};
  const esc = value => String(value ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const ai = {options:{models:[],skills:[{id:'product-video-script-review',label:'统一技能',available:true}]}};
  ${helpers.map(implementation).join('\n')}
  ${['markdownInline','renderMarkdown'].map(markdownImplementation).join('\n')}
  ({${helpers.join(',')},renderMarkdown,ai,localStorage});
`);

test('creation settings show exactly auto, unified skill and none', () => {
  const html = scope.settingsMarkup('test',{model:null,reasoning_effort:null,skill_selection:'auto'});
  const skills = html.match(/<select id="test-skills">([\s\S]*?)<\/select>/)[1];
  assert.deepEqual([...skills.matchAll(/<option value="([^"]+)"/g)].map(match => match[1]), ['auto','product-video-script-review','none']);
  assert.doesNotMatch(skills, /tiktok-video-script-voiceover|both/);
});

test('historical selection migrates for continuation without changing its source round', () => {
  for (const selection of ['both','tiktok-video-script-voiceover']) {
    const round = {skill_selection:selection,requested_model:'test-a',requested_reasoning_effort:'high'};
    const before = JSON.stringify(round);
    const next = scope.settingsFromRound(round);
    assert.equal(next.skill_selection,'product-video-script-review');
    assert.match(next.migration_notice,/合并/);
    assert.equal(next.model,'test-a');
    assert.equal(next.reasoning_effort,'high');
    assert.equal(JSON.stringify(round),before);
  }
  assert.equal(scope.settingsFromRound({skill_selection:'none'}).skill_selection,'none');
});

test('draft and second-call text stay hidden while running and after final delivery', () => {
  const stages = {draft:{status:'completed',output:'PRIVATE_DRAFT'},review:{status:'running',output:'PARTIAL_REVIEW'}};
  for (const status of ['running','completed']) {
    const html = scope.stageMetadata({status,stages});
    assert.doesNotMatch(html,/PRIVATE_DRAFT|PARTIAL_REVIEW/);
    assert.match(html,/独立内容复核/);
    assert.match(html,/不代表本土达人认证/);
  }
});

test('failed review exposes recoverable draft explicitly marked unfinished and escapes markup', () => {
  const html = scope.stageMetadata({status:'failed',stages:{draft:{status:'completed',output:'<script>draft</script>'},review:{status:'failed',messages:[{phase:'final_answer',text:'partial'}]}}});
  assert.match(html,/本轮未完成/);
  assert.match(html,/&lt;script&gt;draft&lt;\/script&gt;/);
  assert.doesNotMatch(html,/<script>/);
  assert.match(html,/partial/);
});

test('copy action never promotes stored draft to reviewed output', () => {
  assert.equal(scope.roundCopyText({status:'failed',output:'',messages:[],stages:{draft:{output:'DRAFT'}}}),'');
  assert.equal(scope.roundCopyText({status:'completed',output:'REVISED',messages:[],stages:{draft:{output:'DRAFT'}}}),'REVISED');
});

test('completed scoring markdown is copied verbatim and rendered by the existing markdown table renderer', () => {
  for (const name of ['native-scoring-complete.md','native-scoring-incomplete.md']) {
    const markdown = fs.readFileSync(path.join(__dirname,'fixtures',name),'utf8');
    const round = {status:'completed',output:markdown,messages:[],stages:{draft:{output:'PRIVATE_DRAFT'}}};
    assert.equal(scope.roundCopyText(round),markdown);
    const html = scope.renderMarkdown(round.output);
    assert.match(html,/<table><thead>/);
    for (const dimension of ['用词搭配','句间承接','说话者场景','节奏','市场圈层']) {
      assert.ok(html.includes(`<td>${dimension}</td>`));
    }
    assert.match(html,/样本贴合/);
    assert.match(html,/营销判断/);
    assert.doesNotMatch(html,/PRIVATE_DRAFT/);
    if (name === 'native-scoring-complete.md') {
      assert.match(html,/17\/20/);
    } else {
      assert.match(html,/<td>待核<\/td>/);
      assert.match(html,/<td>不适用<\/td>/);
      assert.match(html,/完整总分不提供/);
      assert.doesNotMatch(html,/\d+\/20/);
    }
  }
});

test('a round says whether the user-review digest was attached, could not be read, or was left out', () => {
  assert.equal(scope.reviewContextText(null),'');
  assert.match(scope.reviewContextText({status:'attached',reviews:587,synced_at:'2026-10-06T16:00:00'}),/已附用户评论库摘要（587 条评论，同步于 2026-10-06 16:00:00）/);
  assert.match(scope.reviewContextText({status:'unavailable',reason:'OSError: 被占用'}),/未能加载：OSError: 被占用/);
  assert.match(scope.reviewContextText({status:'skipped',reason:'加上评论摘要后提示超过长度上限，本轮未附用户评论库摘要。'}),/本轮未附用户评论库摘要/);
  assert.match(scope.reviewContextText({status:'skipped'}),/本轮未附用户评论库摘要/);
});

test('failed or cancelled partial scoring stays recoverable and never becomes the copied final result', () => {
  const partial = fs.readFileSync(path.join(__dirname,'fixtures/native-scoring-incomplete.md'),'utf8');
  for (const status of ['failed','cancelled']) {
    const round = {status,output:'',messages:[],stages:{draft:{status:'completed',output:'RECOVERABLE_DRAFT'},review:{status,messages:[{phase:'final_answer',text:partial,complete:false}]}}};
    assert.equal(scope.roundCopyText(round),'');
    const html = scope.stageMetadata(round);
    assert.match(html,/本轮未完成/);
    assert.match(html,/RECOVERABLE_DRAFT/);
    assert.match(html,/原生度文本评估 v1/);
    assert.match(html,/待核/);
    assert.doesNotMatch(html,/<table>/);
  }
});

test('Claude and Codex models are grouped, and unready Claude models cannot be picked', () => {
  const models = [{id:'gpt-5',name:'GPT-5',provider:'codex',reasoning_efforts:['low']},
    {id:'claude-opus-5-5',name:'Claude Opus 5.5',provider:'claude',is_default:true,available:false,reasoning_efforts:['high'],default_reasoning_effort:'high'}];
  const html = scope.modelOptionGroups(models,{model:null});
  assert.match(html,/<optgroup label="本机 Claude Code"><option value="claude-opus-5-5"  disabled>Claude Opus 5\.5 · 推荐 · 未就绪<\/option><\/optgroup>/);
  assert.match(html,/<optgroup label="本机 Codex"><option value="gpt-5"/);
  assert.equal(scope.modelOptionGroups([models[0]],{model:null}).includes('optgroup'),false);
});

test('Claude client default is selected on first use and preserves client thinking settings', () => {
  scope.ai.options.models = [
    {id:'claude-default',name:'Claude 客户端默认',provider:'claude',is_default:true,available:true,reasoning_efforts:[]},
    {id:'claude-opus',name:'Claude Opus',provider:'claude',available:true,reasoning_efforts:['high']},
  ];
  scope.localStorage.value = null;
  assert.deepEqual(JSON.parse(JSON.stringify(scope.rememberedSettings())), {model:'claude-default',reasoning_effort:null});
  const html = scope.settingsMarkup('client',{model:'claude-default',reasoning_effort:null,skill_selection:'auto'});
  assert.match(html,/<select id="client-effort" disabled>/);
  assert.match(html,/Claude 客户端默认 · 推荐/);
  scope.localStorage.value = JSON.stringify({model:'claude-opus',reasoning_effort:'high'});
  assert.deepEqual(JSON.parse(JSON.stringify(scope.rememberedSettings())), {model:'claude-opus',reasoning_effort:'high'});
  scope.ai.options.models = [];
  scope.localStorage.value = null;
});

test('unavailable selected Claude blocks submission and points to client status', () => {
  scope.ai.options.models = [{id:'claude-default',provider:'claude',available:false}];
  scope.ai.status = {providers:{claude:{available:false,error:'请先登录本机 Claude Code。'}}};
  assert.equal(scope.settingsIssue({model:'claude-default',reasoning_effort:null,skill_selection:'auto'}),'请先登录本机 Claude Code。');
  assert.match(scope.claudeHint(),/请先登录本机 Claude Code。/);
  assert.match(scope.claudeHint(),/href="#settings">查看客户端状态/);
  scope.ai.status.providers.claude.available = true;
  assert.equal(scope.claudeHint(),'');
  scope.ai.options.models = [];
  scope.ai.status = null;
});

test('current Claude aliases and historical version ids keep readable labels', () => {
  assert.equal(scope.modelLabel('claude-default'),'Claude 客户端默认');
  assert.equal(scope.modelLabel('claude-opus'),'Claude Opus');
  assert.equal(scope.modelLabel('claude-sonnet'),'Claude Sonnet');
  assert.equal(scope.modelLabel('claude-haiku'),'Claude Haiku');
  assert.equal(scope.modelLabel('claude-opus-5-5'),'Claude Opus 5.5');
});

test('checking client login clears unavailable cached models before returning to writing', () => {
  scope.ai.options = {models:[{id:'claude-default',provider:'claude',available:false}],skills:[]};
  const status = {available:true,providers:{claude:{available:true}}};
  scope.refreshAvailability(status);
  assert.equal(scope.ai.status,status);
  assert.equal(scope.ai.options,null);
  scope.ai.status = null;
  scope.ai.options = {models:[],skills:[{id:'product-video-script-review',label:'统一技能',available:true}]};
});

test('title lines are grouped under their script heading, with list markers removed', () => {
  const groups = scope.titleLineGroups('## 变体 A\n🔴Stop guessing#knife #tok\n- 🔴Clamp holds it#cook\nHook: x\n## **变体 B**\n1. 🔴Paper test#knifetok\n');
  assert.deepEqual(JSON.parse(JSON.stringify(groups)),[{heading:'变体 A',lines:['🔴Stop guessing#knife #tok','🔴Clamp holds it#cook']},{heading:'变体 B',lines:['🔴Paper test#knifetok']}]);
});
