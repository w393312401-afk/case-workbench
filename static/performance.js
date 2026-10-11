"use strict";

// TikTok Shop video data: imported exports, follow-up status and case links.
(() => {
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const currentProduct = () => window.WorkbenchShell ? window.WorkbenchShell.product() : perf.filters.product;
  const productMatches = value => window.WorkbenchShell ? window.WorkbenchShell.matchesProduct(value) : !currentProduct() || String(value || "").toLowerCase().includes(currentProduct().toLowerCase());
  const statusOrder = ["focus", "follow", "watch", "pending", "drop", "incomplete"];
  const statusHints = {
    focus: "首两周播放 ≥10,000：历史上 91% 在 30 天内出单，优先做同题材变体",
    follow: "首两周播放 3,000–10,000：约 70% 出单，继续观察并考虑变体",
    watch: "首两周播放 1,000–3,000：出单率 22–46%",
    drop: "首两周播放 <1,000：历史上 30 天内几乎不出单",
    pending: "发布当周和下一周的数据还没导入完",
    incomplete: "发布期间的数据没有覆盖到（早期视频或导出缺口）",
  };
  const ranges = { recent: ["近 5 周发布", 35], quarter: ["近 3 个月发布", 92], tracked: ["4 月以后发布", null], all: ["全部视频", null] };
  // Sortable table columns: [header label, sort value (null sorts last), first-click direction].
  const sortColumns = {
    posted: ["发布", (v) => v.posted_at || null, "desc"],
    window: ["首两周播放", (v) => v.window_views, "desc"],
    status: ["状态", (v) => statusOrder.indexOf(v.status), "asc"],
    views: ["累计播放", (v) => v.views, "desc"],
    items: ["售出", (v) => v.items, "desc"],
    gmv: ["GMV", (v) => v.gmv || 0, "desc"],
    ctor: ["CTOR", (v) => v.ctor_pct, "desc"],
    week: ["本周 GMV", (v) => v.this_week_gmv || 0, "desc"],
    reviews: ["评论", (v) => v.review_comments || 0, "desc"],
  };
  const filterDefaults = { q: "", account: "", product: "", status: "", range: "recent", sort: "posted", dir: "desc", tab: "board", period: "all", metric: "gmv" };
  const perf = {
    videos: null, summary: null, analytics: null, catalog: null, loading: null, coreLoading: null, analyticsReady: null, catalogReady: null, loadSequence: 0, routeSequence: 0, analyticsLoading: false, catalogLoading: false, shown: 200,
    filters: { ...filterDefaults },
    openVideo: "", renderedProduct: null,
  };
  try {
    Object.assign(perf.filters, JSON.parse(sessionStorage.getItem("video-data-filters") || "{}"));
  } catch {
    /* Filters fall back to defaults without browser storage. */
  }

  let searchTimer = null;
  function cancelSearch() {
    clearTimeout(searchTimer);
    searchTimer = null;
  }
  function captureListState() {
    if (!$("#perf-board") && !$("#perf-results")) return null;
    const filters = { ...perf.filters };
    const field = $("#perf-q");
    if (field) filters.q = field.value;
    const query = new URLSearchParams();
    for (const key of Object.keys(filterDefaults)) {
      if (key !== "product" && filters[key] && filters[key] !== filterDefaults[key]) query.set(key, filters[key]);
    }
    return { product: perf.renderedProduct, filters, shown: perf.shown, hash: pageURL(`#videos${query.size ? `?${query}` : ""}`) };
  }

  const statusNames = { focus: "重点复制", follow: "跟进", watch: "观察", drop: "放弃", pending: "待数据", incomplete: "数据不全" };
  const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US"));
  const count = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null ? "—" : v.toFixed(2) + "%");
  const badge = (v) => `<span class="perf-status perf-${v.status}" title="${esc(statusHints[v.status])}">${esc(v.status_label)}</span>`;
  function load(force = false) {
    if (perf.loading && !force) return perf.loading;
    const sequence = ++perf.loadSequence;
    perf.analyticsLoading = true;
    perf.catalogLoading = true;
    // The table and toolbar need only these two reads; the chart and direction catalog follow independently.
    const core = Promise.all([api("/api/performance/summary"), api("/api/performance/videos")]).then(([summary, data]) => {
      if (sequence !== perf.loadSequence) return;
      perf.summary = summary;
      perf.videos = data.videos;
      window.WorkbenchShell?.registerProducts(perf.videos.map(video => video.product));
    });
    const analytics = api("/api/performance/analytics").catch(() => null).then(data => {
      if (sequence !== perf.loadSequence) return;
      perf.analytics = data;
      perf.analyticsLoading = false;
    });
    const catalog = window.CaseDirectionPicker.fetchCatalog().catch(() => null).then(data => {
      if (sequence !== perf.loadSequence) return;
      perf.catalog = data;
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
  function filtered(ignoreStatus = false, ignoreProduct = false) {
    const f = perf.filters, last = perf.summary?.coverage?.last;
    const days = ranges[f.range]?.[1];
    const since = days && last ? addCalendarDays(last, -days) : f.range === "tracked" ? perf.summary?.coverage?.first : null;
    const q = f.q.trim().toLowerCase();
    const rows = perf.videos.filter((v) =>
      (!since || (v.posted_on || "") >= since) &&
      (!f.account || v.account === f.account) &&
      (ignoreProduct || productMatches(v.product)) &&
      (ignoreStatus || !f.status || v.status === f.status) &&
      (!q || `${v.title} ${v.video_id}`.toLowerCase().includes(q)));
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

  // Follow-up status of the videos matching every filter except status; a click narrows the table to one status.
  function statusMarkup(rows) {
    // Ordered strongest to weakest, then the two "not yet judged" states in gray.
    const tally = ["focus", "follow", "watch", "drop", "pending", "incomplete"].map((k) => [k, rows.filter((v) => v.status === k).length]);
    const total = rows.length, active = perf.filters.status;
    const share = (n) => (total ? `${Math.round((n / total) * 100)}%` : "—");
    const segments = tally.filter(([, n]) => n).map(([k, n]) => `<button type="button" class="perf-seg perf-swatch-${k}${active && active !== k ? " is-muted" : ""}" data-status-filter="${k}" data-count="${n}" title="${esc(statusNames[k])} ${n} 条（${share(n)}）：${esc(statusHints[k])}" aria-label="只看${esc(statusNames[k])}，${n} 条"></button>`).join("");
    const legend = tally.map(([k, n]) => `<button type="button" class="perf-legend-item${active === k ? " is-active" : ""}${active && active !== k ? " is-muted" : ""}" data-status-filter="${k}" aria-pressed="${active === k}" title="${esc(statusHints[k])}"><i class="perf-swatch perf-swatch-${k}"></i><span>${esc(statusNames[k])}</span><strong>${n}</strong><small>${share(n)}</small></button>`).join("");
    return `<section class="section-card perf-status-card"><div class="perf-card-head"><h2>跟进状态</h2><span>当前筛选 ${total} 条视频${active ? ` · 表格只显示「${esc(statusNames[active])}」，再点一次取消` : " · 点击状态筛选表格"}</span></div>
      ${total ? `<div class="perf-status-bar" role="group" aria-label="跟进状态分布">${segments}</div>` : ""}<div class="perf-status-legend">${legend}</div></section>`;
  }

  function tableMarkup(rows) {
    if (!rows.length) return `<div class="empty">${icon("search")}<h3>没有符合条件的视频</h3><p>换个筛选条件，或先同步最新的后台导出。</p></div>`;
    const visible = rows.slice(0, perf.shown);
    const th = (key, cls = "") => {
      const active = perf.filters.sort === key, asc = perf.filters.dir === "asc", label = sortColumns[key][0];
      return `<th class="${cls}" aria-sort="${active ? (asc ? "ascending" : "descending") : "none"}"><button type="button" class="perf-sort${active ? " is-active" : ""}" data-sort="${key}" title="按${esc(label)}排序">${esc(label)}<span class="perf-sort-arrow" aria-hidden="true">${active ? (asc ? "↑" : "↓") : "↕"}</span></button></th>`;
    };
    return `<div class="perf-table-wrap"><table class="perf-table perf-video-table"><thead><tr>${th("posted", "perf-col-video")}<th>测试方向</th>${th("status")}${th("window", "num")}${th("views", "num")}${th("items", "num")}${th("gmv", "num")}${th("ctor", "num")}</tr></thead><tbody>${visible.map((v) => `<tr data-video-row="${esc(v.video_id)}" tabindex="0" aria-label="查看 ${esc(v.headline || v.video_id)} 的详情"${perf.openVideo === v.video_id ? ' class="is-open"' : ""}>
      <td class="perf-title" title="${esc(v.title || "")}"><strong>${esc(v.headline || v.video_id)}</strong><small>${esc(v.account || "账号未记录")} · ${esc(v.posted_on || "—")}${v.case_id ? " · 已收为案例" : ""}${v.review_comments ? ` · ${count(v.review_comments)} 条评论` : ""}</small></td>
      ${window.CaseDirectionPicker.videoCell(v, perf.catalog)}<td>${badge(v)}</td><td class="num">${v.window_views == null ? "—" : count(v.window_views)}</td>
      <td class="num">${count(v.views)}</td><td class="num">${count(v.items)}</td><td class="num">${money(v.gmv)}</td><td class="num">${pct(v.ctor_pct)}</td>
    </tr>`).join("")}</tbody></table></div>${rows.length > visible.length ? `<button type="button" class="secondary perf-more" id="perf-more">再显示 ${Math.min(200, rows.length - visible.length)} 条（共 ${rows.length} 条）</button>` : ""}`;
  }

  // Everything about one video that the table leaves out, with its actions.
  function videoDetail(v) {
    const link = `https://www.tiktok.com/@${encodeURIComponent(v.account || "_")}/video/${encodeURIComponent(v.video_id)}`;
    const fact = (label, value) => `<dt>${esc(label)}</dt><dd>${value}</dd>`;
    const hooks = window.MaterialBasket?.count({ type: "hook", source_id: v.video_id }) || 0;
    return `<div class="rev-drawer-head"><h2 id="perf-video-title">${esc(v.headline || v.video_id)}</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭视频详情">${icon("close")}</button></div>
      <div class="rev-drawer-body perf-video-body"><p class="perf-video-meta">${esc(v.account || "账号未记录")} · 发布 ${esc(v.posted_on || "—")} · ${esc(v.product || "产品未记录")}</p>
        <section class="perf-video-status">${badge(v)}<p>${esc(statusHints[v.status] || "")}${v.window_end ? `（观察窗到 ${esc(v.window_end)}）` : ""}</p></section>
        <dl class="perf-video-facts">${fact("首两周播放", v.window_views == null ? "—" : count(v.window_views))}${fact("累计播放", count(v.views))}${fact("曝光", count(v.impressions))}${fact("商品点击", count(v.clicks))}${fact("点击率", pct(v.ctr_pct))}${fact("订单", count(v.orders))}${fact("售出件数", count(v.items))}${fact("归因 GMV", money(v.gmv))}${fact("GPM", v.gpm == null ? "—" : "$" + v.gpm.toFixed(2))}${fact("CTOR", pct(v.ctor_pct))}${fact("本周 GMV", v.this_week_gmv ? money(v.this_week_gmv) : "—")}${fact("数据覆盖", v.first_data ? `${esc(v.first_data)} 至 ${esc(v.last_data || "—")}` : "—")}</dl>
        <section class="perf-video-section"><h3>用户评论</h3><p class="perf-reviews">${v.review_comments ? `<a class="perf-case-link" href="#reviews?video=${esc(v.video_id)}">${count(v.review_comments)} 条</a>${v.review_open ? ` <span class="rev-badge rev-badge-open" title="提问没人回复">${count(v.review_open)} 个没人回复</span>` : ""}` : "评论库里还没有这条视频的评论。"}</p></section>
        <section class="perf-video-section"><h3>案例与素材</h3><div class="perf-video-actions">${v.case_id ? `<a class="secondary compact perf-case-link" href="#case/${esc(v.case_id)}">查看案例</a>` : `<button type="button" class="secondary compact" data-collect="${esc(v.video_id)}">收为案例</button>`}<button type="button" class="secondary compact" data-video-hook="${esc(v.video_id)}">${hooks ? `摘录钩子 · 已有 ${hooks}` : "摘录钩子"}</button><a class="subtle" href="${esc(link)}" target="_blank" rel="noopener noreferrer">${icon("link")}在 TikTok 打开</a></div></section>
        <p class="perf-video-id">视频 ID <code>${esc(v.video_id)}</code></p></div>`;
  }
  function openVideo(id) {
    const v = perf.videos?.find((item) => item.video_id === id), drawer = $("#perf-video-drawer");
    if (!v || !drawer) return;
    perf.openVideo = id;
    drawer.innerHTML = videoDetail(v);
    $$("[data-video-row]").forEach((row) => row.classList.toggle("is-open", row.dataset.videoRow === id));
    $("[data-collect]", drawer)?.addEventListener("click", () => {drawer.close();collect(id);});
    $("[data-video-hook]", drawer).onclick = () => window.MaterialBasket?.openHook(v);
    if (!drawer.open) drawer.showModal();
  }

  function render() {
    cancelSearch();
    perf.filters.product = currentProduct();
    const s = perf.summary, f = perf.filters, board = f.tab === "board";
    const rows = board ? [] : filtered();
    const accounts = [...new Set(perf.videos.map((v) => v.account).filter(Boolean))].sort();
    const gaps = s.coverage.gaps.map(([a, b]) => (a === b ? a : `${a} 至 ${b}`)).join("、");
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 视频数据";
    $("#content").innerHTML = `<div class="page-heading perf-heading"><div><h1>视频数据</h1><p>${s.coverage.first ? `后台导出覆盖 ${s.coverage.first} 至 ${s.coverage.last}${gaps ? `，缺 ${gaps}` : ""}。` : "还没有导入后台数据。"}跟进状态按${esc(s.rule.window)}判断。</p></div>
      <div class="perf-actions"><button type="button" class="secondary" id="perf-upload">${icon("plus")}导入表格</button><details class="perf-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" id="perf-copy">复制复盘材料</button><button type="button" role="menuitem" id="perf-sync">同步数据文件夹</button></div></details><input type="file" id="perf-file" accept=".xlsx" multiple hidden></div></div>
      <details class="perf-about"><summary>数据来源与口径</summary><p>数据文件夹：${s.sources.length ? s.sources.map((p) => `<code>${esc(p)}</code>`).join("、") : "未设置"} · 已导入 ${s.imports.files} 个文件${s.imports.last ? `，最近一次 ${esc(s.imports.last.replace("T", " "))}` : ""}。</p><p>视频指标是后台导出里逐条视频的归因数据加总；店铺每日汇总来自另一份导出，口径更大，单独展示不与视频明细相加。点击率 = 商品点击 ÷ 播放，CTOR = 订单 ÷ 商品点击，GPM = 每千次播放的归因 GMV。${gaps ? `导出缺口：${esc(gaps)}，缺口所在周在走势里以短横表示。` : ""}</p></details>
      <div id="perf-import-result" role="status"></div>
      <div class="perf-tabs" role="tablist" aria-label="视频数据视图">${[["board", "数据看板"], ["list", "视频明细"]].map(([k, label]) => `<button type="button" role="tab" id="perf-tab-${k}" data-perf-tab="${k}" aria-selected="${k === f.tab}" class="${k === f.tab ? "is-active" : ""}">${label}</button>`).join("")}</div>
      <div class="toolbar perf-toolbar">${board ? `<div class="perf-segment" role="group" aria-label="统计周期">${Object.entries(PerfDashboard.periods).map(([k, [label]]) => `<button type="button" data-perf-period="${k}" aria-pressed="${k === f.period}">${label}</button>`).join("")}</div>`
        : `<label class="search">${icon("search")}<input id="perf-q" aria-label="搜索视频标题或 ID" placeholder="搜索标题或视频 ID…" value="${esc(f.q)}"></label>
        <select id="perf-range" aria-label="发布时间">${Object.entries(ranges).map(([k, [label]]) => `<option value="${k}" ${k === f.range ? "selected" : ""}>${label}</option>`).join("")}</select>`}
        <select id="perf-account" aria-label="账号">${options(accounts, f.account, "全部账号")}</select>
        <button type="button" class="subtle perf-reset" id="perf-reset" hidden>清除筛选</button>
        ${board ? "" : `<span class="result-count">${rows.length} 条</span>`}</div>
      ${board ? `<div id="perf-board" class="perf-board" role="tabpanel" aria-labelledby="perf-tab-board">${boardMarkup()}</div>`
        : `<div id="perf-status-slot">${statusMarkup(filtered(true))}</div><div id="perf-results" role="tabpanel" aria-labelledby="perf-tab-list">${tableMarkup(rows)}</div>`}
      <dialog id="perf-video-drawer" class="rev-drawer perf-video-drawer" aria-labelledby="perf-video-title"></dialog>`;
    perf.renderedProduct = currentProduct();
    bind();
    syncToolbar();
  }

  const boardContext = () => ({ filters: { ...perf.filters, product: currentProduct() }, summary: perf.summary, analytics: perf.analytics, videos: perf.videos });
  const boardMarkup = () => perf.analyticsLoading ? '<p class="context-note" role="status">正在读取看板数据…</p>' : PerfDashboard.board(boardContext());

  // The dashboard redraws as a whole; the list only redraws its table.
  function refresh() {
    if (perf.filters.tab === "board") {
      const board = $("#perf-board");
      board.innerHTML = boardMarkup();
      if (!perf.analyticsLoading) PerfDashboard.bind(board);
      bindBoardLinks();
      syncToolbar();
    } else rerenderResults();
  }

  function setTab(tab) {
    if (perf.filters.tab === tab) return;
    perf.filters.tab = tab;
    saveFilters();
    render();
    window.scrollTo({ top: 0 });
  }

  function bindBoardLinks() {
    $$("#perf-board [data-perf-tab]").forEach((button) => (button.onclick = () => setTab(button.dataset.perfTab)));
    $$("#perf-board [data-perf-metric]").forEach((button) => (button.onclick = () => { perf.filters.metric = button.dataset.perfMetric; saveFilters(); refresh(); }));
    $$("#perf-board [data-perf-video]").forEach((button) => (button.onclick = () => openVideo(button.dataset.perfVideo)));
  }

  // Controls that narrow the table are outlined, and one button puts everything back.
  function syncToolbar() {
    const f = perf.filters, q = f.q.trim(), board = f.tab === "board";
    const set = (selector, on) => $(selector)?.classList.toggle("is-set", on);
    set("#perf-range", f.range !== "recent");
    $$("[data-perf-period]").forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.perfPeriod === f.period)));
    set("#perf-account", !!f.account);
    set("#perf-status", !!f.status);
    set(".perf-toolbar .search", !!q);
    // Only the controls on screen count: the dashboard ignores the list's own range, status and search.
    $("#perf-reset").hidden = !(f.account || (board ? f.period !== "all" : f.range !== "recent" || f.status || q));
  }

  function saveFilters() {
    perf.filters.product = currentProduct();
    try {
      sessionStorage.setItem("video-data-filters", JSON.stringify(perf.filters));
    } catch {}
  }

  function rerenderResults() {
    const rows = filtered();
    $("#perf-results").innerHTML = tableMarkup(rows);
    $(".perf-toolbar .result-count").textContent = `${rows.length} 条`;
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
    $$("[data-perf-period]").forEach((button) => (button.onclick = () => { perf.filters.period = button.dataset.perfPeriod; saveFilters(); refresh(); }));
    const drawer = $("#perf-video-drawer");
    drawer.addEventListener("click", (event) => { if (event.target === drawer || event.target.closest("[data-drawer-close]")) drawer.close(); });
    drawer.addEventListener("close", () => { perf.openVideo = ""; $$("[data-video-row].is-open").forEach((row) => row.classList.remove("is-open")); });
    $(".perf-menu")?.addEventListener("click", (event) => { if (event.target.closest("[role=menuitem]")) event.currentTarget.open = false; });
    [["#perf-range", "range"], ["#perf-account", "account"]].forEach(([selector, key]) =>
      $(selector)?.addEventListener("change", (event) => { perf.filters[key] = event.target.value; perf.shown = 200; saveFilters(); refresh(); }));
    $$(".perf-tabs [data-perf-tab]").forEach((button) => (button.onclick = () => setTab(button.dataset.perfTab)));
    $("#perf-reset").onclick = () => {
      Object.assign(perf.filters, { account: "" }, perf.filters.tab === "board" ? { period: "all" } : { q: "", status: "", range: "recent" });
      perf.shown = 200;
      saveFilters();
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
    if (perf.filters.tab === "board") {
      if (!perf.analyticsLoading) PerfDashboard.bind($("#perf-board"));
      bindBoardLinks();
    } else bindResults();
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
    return api("/api/performance/import", { method: "POST", body: { paths } });
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
    const line = (v) => `- ${v.posted_on} ${v.account}｜${v.headline}｜${v.product}｜首两周播放 ${count(v.window_views)}｜累计 GMV ${money(v.gmv)}｜CTOR ${pct(v.ctor_pct)}｜${v.status_label}${v.case_id ? `｜案例 ${v.case_id}` : ""}`;
    const top = [...scopedVideos].filter((v) => v.this_week_gmv > 0).sort((a, b) => b.this_week_gmv - a.this_week_gmv).slice(0, 10);
    return [
      `# 视频数据复盘材料（${latest ? `${latest.week} 这一周` : "暂无完整周"}）`,
      ...(currentProduct() || perf.filters.account ? ["", `范围：${[currentProduct(), perf.filters.account].filter(Boolean).join(" · ")}（全店每周汇总未混入本范围）`] : ["", "## 每周走势（最近 6 个完整周）", ...weeks.slice(-6).map((w) => `- ${w.week} 起：GMV ${money(w.gmv)}，播放 ${count(w.views)}，售出 ${count(w.items)}，新发 ${w.posted} 条`)]),
      "", "## 本周 GMV 前 10 的视频", ...top.map((v) => `${line(v)}｜本周 GMV ${money(v.this_week_gmv)}`),
      "", "## 本周刚满两周观察期的新视频", ...(judged.length ? statusOrder.flatMap((k) => judged.filter((v) => v.status === k).map(line)) : ["- 无"]),
      "", `跟进规则：${s.rule.window}。${s.rule.thresholds.map(([floor, label]) => `≥${floor.toLocaleString("en-US")} ${label}`).join("；")}。`,
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

  function collect(videoId) {
    const video = perf.videos.find((v) => v.video_id === videoId);
    if (!video || !openEditor(null)) return;
    fillEditor(video, true);
    window.CaseDirectionPicker.inheritNotice(video, perf.catalog, $("#edit-form"));
    toast("已关联销售数据表中的视频，补上一句话结论后保存。");
  }

  async function route() {
    const hash = pageHash(), sequence = ++perf.routeSequence;
    const isCurrent = () => sequence === perf.routeSequence && pageHash() === hash && hash.startsWith("#videos");
    const restored = window.WorkbenchShell?.listState?.("videos", currentProduct());
    if (restored?.filters) {
      for (const key of Object.keys(filterDefaults)) {
        if (typeof restored.filters[key] === typeof filterDefaults[key]) perf.filters[key] = restored.filters[key];
      }
    }
    if (restored && Number.isFinite(restored.shown)) perf.shown = Math.max(200, Math.trunc(restored.shown));
    perf.filters.product = currentProduct();
    const linked = new URLSearchParams(hash.split("?")[1] || "");
    for (const key of ["q", "account", "status", "range", "sort", "dir", "period", "metric"]) {
      if (linked.has(key)) perf.filters[key] = linked.get(key);
    }
    if (!(perf.filters.range in ranges)) perf.filters.range = "recent";
    if (!(perf.filters.sort in sortColumns)) perf.filters.sort = "posted";
    if (!["asc", "desc"].includes(perf.filters.dir)) perf.filters.dir = "desc";
    if (perf.filters.status && !statusOrder.includes(perf.filters.status)) perf.filters.status = "";
    if (!["board", "list"].includes(perf.filters.tab)) perf.filters.tab = "board";
    if (window.PerfDashboard && !(perf.filters.period in window.PerfDashboard.periods)) perf.filters.period = "all";
    if (window.PerfDashboard && !(perf.filters.metric in window.PerfDashboard.metrics)) perf.filters.metric = "gmv";
    const videoId = linked.get("video");
    perf.pendingVideo = videoId || "";
    if (videoId) {
      Object.assign(perf.filters, { q: videoId, account: "", status: "", range: "all", tab: "list" });
      if (!restored) perf.shown = 200;
    }
    if (["board", "list"].includes(linked.get("tab"))) perf.filters.tab = linked.get("tab");
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 视频数据";
    if (!perf.videos || (!$("#perf-board") && !$("#perf-results"))) $("#content").innerHTML = skeletonMarkup(3);
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
      await Promise.all([loading,
        Promise.all([firstPaint,perf.analyticsReady]).then(() => {if (needsAnalytics && isCurrent() && perf.filters.tab === "board") refresh();}),
        Promise.all([firstPaint,perf.catalogReady]).then(() => {if (needsCatalog && isCurrent() && perf.filters.tab !== "board" && !window.CaseDirectionPicker.hasVideoEdits?.()) rerenderResults();}),
      ]);
    } catch (error) {
      if (!isCurrent()) return;
      $("#content").innerHTML = `<div class="empty">${icon("chart")}<h3>视频数据暂不可用</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  const closeMenus = (event) => $$(".perf-menu[open]").forEach((menu) => { if (event.key === "Escape" || !menu.contains(event.target)) menu.open = false; });
  window.addEventListener("click", closeMenus, true);
  window.addEventListener("keydown", (event) => { if (event.key === "Escape") closeMenus(event); });

  window.addEventListener("hashchange", cancelSearch);

  window.CasePerformance = { route, captureListState, leaveList: cancelSearch, reviewMaterial: () => reviewMaterial() };
})();
