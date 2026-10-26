import { readFile, realpath, stat } from 'node:fs/promises';
import { extname, isAbsolute, resolve, sep } from 'node:path';
import { createHash } from 'node:crypto';
import { promisify } from 'node:util';
import { gzip } from 'node:zlib';
import worker from './text-only/_worker.js';

const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.txt': 'text/plain; charset=utf-8' };
const unavailable = () => Response.json({ error: '访客内容正在准备，请稍后刷新。' }, { status: 503 });
const missing = () => new Response('Not found', { status: 404 });
const pack = promisify(gzip);

function acceptsGzip(value = '') {
  let explicit, wildcard;
  for (const entry of String(value).toLowerCase().split(',')) {
    const [coding, ...parameters] = entry.trim().split(';');
    let quality = 1;
    for (const parameter of parameters) {
      const match = /^\s*q\s*=\s*(0(?:\.\d{0,3})?|1(?:\.0{0,3})?)\s*$/.exec(parameter);
      if (/^\s*q\s*=/i.test(parameter)) quality = match ? Number(match[1]) : 0;
    }
    if (coding === 'gzip') explicit = quality;
    if (coding === '*') wildcard = quality;
  }
  return (explicit ?? wildcard ?? 0) > 0;
}

async function representation(result, compressed) {
  const bytes = Buffer.from(await result.arrayBuffer());
  const headers = new Headers(result.headers);
  const digest = createHash('sha256').update(bytes).digest('hex');
  let body = bytes;
  if (compressed && bytes.length >= 1024 && /^(?:text\/|application\/(?:json|javascript))/i.test(headers.get('content-type') || '')) {
    const packed = await pack(bytes, { level: 6 });
    if (packed.length < bytes.length) { body = packed; headers.set('content-encoding', 'gzip'); }
  }
  headers.set('content-length', String(body.length));
  return { status: result.status, headers, body, digest,
    etag: `"guest-${digest}-${headers.has('content-encoding') ? 'gzip' : 'identity'}"` };
}

