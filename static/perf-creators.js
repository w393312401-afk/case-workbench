"use strict";

// Overseas creators (海外达人): the shop's all-creator video exports, summed per export range and per creator.
// Our own and unnamed accounts never count as creators; they only give the creators' share of all video GMV.
(() => {
  const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US"));
  const money2 = (v) => (v == null ? "—" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const count = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null ? "—" : v.toFixed(2) + "%");
  const ratio = (a, b, scale = 1) => (b ? (a * scale) / b : null);
  const share = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "—");
  const blank = (extra = {}) => Object.assign({ views: 0, impressions: 0, clicks: 0, orders: 0, items: 0, gmv: 0 }, extra);
  const add = (t, r) => { t.views += r[2]; t.impressions += r[3]; t.clicks += r[4]; t.orders += r[5]; t.items += r[6]; t.gmv += r[7]; };
  const sets = () => ({ creators: new Set(), sellers: new Set(), videos: new Set(), sold: new Set() });
  // A row "sold" when the export attributes an order or GMV to it in that range.
  const sold = (r) => r[5] > 0 || r[7] > 0;

  const defaults = { cq: "", cshow: "sold", csort: "gmv", cdir: "desc" };
  const showModes = { sold: "出单达人", all: "全部达人" };
  // Sortable creator columns: [header, value]. Period columns (p0, p1…) sort by that range's GMV. First click sorts high to low.
  const sortColumns = {
    sold: ["出单视频", (c) => c.sold.size],
    views: ["播放", (c) => c.views],
    orders: ["订单", (c) => c.orders],
    gmv: ["GMV", (c) => c.gmv],
    gpm: ["GPM", (c) => ratio(c.gmv, c.views, 1000)],
    ctor: ["CTOR", (c) => ratio(c.orders, c.clicks, 100)],
  };
  const sortValue = (key) => sortColumns[key]?.[1] || (/^p\d+$/.test(key) ? (c) => c.byPeriod[Number(key.slice(1))]?.gmv ?? null : null);

  // ---- data ---------------------------------------------------------------
  const prepared = new WeakMap();
  function videosOf(data) {
    if (!prepared.has(data)) prepared.set(data, data.videos.map(([id, account, country, title, posted, products, kind]) => ({ id, account, country, title, posted, products, kind })));
    return prepared.get(data);
  }
  // A video linking several products shows under the shell's product when any of them matches.
  const productOk = (video) => {
    const shell = window.WorkbenchShell;
    return !shell?.product() || video.products.some((name) => shell.matchesProduct(name));
  };

  // Every range keeps its own figures (the monthly table and each creator's months); the chosen range feeds the rest.
  function scope(data, f) {
    // The shared date range picks every export range it touches.
    const videos = videosOf(data), range = window.PerfDashboard.rangeOf(f.period);
    const picked = range ? new Set(data.periods.flatMap((p, i) => (p.start <= range[1] && p.end >= range[0] ? [i] : []))) : null;
    const inScope = (i) => !picked || picked.has(i);
    const periods = data.periods.map((p) => blank({ ...p, ...sets(), all: 0, own: 0, fresh: 0 }));
    const total = blank({ ...sets(), all: 0, own: 0 });
    const creators = new Map(), perVideo = new Map(), products = new Map();
    for (const r of data.rows) {
      const video = videos[r[0]];
      if (!productOk(video)) continue;
      const period = periods[r[1]], hit = inScope(r[1]), sale = sold(r);
      period.all += r[7];
      if (hit) total.all += r[7];
      if (video.kind !== "creator") {
        if (video.kind === "own") { period.own += r[7]; if (hit) total.own += r[7]; }
        continue;
      }
      let c = creators.get(video.account);
      if (!c) creators.set(video.account, (c = blank({ account: video.account, country: video.country, videos: new Set(), sold: new Set(), products: new Map(), firstSold: null,
        byPeriod: data.periods.map(() => blank({ videos: new Set(), sold: new Set() })) })));
      const own = c.byPeriod[r[1]];
      add(own, r); own.videos.add(r[0]);
      add(period, r); period.creators.add(video.account); period.videos.add(r[0]);
      if (sale) {
        own.sold.add(r[0]); period.sellers.add(video.account); period.sold.add(r[0]);
        if (c.firstSold == null || r[1] < c.firstSold) c.firstSold = r[1];
      }
      if (!hit) continue;
      add(c, r); c.videos.add(r[0]);
      add(total, r); total.creators.add(video.account); total.videos.add(r[0]);
      if (!perVideo.has(r[0])) perVideo.set(r[0], blank({ video, index: r[0] }));
      add(perVideo.get(r[0]), r);
      const name = video.products[0];
      c.products.set(name, (c.products.get(name) || 0) + r[7]);
      if (!products.has(name)) products.set(name, blank({ name, ...sets() }));
      const product = products.get(name);
      add(product, r); product.creators.add(video.account); product.videos.add(r[0]);
      if (sale) { c.sold.add(r[0]); total.sellers.add(video.account); total.sold.add(r[0]); product.sellers.add(video.account); product.sold.add(r[0]); }
    }
    // "New" only means new within the imported ranges, so the first range has none.
    creators.forEach((c) => { if (c.firstSold > 0) periods[c.firstSold].fresh++; });
    return { picked, campaignRange: f.board_campaign ? range : null, label: window.PerfDashboard.periodLabel(f.period), periods, total, creators, perVideo, products };
  }

  // A month column chosen before the activity filter must not keep sorting by a hidden historical month.
  const sorting = (S, f) => S.campaignRange && /^p\d+$/.test(f.csort) ? { key: "gmv", dir: "desc" } : { key: f.csort, dir: f.cdir };
  function listed(S, f) {
    const sort = sorting(S, f);
    const q = f.cq.trim().replace(/^@/, "").toLowerCase(), key = sortValue(sort.key) || sortColumns.gmv[1], sign = sort.dir === "asc" ? 1 : -1;
    return [...S.creators.values()].filter((c) => c.videos.size && (f.cshow === "all" || c.sold.size) && (!q || c.account.toLowerCase().includes(q)))
      .sort((a, b) => {
        const x = key(a), y = key(b);
        if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1;
        return x > y ? sign : x < y ? -sign : b.gmv - a.gmv || a.account.localeCompare(b.account);
      });
  }

  const mainProduct = (c) => [...c.products].sort((x, y) => y[1] - x[1])[0]?.[0] || "未标注";
  const shownPeriods = S => S.periods.map((p, i) => ({ p, i })).filter(({i}) => !S.campaignRange || S.picked.has(i));
  const reportSpan = S => shownPeriods(S).map(({p}) => `${p.start} 至 ${p.end}`).join("、");
  const scopeLabel = (S, data) => {
    if (S.campaignRange) return S.picked.size ? `报表 ${reportSpan(S)}` : `${S.label}（无重叠导出）`;
    if (S.picked) return S.picked.size ? S.label : `${S.label}（无导出）`;
    return data.periods.length > 1 ? `${data.periods[0].label}至${data.periods.at(-1).label}` : data.periods[0]?.label || "";
  };
  const coverage = (p) => {
    const [year, month] = p.start.split("-").map(Number), whole = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const short = p.label.endsWith("月") && p.days < whole ? ` <span class="perf-muted">缺 ${whole - p.days} 天</span>` : "";
    return `${esc(p.start.slice(5))} 至 ${esc(p.end.slice(5))}（${p.days} 天）${short}`;
  };
  const productCell = (v) => `<span class="perf-cell-product" title="${esc(v.products.join("、"))}">${esc(v.products.length > 1 ? `${v.products[0]} 等 ${v.products.length} 款` : v.products[0])}</span>`;
  const tiktok = (account, id) => `https://www.tiktok.com/@${encodeURIComponent(account || "_")}${id ? `/video/${encodeURIComponent(id)}` : ""}`;
  const publicVideoColumns = [["公开播放", 1], ["点赞", 1], ["评论", 1], ["转发", 1], ["收藏", 1]];
  const publicVideoCells = (data, id) => ["views", "likes", "comments", "shares", "saves"].map(key => count(data.public_stats?.[id]?.[key]));
  const publicVideoNote = "公开播放与互动来自插件最近一次推送，不随报表区间变化；未推送或未采到的指标显示“—”。";

  // ---- markup ---------------------------------------------------------------
  function table(head, rows, empty = "当前筛选下没有数据") {
    if (!rows.length) return `<p class="perf-muted perf-table-empty">${esc(empty)}</p>`;
    return `<div class="perf-table-wrap"><table class="perf-week-data"><thead><tr>${head.map(([label, num]) => `<th${num ? ' class="num"' : ""}>${esc(label)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell, i) => `<td${head[i][1] ? ' class="num"' : ""}>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }
  const section = (title, hint, body) => `<section class="section-card perf-trend-card"><div class="perf-card-head"><h2>${esc(title)}</h2>${hint ? `<span>${esc(hint)}</span>` : ""}</div>${body}</section>`;
  const fold = (title, hint, body) => `<details class="perf-fold section-card"><summary><span>${esc(title)}</span>${hint ? `<small>${esc(hint)}</small>` : ""}</summary><div class="perf-fold-body">${body}</div></details>`;

  function note(data, S) {
    const files = [...new Set((S?.campaignRange ? shownPeriods(S).map(({p}) => p) : data.periods).flatMap((p) => p.files))];
    const excluded = data.excluded_periods.filter(p => !S?.campaignRange || (p.start <= S.campaignRange[1] && p.end >= S.campaignRange[0])).map((p) => `${p.start} 至 ${p.end}（${p.files.join("、")}）`);
    return `<p class="perf-board-note perf-creator-note"><strong>口径</strong>来自后台全部达人的视频导出（${esc(files.join("、"))}），按导出区间汇总，不与自制视频数据相加。自有账号${data.own_accounts.length ? `（${esc(data.own_accounts.join("、"))}）` : ""}和未署名视频不算达人，只用来算达人占比。出单 = 该区间有归因订单或 GMV；挂多款商品的视频按第一个商品归类。${excluded.length ? `与其他导出重叠、未计入：${esc(excluded.join("；"))}。` : ""}</p>`;
  }
  const campaignNote = S => S.campaignRange ? `<p class="perf-chart-note perf-creator-campaign-note">活动日期：${esc(S.label)}。${S.picked.size ? `实际计入完整报表：${esc(reportSpan(S))}。按完整导出统计，可能包含活动外日期，无法精确拆分到活动日。` : "没有与活动日期重叠的达人导出。"}</p>` : "";

  function kpis(S, data) {
    const t = S.total;
    const tiles = [
      ["达人归因 GMV", money(t.gmv), `${scopeLabel(S, data)} · 占全部视频 ${share(t.gmv, t.all)}`],
      ["出单达人", count(t.sellers.size), `有数据达人 ${count(t.creators.size)} 位`],
      ["出单视频", count(t.sold.size), `有数据视频 ${count(t.videos.size)} 条`],
      ["归因订单", count(t.orders), `售出 ${count(t.items)} 件${t.orders ? ` · 每单 ${money2(t.gmv / t.orders)}` : ""}`],
      ["GPM（$/千次播放）", t.views ? money2(ratio(t.gmv, t.views, 1000)) : "—", `播放 ${count(t.views)} · CTOR ${pct(ratio(t.orders, t.clicks, 100))}`],
    ];
    return `<div class="perf-kpis perf-kpis-compact">${tiles.map(([label, value, sub]) => `<div class="perf-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(sub)}</small></div>`).join("")}</div>`;
  }

  function monthTable(S) {
    const rows = shownPeriods(S).map(({p, i}) => [`${esc(p.label)}${S.picked?.has(i) ? ' <span class="perf-muted">· 当前</span>' : ""}`, coverage(p), count(p.creators.size), count(p.sellers.size), i ? count(p.fresh) : '<span class="perf-muted">—</span>',
      count(p.sold.size), count(p.views), count(p.orders), money2(p.gmv), money(p.own), share(p.gmv, p.all), p.views ? money2(ratio(p.gmv, p.views, 1000)) : "—"]);
    return table([["月份"], ["覆盖"], ["有数据达人", 1], ["出单达人", 1], ["新出单达人", 1], ["出单视频", 1], ["播放", 1], ["订单", 1], ["达人 GMV", 1], ["自有账号 GMV", 1], ["达人占比", 1], ["GPM", 1]], rows);
  }

  // The latest ranges get a GMV column each when all ranges are shown, so a creator's trend reads along the row.
  const periodColumns = (S, data) => (!S.picked && data.periods.length > 1 ? data.periods.map((_, i) => i).slice(-4) : []);

  function creatorTable(S, data, f, shown) {
    const rows = listed(S, f);
    if (!rows.length) return { rows, html: `<p class="perf-muted perf-table-empty">没有符合条件的达人，换个月份或改成「全部达人」试试。</p>` };
    const cols = periodColumns(S, data);
    const sort = sorting(S, f);
    const th = (key, label) => {
      const active = sort.key === key, asc = sort.dir === "asc";
      return `<th class="num" aria-sort="${active ? (asc ? "ascending" : "descending") : "none"}"><button type="button" class="perf-sort${active ? " is-active" : ""}" data-creator-sort="${esc(key)}" title="按${esc(label)}排序">${esc(label)}<span class="perf-sort-arrow" aria-hidden="true">${active ? (asc ? "↑" : "↓") : "↕"}</span></button></th>`;
    };
    const visible = rows.slice(0, shown);
    const html = `<div class="perf-table-wrap"><table class="perf-table perf-video-table perf-creator-table"><thead><tr><th class="perf-col-video">达人</th>${cols.map((i) => th(`p${i}`, `${data.periods[i].label} GMV`)).join("")}${th("sold", "出单视频")}${th("views", "播放")}${th("orders", "订单")}${th("gmv", cols.length ? "合计 GMV" : "GMV")}${th("gpm", "GPM")}${th("ctor", "CTOR")}</tr></thead><tbody>${visible.map((c) => `<tr data-creator-row="${esc(c.account)}" tabindex="0" aria-label="查看 @${esc(c.account)} 的视频">
      <td class="perf-title"><strong>@${esc(c.account)}</strong><small>${count(c.videos.size)} 条视频 · 主卖 ${esc(mainProduct(c))}${c.firstSold > 0 && (!S.campaignRange || S.picked.has(c.firstSold)) ? ` · ${esc(data.periods[c.firstSold].label)}新出单` : ""}</small></td>
      ${cols.map((i) => `<td class="num">${c.byPeriod[i].gmv ? money(c.byPeriod[i].gmv) : '<span class="perf-muted">—</span>'}</td>`).join("")}<td class="num">${count(c.sold.size)}</td><td class="num">${count(c.views)}</td><td class="num">${count(c.orders)}</td><td class="num">${money(c.gmv)}</td><td class="num">${c.views ? money2(ratio(c.gmv, c.views, 1000)) : "—"}</td><td class="num">${pct(ratio(c.orders, c.clicks, 100))}</td>
    </tr>`).join("")}</tbody></table></div>${rows.length > visible.length ? `<button type="button" class="secondary perf-more" id="perf-creator-more">再显示 ${Math.min(100, rows.length - visible.length)} 位（共 ${rows.length} 位）</button>` : ""}`;
    return { rows, html };
  }

  function topVideos(S, data) {
    const rows = [...S.perVideo.values()].filter((v) => v.gmv > 0).sort((x, y) => y.gmv - x.gmv).slice(0, 10).map((m) => {
      const v = m.video, caseId = data.cases?.[v.id];
      return [`<button type="button" class="perf-link" data-creator-open="${esc(v.account)}">@${esc(v.account)}</button>`,
        `<a class="perf-cell-title" href="${esc(tiktok(v.account, v.id))}" target="_blank" rel="noopener noreferrer" title="${esc(v.title)}">${esc(v.title || v.id)}</a>${caseId ? ` <a class="perf-case-link" href="#case/${esc(caseId)}">案例</a>` : ""}`,
        esc(v.posted || "—"), productCell(v), count(m.views), count(m.orders), money2(m.gmv), m.views ? money2(ratio(m.gmv, m.views, 1000)) : "—", ...publicVideoCells(data,v.id)];
    });
    return `<p class="context-note">${esc(publicVideoNote)}</p>` + table([["达人"], ["视频"], ["发布"], ["产品"], ["区间播放", 1], ["订单", 1], ["归因 GMV", 1], ["GPM", 1], ...publicVideoColumns], rows);
  }

  function productTable(S) {
    const head = [["产品型号"], ["带货达人", 1], ["出单达人", 1], ["出单视频", 1], ["播放", 1], ["订单", 1], ["达人 GMV", 1], ["占达人 GMV", 1]];
    const rows = [...S.products.values()].sort((x, y) => y.gmv - x.gmv).map((p) => [esc(p.name), count(p.creators.size), count(p.sellers.size), count(p.sold.size), count(p.views), count(p.orders), money2(p.gmv), share(p.gmv, S.total.gmv)]);
    const shown = 12;
    return table(head, rows.slice(0, shown)) + (rows.length > shown ? `<details class="perf-more-rows"><summary>其余 ${rows.length - shown} 个产品</summary>${table(head, rows.slice(shown))}</details>` : "");
  }

  function ownPublicVideos(data) {
    const own = videosOf(data).filter(v => v.kind === "own" && productOk(v) && data.public_stats?.[v.id]);
    if (!own.length) return "";
    const rows = own.map(v => {
      const snapshot = data.public_stats[v.id], seen = snapshot.last_seen_at || snapshot.observed_at;
      const moment = seen ? new Date(seen) : null;
      const time = moment && Number.isFinite(moment.getTime()) ? moment.toLocaleString("zh-CN", {hour12:false}) : "—";
      return [esc("@" + v.account), `<a class="perf-cell-title" href="${esc(tiktok(v.account,v.id))}" target="_blank" rel="noopener noreferrer" title="${esc(v.title)}">${esc(v.title || v.id)}</a>`,
        productCell(v), ...publicVideoCells(data,v.id), esc(time)];
    });
    return `<section class="section-card perf-trend-card perf-own-public"><div class="perf-card-head"><h2>自有账号公开数据</h2><span>${count(rows.length)} 条视频</span></div>
      <p class="context-note">当前产品下自有账号的最新公开累计值，不受报表区间筛选影响；未采到的指标显示“—”。这些视频不计入达人汇总。</p>
      ${table([["账号"], ["视频"], ["产品"], ...publicVideoColumns, ["采集时间"]],rows)}</section>`;
  }

  // The page: caliber, headline numbers, each range, then the creators themselves.
  function board({ data, filters: f, shown }) {
    if (!data.periods.length) return `<div class="empty"><h3>还没有达人数据</h3><p>把后台「全部达人」视频导出（含 Creator country 列）放进销售数据文件夹的「达人」子文件夹，再点「同步数据」。</p></div>`;
    const S = scope(data, f), ownPublic = ownPublicVideos(data);
    if (S.campaignRange && !S.picked.size) return `${campaignNote(S)}${ownPublic}<div class="empty"><h3>当前活动期没有达人导出</h3><p>换个活动或导入对应日期的达人报表。</p></div>`;
    if (!(S.campaignRange ? S.total.creators.size : S.creators.size)) return `${note(data, S)}${campaignNote(S)}${ownPublic}<div class="empty"><h3>${S.campaignRange ? "当前活动期和产品下没有达人视频" : "当前产品下没有达人视频"}</h3><p>换个产品试试。</p></div>`;
    const ranked = creatorTable(S, data, f, shown);
    return `${note(data, S)}${campaignNote(S)}${kpis(S, data)}
      ${section("按月汇总", S.campaignRange ? "仅与活动重叠的完整导出区间，每个区间一行；非活动逐日值" : "每个导出区间一行，只受产品筛选影响", monthTable(S))}
      ${section("达人排行", `${scopeLabel(S, data)} · ${showModes[f.cshow] || showModes.sold} ${count(ranked.rows.length)} 位 · 点一行看该达人的视频`, ranked.html)}
      <div class="perf-folds">${fold("达人视频 Top 10", "按归因 GMV", topVideos(S, data))}
      ${S.products.size > 1 ? fold("产品型号", "达人带货按商品拆分", productTable(S)) : ""}</div>${ownPublic}`;
  }

  function toolbar(data, f) {
    return `      <label class="search">${icon("search")}<input id="perf-cq" aria-label="搜索达人账号" placeholder="搜索达人账号…" value="${esc(f.cq)}"></label>
      <select id="perf-cshow" aria-label="达人范围">${Object.entries(showModes).map(([key, label]) => `<option value="${key}" ${key === f.cshow ? "selected" : ""}>${label}</option>`).join("")}</select>`;
  }

  // One creator: figures for the chosen range, every range side by side, and the videos behind them.
  function detail({ data, filters: f }, account) {
    const S = scope(data, f), c = S.creators.get(account);
    if (!c || (S.campaignRange && !c.videos.size)) return null;
    const fact = (label, value) => `<dt>${esc(label)}</dt><dd>${value}</dd>`;
    const first = c.firstSold == null ? "已导入区间内未出单" : c.firstSold === 0 ? `${data.periods[0].label}或更早` : data.periods[c.firstSold].label;
    const months = shownPeriods(S).map(({p, i}) => [p, c.byPeriod[i]]).filter(([, m]) => m.videos.size)
      .map(([p, m]) => [esc(p.label), count(m.videos.size), count(m.sold.size), count(m.views), count(m.orders), money2(m.gmv)]);
    const videos = [...c.videos].map((index) => S.perVideo.get(index)).sort((x, y) => y.gmv - x.gmv || y.views - x.views).map((m) => {
      const v = m.video, caseId = data.cases?.[v.id];
      return [`<a class="perf-cell-title" href="${esc(tiktok(account, v.id))}" target="_blank" rel="noopener noreferrer" title="${esc(v.title)}">${esc(v.title || v.id)}</a>${caseId ? ` <a class="perf-case-link" href="#case/${esc(caseId)}">案例</a>` : ""}`,
        esc(v.posted || "—"), productCell(v), count(m.views), count(m.orders), money2(m.gmv), ...publicVideoCells(data,v.id)];
    });
    return `<div class="rev-drawer-head"><h2 id="perf-video-title">@${esc(account)}</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭达人详情">${icon("close")}</button></div>
      <div class="rev-drawer-body perf-video-body"><p class="perf-video-meta">${esc(c.country || "国家未记录")} · ${esc(scopeLabel(S, data))} · 主卖 ${esc(mainProduct(c))} · <a class="subtle" href="${esc(tiktok(account))}" target="_blank" rel="noopener noreferrer">${icon("link")}TikTok 主页</a></p>
        ${campaignNote(S)}<dl class="perf-video-facts">${fact("归因 GMV", money2(c.gmv))}${fact("归因订单", count(c.orders))}${fact("售出件数", count(c.items))}${fact("出单视频", `${count(c.sold.size)} / ${count(c.videos.size)}`)}${fact("播放", count(c.views))}${fact("商品点击", count(c.clicks))}${fact("GPM", c.views ? money2(ratio(c.gmv, c.views, 1000)) : "—")}${fact("CTOR", pct(ratio(c.orders, c.clicks, 100)))}${fact(S.campaignRange ? "首次出单（导入历史）" : "首次出单", esc(first))}</dl>
        <section class="perf-video-section"><h3>每月</h3>${table([["月份"], ["视频", 1], ["出单", 1], ["播放", 1], ["订单", 1], ["GMV", 1]], months)}</section>
        <section class="perf-video-section"><h3>视频 ${count(videos.length)} 条</h3><p class="context-note">${esc(publicVideoNote)}</p>${table([["标题"], ["发布"], ["产品"], ["区间播放", 1], ["订单", 1], ["GMV", 1], ...publicVideoColumns], videos)}</section></div>`;
  }

  // Headline figures and per-range figures for the dashboard's source strip and monthly comparison.
  // Creator videos only (own and unnamed accounts left out); the product filter applies, the account filter does not.
  function overview(data, f) {
    const S = scope(data, f);
    return {
      total: { gmv: S.total.gmv, orders: S.total.orders, views: S.total.views, sellers: S.total.sellers.size, creators: S.total.creators.size },
      periods: S.periods.map((p) => ({ start: p.start, end: p.end, days: p.days, label: p.label, gmv: p.gmv, orders: p.orders, views: p.views })),
      picked: S.picked,
    };
  }

  window.PerfCreators = { defaults, showModes, validSort: (key) => !!sortValue(key), board, toolbar, detail, overview };
})();
