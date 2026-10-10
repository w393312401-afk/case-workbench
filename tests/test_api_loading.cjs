'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const implementation = source.slice(source.indexOf('const pendingApiReads ='), source.indexOf('function toast('));
function harness() {
  const calls = [];
  const context = vm.createContext({
    Map, File: class {}, structuredClone, state: {token: 'test-token'},
    fetch(url, options) {
      return new Promise((resolve, reject) => calls.push({url, options, resolve, reject}));
    },
  });
  vm.runInContext(`${implementation}\nthis.api = api;`, context);
  const finish = (index, data = {items: [{id: 'one'}]}, status = 200) => calls[index].resolve({
    ok: status < 400, status, json: async () => data,
  });
  return {api: context.api, calls, finish};
}

test('overlapping plain reads share the network request and own independent data', async () => {
  const {api, calls, finish} = harness();
  const first = api('/api/creation'), second = api('/api/creation');
  assert.equal(calls.length, 1);
  finish(0);
  const [a, b] = await Promise.all([first, second]);
  a.items[0].id = 'edited';
  assert.equal(b.items[0].id, 'one');
  const later = api('/api/creation');
  assert.equal(calls.length, 2, 'settled responses are never reused');
  finish(1);
  await later;
});

test('cancelable and custom-header reads retain independent request lifecycles', async () => {
  const {api, calls, finish} = harness();
  const signal = new AbortController().signal;
  const requests = [api('/api/state'), api('/api/state', {signal}), api('/api/state', {headers: {'X-Custom': 'value'}})];
  assert.equal(calls.length, 3);
  assert.equal(calls[1].options.signal, signal);
  assert.equal(calls[2].options.headers['X-Custom'], 'value');
  calls.forEach((_, i) => finish(i));
  await Promise.all(requests);
});

test('reads started after a write cannot join an earlier in-flight snapshot', async () => {
  const {api, calls, finish} = harness();
  const before = api('/api/creation');
  const options = {method: 'POST', body: {note: 'save'}};
  const write = api('/api/scripts', options);
  const after = api('/api/creation');
  assert.equal(calls.length, 3);
  assert.equal(calls[1].options.headers['X-Workbench-Token'], 'test-token');
  assert.equal(typeof options.body, 'object', 'caller options remain reusable');
  finish(0, {version: 'old'});
  await before;
  const shared = api('/api/creation');
  assert.equal(calls.length, 3, 'settling old reads cannot evict the new request');
  finish(1, {saved: true});
  finish(2, {version: 'new'});
  assert.equal((await after).version, 'new');
  assert.equal((await shared).version, 'new');
  await write;
});

test('failed shared reads preserve errors and allow a fresh retry', async () => {
  const {api, calls, finish} = harness();
  const first = api('/api/state'), second = api('/api/state');
  const checks = [first, second].map(request => assert.rejects(request, {message: 'try again', status: 503}));
  finish(0, {error: 'try again'}, 503);
  await Promise.all(checks);
  const retry = api('/api/state');
  assert.equal(calls.length, 2);
  finish(1);
  await retry;
});

const aiSource = fs.readFileSync(path.join(__dirname, '../static/ai.js'), 'utf8');
function serviceHarness() {
  const calls = [], ai = {};
  let now = 100000;
  const context = vm.createContext({ai, Date: {now: () => now}, message: error => error.message,
    api: () => new Promise(resolve => calls.push(resolve)),
  });
  const code = aiSource.slice(aiSource.indexOf('  async function checkStatus('), aiSource.indexOf('  async function loadOptions('));
  vm.runInContext(`${code}\nthis.checkStatus = checkStatus; this.refreshAvailability = refreshAvailability;`, context);
  return {ai, calls, check: context.checkStatus, refresh: context.refreshAvailability, advance: value => now += value};
}

test('client checks coalesce, expire, and allow explicit rechecking', async () => {
  const {calls, check, advance} = serviceHarness();
  const first = check(), second = check();
  assert.equal(calls.length, 1);
  calls[0]({available: true});
  await Promise.all([first, second]);
  await check();
  assert.equal(calls.length, 1);
  const forced = check(true);
  assert.equal(calls.length, 2);
  calls[1]({available: false});
  await forced;
  advance(3001);
  const retry = check();
  assert.equal(calls.length, 3, 'unavailable clients are checked again sooner');
  calls[2]({available: true});
  await retry;
});

test('an older client check cannot overwrite newly confirmed login state', async () => {
  const {ai, calls, check, refresh} = serviceHarness();
  const pending = check();
  const ready = {available: true, label: 'confirmed login'};
  refresh(ready);
  calls[0]({available: false});
  assert.equal(await pending, ready);
  assert.equal(ai.status, ready);
});
