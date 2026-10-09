// Cloudflare Pages advanced-mode entry point for the published text snapshot.
// The ASSETS binding is the only data source; this worker never contacts the local service.
const POLICY = "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'none'; media-src 'none'; font-src 'self'; object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const BUNDLE_PATH = '/public-data.json';
const assetCaches = new WeakMap();
const manifestRefreshes = new WeakMap();
const manifestVersions = new WeakMap();
const manifestSequences = new WeakMap();
const TAG_KINDS = ['nature', 'topic', 'competitor'];
const PLATFORMS = ['tiktok_video', 'amazon', 'tiktok_shop', 'reddit', 'facebook'];
// Replaced by the local build with the existing library's Chinese search vocabulary.
const ZH_SEARCH = [[["夹具","夹子","夹持","夹紧","固定器"],["clamp","jaw","holder","vise"]],[["翻转","旋转","转轴","转动"],["pivot","flip","rotat","swivel"]],[["晃动","摇晃","松动","不稳","晃"],["wobbl","flex","loose","deflect","rocking","drift"]],[["底座","底盘"],["the base","base is","base of","slip"]],[["角度","刻度","角度尺"],["angle","degree","dial","gauge"]],[["小刀","短刀","折刀","水果刀","口袋刀"],["pocket","small","short","narrow","paring","folder","thin blade","thin knife"]],[["磨板","磨石","磨刀石","砂板","钻石板","陶瓷板"],["stone","plate","whetstone","diamond","ceramic","grit","abrasive"]],[["目数","粒度"],["grit"]],[["配件","补充装","替换","备件"],["replacement","accessor","refill","spare","additional"]],[["说明书","使用说明","说明","教程","指南"],["instruction","manual","direction","tutorial","guide"]],[["上手","新手","入门","学习曲线","难度"],["learning curve","beginner","practice","hang of","difficult","trial and error"]],[["价格","价钱","价位","多少钱","性价比","值不值","贵","便宜"],["price","expensive","cheap","cost","worth","value","how much","pricey","bucks","$"]],[["保修","质保","售后","客服"],["warranty","guarantee","refund","customer service","support"]],[["买错","错款","买错款","发错"],["wrong","bait","misleading","advertised","all i got"]],[["发货","物流","配送","快递","缺货","缺件","到货"],["ship","deliver","arrive","missing","delay","alaska"]],[["做工","质量","品控","材质","金属","塑料","结实"],["quality","build","sturdy","metal","plastic","solid","flimsy","defect"]],[["收纳","箱子","收纳箱","便携","携带"],["case","carry","storage","organiz","portable"]],[["镜面","抛光","光亮"],["mirror","polish","shine"]],[["荡刀","皮革","皮带","磨刀带"],["strop","leather","compound"]],[["抛光膏","研磨膏"],["compound","polishing"]],[["修复","崩口","缺口","重塑","修刃"],["repair","chip","reshape","reprofile","restore","damaged"]],[["开刃","刃角","斜面"],["bevel"]],[["钝刀","钝"],["dull","blunt"]],[["手磨","徒手","手工磨"],["by hand","freehand","free hand","hand sharpen","skill issue"]],[["质疑","怀疑","造假","假的","噱头","智商税"],["phony","fake","scam","gimmick","show me","staged"]],[["配音","人声","数字人","机器人"],["owen wilson","robot","white person","clear its throat","voiceover","voice over"]],[["送礼","礼物","老公","爸爸","父亲节"],["gift","husband","dad","wife","father"]],[["斧头","斧"],["axe","hatchet"]],[["武士刀","日本刀","砍刀","剑"],["sword","katana","machete"]],[["锯齿刀","锯齿","锯刃"],["serrat","seriat"]],[["剪刀"],["scissor","clipper","shears"]],[["凿子","刨刀"],["chisel","plane iron"]],[["弯刀","弧形","弧度"],["curved","recurve","concave","tanto"]],[["菜刀","厨刀","厨师刀","切肉刀","牛排刀"],["chef","kitchen","cleaver","butcher","steak","santoku"]],[["竞品","对手"],["work sharp","worksharp","lansky","tormek","wicked edge","kme","edge pro","hapstone","xarilk","ken onion","spyderco","shapton"]],[["产地","美国制造","国产","中国制造"],["made in","usa"]],[["安装","组装","装配","调试"],["assembl","setup","set up","install","screw"]],[["螺丝","内六角","扳手"],["screw","allen"]],[["锋利","剃须","剃毛","削纸","切纸","番茄"],["razor","shav","hair","paper","tomato"]],[["测评","评测","视频","油管"],["review","youtube","video","outdoor55"]],[["好评","喜欢","满意","推荐","好用"],["love","great","awesome","amazing","excellent","recommend","happy","satisf"]],[["差评","失望","不好","垃圾"],["disappoint","poor","waste","junk","bad","worst"]],[["缺点","缺陷","问题","毛病","投诉","不足"],["issue","problem","defect","complain","downside","flaw","disappoint"]],[["重量","重","沉"],["heavy","weight"]],[["马克笔","记号笔","蓝墨水"],["marker","sharpie","blue ink"]],[["户外","野外","狩猎","露营","钓鱼"],["hunting","outdoor","camp","bushcraft","fishing"]],[["多功能刀","多功能工具","军刀"],["multitool","multi-tool","leatherman"]],[["磨损","损耗","耐用"],["wear","worn","wore","durab"]],[["步骤","流程"],["step"]],[["一整套","套装","整套"],["kit","complete","full set"]],[["多久","时间","快","慢"],["minute","hour","quick","fast","slow"]],[["磨刀","磨"],["sharpen","hone"]],[["学会","学习"],["learn"]]];

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
function one(query, name, fallback = '') {
  const values = query.getAll(name);
  if (values.length > 1) throw new ApiError(400, `参数 ${name} 只能出现一次。`);
  return values.length ? values[0] : fallback;
}
function integer(query, name, fallback) {
  const value = one(query, name, String(fallback));
  if (!/^[+-]?\d+$/.test(value.trim())) throw new ApiError(400, `参数 ${name} 必须是整数。`);
  const number = Number(value);
  if (!Number.isSafeInteger(number)) throw new ApiError(400, `参数 ${name} 超出范围。`);
  return number;
}
function productKey(value) {
  const text = String(value || '').trim();
  const codes = [...new Set(text.toUpperCase().match(/(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])/g) || [])];
  return codes.length === 1 ? codes[0] : text.toLowerCase();
}
function productMatches(item, selected) {
  return !selected || selected === 'all' || productKey(item.product) === productKey(selected);
}
function filteredCatalog(catalog, selected) {
  return Object.fromEntries(Object.entries(catalog || {}).map(([key, value]) =>
    [key, Array.isArray(value) ? value.filter(item => productMatches(item, selected)) : value]));
}
function array(value, key) { return Array.isArray(value) ? value : value?.[key] || []; }
function wrapped(value, key) {
  return value && Object.prototype.hasOwnProperty.call(value, key) ? value : { [key]: value };
}
function required(value, message) {
  if (value === undefined || value === null) throw new ApiError(404, message);
  return value;
}
function secure(response, request) {
  const headers = new Headers(response.headers);
  headers.set('Content-Security-Policy', POLICY);
  headers.set('X-Content-Type-Options', 'nosniff');
  headers.set('Referrer-Policy', 'no-referrer');
  headers.set('X-Frame-Options', 'DENY');
  headers.set('Permissions-Policy', 'camera=(), microphone=(), geolocation=()');
  headers.set('Cross-Origin-Resource-Policy', 'same-origin');
  headers.set('X-Robots-Tag', 'noindex, nofollow');
  headers.set('Cache-Control', 'no-store');
  headers.delete('Access-Control-Allow-Origin');
  return new Response(request.method === 'HEAD' ? null : response.body,
    { status: response.status, statusText: response.statusText, headers });
}
function json(value, status = 200, extra = {}) {
  return new Response(JSON.stringify(value), {
    status, headers: { 'Content-Type': 'application/json; charset=utf-8', ...extra },
  });
}
function publicVersion(data) {
  const exported_at = String(data.meta?.exported_at || data.state?.exported_at || '');
  const version = String(data.meta?.content_revision || data.meta?.public_revision || data.meta?.revision || data.state?.digest || exported_at);
  return { version, exported_at };
}
async function loadSnapshot(env, request, path, refresh = false) {
  if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') throw new ApiError(503, '公开文字资料暂时不可用。');
  let cache = assetCaches.get(env.ASSETS);
  if (!cache) { cache = new Map(); assetCaches.set(env.ASSETS, cache); }
  // Polls bypass our manifest cache. A deployment may reuse stable asset URLs or
  // even the binding object; a successful new revision clears cached fragments.
  let promise = refresh ? manifestRefreshes.get(env.ASSETS) : cache.get(path);
  if (!promise) {
    const sequence = path === BUNDLE_PATH ? (manifestSequences.get(env.ASSETS) || 0) + 1 : 0;
    if (sequence) manifestSequences.set(env.ASSETS, sequence);
    promise = env.ASSETS.fetch(new Request(new URL(path, request.url), {
      method: 'GET', ...(refresh ? { headers: { 'Cache-Control': 'no-cache' } } : {}),
    }))
      .then(async response => {
        if (!response.ok) throw new Error('snapshot unavailable');
        const data = await response.json();
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('invalid snapshot');
        if (path === BUNDLE_PATH && (!data.state || !data.cases)) throw new Error('invalid manifest');
        if (path === BUNDLE_PATH && sequence === manifestSequences.get(env.ASSETS)) {
          const identity = JSON.stringify(publicVersion(data));
          const previous = manifestVersions.get(env.ASSETS);
          if (previous !== identity) cache.clear();
          manifestVersions.set(env.ASSETS, identity);
          cache.set(BUNDLE_PATH, promise);
        }
        return data;
      }).catch(() => {
        // An old in-flight request must not remove a newer revision's entry.
        if (cache.get(path) === promise) cache.delete(path);
        throw new ApiError(503, '公开文字资料暂时不可用。');
      }).finally(() => {
        if (refresh && manifestRefreshes.get(env.ASSETS) === promise) manifestRefreshes.delete(env.ASSETS);
      });
    if (refresh) manifestRefreshes.set(env.ASSETS, promise);
    else cache.set(path, promise);
  }
  return promise;
}
async function apiSnapshot(env, request, path) {
  const base = await loadSnapshot(env, request, BUNDLE_PATH);
  if (!base.split || path === '/api/state') return base;
  let fragment;
  let match;
  if (path === '/api/reviews' || /^\/api\/reviews\/(?:summary|picks|tag-edits|[0-9a-f]{16})$/.test(path)) fragment = '/data/reviews.json';
  else if (/^\/api\/performance\/(?:summary|analytics|videos)$/.test(path)) fragment = '/data/performance.json';
  else if ((match = /^\/api\/performance\/videos\/(\d{6,25})$/.exec(path))) fragment = `/data/video-shard-${Number(match[1].slice(-5)) % 32}.json`;
  else if ((match = /^\/api\/cases\/(PVR-[0-9a-f]{12})$/.exec(path))) {
    if (!(base.state.cases || []).some(item => item.case_id === match[1])) throw new ApiError(404, '案例不存在。');
    fragment = `/data/case-${match[1]}.json`;
  } else if (/^\/api\/ai\/(?:status|options|tasks(?:\/AIT-[0-9a-f]{12})?)$/.test(path)) fragment = '/data/ai.json';
  else if (/^\/api\/(?:creation|materials|scripts\/SCR-[0-9a-f]{12})$/.test(path)) fragment = '/data/creation.json';
  else if (/^\/api\/direction-catalog(?:\/[^/]{1,100})?$/.test(path)) fragment = '/data/direction-catalog.json';
  else if (path === '/api/directions') fragment = '/data/directions.json';
  else throw new ApiError(404, '页面不存在。');
  return { ...base, ...await loadSnapshot(env, request, fragment), state: base.state };
}

