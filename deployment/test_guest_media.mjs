import assert from 'node:assert/strict';
import { test } from 'node:test';
import http from 'node:http';
import { once } from 'node:events';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRemoteProxy } from './remote_proxy.mjs';
import { streamGuestMedia } from './guest_media.mjs';

const HOST = 'workbench.example.com';
const CASE = 'PVR-000000000001';
const EVENT = '20261016T022934.380467Z-00000000000000000000000000000001';
const index = { schema: 1, cases: { [CASE]: { current_event_id: EVENT,
  events: { [EVENT]: ['video', 'screenshot', 'contact_sheet'] } } } };
async function listen(server) { server.listen(0, '127.0.0.1'); await once(server, 'listening'); return server.address().port; }
async function close(server) { server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); }
async function fixture(t, handler) {
  const root = await mkdtemp(join(tmpdir(), 'guest-media-test-'));
  const site = join(root, 'site'); await mkdir(site);
  await writeFile(join(site, 'public-data.json'), JSON.stringify({ state: { token: '', cases: [] }, cases: {} }));
  await writeFile(join(site, 'media-index.json'), JSON.stringify(index));
  await writeFile(join(site, 'index.html'), '<!doctype html><title>Guest</title>');
  const seen = [];
  const upstream = http.createServer((req, res) => { seen.push({ path: req.url, headers: req.headers }); handler(req, res); });
  const sourcePort = await listen(upstream);
  const proxy = createRemoteProxy({ hostname: HOST, authMode: 'password', guestEnabled: true },
    { guestSite: site, accountFile: join(root, 'absent-account.json'), upstream: `http://127.0.0.1:${sourcePort}` });
  const port = await listen(proxy);
  t.after(async () => { await close(proxy); await close(upstream); await rm(root, { recursive: true, force: true }); });
  return { port, site, seen };
}
function get(port, path, headers = {}, method = 'GET') {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method, headers: { host: HOST, ...headers } }, res => {
      const chunks = []; res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks).toString() }));
      res.on('error', reject);
    });
    req.on('error', reject); req.end();
  });
}
const path = (kind = 'video') => `/guest-media/${CASE}/${kind}`;

test('a browser disconnected during permission lookup starts no upstream operation', () => {
  const untouched = new Proxy({}, { get() { throw new Error('upstream must not be touched'); } });
  streamGuestMedia({ destroyed: true }, {}, untouched, untouched);
  streamGuestMedia({ destroyed: false }, { destroyed: true }, untouched, untouched);
});

test('anonymous media is pinned to approved case/event and streams ranges without credentials', async t => {
  const { port, seen } = await fixture(t, (req, res) => {
    const image = req.url.includes('/thumbnail') || req.url.includes('/screenshot') || req.url.includes('/contact_sheet');
    const partial = !!req.headers.range;
    res.writeHead(partial ? 206 : 200, { 'content-type': image ? 'image/jpeg' : 'video/mp4',
      'content-length': partial ? 4 : 10, ...(partial ? { 'content-range': 'bytes 2-5/10' } : {}),
      'set-cookie': 'private=do-not-forward', 'x-local-path': 'C:/private/original.mp4' });
    res.end(req.method === 'HEAD' ? undefined : partial ? '2345' : '0123456789');
  });
  const partial = await get(port, path(), { range: 'bytes=2-5', cookie: 'private=secret', authorization: 'Bearer private', 'x-workbench-token': 'secret' });
  assert.equal(partial.status, 206); assert.equal(partial.body, '2345');
  assert.equal(partial.headers['content-range'], 'bytes 2-5/10');
  assert.equal(partial.headers['content-disposition'], 'inline');
  assert.equal(partial.headers['set-cookie'], undefined); assert.equal(partial.headers['x-local-path'], undefined);
  assert.equal(seen[0].path, `/media/${CASE}/video?event=${EVENT}`);
  for (const name of ['cookie', 'authorization', 'x-workbench-token']) assert.equal(seen[0].headers[name], undefined);
  const head = await get(port, path(), {}, 'HEAD');
  assert.equal(head.status, 200); assert.equal(head.body, ''); assert.equal(head.headers['content-length'], '10');
  for (const kind of ['thumbnail', 'screenshot', 'contact_sheet']) assert.equal((await get(port, path(kind))).status, 200);
  const conditional = await get(port, path(), { range: 'bytes=2-5', 'if-range': 'old-entity' });
  assert.equal(conditional.status, 200); assert.equal(conditional.body, '0123456789');
  const document = await get(port, '/');
  assert.match(document.headers['content-security-policy'], /img-src 'self' data:/);
  assert.match(document.headers['content-security-policy'], /media-src 'self'/);
});

