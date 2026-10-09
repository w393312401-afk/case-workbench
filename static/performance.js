"use strict";

// TikTok Shop video data: imported exports, follow-up status and case links.
(() => {
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
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
  const perf = {
    videos: null, summary: null, analytics: null, catalog: null, loading: null, shown: 200,
    filters: { q: "", account: "", product: "", status: "", range: "recent", sort: "posted", dir: "desc", tab: "board", period: "all" },
  };
  try {
    Object.assign(perf.filters, JSON.parse(sessionStorage.getItem("video-data-filters") || "{}"));
  } catch {
    /* Filters fall back to defaults without browser storage. */
  }

  const statusNames = { focus: "重点复制", follow: "跟进", watch: "观察", drop: "放弃", pending: "待数据", incomplete: "数据不全" };
  const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US"));
  const count = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null ? "—" : v.toFixed(2) + "%");
  const badge = (v) => `<span class="perf-status perf-${v.status}" title="${esc(statusHints[v.status])}">${esc(v.status_label)}</span>`;
  async function load(force = false) {
    if (perf.loading && !force) return perf.loading;
    perf.loading = Promise.all([api("/api/performance/summary"), api("/api/performance/videos"), api("/api/performance/analytics").catch(() => null), window.CaseDirectionPicker.fetchCatalog().catch(() => null)]).then(([summary, data, analytics, catalog]) => {
      perf.summary = summary;
      perf.videos = data.videos;
      window.WorkbenchShell?.registerProducts(perf.videos.map(video => video.product));
      perf.analytics = analytics;
      perf.catalog = catalog;
    });
    try {
      await perf.loading;
    } finally {
      perf.loading = null;
    }
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
    return `<div class="perf-table-wrap"><table class="perf-table"><thead><tr>${th("posted")}<th>账号</th><th>标题</th><th>产品</th><th>测试方向</th>${th("window", "num")}${th("status")}${th("views", "num")}${th("items", "num")}${th("gmv", "num")}${th("ctor", "num")}${th("week", "num")}${th("reviews")}<th>案例</th></tr></thead><tbody>${visible.map((v) => `<tr>
      <td>${esc(v.posted_on || "—")}</td><td>${esc(v.account || "—")}</td>
      <td class="perf-title" title="${esc(v.title || "")}"><a href="https://www.tiktok.com/@${encodeURIComponent(v.account || "_")}/video/${esc(v.video_id)}" target="_blank" rel="noopener noreferrer">${esc(v.headline || v.video_id)}</a><button type="button" class="subtle" data-video-hook="${esc(v.video_id)}">摘录钩子</button></td>
      <td>${esc(v.product)}</td>${window.CaseDirectionPicker.videoCell(v, perf.catalog)}<td class="num">${v.window_views == null ? "—" : count(v.window_views)}</td><td>${badge(v)}</td>
      <td class="num">${count(v.views)}</td><td class="num">${count(v.items)}</td><td class="num">${money(v.gmv)}</td><td class="num">${pct(v.ctor_pct)}</td><td class="num">${v.this_week_gmv ? money(v.this_week_gmv) : "—"}</td>
      <td class="perf-reviews">${v.review_comments ? `<a class="perf-case-link" href="#reviews?video=${esc(v.video_id)}" title="在用户评论库里只看这条视频下的评论">${count(v.review_comments)} 条</a>${v.review_open ? ` <span class="rev-badge rev-badge-open" title="提问没人回复">${count(v.review_open)} 个没人回复</span>` : ""}` : "—"}</td>
      <td>${v.case_id ? `<a class="perf-case-link" href="#case/${esc(v.case_id)}">查看案例</a>` : `<button type="button" class="subtle" data-collect="${esc(v.video_id)}">收为案例</button>`}</td>
    </tr>`).join("")}</tbody></table></div>${rows.length > visible.length ? `<button type="button" class="secondary perf-more" id="perf-more">再显示 ${Math.min(200, rows.length - visible.length)} 条（共 ${rows.length} 条）</button>` : ""}`;
  }

  function render() {
    perf.filters.product = currentProduct();
    const s = perf.summary, f = perf.filters, board = f.tab === "board";
    const rows = board ? [] : filtered();
    const accounts = [...new Set(perf.videos.map((v) => v.account).filter(Boolean))].sort();
    const gaps = s.coverage.gaps.map(([a, b]) => (a === b ? a : `${a} 至 ${b}`)).join("、");
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 视频数据";
    $("#content").innerHTML = `<div class="page-heading"><div><span class="eyebrow">TIKTOK SHOP VIDEO DATA</span><h1>视频数据</h1><p>${s.coverage.first ? `后台导出覆盖 ${s.coverage.first} 至 ${s.coverage.last}${gaps ? `，缺 ${gaps}` : ""}。` : "还没有导入后台数据。"}跟进状态按${esc(s.rule.window)}判断。</p></div><div class="perf-actions"><button type="button" class="secondary" id="perf-copy">${icon("copy")}复制复盘材料</button><button type="button" class="secondary" id="perf-upload">${icon("plus")}导入表格</button><button type="button" class="primary" id="perf-sync">${icon("refresh")}同步数据文件夹</button><input type="file" id="perf-file" accept=".xlsx" multiple hidden></div></div>
      <p class="perf-sources">数据文件夹：${s.sources.length ? s.sources.map((p) => `<code>${esc(p)}</code>`).join("、") : "未设置"} · 已导入 ${s.imports.files} 个文件${s.imports.last ? `，最近一次 ${esc(s.imports.last.replace("T", " "))}` : ""}</p>
      <div id="perf-import-result" role="status"></div>
      <div class="perf-tabs" role="tablist" aria-label="视频数据视图">${[["board", "数据看板"], ["list", "视频明细"]].map(([k, label]) => `<button type="button" role="tab" id="perf-tab-${k}" data-perf-tab="${k}" aria-selected="${k === f.tab}" class="${k === f.tab ? "is-active" : ""}">${label}</button>`).join("")}</div>
      <div class="toolbar perf-toolbar">${board ? "" : `<label class="search">${icon("search")}<input id="perf-q" aria-label="搜索视频标题或 ID" placeholder="搜索标题或视频 ID…" value="${esc(f.q)}"></label>
        <select id="perf-range" aria-label="发布时间">${Object.entries(ranges).map(([k, [label]]) => `<option value="${k}" ${k === f.range ? "selected" : ""}>${label}</option>`).join("")}</select>`}
        ${board ? `<select id="perf-period" aria-label="统计周期">${Object.entries(PerfDashboard.periods).map(([k, [label]]) => `<option value="${k}" ${k === f.period ? "selected" : ""}>${label}</option>`).join("")}</select>` : ""}
        <select id="perf-account" aria-label="账号">${options(accounts, f.account, "全部账号")}</select>
        ${board ? "" : `<select id="perf-status" aria-label="跟进状态"><option value="">全部状态</option>${statusOrder.map((k) => `<option value="${k}" ${k === f.status ? "selected" : ""}>${esc(statusNames[k])}</option>`).join("")}</select>`}
        <button type="button" class="subtle perf-reset" id="perf-reset" hidden>清除筛选</button>
        ${board ? "" : `<span class="result-count">${rows.length} 条</span>`}</div>
      ${board ? `<div id="perf-board" class="perf-board" role="tabpanel" aria-labelledby="perf-tab-board">${PerfDashboard.markup(boardContext())}</div>`
        : `<div id="perf-status-slot">${statusMarkup(filtered(true))}</div><div id="perf-results" role="tabpanel" aria-labelledby="perf-tab-list">${tableMarkup(rows)}</div>`}`;
    bind();
    syncToolbar();
  }

  const boardContext = () => ({ filters: { ...perf.filters, product: currentProduct() }, summary: perf.summary, analytics: perf.analytics, videos: perf.videos });

  // The dashboard redraws as a whole; the list only redraws its table.
  function refresh() {
    if (perf.filters.tab === "board") {
      const board = $("#perf-board");
      board.innerHTML = PerfDashboard.markup(boardContext());
      PerfDashboard.bind(board);
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
  }

  // Controls that narrow the table are outlined, and one button puts everything back.
  function syncToolbar() {
    const f = perf.filters, q = f.q.trim(), board = f.tab === "board";
    const set = (selector, on) => $(selector)?.classList.toggle("is-set", on);
    set("#perf-range", f.range !== "recent");
    set("#perf-period", f.period !== "all");
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
    $("#perf-status").value = perf.filters.status;
    syncToolbar();
    bindResults();
  }

  function bindResults() {
    if ($("#perf-results")) window.CaseDirectionPicker.bindVideos($("#perf-results"), perf.videos, () => perf.catalog, catalog => {perf.catalog = catalog;});
    $$("[data-collect]").forEach((button) => (button.onclick = () => collect(button.dataset.collect)));
    $$("[data-video-hook]").forEach(button => button.onclick = () => {
      const video = perf.videos.find(item => item.video_id === button.dataset.videoHook);
      if (video) window.MaterialBasket?.openHook(video);
    });
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
    let timer;
    $("#perf-q")?.addEventListener("input", (event) => {
      perf.filters.q = event.target.value;
      clearTimeout(timer);
      timer = setTimeout(() => { perf.shown = 200; saveFilters(); rerenderResults(); }, 150);
    });
    [["#perf-range", "range"], ["#perf-period", "period"], ["#perf-account", "account"], ["#perf-status", "status"]].forEach(([selector, key]) =>
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
      PerfDashboard.bind($("#perf-board"));
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
    const hash = pageHash();
    perf.filters.product = currentProduct();
    const videoId = new URLSearchParams(hash.split("?")[1] || "").get("video");
    if (videoId) {
      Object.assign(perf.filters, { q: videoId, account: "", status: "", range: "all", tab: "list" });
      perf.shown = 200;
    }
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 视频数据";
    if (!perf.videos) $("#content").innerHTML = skeletonMarkup(3);
    try {
      await load(true);
      if (pageHash() === hash && hash.startsWith("#videos")) render();
    } catch (error) {
      if (pageHash() !== hash) return;
      $("#content").innerHTML = `<div class="empty">${icon("chart")}<h3>视频数据暂不可用</h3><p>${esc(error.message)}</p></div>`;
    }
  }

  window.CasePerformance = { route, reviewMaterial: () => reviewMaterial() };
})();
