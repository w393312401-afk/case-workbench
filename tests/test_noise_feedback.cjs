const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
test('only identical current metric scopes can share the outer core figures',()=>{
  const context = vm.createContext({window:{},Map,URLSearchParams});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/feedback-chain.js'),'utf8'), context);
  const video={window_end:'2026-10-18',posted_on:'2026-10-01',window_views:100,views:200,ctr_pct:2,orders:1,ctor_pct:5,gmv:99};
  const data={current_window:true,window_end:video.window_end,summary:{window_end:video.window_end,posted_on:video.posted_on,metrics:{...video}}};
  const same=context.window.FeedbackChain.sameMetricScope;
  assert.equal(same(data,video),true);
  assert.equal(same({...data,current_window:false},video),false);
  assert.equal(same({...data,window_end:'2026-10-11'},video),false);
  assert.equal(same(data,{...video,views:201}),false);
  assert.equal(same(data,{...video,ctor_pct:null}),false);
  assert.equal(same(data,null),false);
  assert.equal(same({...data,summary:{...data.summary,status:'incomplete',status_label:'数据不全'}},video),true);
  assert.equal(context.window.FeedbackChain.sameStatusScope,undefined);
});

test('manual classification never borrows automatic or another-window status',()=>{
  const context = vm.createContext({window:{},Map,URLSearchParams});
  vm.runInContext(fs.readFileSync(path.join(__dirname,'../static/feedback-chain.js'),'utf8'),context);
  const classify=context.window.FeedbackChain.manualReviewStatus;
  const raw={summary:{status:'focus',status_label:'重点复制'},review:null};
  assert.equal(classify(raw).label,'待复盘');
  assert.equal(classify({...raw,review:{state:'reviewed'}}).label,'待分类');
  assert.equal(classify({...raw,review:{state:'reviewed',reference_value:null}}).label,'待分类');
  assert.equal(classify({...raw,review:{state:'pending',reference_value:'high'}}).label,'待复盘');
  assert.equal(classify({...raw,current_window:false,review:{state:'reviewed',reference_value:'weak'}}).label,'弱参考价值');
  assert.equal(classify({...raw,review_status:{key:'pending'},review:{state:'reviewed',reference_value:'high'}}).label,'待复盘');
  for(const [key,label] of Object.entries({none:'无参考价值',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'})){
    assert.equal(classify({...raw,review_status:{key,label}}).label,label);
  }
});
