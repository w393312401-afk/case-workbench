import assert from 'node:assert/strict';
import { test } from 'node:test';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { gunzipSync } from 'node:zlib';
import { createGuestView } from './guest_view.mjs';

const origin = 'https://workbench.example.com';
const script = 'window.PublicExample = ' + JSON.stringify('public text '.repeat(2000)) + ';';
const version = value => createHash('sha256').update(value).digest('hex').slice(0, 12);
const bundle = digest => JSON.stringify({ state: { token: '', cases: [], digest, description: 'public text '.repeat(2000) }, cases: {} });
async function fixture(t) {
  const site = await mkdtemp(join(tmpdir(), 'guest-view-test-'));
  await writeFile(join(site, 'public-data.json'), bundle('first'));
  await writeFile(join(site, 'app.js'), script);
  await writeFile(join(site, 'index.html'), '<!doctype html><title>Public</title>' + 'public text '.repeat(2000));
  t.after(() => rm(site, { recursive: true, force: true }));
  const view = createGuestView({ site });
  return { site, get: (path = '/', headers = {}, method = 'GET') => view.fetch(origin + path, method, headers) };
}

test('guest JSON and documents negotiate gzip while retaining no-store', async t => {
  const { get } = await fixture(t);
  for (const path of ['/', '/guest/api/state']) {
    const plain = await get(path);
    const content = Buffer.from(await plain.arrayBuffer());
    const packed = await get(path, { 'accept-encoding': 'br, gzip;q=0.8' });
    assert.equal(packed.headers.get('content-encoding'), 'gzip');
    assert.equal(packed.headers.get('cache-control'), 'private, no-store');
    assert.equal(packed.headers.get('vary'), 'Cookie, Accept-Encoding');
    assert.equal(packed.headers.get('etag'), null);
    const bytes = Buffer.from(await packed.arrayBuffer());
    assert.deepEqual(gunzipSync(bytes), content);
    assert.ok(bytes.length < content.length / 5);
    const head = await get(path, { 'accept-encoding': 'gzip' }, 'HEAD');
    assert.equal(head.headers.get('content-length'), String(bytes.length));
    assert.equal(await head.text(), '');
  }
  for (const value of ['gzip;q=0, *;q=1', 'br', '*;q=0', 'gzip;q=invalid']) {
    const result = await get('/guest/api/state', { 'accept-encoding': value });
    assert.equal(result.headers.get('content-encoding'), null, value);
  }
});

test('guest assets validate representation tags and only matching content versions become immutable', async t => {
  const { get } = await fixture(t);
  const plain = await get('/guest-assets/app.js');
  const tag = plain.headers.get('etag');
  assert.ok(tag);
  assert.equal(plain.headers.get('cache-control'), 'private, no-cache');
  assert.equal(await plain.text(), script);
  const checked = await get('/guest-assets/app.js', { 'if-none-match': 'W/' + tag });
  assert.equal(checked.status, 304);
  assert.equal(await checked.text(), '');
  const packed = await get('/guest-assets/app.js', { 'accept-encoding': 'gzip', 'if-none-match': tag });
  assert.equal(packed.status, 200);
  assert.notEqual(packed.headers.get('etag'), tag);
  assert.equal(gunzipSync(Buffer.from(await packed.arrayBuffer())).toString(), script);
  const valid = '/guest-assets/app.js?v=' + version(script);
  assert.equal((await get(valid)).headers.get('cache-control'), 'private, max-age=31536000, immutable');
  for (const path of ['/guest-assets/app.js?v=old', valid + '&v=' + version(script)]) {
    assert.equal((await get(path)).headers.get('cache-control'), 'private, no-cache');
  }
  const missing = await get('/guest-assets/missing.js', { 'if-none-match': '*' });
  assert.equal(missing.status, 404);
  assert.equal(missing.headers.get('cache-control'), 'private, no-store');
});

test('cached guest assets are replaced with a new snapshot and fail closed without its manifest', async t => {
  const { get, site } = await fixture(t);
  const first = await get('/guest-assets/app.js');
  const tag = first.headers.get('etag');
  const updated = script + '\nwindow.NewSnapshot = true;';
  await writeFile(join(site, 'app.js'), updated);
  await writeFile(join(site, 'public-data.json'), bundle('second-generation'));
  const next = await get('/guest-assets/app.js?v=' + version(script), { 'if-none-match': tag });
  assert.equal(next.status, 200);
  assert.equal(await next.text(), updated);
  assert.equal(next.headers.get('cache-control'), 'private, no-cache');
  assert.notEqual(next.headers.get('etag'), tag);
  await rm(join(site, 'public-data.json'));
  const unavailable = await get('/guest-assets/app.js', { 'if-none-match': '*' });
  assert.equal(unavailable.status, 503);
  assert.equal(unavailable.headers.get('cache-control'), 'private, no-store');
});
