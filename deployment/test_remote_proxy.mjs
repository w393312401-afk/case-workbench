import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import net from 'node:net';
import { once } from 'node:events';
import { generateKeyPairSync, sign, scryptSync } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { gzipSync, gunzipSync } from 'node:zlib';
import { createAccessVerifier, createJwksResolver, createRemoteProxy, validateConfig } from './remote_proxy.mjs';

const fixturePassword = 'local-test-password-12345';
const fixtureSalt = '12'.repeat(32);
const fixtureHash = scryptSync(fixturePassword, Buffer.from(fixtureSalt, 'hex'), 64,
  { N: 32768, r: 8, p: 3, maxmem: 64 * 1024 * 1024 }).toString('hex');
async function passwordFixture(t, { guest = false } = {}) {
  const directory = await mkdtemp(join(tmpdir(), 'workbench-proxy-test-'));
  const accountFile = join(directory, 'account.json');
  const account = { version: 1, username: 'fixture-admin', salt: fixtureSalt, password_hash: fixtureHash,
    scrypt: { n: 32768, r: 8, p: 3 }, credential_id: '34'.repeat(16), updated_at: new Date().toISOString() };
  await writeFile(accountFile, JSON.stringify(account));
  const guestSite = join(directory, 'guest', 'site');
  if (guest) {
    await mkdir(guestSite, { recursive: true });
    await writeFile(join(guestSite, 'public-data.json'), JSON.stringify({ state: { app: 'video-case-workbench',
      token: '', paths: {}, cases: [], digest: 'guest-v1' }, cases: {}, performance: { videos: [
        { video_id: '123456789', product: '202H', title: 'Public record', flags: [], reference: null, private_test_field: 'not-in-projection' },
        { video_id: '999999999', product: '198H', title: 'Other product' }] } }));
    await writeFile(join(guestSite, 'index.html'), '<!doctype html><title>Guest workbench</title>');
    await writeFile(join(guestSite, 'guest-mode.js'), 'window.WorkbenchGuest = true;');
  }
  const seen = [];
  const upstream = http.createServer((req, res) => {
    seen.push({ url: req.url, cookie: req.headers.cookie });
    req.resume(); req.on('end', () => res.end('protected-workbench'));
  });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy({ hostname: config.hostname, authMode: 'password', guestEnabled: guest },
    { accountFile, guestSite, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); await rm(directory, { recursive: true, force: true }); });
  const plain = { host: config.hostname };
  const post = { ...plain, origin: `https://${config.hostname}`, 'content-type': 'application/json', 'sec-fetch-site': 'same-origin' };
  const login = (body = { username: account.username, password: fixturePassword }, extra = {}) => request(port,
    { path: '/auth/login', method: 'POST', headers: { ...post, ...extra }, body: JSON.stringify(body) });
  return { port, seen, account, accountFile, plain, post, login, guestSite };
}

test('guest browsing reads only the prepared snapshot and never sends private requests upstream', async t => {
  const { port, plain, post, seen } = await passwordFixture(t, { guest: true });
  assert.throws(() => validateConfig({ ...config, guestEnabled: true }));
  assert.throws(() => validateConfig({ hostname: config.hostname, authMode: 'password', guestEnabled: 'true' }));
  const page = await request(port, { headers: plain });
  assert.equal(page.status, 200);
  assert.match(page.body, /Guest workbench/);
  assert.equal(page.headers['x-workbench-view'], 'guest');
  assert.equal(page.headers['cache-control'], 'private, no-store');
  assert.equal(page.headers.vary, 'Cookie, Accept-Encoding');
  const state = await request(port, { path: '/guest/api/state', headers: plain });
  assert.equal(state.status, 200);
  assert.equal(JSON.parse(state.body).token, '');
  assert.equal((await request(port, { path: '/guest-assets/guest-mode.js', headers: plain })).status, 200);
  for (const path of ['/api/state', '/api/data/export', '/api/ai/tasks', '/media/private.mp4', '/api/service/restart']) {
    assert.equal((await request(port, { path, headers: plain })).status, 401);
  }
  for (const path of ['/guest/api/data/export', '/guest/api/service/restart', '/guest/api/remote-account',
    '/guest-assets/public-data.json', '/guest-assets/_worker.js', '/guest-assets/unknown.js']) {
    assert.equal((await request(port, { path, headers: plain })).status, 404);
  }
  for (const path of ['/guest/api/creation', '/guest/api/ai/tasks', '/guest-assets/guest-mode.js']) {
    assert.equal((await request(port, { path, method: 'POST', headers: post, body: '{}' })).status, 405);
  }
  const directWrite = await request(port, { path: '/api/creation', method: 'POST', headers: post, body: '{}' });
  assert.equal(directWrite.status, 401);
  assert.equal(seen.length, 0);
});