/** Only the already-sanitized snapshot is readable here. There is no origin connection. */
export function createGuestView({ site }) {
  if (typeof site !== 'string' || !isAbsolute(site)) throw new Error('An absolute guest snapshot directory is required.');
  const root = resolve(site);
  let generation, environment, mediaSignature, mediaIndex;
  async function currentMediaIndex() {
    try {
      // Check the allowlist itself on every request, including deletion or a
      // failed replacement. Never retain permission from a missing index.
      await stat(resolve(root, 'public-data.json'));
      const canonicalRoot = await realpath(root);
      const file = await realpath(resolve(root, 'media-index.json'));
      if (!file.startsWith(canonicalRoot + sep)) throw new Error();
      const info = await stat(file, { bigint: true });
      if (!info.isFile() || info.size > 2n * 1024n * 1024n) throw new Error();
      const signature = `${info.ino}:${info.size}:${info.mtimeNs}:${info.ctimeNs}`;
      if (!mediaIndex || signature !== mediaSignature) {
        const value = JSON.parse(await readFile(file, 'utf8'));
        if (value?.schema !== 1 || !value.cases || Array.isArray(value.cases) || typeof value.cases !== 'object') throw new Error();
        mediaIndex = value;
        mediaSignature = signature;
      }
      return mediaIndex;
    } catch { mediaIndex = undefined; mediaSignature = undefined; return null; }
  }
  async function currentEnvironment() {
    const manifest = await stat(resolve(root, 'public-data.json'), { bigint: true });
    const signature = `${manifest.ino}:${manifest.size}:${manifest.mtimeNs}:${manifest.ctimeNs}`;
    if (signature !== generation || !environment) {
      const canonicalRoot = await realpath(root);
      const assets = new Map();
      environment = { representations: new Map(), ASSETS: { async fetch(request) {
        const path = decodeURIComponent(new URL(request.url).pathname);
        const relative = path === '/' ? 'index.html' : path.slice(1);
        const candidate = resolve(root, relative);
        if (!candidate.startsWith(root + sep) || !TYPES[extname(candidate)]) return missing();
        try {
          const resolved = await realpath(candidate);
          if (!resolved.startsWith(canonicalRoot + sep)) return missing();
          let promise = assets.get(relative);
          if (!promise) {
            const info = await stat(resolved);
            if (!info.isFile() || info.size > 25 * 1024 * 1024) return missing();
            promise = readFile(resolved).catch(error => { assets.delete(relative); throw error; });
            assets.set(relative, promise);
          }
          return new Response(await promise, { headers: { 'content-type': TYPES[extname(resolved)] } });
        } catch { return missing(); }
      } } };
      generation = signature;
    }
    return environment;
  }
  return Object.freeze({
    async media(url) {
      const target = new URL(url);
      const match = /^\/guest-media\/(PVR-[0-9a-f]{12})\/(video|thumbnail|screenshot|contact_sheet)$/.exec(target.pathname);
      if (!match || [...target.searchParams.keys()].some(key => key !== 'event') || target.searchParams.getAll('event').length > 1) return null;
      const index = await currentMediaIndex();
      const record = index?.cases?.[match[1]];
      const event = target.searchParams.has('event') ? target.searchParams.get('event') : record?.current_event_id;
      if (typeof event !== 'string' || !/^[A-Za-z0-9_.-]{1,128}$/.test(event)) return null;
      const kinds = record?.events?.[event];
      if (!Array.isArray(kinds) || !kinds.includes(match[2] === 'thumbnail' ? 'video' : match[2])) return null;
      // Pin every read to the exported event, never to a later current file.
      return { kind: match[2], path: `/media/${match[1]}/${match[2]}?event=${encodeURIComponent(event)}` };
    },
    async fetch(url, method = 'GET', browserHeaders = {}) {
      let prepared, immutable = false, asset = false;
      try {
        const target = new URL(url);
        const path = target.pathname;
        if (path.startsWith('/guest/api/')) target.pathname = path.slice('/guest'.length);
        else if (path.startsWith('/guest-assets/') && /^\/guest-assets\/[A-Za-z0-9_-]+\.(?:html|css|js)$/.test(path)) {
          target.pathname = path.slice('/guest-assets'.length);
          asset = /\.(?:css|js)$/.test(path) && ['GET', 'HEAD'].includes(method);
        } else if (!['/', '/index.html', '/robots.txt'].includes(path)) return missing();
        const env = await currentEnvironment();
        const compressed = acceptsGzip(browserHeaders['accept-encoding']);
        const key = `${target.pathname}:${compressed ? 'gzip' : 'identity'}`;
        // Deliberately omit cookies, tokens, authorization and all browser headers.
        // HEAD uses the same representation metadata as GET, without a body.
        const request = new Request(target, { method: method === 'HEAD' ? 'GET' : method });
        let pending = asset ? env.representations.get(key) : undefined;
        if (!pending) {
          pending = worker.fetch(request, env).then(result => representation(result, compressed));
          if (asset) {
            env.representations.set(key, pending);
            pending.then(value => { if (value.status !== 200) env.representations.delete(key); }, () => env.representations.delete(key));
          }
        }
        prepared = await pending;
        immutable = asset && target.searchParams.getAll('v').length === 1
          && target.searchParams.get('v') === prepared.digest.slice(0, 12);
      } catch { prepared = await representation(unavailable(), false); }
      const headers = new Headers(prepared.headers);
      const policy = headers.get('content-security-policy');
      if (policy) headers.set('content-security-policy', policy.replace("img-src 'none'", "img-src 'self' data:").replace("media-src 'none'", "media-src 'self'"));
      const cacheable = asset && prepared.status === 200;
      headers.set('cache-control', cacheable ? immutable ? 'private, max-age=31536000, immutable' : 'private, no-cache' : 'private, no-store');
      headers.set('vary', 'Cookie, Accept-Encoding');
      headers.set('x-workbench-view', 'guest');
      headers.set('x-robots-tag', 'noindex, nofollow');
      if (cacheable) headers.set('etag', prepared.etag);
      else headers.delete('etag');
      headers.delete('last-modified');
      const supplied = String(browserHeaders['if-none-match'] || '').split(',').map(value => value.trim().replace(/^W\//, ''));
      if (cacheable && (supplied.includes('*') || supplied.includes(prepared.etag))) {
        headers.delete('content-length');
        return new Response(null, { status: 304, headers });
      }
      return new Response(method === 'HEAD' ? null : prepared.body, { status: prepared.status, headers });
    },
  });
}