test('unlisted files, versions, downloads, malformed ranges and writes never reach upstream', async t => {
  const { port, seen } = await fixture(t, (_req, res) => res.end('unexpected'));
  for (const url of [path('transcript'), path('source_manifest'), path('performance'), path('audio'),
    path() + '?download', path() + '?event=unknown', path() + '?event=', path() + '?event=' + EVENT + '&event=' + EVENT,
    path() + '?path=C%3A%2Fprivate', '/guest-media/PVR-000000000002/video',
    '/guest-assets/media-index.json', '/guest/api/media-index']) {
    assert.equal((await get(port, url)).status, 404, url);
  }
  assert.equal((await get(port, path(), { range: 'bytes=0-1,4-5' })).status, 416);
  assert.equal((await get(port, path(), { origin: `https://${HOST}` }, 'POST')).status, 405);
  assert.equal((await get(port, `/media/${CASE}/video`)).status, 401);
  assert.equal(seen.length, 0);
});

test('missing or corrupt media permissions revoke access even after a successful cached read', async t => {
  const { port, site, seen } = await fixture(t, (_req, res) => { res.writeHead(200, { 'content-type': 'video/mp4' }); res.end('video'); });
  assert.equal((await get(port, path())).status, 200);
  await writeFile(join(site, 'media-index.json'), '{invalid');
  assert.equal((await get(port, path())).status, 404);
  await writeFile(join(site, 'media-index.json'), JSON.stringify(index));
  assert.equal((await get(port, path())).status, 200);
  await rm(join(site, 'media-index.json'));
  assert.equal((await get(port, path())).status, 404);
  assert.equal(seen.length, 2);
});

test('upstream errors, redirects and non-media bodies cannot disclose local details', async t => {
  let mode = 'error';
  const { port } = await fixture(t, (_req, res) => {
    if (mode === 'error') res.writeHead(404, { 'content-type': 'application/json' });
    else if (mode === 'redirect') res.writeHead(302, { location: 'http://127.0.0.1/private' });
    else if (mode === 'range') res.writeHead(416, { 'content-range': 'bytes */10' });
    else res.writeHead(200, { 'content-type': 'image/svg+xml' });
    res.end('C:/private/original.mp4');
  });
  for (const [next, status] of [['error', 404], ['redirect', 502], ['svg', 502], ['range', 416]]) {
    mode = next; const result = await get(port, path());
    assert.equal(result.status, status); assert.doesNotMatch(result.body, /private|original/);
    assert.equal(result.headers.location, undefined);
    if (mode === 'range') assert.equal(result.headers['content-range'], 'bytes */10');
  }
});

test('media starts reaching the browser before the full source finishes', async t => {
  let finish;
  const { port } = await fixture(t, (_req, res) => {
    res.writeHead(200, { 'content-type': 'video/mp4' }); res.write('first');
    finish = () => res.end('last');
  });
  const body = await new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port, path: path(), headers: { host: HOST } }, res => {
      let text = ''; res.on('data', chunk => { text += chunk; if (text === 'first') finish(); });
      res.on('end', () => resolve(text)); res.on('error', reject);
    }).on('error', reject);
  });
  assert.equal(body, 'firstlast');
});

test('guest images revalidate privately and permission revocation still denies cached previews', async t => {
  const etag = '"public-image-v1"';
  const { port, site, seen } = await fixture(t, (req, res) => {
    if (req.headers['if-none-match'] === etag) { res.writeHead(304, { etag }); res.end(); return; }
    res.writeHead(200, { 'content-type': 'image/jpeg', etag }); res.end('image');
  });
  const first = await get(port, path('thumbnail'));
  assert.equal(first.headers.etag, etag);
  assert.equal(first.headers['cache-control'], 'private, no-cache');
  const checked = await get(port, path('thumbnail'), { 'if-none-match': etag });
  assert.equal(checked.status, 304);
  assert.equal(checked.body, '');
  assert.equal(checked.headers.etag, etag);
  assert.equal(seen[1].headers['if-none-match'], etag);
  await rm(join(site, 'media-index.json'));
  assert.equal((await get(port, path('thumbnail'), { 'if-none-match': etag })).status, 404);
  assert.equal(seen.length, 2);
});

test('unsolicited media not-modified responses cannot bypass a complete validated response', async t => {
  const { port } = await fixture(t, (_req, res) => { res.writeHead(304, { etag: '"image"' }); res.end(); });
  assert.equal((await get(port, path('thumbnail'))).status, 502);
  assert.equal((await get(port, path(), { 'if-none-match': '"image"' })).status, 502);
});
