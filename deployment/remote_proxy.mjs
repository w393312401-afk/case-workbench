import http from 'node:http';
import { createPublicKey, verify } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { dirname, isAbsolute, join, resolve } from 'node:path';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { createPasswordAuth } from './password_auth.mjs';
import { createGuestView } from './guest_view.mjs';
import { streamGuestMedia } from './guest_media.mjs';

const UPSTREAM = 'http://127.0.0.1:8767';
const LISTEN_HOST = '127.0.0.1';
const LISTEN_PORT = 8768;
const MAX_TOKEN = 16384;
const JWKS_TIMEOUT_MS = 5000;
const JWKS_MAX_BYTES = 262144;
const JWKS_MAX_KEYS = 16;
const JWKS_TTL_MS = 300000;
const JWKS_REFRESH_GAP_MS = 30000;
const JWKS_REFRESH_AHEAD_MS = 60000;
const IDLE_TIMEOUT_MS = 30 * 60 * 1000;
const HOP_HEADERS = new Set([
  'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
  'te', 'trailer', 'transfer-encoding', 'upgrade', 'expect',
]);
const SAFE_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

class RequestError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export function validateConfig(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Config must be an object.');
  const allowed = new Set(['hostname', 'teamDomain', 'audience', 'allowedEmails', 'nodePath', 'cloudflaredPath', 'httpsProxy', 'authMode', 'guestEnabled']);
  if (Object.keys(value).some(key => !allowed.has(key))) throw new Error('Config contains an unsupported field.');
  const dns = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
  if (typeof value.hostname !== 'string' || !dns.test(value.hostname)) throw new Error('hostname must be a lowercase DNS hostname.');
  const authMode = value.authMode ?? 'cloudflare';
  if (!['cloudflare', 'password'].includes(authMode)) throw new Error('Unsupported authentication mode.');
  const guestEnabled = value.guestEnabled ?? false;
  if (typeof guestEnabled !== 'boolean' || (guestEnabled && authMode !== 'password')) throw new Error('Guest browsing requires password mode.');
  if (authMode === 'password') return Object.freeze({ hostname: value.hostname, authMode, guestEnabled });
  if (typeof value.teamDomain !== 'string') throw new Error('teamDomain is required.');
  const teamDomain = value.teamDomain.replace(/^https:\/\//, '').replace(/\/$/, '');
  if (!/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.cloudflareaccess\.com$/.test(teamDomain)) {
    throw new Error('teamDomain must be the Cloudflare Access team hostname.');
  }
  if (typeof value.audience !== 'string' || !/^[A-Za-z0-9_-]{8,256}$/.test(value.audience)) throw new Error('audience is required.');
  if (!Array.isArray(value.allowedEmails) || !value.allowedEmails.length || value.allowedEmails.length > 100
      || value.allowedEmails.some(email => typeof email !== 'string' || email.length > 254
        || /[\x00-\x20\x7f*]/.test(email)
        || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email))) throw new Error('allowedEmails must contain explicit email addresses.');
  return Object.freeze({
    hostname: value.hostname, authMode, teamDomain, audience: value.audience,
    allowedEmails: Object.freeze([...new Set(value.allowedEmails)]),
  });
}