test('guest remains read-only after login, while root and private APIs switch to authenticated access', async t => {
  const { port, plain, post, login, seen } = await passwordFixture(t, { guest: true });
  const result = await login();
  const cookie = result.headers['set-cookie'][0].split(';')[0];
  const root = await request(port, { headers: { ...plain, cookie, 'if-none-match': 'guest-page' } });
  assert.equal(root.body, 'protected-workbench');
  assert.equal(root.headers['cache-control'], 'private, no-store');
  const preview = await request(port, { path: '/?guest=1', headers: { ...plain, cookie } });
  assert.match(preview.body, /Guest workbench/);
  const guestState = await request(port, { path: '/guest/api/state', headers: { ...plain, cookie } });
  assert.equal(JSON.parse(guestState.body).token, '');
  assert.equal((await request(port, { path: '/guest/api/creation', method: 'POST', headers: { ...post, cookie }, body: '{}' })).status, 405);
  assert.equal(seen.length, 1);
  assert.equal((await request(port, { path: '/api/creation', method: 'POST', headers: { ...post, cookie }, body: '{}' })).status, 200);
  assert.equal(seen.length, 2);
});

test('guest negotiation reaches the snapshot without forwarding credentials', async t => {
  const { port, plain, guestSite, seen } = await passwordFixture(t, { guest: true });
  const source = 'window.guestFixture = true;\n'.repeat(300);
  await writeFile(join(guestSite, 'guest-mode.js'), source);
  const packed = await request(port, { path: '/guest-assets/guest-mode.js',
    headers: { ...plain, 'accept-encoding': 'gzip', cookie: 'private=never-forward' } });
  assert.equal(packed.status, 200);
  assert.equal(packed.headers['content-encoding'], 'gzip');
  assert.equal(gunzipSync(packed.bytes).toString(), source);
  assert.equal(packed.headers['cache-control'], 'private, no-cache');
  const checked = await request(port, { path: '/guest-assets/guest-mode.js',
    headers: { ...plain, 'accept-encoding': 'gzip', 'if-none-match': packed.headers.etag } });
  assert.equal(checked.status, 304);
  assert.equal(checked.bytes.length, 0);
  assert.equal(seen.length, 0);
});

test('guest snapshots reload after replacement and missing accounts cannot unlock private access', async t => {
  const { port, plain, accountFile, guestSite, seen } = await passwordFixture(t, { guest: true });
  await rm(accountFile);
  const mode = await request(port, { path: '/auth/mode', headers: plain });
  assert.deepEqual(JSON.parse(mode.body), { configured: false, guest_enabled: true });
  assert.equal((await request(port, { headers: plain })).status, 200);
  assert.equal((await request(port, { path: '/api/state', headers: plain })).status, 401);
  const compact = await request(port, { path: '/guest/api/performance/videos?product=202H&projection=home', headers: plain });
  const data = JSON.parse(compact.body);
  assert.equal(data.videos.length, 1);
  assert.equal(data.videos[0].private_test_field, undefined);
  assert.deepEqual(data.published_video_ids, ['123456789', '999999999']);
  await writeFile(join(guestSite, 'public-data.json'), JSON.stringify({ state: { token: '', cases: [], digest: 'guest-v2-updated' }, cases: {} }));
  const next = await request(port, { path: '/guest/api/state', headers: plain });
  assert.equal(JSON.parse(next.body).digest, 'guest-v2-updated');
  await rm(join(guestSite, 'public-data.json'));
  assert.equal((await request(port, { headers: plain })).status, 503);
  assert.equal(seen.length, 0);
});

