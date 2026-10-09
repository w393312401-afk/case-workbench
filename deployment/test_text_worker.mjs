import assert from 'node:assert/strict';
import test from 'node:test';
import worker from './text-only/_worker.js';

const caseId = 'PVR-0123456789ab', archivedId = 'PVR-1123456789ab';
const scriptId = 'SCR-0123456789ab', taskId = 'AIT-0123456789ab', videoId = '123456789';
const reviews = [
  { id: '0000000000000001', platform: 'tiktok_video', product: 'SHARPAL 202H', role: 'viewer', text: 'The clamp is loose. Angle needs practice.', relevant: 1, likes: 5, posted_on: '2026-10-02', first_seen: '2026-10-06', source_ref: videoId, rating: null, is_reply: 0, replies: { total: 1, brand: 1 }, tags: { nature: ['question', 'negative'], topic: ['clamp', 'angle'], competitor: [] } },
  { id: '0000000000000002', platform: 'amazon', product: '202H', role: 'buyer', text: 'Great clamp and angle', relevant: 1, likes: null, posted_on: '2026-10-01', first_seen: null, rating: 2, is_reply: 0, tags: { nature: ['negative'], topic: ['clamp'], competitor: ['work_sharp'] } },
  { id: '0000000000000003', platform: 'tiktok_video', product: '202H', role: 'brand_account', text: 'Please tighten the clamp', relevant: 1, likes: 15, posted_on: '2026-10-03', first_seen: '2026-10-06', source_ref: videoId, parent_id: '0000000000000001', is_reply: 1, tags: {} },
  { id: '0000000000000004', platform: 'reddit', product: 'other', role: 'viewer', text: 'Unrelated', relevant: 0, likes: 3, posted_on: null, is_reply: 0, tags: {} },
  { id: '0000000000000005', platform: 'facebook', product: '202H', role: 'viewer', text: '', relevant: 1, likes: 1, posted_on: null, is_reply: 0, tags: {} },
];
const cases = [
  { case_id: caseId, product: 'SHARPAL 202H', archived: false, current: { title: 'Clamp', test_direction: { direction_id: '202H-S1' }, metrics: { video_id: videoId } } },
  { case_id: archivedId, product: '202H', archived: true, current: { title: 'Old clamp', test_direction: { direction_id: '202H-S1' }, metrics: { video_id: '222222222' } } },
];
const observations = cases.map(item => ({ ...item, test_direction: { variant_type: 'direction' }, metrics: item.current.metrics, evidence: {}, sales_data: { status: 'not_found' } }));
const group = { product: 'SHARPAL 202H', direction_id: '202H-S1', lead_code: 'S1', observations, metric_availability: { views: 0 }, cohorts: [{ case_ids: [caseId, archivedId], case_count: 2 }], duplicate_video_ids: [] };
const fixture = {
  state: { app: 'video-case-workbench', token: '', digest: 'snapshot-v1', signature: 'signature', cases, navigation: {} },
  cases: Object.fromEntries(cases.map(item => [item.case_id, item])),
  reviews: { items: reviews, summary: { total: 5, labels: { nature: { question: '提问', negative: '负面' }, topic: { clamp: '夹具', angle: '角度' }, competitor: { work_sharp: 'Work Sharp' } } }, picks: [reviews[0]], tag_edits: [{ id: reviews[0].id, kind: 'topic', tag: 'clamp' }], videos: { [videoId]: { title: 'Demo' } } },
  performance: { summary: { days: [] }, analytics: { daily: [] }, videos: [{ video_id: videoId, product: '202H' }], details: { [videoId]: { video_id: videoId, rows: [] } } },
  direction_catalog: { revision: 'rev', directions: [{ product: '202H', direction_id: '202H-S1', selling_point_id: 'point' }, { product: 'other', direction_id: 'other-A1' }], selling_points: [{ id: 'point', product: '202H' }], video_links: [] },
  direction_details: { '202H|202H-S1': { direction: { product: '202H', direction_id: '202H-S1' }, cases, scripts: [], exact: true } },
  directions: { groups: [{ ...group, observations: [observations[0]] }], archive: 'active' },
  directions_all: { groups: [group], archive: 'all' },
  creation: { revision: 'creation-rev', scripts: [{ id: scriptId, product: '202H', direction_id: '202H-S1' }], plans: [{ product: 'other' }], materials: [], theme_links: [] },
  materials: { revision: 'materials-rev', items: [{ id: 'material', product: '202H' }], theme_links: [] },
  scripts: { [scriptId]: { revision: 'creation-rev', script: { id: scriptId, text: 'Text' } } },
  ai: { status: { available: true }, options: { models: [], skills: [] }, tasks: [{ task_id: taskId, case_id: caseId, product: '202H' }], details: { [taskId]: { task_id: taskId, rounds: [] } } },
};
function environment(data = fixture) {
  const calls = [];
  return {
    calls,
    ASSETS: { async fetch(request) {
      const path = new URL(request.url).pathname;
      calls.push({ path, method: request.method, authorization: request.headers.get('Authorization') });
      if (path === '/public-data.json') return Response.json(data);
      if (path === '/' || path === '/index.html') return new Response('<!doctype html><title>Text</title>', { headers: { 'Content-Type': 'text/html' } });
      if (path === '/app.js') return new Response('const text = true;', { headers: { 'Content-Type': 'application/javascript' } });
      if (path === '/style.css') return new Response('body{}', { headers: { 'Content-Type': 'text/css' } });
      if (path === '/robots.txt') return new Response('User-agent: *\nDisallow: /\n', { headers: { 'Content-Type': 'text/plain' } });
      return new Response('Missing', { status: 404, headers: { 'Content-Type': 'text/plain' } });
    } },
  };
}
const request = (path, method = 'GET') => new Request(`https://case-workbench-text.pages.dev${path}`, { method });
const response = (path, env = environment(), method = 'GET') => worker.fetch(request(path, method), env);
function splitEnvironment() {
  const calls = [];
  const manifest = { meta: { mode: 'read-only-text' }, state: fixture.state, cases: {}, split: true };
  const files = {
    '/public-data.json': manifest,
    '/data/reviews.json': { reviews: fixture.reviews },
    '/data/performance.json': { performance: { summary: fixture.performance.summary, analytics: fixture.performance.analytics, videos: fixture.performance.videos } },
    [`/data/video-shard-${Number(videoId.slice(-5)) % 32}.json`]: { performance: { details: fixture.performance.details } },
    [`/data/case-${caseId}.json`]: { cases: { [caseId]: fixture.cases[caseId] } },
    [`/data/case-${archivedId}.json`]: { cases: { [archivedId]: fixture.cases[archivedId] } },
    '/data/ai.json': { ai: fixture.ai },
    '/data/creation.json': { creation: fixture.creation, materials: fixture.materials, scripts: fixture.scripts },
    '/data/direction-catalog.json': { direction_catalog: fixture.direction_catalog, direction_details: fixture.direction_details },
    '/data/directions.json': { directions: fixture.directions, directions_all: fixture.directions_all },
  };
  return { calls, files, ASSETS: { async fetch(request) {
    const path = new URL(request.url).pathname;
    calls.push({ path, method: request.method, authorization: request.headers.get('Authorization') });
    return files[path] ? Response.json(files[path]) : new Response('Missing', { status: 404 });
  } } };
}
async function body(path, env = environment()) {
  const result = await response(path, env);
  assert.equal(result.status, 200, `${path}: ${await result.clone().text()}`);
  return result.json();
}