/** Only keys from this pinned HTTPS endpoint are trusted; JWT jku/jwk URLs are never used. */
export function createJwksResolver(config, { fetchImpl = fetch, now = Date.now } = {}) {
  const { teamDomain } = validateConfig(config);
  let cached = new Map(), expiresAt = 0, lastAttempt = -Infinity, pending;
  async function refresh() {
    lastAttempt = now();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), JWKS_TIMEOUT_MS);
    try {
      const response = await fetchImpl(`https://${teamDomain}/cdn-cgi/access/certs`, {
        signal: controller.signal, redirect: 'error', headers: { accept: 'application/json' },
      });
      if (!response.ok || !response.body) throw new Error('Key service failed.');
      const chunks = [];
      let size = 0;
      for await (const chunk of response.body) {
        size += chunk.byteLength;
        if (size > JWKS_MAX_BYTES) { controller.abort(); throw new Error('Key response too large.'); }
        chunks.push(Buffer.from(chunk));
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!Array.isArray(body.keys) || !body.keys.length || body.keys.length > JWKS_MAX_KEYS) throw new Error('Invalid key set.');
      const keys = new Map();
      for (const jwk of body.keys) {
        if (jwk.kty !== 'RSA' || (jwk.alg && jwk.alg !== 'RS256') || (jwk.use && jwk.use !== 'sig')
            || typeof jwk.kid !== 'string' || !jwk.kid.length || jwk.kid.length > 256 || keys.has(jwk.kid)) throw new Error('Invalid signing key.');
        const key = createPublicKey({ key: jwk, format: 'jwk' });
        if (key.asymmetricKeyType !== 'rsa' || key.asymmetricKeyDetails.modulusLength < 2048) throw new Error('Invalid RSA signing key.');
        keys.set(jwk.kid, key);
      }
      cached = keys;
      expiresAt = now() + JWKS_TTL_MS;
    } catch {
      throw new RequestError(503, 'Authentication is temporarily unavailable.');
    } finally { clearTimeout(timer); }
  }
  function refreshIfDue() {
    if (!pending && now() - lastAttempt >= JWKS_REFRESH_GAP_MS) {
      pending = refresh().finally(() => { pending = undefined; });
    }
    return pending;
  }
  const resolveKey = async kid => {
    if (now() < expiresAt && cached.has(kid)) {
      const key = cached.get(kid);
      // Refresh while the current key is still valid, without adding a network
      // round trip to the request. A failed refresh never extends its lifetime.
      if (now() >= expiresAt - JWKS_REFRESH_AHEAD_MS) refreshIfDue()?.catch(() => {});
      return key;
    }
    refreshIfDue();
    if (pending) await pending;
    if (now() >= expiresAt) throw new RequestError(503, 'Authentication is temporarily unavailable.');
    const key = cached.get(kid);
    if (!key) throw new RequestError(401, 'Authentication required.');
    return key;
  };
  resolveKey.warmup = async () => {
    if (now() >= expiresAt - JWKS_REFRESH_AHEAD_MS) await refreshIfDue();
  };
  return resolveKey;
}

function decodePart(part) {
  if (!part || !/^[A-Za-z0-9_-]+$/.test(part)) throw new Error('Invalid token encoding.');
  const bytes = Buffer.from(part, 'base64url');
  if (bytes.toString('base64url') !== part) throw new Error('Invalid token encoding.');
  return bytes;
}

export function createAccessVerifier(config, { resolveKey, now = Date.now } = {}) {
  config = validateConfig(config);
  resolveKey ??= createJwksResolver(config);
  const emails = new Set(config.allowedEmails);
  return async token => {
    try {
      if (typeof token !== 'string' || token.length > MAX_TOKEN) throw new Error('Invalid token.');
      const parts = token.split('.');
      if (parts.length !== 3) throw new Error('Invalid token.');
      const header = JSON.parse(decodePart(parts[0]).toString('utf8'));
      const claims = JSON.parse(decodePart(parts[1]).toString('utf8'));
      const signature = decodePart(parts[2]);
      if (!header || header.alg !== 'RS256' || typeof header.kid !== 'string' || !header.kid.length || header.kid.length > 256
          || header.crit !== undefined || header.b64 !== undefined) throw new Error('Invalid token header.');
      const seconds = Math.floor(now() / 1000);
      if (!claims || claims.iss !== `https://${config.teamDomain}`
          || !(typeof claims.aud === 'string' ? claims.aud === config.audience
            : Array.isArray(claims.aud) && claims.aud.every(aud => typeof aud === 'string') && claims.aud.includes(config.audience))
          || !Number.isSafeInteger(claims.exp) || claims.exp <= seconds
          || (claims.nbf !== undefined && (!Number.isSafeInteger(claims.nbf) || claims.nbf > seconds))
          || typeof claims.email !== 'string' || !emails.has(claims.email)) throw new Error('Invalid token claims.');
      const key = await resolveKey(header.kid);
      if (!verify('RSA-SHA256', Buffer.from(`${parts[0]}.${parts[1]}`), key, signature)) throw new Error('Invalid token signature.');
      return Object.freeze({ email: claims.email });
    } catch (error) {
      if (error instanceof RequestError && error.status === 503) throw error;
      throw new RequestError(401, 'Authentication required.');
    }
  };
}