test('password mode serves only login assets without authentication and retains local-only routes', async t => {
  const { port, plain, seen } = await passwordFixture(t);
  assert.equal(validateConfig({ hostname: config.hostname, authMode: 'password' }).authMode, 'password');
  assert.throws(() => validateConfig({ ...config, authMode: 'none' }));
  for (const path of ['/auth/login', '/auth/login.css', '/auth/login.js']) {
    const result = await request(port, { path, headers: plain });
    assert.equal(result.status, 200);
    assert.equal(result.headers['cache-control'], 'no-store');
    assert.match(result.headers['content-security-policy'], /frame-ancestors 'none'/);
  }
  const navigation = await request(port, { path: '/?view=home', headers: { ...plain, accept: 'text/html' } });
  assert.equal(navigation.status, 303);
  assert.equal(navigation.headers.location, '/auth/login?next=%2F%3Fview%3Dhome');
  for (const path of ['/api/state', '/auth/session', '/style.css']) {
    const result = await request(port, { path, headers: headers() });
    assert.equal(result.status, 401, 'Cloudflare identity cannot replace the password session');
    assert.equal(result.headers['x-workbench-auth'], 'required');
    assert.equal(JSON.parse(result.body).login_url, '/auth/login');
    assert.ok(!result.body.includes('fixture-admin'));
  }
  for (const path of ['/api/remote-account', '/api/remote-account/', '/api/service/shutdown']) {
    assert.equal((await request(port, { path, headers: plain })).status, 403);
  }
  assert.equal(seen.length, 0);
});

test('password login, session, protected requests and logout use a private revocable cookie', async t => {
  const { port, plain, post, login, seen } = await passwordFixture(t);
  const signed = await login();
  assert.equal(signed.status, 200);
  assert.equal(JSON.parse(signed.body).username, 'fixture-admin');
  const rawCookie = signed.headers['set-cookie'][0];
  for (const flag of ['__Host-WorkbenchSession=', 'Secure', 'HttpOnly', 'SameSite=Lax', 'Path=/']) assert.ok(rawCookie.includes(flag));
  const cookie = rawCookie.split(';')[0];
  assert.equal((await request(port, { path: '/auth/session', headers: { ...plain, cookie } })).status, 200);
  const resource = await request(port, { path: '/api/state', headers: { ...plain, cookie: `${cookie}; CF_Authorization=private; other=kept` } });
  assert.equal(resource.body, 'protected-workbench');
  assert.equal(seen[0].cookie.trim(), 'other=kept');
  assert.equal((await request(port, { path: '/api/remote-account', headers: { ...plain, cookie } })).status, 403);
  const logout = await request(port, { path: '/auth/logout', method: 'POST', headers: { ...post, cookie } });
  assert.equal(logout.status, 200);
  assert.match(logout.headers['set-cookie'][0], /Max-Age=0/);
  assert.equal((await request(port, { path: '/api/state', headers: { ...plain, cookie } })).status, 401);
});