function chineseGroups(token) {
  const taken = Array(token.length).fill(false), found = [];
  const words = ZH_SEARCH.flatMap(([aliases, english]) => aliases.map(word => [word, english]))
    .sort((a, b) => b[0].length - a[0].length);
  for (const [word, english] of words) {
    if (word.length === 1 && token !== word) continue;
    for (let at = token.indexOf(word); at >= 0; at = token.indexOf(word, at + 1)) {
      if (!taken.slice(at, at + word.length).some(Boolean)) {
        taken.fill(true, at, at + word.length);
        found.push([at, word, english]);
      }
    }
  }
  const seen = new Set();
  return found.sort((a, b) => a[0] - b[0]).filter(([, , english]) => {
    const key = JSON.stringify(english);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  }).map(([, term, english]) => ({ term, english }));
}
function searchTerms(text) {
  return (String(text || '').match(/[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[^\s\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+/g) || [])
    .slice(0, 8).map(term => {
      const chinese = /[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]/.test(term);
      return { term, chinese, groups: chinese ? chineseGroups(term) : [] };
    });
}
function describeQuery(terms) {
  const seen = new Set(), expanded = [];
  for (const item of terms) for (const group of item.groups) {
    if (!seen.has(group.term)) { seen.add(group.term); expanded.push(group); }
  }
  return { expanded, unmapped: terms.filter(item => item.chinese && !item.groups.length).map(item => item.term) };
}
function reviewPredicate(query, summary) {
  const multi = Object.fromEntries(['platform', 'role', ...TAG_KINDS].map(key => [key, query.getAll(key).filter(Boolean)]));
  for (const platform of multi.platform) if (!PLATFORMS.includes(platform)) throw new ApiError(400, `未知平台：${platform}`);
  for (const kind of TAG_KINDS) for (const tag of multi[kind]) {
    if (tag !== 'none' && !Object.prototype.hasOwnProperty.call(summary.labels?.[kind] || {}, tag)) {
      throw new ApiError(400, `未知标签：${tag}`);
    }
  }
  const filters = Object.fromEntries(['q', 'rating_min', 'rating_max', 'language', 'product', 'sku', 'replied', 'video', 'since'].map(key => [key, one(query, key)]));
  const included = Object.fromEntries(['include_brand', 'include_irrelevant', 'include_empty'].map(key => [key, one(query, key) === '1']));
  const rating = {};
  for (const key of ['rating_min', 'rating_max']) if (filters[key] !== '') {
    if (!/^[+-]?\d+$/.test(filters[key].trim())) throw new ApiError(400, '星级筛选无效。');
    rating[key] = Number(filters[key]);
  }
  if (filters.video && !/^\d{6,25}$/.test(filters.video)) throw new ApiError(400, '视频编号无效。');
  if (filters.since && !/^\d{4}-\d{2}-\d{2}$/.test(filters.since)) throw new ApiError(400, '日期应写成 YYYY-MM-DD。');
  if (filters.replied && !['yes', 'no'].includes(filters.replied)) throw new ApiError(400, '回复情况筛选无效。');
  const terms = searchTerms(filters.q);
  const predicate = (item, ignorePlatform = false) => {
    if (!ignorePlatform && multi.platform.length && !multi.platform.includes(item.platform)) return false;
    if (multi.role.length ? !multi.role.includes(item.role) : (!included.include_brand && item.role === 'brand_account')) return false;
    if (!included.include_irrelevant && Number(item.relevant) !== 1) return false;
    if (!included.include_empty && !String(item.text || '').length) return false;
    if (rating.rating_min !== undefined && (item.rating == null || item.rating < rating.rating_min)) return false;
    if (rating.rating_max !== undefined && (item.rating == null || item.rating > rating.rating_max)) return false;
    if (['en', 'other'].includes(filters.language) && item.language !== filters.language) return false;
    if (!productMatches(item, filters.product)) return false;
    if (filters.sku && item.sku !== filters.sku) return false;
    if (filters.video && (item.platform !== 'tiktok_video' || item.source_ref !== filters.video)) return false;
    if (filters.since && (!item.first_seen || item.first_seen < filters.since)) return false;
    if (filters.replied && (item.platform !== 'tiktok_video' || item.is_reply || Boolean(item.replies?.total) !== (filters.replied === 'yes'))) return false;
    for (const kind of TAG_KINDS) {
      const tags = item.tags?.[kind] || [];
      if (!multi[kind].every(tag => tag === 'none' ? !tags.length : tags.includes(tag))) return false;
    }
    const text = `${item.title || ''}\n${item.text || ''}`.toLowerCase();
    return terms.every(term => text.includes(term.term.toLowerCase()) ||
      (term.groups.length && term.groups.every(group => [group.term, ...group.english].some(word => text.includes(word.toLowerCase())))));
  };
  return { predicate, description: describeQuery(terms) };
}
function compareNullable(a, b, ascending = false) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return (a < b ? -1 : a > b ? 1 : 0) * (ascending ? 1 : -1);
}
function reviewSort(sort) {
  const sorts = {
    likes: (a, b) => compareNullable(a.likes, b.likes) || compareNullable(a.posted_on, b.posted_on),
    newest: (a, b) => compareNullable(a.posted_on, b.posted_on),
    oldest: (a, b) => compareNullable(a.posted_on, b.posted_on, true),
    rating_low: (a, b) => compareNullable(a.rating, b.rating, true) || compareNullable(a.posted_on, b.posted_on),
    rating_high: (a, b) => compareNullable(a.rating, b.rating) || compareNullable(a.posted_on, b.posted_on),
    longest: (a, b) => String(b.text || '').length - String(a.text || '').length,
  };
  if (!sorts[sort]) throw new ApiError(400, '排序方式无效。');
  return (a, b) => sorts[sort](a, b) || String(a.id).localeCompare(String(b.id), 'en');
}
function reviews(data, query) {
  const items = array(data.reviews?.items, 'reviews'), summary = data.reviews?.summary || {};
  const { predicate, description } = reviewPredicate(query, summary);
  const limit = Math.max(1, Math.min(integer(query, 'limit', 50), 200));
  const offset = Math.max(0, integer(query, 'offset', 0)), sort = reviewSort(one(query, 'sort', 'likes'));
  const selected = items.filter(item => predicate(item));
  const facets = { platform: {}, role: {}, rating: {}, nature: {}, topic: {}, competitor: {} };
  const count = (target, key) => { if (key != null) target[key] = (target[key] || 0) + 1; };
  for (const item of items) if (predicate(item, true)) count(facets.platform, item.platform);
  for (const item of selected) {
    count(facets.role, item.role); count(facets.rating, item.rating);
    for (const kind of TAG_KINDS) {
      const tags = item.tags?.[kind] || [];
      for (const tag of tags) count(facets[kind], tag);
      if (!tags.length && kind !== 'competitor') count(facets[kind], 'none');
    }
  }
  const page = selected.sort(sort).slice(offset, offset + limit);
  const videoIds = new Set(page.filter(item => item.platform === 'tiktok_video').map(item => item.source_ref).filter(Boolean));
  if (query.get('video')) videoIds.add(query.get('video'));
  const videos = Object.fromEntries([...videoIds].map(id => [id, data.reviews?.videos?.[id] || {}]));
  return { total: selected.length, limit, offset, reviews: page, facets, query: description, videos };
}
function reviewDetail(data, id) {
  const items = array(data.reviews?.items, 'reviews');
  const item = required(items.find(item => item.id === id), '评论库里没有这条评论。');
  const top = item.parent_id || item.id;
  const thread = items.filter(row => row.id !== id && (row.id === top || row.parent_id === top))
    .sort((a, b) => Number(a.is_reply) - Number(b.is_reply) ||
      (a.posted_on == null ? b.posted_on == null ? 0 : -1 : b.posted_on == null ? 1 : compareNullable(a.posted_on, b.posted_on, true)));
  return { review: { ...item, thread } };
}

function directions(data, query) {
  const product = one(query, 'product'), direction = one(query, 'direction_id'), archive = one(query, 'archive', 'active');
  if (!['active', 'archived', 'all'].includes(archive)) throw new ApiError(400, '归档筛选无效。');
  const source = archive === 'active' ? data.directions : data.directions_all || data.directions;
  if (!product && !direction && archive !== 'archived' && source) return source;
  const selected = (data.state?.cases || []).filter(item =>
    productMatches(item, product) && (archive === 'all' || Boolean(item.archived) === (archive === 'archived')));
  const caseIds = new Set(selected.map(item => item.case_id));
  const groups = (source?.groups || []).filter(group => productMatches(group, product) && (!direction || group.direction_id === direction))
    .map(group => {
      const observations = (group.observations || []).filter(item => caseIds.has(item.case_id));
      if (!observations.length) return null;
      const ids = new Set(observations.map(item => item.case_id));
      const variant_counts = { direction: 0, execution: 0 }, missing_counts = { metrics: 0, evidence_window: 0, script_version: 0, video_id: 0 };
      const metric_availability = Object.fromEntries(Object.keys(group.metric_availability || {}).map(key => [key, 0]));
      const sales_status_counts = {};
      for (const item of observations) {
        const variant = item.test_direction?.variant_type;
        if (variant in variant_counts) variant_counts[variant]++;
        const metrics = item.metrics || {};
        const present = Object.keys(metric_availability).filter(key => Object.prototype.hasOwnProperty.call(metrics, key));
        missing_counts.metrics += Number(!present.length);
        missing_counts.evidence_window += Number(!item.evidence?.window);
        missing_counts.script_version += Number(!item.script_version);
        missing_counts.video_id += Number(!metrics.video_id);
        for (const key of present) metric_availability[key]++;
        if (item.sales_data?.status) sales_status_counts[item.sales_data.status] = (sales_status_counts[item.sales_data.status] || 0) + 1;
      }
      const cohorts = (group.cohorts || []).map(cohort => {
        const case_ids = (cohort.case_ids || []).filter(id => ids.has(id));
        return { ...cohort, case_ids, case_count: case_ids.length };
      }).filter(cohort => cohort.case_count);
      const duplicate_video_ids = (group.duplicate_video_ids || []).map(item => ({ ...item, case_ids: item.case_ids.filter(id => caseIds.has(id)), assignments: (item.assignments || []).filter(item => caseIds.has(item.case_id)) })).filter(item => item.case_ids.length > 1);
      return { ...group, observations, case_count: observations.length, variant_counts, missing_counts, metric_availability, sales_status_counts, cohorts,
        version_conflict_count: observations.filter(item => item.version_conflict).length, duplicate_video_ids,
        cross_direction_video_count: duplicate_video_ids.filter(item => item.cross_direction).length };
    }).filter(Boolean);
  const lead_tier_counts = { S: 0, A: 0, B: 0, uncoded: 0 };
  for (const group of groups) lead_tier_counts[group.lead_code?.[0] in lead_tier_counts ? group.lead_code[0] : 'uncoded']++;
  return { ...(source || {}), archive, filters: { product: productKey(product) || null, product_input: product || null, direction_id: direction || null },
    groups, case_count: selected.length, classified_case_count: groups.reduce((n, group) => n + group.case_count, 0),
    unclassified_case_ids: selected.filter(item => !item.current?.test_direction).map(item => item.case_id), lead_tier_counts,
    duplicate_video_ids: (source?.duplicate_video_ids || []).filter(item => item.case_ids.filter(id => caseIds.has(id)).length > 1) };
}
function directionDetail(data, id, product) {
  const catalog = filteredCatalog(data.direction_catalog, product);
  const matches = (catalog.directions || []).filter(item => item.direction_id === id);
  if (!matches.length) throw new ApiError(404, '这条方向不存在。');
  if (matches.length !== 1) throw new ApiError(400, '多个产品使用此方向编号，请先选择产品。');
  const entity = matches[0], selectedProduct = entity.product;
  const saved = data.direction_details?.[`${selectedProduct}|${id}`] || data.direction_details?.[`${productKey(selectedProduct)}|${id}`];
  if (saved) return saved;
  const cases = Object.values(data.cases || {}).filter(item => productMatches(item, selectedProduct) && item.current?.test_direction?.direction_id === id);
  const creation = filteredCatalog(data.creation, selectedProduct), independent = (creation.scripts || []).filter(item => item.direction_id === id);
  const caseByVideo = Object.fromEntries(cases.map(item => [String(item.current?.metrics?.video_id || ''), item.case_id]));
  const links = (catalog.video_links || []).filter(item => item.product === entity.product);
  const videos = new Set([...links.filter(item => item.direction_id === id).map(item => item.video_id), ...Object.keys(caseByVideo)].filter(Boolean));
  for (const video of [...videos]) if (links.some(item => item.video_id === video && item.direction_id !== id)) videos.delete(video);
  for (const item of independent) if (item.video_id) videos.add(item.video_id);
  const query = new URLSearchParams({ product: selectedProduct, direction_id: id, archive: 'all' });
  return { revision: catalog.revision, direction: entity,
    selling_point: (catalog.selling_points || []).find(item => item.id === entity.selling_point_id) || null,
    group: directions(data, query).groups[0] || null, cases,
    scripts: [...cases.filter(item => item.current?.transcript).map(item => ({ case_id: item.case_id, title: item.current.title, script_version: item.current.production?.script_version || null, text: item.current.transcript.text })), ...independent],
    creation_revision: creation.revision, plans: (creation.plans || []).filter(item => item.direction_id === id),
    theme_links: (creation.theme_links || []).filter(item => item.direction_id === id || item.selling_point_id === entity.selling_point_id),
    tasks: array(data.ai?.tasks, 'tasks').filter(item => productMatches(item, selectedProduct) && item.direction_id === id),
    videos: [...videos].sort().map(video => ({ video_id: video, ...(data.reviews?.videos?.[video] || {}), product: selectedProduct, direction_id: id, case_id: caseByVideo[video] || null })) };
}

function api(data, path, query) {
  if (path === '/api/state') {
    const since = one(query, 'since');
    if (since && since === data.state.digest) return { app: data.state.app, token: data.state.token || '', signature: data.state.signature, digest: data.state.digest, unchanged: true };
    return data.state;
  }
  if (path === '/api/creation') return filteredCatalog(data.creation, one(query, 'product'));
  if (path === '/api/materials') return filteredCatalog(data.materials, one(query, 'product'));
  if (path === '/api/direction-catalog') return filteredCatalog(data.direction_catalog, one(query, 'product'));
  if (path === '/api/directions') return directions(data, query);
  if (path === '/api/ai/status') return { ...(data.ai?.status || {}), available: false, queued: 0, running: 0, read_only: true, message: '公网文字浏览版；AI 历史可查看。' };
  if (path === '/api/ai/options') return data.ai?.options || { models: [], skills: [] };
  if (path === '/api/ai/tasks') {
    const caseId = one(query, 'case_id'), product = one(query, 'product');
    return { tasks: array(data.ai?.tasks, 'tasks').filter(item => (!caseId || item.case_id === caseId) && productMatches(item, product)) };
  }
  if (path === '/api/performance/summary') return data.performance?.summary || {};
  if (path === '/api/performance/analytics') return data.performance?.analytics || {};
  if (path === '/api/performance/videos') {
    const product = one(query, 'product');
    return { videos: array(data.performance?.videos, 'videos').filter(item => productMatches(item, product)) };
  }
  if (path === '/api/reviews/summary') return data.reviews?.summary || {};
  if (path === '/api/reviews') return reviews(data, query);
  if (path === '/api/reviews/picks') return { picks: array(data.reviews?.picks, 'picks') };
  if (path === '/api/reviews/tag-edits') return { edits: array(data.reviews?.tag_edits, 'edits') };
  let match;
  if ((match = /^\/api\/cases\/(PVR-[0-9a-f]{12})$/.exec(path))) return required(data.cases?.[match[1]], '案例不存在。');
  if ((match = /^\/api\/scripts\/(SCR-[0-9a-f]{12})$/.exec(path))) return required(data.scripts?.[match[1]], '这条文案版本不存在。');
  if ((match = /^\/api\/direction-catalog\/([^/]{1,100})$/.exec(path))) return directionDetail(data, match[1], one(query, 'product'));
  if ((match = /^\/api\/ai\/tasks\/(AIT-[0-9a-f]{12})$/.exec(path))) return wrapped(required(data.ai?.details?.[match[1]], 'AI 历史任务不存在。'), 'task');
  if ((match = /^\/api\/performance\/videos\/(\d{6,25})$/.exec(path))) return wrapped(required(data.performance?.details?.[match[1]], '后台数据里没有这条视频。'), 'video');
  if ((match = /^\/api\/reviews\/([0-9a-f]{16})$/.exec(path))) return reviewDetail(data, match[1]);
  throw new ApiError(404, '页面不存在。');
}

export default {
  async fetch(request, env) {
    let response;
    try {
      if (!['GET', 'HEAD'].includes(request.method)) {
        response = json({ error: '公网文字浏览版仅支持读取。' }, 405, { Allow: 'GET, HEAD' });
      } else {
        const url = new URL(request.url);
        let path;
        try { path = decodeURIComponent(url.pathname); } catch { throw new ApiError(400, '页面路径无效。'); }
        if (path.includes('\\') || path.includes('%') || path.includes('\0') || path.split('/').includes('..')) throw new ApiError(404, '页面不存在。');
        if (/^\/(?:public-data\.json(?:\/|$)|_worker\.js(?:\/|$)|_routes\.json(?:\/|$)|media(?:\/|$)|data(?:\/|$))/.test(path) ||
          /^\/api\/(?:open|upload|service[^/]*)(?:\/|$)/.test(path)) throw new ApiError(404, '页面不存在。');
        if (path.startsWith('/api/')) {
          response = path === '/api/public-version'
            ? json(publicVersion(await loadSnapshot(env, request, BUNDLE_PATH, true)))
            : json(api(await apiSnapshot(env, request, path), path, url.searchParams));
        } else {
          if (path !== '/' && path !== '/robots.txt' && !/^\/[a-zA-Z0-9/_-]+\.(?:html|css|js)$/.test(path)) throw new ApiError(404, '页面不存在。');
          if (!env.ASSETS || typeof env.ASSETS.fetch !== 'function') throw new ApiError(503, '页面暂时不可用。');
          response = await env.ASSETS.fetch(request);
          if (response.status >= 300 && response.status < 400) throw new ApiError(404, '页面不存在。');
          const type = response.headers.get('Content-Type') || '';
          if (!/^(?:text\/(?:html|css|javascript|plain)|application\/(?:javascript|x-javascript))\b/i.test(type) && response.ok) throw new ApiError(404, '页面不存在。');
        }
      }
    } catch (error) {
      response = json({ error: error instanceof ApiError ? error.message : '页面暂时不可用。' }, error instanceof ApiError ? error.status : 503);
    }
    return secure(response, request);
  },
};