test('only GET and HEAD can access the site', async () => {
  const env = environment();
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']) {
    for (const path of ['/', '/api/state', '/api/cases', '/api/ai/tasks', '/api/upload']) {
      const result = await response(path, env, method);
      assert.equal(result.status, 405);
      assert.equal(result.headers.get('Allow'), 'GET, HEAD');
    }
  }
  assert.equal(env.calls.length, 0);
});
test('private bundle, Worker source, local actions, all media and unknown APIs stay unavailable', async () => {
  const env = environment();
  for (const path of ['/public-data.json', '/public-data.json?download=1', '/public-data.json/', '/%70ublic-data.json', '/%2570ublic-data.json', '/_worker.js', '/_routes.json', '/media', '/media/x/video', '/api/open', '/api/upload', '/api/service/status', '/api/service/shutdown', '/api/service-other', '/api/sync', '/api/cases/invalid', `/api/cases/${caseId}/delete-preview`, `/api/ai/tasks/${taskId}/stream`, '/photo.jpg', '/image.svg', '/audio.mp3', '/movie.mp4']) {
    assert.equal((await response(path, env)).status, 404, path);
  }
  assert.equal(env.calls.filter(item => item.path !== '/public-data.json').length, 0);
});
test('all responses block media, remote connections, framing and cross-site referrers', async () => {
  for (const path of ['/', '/app.js', '/style.css', '/robots.txt', '/api/state', '/missing', '/media/test']) {
    const result = await response(path);
    const csp = result.headers.get('Content-Security-Policy');
    for (const directive of ["img-src 'none'", "media-src 'none'", "connect-src 'self'", "style-src 'self' 'unsafe-inline'", "frame-ancestors 'none'"]) assert.ok(csp.includes(directive), path);
    assert.equal(result.headers.get('Referrer-Policy'), 'no-referrer');
    assert.equal(result.headers.get('X-Content-Type-Options'), 'nosniff');
    assert.equal(result.headers.get('Access-Control-Allow-Origin'), null);
    assert.equal(result.headers.get('X-Robots-Tag'), 'noindex, nofollow');
  }
});
test('HEAD keeps status, content type and policy while omitting every body', async () => {
  for (const path of ['/', '/app.js', '/api/state', '/missing']) {
    const result = await response(path, environment(), 'HEAD');
    assert.equal(await result.text(), '');
    assert.ok(result.headers.get('Content-Type'));
    assert.ok(result.headers.get('Content-Security-Policy'));
    assert.equal(result.status, path === '/missing' ? 404 : 200);
  }
});
test('the bundle loads internally once and drops incoming credentials', async () => {
  const env = environment();
  await worker.fetch(new Request('https://case-workbench-text.pages.dev/api/state', { headers: { Authorization: 'Bearer local-only' } }), env);
  await response('/api/reviews/summary', env);
  assert.equal(env.calls.length, 1);
  assert.deepEqual(env.calls[0], { path: '/public-data.json', method: 'GET', authorization: null });
});
test('state digest polling and case detail preserve frontend shapes', async () => {
  assert.deepEqual(await body('/api/state'), fixture.state);
  assert.equal((await body('/api/state?since=snapshot-v1')).unchanged, true);
  assert.equal((await body(`/api/cases/${caseId}`)).case_id, caseId);
});
test('reviews default filters exclude brand accounts, irrelevant and empty comments', async () => {
  const result = await body('/api/reviews');
  assert.equal(result.total, 2);
  assert.deepEqual(result.reviews.map(item => item.id), [reviews[0].id, reviews[1].id]);
  assert.equal(result.facets.platform.tiktok_video, 1);
  assert.equal(result.videos[videoId].title, 'Demo');
  assert.equal((await body('/api/reviews?include_brand=1&include_irrelevant=1&include_empty=1')).total, 5);
});
test('review multi-tag filters use AND while platform filters use OR and product aliases match', async () => {
  assert.equal((await body('/api/reviews?nature=question&nature=negative&topic=clamp&topic=angle&product=202h')).total, 1);
  assert.equal((await body('/api/reviews?platform=amazon&platform=tiktok_video')).total, 2);
  const amazon = await body('/api/reviews?platform=amazon');
  assert.equal(amazon.total, 1);
  assert.equal(amazon.facets.platform.tiktok_video, 1);
  assert.equal((await body('/api/reviews?competitor=none')).total, 1);
});
test('review pagination, nullable sorting, rating, replies and first-seen date filters work', async () => {
  const page = await body('/api/reviews?limit=1&offset=1&sort=oldest');
  assert.equal(page.total, 2);
  assert.equal(page.reviews[0].id, reviews[0].id);
  assert.equal((await body('/api/reviews?rating_max=2')).reviews[0].id, reviews[1].id);
  assert.equal((await body('/api/reviews?replied=yes')).total, 1);
  assert.equal((await body('/api/reviews?since=2026-10-06')).total, 1);
  assert.equal((await body('/api/reviews?limit=999')).limit, 200);
  assert.equal((await body('/api/reviews?limit=-1&offset=-1')).offset, 0);
});
test('review Chinese queries keep original English expansions and terms combine with AND', async () => {
  const result = await body('/api/reviews?q=' + encodeURIComponent('夹具 晃动'));
  assert.equal(result.total, 1);
  assert.deepEqual(result.query.expanded.map(item => item.term), ['夹具', '晃动']);
  assert.ok(result.query.expanded[0].english.includes('clamp'));
  assert.equal((await body('/api/reviews?q=loose%20angle')).total, 1);
  assert.deepEqual((await body('/api/reviews?q=' + encodeURIComponent('无映射'))).query.unmapped, ['无映射']);
});
test('review detail includes its conversation and picks and corrections retain wrappers', async () => {
  const result = await body(`/api/reviews/${reviews[0].id}`);
  assert.equal(result.review.thread[0].id, reviews[2].id);
  assert.deepEqual(await body('/api/reviews/picks'), { picks: fixture.reviews.picks });
  assert.deepEqual(await body('/api/reviews/tag-edits'), { edits: fixture.reviews.tag_edits });
});
test('malformed, repeated and unknown review filters return 400', async () => {
  for (const query of ['limit=no', 'offset=1&offset=2', 'q=a&q=b', 'platform=bad', 'topic=bad', 'rating_min=no', 'sort=bad', 'video=invalid', 'since=yesterday', 'replied=maybe']) {
    assert.equal((await response(`/api/reviews?${query}`)).status, 400, query);
  }
});
test('creation, material and direction catalogs keep product filtering and exact direction details', async () => {
  assert.equal((await body('/api/creation?product=SHARPAL%20202H')).scripts.length, 1);
  assert.equal((await body('/api/creation?product=202H')).plans.length, 0);
  assert.equal((await body('/api/materials?product=other')).items.length, 0);
  assert.equal((await body('/api/direction-catalog?product=202h')).directions.length, 1);
  assert.equal((await body('/api/direction-catalog/202H-S1?product=202H')).exact, true);
  assert.deepEqual(await body(`/api/scripts/${scriptId}`), fixture.scripts[scriptId]);
});
test('direction archive filtering rebuilds observation counts and cohorts', async () => {
  assert.deepEqual(await body('/api/directions'), fixture.directions);
  const result = await body('/api/directions?product=202H&archive=archived');
  assert.equal(result.case_count, 1);
  assert.equal(result.groups[0].case_count, 1);
  assert.deepEqual(result.groups[0].cohorts[0].case_ids, [archivedId]);
  assert.equal(result.groups[0].missing_counts.metrics, 1);
  assert.equal((await response('/api/directions?archive=invalid')).status, 400);
});
test('performance and AI histories keep frontend wrappers with AI disabled publicly', async () => {
  assert.deepEqual(await body('/api/performance/summary'), fixture.performance.summary);
  assert.deepEqual(await body('/api/performance/analytics'), fixture.performance.analytics);
  assert.equal((await body('/api/performance/videos?product=other')).videos.length, 0);
  assert.equal((await body(`/api/performance/videos/${videoId}`)).video.video_id, videoId);
  assert.equal((await body('/api/ai/tasks?case_id=' + caseId)).tasks.length, 1);
  assert.equal((await body(`/api/ai/tasks/${taskId}`)).task.task_id, taskId);
  assert.equal((await body('/api/ai/status')).available, false);
  assert.equal((await body('/api/ai/status')).read_only, true);
});
test('snapshot read failures expose a generic error and are retryable', async () => {
  let calls = 0;
  const env = { ASSETS: { async fetch() { calls++; return calls === 1 ? new Response('private local failure', { status: 500 }) : Response.json(fixture); } } };
  const first = await response('/api/state', env);
  assert.equal(first.status, 503);
  assert.ok(!(await first.text()).includes('private local failure'));
  assert.equal((await response('/api/state', env)).status, 200);
});