test('password endpoints require the expected host and same-origin writes with bounded JSON', async t => {
  const { port, plain, post, login, seen } = await passwordFixture(t);
  assert.equal((await login(undefined, { origin: 'https://other.example' })).status, 403);
  const missingOrigin = { ...post }; delete missingOrigin.origin;
  assert.equal((await request(port, { path: '/auth/login', method: 'POST', headers: missingOrigin, body: '{}' })).status, 403);
  assert.equal((await login(undefined, { host: 'other.example' })).status, 403);
  assert.equal((await login(undefined, { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await request(port, { path: '/auth/login', method: 'POST', headers: post, body: '{broken' })).status, 400);
  assert.equal((await login({ username: 'fixture-admin', password: 'x'.repeat(5000) })).status, 413);
  assert.equal((await request(port, { path: '/auth/logout', method: 'POST', headers: plain })).status, 403);
  assert.equal(seen.length, 0);
});

test('password failures are generic, rate limited, and account changes revoke sessions', async t => {
  const { port, plain, login, account, accountFile } = await passwordFixture(t);
  const signed = await login();
  const cookie = signed.headers['set-cookie'][0].split(';')[0];
  const wrong = await login({ username: 'fixture-admin', password: 'wrong-password' });
  const unknown = await login({ username: 'unknown-user', password: 'wrong-password' });
  assert.equal(wrong.status, 401);
  assert.equal(unknown.body, wrong.body);
  await writeFile(accountFile, JSON.stringify({ ...account, credential_id: '56'.repeat(16) }));
  assert.equal((await request(port, { path: '/auth/session', headers: { ...plain, cookie } })).status, 401);
  for (let index = 0; index < 5; index++) await login({ username: 'unknown-user', password: 'wrong-password' });
  const limited = await login();
  assert.equal(limited.status, 429);
  assert.ok(Number(limited.headers['retry-after']) > 0);
  await rm(accountFile);
  assert.equal((await request(port, { path: '/api/state', headers: { ...plain, cookie } })).status, 401);
});

const config = {
  hostname: 'workbench.example.com', teamDomain: 'test-team.cloudflareaccess.com',
  audience: 'workbench-test-audience', allowedEmails: ['owner@example.com'],
};
const instant = 1800000000000;
const { privateKey, publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
const claims = { iss: `https://${config.teamDomain}`, aud: [config.audience], exp: instant / 1000 + 3600, email: config.allowedEmails[0] };
const encode = value => Buffer.from(JSON.stringify(value)).toString('base64url');
function token(changes = {}, head = {}) {
  const input = `${encode({ alg: 'RS256', kid: 'test-key', typ: 'JWT', ...head })}.${encode({ ...claims, ...changes })}`;
  return `${input}.${sign('RSA-SHA256', Buffer.from(input), privateKey).toString('base64url')}`;
}
const resolveKey = async kid => { assert.equal(kid, 'test-key'); return publicKey; };
const options = { resolveKey, now: () => instant };
const headers = extra => ({ host: config.hostname, 'cf-access-jwt-assertion': token(), ...extra });

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}
async function close(server) {
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
function request(port, { path = '/', method = 'GET', headers: values = headers(), body } = {}) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: '127.0.0.1', port, path, method, headers: values, agent: false }, res => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('error', reject);
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString(), bytes: Buffer.concat(chunks) }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

test('configuration requires explicit team, audience, hostname, and email allowlist', () => {
  assert.equal(validateConfig({ ...config, teamDomain: `https://${config.teamDomain}/` }).teamDomain, config.teamDomain);
  for (const change of [
    { hostname: 'localhost:8767' }, { hostname: 'workbench.example.com:443' },
    { teamDomain: 'attacker.example.com' }, { audience: '' }, { allowedEmails: [] },
    { allowedEmails: ['*@example.com'] }, { allowedEmails: [''] }, { upstream: 'http://attacker.example.com' },
  ]) assert.throws(() => validateConfig({ ...config, ...change }));
});

test('RS256 verification pins issuer, audience, exact email, validity and signature', async () => {
  const verify = createAccessVerifier(config, options);
  assert.deepEqual(await verify(token()), { email: claims.email });
  for (const invalid of [
    undefined, '', 'unsigned', `${encode({ alg: 'none', kid: 'test-key' })}.${encode(claims)}.`,
    token({ exp: instant / 1000 }), token({ exp: '9999999999' }), token({ nbf: instant / 1000 + 1 }),
    token({ iss: 'https://attacker.example.com' }), token({ aud: 'another-audience' }),
    token({ email: 'OWNER@example.com' }), token({ email: 'other@example.com' }),
    token({}, { alg: 'HS256' }), token({}, { crit: ['b64'] }),
  ]) await assert.rejects(verify(invalid), error => error.status === 401);
  const pieces = token().split('.');
  pieces[1] = encode({ ...claims, exp: claims.exp + 1 });
  await assert.rejects(verify(pieces.join('.')), error => error.status === 401);
  const other = generateKeyPairSync('rsa', { modulusLength: 2048 });
  await assert.rejects(createAccessVerifier(config, { ...options, resolveKey: async () => other.publicKey })(token()), error => error.status === 401);
});

test('JWKS is pinned, cached, bounded, and expires closed on network failure', async () => {
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'test-key', alg: 'RS256', use: 'sig' };
  let requests = 0, clock = instant, fail = false;
  const resolver = createJwksResolver(config, {
    now: () => clock,
    fetchImpl: async (url, init) => {
      requests++;
      assert.equal(url, `https://${config.teamDomain}/cdn-cgi/access/certs`);
      assert.equal(init.redirect, 'error');
      assert.ok(init.signal instanceof AbortSignal);
      if (fail) throw new Error('offline');
      return Response.json({ keys: [jwk] });
    },
  });
  await Promise.all([resolver('test-key'), resolver('test-key')]);
  assert.equal(requests, 1);
  await assert.rejects(resolver('unknown-key'), error => error.status === 401);
  assert.equal(requests, 1, 'unknown IDs cannot cause unbounded JWKS fetches');
  clock += 300001;
  fail = true;
  await assert.rejects(resolver('test-key'), error => error.status === 503);
  await assert.rejects(resolver('test-key'), error => error.status === 503);
  assert.equal(requests, 2, 'a failure has a bounded refresh rate');
  for (const body of [
    { keys: Array.from({ length: 17 }, (_, n) => ({ ...jwk, kid: String(n) })) },
    { keys: [{ ...jwk, kty: 'oct' }] }, { keys: [jwk, jwk] },
  ]) await assert.rejects(createJwksResolver(config, { fetchImpl: async () => Response.json(body) })('test-key'), error => error.status === 503);
  await assert.rejects(createJwksResolver(config, { fetchImpl: async () => new Response(' '.repeat(262145)) })('test-key'), error => error.status === 503);
});