function singleHeader(request, name, required = false) {
  let count = 0;
  for (let index = 0; index < request.rawHeaders.length; index += 2) {
    if (request.rawHeaders[index].toLowerCase() === name) count++;
  }
  if (count > 1 || (required && count !== 1)) throw new RequestError(400, 'Invalid request headers.');
  return request.headers[name];
}

function guardRequest(request, config) {
  const host = singleHeader(request, 'host', true);
  const origin = singleHeader(request, 'origin');
  singleHeader(request, 'content-length');
  const jwt = singleHeader(request, 'cf-access-jwt-assertion');
  // Read routes do not consume bodies in the Python origin. Never let unread
  // bytes survive into another request on a persistent connection.
  if (SAFE_METHODS.has(request.method) && (request.headers['transfer-encoding'] !== undefined
      || (request.headers['content-length'] !== undefined && request.headers['content-length'] !== '0'))) {
    throw new RequestError(400, 'This request method does not accept a body.');
  }
  if (host !== config.hostname) throw new RequestError(403, 'Invalid request host.');
  if (!request.url.startsWith('/') || request.url.startsWith('//') || /[\\\x00-\x20\x7f]/.test(request.url)) throw new RequestError(400, 'Invalid request target.');
  let pathname;
  try { pathname = decodeURIComponent(new URL(request.url, `https://${config.hostname}`).pathname); }
  catch { throw new RequestError(400, 'Invalid request target.'); }
  // Python urlparse strips final-segment ;params before the local route lookup.
  // Reject them entirely so the proxy and upstream cannot disagree about a route.
  if (pathname.includes(';')) throw new RequestError(400, 'Path parameters are not allowed.');
  if (origin !== undefined && origin !== `https://${config.hostname}`) throw new RequestError(403, 'Invalid request origin.');
  const site = singleHeader(request, 'sec-fetch-site');
  // Access login redirects and links from other sites can legitimately open the app.
  const navigation = ['GET', 'HEAD'].includes(request.method) && ['/', '/index.html', '/auth/login'].includes(pathname)
    && singleHeader(request, 'sec-fetch-mode') === 'navigate' && singleHeader(request, 'sec-fetch-dest') === 'document';
  if (site === 'cross-site' && !navigation) throw new RequestError(403, 'Cross-site requests are not allowed.');
  if (!SAFE_METHODS.has(request.method) && (origin !== `https://${config.hostname}` || (site && site !== 'same-origin' && site !== 'none'))) {
    throw new RequestError(403, 'A same-origin browser request is required.');
  }
  // Restart is allowed remotely (fly, 2026-10-15); the workbench still refuses it while work is running.
  if (/^\/api\/(?:service\/shutdown|remote-account)(\/|$)/.test(pathname)
      || (!SAFE_METHODS.has(request.method) && /^\/api\/ai\/claude\/(login|callback|key|install)(\/|$)/.test(pathname))) {
    throw new RequestError(403, 'This action is available only on the local workbench.');
  }
  return jwt;
}

function filterHeaders(headers) {
  const blocked = new Set(HOP_HEADERS);
  for (const token of String(headers.connection || '').split(',')) blocked.add(token.trim().toLowerCase());
  const result = Object.create(null);
  for (const [name, value] of Object.entries(headers)) {
    if (!blocked.has(name) && !/^(cf-|x-forwarded-|x-original-|proxy-)/.test(name)
        && !['forwarded', 'x-real-ip', 'true-client-ip', 'x-rewrite-url'].includes(name)) result[name] = value;
  }
  return result;
}

function fail(response, status, message, headers = {}, extra = {}) {
  if (response.destroyed) return;
  if (response.headersSent) { response.destroy(); return; }
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store',
    'x-content-type-options': 'nosniff', connection: 'close', ...headers,
  });
  response.end(JSON.stringify({ error: message, ...extra }));
}