test('a split manifest loads state alone and keeps digest polling', async () => {
  const env = splitEnvironment();
  assert.deepEqual(await body('/api/state', env), fixture.state);
  assert.equal((await body('/api/state?since=snapshot-v1', env)).unchanged, true);
  assert.deepEqual(env.calls.map(item => item.path), ['/public-data.json']);
});
test('split API routes select the correct single fragment and match the complete bundle', async () => {
  const routes = [
    ['/api/reviews?topic=clamp', '/data/reviews.json'],
    ['/api/reviews/summary', '/data/reviews.json'],
    ['/api/reviews/picks', '/data/reviews.json'],
    ['/api/reviews/tag-edits', '/data/reviews.json'],
    [`/api/reviews/${reviews[0].id}`, '/data/reviews.json'],
    ['/api/performance/summary', '/data/performance.json'],
    ['/api/performance/analytics', '/data/performance.json'],
    ['/api/performance/videos?product=202H', '/data/performance.json'],
    [`/api/performance/videos/${videoId}`, `/data/video-shard-${Number(videoId.slice(-5)) % 32}.json`],
    [`/api/cases/${caseId}`, `/data/case-${caseId}.json`],
    ['/api/ai/status', '/data/ai.json'],
    ['/api/ai/options', '/data/ai.json'],
    ['/api/ai/tasks?case_id=' + caseId, '/data/ai.json'],
    [`/api/ai/tasks/${taskId}`, '/data/ai.json'],
    ['/api/creation?product=202H', '/data/creation.json'],
    ['/api/materials', '/data/creation.json'],
    [`/api/scripts/${scriptId}`, '/data/creation.json'],
    ['/api/direction-catalog?product=202H', '/data/direction-catalog.json'],
    ['/api/direction-catalog/202H-S1?product=202H', '/data/direction-catalog.json'],
    ['/api/directions?product=202H&archive=all', '/data/directions.json'],
  ];
  for (const [path, fragment] of routes) {
    const env = splitEnvironment();
    assert.deepEqual(await body(path, env), await body(path), path);
    assert.deepEqual(env.calls.map(item => item.path), ['/public-data.json', fragment], path);
  }
});
test('split fragment promises are cached per binding and path, including simultaneous requests', async () => {
  const env = splitEnvironment();
  await Promise.all(['/api/reviews', '/api/reviews/picks', '/api/reviews/summary', '/api/performance/videos', '/api/performance/summary'].map(path => body(path, env)));
  assert.deepEqual(env.calls.map(item => item.path).sort(), ['/public-data.json', '/data/reviews.json', '/data/performance.json'].sort());
  await body('/api/reviews', env);
  assert.equal(env.calls.length, 3);
  const differentBinding = splitEnvironment();
  await body('/api/reviews', differentBinding);
  assert.equal(differentBinding.calls.length, 2);
});
test('split data files stay hidden and media and mutations remain blocked before loading any data', async () => {
  const env = splitEnvironment();
  for (const path of ['/data', '/data/reviews.json', '/data/performance.json', '/data/creation.json', '/data/app.js', '/%64ata/reviews.json', '/data%2freviews.json', '/public-data.json', '/media/demo']) {
    assert.equal((await response(path, env)).status, 404, path);
  }
  assert.equal((await response('/api/state', env, 'POST')).status, 405);
  assert.equal(env.calls.length, 0);
});
test('split HEAD preserves JSON policies and an unknown case returns 404 without a fragment request', async () => {
  const env = splitEnvironment();
  const head = await response('/api/reviews', env, 'HEAD');
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('Content-Type'), 'application/json; charset=utf-8');
  assert.ok(head.headers.get('Content-Security-Policy').includes("media-src 'none'"));
  assert.equal((await response('/api/cases/PVR-999999999999', env)).status, 404);
  assert.equal(env.calls.length, 2);
});