test('JWKS warmup coalesces the first request and refreshes valid keys without waiting on the network', async () => {
  const jwk = { ...publicKey.export({ format: 'jwk' }), kid: 'test-key', alg: 'RS256', use: 'sig' };
  let clock = instant, requests = 0, finishFetch, rejectFetch;
  const resolver = createJwksResolver(config, {
    now: () => clock,
    fetchImpl: () => {
      requests++;
      return new Promise((resolve, reject) => {
        finishFetch = () => resolve(Response.json({ keys: [jwk] }));
        rejectFetch = () => reject(new Error('offline'));
      });
    },
  });
  const warmup = resolver.warmup();
  const coldRequest = resolver('test-key');
  assert.equal(requests, 1, 'startup and a concurrent request share the same fetch');
  finishFetch();
  await warmup;
  const cachedKey = await coldRequest;
  await resolver.warmup();
  assert.equal(requests, 1, 'fresh keys do not need a network request');

  clock += 240001;
  assert.equal(await resolver('test-key'), cachedKey, 'a slow refresh cannot delay requests with an unexpired key');
  assert.equal(requests, 2);
  const refresh = resolver.warmup();
  rejectFetch();
  await assert.rejects(refresh, error => error.status === 503);
  assert.equal(await resolver('test-key'), cachedKey, 'failure keeps only the remaining original TTL');
  assert.equal(requests, 2, 'background refresh failures are also rate limited');

  clock += 60000;
  const expired = resolver('test-key');
  rejectFetch();
  await assert.rejects(expired, error => error.status === 503, 'an expired key is never used when refresh fails');
  assert.equal(requests, 3);
});

test('reuses upstream connections, authenticates reused requests, and reconnects after upstream close', async t => {
  let connections = 0, forwarded = 0;
  const upstream = http.createServer((req, res) => {
    forwarded++;
    if (req.url === '/close') res.setHeader('connection', 'close');
    res.end(req.url);
  });
  upstream.on('connection', () => connections++);
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  for (let index = 0; index < 12; index++) {
    const result = await request(port, { path: `/api/item-${index}` });
    assert.equal(result.status, 200);
    assert.equal(result.body, `/api/item-${index}`);
  }
  assert.equal(connections, 1, 'separate downstream requests reuse one upstream TCP connection');
  assert.equal((await request(port, { headers: { host: config.hostname } })).status, 401);
  assert.equal(forwarded, 12, 'a warm upstream connection does not bypass authentication');
  assert.equal((await request(port, { path: '/close' })).status, 200);
  assert.equal((await request(port, { path: '/after-close' })).body, '/after-close');
  assert.equal(connections, 2, 'upstream Connection: close is respected');
});