const AUTH_HEADERS = Object.freeze({
  'cache-control': 'no-store', 'x-content-type-options': 'nosniff',
  'referrer-policy': 'same-origin', 'x-frame-options': 'DENY',
  'content-security-policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
});
const AUTH_ASSETS = new Map([
  ['/auth/login', ['login.html', 'text/html; charset=utf-8']],
  ['/auth/login.css', ['login.css', 'text/css; charset=utf-8']],
  ['/auth/login.js', ['login.js', 'text/javascript; charset=utf-8']],
]);
const assetCache = new Map();
async function authAsset(name) {
  if (!assetCache.has(name)) assetCache.set(name, readFile(new URL(`./auth-assets/${name}`, import.meta.url)).catch(error => {
    assetCache.delete(name); throw error;
  }));
  return assetCache.get(name);
}
function jsonResponse(response, value, headers = {}) {
  response.writeHead(200, { ...AUTH_HEADERS, 'content-type': 'application/json; charset=utf-8', ...headers });
  response.end(JSON.stringify(value));
}
async function loginBody(request) {
  if (!/^application\/json(?:\s*;|$)/i.test(singleHeader(request, 'content-type') || '')) throw new RequestError(415, 'JSON is required.');
  if (request.headers.expect) throw new RequestError(417, 'Expectation is not supported for login.');
  if (Number(request.headers['content-length'] || 0) > 4096) throw new RequestError(413, 'Login request is too large.');
  return new Promise((accept, reject) => {
    const chunks = [];
    let size = 0;
    const done = (error, value) => {
      clearTimeout(timer);
      request.off('data', data); request.off('end', end); request.off('aborted', aborted); request.off('error', aborted);
      if (error) { request.pause(); reject(error); } else accept(value);
    };
    const data = chunk => {
      size += chunk.length;
      if (size > 4096) done(new RequestError(413, 'Login request is too large.'));
      else chunks.push(chunk);
    };
    const end = () => {
      try {
        const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error();
        done(null, value);
      } catch { done(new RequestError(400, 'Invalid login request.')); }
    };
    const aborted = () => done(new RequestError(400, 'Incomplete login request.'));
    const timer = setTimeout(() => done(new RequestError(408, 'Login request timed out.')), 10000);
    request.on('data', data); request.once('end', end); request.once('aborted', aborted); request.once('error', aborted);
  });
}

async function sendGuest(request, response, guestView, hostname) {
  const result = await guestView.fetch(`https://${hostname}${request.url}`, request.method, {
    'accept-encoding': request.headers['accept-encoding'],
    'if-none-match': request.headers['if-none-match'],
  });
  response.writeHead(result.status, Object.fromEntries(result.headers));
  response.end(request.method === 'HEAD' ? undefined : Buffer.from(await result.arrayBuffer()));
}

