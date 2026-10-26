import http from 'node:http';

const VIDEO_TYPES = new Set(['video/mp4', 'video/webm', 'video/quicktime', 'video/x-m4v']);
const IMAGE_TYPES = new Set(['image/jpeg', 'image/png', 'image/webp']);
const validEtag = value => typeof value === 'string' && /^(?:W\/)?"[\x21\x23-\x7e]{1,200}"$/.test(value);
const HEADERS = Object.freeze({
  'cache-control': 'private, no-store', vary: 'Cookie', 'x-workbench-view': 'guest',
  'content-disposition': 'inline', 'x-content-type-options': 'nosniff',
  'cross-origin-resource-policy': 'same-origin', 'referrer-policy': 'no-referrer',
  'x-robots-tag': 'noindex, nofollow',
});

function reject(response, status, extra = {}) {
  if (response.destroyed) return;
  if (response.headersSent) { response.destroy(); return; }
  response.writeHead(status, { ...HEADERS, 'content-type': 'text/plain; charset=utf-8', ...extra });
  response.end('Media unavailable.');
}

/** Stream only the exact case/event route already approved by the snapshot. */
export function streamGuestMedia(request, response, target, resource) {
  if (request.destroyed || response.destroyed) return;
  const headers = { host: target.host, 'accept-encoding': 'identity' };
  const range = request.headers.range;
  if (range !== undefined && (typeof range !== 'string' || !/^bytes=(?:\d{1,16}-\d{0,16}|-\d{1,16})$/.test(range))) {
    reject(response, 416); return;
  }
  // The local service does not implement If-Range. Serve a complete entity
  // when supplied, which is valid for a server ignoring the range request.
  if (range && !request.headers['if-range']) headers.range = range;
  // Permission is checked before reaching this function on every request. A
  // private image may be reused only after the origin validates its entity tag.
  const conditional = request.headers['if-none-match'];
  if (resource.kind !== 'video' && typeof conditional === 'string' && conditional.length <= 1024
      && !/[\x00-\x1f\x7f]/.test(conditional)) headers['if-none-match'] = conditional;
  const forwarded = http.request({ hostname: target.hostname, port: target.port,
    method: request.method, path: resource.path, headers, agent: false });
  forwarded.setTimeout(60000, () => forwarded.destroy(new Error('Media timeout.')));
  const close = () => forwarded.destroy();
  request.once('aborted', close);
  response.once('close', () => { if (!response.writableFinished) close(); });
  forwarded.once('error', () => reject(response, 502));
  forwarded.once('response', incoming => {
    const status = incoming.statusCode;
    if (status === 304) {
      const etag = incoming.headers.etag;
      if (!headers['if-none-match'] || !validEtag(etag)) {
        incoming.destroy(); reject(response, 502); return;
      }
      incoming.resume();
      response.writeHead(304, { ...HEADERS, 'cache-control': 'private, no-cache', etag });
      response.end(); return;
    }
    if (status === 416) {
      const value = incoming.headers['content-range'];
      incoming.resume();
      reject(response, 416, typeof value === 'string' && /^bytes \*\/\d+$/.test(value) ? { 'content-range': value } : {});
      return;
    }
    const type = String(incoming.headers['content-type'] || '').split(';')[0].trim().toLowerCase();
    const allowed = resource.kind === 'video' ? VIDEO_TYPES : IMAGE_TYPES;
    if (![200, 206].includes(status) || !allowed.has(type)) {
      incoming.destroy(); reject(response, status === 404 ? 404 : 502); return;
    }
    const result = { ...HEADERS, 'content-type': type, 'accept-ranges': 'bytes' };
    if (resource.kind !== 'video' && validEtag(incoming.headers.etag)) {
      result.etag = incoming.headers.etag;
      result['cache-control'] = 'private, no-cache';
    }
    const length = incoming.headers['content-length'];
    if (typeof length === 'string' && /^\d+$/.test(length)) result['content-length'] = length;
    if (status === 206) {
      const value = incoming.headers['content-range'];
      if (typeof value !== 'string' || !/^bytes \d+-\d+\/\d+$/.test(value)) {
        incoming.destroy(); reject(response, 502); return;
      }
      result['content-range'] = value;
    }
    response.writeHead(status, result);
    response.flushHeaders();
    incoming.on('error', () => response.destroy());
    if (request.method === 'HEAD') { incoming.resume(); response.end(); }
    else incoming.pipe(response);
  });
  forwarded.end();
}