test('read routes reject bodies and writes use separate closing connections', async t => {
  const seen = [];
  let connections = 0;
  const upstream = http.createServer((req, res) => {
    seen.push({ method: req.method, connection: req.headers.connection });
    req.resume();
    req.on('end', () => res.end('ok'));
  });
  upstream.on('connection', () => connections++);
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  for (const method of ['GET', 'HEAD', 'OPTIONS']) {
    for (const framing of [{ 'content-length': '1' }, { 'transfer-encoding': 'chunked' }]) {
      assert.equal((await request(port, { method, headers: headers(framing), body: 'x' })).status, 400);
    }
  }
  assert.equal(seen.length, 0, 'unsupported read bodies never reach the upstream');
  assert.equal((await request(port)).status, 200);
  for (let index = 0; index < 2; index++) {
    assert.equal((await request(port, { method: 'POST', headers: headers({
      origin: `https://${config.hostname}`, 'content-length': '1',
    }), body: 'x' })).status, 200);
  }
  assert.equal((await request(port)).status, 200);
  assert.equal(connections, 3, 'each write opens its own connection; reads still reuse their connection');
  assert.deepEqual(seen.filter(item => item.method === 'POST').map(item => item.connection), ['close', 'close']);
});

test('HTTP forwarding authenticates every route and protects origins and local-only actions', async t => {
  const seen = [];
  const upstream = http.createServer((req, res) => {
    let bytes = 0;
    req.on('data', chunk => { bytes += chunk.length; });
    req.on('end', () => {
      seen.push({ path: req.url, method: req.method, headers: req.headers, bytes });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(seen.at(-1)));
    });
  });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  for (const path of ['/', '/api/state', '/api/local-media?id=private']) {
    assert.equal((await request(port, { path, headers: { host: config.hostname } })).status, 401);
  }
  assert.equal(seen.length, 0);
  for (const values of [
    { host: '127.0.0.1:8768' }, { origin: 'http://workbench.example.com' },
    { origin: 'https://evil.example.com' }, { 'sec-fetch-site': 'cross-site' },
  ]) assert.equal((await request(port, { headers: headers(values) })).status, 403);
  for (const values of [
    {}, { origin: `https://${config.hostname}`, 'sec-fetch-site': 'same-site' },
  ]) assert.equal((await request(port, { method: 'POST', headers: headers(values), body: '{}' })).status, 403);
  for (const path of ['/api/service/shutdown', '/api/ai/claude/login', '/api/ai/claude/callback', '/api/ai/claude/login/cancel', '/api/ai/claude/key/delete', '/api/ai/claude/install', '/%61pi/service/shutdown']) {
    assert.equal((await request(port, { path, method: 'POST', headers: headers({ origin: `https://${config.hostname}` }), body: '{}' })).status, 403);
  }
  for (const path of ['/api/service/shutdown;x', '/api/service/restart;ignored', '/api/ai/claude/key;x', '/api/ai/claude/login;ignored', '/api/ai/claude/callback%3Bx', '/api/service/shutdown%3bx']) {
    assert.equal((await request(port, { path, method: 'POST', headers: headers({ origin: `https://${config.hostname}` }), body: '{}' })).status, 400,
      'Python-style semicolon parameters must never reach the backend');
  }
  assert.equal(seen.length, 0, 'all denied routes are blocked before upstream');
  const result = await request(port, { path: '/api/plans', method: 'POST', headers: headers({
    origin: `https://${config.hostname}`, 'sec-fetch-site': 'same-origin', 'x-workbench-token': 'real-local-token',
    'content-type': 'application/json', 'x-forwarded-host': 'evil.example.com', 'x-forwarded-for': '10.0.0.1',
    forwarded: 'for=evil', 'cf-access-authenticated-user-email': 'spoof@example.com',
    connection: 'close, x-remove-me', 'x-remove-me': 'do not forward',
    cookie: 'CF_Authorization=secret; app=value',
  }), body: '{"ok":true}' });
  assert.equal(result.status, 200);
  const forwarded = JSON.parse(result.body);
  assert.equal(forwarded.headers.host, `127.0.0.1:${upstreamPort}`);
  assert.equal(forwarded.headers.origin, `http://127.0.0.1:${upstreamPort}`);
  assert.equal(forwarded.headers['x-workbench-token'], 'real-local-token');
  assert.equal(forwarded.bytes, 11);
  assert.equal(forwarded.headers.cookie, 'app=value');
  for (const name of ['x-forwarded-host', 'x-forwarded-for', 'forwarded', 'cf-access-jwt-assertion', 'cf-access-authenticated-user-email', 'x-remove-me']) assert.equal(forwarded.headers[name], undefined);
  assert.equal((await request(port, { path: '/api/ai/claude/login' })).status, 200, 'read-only login state remains available');
  const navigation = headers({ 'sec-fetch-site': 'cross-site', 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' });
  const entered = await request(port, { headers: navigation });
  assert.equal(entered.status, 200, 'authenticated top-level entry from a link or Access redirect works');
  assert.equal(JSON.parse(entered.body).headers['sec-fetch-site'], undefined);
  assert.equal((await request(port, { path: '/api/state', headers: navigation })).status, 403, 'navigation metadata cannot permit cross-site API access');
  assert.equal((await request(port, { path: '/', method: 'POST', headers: navigation })).status, 403);
});

test('reject duplicate security headers and absolute-form request targets before forwarding', async t => {
  let reached = false;
  const upstream = http.createServer((_req, res) => { reached = true; res.end('unexpected'); });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  async function raw(target, extra = '') {
    const socket = net.connect(port, '127.0.0.1');
    await once(socket, 'connect');
    socket.write(`GET ${target} HTTP/1.1\r\nHost: ${config.hostname}\r\nCf-Access-Jwt-Assertion: ${token()}\r\n${extra}Connection: close\r\n\r\n`);
    let result = '';
    socket.on('data', data => { result += data; });
    await once(socket, 'end');
    return result;
  }
  for (const duplicate of [
    `Host: ${config.hostname}\r\n`, `Cf-Access-Jwt-Assertion: ${token()}\r\n`,
    `Origin: https://${config.hostname}\r\nOrigin: https://${config.hostname}\r\n`,
    'Content-Length: 0\r\nContent-Length: 0\r\n',
  ]) assert.match(await raw('/', duplicate), /^HTTP\/1\.1 400 /);
  assert.match(await raw(`https://${config.hostname}/api/state`), /^HTTP\/1\.1 400 /);
  assert.equal(reached, false);
});

test('preserves browser caching only for authenticated program assets', async t => {
  let reached = 0;
  const upstream = http.createServer((req, res) => {
    reached++;
    const path = new URL(req.url, 'http://localhost').pathname;
    const plain = path === '/style.css';
    const error = path === '/error.js';
    const privateData = path === '/private.js';
    const cookie = path === '/cookie.js';
    const status = error ? 404 : req.headers['if-none-match'] ? 304 : 200;
    res.writeHead(status, { 'content-type': privateData ? 'application/json' : plain ? 'text/css' : 'text/javascript',
      'cache-control': plain ? 'private, no-cache' : 'private, max-age=31536000, immutable',
      etag: '"asset-v1"', ...(cookie ? { 'set-cookie': 'private=value' } : {}) });
    res.end(status === 304 ? undefined : 'fixture');
  });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  const path = '/app.js?v=123456abcdef';
  const asset = await request(port, { path });
  assert.equal(asset.headers['cache-control'], 'private, max-age=31536000, immutable');
  assert.equal(asset.headers.vary, 'Cookie');
  const checked = await request(port, { path, headers: headers({ 'if-none-match': '"asset-v1"' }) });
  assert.equal(checked.status, 304);
  assert.equal(checked.headers['cache-control'], asset.headers['cache-control']);
  assert.equal((await request(port, { path: '/style.css' })).headers['cache-control'], 'private, no-cache');
  for (const unsafe of ['/', '/index.html?v=123456abcdef', '/api/state?v=123456abcdef', '/error.js?v=123456abcdef',
    '/private.js?v=123456abcdef', '/cookie.js?v=123456abcdef', '/app.js', '/app.js?v=old', '/app.js?v=123456abcdef&v=123456abcdef']) {
    assert.equal((await request(port, { path: unsafe })).headers['cache-control'], 'private, no-store', unsafe);
  }
  const before = reached;
  assert.equal((await request(port, { path, headers: { host: config.hostname } })).status, 401);
  assert.equal(reached, before, 'cached static responses never bypass authentication');
});

test('streams SSE before completion, cancels on disconnect, and preserves media Range/status/headers', async t => {
  let finishEvents, signalClosed;
  const streamClosed = new Promise(resolve => { signalClosed = resolve; });
  const upstream = http.createServer((req, res) => {
    if (req.url === '/events') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
      res.write('data: first\n\n');
      finishEvents = () => res.end('data: last\n\n');
    } else if (req.url === '/cancel') {
      res.writeHead(200, { 'content-type': 'text/event-stream' });
      res.write(': connected\n\n');
      res.on('close', signalClosed);
    } else if (req.url === '/cached') {
      assert.equal(req.headers['if-none-match'], '"media-v1"');
      res.writeHead(304, { etag: '"media-v1"' });
      res.end();
    } else if (req.url === '/gzip') {
      assert.equal(req.headers['accept-encoding'], 'gzip');
      const bytes = gzipSync('{"value":"compressed"}');
      res.writeHead(200, { 'content-type': 'application/json', 'content-encoding': 'gzip', 'content-length': String(bytes.length), vary: 'Accept-Encoding' });
      res.end(bytes);
    } else {
      assert.equal(req.headers.range, 'bytes=2-5');
      assert.equal(req.headers['if-none-match'], '"media-v1"');
      res.writeHead(206, { 'content-type': 'video/mp4', 'content-range': 'bytes 2-5/10', 'accept-ranges': 'bytes', etag: '"media-v1"', 'content-length': '4' });
      res.end('2345');
    }
  });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  await new Promise((resolve, reject) => {
    const req = http.get({ hostname: '127.0.0.1', port, path: '/events', headers: headers(), agent: false }, res => {
      let received = '';
      res.on('data', chunk => {
        received += chunk;
        if (received === 'data: first\n\n') finishEvents();
      });
      res.on('end', () => { assert.equal(received, 'data: first\n\ndata: last\n\n'); resolve(); });
    });
    req.on('error', reject);
  });
  await new Promise((resolve, reject) => {
    const req = http.get({ hostname: '127.0.0.1', port, path: '/cancel', headers: headers(), agent: false }, res => {
      res.once('data', () => { req.destroy(); resolve(); });
    });
    req.on('error', reject);
  });
  await Promise.race([streamClosed, new Promise((_, reject) => setTimeout(() => reject(new Error('Upstream did not close after client disconnect.')), 2000).unref())]);
  const media = await request(port, { path: '/media', headers: headers({ range: 'bytes=2-5', 'if-none-match': '"media-v1"' }) });
  assert.equal(media.status, 206);
  assert.equal(media.body, '2345');
  assert.equal(media.headers['content-range'], 'bytes 2-5/10');
  assert.equal(media.headers['accept-ranges'], 'bytes');
  assert.equal(media.headers.etag, '"media-v1"');
  const cached = await request(port, { path: '/cached', headers: headers({ 'if-none-match': '"media-v1"' }) });
  assert.equal(cached.status, 304);
  assert.equal(cached.body, '');
  const compressed = await request(port, { path: '/gzip', headers: headers({ 'accept-encoding': 'gzip' }) });
  assert.equal(compressed.headers['content-encoding'], 'gzip');
  assert.equal(compressed.headers.vary, 'Accept-Encoding, Cookie');
  assert.equal(gunzipSync(compressed.bytes).toString(), '{"value":"compressed"}');
});

