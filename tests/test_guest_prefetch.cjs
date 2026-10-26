'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../static/guest-mode.js'), 'utf8');
const guard = source.slice(0, source.indexOf('  const HIDDEN =')) + '\n})();';
const origin = 'https://workbench.test';

function harness() {
  const calls = [], timers = new Map(), events = new Map(), documentEvents = new Map();
  let now = 1000, nextTimer = 0;
  const document = { hidden: false, documentElement: { classList: { add() {} } },
    addEventListener(name, callback) { documentEvents.set(name, callback); } };
  const window = {
    addEventListener(name, callback) { events.set(name, callback); },
    fetch(input, options = {}) {
      return new Promise((resolve, reject) => {
        const call = { input, options, resolve, reject };
        calls.push(call);
        options.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
      });
    },
  };
  const context = vm.createContext({ window, document, URL, Request, Response, Headers, AbortController,
    location: { href: origin + '/', origin }, Date: { now: () => now },
    setTimeout(callback) { const id = ++nextTimer; timers.set(id, callback); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(guard, context);
  const fetch = (...args) => window.fetch(...args);
  const fulfill = (index, data = { digest: 'snapshot-one' }, init = {}) => {
    const response = Response.json(data, init);
    calls[index].resolve(response);
    return response;
  };
  return { calls, timers, events, documentEvents, document, fetch, fulfill,
    advance(milliseconds) { now += milliseconds; },
    expire() { for (const callback of [...timers.values()]) callback(); timers.clear(); },
  };
}

test('starts the public state read before startup and gives its body to exactly one plain read', async () => {
  const fixture = harness();
  assert.equal(fixture.calls.length, 1);
  assert.equal(fixture.calls[0].input, '/guest/api/state');
  assert.equal(fixture.calls[0].options.cache, 'no-store');
  assert.equal(fixture.calls[0].options.credentials, 'same-origin');
  const first = fixture.fetch('/api/state', { headers: {} });
  assert.equal(fixture.calls.length, 1, 'startup joins the request already in flight');
  const response = fixture.fulfill(0, { digest: 'first' }, { headers: { 'X-Public': 'kept' } });
  const result = await first;
  assert.equal(result, response, 'the original response metadata and independent body are retained');
  assert.equal(result.bodyUsed, false, 'the readiness check consumed only its clone');
  assert.equal(result.headers.get('X-Public'), 'kept');
  assert.deepEqual(await result.json(), { digest: 'first' });
  assert.equal(fixture.timers.size, 0);
  const second = fixture.fetch('/api/state');
  assert.equal(fixture.calls.length, 2, 'later navigation never reuses the startup snapshot');
  fixture.fulfill(1, { digest: 'second' });
  assert.deepEqual(await (await second).json(), { digest: 'second' });
});

test('the already-namespaced state path also consumes the single startup request', async () => {
  const fixture = harness();
  fixture.fulfill(0);
  const response = await fixture.fetch(origin + '/guest/api/state', { method: 'GET', headers: new Headers() });
  assert.equal(response.status, 200);
  assert.equal(fixture.calls.length, 1);
  await response.json();
});

test('queries, Request inputs and custom lifecycles invalidate the speculative snapshot and stay independent', async () => {
  const cases = [
    ['/api/state?since=old', {}],
    ['/guest/api/state?since=old', {}],
    ['/api/state', { signal: new AbortController().signal }],
    ['/api/state', { headers: { 'X-Custom': 'kept' } }],
    ['/api/state', { cache: 'reload' }],
    ['/api/state', { credentials: 'omit' }],
    ['/api/state', { method: 'HEAD' }],
    [new Request(origin + '/api/state', { headers: { 'X-Request': 'kept' } }), {}],
  ];
  for (const [input, options] of cases) {
    const fixture = harness();
    const pending = fixture.fetch(input, options);
    assert.equal(fixture.calls.length, 2);
    assert.equal(fixture.calls[0].options.signal.aborted, true);
    const forwarded = fixture.calls[1];
    assert.ok(new URL(forwarded.input instanceof Request ? forwarded.input.url : forwarded.input, origin).href.startsWith(origin + '/guest/api/state'));
    assert.equal(forwarded.options, options);
    fixture.fulfill(1);
    await pending;
    const later = fixture.fetch('/api/state');
    assert.equal(fixture.calls.length, 3);
    fixture.fulfill(2);
    await later;
  }
});

test('a failed or unsuccessful speculative request retries through the normal guest route', async () => {
  for (const failure of ['network', 'http', 'body']) {
    const fixture = harness();
    const read = fixture.fetch('/api/state', { headers: {} });
    if (failure === 'network') fixture.calls[0].reject(new Error('offline'));
    else if (failure === 'http') fixture.fulfill(0, { error: 'not ready' }, { status: 503 });
    else fixture.calls[0].resolve(new Response(new ReadableStream({ start(controller) { controller.error(new Error('incomplete body')); } })));
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(fixture.calls.length, 2, failure);
    fixture.fulfill(1, { digest: 'retry' });
    assert.deepEqual(await (await read).json(), { digest: 'retry' });
    assert.equal(fixture.timers.size, 0);
  }
});

test('timeout and stale responses are discarded before the normal read', async () => {
  for (const expire of [fixture => fixture.expire(), fixture => fixture.advance(15001)]) {
    const fixture = harness();
    expire(fixture);
    const read = fixture.fetch('/api/state');
    assert.equal(fixture.calls.length, 2);
    assert.equal(fixture.calls[0].options.signal.aborted, true);
    fixture.fulfill(1, { digest: 'fresh' });
    assert.deepEqual(await (await read).json(), { digest: 'fresh' });
  }
});

test('a body that stalls after startup joins is aborted and retried independently', async () => {
  const fixture = harness();
  let body;
  const response = new Response(new ReadableStream({ start(controller) { body = controller; } }));
  fixture.calls[0].options.signal.addEventListener('abort', () => body.error(new Error('Timed out')));
  const read = fixture.fetch('/api/state');
  fixture.calls[0].resolve(response);
  await new Promise(resolve => setImmediate(resolve));
  fixture.expire();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(fixture.calls.length, 2, 'the bounded bootstrap cannot strand normal startup');
  fixture.fulfill(1, { digest: 'after-timeout' });
  assert.deepEqual(await (await read).json(), { digest: 'after-timeout' });
});

test('leaving, changing route or hiding the document releases unused prefetches', async () => {
  for (const event of ['pagehide', 'hashchange', 'visibilitychange']) {
    const fixture = harness();
    if (event === 'visibilitychange') {
      fixture.document.hidden = true;
      fixture.documentEvents.get(event)();
      fixture.document.hidden = false;
    } else fixture.events.get(event)();
    assert.equal(fixture.calls[0].options.signal.aborted, true, event);
    const read = fixture.fetch('/api/state');
    assert.equal(fixture.calls.length, 2);
    fixture.fulfill(1);
    await read;
  }
});

test('early prefetch never changes write guards or unrelated reads', async () => {
  const fixture = harness();
  for (const method of ['POST', 'PUT', 'DELETE', 'PATCH']) {
    assert.equal((await fixture.fetch('/api/state', { method, body: '{}' })).status, 403);
  }
  assert.equal(fixture.calls.length, 1, 'writes never reach the network');
  const unrelated = fixture.fetch('/api/cases/PVR-000000000001');
  assert.equal(String(fixture.calls[1].input), origin + '/guest/api/cases/PVR-000000000001');
  fixture.fulfill(1);
  await unrelated;
  const state = fixture.fetch('/api/state');
  fixture.fulfill(0);
  await state;
  assert.equal(fixture.calls.length, 2, 'an unrelated read cannot consume the startup request');
});
