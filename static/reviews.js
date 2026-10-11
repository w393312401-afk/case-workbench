"use strict";

// Real-user review library: reviews from TikTok videos, TikTok Shop, Amazon, Reddit and Facebook, with full-text
// search, classification chips and filters, the videos the comments sit under, and the records people keep here
// (tag changes made by hand, quotes picked for a script). Reviewer names are never stored.
(() => {
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const currentProduct = () => window.WorkbenchShell?.product() || "";
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const defaults = {
    q: "", platform: [], role: "", rating: "", language: "", sort: "likes", nature: [], topic: [], competitor: [],
    brand: false, empty: false, irrelevant: false, replied: "", video: "", sku: "", since: "",
  };
  const fresh = () => ({ ...defaults, platform: [], nature: [], topic: [], competitor: [] });
  // threads: conversations already fetched (by review id). open / editing: the cards whose conversation / tag editor is
  // expanded; both are kept across repaints. Picks are shared with the global material basket.
  const rev = { summary: null, data: null, shown: 50, request: 0, resultQuery: null, threads: {}, threadLoading: {}, open: new Set(), editing: new Set(), filters: fresh() };
  let searchTimer = null, searchField = null, searchHash = "", submittedSearch = null;

  function cancelSearch() {
    clearTimeout(searchTimer);
    searchTimer = null;
    searchField = null;
    searchHash = "";
  }
  function captureListState() {
    if (!$("#rev-results")) return null;
    const filters = { ...rev.filters, q: $("#rev-q")?.value ?? rev.filters.q };
    listKeys.forEach(key => { filters[key] = [...rev.filters[key]]; });
    const query = hashQuery(filters);
    return { product: renderedProduct, filters, shown: rev.shown, open: [...rev.open], hash: pageURL(`#reviews${query ? `?${query}` : ""}`) };
  }
  function leaveList() {
    cancelSearch();
    rev.editing.clear();
  }

  function prepareSearchRefresh() {
    if (searchTimer === null) return;
    cancelSearch();
    rev.shown = 50;
    save();
  }
  try {
    const saved = JSON.parse(sessionStorage.getItem("review-library-filters") || "{}");
    for (const key of Object.keys(defaults)) if (key in saved && typeof saved[key] === typeof defaults[key]) rev.filters[key] = saved[key];
  } catch {
    /* Filters fall back to defaults without browser storage. */
  }

  const sorts = { likes: "按点赞/有帮助数", newest: "最新", oldest: "最早", rating_low: "星级从低到高", rating_high: "星级从高到低", longest: "最长" };
  const ratings = { "": "全部星级", 5: "5 星", 4: "4 星", 3: "3 星", 2: "2 星", 1: "1 星", low: "3 星及以下", high: "4 星及以上" };
  const languages = { "": "全部语言", en: "只看英文", other: "只看其他语言" };
  // Only TikTok video comments record who answered whom, so this filter leaves the other sources out.
  const repliedOptions = { "": "回复情况", no: "没人回复（仅 TikTok）", yes: "有人回复（仅 TikTok）" };
  const roleHints = {
    verified_buyer: "已在平台验证购买", incentivized_buyer: "免费试用或有激励，写评价时有利益关系", unverified_buyer: "没有验证购买标记",
    viewer: "看视频的观众，不一定是买家", peer: "爱好者社区里的讨论者，多为转述", brand_account: "品牌或达人账号自己的回复，不是用户声音",
  };
  const NL = String.fromCharCode(10);
  const num = (v) => Math.round(v).toLocaleString("en-US");
  const compact = (v) => (v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${Math.round(v / 1e3)}K` : String(Math.round(v)));

  // ---- the filters, kept in the address so a view can be bookmarked or linked to from another page ----
  const listKeys = ["platform", "nature", "topic", "competitor"];
  const textKeys = ["q", "role", "rating", "language", "sort", "replied", "video", "sku", "since"];
  const flagKeys = ["brand", "empty", "irrelevant"];

  function hashQuery(filters = rev.filters) {
    const p = new URLSearchParams();
    listKeys.forEach((k) => filters[k].forEach((v) => p.append(k, v)));
    textKeys.forEach((k) => { if (filters[k] && filters[k] !== defaults[k]) p.set(k, filters[k]); });
    flagKeys.forEach((k) => { if (filters[k]) p.set(k, "1"); });
    return p.toString();
  }

  // null when the address carries no filters (the saved ones stay in force).
  function filtersFromHash(hash = pageHash()) {
    const at = hash.indexOf("?");
    if (at < 0) return null;
    const p = new URLSearchParams(hash.slice(at + 1)), f = fresh();
    if (![...p.keys()].some(key => key !== "product")) return null;
    listKeys.forEach((k) => { f[k] = p.getAll(k).filter(Boolean); });
    textKeys.forEach((k) => { if (p.has(k)) f[k] = p.get(k); });
    flagKeys.forEach((k) => { f[k] = p.get(k) === "1"; });
    return f;
  }

  // Whatever came from the address or from storage is checked against what the library really has.
  function sanitize(f, s = rev.summary) {
    const list = (value, labels, extra = []) => (Array.isArray(value) ? value.filter((c) => typeof c === "string" && (!labels || labels[c] || extra.includes(c))) : []);
    f.platform = list(f.platform, s?.labels.platform);
    f.nature = list(f.nature, s?.labels.nature, ["none"]);
    f.topic = list(f.topic, s?.labels.topic, ["none"]);
    f.competitor = list(f.competitor, s?.labels.competitor);
    if (s) f.role = s.labels.role[f.role] ? f.role : "";
    f.rating = String(f.rating) in ratings ? String(f.rating) : "";
    f.language = f.language in languages ? f.language : "";
    f.replied = f.replied in repliedOptions ? f.replied : "";
    f.sort = f.sort in sorts ? f.sort : "likes";
    f.video = /^\d{6,25}$/.test(f.video) ? f.video : "";
    if (s) f.sku = s.skus.includes(f.sku) ? f.sku : "";
    f.since = /^\d{4}-\d{2}-\d{2}$/.test(f.since) ? f.since : "";
    return f;
  }

  function writeHash() {
    if (!pageHash().startsWith("#reviews")) return;
    const query = hashQuery(), next = pageURL(`#reviews${query ? `?${query}` : ""}`);
    if (location.hash !== next) history.replaceState(null, "", next);
  }

  const save = () => {
    try { sessionStorage.setItem("review-library-filters", JSON.stringify(rev.filters)); } catch { /* ignore */ }
    writeHash();
  };

  function params() {
    const f = rev.filters, p = new URLSearchParams();
    listKeys.forEach((k) => f[k].forEach((v) => p.append(k, v)));
    if (currentProduct()) p.set("product", currentProduct());
    if (f.role) p.set("role", f.role);
    if (f.q.trim()) p.set("q", f.q.trim());
    if (f.language) p.set("language", f.language);
    if (f.replied) p.set("replied", f.replied);
    if (f.video) p.set("video", f.video);
    if (f.sku) p.set("sku", f.sku);
    if (f.since) p.set("since", f.since);
    if (f.rating === "low") p.set("rating_max", "3");
    else if (f.rating === "high") p.set("rating_min", "4");
    else if (f.rating) { p.set("rating_min", f.rating); p.set("rating_max", f.rating); }
    if (f.brand) p.set("include_brand", "1");
    if (f.empty) p.set("include_empty", "1");
    if (f.irrelevant) p.set("include_irrelevant", "1");
    p.set("sort", f.sort);
    p.set("limit", String(rev.shown));
    return p;
  }

  async function loadSummary() {
    const summary = await api("/api/reviews/summary");
    // The page is served fresh from disk but the server only changes when it is restarted: an older server lacks the newer
    // fields, and the page must still work without them.
    summary.videos ||= [];
    summary.skus ||= [];
    window.WorkbenchShell?.registerProducts(summary.products || []);
    summary.labels.competitor ||= {};
    for (const key of ["new_recent", "picks", "tag_edits"]) summary[key] ||= 0;
    rev.summary = summary;
  }

  async function loadResults() {
    const mine = ++rev.request, hash = pageHash(), query = params().toString();
    const isCurrent = () => mine === rev.request && pageHash() === hash && params().toString() === query;
    submittedSearch = query;
    // A pending refresh can contain changed records even when its filters match the displayed data.
    rev.resultQuery = null;
    try {
      const data = await api(`/api/reviews?${query}`);
      if (!isCurrent()) return false; // input changes invalidate old results before the next debounce fires
      data.facets.competitor ||= {};
      data.videos ||= {};
      rev.data = data;
      rev.resultQuery = query;
      return true;
    } catch (error) {
      if (!isCurrent()) return false;
      submittedSearch = null;
      throw error;
    }
  }

  function highlight(text, terms) {
    const safe = esc(text);
    const words = terms.map((t) => t.trim()).filter((t) => t.length >= 1).map((t) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    if (!words.length) return safe;
    return safe.replace(new RegExp(`(${words.join("|")})`, "gi"), "<mark>$1</mark>");
  }

  function labelOf(kind, code) {
    if (code === "none") return "未归类";
    return rev.summary.labels[kind]?.[code] || code;
  }

  function chip(kind, code, count, active) {
    const label = labelOf(kind, code);
    return `<button type="button" class="rev-filter${active ? " is-active" : ""}" data-chip="${kind}" data-code="${esc(code)}" aria-pressed="${active}"><span>${esc(label)}</span><strong>${count}</strong></button>`;
  }

  function stars(value) {
    if (value == null) return "";
    return `<span class="rev-stars rev-stars-${value}" role="img" aria-label="${value} 星">${"★".repeat(value)}${"☆".repeat(5 - value)}</span>`;
  }

  function excerptAround(text, terms) {
    const lower = text.toLowerCase();
    let at = -1;
    for (const term of terms) {
      const i = lower.indexOf(term.toLowerCase());
      if (i >= 0 && (at < 0 || i < at)) at = i;
    }
    if (at < 380) return null; // the match is already inside the visible head
    const start = Math.max(0, at - 160);
    return `…${text.slice(start, start + 460).trim()}…`;
  }

  // ---- videos: the title and figures come from the video data, the link from the comment itself ----
  const videoName = (v, id) => v?.headline || v?.title || `视频 …${String(id).slice(-6)}`;
  const clip = (text, length) => (text.length > length ? `${text.slice(0, length - 1)}…` : text);
  const videoFigures = (v) => [v.account && `@${v.account}`, v.posted_on && `发布 ${v.posted_on}`, v.views != null && `播放 ${compact(v.views)}`,
    v.orders != null && `订单 ${num(v.orders)}`, v.ctor_pct != null && `CTOR ${v.ctor_pct}%`, v.gmv != null && `GMV $${num(v.gmv)}`].filter(Boolean).join(" · ");

  function videoLine(r) {
    if (r.platform !== "tiktok_video" || !r.source_ref || rev.filters.video) return "";
    const v = rev.data.videos?.[r.source_ref];
    return `<p class="rev-context">在视频「<button type="button" class="rev-video-chip" data-video="${esc(r.source_ref)}" title="只看这条视频下的评论">${esc(clip(videoName(v, r.source_ref), 40))}</button>」下${v && v.views != null ? ` · ${esc(videoFigures({ views: v.views, orders: v.orders, ctor_pct: v.ctor_pct }))}` : ""}</p>`;
  }

  function videoBar() {
    const id = rev.filters.video;
    if (!id) return "";
    const v = rev.data.videos?.[id] || {}, own = rev.summary.videos.find((x) => x.video_id === id);
    const link = `https://www.tiktok.com/@${encodeURIComponent(v.account || "_")}/video/${encodeURIComponent(id)}`;
    return `<section class="section-card rev-video-bar"><div><strong>只看一条视频下的评论</strong> · <a href="${esc(link)}" target="_blank" rel="noopener noreferrer">${esc(videoName(v, id))}</a>
      ${videoFigures(v) ? `<p>${esc(videoFigures(v))}</p>` : '<p class="rev-muted">视频数据里没有这条视频的标题和销售数字。</p>'}
      <p class="rev-muted">${own ? `库里有 ${num(own.comments)} 条评论，其中 ${num(own.open_questions)} 个提问没人回复。` : "库里没有这条视频的评论。"}</p></div>
      <div class="rev-video-actions">${v.case_id ? `<a class="secondary" href="#case/${esc(v.case_id)}">查看案例</a>` : ""}<button type="button" class="subtle" id="rev-video-clear">看全部视频</button></div></section>`;
  }

  // ---- one review ----
  // What a TikTok comment's replies look like in the list: how many, or "no reply" for a question nobody answered.
  function replyBadge(r) {
    if (!r.replies) return "";
    if (r.replies.total) return `<span class="rev-badge"${r.replies.brand ? ` title="其中 ${r.replies.brand} 条来自品牌或达人账号"` : ""}>${r.replies.total} 条回复</span>`;
    return r.tags.nature.includes("question") ? '<span class="rev-badge rev-badge-open" title="库里没有这条提问的任何回复">没人回复</span>' : "";
  }

  const isNew = (r) => Boolean(r.first_seen && rev.summary.new_since && r.first_seen >= rev.summary.new_since);

  function threadButton(r) {
    const label = r.platform === "tiktok_video" && r.is_reply ? "看上下文" : r.replies?.total ? `展开对话（${r.replies.total}）` : "";
    if (!label) return "";
    return `<button type="button" class="subtle" data-thread="${esc(r.id)}" aria-expanded="${rev.open.has(r.id)}">${icon("chat")}${label}</button>`;
  }

  // The rest of the conversation: the comment this one answers and its other replies, or the replies to this comment.
  function threadMarkup(rows) {
    if (!rows.length) return '<p class="rev-muted">库里没有这条评论的其他回复。</p>';
    const items = rows.map((t) => `<div class="rev-reply${t.role === "brand_account" ? " is-brand" : ""}${t.is_reply ? "" : " is-parent"}"><div class="rev-reply-head"><span class="rev-role rev-role-${t.role}" title="${esc(roleHints[t.role] || "")}">${esc(labelOf("role", t.role))}</span><span class="rev-meta">${esc(t.posted_on || "")}${t.is_reply ? "" : " · 原评论"}</span>${t.likes != null ? `<span class="rev-meta">点赞 ${num(t.likes)}</span>` : ""}</div><p class="rev-text">${t.text ? esc(t.text) : "（没有文字）"}</p></div>`).join("");
    return `${items}<p class="rev-muted">品牌或达人账号在评论区的回复同样未核实（价格、保修、适配范围都要以产品资料为准）。</p>`;
  }

  // Every tag with its state; a click adds or removes it. The dashed ones were changed by hand.
  function tagEditor(r) {
    const group = (kind, title) => `<div class="rev-chip-group"><span class="rev-chip-title">${title}</span><div class="rev-chip-row">${Object.keys(rev.summary.labels[kind]).map((code) => {
      const on = r.tags[kind].includes(code), manual = r.manual?.[kind]?.[code] !== undefined;
      return `<button type="button" class="rev-filter${on ? " is-active" : ""}${manual ? " is-manual" : ""}" data-tag-edit="${esc(r.id)}" data-kind="${kind}" data-code="${esc(code)}" data-on="${on}" aria-pressed="${on}" title="${manual ? "手动改过" : "规则自动打的"}"><span>${esc(labelOf(kind, code))}</span></button>`;
    }).join("")}</div></div>`;
    return `${group("nature", "性质")}${group("topic", "主题")}${group("competitor", "竞品")}<p class="rev-muted">点一下加上或去掉，会保存下来，重新同步后依然有效；虚线的是手动改过的。</p>`;
  }

  function card(r, terms) {
    const manual = (kind, t) => r.manual?.[kind]?.[t] === true;
    const tags = [["nature", r.tags.nature], ["topic", r.tags.topic], ["competitor", r.tags.competitor || []]]
      .flatMap(([kind, list]) => list.map((t) => `<span class="rev-tag rev-tag-${kind}${manual(kind, t) ? " is-manual" : ""}">${esc(labelOf(kind, t))}</span>`)).join("");
    const likes = r.likes != null ? `<span class="rev-meta rev-likes" title="${r.platform === "amazon" ? "觉得有帮助的人数" : "点赞"}">${r.platform === "amazon" ? "有帮助" : "赞"} ${num(r.likes)}</span>` : "";
    const long = r.text.length > 520, excerpt = long ? excerptAround(r.text, terms) : null;
    // A long review shows its head; when the search word sits further down, show the passage around it instead.
    const body = r.text
      ? `<div class="rev-body${excerpt ? " has-excerpt" : ""}">${excerpt ? `<p class="rev-text rev-excerpt">${highlight(excerpt, terms)}</p>` : ""}<p class="rev-text rev-full${long && !excerpt ? " is-clamped" : ""}">${highlight(r.text, terms)}</p>${long ? '<button type="button" class="subtle rev-expand" data-expand>展开全文</button>' : ""}</div>`
      : '<p class="rev-text rev-muted">（只打了星，没写文字）</p>';
    const editing = rev.editing.has(r.id), picked = Boolean(r.pick);
    const meta = [r.posted_on, r.sku, r.is_reply ? "回复" : "", r.language === "other" ? "其他语言" : ""].filter(Boolean).map(esc).join(" · ");
    return `<article class="rev-card${r.role === "brand_account" ? " is-brand" : ""}" data-id="${esc(r.id)}">
      <header class="rev-head"><span class="rev-platform rev-platform-${r.platform}">${esc(labelOf("platform", r.platform))}</span><span class="rev-role rev-role-${r.role}" title="${esc(roleHints[r.role] || "")}">${esc(labelOf("role", r.role))}</span>${stars(r.rating)}${replyBadge(r)}${isNew(r) ? '<span class="rev-badge rev-badge-new" title="最近一次同步新增的评论">新</span>' : ""}<span class="rev-meta rev-date">${meta}</span>${likes}</header>
      ${r.title ? `<h3 class="rev-title">${highlight(r.title, terms)}</h3>` : ""}${body}
      ${r.context ? `<p class="rev-context">在帖子「${esc(r.context.slice(0, 80))}」下</p>` : ""}${videoLine(r)}
      <footer class="rev-foot"><div class="rev-tags">${tags}</div><div class="rev-actions">${threadButton(r)}<button type="button" class="subtle rev-pick${picked ? " is-on" : ""}" data-pick="${esc(r.id)}" aria-pressed="${picked}" title="放进全局摘录篮，写稿时带入">${picked ? "已摘录" : "摘录"}</button><button type="button" class="subtle" data-copy="${esc(r.id)}">复制引用</button><details class="rev-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-edit-tags="${esc(r.id)}" aria-expanded="${editing}">改标签</button><button type="button" role="menuitem" data-review-theme="${esc(r.id)}">关联主题</button>${r.url ? `<a role="menuitem" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">打开原帖</a>` : ""}</div></details></div></footer>
      <div class="rev-tag-editor" id="rev-tags-${esc(r.id)}"${editing ? "" : " hidden"}>${editing ? tagEditor(r) : ""}</div>
      <div class="rev-thread" id="rev-thread-${esc(r.id)}" hidden></div></article>`;
  }

  // ---- the filters above the list ----
  // The source switch above the list: one source at a time, or all of them.
  function sourceBar() {
    const f = rev.filters, facets = rev.data.facets, all = rev.summary.platforms.reduce((sum, p) => sum + (facets.platform[p.platform] || 0), 0);
    const button = (code, label, count, on) => `<button type="button" class="rev-source${on ? " is-active" : ""}"${code ? ` data-chip="platform" data-code="${code}"` : " data-platform-all"} aria-pressed="${on}">${esc(label)}<span>${num(count)}</span></button>`;
    return button("", "全部", all, !f.platform.length) + rev.summary.platforms.map((p) => button(p.platform, p.label, facets.platform[p.platform] || 0, f.platform.includes(p.platform))).join("");
  }

  // The tag groups inside the filter drawer. "未归类" comes last: the reviews the rules found nothing to say about.
  function filterRows() {
    const f = rev.filters, facets = rev.data.facets, s = rev.summary;
    const group = (kind, title, active, withNone = true) => {
      const counts = facets[kind] || {};
      const codes = Object.keys(s.labels[kind]).filter((c) => (counts[c] || 0) > 0 || active.includes(c)).sort((a, b) => (counts[b] || 0) - (counts[a] || 0));
      if (withNone && ((counts.none || 0) > 0 || active.includes("none"))) codes.push("none");
      if (!codes.length && kind === "competitor") return "";
      return `<div class="rev-chip-group"><span class="rev-chip-title">${title}</span><div class="rev-chip-row">${codes.length ? codes.map((c) => chip(kind, c, num(counts[c] || 0), active.includes(c))).join("") : '<span class="rev-muted">当前筛选下没有</span>'}</div></div>`;
    };
    return `${group("nature", "性质", f.nature)}${group("topic", "主题", f.topic)}${group("competitor", "竞品", f.competitor, false)}`;
  }

  // Every filter in force, each removable on its own. [key, code, label]; code is set for the list filters.
  function activeFilters() {
    const f = rev.filters, out = [];
    f.platform.forEach((c) => out.push(["platform", c, labelOf("platform", c)]));
    if (f.q.trim()) out.push(["q", "", `“${f.q.trim()}”`]);
    if (f.rating) out.push(["rating", "", ratings[f.rating]]);
    if (f.role) out.push(["role", "", labelOf("role", f.role)]);
    if (f.language) out.push(["language", "", languages[f.language]]);
    if (f.replied) out.push(["replied", "", repliedOptions[f.replied].replace("（仅 TikTok）", "")]);
    if (f.video) out.push(["video", "", "视频：" + clip(videoName(rev.data.videos?.[f.video], f.video), 16)]);
    if (f.sku) out.push(["sku", "", "款式：" + f.sku]);
    if (f.since) out.push(["since", "", `近 ${rev.summary.new_days || 14} 天新增`]);
    for (const kind of ["nature", "topic", "competitor"]) f[kind].forEach((c) => out.push([kind, c, labelOf(kind, c)]));
    if (f.brand) out.push(["brand", "", "含品牌回复"]);
    if (f.empty) out.push(["empty", "", "含只打星评价"]);
    if (f.irrelevant) out.push(["irrelevant", "", "含没提到产品的评论"]);
    return out;
  }
  // How many of the drawer's own filters are set, shown on the 筛选 button.
  const drawerKeys = new Set(["rating", "role", "language", "replied", "video", "sku", "nature", "topic", "competitor", "brand", "empty", "irrelevant"]);
  function activeMarkup(items) {
    if (!items.length) return "";
    return items.map(([key, code, label]) => `<button type="button" class="rev-token" data-clear="${key}" data-code="${esc(code)}" aria-label="去掉筛选：${esc(label)}">${esc(label)}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M7 17 17 7"/></svg></button>`).join("")
      + '<button type="button" class="subtle rev-clear-all" data-reset>清除全部</button>';
  }
  // The quick filters: each is one click on, one click off.
  const quick = {
    low: { on: (f) => f.rating === "low", set: (on) => ({ rating: on ? "low" : "" }) },
    questions: { on: (f) => f.replied === "no" && f.nature.includes("question") && f.platform.join() === "tiktok_video",
      set: (on, f) => on ? { platform: ["tiktok_video"], nature: [...new Set([...f.nature, "question"])], replied: "no" } : { platform: [], nature: f.nature.filter((c) => c !== "question"), replied: "" } },
    defects: { on: (f) => f.nature.includes("defect"), set: (on, f) => ({ nature: on ? [...f.nature, "defect"] : f.nature.filter((c) => c !== "defect") }) },
  };
  function paintQuick() {
    const f = rev.filters;
    for (const [key, item] of Object.entries(quick)) { const button = $(`[data-quick="${key}"]`); if (button) button.setAttribute("aria-pressed", String(item.on(f))); }
  }

  // The words to mark in the text: what was typed, plus the English words a Chinese search word was also searched as.
  function highlightTerms(data) {
    const typed = rev.filters.q.trim().split(/\s+/).filter(Boolean);
    const english = (data.query?.expanded || []).flatMap((e) => e.english).filter((w) => w.trim().length >= 3);
    return [...new Set([...typed, ...english])];
  }

  // Tells the reader what a Chinese search word turned into, since the reviews are English.
  function queryNote(data) {
    const q = data.query || {}, parts = [];
    for (const e of q.expanded || []) parts.push(`「${esc(e.term)}」也按 ${e.english.map((w) => `<code>${esc(w)}</code>`).join(" ")} 搜索`);
    for (const t of q.unmapped || []) parts.push(`「${esc(t)}」没有对应的英文词，只按原文搜索；评论基本是英文，可换成英文词，或点下面的主题标签`);
    return parts.join("；");
  }

  function resultsMarkup() {
    const d = rev.data, terms = highlightTerms(d);
    if (!d.reviews.length) return `<div class="empty">${icon("search")}<h3>没有符合条件的评论</h3><p>换个关键词，或点掉一些标签。</p></div>`;
    const more = d.total > d.reviews.length ? `<button type="button" class="secondary rev-more" id="rev-more">再显示 ${Math.min(50, d.total - d.reviews.length)} 条（共 ${num(d.total)} 条）</button>` : "";
    return `<div class="rev-list">${d.reviews.map((r) => card(r, terms)).join("")}</div>${more}`;
  }

  function coverageMarkup() {
    const s = rev.summary, notes = s.coverage || {};
    const rows = s.platforms.map((p) => `<tr><td>${esc(p.label)}</td><td class="num">${num(p.total)}</td><td class="num">${num(p.with_text)}</td><td class="num">${p.avg_rating == null ? "—" : p.avg_rating}</td><td>${esc(p.first || "—")} 至 ${esc(p.last || "—")}</td><td class="rev-note">${esc(notes[p.platform] || "")}</td></tr>`).join("");
    return `<section class="rev-coverage section-card" id="rev-coverage" hidden><h2>各来源抓了多少、缺什么、哪些要小心用</h2><div class="perf-table-wrap"><table class="perf-table"><thead><tr><th>来源</th><th class="num">总数</th><th class="num">有文字</th><th class="num">平均星级</th><th>评论日期</th><th>说明</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="rev-muted">默认已隐藏：品牌/达人账号自己的回复、没提到产品的评论、只打星没写字的评价。标签是规则自动打的，可能有误判；星级为 1–3 的评价和标「具体缺陷」的评论最值得先读。</p></section>`;
  }

  // The two selects that only make sense when the library has something to choose from.
  function videoSelect() {
    const videos = rev.summary.videos;
    if (!videos.length && !rev.filters.video) return "";
    const known = videos.some((v) => v.video_id === rev.filters.video);
    const options = videos.map((v) => `<option value="${esc(v.video_id)}" ${v.video_id === rev.filters.video ? "selected" : ""}>${esc(clip(videoName(v, v.video_id), 34))} · ${num(v.comments)} 条</option>`);
    if (rev.filters.video && !known) options.unshift(`<option value="${esc(rev.filters.video)}" selected>视频 …${esc(rev.filters.video.slice(-6))}</option>`);
    return `<select id="rev-video" aria-label="按视频看评论"><option value="">全部视频（${videos.length} 条有评论）</option>${options.join("")}</select>`;
  }

  function skuSelect() {
    const skus = rev.summary.skus;
    if (!skus.length) return "";
    return `<select id="rev-sku" aria-label="购买的款式"><option value="">全部款式</option>${skus.map((k) => `<option value="${esc(k)}" ${k === rev.filters.sku ? "selected" : ""}>${esc(k)}</option>`).join("")}</select>`;
  }

  function render() {
    cancelSearch();
    const f = rev.filters, s = rev.summary, sources = s.platforms.filter((p) => p.total).length;
    const select = (id, label, options, value) => `<label class="rev-field"><span>${label}</span><select id="${id}">${options.map(([k, v]) => `<option value="${esc(k)}" ${String(k) === String(value) ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`;
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 用户声音";
    $("#content").innerHTML = `<div id="rev-page"><div class="page-heading rev-heading"><div><h1>用户声音</h1><p>${num(s.default_visible)} 条真实评论，来自 ${sources} 个来源${s.synced_at ? ` · 最近同步 ${esc(s.synced_at.slice(0, 16).replace("T", " "))}` : ""}。评论里的说法都未核实，不含评论者姓名。<button type="button" class="subtle rev-coverage-link" id="rev-coverage-toggle" aria-expanded="false" aria-controls="rev-coverage">数据来源说明</button></p></div><div class="perf-actions"><button type="button" class="subtle" id="rev-basket-toggle"></button><button type="button" class="secondary" id="rev-copy-all">${icon("copy")}复制当前结果</button><button type="button" id="rev-sync" hidden>重新同步</button></div></div>
      <div id="rev-sync-result" role="status"></div><p class="rev-records" id="rev-records"></p>${coverageMarkup()}
      <div class="rev-searchbar"><label class="search">${icon("search")}<input id="rev-q" aria-label="搜索评论" placeholder="搜索评论：英文原词，或夹具、晃动、价格等中文词" value="${esc(f.q)}"></label><button type="button" class="secondary rev-filter-open" id="rev-filter-open" aria-haspopup="dialog" aria-controls="rev-drawer"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>筛选<span class="rev-filter-count" id="rev-filter-count" hidden></span></button></div>
      <div class="rev-sources" id="rev-sources" role="group" aria-label="来源"></div>
      <div class="rev-quick" role="group" aria-label="快捷筛选"><button type="button" class="rev-quick-btn" data-quick="low">低星评价</button><button type="button" class="rev-quick-btn" data-quick="questions" id="rev-open-questions" title="TikTok 视频评论里，库里没有任何回复的提问：可以用真实画面回答的选题">没人回复的提问</button><button type="button" class="rev-quick-btn" id="rev-new" title="最近几次同步新增的评论"></button><button type="button" class="rev-quick-btn" data-quick="defects">具体缺陷</button></div>
      <p class="rev-query-note" id="rev-query-note" role="status"></p>
      <div class="rev-resultbar"><div class="rev-active" id="rev-active"></div><span class="result-count" id="rev-count"></span><label class="rev-sort"><span>排序</span><select id="rev-sort" aria-label="排序">${Object.entries(sorts).map(([k, v]) => `<option value="${k}" ${k === f.sort ? "selected" : ""}>${v}</option>`).join("")}</select></label></div>
      <div id="rev-video-bar"></div><div id="rev-results"></div>
      <dialog id="rev-drawer" class="rev-drawer" aria-labelledby="rev-drawer-title"><div class="rev-drawer-head"><h2 id="rev-drawer-title">筛选</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭筛选">${icon("close")}</button></div>
        <div class="rev-drawer-body"><div class="rev-field-grid">
          ${select("rev-rating", "星级", Object.entries(ratings), f.rating)}
          ${select("rev-role", "评论者", [["", "全部角色（不含品牌账号）"], ...Object.entries(s.labels.role)], f.role)}
          ${select("rev-language", "语言", Object.entries(languages), f.language)}
          ${select("rev-replied", "回复情况", [["", "不限"], ...Object.entries(repliedOptions).slice(1)], f.replied)}
          ${videoSelect() ? `<label class="rev-field"><span>视频</span>${videoSelect()}</label>` : ""}${skuSelect() ? `<label class="rev-field"><span>款式</span>${skuSelect()}</label>` : ""}
        </div><section class="rev-filters" id="rev-filters" aria-label="标签"></section>
        <fieldset class="rev-toggles"><legend>默认隐藏的内容</legend><label><input type="checkbox" id="rev-brand" ${f.brand ? "checked" : ""}> 品牌或达人账号的回复</label><label><input type="checkbox" id="rev-empty" ${f.empty ? "checked" : ""}> 只打星没写字的评价</label><label><input type="checkbox" id="rev-irrelevant" ${f.irrelevant ? "checked" : ""}> 没提到产品的评论</label></fieldset></div>
        <div class="rev-drawer-foot"><button type="button" class="subtle" id="rev-reset">清除全部</button><button type="button" class="primary" data-drawer-close id="rev-drawer-done">显示结果</button></div></dialog></div>`;
    renderedProduct = currentProduct();
    bindToolbar();
    const conversations = paintResults();
    paintNotes();
    return conversations;
  }

  // The buttons and notes that depend on the records: what is new, how many are picked, what was changed by hand.
  function paintNotes() {
    const s = rev.summary, f = rev.filters;
    const records = [];
    if (s.state_error) records.push(`<span class="data-warning">${esc(s.state_error)}。手动改标签和摘录夹暂时不能保存，已有的记录没有被覆盖。</span>`);
    if (s.tag_edits) records.push(`手动改过 ${num(s.tag_edits)} 处标签 · <button type="button" class="subtle" id="rev-copy-edits" title="规则打错的地方，用来调整规则">复制纠错清单</button>`);
    $("#rev-records").innerHTML = records.join(" ");
    const recent = $("#rev-new");
    recent.hidden = !(s.new_recent > 0 || f.since);
    recent.innerHTML = `近 ${s.new_days || 14} 天新增<span>${num(s.new_recent)}</span>`;
    recent.setAttribute("aria-pressed", String(Boolean(f.since)));
    const basket = $("#rev-basket-toggle");
    basket.innerHTML = `${icon("folder")}摘录篮`;
    basket.setAttribute("aria-haspopup", "dialog");
  }

  function paintResults() {
    const conversations = [];
    const active = activeFilters(), inDrawer = active.filter(([key]) => drawerKeys.has(key)).length;
    $("#rev-sources").innerHTML = sourceBar();
    $("#rev-active").innerHTML = activeMarkup(active);
    $("#rev-filter-count").hidden = !inDrawer;
    $("#rev-filter-count").textContent = inDrawer;
    $("#rev-drawer-done").textContent = `显示 ${num(rev.data.total)} 条结果`;
    paintQuick();
    $("#rev-filters").innerHTML = filterRows();
    $("#rev-video-bar").innerHTML = videoBar();
    $("#rev-results").innerHTML = resultsMarkup();
    $("#rev-count").textContent = `${num(rev.data.total)} 条`;
    $("#rev-query-note").innerHTML = queryNote(rev.data);
    $("#rev-more")?.addEventListener("click", () => { cancelSearch(); rev.shown += 50; refresh(); });
    // A repaint (more results, another filter) replaces the cards; conversations that were open stay open.
    for (const id of rev.open) {
      const box = document.getElementById(`rev-thread-${id}`);
      if (box && rev.threads[id]) { box.innerHTML = threadMarkup(rev.threads[id]); box.hidden = false; }
      else if (box) {
        const button = $("[data-thread]", box.closest(".rev-card"));
        if (button) conversations.push(toggleThread(button, true));
      }
    }
    return conversations;
  }

  async function toggleThread(button, restore = false) {
    const id = button.dataset.thread, box = document.getElementById(`rev-thread-${id}`);
    if (!box) return;
    if (rev.open.has(id) && !restore) {
      rev.open.delete(id);
      box.hidden = true;
      button.setAttribute("aria-expanded", "false");
      return;
    }
    rev.open.add(id);
    box.hidden = false;
    button.setAttribute("aria-expanded", "true");
    if (!rev.threads[id]) {
      box.innerHTML = '<p class="rev-muted">正在读取对话…</p>';
      try {
        rev.threadLoading[id] ||= api(`/api/reviews/${id}`).then(data => {
          rev.threads[id] = data.review.thread || [];
        }).finally(() => { delete rev.threadLoading[id]; });
        await rev.threadLoading[id];
      } catch (error) {
        rev.open.delete(id);
        box.innerHTML = `<div class="data-warning">${esc(error.message)}</div>`;
        button.setAttribute("aria-expanded", "false");
        return;
      }
    }
    if (rev.open.has(id)) box.innerHTML = threadMarkup(rev.threads[id]);
  }

  async function refresh() {
    prepareSearchRefresh();
    const hash = pageHash();
    try {
      if (await loadResults()) paintResults();
    } catch (error) {
      if (pageHash() !== hash || !$("#rev-results")) return;
      $("#rev-results").innerHTML = `<div class="data-warning">${esc(error.message)}</div>`;
    }
  }

  // After the records changed: the counts in the summary and the facets of the list are read again.
  async function refreshAll() {
    prepareSearchRefresh();
    const hash = pageHash();
    try {
      const [,loaded] = await Promise.all([loadSummary(), loadResults()]);
      if (!loaded || pageHash() !== hash || !$("#rev-results")) return;
      paintResults();
      paintNotes();
    } catch (error) {
      toast(error.message);
    }
  }

  // ---- tags changed by hand ----
  async function editTag(button) {
    const { tagEdit: id, kind, code, on } = button.dataset;
    button.disabled = true;
    try {
      await api(`/api/reviews/${id}/tag`, { method: "POST", body: { kind, tag: code, present: on !== "true" } });
    } catch (error) {
      toast(error.message);
      button.disabled = false;
      return;
    }
    await refreshAll();
  }

  // ---- quotes picked for a script ----
  async function setPick(id, picked) {
    try {
      await api(`/api/reviews/${id}/pick`, { method: "POST", body: { picked } });
      window.dispatchEvent(new CustomEvent("workbench:basketchange", { detail: { source: "reviews" } }));
    } catch (error) {
      toast(error.message);
      return;
    }
    await refreshAll();
  }

  async function copyEdits() {
    try {
      const { edits } = await api("/api/reviews/tag-edits");
      const lines = ["# 评论标签纠错清单（规则打错的地方，供调整规则用）", ""];
      const kinds = { nature: "性质", topic: "主题", competitor: "竞品" };
      for (const e of edits) lines.push(`- ${e.present ? "加上" : "去掉"}「${e.label}」（${kinds[e.kind] || e.kind}）｜${labelOf("platform", e.platform)}｜编号 ${e.id}｜${e.text.replace(/\s+/g, " ").slice(0, 120)}`);
      copyText(lines.join(NL), `已复制 ${edits.length} 处纠错`);
    } catch (error) {
      toast(error.message);
    }
  }

  function reviewQuote(r) {
    const head = [labelOf("platform", r.platform), labelOf("role", r.role), r.rating != null ? `${r.rating}星` : null, r.posted_on].filter(Boolean).join("｜");
    return `[${head}] ${r.title ? r.title + "｜" : ""}${r.text}${r.url ? `（${r.url}）` : ""}`;
  }

  function filterSummaryText() {
    const f = rev.filters, parts = [];
    if (currentProduct()) parts.push("产品：" + currentProduct());
    if (f.platform.length) parts.push("来源：" + f.platform.map((p) => labelOf("platform", p)).join("、"));
    if (f.role) parts.push("角色：" + labelOf("role", f.role));
    if (f.rating) parts.push("星级：" + ratings[f.rating]);
    if (f.replied) parts.push("回复情况：" + repliedOptions[f.replied]);
    if (f.video) parts.push("视频：" + videoName(rev.data.videos?.[f.video], f.video));
    if (f.sku) parts.push("款式：" + f.sku);
    if (f.since) parts.push("新增自：" + f.since);
    if (f.nature.length) parts.push("性质：" + f.nature.map((t) => labelOf("nature", t)).join("、"));
    if (f.topic.length) parts.push("主题：" + f.topic.map((t) => labelOf("topic", t)).join("、"));
    if (f.competitor.length) parts.push("竞品：" + f.competitor.map((t) => labelOf("competitor", t)).join("、"));
    if (f.q.trim()) parts.push("关键词：" + f.q.trim());
    return parts.length ? parts.join("；") : "无筛选（默认可见评论）";
  }

  // Any change of the filters: back to the first page of results, remembered, and shown in the address.
  function changed(next) {
    cancelSearch();
    Object.assign(rev.filters, next);
    if ("replied" in next && $("#rev-replied")) $("#rev-replied").value = rev.filters.replied;
    rev.shown = 50;
    save();
    refresh();
  }

  function bindToolbar() {
    const field = $("#rev-q");
    let composing = false;
    const isCurrentField = () => field.isConnected && $("#rev-q") === field && pageHash().startsWith("#reviews");
    const submit = () => {
      cancelSearch();
      if (composing || !isCurrentField()) return;
      rev.filters.q = field.value;
      rev.shown = 50;
      const query = params().toString();
      save();
      if (query === submittedSearch || query === rev.resultQuery) return;
      void refresh();
    };
    const queue = (inputComposing = false) => {
      if (!isCurrentField()) return;
      rev.filters.q = field.value;
      ++rev.request;
      submittedSearch = null;
      cancelSearch();
      if (composing || inputComposing) return;
      searchField = field;
      searchHash = pageHash();
      searchTimer = setTimeout(submit,150);
    };
    field.addEventListener("input", event => queue(event.isComposing));
    field.addEventListener("compositionstart", () => {
      composing = true;
      ++rev.request;
      submittedSearch = null;
      cancelSearch();
    });
    field.addEventListener("compositionend", () => {composing = false;queue();});
    field.addEventListener("keydown", event => {
      if (event.key !== "Enter" || composing || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      submit();
    });
    [["#rev-rating", "rating"], ["#rev-role", "role"], ["#rev-language", "language"], ["#rev-replied", "replied"], ["#rev-video", "video"], ["#rev-sku", "sku"], ["#rev-sort", "sort"]].forEach(([selector, key]) =>
      $(selector)?.addEventListener("change", (event) => changed({ [key]: event.target.value })));
    [["#rev-brand", "brand"], ["#rev-empty", "empty"], ["#rev-irrelevant", "irrelevant"]].forEach(([selector, key]) =>
      $(selector).addEventListener("change", (event) => changed({ [key]: event.target.checked })));
    // Clearing rebuilds the controls; the drawer stays open when it was.
    const reset = () => { cancelSearch();const open = $("#rev-drawer").open; rev.filters = fresh(); rev.shown = 50; save(); render(); refresh(); if (open) $("#rev-drawer").showModal(); };
    $("#rev-reset").onclick = reset;
    // Quick filters, e.g. the unanswered TikTok questions: things viewers asked that nobody replied to.
    $$("[data-quick]").forEach((button) => button.onclick = () => { const item = quick[button.dataset.quick]; changed(item.set(!item.on(rev.filters), rev.filters)); });
    $("#rev-new").onclick = () => { changed({ since: rev.filters.since ? "" : rev.summary.new_since }); paintNotes(); };
    const drawer = $("#rev-drawer");
    $("#rev-filter-open").onclick = () => drawer.showModal();
    drawer.addEventListener("click", (event) => { if (event.target === drawer || event.target.closest("[data-drawer-close]")) drawer.close(); });
    $("#rev-coverage-toggle").onclick = (event) => {
      const box = $("#rev-coverage"), open = box.hidden;
      box.hidden = !open;
      event.currentTarget.setAttribute("aria-expanded", String(open));
    };
    $("#rev-basket-toggle").onclick = () => window.MaterialBasket?.open();
    $("#rev-sync").onclick = async () => {
      prepareSearchRefresh();
      const hash = pageHash(), button = $("#rev-sync"), box = $("#rev-sync-result");
      button.disabled = true;
      box.className = "context-note";
      box.textContent = "正在同步…";
      try {
        const result = await api("/api/reviews/sync", { method: "POST", body: {} });
        const [,loaded] = await Promise.all([loadSummary(),loadResults()]);
        if (!loaded || pageHash() !== hash) return;
        render();
        const note = $("#rev-sync-result");
        note.className = result.failed.length || result.missing_sources.length || result.state_error ? "data-warning" : "context-note";
        note.innerHTML = `已重新读取 ${result.files} 个文件，共 ${num(result.reviews)} 条评论${result.added ? `，其中新增 ${num(result.added)} 条（点「近 ${rev.summary.new_days || 14} 天新增」查看）` : ""}。` + result.failed.map((f) => `<br>未导入 ${esc(f.file)}：${esc(f.error)}`).join("") + result.missing_sources.map((p) => `<br>找不到文件夹：${esc(p)}`).join("") + (result.state_error ? `<br>${esc(result.state_error)}` : "");
      } catch (error) {
        box.className = "data-warning";
        box.textContent = error.message;
      } finally {
        button.disabled = false;
      }
    };
    $("#rev-copy-all").onclick = () => {
      const d = rev.data;
      copyText([`# 用户声音摘录（${filterSummaryText()}）`, `共 ${d.total} 条，下面是其中 ${d.reviews.length} 条。评论里的说法均未核实，评论者不一定是买家。`, "", ...d.reviews.map((r) => "- " + reviewQuote(r).replace(/\n+/g, " "))].join(NL), `已复制 ${d.reviews.length} 条`);
    };
    // chips, copy, expand and the rest are delegated to this render's own wrapper, so repeated renders never stack listeners
    $("#rev-page").addEventListener("click", (event) => {
      const target = event.target;
      const menu = target.closest(".rev-menu");
      if (menu && target.closest("[role=menuitem]")) menu.open = false;
      if (target.closest("[data-platform-all]")) { changed({ platform: [] }); return; }
      const platform = target.closest("[data-chip='platform']");
      if (platform) { const code = platform.dataset.code; changed({ platform: rev.filters.platform.length === 1 && rev.filters.platform[0] === code ? [] : [code] }); return; }
      if (target.closest("[data-reset]")) { reset(); return; }
      const clear = target.closest("[data-clear]");
      if (clear) {
        const key = clear.dataset.clear, code = clear.dataset.code;
        if (listKeys.includes(key)) changed({ [key]: rev.filters[key].filter((c) => c !== code) });
        else if (flagKeys.includes(key)) changed({ [key]: false });
        else { changed({ [key]: "" }); if (key === "q") $("#rev-q").value = ""; }
        const control = { rating: "#rev-rating", role: "#rev-role", language: "#rev-language", replied: "#rev-replied", video: "#rev-video", sku: "#rev-sku" }[key];
        if (control && $(control)) $(control).value = "";
        if (flagKeys.includes(key)) $(`#rev-${key}`).checked = false;
        if (key === "since") paintNotes();
        return;
      }
      const chipButton = target.closest("[data-chip]");
      if (chipButton) {
        const kind = chipButton.dataset.chip, code = chipButton.dataset.code, list = rev.filters[kind];
        changed({ [kind]: list.includes(code) ? list.filter((c) => c !== code) : [...list, code] });
        return;
      }
      const videoButton = target.closest("[data-video]");
      if (videoButton) { changed({ video: videoButton.dataset.video }); if ($("#rev-video")) $("#rev-video").value = videoButton.dataset.video; window.scrollTo({ top: 0 }); return; }
      if (target.closest("#rev-video-clear")) { changed({ video: "" }); if ($("#rev-video")) $("#rev-video").value = ""; return; }
      const thread = target.closest("[data-thread]");
      if (thread) { toggleThread(thread); return; }
      const edit = target.closest("[data-edit-tags]");
      if (edit) {
        const id = edit.dataset.editTags, box = document.getElementById(`rev-tags-${id}`), item = rev.data.reviews.find((r) => r.id === id);
        if (rev.editing.has(id)) rev.editing.delete(id); else rev.editing.add(id);
        box.hidden = !rev.editing.has(id);
        box.innerHTML = rev.editing.has(id) && item ? tagEditor(item) : "";
        edit.setAttribute("aria-expanded", String(rev.editing.has(id)));
        return;
      }
      const tagButton = target.closest("[data-tag-edit]");
      if (tagButton) { editTag(tagButton); return; }
      const pick = target.closest("[data-pick]");
      if (pick) { setPick(pick.dataset.pick, pick.getAttribute("aria-pressed") !== "true"); return; }
      const theme = target.closest("[data-review-theme]");
      if (theme) { const review = rev.data.reviews.find(item => item.id === theme.dataset.reviewTheme); if (review) void window.MaterialBasket?.openTheme(review); return; }
      if (target.closest("#rev-copy-edits")) { copyEdits(); return; }
      const copy = target.closest("[data-copy]");
      if (copy) {
        const item = rev.data.reviews.find((r) => r.id === copy.dataset.copy);
        if (item) copyText(reviewQuote(item), "已复制");
        return;
      }
      const expand = target.closest("[data-expand]");
      if (expand) {
        const body = expand.closest(".rev-body");
        body.classList.toggle("is-open");
        expand.textContent = body.classList.contains("is-open") ? "收起" : "展开全文";
      }
    });

  }

  const closeMenus = (event) => { document.querySelectorAll(".rev-menu[open]").forEach((menu) => { if (event.key === "Escape" || !menu.contains(event.target)) menu.open = false; }); };
  window.addEventListener("click", closeMenus, true);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenus(event); });

  let rendered = false, renderedProduct = null, filterProduct = null, routeSequence = 0;
  window.addEventListener("hashchange", cancelSearch);
  window.addEventListener("workbench:basketchange", event => {
    if (event.detail?.source === "global" && pageHash().startsWith("#reviews") && $("#rev-results")) void refreshAll().catch(error => toast(error.message));
  });
  function routeFilters(product, summary = rev.summary) {
    const linked = filtersFromHash();
    // Product switches remove SKU/video from the URL. Do not restore these product-specific
    // filters from the previous page, including while its result request is still pending.
    const saved = filterProduct !== null && filterProduct !== product ? { ...rev.filters, sku: "", video: "" } : rev.filters;
    const source = linked || saved;
    return sanitize({...source,...Object.fromEntries(listKeys.map(key => [key,Array.isArray(source[key]) ? [...source[key]] : []]))},summary);
  }
  async function route() {
    const hash = pageHash(), product = currentProduct(), sequence = ++routeSequence;
    const isCurrent = () => sequence === routeSequence && hash === pageHash();
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 用户声音";
    // A background route check must leave the current draft to its scheduled search.
    if (searchTimer !== null && searchField?.isConnected && searchHash === hash && product === renderedProduct) return;
    const restored = window.WorkbenchShell?.listState?.("reviews", product);
    if (restored?.filters) {
      cancelSearch();
      rev.filters = fresh();
      for (const key of Object.keys(defaults)) {
        if (typeof restored.filters[key] === typeof defaults[key]) rev.filters[key] = restored.filters[key];
      }
      rev.shown = Number.isFinite(restored.shown) ? Math.max(50, Math.trunc(restored.shown)) : 50;
      rev.open = new Set(Array.isArray(restored.open) ? restored.open.filter(id => typeof id === "string") : []);
      rev.editing.clear();
      filterProduct = product;
    }
    // Product context is owned by the shell. Saved filters only control this page's other dimensions.
    if (rendered && hash.startsWith("#reviews") && $("#rev-results")) {
      const next = routeFilters(product);
      if (!restored && product === renderedProduct && hashQuery(next) === hashQuery()) return;
      rev.filters = next;
      filterProduct = product;
      if (!restored) rev.shown = 50;
      try {
        if (!await loadResults() || !isCurrent()) return;
        const conversations = render();
        if (restored) {
          await Promise.all(conversations);
          if (!isCurrent()) return;
        }
        renderedProduct = product;
        save();
      } catch (error) {if (isCurrent()) toast(error.message);}
      return;
    }
    rev.editing.clear();
    $("#content").innerHTML = skeletonMarkup(3);
    try {
      const linked = filtersFromHash();
      // Validate the fixed choices immediately; label/SKU validation follows the fresh summary.
      // Valid saved filters can fetch the list and its summary at the same time.
      rev.filters = routeFilters(product,null);
      filterProduct = product;
      if (linked && !restored) rev.shown = 50;
      const requested = params().toString();
      const initialRead = loadResults().then(loaded => ({loaded}),error => ({error}));
      const [,initial] = await Promise.all([loadSummary(),initialRead]);
      if (!isCurrent()) return;
      rev.filters = routeFilters(product);
      // Unknown catalog filters are discarded as before, with one corrective read only when needed.
      if (params().toString() !== requested) {
        if (!await loadResults() || !isCurrent()) return;
      } else {
        if (initial.error) throw initial.error;
        if (!initial.loaded) return;
      }
      const conversations = render();
      if (restored) {
        await Promise.all(conversations);
        if (!isCurrent()) return;
      }
      rendered = true;
      renderedProduct = product;
      save();
    } catch (error) {
      if (isCurrent()) $("#content").innerHTML = `<div class="empty">${icon("chat")}<h3>用户声音暂不可用</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  // Case detail: a short pointer to the library for 202H cases, with the comments under the case's own video.
  async function decorateCase(item) {
    const section = $("#detail-section-performance");
    if (!section || !/202H/i.test(`${item.product || ""} ${item.current?.title || ""}`) || $("#case-review-box")) return;
    try {
      if (!rev.summary) await loadSummary();
    } catch {
      return;
    }
    if (state.detail?.case_id !== item.case_id || $("#case-review-box")) return;
    const s = rev.summary, parts = s.platforms.filter((p) => p.with_text).map((p) => `${esc(p.label)} ${num(p.with_text - p.brand - p.irrelevant)}`).join(" · ");
    const videoId = String(item.current?.metrics?.video_id || "").trim();
    const own = s.videos.find((v) => v.video_id === videoId);
    const ownLine = own ? `<p>这条案例的视频下有 ${num(own.comments)} 条评论${own.open_questions ? `，其中 ${num(own.open_questions)} 个提问没人回复` : ""}。<a href="#reviews?video=${esc(own.video_id)}">只看这条视频的评论</a></p>` : "";
    const box = document.createElement("div");
    box.id = "case-review-box";
    box.className = "perf-case-box";
    box.innerHTML = `<div><strong>用户声音</strong><p>${num(s.default_visible)} 条真实评论可查：${parts}。评论里的说法均未核实。</p>${ownLine}</div><a class="secondary" href="#reviews">打开用户声音</a>`;
    section.append(box);
  }

  window.CaseReviews = { route, decorateCase, captureListState, leaveList };
})();