test('streams large chunked uploads incrementally with the workbench token', async t => {
  let allowRemaining;
  const firstSeen = new Promise(resolve => { allowRemaining = resolve; });
  const expectedBytes = 8 * 1024 * 1024;
  const upstream = http.createServer((req, res) => {
    assert.equal(req.headers['x-workbench-token'], 'upload-token');
    let count = 0;
    req.on('data', chunk => { count += chunk.length; allowRemaining(); });
    req.on('end', () => { res.end(String(count)); });
  });
  const upstreamPort = await listen(upstream);
  const proxy = createRemoteProxy(config, { ...options, upstream: `http://127.0.0.1:${upstreamPort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); });
  const req = http.request({ hostname: '127.0.0.1', port, path: '/upload', method: 'POST', agent: false,
    headers: headers({ origin: `https://${config.hostname}`, 'x-workbench-token': 'upload-token' }) });
  const finished = new Promise((resolve, reject) => {
    req.on('response', res => { let result = ''; res.on('data', chunk => { result += chunk; }); res.on('end', () => resolve(result)); });
    req.on('error', reject);
  });
  const chunk = Buffer.alloc(65536, 77);
  req.write(chunk);
  await Promise.race([firstSeen, new Promise((_, reject) => setTimeout(() => reject(new Error('Upload was buffered.')), 2000).unref())]);
  for (let sent = chunk.length; sent < expectedBytes; sent += chunk.length) if (!req.write(chunk)) await once(req, 'drain');
  req.end();
  assert.equal(await finished, String(expectedBytes));
});
