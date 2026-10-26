"use strict";

// TikTok Shop sales data (带货数据): per shop (本土店 / 跨境店), the video exports, photo exports, follow-up status and case links.
(() => {
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const currentProduct = () => window.WorkbenchShell ? window.WorkbenchShell.product() : perf.filters.product;
  const productMatches = value => window.WorkbenchShell ? window.WorkbenchShell.matchesProduct(value) : !currentProduct() || String(value || "").toLowerCase().includes(currentProduct().toLowerCase());
  const statusOrder = ["high", "medium", "weak", "none", "unclassified", "pending"];
  const statusNames = {high:"高参考价值",medium:"中等参考价值",weak:"弱参考价值",none:"无参考价值",unclassified:"待分类",pending:"待复盘"};
  const statusHints = {
    high:"人工复盘后归为高参考价值", medium:"人工复盘后归为中等参考价值", weak:"人工复盘后归为弱参考价值",
    none:"人工复盘后归为无参考价值，仅留档", unclassified:"已完成人工复盘，尚未选择参考价值", pending:"尚未完成人工复盘",
  };
  const reviewStatus = video => {
    const key = video?.review_status?.key;
    return statusOrder.includes(key) ? {key,label:statusNames[key]} : {key:"pending",label:statusNames.pending};
  };
  // Sortable table columns: [header label, sort value (null sorts last), first-click direction].
  const sortColumns = {
    posted: ["发布", (v) => v.posted_at || null, "desc"],
    window: ["首两周播放", (v) => v.window_views, "desc"],
    status: ["参考价值", (v) => statusOrder.indexOf(reviewStatus(v).key), "asc"],
    views: ["累计播放", (v) => v.views, "desc"],
    items: ["售出", (v) => v.items, "desc"],
    gmv: ["GMV", (v) => v.gmv || 0, "desc"],
    ctor: ["CTOR", (v) => v.ctor_pct, "desc"],
    week: ["本周 GMV", (v) => v.this_week_gmv || 0, "desc"],
    likes: ["点赞", (v) => v.likes, "desc"],
    comments: ["评论", (v) => v.comments, "desc"],
    shares: ["转发", (v) => v.shares, "desc"],
    saves: ["收藏", (v) => v.saves, "desc"],
    reviews: ["评论库已收录", (v) => v.review_comments || 0, "desc"],
  };
  const tabs = [["board", "数据看板"], ["list", "视频明细"], ["photos", "图文"], ["creators", "海外达人"]];
  // The shop is the first level: every read below asks for one shop, and switching it reloads them all.
  const shopNames = { local: "本土店", cross: "跨境店" };
  const filterDefaults = { shop: "local", q: "", account: "", product: "", status: "", campaign: "", board_campaign: "", sort: "posted", dir: "desc", tab: "board", period: "all", list_period: "all", board_period: "all", metric: "gmv", ...window.PerfCreators.defaults };
  const perf = {
    videos: null, summary: null, analytics: null, catalog: null, loading: null, coreLoading: null, analyticsReady: null, catalogReady: null, loadSequence: 0, routeSequence: 0, analyticsLoading: false, catalogLoading: false, shown: 200,
    creators: null, creatorsLoading: null, creatorsError: "", creatorsSequence: 0, creatorShown: 100,
    photos: null, photoItems: null, photoItemsState: "", photosLoading: null, photosError: "", photosSequence: 0, requestedShop: "",
    filters: { ...filterDefaults },
    openVideo: "", renderedProduct: null,
  };
  try {
    const saved = JSON.parse(localStorage.getItem("workbench-video-data-filters") || "{}");
    Object.assign(perf.filters, saved);
    if (saved.review_status_filter_version !== 1) perf.filters.status = "";
    if (!Object.hasOwn(saved, "list_period") && saved.tab === "list") perf.filters.list_period = saved.period || "all";
    if (!Object.hasOwn(saved, "board_period") && saved.tab !== "list") perf.filters.board_period = saved.period || "all";
  } catch {
    /* Filters fall back to defaults without browser storage. */
  }
  if (!(perf.filters.shop in shopNames)) perf.filters.shop = "local";
  const shopQuery = () => `shop=${perf.filters.shop}`;
  // Everything read for one shop is dropped when another is shown; a read still on its way for the old shop is ignored.
  function resetData() {
    Object.assign(perf, {
      videos: null, summary: null, analytics: null, catalog: null, creators: null, creatorsLoading: null, creatorsError: "",
      photos: null, photoItems: null, photoItemsState: "", photosLoading: null, photosError: "", requestedShop: "",
      creatorsSequence: perf.creatorsSequence + 1, photosSequence: perf.photosSequence + 1, loadSequence: perf.loadSequence + 1, loading: null,
    });
  }
  let searchTimer = null;
  function cancelSearch() {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  function captureListState() {
    if (!$("#perf-board") && !$("#perf-results") && !$("#perf-creators") && !$("#perf-photos")) return null;
    const filters = { ...perf.filters };
    const field = $("#perf-q");
    if (field) filters.q = field.value;
    return { product: perf.renderedProduct, filters, review_status_filter_version:1, shown: perf.shown, hash: filterURL(filters) };
  }
  function filterURL(filters, preserveDetails = false) {
    const query = new URLSearchParams();
    const campaignKey = filters.tab === "list" ? "campaign" : "board_campaign";
    for (const key of Object.keys(filterDefaults)) {
      if (["product", "list_period", "board_period"].includes(key) || (["campaign", "board_campaign"].includes(key) && key !== campaignKey)) continue;
      if (filters[key] && filters[key] !== filterDefaults[key]) query.set(key === "status" ? "review_status" : key, filters[key]);
    }
    if (preserveDetails) {
      const linked = new URLSearchParams(pageHash().split("?")[1] || "");
      for (const key of ["video", "feedback_window"]) if (linked.has(key)) query.set(key, linked.get(key));
    }
    return pageURL(`#videos${query.size ? `?${query}` : ""}`);
  }
  function syncFilterURL(preserveDetails = false) {
    if (!pageHash().startsWith("#videos")) return;
    const next = filterURL(perf.filters, preserveDetails);
    if (next !== location.hash) history.replaceState(null, "", next);
  }

  const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US"));
  const count = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null ? "—" : v.toFixed(2) + "%");
  const badge = (v) => {const status = reviewStatus(v);return `<span class="perf-status perf-review-${status.key}" title="${esc(statusHints[status.key])}">${esc(status.label)}</span>`;};
  const videoStatusMarkup = (v) => `<section class="perf-video-status"><h3>复盘</h3>${badge(v)}</section>`;
  function load(force = false) {
    if (perf.loading && !force) return perf.loading;
    if (force) Object.assign(perf, { creators: null, creatorsLoading: null, creatorsError: "", creatorsSequence: perf.creatorsSequence + 1,
      photos: null, photoItems: null, photoItemsState: "", photosLoading: null, photosError: "", photosSequence: perf.photosSequence + 1 });
    const sequence = ++perf.loadSequence, query = shopQuery();
    let publicSequence = publicRefreshSequence;
    perf.requestedShop = perf.filters.shop;
    perf.analyticsLoading = true;
    perf.catalogLoading = true;
    // Sales reads share a server lock. Finish the table's reads before giving charts that lock.
    const core = Promise.all([api(`/api/performance/summary?${query}`), api(`/api/performance/videos?${query}`)]).then(async ([summary, data]) => {
      // A plugin push can arrive while the first table read is still in flight.
      while (sequence === perf.loadSequence && publicSequence !== publicRefreshSequence) {
        publicSequence = publicRefreshSequence;
        data = await api(`/api/performance/videos?${query}`, {cache:'no-store'});
      }
      if (sequence !== perf.loadSequence) return;
      perf.summary = summary;
      perf.videos = data.videos;
      window.WorkbenchShell?.registerProducts(perf.videos.map(video => video.product));
    });
    const supplements = core.then(async () => {
      // The route renders the table/toolbar in the same turn; yield before optional requests begin.
      await new Promise(resolve => setTimeout(resolve,0));
      return sequence === perf.loadSequence && pageHash().startsWith("#videos");
    });
    const analytics = supplements.then(active => active ? api(`/api/performance/analytics?${query}`).catch(() => null) : undefined).then(data => {
      if (sequence !== perf.loadSequence) return;
      if (data !== undefined) perf.analytics = data;
      perf.analyticsLoading = false;
    });
    const catalog = supplements.then(active => active ? window.CaseDirectionPicker.fetchCatalog().catch(() => null) : undefined).then(data => {
      if (sequence !== perf.loadSequence) return;
      if (data !== undefined) perf.catalog = data;
      perf.catalogLoading = false;
    });
    perf.coreLoading = core;
    perf.analyticsReady = analytics;
    perf.catalogReady = catalog;
    const loading = Promise.all([core,analytics,catalog]).finally(() => { if (perf.loading === loading) perf.loading = null; });
    perf.loading = loading;
    return loading;
  }

  // ignoreStatus: the status chart shows every status within the other filters, so choosing one keeps the rest visible.
  // The shell supplies the product scope; account, time and status remain page filters.
  // Export rows with no title and nothing recorded, usually deleted posts; hidden unless searched by ID.
  const emptyRow = (v) => !String(v.title || "").trim() && !(v.views > 0) && !(v.orders > 0) && !(v.gmv > 0);
  const campaignPending = () => perf.filters.tab === "list" && perf.filters.campaign && !window.Campaigns?.current();
  // All views share the activity, while the video list applies it to observation windows.
  const periodCampaignPending = () => perf.filters.tab !== "list" && perf.filters.board_campaign && !window.Campaigns?.current();
  const campaignLoadingMarkup = () => `<p class="context-note" role="status">${perf.campaignsError ? "活动日历暂时无法读取，请重试或清除筛选。" : "正在读取所选活动期…"}</p>`;
  const resultCount = (rows) => {
    if (campaignPending()) return perf.campaignsError ? "活动读取失败" : "正在读取活动…";
    const hidden = perf.hiddenEmpty || 0;
    return `${rows.length} 条${hidden ? `<small title="没有标题、播放和出单都为 0 的导出行，多为已删除的视频；搜索视频 ID 仍可找到"> · 已隐藏 ${hidden} 条空视频</small>` : ""}`;
  };
  function filtered(ignoreStatus = false, ignoreProduct = false) {
    const f = perf.filters, range = PerfDashboard.rangeOf(f.period);
    const campaignId = f.tab === "list" ? f.campaign : "";
    const campaign = window.Campaigns?.current()?.campaigns.find(row => row.id === campaignId);
    const q = f.q.trim().toLowerCase();
    let hidden = 0;
    const rows = perf.videos.filter((v) =>
      (!range || ((v.posted_on || "") >= range[0] && (v.posted_on || "") <= range[1])) &&
      (!campaignId || (campaign && v.posted_on && v.window_end && v.posted_on <= campaign.end && v.window_end >= campaign.start)) &&
      (!f.account || v.account === f.account) &&
      (ignoreProduct || productMatches(v.product)) &&
      (ignoreStatus || !f.status || reviewStatus(v).key === f.status) &&
      (!q || `${v.title} ${v.video_id}`.toLowerCase().includes(q)) &&
      (!emptyRow(v) || (q && v.video_id.includes(q)) || !++hidden));
    if (!ignoreStatus && !ignoreProduct) perf.hiddenEmpty = hidden;
    const key = (sortColumns[f.sort] || sortColumns.posted)[1], sign = f.dir === "asc" ? 1 : -1;
    // Rows without a value stay at the bottom in either direction; ties fall back to newest first.
    return rows.sort((a, b) => {
      const x = key(a), y = key(b);
      if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1;
      return x > y ? sign : x < y ? -sign : (b.posted_at || "").localeCompare(a.posted_at || "");
    });
  }

  function options(values, selected, all) {
    return `<option value="">${all}</option>` + values.map((v) => `<option value="${esc(v)}" ${v === selected ? "selected" : ""}>${esc(v)}</option>`).join("");
  }

  // Human review classification across the videos matching every other filter.
  function statusMarkup(rows) {
    if (campaignPending()) return "";
    // Manual reference values first, followed by reviewed-but-unclassified and pending reviews.
    const tally = statusOrder.map((k) => [k, rows.filter((v) => reviewStatus(v).key === k).length]);
    const total = rows.length, active = perf.filters.status;
    const share = (n) => (total ? `${Math.round((n / total) * 100)}%` : "—");
    const segments = tally.filter(([, n]) => n).map(([k, n]) => `<button type="button" class="perf-seg perf-review-swatch-${k}${active && active !== k ? " is-muted" : ""}" data-status-filter="${k}" data-count="${n}" title="${esc(statusNames[k])} ${n} 条（${share(n)}）：${esc(statusHints[k])}" aria-label="只看${esc(statusNames[k])}，${n} 条"></button>`).join("");
    const legend = tally.map(([k, n]) => `<button type="button" class="perf-legend-item${active === k ? " is-active" : ""}${active && active !== k ? " is-muted" : ""}" data-status-filter="${k}" aria-pressed="${active === k}" title="${esc(statusHints[k])}"><i class="perf-swatch perf-review-swatch-${k}"></i><span>${esc(statusNames[k])}</span><strong>${n}</strong><small>${share(n)}</small></button>`).join("");
    const batch = batchCandidates(filtered()).length;
    return `<section class="section-card perf-status-card"><div class="perf-card-head"><h2>参考价值</h2><span>当前筛选 ${total} 条视频${active ? ` · 表格只显示「${esc(statusNames[active])}」，再点一次取消` : " · 点击状态筛选表格"}</span>${batch ? `<button type="button" class="secondary compact perf-batch-collect" data-batch-collect title="当前筛选里还没收为案例、订单最多的 ${batch} 条视频，保存后自动下载原视频并拆解">批量收为案例 · ${batch} 条</button>` : ""}</div>
      ${total ? `<div class="perf-status-bar" role="group" aria-label="参考价值分布">${segments}</div>` : ""}<div class="perf-status-legend">${legend}</div></section>`;
  }

  // Uncollected videos with orders in the current filter, most orders first; at most 10 per batch.
  function batchCandidates(rows) {
    return rows.filter((v) => !v.case_id && (v.orders || 0) > 0).sort((a, b) => (b.orders || 0) - (a.orders || 0)).slice(0, 10);
  }

  async function batchCollect(button) {
    const picks = batchCandidates(filtered());
    if (!picks.length || !window.CaseAutoBreakdown) return;
    const list = picks.map((v) => `${v.orders} 单 · ${v.headline || v.video_id}`).join("\n");
    if (!window.confirm(`把这 ${picks.length} 条视频收为案例，并自动下载原视频、排队拆解？\n\n${list}`)) return;
    button.disabled = true;
    try {
      const results = await window.CaseAutoBreakdown.batchCollect(picks);
      const queued = results.filter((r) => r.status === "queued").length, failed = results.filter((r) => r.status === "failed");
      toast(`已收为案例 ${queued} 条，正在后台下载和拆解，进度见任务面板。${failed.length ? `另有 ${failed.length} 条没收成：${failed[0].message}` : ""}`, !!failed.length);
      await load(true);
      render();
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
    }
  }

  function tableMarkup(rows) {
    if (campaignPending()) return `<div class="empty"><p role="status">${perf.campaignsError ? "活动日历暂时无法读取，请重试或清除筛选。" : "正在读取所选活动期…"}</p></div>`;
    if (!rows.length) return `<div class="empty">${icon("search")}<h3>没有符合条件的视频</h3><p>换个筛选条件，或先同步最新的后台导出。</p></div>`;
    const visible = rows.slice(0, perf.shown);
    const th = (key, cls = "") => {
      const active = perf.filters.sort === key, asc = perf.filters.dir === "asc", label = sortColumns[key][0];
      return `<th class="${cls}" aria-sort="${active ? (asc ? "ascending" : "descending") : "none"}"><button type="button" class="perf-sort${active ? " is-active" : ""}" data-sort="${key}" title="按${esc(label)}排序">${esc(label)}<span class="perf-sort-arrow" aria-hidden="true">${active ? (asc ? "↑" : "↓") : "↕"}</span></button></th>`;
    };
    return `<div class="perf-table-wrap"><table class="perf-table perf-video-table"><thead><tr>${th("posted", "perf-col-video")}<th>测试方向</th>${th("status")}${th("window", "num")}${th("views", "num")}${th("items", "num")}${th("gmv", "num")}${th("ctor", "num")}${th("likes", "num")}${th("comments", "num")}${th("shares", "num")}${th("saves", "num")}</tr></thead><tbody>${visible.map((v) => `<tr data-video-row="${esc(v.video_id)}" tabindex="0" aria-label="查看 ${esc(v.headline || v.video_id)} 的详情"${perf.openVideo === v.video_id ? ' class="is-open"' : ""}>
      <td class="perf-title" title="${esc(v.title || "")}"><strong>${esc(v.headline || v.video_id)}</strong><small>${esc(v.account || "账号未记录")} · ${esc(v.posted_on || "—")}${v.case_id ? " · 已收为案例" : ""}${v.review_comments ? ` · 评论库已收录 ${count(v.review_comments)} 条` : ""}</small></td>
      ${window.CaseDirectionPicker.videoCell(v, perf.catalog)}<td>${badge(v)}</td><td class="num">${v.window_views == null ? "—" : count(v.window_views)}</td>
      <td class="num">${count(v.views)}</td><td class="num">${count(v.items)}</td><td class="num">${money(v.gmv)}</td><td class="num">${pct(v.ctor_pct)}</td>
      ${['likes','comments','shares','saves'].map(field => `<td class="num" data-stat="${field}" title="${v.public_stats ? 'TK 看板插件最新推送' : '销售数据表'}">${count(v[field])}</td>`).join('')}
    </tr>`).join("")}</tbody></table></div>${rows.length > visible.length ? `<button type="button" class="secondary perf-more" id="perf-more">再显示 ${Math.min(200, rows.length - visible.length)} 条（共 ${rows.length} 条）</button>` : ""}`;
  }

  // The filtered videos as a separate .xlsx (export and public views side by side); the original exports stay untouched.
  async function exportExcel(button) {
    const ids = filtered().map((v) => v.video_id);
    if (!ids.length) return toast("当前筛选没有视频。", true);
    button.disabled = true;
    try {
      const result = await api("/api/performance/public-export", { method: "POST", body: { video_ids: ids } });
      if (typeof result?.download_url !== "string" || !/^\/api\/performance\/public-export\/[\w-]+$/.test(result.download_url)) throw new Error("导出文件下载地址无效，请重试。");
      const link = document.createElement("a");
      link.href = result.download_url;
      link.download = result.name;
      document.body.append(link);
      link.click();
      link.remove();
      toast(`已导出 ${result.rows} 条视频，其中 ${result.with_public} 条有公开页播放。文件也保存在 ${result.path}`);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  // Everything about one video that the table leaves out, with its actions.
  const videoSourceURL = (v) => `https://www.tiktok.com/@${encodeURIComponent(String(v.account || '').replace(/^@/,'') || '_')}/video/${encodeURIComponent(v.video_id)}`;
  // One video's totals as four reads: the buying chain, how its plays came in, engagement on one scale, and money.
  // Each figure stays a <dt>/<dd> pair so it reads (and is found) by its label; bars and rates sit beside the value.
  function videoFacts(v) {
    const fact = (label, value, extra = "", cls = "") => `<div class="vb-fact${cls ? " " + cls : ""}"><dt>${esc(label)}</dt><dd>${value}</dd>${extra}</div>`;
    const real = (n) => typeof n === "number" && Number.isFinite(n);
    const share = (part, whole) => (real(part) && real(whole) && whole > 0 ? part / whole : null);
    // 点击率 is clicks over the shop's own play count, which the plugin's public count replaces in `views`.
    const shopViews = v.views_source === "public" ? v.views_shop : v.views;
    const basket = real(v.gmv) && real(v.orders) && v.orders > 0 ? v.gmv / v.orders : null;
    const rate = (label, value) => fact(label, value, "", "vb-rate");
    const chain = `<section class="vb-block"><h4>转化链路</h4><dl class="vb-chain">
        ${fact("后台播放", count(shopViews), "", "vb-step")}${rate("点击率", pct(v.ctr_pct))}
        ${fact("商品点击", count(v.clicks), "", "vb-step")}${rate("CTOR", pct(v.ctor_pct))}
        ${fact("订单", count(v.orders), "", "vb-step")}${rate("客单价", money(basket))}
        ${fact("归因 GMV", money(v.gmv), "", "vb-step is-money")}</dl></section>`;
    const early = share(v.window_views, v.views), later = real(v.views) && real(v.window_views) ? v.views - v.window_views : null;
    const split = early != null && early <= 1
      ? `<div class="vb-split" role="img" aria-label="首两周占累计播放 ${Math.round(early * 100)}%"><i data-width="${(early * 100).toFixed(1)}"></i><b></b></div>
         <p class="vb-note">首两周 <strong>${Math.round(early * 100)}%</strong> · 之后 +${count(later)}</p>` : "";
    const plays = `<section class="vb-block"><h4>播放</h4><dl class="vb-facts">${fact("累计播放", count(v.views))}${fact("首两周播放", v.window_views == null ? "—" : count(v.window_views))}${fact("曝光", count(v.impressions))}</dl>${split}</section>`;
    const engagement = [["点赞", v.likes], ["评论", v.comments], ["转发", v.shares], ["收藏", v.saves]];
    const most = Math.max(0, ...engagement.map(([, n]) => (real(n) ? n : 0)));
    const perMille = (n) => { const r = share(n, v.views); return r == null ? "" : `${(r * 1000).toFixed(r * 1000 >= 10 ? 0 : 1)}‰`; };
    const engage = `<section class="vb-block"><h4>互动</h4><dl class="vb-engage">${engagement.map(([label, n]) =>
      fact(label, count(n), `<span class="vb-bar" aria-hidden="true"><i data-width="${most && real(n) ? Math.max(n > 0 ? 2 : 0, n / most * 100).toFixed(1) : 0}"></i></span><small class="vb-per">${perMille(n)}</small>`)).join("")}</dl></section>`;
    const sales = `<section class="vb-block"><h4>成交</h4><dl class="vb-facts">${fact("售出件数", count(v.items))}${fact("GPM", v.gpm == null ? "—" : "$" + v.gpm.toFixed(2))}${fact("本周 GMV", v.this_week_gmv ? money(v.this_week_gmv) : "—")}</dl></section>`;
    const coverage = `<div class="vb-meta"><dl class="vb-coverage">${fact("数据覆盖", v.first_data ? `${esc(v.first_data)} 至 ${esc(v.last_data || "—")}` : "—")}</dl>${publicStatsSource(v)}</div>`;
    return `<div class="perf-video-board" data-video-facts>${chain}${plays}${engage}${sales}${coverage}</div>`;
  }
  // The page's CSP forbids inline style attributes, so bar lengths travel as data-width and are set here.
  const paintFactBars = (root) => root?.querySelectorAll("[data-video-facts] i[data-width]").forEach((bar) => { bar.style.width = `${bar.dataset.width}%`; });
  // Campaigns overlapping the observation window; the review's own read replaces this once it loads.
  const campaignNote = (v, rows = window.Campaigns?.overlapping(v.posted_on, v.window_end)) => window.Campaigns?.reviewNote(rows, { bare: true }) || "";
  function publicStatsSource(v) {
    const stamp = v.public_stats?.last_seen_at || v.public_stats?.observed_at;
    const parsed = stamp ? new Date(stamp) : null;
    const when = parsed && !Number.isNaN(parsed.getTime()) ? ` · <time datetime="${esc(stamp)}">${esc(parsed.toLocaleString('zh-CN',{hour12:false}))}</time>` : '';
    return `<p class="vb-stamp" data-public-stats-source${v.public_stats ? '' : ' hidden'}>${v.public_stats ? '公开页' + when : ''}</p>`;
  }
  function videoDetail(v) {
    const hooks = window.MaterialBasket?.count({ type: "hook", source_id: v.video_id }) || 0;
    return `<div class="rev-drawer-head"><h2 id="perf-video-title">${esc(v.headline || v.video_id)}</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭视频详情">${icon("close")}</button></div>
      <div class="rev-drawer-body perf-video-body"><div class="perf-video-ident"><p class="perf-video-meta">${esc(v.account || "账号未记录")} · 发布 ${esc(v.posted_on || "—")} · ${esc(v.product || "产品未记录")}</p>
        <div class="perf-video-source"><span>ID ${esc(v.video_id)}${copyIdButton(v.video_id,'视频 ID')}</span><span><a href="${esc(videoSourceURL(v))}" target="_blank" rel="noopener noreferrer">原视频 ↗</a><button type="button" class="subtle compact" data-copy-video-link>复制直链</button></span></div></div>
        <div data-video-campaigns>${campaignNote(v)}</div>
        ${videoFacts(v)}<p class="context-note" data-video-facts-error hidden role="status"></p>
        <div class="vb-block perf-review-card">${videoStatusMarkup(v)}</div>
        <section class="vb-block perf-video-section"><h3>用户评论</h3>${videoComments(v)}</section>
        <section class="vb-block perf-video-section"><h3>案例与素材</h3><div class="perf-video-actions">${v.case_id ? `<a class="secondary compact perf-case-link" href="#case/${esc(v.case_id)}">查看案例</a>` : `<button type="button" class="secondary compact" data-collect="${esc(v.video_id)}">收为案例</button>`}<button type="button" class="secondary compact" data-video-hook="${esc(v.video_id)}">${hooks ? `摘录钩子 · 已有 ${hooks}` : "摘录钩子"}</button></div></section></div>`;
  }
  function videoComments(v) {
    return `<p class="perf-reviews" data-video-comments>${v.review_comments ? `<a class="perf-case-link" href="#reviews?video=${esc(v.video_id)}">评论库已收录 ${count(v.review_comments)} 条</a>${v.review_open ? ` <span class="rev-badge rev-badge-open" title="提问没人回复">${count(v.review_open)} 个没人回复</span>` : ""}` : "评论库里还没有这条视频的评论。"}</p>`;
  }
  // The drawer body is the same wherever it opens; only what "收为案例" can do depends on the page behind it.
  let videoDrawerSequence = 0;
  const videoFactReaders = new WeakMap();
  const sameReviewProduct = (a,b) => String(a || '').replace(/^SHARPAL\s+/i,'').trim().toLowerCase() === String(b || '').replace(/^SHARPAL\s+/i,'').trim().toLowerCase();
  function renderVideo(drawer, v, frozenWindow, onCollect, reviewScope = currentProduct(), initialData = null, onChanged = null) {
    const owner = `${v.product}|${v.video_id}|${frozenWindow || v.window_end}|${++videoDrawerSequence}`, openedHash = location.hash;
    drawer.dataset.feedbackOwner = owner;
    const isCurrent = () => drawer.isConnected && drawer.open && drawer.contains(feedback) && drawer.dataset.feedbackOwner === owner && location.hash === openedHash;
    drawer.classList.remove("perf-creator-drawer");
    drawer.innerHTML = videoDetail(v);
    paintFactBars(drawer);
    if (frozenWindow && frozenWindow !== v.window_end) $('.perf-video-status .perf-status',drawer).hidden = true;
    const feedback = document.createElement('section');feedback.id = 'video-feedback-chain';$('.perf-video-status',drawer).after(feedback);
    // Refresh figures in place so a plugin push cannot discard an unfinished review.
    let factsSequence = 0, feedbackLoaded = false;
    const refreshFacts = async () => {
      if (v.registration_only) return;
      const request = ++factsSequence;
      try {
        const full = (await api(`/api/performance/videos/${encodeURIComponent(v.video_id)}`, {cache:'no-store'})).video;
        if (!isCurrent() || request !== factsSequence || full?.video_id !== v.video_id || !sameReviewProduct(full.product,v.product)) return;
        const fields = ['views','views_shop','views_source','likes','comments','shares','saves','likes_shop','comments_shop','shares_shop','saves_shop','likes_source','comments_source','shares_source','saves_source','public_stats','window_views','impressions','clicks','ctr_pct','orders','items','gmv','gpm','ctor_pct','this_week_gmv','first_data','last_data','review_comments','review_open'];
        const rows = [...(perf.videos || []), ...[...floating.videos.values()].flatMap(cache => cache.videos || []), v];
        for (const row of rows) if (row.video_id === v.video_id) {
          for (const field of fields) {
            if (Object.hasOwn(full,field)) row[field] = full[field];
            else delete row[field];
          }
        }
        $('[data-video-facts]',drawer).outerHTML = videoFacts(v);paintFactBars(drawer);
        $('[data-public-stats-source]',drawer).outerHTML = publicStatsSource(v);
        $('[data-video-comments]',drawer).outerHTML = videoComments(v);
        $('[data-video-facts-error]',drawer).hidden = true;
        if (pageHash().startsWith('#videos') && $('#perf-results')) rerenderResults();
      } catch {
        if (!isCurrent() || request !== factsSequence) return;
        const note = $('[data-video-facts-error]',drawer);
        note.textContent = '播放与成交数据暂未刷新，当前显示上次读取结果。';
        note.hidden = false;
      }
    };
    videoFactReaders.set(drawer,refreshFacts);
    if (!initialData) void refreshFacts();
    const onSaved = window.FeedbackChain?.continueAfterSave?.({host:drawer,product:reviewScope,isCurrent,onNext:async (candidate,{isCurrent:continueCurrent}) => {
      // Read both the full video and its review before replacing the saved record on screen.
      const full = (await api(`/api/performance/videos/${encodeURIComponent(candidate.video_id)}`)).video;
      if (!isCurrent() || !continueCurrent()) return;
      if (!full || full.video_id !== candidate.video_id || !sameReviewProduct(full.product,candidate.product) || full.window_end !== candidate.window_end) throw new Error('下一条的数据窗口已更新，请重新读取。');
      const query = new URLSearchParams({product:candidate.product,video_id:candidate.video_id,window_end:candidate.window_end});
      const chain = await api(`/api/feedback-chain?${query}`);
      if (!isCurrent() || !continueCurrent()) return;
      const today = window.WorkbenchHome?.currentWeek?.().today;
      const stillReady = today && window.ReviewFollowups?.bucket({...full,review_context:chain.review_context},chain.review ? [chain.review] : [],today) === 'ready';
      if (chain.video_id !== candidate.video_id || !sameReviewProduct(chain.product,candidate.product) || !chain.current_window || chain.window_end !== candidate.window_end || chain.review_status?.key !== 'pending' || chain.review_context?.closing_allowed !== true || !stillReady) throw new Error('下一条的复盘状态已更新，请重新读取。');
      if (drawer.id === 'perf-video-drawer') {
        const linked = new URLSearchParams(pageHash().split('?')[1] || '');
        linked.set('video',candidate.video_id);linked.set('feedback_window',candidate.window_end);
        history.replaceState(null,'',pageURL(`#videos?${linked}`));
        perf.openVideo = candidate.video_id;
        $$('[data-video-row]').forEach(row => row.classList.toggle('is-open',row.dataset.videoRow === candidate.video_id));
      }
      renderVideo(drawer,full,candidate.window_end,onCollect,reviewScope,chain,onChanged);
      drawer.scrollTop = 0;
      const body = $('.rev-drawer-body',drawer);if (body) body.scrollTop = 0;
    }});
    window.FeedbackChain?.mount(feedback,{video:{...v,...(frozenWindow ? {window_end:frozenWindow} : {})},externalMetrics:v,heading:false,initialData,onSaved,onChanged:() => isCurrent() ? onChanged?.() : undefined,onLoaded:data => {
      if (!isCurrent()) return;
      if (feedbackLoaded) void refreshFacts();
      feedbackLoaded = true;
      // The requested review window owns the drawer's label, including historical windows.
      const status = reviewStatus({review_status:data.review_status || data.summary?.review_status});
      const campaigns = $('[data-video-campaigns]',drawer);if (campaigns) campaigns.innerHTML = campaignNote(v,data.campaigns);
      $('.perf-video-status',drawer).outerHTML = videoStatusMarkup({...v,window_end:data.window_end,review_status:status});
      if (!data.current_window || data.window_end !== v.window_end) return;
      const matches = row => row.video_id === v.video_id && row.window_end === data.window_end && sameReviewProduct(row.product,data.product);
      const rows = [...(perf.videos || []), ...[...floating.videos.values()].flatMap(cache => cache.videos || []), v];
      let changed = false;
      rows.forEach(row => {if (matches(row)) {changed ||= reviewStatus(row).key !== status.key;row.review_status = {...status};}});
      if (changed && pageHash().startsWith('#videos') && $('#perf-results')) rerenderResults();
    }});
    $("[data-collect]", drawer)?.addEventListener("click", () => onCollect(v));
    $("[data-video-hook]", drawer).onclick = () => window.MaterialBasket?.openHook(v);
    $("[data-copy-video-link]", drawer).onclick = () => copyText(videoSourceURL(v),'视频直链已复制');
    window.DrawerResize?.attach(drawer, "video");
  }
  function refreshOpenVideoFacts() {
    for (const drawer of $$('dialog.perf-video-drawer[open]')) {
      if (drawer.classList.contains('perf-creator-drawer')) void refreshCreatorPublicStats();
      else void videoFactReaders.get(drawer)?.();
    }
  }
  let publicRefreshSequence = 0;
  async function publicStatsChanged() {
    floating.videos.clear();
    const request = ++publicRefreshSequence, sequence = perf.loadSequence, shop = perf.requestedShop;
    for (const drawer of $$('dialog.perf-video-drawer[open]:not(.perf-creator-drawer)')) void videoFactReaders.get(drawer)?.();
    if (perf.creators) void refreshCreatorPublicStats();
    if (!perf.videos || !shop) return;
    try {
      const data = await api(`/api/performance/videos?shop=${encodeURIComponent(shop)}`, {cache:'no-store'});
      if (request !== publicRefreshSequence || sequence !== perf.loadSequence || shop !== perf.requestedShop) return;
      perf.videos = data.videos;
      if (pageHash().startsWith('#videos') && $('#perf-results')) rerenderResults();
    } catch { /* Open details report their own refresh failure and retry on focus. */ }
  }
  function openVideo(id) {
    const v = perf.videos?.find((item) => item.video_id === id), drawer = $("#perf-video-drawer");
    if (!v || !drawer) return;
    perf.openVideo = id;
    if (!drawer.open) drawer.showModal();
    renderVideo(drawer, v, new URLSearchParams(pageHash().split('?')[1] || '').get('feedback_window'), selected => {drawer.close();collect(selected.video_id,selected);});
    $$("[data-video-row]").forEach((row) => row.classList.toggle("is-open", row.dataset.videoRow === id));
  }

  // ---- the same drawer opened from any other page, without leaving it ----------
  // Sales rows are read once per shop and kept briefly; the data page's own list is used when it already has them.
  const floating = {videos: new Map(), drawer: null, openSequence: 0, onClose: null};
  async function findVideo(id) {
    const shops = [perf.filters.shop, ...Object.keys(shopNames).filter((shop) => shop !== perf.filters.shop)];
    for (const shop of shops) {
      let list = perf.requestedShop === shop && perf.videos ? perf.videos : null;
      const cached = floating.videos.get(shop);
      if (!list && cached && Date.now() - cached.at < 60000) list = cached.videos;
      if (!list) {
        list = (await api(`/api/performance/videos?shop=${shop}`)).videos || [];
        floating.videos.set(shop, {at: Date.now(), videos: list});
      }
      const found = list.find((item) => item.video_id === id);
      if (found) return found;
    }
    return null;
  }
  function floatingDrawer() {
    if (floating.drawer?.isConnected) return floating.drawer;
    const drawer = document.createElement("dialog");
    drawer.id = "perf-video-drawer-floating";
    drawer.className = "rev-drawer perf-video-drawer";
    drawer.setAttribute("aria-labelledby", "perf-video-title");
    drawer.addEventListener("click", (event) => { if (event.target === drawer || event.target.closest("[data-drawer-close]")) drawer.close(); });
    drawer.addEventListener("close", () => {
      // A close event queued before a new open must not clear the new session.
      if (drawer.open) return;
      const onClose = floating.onClose;floating.onClose = null;
      void onClose?.();
    });
    // Following a link inside the drawer moves to another page; the drawer must not stay over it.
    window.addEventListener("hashchange", () => { if (drawer.open) drawer.close(); });
    document.body.append(drawer);
    return (floating.drawer = drawer);
  }
  async function openVideoInPlace(id, {feedbackWindow = "", video: source = null, onChanged, onClose} = {}) {
    const sequence = ++floating.openSequence, openedHash = location.hash, reviewScope = currentProduct();
    const previousDrawer = floating.drawer?.open ? floating.drawer : null;
    const isCurrent = () => sequence === floating.openSequence && location.hash === openedHash && currentProduct() === reviewScope && (!previousDrawer || previousDrawer.open);
    const dataPage = () => { location.hash = pageURL(`#videos?video=${encodeURIComponent(id)}${feedbackWindow ? `&feedback_window=${encodeURIComponent(feedbackWindow)}` : ""}`); };
    if (pageHash().startsWith("#videos")) return dataPage();
    let video = null;
    try { video = source?.registration_only && source.video_id === id ? source : await findVideo(id); }
    catch { if (!isCurrent()) return;toast("带货数据暂时读不到，已为你打开带货数据页。", true); return dataPage(); }
    if (!isCurrent()) return;
    if (!video) { toast("销售数据表里没有这条视频，已为你打开带货数据页。"); return dataPage(); }
    const drawer = floatingDrawer();
    floating.onClose = () => {
      if (sequence === floating.openSequence && location.hash === openedHash && currentProduct() === reviewScope) return onClose?.();
    };
    if (!drawer.open) drawer.showModal();
    renderVideo(drawer, video, feedbackWindow, selected => { drawer.close();location.hash = pageURL(`#videos?video=${encodeURIComponent(selected.video_id)}`); },reviewScope,null,onChanged);
    drawer.scrollTop = 0;
    const body = $('.rev-drawer-body',drawer);if (body) body.scrollTop = 0;
  }
  // A link to a video's data keeps the reader where they are, unless the data page itself is showing.
  function videoLinkTarget(hash) {
    const legacy = window.WorkbenchShell?.legacyHash?.(hash) || hash;
    if (!legacy.startsWith("#videos?")) return null;
    const params = new URLSearchParams(legacy.split("?")[1] || "");
    return params.get("video") ? {id: params.get("video"), feedbackWindow: params.get("feedback_window") || ""} : null;
  }
  function openVideoHash(hash) {
    const target = videoLinkTarget(hash);
    if (!target || pageHash().startsWith("#videos")) return false;
    void openVideoInPlace(target.id, {feedbackWindow: target.feedbackWindow});
    return true;
  }
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest?.("a[href]");
    if (!link || (link.target && link.target !== "_self") || link.hasAttribute("download")) return;
    const hash = new URL(link.href, location.href).hash;
    if (hash && openVideoHash(hash)) event.preventDefault();
  });

  // ---- overseas creators --------------------------------------------------
  // The all-creator exports load the first time their tab is shown, and again after an import.
  function ensureCreators() {
    if (perf.creators || perf.creatorsLoading || perf.creatorsError) return;
    const sequence = perf.creatorsSequence;
    let publicSequence = publicRefreshSequence;
    const loading = api("/api/performance/creators").then(async (data) => {
      while (sequence === perf.creatorsSequence && publicSequence !== publicRefreshSequence) {
        publicSequence = publicRefreshSequence;
        data = await api("/api/performance/creators", {cache:'no-store'});
      }
      if (sequence === perf.creatorsSequence) perf.creators = data;
    }).catch((error) => {
      if (sequence === perf.creatorsSequence) perf.creatorsError = error.message;
    }).finally(() => {
      if (perf.creatorsLoading === loading) perf.creatorsLoading = null;
      if (sequence !== perf.creatorsSequence || !pageHash().startsWith("#videos")) return;
      if (perf.filters.tab === "creators" && $("#perf-creators")) render();
      else if (perf.filters.tab === "board" && $("#perf-board")) refresh();
    });
    perf.creatorsLoading = loading;
  }
  const creatorContext = () => ({ data: perf.creators, filters: perf.filters, shown: perf.creatorShown });
  let creatorPublicRequest = 0;
  async function refreshCreatorPublicStats() {
    const request = ++creatorPublicRequest, sequence = perf.creatorsSequence, publicSequence = publicRefreshSequence;
    try {
      const data = await api('/api/performance/creators', {cache:'no-store'});
      if (request !== creatorPublicRequest || sequence !== perf.creatorsSequence || publicSequence !== publicRefreshSequence) return;
      perf.creators = data;
      if (!pageHash().startsWith('#videos')) return;
      if ($('#perf-creators')) rerenderCreators();
      const drawer = $('#perf-video-drawer[open].perf-creator-drawer');
      if (drawer?.dataset.creatorAccount) openCreator(drawer.dataset.creatorAccount);
    } catch { /* Keep the last displayed creator snapshot until the next refresh. */ }
  }
  const creatorsMarkup = () => periodCampaignPending() ? campaignLoadingMarkup() : perf.creators ? PerfCreators.board(creatorContext())
    : perf.creatorsError ? `<div class="empty">${icon("chart")}<h3>达人数据暂不可用</h3><p>${esc(perf.creatorsError)}</p></div>` : '<p class="context-note" role="status">正在读取达人数据…</p>';
  function rerenderCreators() {
    const root = $("#perf-creators");
    if (!root) return;
    root.innerHTML = creatorsMarkup();
    syncToolbar();
    bindCreators();
  }
  function openCreator(account) {
    const drawer = $("#perf-video-drawer"), html = perf.creators && PerfCreators.detail(creatorContext(), account);
    if (!drawer || !html) return;
    drawer.classList.add("perf-creator-drawer");
    drawer.dataset.creatorAccount = account;
    drawer.innerHTML = html;
    window.DrawerResize?.attach(drawer, "creator");
    $$("[data-creator-row]").forEach((row) => row.classList.toggle("is-open", row.dataset.creatorRow === account));
    if (!drawer.open) drawer.showModal();
  }
  function bindCreators() {
    const root = $("#perf-creators");
    if (!root) return;
    $$("[data-creator-sort]", root).forEach((button) => (button.onclick = () => {
      const key = button.dataset.creatorSort, f = perf.filters;
      f.cdir = f.csort === key && f.cdir === "desc" ? "asc" : "desc";
      f.csort = key;
      perf.creatorShown = 100;
      saveFilters();
      rerenderCreators();
    }));
    $$("[data-creator-row]", root).forEach((row) => {
      const open = (event) => { if (event.target.closest("a,button")) return; openCreator(row.dataset.creatorRow); };
      row.onclick = open;
      row.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(event); } };
    });
    $$("[data-creator-open]", root).forEach((button) => (button.onclick = () => openCreator(button.dataset.creatorOpen)));
    const more = $("#perf-creator-more");
    if (more) more.onclick = () => { perf.creatorShown += 100; rerenderCreators(); };
  }

  function render() {
    cancelSearch();
    perf.filters.product = currentProduct();
    normalizeCampaignFilter();
    syncFilterURL(Boolean(perf.pendingVideo || perf.openVideo));
    const s = perf.summary, f = perf.filters, board = f.tab === "board", creators = f.tab === "creators", photos = f.tab === "photos";
    const rows = board || creators || photos ? [] : filtered();
    const shop = shopNames[f.shop], info = (s.shops || []).find((item) => item.id === f.shop);
    // A shop whose folder has no exports yet shows how to fill it, instead of empty charts.
    const bare = info && !info.files;
    const accounts = [...new Set(perf.videos.map((v) => v.account).filter(Boolean))].sort();
    const gaps = s.coverage.gaps.map(([a, b]) => (a === b ? a : `${a} 至 ${b}`)).join("、");
    $("#breadcrumb").innerHTML = "带货数据";
    $("#content").innerHTML = `<div class="page-heading perf-heading"><div><h1>带货数据</h1><p>${esc(shop)}：${s.coverage.first ? `视频导出覆盖 ${s.coverage.first} 至 ${s.coverage.last}${gaps ? `，缺 ${gaps}` : ""}。` : "还没有导入后台数据。"}参考价值以人工复盘分类为准。</p></div>
      <div class="perf-actions"><button type="button" class="secondary" id="perf-sync" title="读取数据文件夹里所有店铺的新导出">${icon("refresh")}同步数据</button><button type="button" class="secondary" id="perf-upload" title="选的表格会算作${esc(shop)}的数据">${icon("plus")}导入表格</button><details class="perf-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" id="perf-copy">复制复盘材料</button></div></details><input type="file" id="perf-file" accept=".xlsx" multiple hidden></div></div>
      ${shopBar(s)}
      <details class="perf-about"><summary>数据来源与口径</summary><p>数据文件夹：${s.sources.length ? s.sources.map((p) => `<code>${esc(p)}</code>`).join("、") : "未设置"} · 已导入 ${s.imports.files} 个文件${s.imports.last ? `，最近一次 ${esc(s.imports.last.replace("T", " "))}` : ""}。</p><p>数据文件夹里按「店铺 / 内容类型（视频、销售、图文、达人）」分开放：放在「跨境店」文件夹里的算跨境店，其余算本土店。图文是另一份按商品汇总的导出，在「图文」标签单独看。</p><p>视频指标是后台导出里逐条视频的归因数据加总；店铺每日汇总来自另一份导出，口径更大，单独展示不与视频明细相加。点击率 = 商品点击 ÷ 播放，CTOR = 订单 ÷ 商品点击，GPM = 每千次播放的归因 GMV。${gaps ? `导出缺口：${esc(gaps)}，缺口所在周在走势里以短横表示。` : ""}</p></details>
      <div id="perf-import-result" role="status"></div>
      <div class="perf-tabs" role="tablist" aria-label="带货数据视图">${tabs.map(([k, label]) => `<button type="button" role="tab" id="perf-tab-${k}" data-perf-tab="${k}" aria-selected="${k === f.tab}" class="${k === f.tab ? "is-active" : ""}">${label}</button>`).join("")}</div>
      <div class="toolbar perf-toolbar"${bare ? " hidden" : ""}>${PerfDatePicker.trigger(f.period, perf.summary)}${creators ? PerfCreators.toolbar(perf.creators, f) : board || photos ? ""
        : `<label class="search">${icon("search")}<input id="perf-q" aria-label="搜索视频标题或 ID" placeholder="搜索标题或视频 ID…" value="${esc(f.q)}"></label>`}
        ${creators || photos ? "" : `<select id="perf-account" aria-label="账号">${options(accounts, f.account, "全部账号")}</select>`}
        <button type="button" class="subtle perf-reset" id="perf-reset" hidden>清除筛选</button>
        ${board || creators || photos ? "" : `<span class="result-count">${resultCount(rows)}</span><button type="button" class="secondary compact" id="perf-export" title="把当前筛选的视频导出为 Excel：后台累计播放与公开页播放并排，原导出表不改">导出 Excel</button>`}</div>
      ${!bare && window.Campaigns ? '<div class="perf-campaign-filter" id="perf-campaign-filter"></div>' : ""}
      ${bare ? `<div class="empty">${icon("chart")}<h3>${esc(shop)}还没有数据</h3><p>把${esc(shop)}的后台导出放进数据文件夹里的「${esc(shop)}」子文件夹（视频、销售、图文、达人分开放），再点「同步数据」；也可以直接点「导入表格」。</p></div>`
        : creators ? `<div id="perf-creators" class="perf-board" role="tabpanel" aria-labelledby="perf-tab-creators">${creatorsMarkup()}</div>`
        : photos ? `<div id="perf-photos" class="perf-board" role="tabpanel" aria-labelledby="perf-tab-photos">${photosMarkup()}</div>`
        : board ? `<div id="perf-board" class="perf-board" role="tabpanel" aria-labelledby="perf-tab-board">${boardMarkup()}</div>`
        : `<div id="perf-status-slot">${statusMarkup(filtered(true))}</div><div id="perf-results" role="tabpanel" aria-labelledby="perf-tab-list">${tableMarkup(rows)}</div>`}
      <dialog id="perf-video-drawer" class="rev-drawer perf-video-drawer" aria-labelledby="perf-video-title"></dialog>`;
    perf.renderedProduct = currentProduct();
    bind();
    drawCampaignFilter();
    syncToolbar();
    if (!bare) ensureCampaigns();
    if (creators && !bare) ensureCreators();
    if (photos && !bare) ensurePhotos();
    if (board && !bare && perf.analytics && !perf.analyticsLoading) ensureBoardExtras();
  }

  // The first level of the page: one button per shop, each saying how far its exports reach.
  function shopBar(s) {
    const current = perf.filters.shop;
    return `<div class="perf-shops" role="tablist" aria-label="店铺">${Object.entries(shopNames).map(([id, name]) => {
      const info = (s.shops || []).find((item) => item.id === id);
      const note = !info ? "" : info.files ? `导出至 ${info.last.slice(5)}` : "暂无数据";
      return `<button type="button" role="tab" class="perf-shop${id === current ? " is-active" : ""}" data-perf-shop="${id}" aria-selected="${id === current}"><strong>${esc(name)}</strong>${note ? `<small>${esc(note)}</small>` : ""}</button>`;
    }).join("")}</div>`;
  }

  // Photo (图文) exports load the first time their tab is shown, and again after an import or a shop switch.
  function ensurePhotos() {
    if (perf.photos || perf.photosLoading || perf.photosError) return;
    const sequence = perf.photosSequence;
    // The per-post table is a second export; a service that does not have it yet still shows the per-product one.
    const loading = Promise.all([api(`/api/performance/photos?${shopQuery()}`), api(`/api/performance/photo-items?${shopQuery()}`).catch(() => null)]).then(([data, items]) => {
      if (sequence === perf.photosSequence) { perf.photos = data; perf.photoItems = items; perf.photoItemsState = items ? "done" : "failed"; }
    }, (error) => {
      if (sequence === perf.photosSequence) perf.photosError = error.message;
    }).finally(() => {
      if (perf.photosLoading === loading) perf.photosLoading = null;
      if (sequence === perf.photosSequence && perf.filters.tab === "photos" && $("#perf-photos") && pageHash().startsWith("#videos")) rerenderPhotos();
    });
    perf.photosLoading = loading;
  }
  const photosMarkup = () => periodCampaignPending() ? campaignLoadingMarkup() : perf.photos ? PerfDashboard.photoBoard({ data: perf.photos, items: perf.photoItems, filters: { ...perf.filters, product: currentProduct() } })
    : perf.photosError ? `<div class="empty">${icon("chart")}<h3>图文数据暂不可用</h3><p>${esc(perf.photosError)}</p></div>` : '<p class="context-note" role="status">正在读取图文数据…</p>';
  function rerenderPhotos() {
    const root = $("#perf-photos");
    if (!root) return;
    root.innerHTML = photosMarkup();
    PerfDashboard.bind(root);
    syncToolbar();
  }

  async function switchShop(shop) {
    const f = perf.filters;
    if (!(shop in shopNames) || shop === f.shop) return;
    // Keep the shared activity while resetting the previous shop's own filters.
    Object.assign(f, { shop, account: "", status: "", q: "", period: "all", list_period: "all", board_period: "all" });
    normalizeCampaignFilter();
    syncFilterURL();
    perf.shown = 200;
    perf.creatorShown = 100;
    saveFilters();
    resetData();
    $("#content").innerHTML = skeletonMarkup(3);
    const isCurrent = () => f.shop === shop && pageHash().startsWith("#videos") && perf.requestedShop === shop;
    try {
      const loading = load(true);
      let needsAnalytics = false;
      await perf.coreLoading;
      if (!isCurrent()) return;
      needsAnalytics = perf.analyticsLoading;
      render();
      void loading.catch(() => {});
      await perf.analyticsReady;
      if (needsAnalytics && isCurrent() && f.tab === "board") refresh();
    } catch (error) {
      if (!isCurrent()) return;
      $("#content").innerHTML = `<div class="empty">${icon("chart")}<h3>带货数据暂不可用</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  // The dashboard also shows photo posts and overseas creators; they are read after the charts, so the video figures never wait for them.
  const boardContext = () => ({ filters: { ...perf.filters, product: currentProduct() }, summary: perf.summary, analytics: perf.analytics, videos: perf.videos,
    extras: { photoItems: perf.photoItems, photoFailed: perf.photoItemsState === "failed", creators: perf.creators, creatorsFailed: !!perf.creatorsError } });
  // Read the same saved calendar as the board and retrospective; a calendar edit is visible on the next render.
  function normalizeCampaignFilter() {
    const data = window.Campaigns?.current();
    const f = perf.filters, selected = window.Campaigns?.selection?.() ?? "";
    let changed = false;
    if (data) perf.campaignsError = false;
    // Restored list state remembers search/date controls, never an older activity selection.
    if (f.board_campaign && !selected && f.board_period !== "all") {
      f.board_period = "all";
      changed = true;
    }
    if (f.campaign !== selected || f.board_campaign !== selected) {
      f.campaign = f.board_campaign = selected;
      perf.shown = 200;
      perf.creatorShown = 100;
      changed = true;
    }
    const campaign = data?.campaigns.find(row => row.id === selected);
    if (campaign && f.board_period !== `${campaign.start}~${campaign.end}`) {
      f.board_period = `${campaign.start}~${campaign.end}`;
      changed = true;
    }
    const period = f.tab === "list" ? f.list_period : f.board_period;
    if (f.period !== period) {
      f.period = period;
      changed = true;
    }
    if (perf.filters.tab === "creators" && perf.filters.board_campaign && /^p\d+$/.test(perf.filters.csort)) {
      perf.filters.csort = "gmv";
      perf.filters.cdir = "desc";
      changed = true;
    }
    if (changed) saveFilters();
    return changed;
  }
  function drawCampaignFilter() {
    const root = $("#perf-campaign-filter");
    if (!root) return;
    const periodView = perf.filters.tab !== "list", key = periodView ? "board_campaign" : "campaign";
    const data = window.Campaigns?.current(), selected = perf.filters[key];
    const rows = [...(data?.campaigns || [])].sort((a,b) => (window.Campaigns.phase(a) === "next") - (window.Campaigns.phase(b) === "next") || b.start.localeCompare(a.start));
    const campaign = rows.find(row => row.id === selected);
    const basis = {
      board: "已切换到活动日期。店铺日度数据按活动起止日统计；视频按活动涉及的整周、图文与达人按重叠的整份导出统计，可能包含活动外日期，无法精确拆分到活动日。",
      photos: "已切换到活动日期。逐条图文按与活动重叠的整份导出统计，周汇总按整周统计；可能包含活动外日期，无法精确拆分到活动日。",
      creators: "已切换到活动日期。达人排行与明细按与活动重叠的整份导出统计，通常为月报；可能包含活动外日期，无法精确拆分到活动日。",
      list: "观察窗与活动期有重叠的视频会计入，含起止日期；上方日期仍按发布日期筛选。",
    }[perf.filters.tab];
    root.innerHTML = `<label for="perf-campaign">活动期</label><select id="perf-campaign" aria-label="活动期快速筛选" ${!data ? "disabled" : ""}><option value="">${data ? "全部活动期" : perf.campaignsError ? "活动读取失败" : "正在读取活动…"}</option>${rows.map(row => `<option value="${esc(row.id)}" ${row.id === selected ? "selected" : ""}>${esc(row.name)} · ${esc(row.start)} 至 ${esc(row.end)}${window.Campaigns.phase(row) === "next" ? " · 未开始" : ""}</option>`).join("")}</select>${perf.campaignsError ? '<span role="status">活动日历暂时无法读取。</span><button type="button" class="subtle compact" data-perf-campaign-retry>重试</button>' : data && !rows.length ? '<span>还没有登记活动。</span>' : ""}${campaign ? `<p id="perf-campaign-hint">${esc(campaign.name)} · ${esc(campaign.start)} 至 ${esc(campaign.end)}（太平洋时间）${campaign.verified ? " · 已核对" : ["planning","disputed"].includes(campaign.confidence) ? " · 日期待核对" : ""}。${basis}</p>` : ""}`;
    $("#perf-campaign").onchange = event => {
      window.Campaigns.select(event.target.value);
      normalizeCampaignFilter();
      perf.shown = 200;
      perf.creatorShown = 100;
      saveFilters();
      syncFilterURL();
      if (periodView) render();
      else {drawCampaignFilter();rerenderResults();}
      $("#perf-campaign")?.focus();
    };
    root.querySelector("[data-perf-campaign-retry]")?.addEventListener("click", () => ensureCampaigns(true));
  }
  function ensureCampaigns(force = false) {
    if (!window.Campaigns || perf.campaignsLoading || (!force && (window.Campaigns.current() || perf.campaignsRequested))) return;
    perf.campaignsRequested = true;
    perf.campaignsLoading = true;
    perf.campaignsError = false;
    drawCampaignFilter();
    if (campaignPending() && $("#perf-results")) rerenderResults();
    if (periodCampaignPending() && $(`#perf-${perf.filters.tab}`)) refresh();
    void window.Campaigns.load(force).then(data => {perf.campaignsError = !data;}, () => {perf.campaignsError = true;}).finally(() => {
      perf.campaignsLoading = false;
      if (!pageHash().startsWith("#videos")) return;
      const filteredByCampaign = Boolean(perf.filters.campaign);
      const periodCampaign = Boolean(perf.filters.board_campaign);
      normalizeCampaignFilter();
      syncFilterURL(Boolean(perf.pendingVideo || perf.openVideo));
      if (perf.filters.tab === "list" && $("#perf-results")) {
        drawCampaignFilter();
        if (filteredByCampaign) rerenderResults();
        else syncToolbar();
      } else if (perf.filters.tab !== "list" && $(`#perf-${perf.filters.tab}`)) {
        if (periodCampaign) render();
        else {drawCampaignFilter();refresh();}
      }
    });
  }
  function ensureBoardExtras() {
    ensureCreators();
    ensureCampaigns();
    if (perf.photoItems || perf.photoItemsState) return;
    perf.photoItemsState = "loading";
    const sequence = perf.photosSequence;
    api(`/api/performance/photo-items?${shopQuery()}`).then((data) => {
      if (sequence === perf.photosSequence) { perf.photoItems = data; perf.photoItemsState = "done"; }
    }, () => {
      if (sequence === perf.photosSequence) perf.photoItemsState = "failed";
    }).finally(() => {
      if (sequence === perf.photosSequence && perf.filters.tab === "board" && $("#perf-board") && pageHash().startsWith("#videos")) refresh();
    });
  }
  // The campaign calendar reads this page's loaded sales data to size a campaign for one product.
  const campaignContext = () => perf.analytics && perf.summary && perf.videos
    ? {analytics: perf.analytics, weeks: perf.summary.weeks, videos: perf.videos, product: currentProduct(), matches: productMatches} : null;
  function boardMarkup() {
    if (periodCampaignPending()) return campaignLoadingMarkup();
    if (perf.analyticsLoading) return '<p class="context-note" role="status">正在读取看板数据…</p>';
    const calendar = window.Campaigns?.card() || "", dashboard = PerfDashboard.board(boardContext());
    // Once an activity is picked, its figures lead; the editable calendar remains available below them.
    return perf.filters.board_campaign ? dashboard + calendar : calendar + dashboard;
  }

  // The dashboard redraws as a whole; the list only redraws its table.
  function refresh() {
    if (perf.filters.tab !== "list") {
      if (normalizeCampaignFilter()) return render();
      drawCampaignFilter();
    }
    if (perf.filters.tab === "creators") rerenderCreators();
    else if (perf.filters.tab === "photos") rerenderPhotos();
    else if (perf.filters.tab === "board") {
      const board = $("#perf-board");
      if (!board) return;
      board.innerHTML = boardMarkup();
      if (!perf.analyticsLoading) PerfDashboard.bind(board);
      bindBoardLinks();
      syncToolbar();
      if (!perf.analyticsLoading && perf.analytics) ensureBoardExtras();
    } else rerenderResults();
  }

  function setTab(tab) {
    if (perf.filters.tab === tab) return;
    perf.filters.tab = tab;
    normalizeCampaignFilter();
    saveFilters();
    syncFilterURL();
    render();
    window.scrollTo({ top: 0 });
  }

  function bindBoardLinks() {
    $$("#perf-board [data-perf-tab]").forEach((button) => (button.onclick = () => setTab(button.dataset.perfTab)));
    $$("#perf-board [data-perf-metric]").forEach((button) => (button.onclick = () => { perf.filters.metric = button.dataset.perfMetric; saveFilters(); refresh(); }));
    $$("#perf-board [data-perf-video]").forEach((button) => (button.onclick = () => openVideo(button.dataset.perfVideo)));
    if (window.Campaigns && $("#perf-board")) window.Campaigns.bind($("#perf-board"), refresh);
  }

  // Controls that narrow the table are outlined, and one button puts everything back.
  function syncToolbar() {
    const f = perf.filters, q = f.q.trim(), board = f.tab === "board", creators = f.tab === "creators", photos = f.tab === "photos";
    const set = (selector, on) => $(selector)?.classList.toggle("is-set", on);
    set("#perf-cshow", f.cshow !== "sold");
    $("#perf-date-trigger")?.classList.toggle("is-set", f.period !== "all");
    set("#perf-account", !!f.account);
    set("#perf-campaign", !!(f.tab !== "list" ? f.board_campaign : f.campaign));
    if ($("#perf-export")) $("#perf-export").disabled = Boolean(campaignPending());
    set("#perf-status", !!f.status);
    set(".perf-toolbar .search", !!(creators ? f.cq.trim() : q));
    // Only the controls on screen count: the dashboard ignores the list's own status and search, and the creators tab has its own.
    $("#perf-reset").hidden = creators ? !(f.period !== "all" || f.board_campaign || f.cq.trim() || f.cshow !== "sold")
      : photos ? !(f.period !== "all" || f.board_campaign)
      : !(f.account || f.period !== "all" || (board ? f.board_campaign : (f.status || q || f.campaign)));
  }

  function saveFilters() {
    perf.filters.product = currentProduct();
    try {
      localStorage.setItem("workbench-video-data-filters", JSON.stringify({...perf.filters,review_status_filter_version:1}));
    } catch {}
    syncFilterURL(Boolean(perf.pendingVideo || perf.openVideo));
  }

  function rerenderResults() {
    if (!$("#perf-results")) return;
    const rows = filtered();
    $("#perf-results").innerHTML = tableMarkup(rows);
    $(".perf-toolbar .result-count").innerHTML = resultCount(rows);
    $("#perf-status-slot").innerHTML = statusMarkup(filtered(true));
    syncToolbar();
    bindResults();
  }

  // Hooks already in the basket are counted on the button; a click still adds another one.
  function syncHooks() {
    $$("[data-video-hook]").forEach(button => {
      const count = window.MaterialBasket?.count({ type: "hook", source_id: button.dataset.videoHook }) || 0;
      button.textContent = count ? `摘录钩子 · 已有 ${count}` : "摘录钩子";
    });
  }
  window.addEventListener("workbench:basketitems", syncHooks);

  function bindResults() {
    if ($("#perf-results")) window.CaseDirectionPicker.bindVideos($("#perf-results"), perf.videos, () => perf.catalog, catalog => {perf.catalog = catalog;});
    $$("[data-video-row]").forEach((row) => {
      const open = (event) => { if (event.target.closest("a,button,select,input,form,.perf-direction")) return; openVideo(row.dataset.videoRow); };
      row.onclick = open;
      row.onkeydown = (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(event); } };
    });
    syncHooks();
    $$("[data-sort]").forEach((button) => (button.onclick = () => {
      const key = button.dataset.sort, f = perf.filters;
      // Same column flips direction; a new column starts from its natural direction (numbers high-to-low, status strongest first).
      f.dir = f.sort === key ? (f.dir === "asc" ? "desc" : "asc") : sortColumns[key][2];
      f.sort = key;
      perf.shown = 200;
      saveFilters();
      rerenderResults();
    }));
    const more = $("#perf-more");
    if (more) more.onclick = () => { perf.shown += 200; rerenderResults(); };
    // The page CSP blocks inline style attributes; segment sizes go through the CSSOM.
    $$(".perf-seg[data-count]").forEach((segment) => (segment.style.flexGrow = segment.dataset.count));
    const batchButton = $("[data-batch-collect]");
    if (batchButton) batchButton.onclick = () => void batchCollect(batchButton);
    $$("[data-status-filter]").forEach((button) => (button.onclick = () => {
      const status = button.dataset.statusFilter;
      perf.filters.status = perf.filters.status === status ? "" : status;
      perf.shown = 200;
      saveFilters();
      rerenderResults();
    }));
  }

  function bind() {
    $("#perf-q")?.addEventListener("input", (event) => {
      const field = event.target;
      perf.filters.q = field.value;
      cancelSearch();
      searchTimer = setTimeout(() => {
        cancelSearch();
        if (!field.isConnected || $("#perf-q") !== field || !pageHash().startsWith("#videos")) return;
        perf.shown = 200;
        saveFilters();
        rerenderResults();
      }, 150);
    });
    PerfDatePicker.bind({ period: () => perf.filters.period, summary: () => perf.summary, choose: (value) => {
      const f = perf.filters;
      f.period = value;
      if (f.tab === "list") f.list_period = value;
      else { f.board_period = value; window.Campaigns?.select?.(""); f.campaign = f.board_campaign = ""; }
      perf.shown = 200;
      perf.creatorShown = 100;
      saveFilters();
      syncFilterURL();
      render();
    } });
    $("#perf-cq")?.addEventListener("input", (event) => {
      const field = event.target;
      perf.filters.cq = field.value;
      cancelSearch();
      searchTimer = setTimeout(() => {
        cancelSearch();
        if (!field.isConnected || $("#perf-cq") !== field || !pageHash().startsWith("#videos")) return;
        perf.creatorShown = 100;
        saveFilters();
        rerenderCreators();
      }, 150);
    });
    $("#perf-cshow")?.addEventListener("change", (event) => { perf.filters.cshow = event.target.value; perf.creatorShown = 100; saveFilters(); refresh(); });
    const drawer = $("#perf-video-drawer");
    drawer.addEventListener("click", (event) => { if (event.target === drawer || event.target.closest("[data-drawer-close]")) drawer.close(); });
    drawer.addEventListener("close", () => { perf.openVideo = ""; syncFilterURL(); $$("[data-video-row].is-open, [data-creator-row].is-open").forEach((row) => row.classList.remove("is-open")); });
    $(".perf-menu")?.addEventListener("click", (event) => { if (event.target.closest("[role=menuitem]")) event.currentTarget.open = false; });
    [["#perf-account", "account"]].forEach(([selector, key]) =>
      $(selector)?.addEventListener("change", (event) => { perf.filters[key] = event.target.value; perf.shown = 200; saveFilters(); refresh(); }));
    $$(".perf-tabs [data-perf-tab]").forEach((button) => (button.onclick = () => setTab(button.dataset.perfTab)));
    $$("[data-perf-shop]").forEach((button) => (button.onclick = () => void switchShop(button.dataset.perfShop)));
    $("#perf-reset").onclick = () => {
      const f = perf.filters;
      window.Campaigns?.select?.("");
      if (f.board_campaign || f.tab !== "list") f.board_period = "all";
      f.campaign = f.board_campaign = "";
      if (f.tab === "creators") Object.assign(f, { period: "all", cq: "", cshow: "sold" });
      else if (f.tab === "photos") f.period = "all";
      else Object.assign(f, { account: "", period: "all" }, f.tab === "board" ? {} : { q: "", status: "", list_period: "all" });
      perf.shown = 200;
      perf.creatorShown = 100;
      saveFilters();
      syncFilterURL();
      render();
    };
    $("#perf-sync").onclick = () => runImport(() => api("/api/performance/sync", { method: "POST", body: {} }), $("#perf-sync"));
    $("#perf-upload").onclick = () => $("#perf-file").click();
    $("#perf-file").onchange = (event) => {
      const files = [...event.target.files];
      event.target.value = "";
      if (files.length) runImport(() => uploadExports(files), $("#perf-upload"));
    };
    $("#perf-copy").onclick = () => copyText(reviewMaterial(), "复盘材料已复制");
    const exportButton = $("#perf-export");
    if (exportButton) exportButton.onclick = () => exportExcel(exportButton);
    if (perf.filters.tab === "board") {
      if (!perf.analyticsLoading && $("#perf-board")) PerfDashboard.bind($("#perf-board"));
      bindBoardLinks();
    } else if (perf.filters.tab === "creators") bindCreators();
    else if (perf.filters.tab === "photos") { if ($("#perf-photos")) PerfDashboard.bind($("#perf-photos")); }
    else bindResults();
  }

  async function uploadExports(files) {
    const paths = [];
    for (const file of files) {
      if (!/\.xlsx$/i.test(file.name)) throw new Error(`${file.name} 不是 .xlsx 表格`);
      const response = await fetch(`/api/upload?kind=performance&name=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "X-Workbench-Token": state.token, "Content-Type": "application/octet-stream" }, body: file,
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "上传失败");
      paths.push(result.path);
    }
    return api("/api/performance/import", { method: "POST", body: { paths, shop: perf.filters.shop } });
  }

  async function runImport(action, button) {
    const hash = pageHash();
    button.disabled = true;
    const box = $("#perf-import-result");
    box.className = "context-note";
    box.textContent = "正在导入…";
    try {
      const result = await action();
      await load(true);
      if (pageHash() !== hash) return;
      render();
      const note = $("#perf-import-result");
      const imported = result.imported.length, failed = result.failed || [];
      note.className = failed.length || result.missing_sources?.length ? "data-warning" : "context-note";
      note.innerHTML = `导入 ${imported} 个文件${result.skipped ? `，${result.skipped} 个未变化已跳过` : ""}。` +
        (result.missing_sources?.length ? `找不到文件夹：${result.missing_sources.map(esc).join("、")}。` : "") +
        failed.map((f) => `<br>未导入 ${esc(f.file)}：${esc(f.error)}`).join("");
    } catch (error) {
      box.className = "data-warning";
      box.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  }

  function reviewMaterial() {
    const s = perf.summary, weeks = s.weeks.filter((w) => w.complete), latest = weeks.at(-1);
    const scopedVideos = perf.videos.filter(v => productMatches(v.product) && (!perf.filters.account || v.account === perf.filters.account));
    const judged = scopedVideos.filter((v) => v.window_end && latest && v.window_end >= addCalendarDays(latest.week, -7) && v.window_end <= addCalendarDays(latest.week, 6));
    const line = (v) => `- ${v.posted_on} ${v.account}｜${v.headline}｜${v.product}｜首两周播放 ${count(v.window_views)}｜累计 GMV ${money(v.gmv)}｜CTOR ${pct(v.ctor_pct)}｜${reviewStatus(v).label}${v.case_id ? `｜案例 ${v.case_id}` : ""}`;
    const top = [...scopedVideos].filter((v) => v.this_week_gmv > 0).sort((a, b) => b.this_week_gmv - a.this_week_gmv).slice(0, 10);
    return [
      `# ${shopNames[perf.filters.shop]}带货数据复盘材料（${latest ? `${latest.week} 这一周` : "暂无完整周"}）`,
      ...(currentProduct() || perf.filters.account ? ["", `范围：${[currentProduct(), perf.filters.account].filter(Boolean).join(" · ")}（全店每周汇总未混入本范围）`] : ["", "## 每周走势（最近 6 个完整周）", ...weeks.slice(-6).map((w) => `- ${w.week} 起：GMV ${money(w.gmv)}，播放 ${count(w.views)}，售出 ${count(w.items)}，新发 ${w.posted} 条`)]),
      "", "## 本周 GMV 前 10 的视频", ...top.map((v) => `${line(v)}｜本周 GMV ${money(v.this_week_gmv)}`),
      "", "## 本周刚满两周观察期的新视频", ...(judged.length ? statusOrder.flatMap((k) => judged.filter((v) => reviewStatus(v).key === k).map(line)) : ["- 无"]),
      "", "参考价值采用人工复盘分类；未复盘显示待复盘，已复盘但未分类显示待分类。",
      "数据只说明哪些视频表现好，不证明原因；请区分观察与推测，给出下一轮只改一个变量的测试建议。",
    ].join("\n");
  }

  // ---- cases ------------------------------------------------------------
  function setField(name, value, onlyIfEmpty = false) {
    const field = $(`[name="${name}"]`);
    if (!field || value == null || value === "" || (onlyIfEmpty && field.value.trim())) return;
    field.value = value;
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function fillEditor(v, isNew) {
    const handle = v.account ? "@" + v.account.replace(/^@/, "") : "";
    if (isNew) {
      setField("title", v.headline);
      setField("product", v.products || v.product);
      const productLabel = $(".quick-create-product strong");
      if (productLabel) productLabel.textContent = $('[name="product"]')?.value || "";
      setField("content_origin", "self_produced");
      setField("url", `https://www.tiktok.com/@${v.account || "_"}/video/${v.video_id}`);
      setField("note", "从后台视频数据收录");
    }
    setField("metric_video_id", v.video_id);
    setField("metric_creator_handle", handle, true);
    setField("metric_published_title", v.title, true);
    setField("publish_published_on", v.posted_on, true);
    if (state.editor) state.editor.dirty = true;
  }

  function collect(videoId, selected = null) {
    const video = selected || perf.videos.find((v) => v.video_id === videoId);
    if (!video || !openEditor(null)) return;
    fillEditor(video, true);
    if (state.editor) state.editor.autoBreakdown = video.video_id;
    window.CaseDirectionPicker.inheritNotice(video, perf.catalog, $("#edit-form"));
    toast("已关联销售数据表中的视频，补上一句话结论后保存。");
  }

  async function route() {
    const hash = pageHash(), product = currentProduct(), sequence = ++perf.routeSequence;
    // Normalizing a remembered activity changes the URL without starting another route.
    const isCurrent = () => sequence === perf.routeSequence && currentProduct() === product && pageHash().startsWith("#videos");
    const restored = window.WorkbenchShell?.listState?.("videos", currentProduct());
    if (restored?.filters) {
      for (const key of Object.keys(filterDefaults)) {
        if (typeof restored.filters[key] === typeof filterDefaults[key]) perf.filters[key] = restored.filters[key];
      }
      if (!Object.hasOwn(restored.filters, "list_period") && restored.filters.tab === "list") perf.filters.list_period = restored.filters.period || "all";
      if (!Object.hasOwn(restored.filters, "board_period") && restored.filters.tab !== "list") perf.filters.board_period = restored.filters.period || "all";
    }
    if (restored?.filters && restored.review_status_filter_version !== 1) perf.filters.status = "";
    if (restored && Number.isFinite(restored.shown)) perf.shown = Math.max(200, Math.trunc(restored.shown));
    perf.filters.product = currentProduct();
    const linked = new URLSearchParams(hash.split("?")[1] || "");
    for (const key of ["shop", "q", "account", "sort", "dir", "period", "metric", ...Object.keys(window.PerfCreators.defaults)]) {
      if (linked.has(key)) perf.filters[key] = linked.get(key);
    }
    if (linked.has("review_status")) perf.filters.status = linked.get("review_status");
    else if (linked.has("status")) perf.filters.status = "";
    if (tabs.some(([key]) => key === linked.get("tab"))) perf.filters.tab = linked.get("tab");
    if (linked.get("video")) perf.filters.tab = "list";
    if (!restored) {
      const campaignKey = perf.filters.tab === "list" ? "campaign" : "board_campaign";
      const alternateKey = campaignKey === "campaign" ? "board_campaign" : "campaign";
      if (linked.has(campaignKey) || linked.has(alternateKey)) window.Campaigns?.select?.(linked.get(campaignKey) ?? linked.get(alternateKey));
      else if (perf.filters.tab !== "list" && linked.has("period")) {
        window.Campaigns?.select?.("");
        perf.filters.campaign = perf.filters.board_campaign = "";
      }
    }
    if (linked.has("period")) perf.filters[perf.filters.tab === "list" ? "list_period" : "board_period"] = linked.get("period");
    if (!(perf.filters.shop in shopNames)) perf.filters.shop = "local";
    // Data read for another shop (a link or the restored list state names a different one) is dropped before the page draws.
    if (perf.requestedShop && perf.requestedShop !== perf.filters.shop) resetData();
    if (!(perf.filters.sort in sortColumns)) perf.filters.sort = "posted";
    if (!["asc", "desc"].includes(perf.filters.dir)) perf.filters.dir = "desc";
    if (perf.filters.status && !statusOrder.includes(perf.filters.status)) perf.filters.status = "";
    if (!(perf.filters.cshow in PerfCreators.showModes)) perf.filters.cshow = "sold";
    if (!PerfCreators.validSort(perf.filters.csort)) perf.filters.csort = "gmv";
    if (!["asc", "desc"].includes(perf.filters.cdir)) perf.filters.cdir = "desc";
    perf.creatorsError = "";
    if (window.PerfDashboard && !window.PerfDashboard.validPeriod(perf.filters.list_period)) perf.filters.list_period = "all";
    if (window.PerfDashboard && !window.PerfDashboard.validPeriod(perf.filters.board_period)) perf.filters.board_period = "all";
    if (!tabs.some(([key]) => key === perf.filters.tab)) perf.filters.tab = "board";
    if (window.PerfDashboard && !window.PerfDashboard.validPeriod(perf.filters.period)) perf.filters.period = "all";
    if (window.PerfDashboard && !(perf.filters.metric in window.PerfDashboard.metrics)) perf.filters.metric = "gmv";
    const videoId = linked.get("video");
    perf.pendingVideo = videoId || "";
    if (videoId) {
      Object.assign(perf.filters, { q: "", account: "", status: "", tab: "list" });
      if (!restored) perf.shown = 200;
    }
    normalizeCampaignFilter();
    saveFilters();
    syncFilterURL(Boolean(videoId));
    $("#breadcrumb").innerHTML = "带货数据";
    if (!perf.videos || (!$("#perf-board") && !$("#perf-results") && !$("#perf-creators") && !$("#perf-photos"))) $("#content").innerHTML = skeletonMarkup(3);
    try {
      const loading = load();
      let needsAnalytics = false, needsCatalog = false;
      const firstPaint = perf.coreLoading.then(() => {
        if (!isCurrent()) return;
        needsAnalytics = perf.analyticsLoading;
        needsCatalog = perf.catalogLoading;
        render();
        if (perf.pendingVideo) { const id = perf.pendingVideo; perf.pendingVideo = ""; openVideo(id); }
      });
      // Attach both rejection handlers immediately, including when the core read fails first.
      // Charts and direction labels enrich the mounted page without delaying its route completion.
      const enrich = [
        Promise.all([firstPaint,perf.analyticsReady]).then(() => {if (needsAnalytics && isCurrent() && perf.filters.tab === "board") refresh();}),
        Promise.all([firstPaint,perf.catalogReady]).then(() => {if (needsCatalog && isCurrent() && perf.filters.tab === "list" && !window.CaseDirectionPicker.hasVideoEdits?.()) rerenderResults();}),
      ];
      void Promise.allSettled([loading,...enrich]);
      await firstPaint;
    } catch (error) {
      if (!isCurrent()) return;
      $("#content").innerHTML = `<div class="empty">${icon("chart")}<h3>带货数据暂不可用</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  const closeMenus = (event) => $$(".perf-menu[open]").forEach((menu) => { if (event.key === "Escape" || !menu.contains(event.target)) menu.open = false; });
  window.addEventListener("click", closeMenus, true);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenus(event); });
  window.addEventListener("hashchange", cancelSearch);

  window.addEventListener('focus',refreshOpenVideoFacts);
  document.addEventListener('visibilitychange',() => {if (!document.hidden) refreshOpenVideoFacts();});

  window.CasePerformance = { publicStatsChanged, route, captureListState, leaveList: cancelSearch, reviewMaterial: () => reviewMaterial(), openVideoHash, openVideoInPlace, campaignContext };
})();