test('public version is a lightweight no-store GET/HEAD endpoint with no data or credentials', async () => {
  const env = splitEnvironment();
  env.files['/public-data.json'] = {
    ...env.files['/public-data.json'],
    meta: { exported_at: '2026-10-08T19:00:00+08:00', content_revision: 'public-content-v1' },
  };
  const get = await response('/api/public-version', env);
  assert.deepEqual(await get.json(), { version: 'public-content-v1', exported_at: '2026-10-08T19:00:00+08:00' });
  assert.equal(get.headers.get('Cache-Control'), 'no-store');
  assert.ok(get.headers.get('Content-Security-Policy').includes("img-src 'none'"));
  const head = await response('/api/public-version', env, 'HEAD');
  assert.equal(head.status, 200);
  assert.equal(await head.text(), '');
  assert.equal(head.headers.get('Cache-Control'), 'no-store');
  assert.equal(head.headers.get('Content-Type'), 'application/json; charset=utf-8');
  assert.equal((await response('/api/public-version', env, 'POST')).status, 405);
  assert.deepEqual(env.calls.map(item => item.path), ['/public-data.json', '/public-data.json']);
});
test('public version falls back to manifest digest and then export timestamp', async () => {
  assert.deepEqual(await body('/api/public-version', splitEnvironment()), { version: fixture.state.digest, exported_at: '' });
  const env = splitEnvironment();
  env.files['/public-data.json'] = { ...env.files['/public-data.json'], state: { ...fixture.state, digest: '' }, meta: { exported_at: '2026-10-08T19:01:00+08:00' } };
  assert.deepEqual(await body('/api/public-version', env), { version: '2026-10-08T19:01:00+08:00', exported_at: '2026-10-08T19:01:00+08:00' });
});
test('a new manifest revision invalidates stable-URL fragment caches on the same binding', async () => {
  const env = splitEnvironment();
  assert.equal((await body('/api/reviews?q=fresh', env)).total, 0);
  assert.equal((await body('/api/public-version', env)).version, 'snapshot-v1');
  const manifest = structuredClone(env.files['/public-data.json']);
  manifest.state.digest = 'snapshot-v2';
  manifest.meta.exported_at = '2026-10-08T19:02:00+08:00';
  env.files['/public-data.json'] = manifest;
  const updatedReviews = structuredClone(fixture.reviews);
  updatedReviews.items[0].text = 'fresh publication';
  env.files['/data/reviews.json'] = { reviews: updatedReviews };
  assert.deepEqual(await body('/api/public-version', env), { version: 'snapshot-v2', exported_at: manifest.meta.exported_at });
  assert.equal((await body('/api/state', env)).digest, 'snapshot-v2');
  assert.equal((await body('/api/reviews?q=fresh', env)).total, 1);
  assert.equal(env.calls.filter(item => item.path === '/data/reviews.json').length, 2);
  assert.equal(env.calls.filter(item => item.path === '/public-data.json').length, 3);
});
test('unchanged revision polls refresh only the manifest and preserve cached fragments', async () => {
  const env = splitEnvironment();
  await body('/api/reviews', env);
  await body('/api/public-version', env);
  await body('/api/public-version', env);
  await body('/api/reviews', env);
  assert.equal(env.calls.filter(item => item.path === '/public-data.json').length, 3);
  assert.equal(env.calls.filter(item => item.path === '/data/reviews.json').length, 1);
});
test('public-version simultaneous refreshes coalesce and internal requests bypass caching without credentials', async () => {
  let calls = 0;
  const env = { ASSETS: { async fetch(request) {
    calls++;
    assert.equal(request.headers.get('Cache-Control'), 'no-cache');
    assert.equal(request.headers.get('Authorization'), null);
    return Response.json(fixture);
  } } };
  await Promise.all(Array.from({ length: 4 }, () => worker.fetch(new Request('https://case-workbench-text.pages.dev/api/public-version', { headers: { Authorization: 'Bearer local-only' } }), env)));
  assert.equal(calls, 1);
});
test('failed version refresh is retryable and preserves the last valid manifest', async () => {
  let calls = 0;
  const env = { ASSETS: { async fetch() {
    calls++;
    return calls === 2 ? new Response('temporary failure', { status: 500 }) : Response.json(fixture);
  } } };
  assert.equal((await body('/api/state', env)).digest, 'snapshot-v1');
  assert.equal((await response('/api/public-version', env)).status, 503);
  assert.equal((await body('/api/state', env)).digest, 'snapshot-v1');
  assert.equal((await body('/api/public-version', env)).version, 'snapshot-v1');
  assert.equal(calls, 3);
});
test('a slow old manifest cannot overwrite a newer deployment after the version probe', async () => {
  let releaseOld, calls = 0;
  const oldResponse = new Promise(resolve => { releaseOld = resolve; });
  const updated = structuredClone(fixture);
  updated.state.digest = 'snapshot-v2';
  const env = { ASSETS: { async fetch() {
    calls++;
    return calls === 1 ? oldResponse : Response.json(updated);
  } } };
  const oldState = body('/api/state', env);
  assert.equal((await body('/api/public-version', env)).version, 'snapshot-v2');
  releaseOld(Response.json(fixture));
  assert.equal((await oldState).digest, 'snapshot-v1');
  assert.equal((await body('/api/state', env)).digest, 'snapshot-v2');
  assert.equal(calls, 2);
});