function staticCachePolicy(request, incoming) {
  if (!['GET', 'HEAD'].includes(request.method) || ![200, 304].includes(incoming.statusCode)
      || incoming.headers['set-cookie']) return 'private, no-store';
  const url = new URL(request.url, 'http://localhost');
  // Cache program assets only. Documents, business data, downloads and errors
  // can change with the signed-in user and must never inherit this policy.
  const match = /^\/[A-Za-z0-9_-]+\.(js|css|svg)$/.exec(url.pathname);
  if (!match) return 'private, no-store';
  const type = String(incoming.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
  const types = { js: ['text/javascript', 'application/javascript'], css: ['text/css'], svg: ['image/svg+xml'] };
  if (incoming.statusCode !== 304 && !types[match[1]].includes(type)) return 'private, no-store';
  const policy = incoming.headers['cache-control'];
  if (policy === 'private, no-cache') return policy;
  const versions = url.searchParams.getAll('v');
  if (policy === 'private, max-age=31536000, immutable' && versions.length === 1 && /^[a-f0-9]{12}$/.test(versions[0])) return policy;
  return 'private, no-store';
}

/** Test injection is explicit here. The CLI accepts no upstream or authentication overrides. */
export function createRemoteProxy(config, { resolveKey, upstream = UPSTREAM, now = Date.now, accountFile, passwordAuth, guestSite } = {}) {
  config = validateConfig(config);
  const target = new URL(upstream);
  if (target.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(target.hostname) || target.pathname !== '/' || target.search || target.username || target.password) throw new Error('Upstream must be a loopback HTTP origin.');
  const passwordMode = config.authMode === 'password';
  if (passwordMode) passwordAuth ??= createPasswordAuth({ file: accountFile, hostname: config.hostname, now });
  else resolveKey ??= createJwksResolver(config, { now });
  const authenticate = passwordMode ? request => passwordAuth.authenticate(request) : createAccessVerifier(config, { resolveKey, now });
  const guestView = config.guestEnabled ? createGuestView({ site: guestSite }) : null;
  // Keep only a small idle pool: the Python upstream uses one thread per
  // connection. Active SSE and media requests must not queue regular API calls.
  const upstreamAgent = new http.Agent({ keepAlive: true, maxFreeSockets: 8, timeout: 5000, scheduling: 'lifo' });
  const handle = async (request, response) => {
    request.on('error', () => response.destroy());
    try {
      const jwt = guardRequest(request, config);
      const pathname = new URL(request.url, `https://${config.hostname}`).pathname;
      if (guestView && pathname.startsWith('/guest-media/')) {
        if (!['GET', 'HEAD'].includes(request.method)) { fail(response, 405, 'Guest media is read-only.', { allow: 'GET, HEAD' }); return; }
        const resource = await guestView.media(`https://${config.hostname}${request.url}`);
        if (!resource) { fail(response, 404, 'Media unavailable.'); return; }
        streamGuestMedia(request, response, target, resource); return;
      }
      if (guestView && (pathname.startsWith('/guest/api/') || pathname.startsWith('/guest-assets/') || pathname === '/robots.txt')) {
        await sendGuest(request, response, guestView, config.hostname); return;
      }
      if (!passwordMode && pathname.startsWith('/auth/')) {
        fail(response, 404, 'Not found.'); return;
      }
      if (passwordMode) {
        if (pathname === '/auth/mode' && request.method === 'GET') {
          const status = await passwordAuth.status();
          jsonResponse(response, { guest_enabled: !!guestView, configured: status.configured }); return;
        }
        const asset = AUTH_ASSETS.get(pathname);
        if (asset && ['GET', 'HEAD'].includes(request.method)) {
          const bytes = await authAsset(asset[0]);
          response.writeHead(200, { ...AUTH_HEADERS, 'content-type': asset[1], 'content-length': bytes.length });
          response.end(request.method === 'HEAD' ? undefined : bytes); return;
        }
        if (pathname === '/auth/login' && request.method === 'POST') {
          const { username, password } = await loginBody(request);
          const clientIP = singleHeader(request, 'cf-connecting-ip');
          const client = typeof clientIP === 'string' && isIP(clientIP) ? clientIP : request.socket.remoteAddress;
          const result = await passwordAuth.login(username, password, client);
          jsonResponse(response, { ok: true, username: result.username }, { 'set-cookie': result.cookie }); return;
        }
        if (pathname === '/auth/logout' && request.method === 'POST') {
          const result = await passwordAuth.logout(request);
          jsonResponse(response, { ok: true }, { 'set-cookie': result.cookie }); return;
        }
        const guestDocument = guestView && ['GET', 'HEAD'].includes(request.method) && ['/', '/index.html'].includes(pathname);
        if (guestDocument && new URL(request.url, `https://${config.hostname}`).searchParams.get('guest') === '1') {
          await sendGuest(request, response, guestView, config.hostname); return;
        }
        let user;
        try { user = await authenticate(request); }
        catch (error) {
          if (guestDocument && error.status === 401) { await sendGuest(request, response, guestView, config.hostname); return; }
          throw error;
        }
        if (pathname === '/auth/session' && request.method === 'GET') {
          jsonResponse(response, { authenticated: true, username: user.username }); return;
        }
        if (pathname.startsWith('/auth/')) { fail(response, 404, 'Not found.'); return; }
      } else await authenticate(jwt);
      if (request.destroyed || response.destroyed) return;
      const headers = filterHeaders(request.headers);
      if (headers['sec-fetch-site'] === 'cross-site') delete headers['sec-fetch-site'];
      headers.host = target.host;
      if (request.headers.origin) headers.origin = target.origin;
      if (headers.cookie) {
        headers.cookie = headers.cookie.split(';').filter(cookie => !/^\s*(?:CF_(?:Authorization|AppSession)|__Host-WorkbenchSession)=/i.test(cookie)).join(';');
        if (!headers.cookie.trim()) delete headers.cookie;
      }
      if (['/', '/index.html'].includes(pathname)) {
        delete headers['if-none-match']; delete headers['if-modified-since'];
      }
      if (request.headers.expect?.toLowerCase() === '100-continue') response.writeContinue();
      const forwarded = http.request({
        hostname: target.hostname, port: target.port, method: request.method,
        path: request.url, headers, agent: ['GET', 'HEAD'].includes(request.method) ? upstreamAgent : false,
      });
      forwarded.setTimeout(IDLE_TIMEOUT_MS, () => forwarded.destroy(new Error('Upstream idle timeout.')));
      const close = () => forwarded.destroy();
      request.once('aborted', close);
      response.once('close', () => { if (!response.writableFinished) close(); });
      forwarded.once('response', incoming => {
        const outgoingHeaders = filterHeaders(incoming.headers);
        outgoingHeaders['cache-control'] = staticCachePolicy(request, incoming);
        outgoingHeaders.vary = [...new Set(String(outgoingHeaders.vary || '').split(',').map(value => value.trim()).filter(Boolean).concat('Cookie'))].join(', ');
        if (typeof outgoingHeaders.location === 'string' && outgoingHeaders.location.startsWith(`${target.origin}/`)) {
          outgoingHeaders.location = `https://${config.hostname}${outgoingHeaders.location.slice(target.origin.length)}`;
        }
        response.writeHead(incoming.statusCode, outgoingHeaders);
        response.flushHeaders();
        incoming.on('error', () => response.destroy());
        incoming.pipe(response);
      });
      forwarded.once('error', () => fail(response, 502, 'The local workbench is unavailable.'));
      request.once('error', close);
      request.pipe(forwarded);
    } catch (error) {
      const status = [400, 401, 403, 408, 413, 415, 417, 429, 503].includes(error.status) ? error.status : 500;
      if (passwordMode && status === 401) {
        const loginURL = '/auth/login';
        if (['GET', 'HEAD'].includes(request.method) && !request.url.startsWith('/auth/')
            && (request.headers['sec-fetch-dest'] === 'document' || /text\/html/.test(request.headers.accept || ''))) {
          response.writeHead(303, { ...AUTH_HEADERS, location: `${loginURL}?next=${encodeURIComponent(request.url)}` });
          response.end(); return;
        }
        fail(response, 401, '账号或密码错误，或登录已失效。', { 'x-workbench-auth': 'required' }, { login_url: loginURL });
      } else fail(response, status, status === 500 ? 'Request could not be completed.' : error.message,
        status === 429 ? { 'retry-after': String(error.retryAfter || 60) } : {});
    }
  };
  const server = http.createServer({ maxHeaderSize: 32768, requestTimeout: IDLE_TIMEOUT_MS, headersTimeout: 15000 }, handle);
  server.maxHeadersCount = 100;
  server.keepAliveTimeout = 5000;
  server.once('listening', () => { resolveKey?.warmup?.().catch(() => {}); });
  server.once('close', () => upstreamAgent.destroy());
  server.on('checkContinue', handle);
  server.on('upgrade', (_request, socket) => socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'));
  server.on('connect', (_request, socket) => socket.end('HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n'));
  server.on('clientError', (_error, socket) => { if (socket.writable) socket.end('HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n'); });
  return server;
}

async function main() {
  if (process.argv.length !== 4 || process.argv[2] !== '--config' || !isAbsolute(process.argv[3])) throw new Error('Usage: node remote_proxy.mjs --config <absolute JSON path>');
  const raw = await readFile(process.argv[3], 'utf8');
  let parsed;
  try { parsed = JSON.parse(raw.replace(/^\uFEFF/, '')); }
  catch { throw new Error('Runtime config is not valid JSON.'); }
  const config = validateConfig(parsed);
  const server = createRemoteProxy(config, { accountFile: join(dirname(process.argv[3]), 'account.json'),
    guestSite: join(dirname(process.argv[3]), 'guest', 'site') });
  await new Promise((accept, reject) => {
    server.once('error', reject);
    server.listen(LISTEN_PORT, LISTEN_HOST, accept);
  });
  console.log(`Protected workbench proxy listening on ${LISTEN_HOST}:${LISTEN_PORT} for ${config.hostname}`);
  for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => {
    server.close(() => process.exit(0));
    setTimeout(() => { server.closeAllConnections(); process.exit(0); }, 5000).unref();
  });
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch(error => { console.error(`Protected proxy could not start: ${error.code || error.message}`); process.exitCode = 1; });
}
