"use strict";

// TikTok Shop video dashboard: headline numbers, weekly charts and tables that follow the account, product and period filters.
// One measure per chart (no second axis); bars for amounts, lines for rates, a fixed color per account.
(() => {
  const periods = { 8: ["近 8 周", 8], 13: ["近 13 周", 13], 26: ["近 26 周", 26], all: ["全部周期", null] };
  const money = (v) => (v == null ? "—" : "$" + Math.round(v).toLocaleString("en-US"));
  const money2 = (v) => (v == null ? "—" : "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));
  const count = (v) => (v == null ? "—" : Math.round(v).toLocaleString("en-US"));
  const pct = (v) => (v == null ? "—" : v.toFixed(2) + "%");
  const ratio = (a, b, scale = 1) => (b ? (a * scale) / b : null);
  const compact = (v, prefix = "") => {
    const scaled = v >= 1e6 ? [v / 1e6, "M"] : v >= 1e3 ? [v / 1e3, "K"] : [v, ""];
    return `${prefix}${+scaled[0].toFixed(scaled[0] >= 10 || !scaled[1] ? 0 : 1)}${scaled[1]}`;
  };
  const percentTick = (v) => `${+v.toFixed(v >= 10 ? 0 : 1)}%`;
  // The axis also labels max / 2, so every step keeps a round half (0.5, 1, 1.5, 2, 2.5, 3, 4, 5).
  const niceMax = (v) => {
    if (!(v > 0)) return 1;
    const power = 10 ** Math.floor(Math.log10(v)), n = v / power;
    return [1, 2, 3, 4, 5, 6, 8, 10].find((step) => n <= step) * power;
  };
  const mondayOf = (iso) => {
    const day = new Date(`${iso}T00:00:00Z`);
    day.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 6) % 7));
    return day.toISOString().slice(0, 10);
  };
  const short = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

  // ---- data ---------------------------------------------------------------
  const prepared = new WeakMap();
  function prepare(a) {
    if (prepared.has(a)) return prepared.get(a);
    const attrs = a.videos.map(([id, account, product, posted]) => ({ id, account: account || "未知账号", product, posted }));
    const byAccount = new Map();
    a.weekly.forEach((r) => byAccount.set(attrs[r[0]].account, (byAccount.get(attrs[r[0]].account) || 0) + r[7]));
    // The color follows the account for good: ranked once by all-time GMV, whatever the filters show.
    const accounts = [...new Set([...byAccount.entries()].sort((x, y) => y[1] - x[1]).map(([name]) => name).concat(attrs.map((v) => v.account)))];
    const result = { attrs, accounts, slot: new Map(accounts.map((name, i) => [name, Math.min(i, 7)])), products: new Set(attrs.map((v) => v.product)) };
    prepared.set(a, result);
    return result;
  }

  function scope(ctx) {
    const f = ctx.filters, a = ctx.analytics, P = prepare(a);
    const wanted = f.product.trim(), exact = P.products.has(wanted), low = wanted.toLowerCase();
    const ok = (attr) => (!f.account || attr.account === f.account) &&
      (window.WorkbenchShell ? window.WorkbenchShell.matchesProduct(attr.product) : !wanted || (exact ? attr.product === wanted : (attr.product || "").toLowerCase().includes(low)));
    const known = new Map(ctx.summary.weeks.map((w) => [w.week, w.complete]));
    const first = ctx.summary.weeks[0]?.week, last = ctx.summary.weeks.at(-1)?.week;
    let weeks = [];
    for (let week = first; week && week <= last; week = addCalendarDays(week, 7)) weeks.push({ week, complete: known.get(week) === true });
    const n = periods[f.period]?.[1];
    if (n) weeks = weeks.slice(-n);
    const empty = () => ({ videos: 0, views: 0, impressions: 0, clicks: 0, orders: 0, items: 0, gmv: 0 });
    const byWeek = new Map(weeks.map((w) => [w.week, Object.assign(w, empty(), { byAccount: new Map(), acc: new Map(), posted: 0, postedBy: new Map() })]));
    const total = empty(), seen = new Set(), products = new Map(), videos = new Map(), accountGmv = new Map();
    const add = (target, r) => { target.views += r[2]; target.impressions += r[3]; target.clicks += r[4]; target.orders += r[5]; target.items += r[6]; target.gmv += r[7]; };
    for (const r of a.weekly) {
      const bucket = byWeek.get(r[1]);
      if (!bucket) continue;
      const attr = P.attrs[r[0]];
      if (!ok(attr)) continue;
      bucket.videos++;
      add(bucket, r);
      add(total, r);
      bucket.byAccount.set(attr.account, (bucket.byAccount.get(attr.account) || 0) + r[7]);
      if (!bucket.acc.has(attr.account)) bucket.acc.set(attr.account, empty());
      add(bucket.acc.get(attr.account), r);
      accountGmv.set(attr.account, (accountGmv.get(attr.account) || 0) + r[7]);
      seen.add(r[0]);
      if (!products.has(attr.product)) products.set(attr.product, Object.assign(empty(), { ids: new Set() }));
      const product = products.get(attr.product);
      product.ids.add(r[0]);
      add(product, r);
      if (!videos.has(r[0])) videos.set(r[0], empty());
      add(videos.get(r[0]), r);
    }
    P.attrs.forEach((attr) => {
      const bucket = attr.posted && byWeek.get(mondayOf(attr.posted));
      if (!bucket || !ok(attr)) return;
      bucket.posted++;
      bucket.postedBy.set(attr.account, (bucket.postedBy.get(attr.account) || 0) + 1);
    });
    total.videos = seen.size;
    const months = new Map();
    for (const r of a.monthly) {
      if (!ok(P.attrs[r[0]])) continue;
      if (!months.has(r[1])) months.set(r[1], Object.assign(empty(), { ids: new Set() }));
      const month = months.get(r[1]);
      month.ids.add(r[0]);
      add(month, r);
    }
    const dayFrom = weeks[0]?.week, dayTo = weeks.length ? addCalendarDays(weeks.at(-1).week, 6) : null;
    const shop = dayFrom ? a.shop_daily.filter((d) => d.date >= dayFrom && d.date <= dayTo) : [];
    const accountsShown = [...accountGmv].sort((x, y) => y[1] - x[1]).map(([name]) => name);
    return { P, weeks, total, products, videos, months, shop, accountsShown, gaps: weeks.filter((w) => !w.complete) };
  }

  // ---- svg ---------------------------------------------------------------
  const L = 40, R = 8, T = 18, B = 22;
  let W = 300, H = 140, plotW = W - L - R, plotH = H - T - B, base = T + plotH;
  // Draws with a wider canvas so text keeps its size when a chart spans the page.
  function sized(width, height, draw) {
    const saved = [W, H];
    const set = (w, h) => { W = w; H = h; plotW = W - L - R; plotH = H - T - B; base = T + plotH; };
    set(width, height);
    try { return draw(); } finally { set(...saved); }
  }
  let registry = new Map(), serial = 0;

  function grid(max, tick) {
    const y = (v) => base - (v / max) * plotH;
    return [0, max / 2, max].map((t) => `<line class="${t ? "perf-grid" : "perf-baseline"}" x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}"/><text class="perf-tick" x="${L - 6}" y="${y(t) + 3.5}" text-anchor="end">${esc(tick(t))}</text>`).join("");
  }

  // Up to five labels along the bottom: both ends and a few in between, never crowding the ends.
  function xAxis(n, label) {
    const band = plotW / n, step = Math.max(1, Math.ceil(n / 5));
    const out = [];
    for (let i = 0; i < n; i += 1) {
      const end = i === 0 || i === n - 1;
      if (!end && (i % step || i < 2 || n - 1 - i < 2)) continue;
      out.push(`<text class="perf-tick" x="${L + band * i + (i === 0 ? 0 : i === n - 1 ? band : band / 2)}" y="${H - 5}" text-anchor="${i === 0 ? "start" : i === n - 1 ? "end" : "middle"}">${esc(label(i))}</text>`);
    }
    return out.join("");
  }

  function hits(id, n) {
    const band = plotW / n;
    return Array.from({ length: n }, (_, i) => `<rect class="perf-hit" data-chart="${id}" data-i="${i}" x="${L + band * i}" y="${T - 6}" width="${band}" height="${plotH + 6}"/>`).join("");
  }

  function svgOpen(label) {
    return `<svg class="perf-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">`;
  }

  function columns(id, label, values, o) {
    const n = values.length, band = plotW / n, barW = Math.min(W > 400 ? 44 : 18, Math.max(2, band * (W > 400 ? 0.56 : 0.66)));
    const max = niceMax(Math.max(0, ...values.filter((v) => v != null)));
    const y = (v) => base - (v / max) * plotH;
    const last = values.findLastIndex((v) => v != null);
    const bars = values.map((v, i) => {
      const x = L + band * i + (band - barW) / 2;
      if (v == null) return `<line class="perf-gap-mark" x1="${x}" x2="${x + barW}" y1="${base - 1.5}" y2="${base - 1.5}"/>`;
      const h = (v / max) * plotH, r = Math.min(4, barW / 2, h), t = base - h;
      return h <= 0 ? "" : `<path class="perf-col${i === last ? " is-latest" : ""}" data-i="${i}" d="M${x},${base}V${t + r}Q${x},${t} ${x + r},${t}H${x + barW - r}Q${x + barW},${t} ${x + barW},${t + r}V${base}Z"/>`;
    }).join("");
    const text = last >= 0 && values[last] > 0 ? `<text class="perf-value-label" x="${Math.min(L + band * last + band / 2 + barW / 2, W - R)}" y="${y(values[last]) - 5}" text-anchor="end">${esc(o.tick(values[last]))}</text>` : "";
    return `${svgOpen(label)}${grid(max, o.tick)}${bars}${text}${xAxis(n, o.x)}${hits(id, n)}</svg>`;
  }

  function stacked(id, label, series, o) {
    const n = o.n, band = plotW / n, barW = Math.min(W > 400 ? 44 : 18, Math.max(2, band * (W > 400 ? 0.56 : 0.66)));
    const totals = Array.from({ length: n }, (_, i) => (o.gap(i) ? null : series.reduce((sum, s) => sum + s.values[i], 0)));
    const max = niceMax(Math.max(0, ...totals.filter((v) => v != null)));
    const scale = plotH / max;
    const bars = totals.map((total, i) => {
      const x = L + band * i + (band - barW) / 2;
      if (total == null) return `<line class="perf-gap-mark" x1="${x}" x2="${x + barW}" y1="${base - 1.5}" y2="${base - 1.5}"/>`;
      const parts = series.filter((s) => s.values[i] > 0);
      let top = base;
      return parts.map((s, k) => {
        const h = s.values[i] * scale, y = top - h;
        top = y;
        if (k < parts.length - 1) return `<rect class="perf-seg-rect cat-${s.slot}" data-i="${i}" x="${x}" y="${y}" width="${barW}" height="${h}"/>`;
        const r = Math.min(4, barW / 2, h);
        return `<path class="perf-seg-rect cat-${s.slot}" data-i="${i}" d="M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + barW - r}Q${x + barW},${y} ${x + barW},${y + r}V${y + h}Z"/>`;
      }).join("");
    }).join("");
    return `${svgOpen(label)}${grid(max, o.tick)}${bars}${xAxis(n, o.x)}${hits(id, n)}</svg>`;
  }

  function line(id, label, values, o) {
    const n = values.length, band = plotW / n;
    const max = niceMax(Math.max(0, ...values.filter((v) => v != null)));
    const x = (i) => L + band * i + band / 2, y = (v) => base - (v / max) * plotH;
    let path = "", open = false;
    values.forEach((v, i) => {
      if (v == null) { open = false; return; }
      path += `${open ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
      open = true;
    });
    const gaps = values.map((v, i) => (v == null ? `<line class="perf-gap-mark" x1="${x(i) - 3}" x2="${x(i) + 3}" y1="${base - 1.5}" y2="${base - 1.5}"/>` : "")).join("");
    const last = values.findLastIndex((v) => v != null);
    const end = last >= 0 ? `<circle class="perf-end-dot" cx="${x(last)}" cy="${y(values[last])}" r="3.5"/><text class="perf-value-label" x="${Math.min(x(last) + 4, W - R)}" y="${y(values[last]) - 8}" text-anchor="end">${esc(o.tick(values[last]))}</text>` : "";
    const hitRects = Array.from({ length: n }, (_, i) => `<rect class="perf-hit" data-chart="${id}" data-i="${i}" data-cx="${x(i)}" data-cy="${values[i] == null ? "" : y(values[i])}" x="${L + band * i}" y="${T - 6}" width="${band}" height="${plotH + 6}"/>`).join("");
    return `${svgOpen(label)}${grid(max, o.tick)}<line class="perf-cross" y1="${T - 4}" y2="${base}" visibility="hidden"/>${path ? `<path class="perf-line" d="${path}"/>` : ""}${gaps}${end}<circle class="perf-dot" r="4" visibility="hidden"/>${xAxis(n, o.x)}${hitRects}</svg>`;
  }

  // Horizontal bars for a ranking: name on the left, value at the bar's end.
  function rank(id, label, items, tick) {
    const rowH = 24, labelW = 96, right = 52, height = items.length * rowH + 4;
    const max = Math.max(...items.map((item) => item.value), 1);
    const bars = items.map((item, i) => {
      const w = Math.max(1.5, ((W - labelW - right) * item.value) / max), y = 2 + i * rowH;
      return `<text class="perf-rank-label" x="0" y="${y + 15}">${esc(short(item.label, 13))}<title>${esc(item.label)}</title></text><rect class="perf-bar${item.muted ? " is-muted" : ""}" data-i="${i}" x="${labelW}" y="${y + 4}" width="${w}" height="${rowH - 10}" rx="3"/><text class="perf-value-label" x="${labelW + w + 5}" y="${y + 15}">${esc(tick(item.value))}</text><rect class="perf-hit" data-chart="${id}" data-i="${i}" x="0" y="${y}" width="${W}" height="${rowH}"/>`;
    }).join("");
    return `<svg class="perf-chart perf-rank" viewBox="0 0 ${W} ${height}" role="img" aria-label="${esc(label)}">${bars}</svg>`;
  }

  // ---- cards ---------------------------------------------------------------
  function tipFor(id, tips) {
    registry.set(id, tips);
  }

  // A card with a title, the latest figure and how it moved, then the chart.
  // Gap weeks draw only a short dash, so charts that contain one say what it means instead of leaving a silent hole.
  const gapNote = '<p class="perf-gap-note"><i aria-hidden="true"></i>短横的周导出有缺口，不计入走势</p>';
  function card({ title, note = "", head, chart, legend = "", gaps = false }) {
    return `<div class="perf-mini"><div class="perf-mini-head"><span>${esc(title)}</span>${head ? `<strong>${esc(head[0])}</strong><small>${esc(head[1])}</small>` : ""}</div>${note ? `<p class="perf-chart-note">${esc(note)}</p>` : ""}${chart}${legend}${gaps ? gapNote : ""}<div class="perf-tooltip" role="status" hidden></div></div>`;
  }

  function change(values, fmt) {
    const known = values.map((v, i) => [v, i]).filter(([v]) => v != null);
    if (!known.length) return null;
    const [latest] = known.at(-1), previous = known.at(-2)?.[0];
    const move = previous ? ((latest - previous) / previous) * 100 : null;
    return [fmt(latest), move == null ? "上周无对比" : Math.abs(move) < 0.5 ? "与上周持平" : `比上周 ${move > 0 ? "↑" : "↓"}${Math.abs(move).toFixed(0)}%`];
  }

  const weekLabel = (w) => w.week.slice(5);

  function weeklyCards(S) {
    const ws = S.weeks;
    const series = (read) => ws.map((w) => (w.complete ? read(w) : null));
    const note = (w) => (w.complete ? "" : "该周导出有缺口，不计入走势");
    const tipLines = (w) => [["订单", count(w.orders)], ["GMV", money(w.gmv)], ["播放", count(w.views)], ["商品点击", count(w.clicks)], ["GPM", w.views ? money2(ratio(w.gmv, w.views, 1000)) : "—"], ["点击率", pct(ratio(w.clicks, w.views, 100))], ["CTOR", pct(ratio(w.orders, w.clicks, 100))]];
    const make = (key, title, read, fmt, tick, kind, extra) => {
      const id = `c${serial++}`, values = series(read);
      tipFor(id, ws.map((w, i) => ({ value: w.complete ? fmt(values[i]) : "数据不全", when: `${w.week} 起一周`, lines: w.complete ? tipLines(w).filter(([label]) => label !== extra) : [["", note(w)]] })));
      const o = { tick, x: (i) => weekLabel(ws[i]) };
      return card({ title, head: change(values, fmt), chart: (kind === "line" ? line : columns)(id, title, values, o), gaps: values.includes(null) });
    };
    return {
      gmv: make("gmv", "每周归因 GMV", (w) => w.gmv, money, (v) => compact(v, "$"), "columns", "GMV"),
      orders: make("orders", "每周归因订单", (w) => w.orders, count, (v) => compact(v), "columns", "订单"),
      items: make("items", "每周售出件数", (w) => w.items, count, (v) => compact(v), "columns", "售出件数"),
      posted: make("posted", "每周新发布视频", (w) => w.posted, count, (v) => compact(v), "columns", "新发布视频"),
      gpm: make("gpm", "每周 GPM（$/千次播放）", (w) => ratio(w.gmv, w.views, 1000), money2, (v) => compact(v, "$"), "line", "GPM"),
      ctr: make("ctr", "每周商品点击率", (w) => ratio(w.clicks, w.views, 100), pct, percentTick, "line", "点击率"),
      ctor: make("ctor", "每周 CTOR（订单 / 商品点击）", (w) => ratio(w.orders, w.clicks, 100), pct, percentTick, "line", "CTOR"),
      views: make("views", "每周播放量", (w) => w.views, count, (v) => compact(v), "columns", "播放"),
      clicks: make("clicks", "每周商品点击", (w) => w.clicks, count, (v) => compact(v), "columns", "商品点击"),
    };
  }

  function legendFor(names, S, valueOf, fmt) {
    return `<ul class="perf-legend">${names.map((name) => `<li><i class="perf-key cat-${S.P.slot.get(name)}"></i><span>${esc(name)}</span><small>${esc(fmt(valueOf(name)))}</small></li>`).join("")}</ul>`;
  }

  function accountCards(S) {
    const ws = S.weeks, names = S.accountsShown;
    const make = (title, valueOf, fmt, tick, totalLabel, totalOf, legendValue, legendFmt) => {
      const id = `c${serial++}`;
      const series = names.map((name) => ({ name, slot: S.P.slot.get(name), values: ws.map((w) => valueOf(w, name)) }));
      tipFor(id, ws.map((w, i) => ({
        value: w.complete ? fmt(totalOf(w)) : "数据不全", when: `${w.week} 起一周 · ${totalLabel}`,
        lines: w.complete ? series.filter((s) => s.values[i] > 0).sort((x, y) => y.values[i] - x.values[i]).map((s) => [s.name, fmt(s.values[i]), s.slot]) : [["", "该周导出有缺口，不计入走势"]],
      })));
      const latest = ws.findLast((w) => w.complete);
      return card({
        title, head: latest ? [fmt(totalOf(latest)), `${latest.week.slice(5)} 起一周`] : null,
        chart: stacked(id, title, series, { n: ws.length, gap: (i) => !ws[i].complete, tick, x: (i) => weekLabel(ws[i]) }),
        legend: legendFor(names, S, legendValue, legendFmt), gaps: ws.some((w) => !w.complete),
      });
    };
    const gmvOf = (name) => ws.reduce((sum, w) => sum + (w.byAccount.get(name) || 0), 0);
    const postedOf = (name) => ws.reduce((sum, w) => sum + (w.postedBy.get(name) || 0), 0);
    return [
      make("各账号每周归因 GMV", (w, name) => w.byAccount.get(name) || 0, money, (v) => compact(v, "$"), "合计", (w) => w.gmv, gmvOf, money),
      make("各账号每周新发布视频数", (w, name) => w.postedBy.get(name) || 0, count, (v) => compact(v), "合计", (w) => w.posted, postedOf, (v) => `${count(v)} 条`),
    ].join("");
  }

  function productRank(S) {
    const rows = [...S.products].map(([name, p]) => ({ name, ...p, ids: p.ids })).sort((x, y) => y.gmv - x.gmv);
    const top = rows.slice(0, 8), rest = rows.slice(8);
    const items = top.map((p) => ({ label: p.name, value: p.gmv, tip: p }));
    if (rest.length) items.push({ label: `其他 ${rest.length} 个产品`, value: rest.reduce((sum, p) => sum + p.gmv, 0), muted: true, tip: { videos: rest.reduce((sum, p) => sum + p.ids.size, 0), views: rest.reduce((sum, p) => sum + p.views, 0), orders: rest.reduce((sum, p) => sum + p.orders, 0), gmv: rest.reduce((sum, p) => sum + p.gmv, 0) } });
    if (!items.length) return card({ title: "产品型号 GMV 排行", chart: '<p class="perf-muted">当前筛选下没有数据</p>' });
    const id = `c${serial++}`;
    tipFor(id, items.map((item) => ({ value: money(item.value), when: item.label, lines: [["视频数", count(item.tip.ids ? item.tip.ids.size : item.tip.videos)], ["播放", count(item.tip.views)], ["订单", count(item.tip.orders)]] })));
    return card({ title: "产品型号 GMV 排行", chart: rank(id, "产品型号 GMV 排行", items, (v) => compact(v, "$")) });
  }

  function shopCards(S) {
    if (!S.shop.length) return "";
    const days = S.shop;
    const make = (title, read, fmt, tick, kind) => {
      const id = `c${serial++}`, values = days.map(read);
      tipFor(id, days.map((d) => ({ value: fmt(read(d)), when: d.date, lines: [["GMV", money(d.gmv)], ["订单", count(d.orders)], ["播放", count(d.views)], ["商品点击", count(d.clicks)]].filter(([label]) => label !== (kind === "line" ? "订单" : "GMV")) })));
      const labels = (i) => days[i].date.slice(5);
      const chart = (kind === "line" ? line : columns)(id, title, values, { tick, x: labels });
      const total = values.reduce((sum, v) => sum + v, 0);
      return card({ title, head: [fmt(total), `${days[0].date.slice(5)} 至 ${days.at(-1).date.slice(5)} 合计`], chart });
    };
    const ratioNote = S.shopRatio ? `店铺汇总约为同期视频明细合计的 ${S.shopRatio.toFixed(1)} 倍` : "";
    return `<section class="section-card perf-trend-card"><div class="perf-card-head"><h2>店铺每日视频 GMV</h2><span>店铺汇总口径，不受账号和产品筛选影响${ratioNote ? ` · ${ratioNote}` : ""}</span></div>
      <div class="perf-trend perf-trend-wide">${make("店铺每日归因 GMV", (d) => d.gmv, money, (v) => compact(v, "$"), "columns")}${make("店铺每日归因订单", (d) => d.orders, count, (v) => compact(v), "line")}</div></section>`;
  }

  // ---- tables ---------------------------------------------------------------
  function table(head, rows, empty = "当前筛选下没有数据") {
    if (!rows.length) return `<p class="perf-muted perf-table-empty">${esc(empty)}</p>`;
    return `<div class="perf-table-wrap"><table class="perf-week-data"><thead><tr>${head.map(([label, num]) => `<th${num ? ' class="num"' : ""}>${esc(label)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell, i) => `<td${head[i][1] ? ' class="num"' : ""}>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  const rates = (m) => [money2(ratio(m.gmv, m.views, 1000)), pct(ratio(m.clicks, m.views, 100)), pct(ratio(m.orders, m.clicks, 100))];

  function monthlyTable(S, a) {
    const rows = [...S.months].sort((x, y) => (x[0] < y[0] ? -1 : 1)).map(([month, m]) => [esc(month), esc(a.months[month] || ""), count(m.ids.size), count(m.views), count(m.clicks), count(m.orders), money2(m.gmv), ...rates(m)]);
    return table([["月份"], ["口径"], ["有数据视频数", 1], ["播放量", 1], ["商品点击", 1], ["归因订单", 1], ["归因 GMV", 1], ["GPM", 1], ["点击率", 1], ["CTOR", 1]], rows);
  }

  function productTable(S) {
    const head = [["产品型号"], ["视频数", 1], ["播放量", 1], ["商品点击", 1], ["归因订单", 1], ["售出件数", 1], ["归因 GMV", 1], ["GPM", 1], ["点击率", 1], ["CTOR", 1]];
    const rows = [...S.products].sort((x, y) => y[1].gmv - x[1].gmv).map(([name, p]) => [esc(name), count(p.ids.size), count(p.views), count(p.clicks), count(p.orders), count(p.items), money2(p.gmv), ...rates(p)]);
    const shown = 12;
    return table(head, rows.slice(0, shown)) + (rows.length > shown ? `<details class="perf-more-rows"><summary>其余 ${rows.length - shown} 个产品</summary>${table(head, rows.slice(shown))}</details>` : "");
  }

  function topVideos(S, ctx) {
    const byId = new Map(ctx.videos.map((v) => [v.video_id, v]));
    const rows = [...S.videos].sort((x, y) => y[1].gmv - x[1].gmv).slice(0, 10).map(([index, m]) => {
      const attr = S.P.attrs[index], video = byId.get(attr.id);
      const title = video ? `<button type="button" class="perf-link" data-perf-video="${esc(attr.id)}">${esc(video.headline || attr.id)}</button>` : esc(attr.id);
      return [esc(attr.account), esc(attr.product), esc(attr.posted || "—"), `<span class="perf-cell-title" title="${esc(video?.title || "")}">${title}</span>`, count(m.views), count(m.clicks), count(m.orders), money2(m.gmv), money2(ratio(m.gmv, m.views, 1000))];
    });
    return table([["账号"], ["产品"], ["发布"], ["标题"], ["播放量", 1], ["商品点击", 1], ["归因订单", 1], ["归因 GMV", 1], ["GPM", 1]], rows);
  }

  function weekTable(S) {
    const rows = [...S.weeks].reverse().map((w) => (w.complete
      ? [esc(`${w.week} 周`), count(w.videos), money(w.gmv), count(w.orders), count(w.views), count(w.clicks), money2(ratio(w.gmv, w.views, 1000)), pct(ratio(w.clicks, w.views, 100)), pct(ratio(w.orders, w.clicks, 100)), count(w.posted)]
      : [esc(`${w.week} 周`), '<span class="perf-muted">该周导出数据不全，不计入走势</span>', "", "", "", "", "", "", "", count(w.posted)]));
    return table([["周"], ["有数据视频", 1], ["GMV", 1], ["订单", 1], ["播放", 1], ["商品点击", 1], ["GPM", 1], ["点击率", 1], ["CTOR", 1], ["新发视频", 1]], rows);
  }

  function accountTable(S) {
    const names = S.accountsShown;
    const rows = [...S.weeks].reverse().filter((w) => w.complete).map((w) => [esc(`${w.week} 周`), ...names.map((n) => `${money(w.byAccount.get(n) || 0)} · ${count(w.postedBy.get(n) || 0)} 条`)]);
    return table([["周（GMV · 新发）"], ...names.map((n) => [n, 1])], rows);
  }

  // ---- page ---------------------------------------------------------------
  function kpis(S) {
    const t = S.total, span = S.weeks.length ? `${S.weeks[0].week} 至 ${addCalendarDays(S.weeks.at(-1).week, 6)}` : "";
    const tiles = [
      ["归因 GMV", money(t.gmv), span],
      ["归因订单", count(t.orders), t.orders ? `平均每单 ${money2(t.gmv / t.orders)}` : ""],
      ["播放量", count(t.views), `商品点击 ${count(t.clicks)} · 点击率 ${pct(ratio(t.clicks, t.views, 100))}`],
      ["GPM（$/千次播放）", t.views ? money2(ratio(t.gmv, t.views, 1000)) : "—", `CTOR ${pct(ratio(t.orders, t.clicks, 100))}`],
      ["有数据视频数", count(t.videos), t.videos ? `平均每条 ${money(t.gmv / t.videos)}` : ""],
    ];
    return `<div class="perf-kpis">${tiles.map(([label, value, sub]) => `<div class="perf-kpi"><span>${esc(label)}</span><strong>${esc(value)}</strong><small>${esc(sub)}</small></div>`).join("")}</div>`;
  }

  const metrics = {
    gmv: ["归因 GMV", (m) => m.gmv, money, (v) => compact(v, "$"), "columns", true],
    orders: ["归因订单", (m) => m.orders, count, (v) => compact(v), "columns", true],
    views: ["播放量", (m) => m.views, count, (v) => compact(v), "columns", true],
    gpm: ["GPM（$/千次播放）", (m) => ratio(m.gmv, m.views, 1000), money2, (v) => compact(v, "$"), "line", false],
    ctr: ["商品点击率", (m) => ratio(m.clicks, m.views, 100), pct, percentTick, "line", false],
    ctor: ["CTOR（订单 / 商品点击）", (m) => ratio(m.orders, m.clicks, 100), pct, percentTick, "line", false],
  };
  function metricTiles(S, chosen) {
    const ws = S.weeks.filter((w) => w.complete), t = S.total;
    return `<div class="perf-kpis perf-kpi-picker" role="group" aria-label="选择主图指标">${Object.entries(metrics).map(([key, [label, read, fmt]]) => {
      const move = change(ws.map(read), fmt);
      return `<button type="button" class="perf-kpi${key === chosen ? " is-selected" : ""}" data-perf-metric="${key}" aria-pressed="${key === chosen}"><span>${esc(label)}</span><strong>${esc(fmt(read(t)))}</strong><small>${esc(move ? `最近一周 ${move[0]} · ${move[1]}` : "没有完整周")}</small></button>`;
    }).join("")}</div>`;
  }
  // The chosen measure, week by week, then the same measure split by account (additive measures only).
  function mainCharts(S, chosen) {
    const [label, read, fmt, tick, kind, additive] = metrics[chosen], ws = S.weeks;
    const id = `c${serial++}`, values = ws.map((w) => (w.complete ? read(w) : null));
    tipFor(id, ws.map((w, i) => ({ value: w.complete ? fmt(values[i]) : "数据不全", when: `${w.week} 起一周`,
      lines: w.complete ? [["订单", count(w.orders)], ["GMV", money(w.gmv)], ["播放", count(w.views)], ["商品点击", count(w.clicks)]].filter(([name]) => !label.includes(name)) : [["", "该周导出有缺口，不计入走势"]] })));
    const main = sized(900, 250, () => (kind === "line" ? line : columns)(id, `每周${label}`, values, { tick, x: (i) => weekLabel(ws[i]) }));
    const head = change(values, fmt);
    let split = "";
    if (additive && S.accountsShown.length > 1) {
      const sid = `c${serial++}`, names = S.accountsShown;
      const series = names.map((name) => ({ name, slot: S.P.slot.get(name), values: ws.map((w) => (w.acc.get(name) ? read(w.acc.get(name)) : 0)) }));
      tipFor(sid, ws.map((w, i) => ({ value: w.complete ? fmt(read(w)) : "数据不全", when: `${w.week} 起一周 · 合计`,
        lines: w.complete ? series.filter((x) => x.values[i] > 0).sort((x, y) => y.values[i] - x.values[i]).map((x) => [x.name, fmt(x.values[i]), x.slot]) : [["", "该周导出有缺口，不计入走势"]] })));
      const totalOf = (name) => ws.reduce((sum, w) => sum + (w.acc.get(name) ? read(w.acc.get(name)) : 0), 0);
      split = `<div class="perf-mini perf-split"><div class="perf-mini-head"><span>按账号拆分</span><small>颜色固定对应账号</small></div>${sized(900, 200, () => stacked(sid, `各账号每周${label}`, series, { n: ws.length, gap: (i) => !ws[i].complete, tick, x: (i) => weekLabel(ws[i]) }))}${legendFor(names, S, totalOf, fmt)}<div class="perf-tooltip" role="status" hidden></div></div>`;
    }
    return `<section class="section-card perf-trend-card perf-main-card"><div class="perf-mini perf-main"><div class="perf-mini-head"><span>每周${esc(label)}</span>${head ? `<strong>${esc(head[0])}</strong><small>${esc(head[1])}</small>` : ""}</div>${main}<div class="perf-tooltip" role="status" hidden></div></div>${split}</section>`;
  }
  function fold(title, hint, body) {
    return `<details class="perf-fold section-card"><summary><span>${esc(title)}</span>${hint ? `<small>${esc(hint)}</small>` : ""}</summary><div class="perf-fold-body">${body}</div></details>`;
  }
  function board(ctx) {
    registry = new Map();
    serial = 0;
    const a = ctx.analytics;
    if (!a) return `<div class="empty"><h3>看板数据暂时读不到</h3><p>服务端还没有新的看板接口：请重启「案例工作台」服务后刷新页面。</p></div>`;
    if (!ctx.summary.weeks.length) return `<div class="empty"><h3>还没有可画的数据</h3><p>先同步或导入后台导出表格。</p></div>`;
    const S = scope(ctx);
    S.shopRatio = shopTotalRatio(a, S);
    if (!S.total.videos) return `<div class="empty"><h3>当前筛选下没有数据</h3><p>换个账号、产品或周期试试，或点「清除筛选」。</p></div>`;
    const chosen = metrics[ctx.filters.metric] ? ctx.filters.metric : "gmv", w = weeklyCards(S);
    const monthly = monthlyTable(S, a), shop = shopCards(S);
    return `${metricTiles(S, chosen)}${mainCharts(S, chosen)}
      ${section("视频排行 Top 10", "按归因 GMV，点一行看详情", `${topVideos(S, ctx)}<button type="button" class="secondary perf-more" data-perf-tab="list">查看全部视频明细</button>`)}
      <div class="perf-folds">${fold("发布节奏", "每周新发布视频数，按账号", `<div class="perf-trend perf-trend-wide">${w.posted}${accountCards(S).split('<div class="perf-mini">').slice(2).map((part) => '<div class="perf-mini">' + part).join("")}</div>`)}
      ${fold("转化效率三项", "GPM、点击率、CTOR 各用各的刻度", `<div class="perf-trend">${w.gpm}${w.ctr}${w.ctor}</div>`)}
      ${shop ? fold("店铺每日汇总", "另一份导出，口径更大，不与视频明细相加", shop) : ""}
      ${monthly.includes("<tbody>") && !monthly.includes("当前筛选下没有数据") ? fold("月度汇总", "按每月最全的报表口径，不受周期影响", monthly) : ""}
      ${S.products.size > 1 ? fold("产品型号表现", "按归因 GMV 从高到低", productTable(S)) : ""}
      ${fold("每周数据表", "", weekTable(S))}
      ${fold("各账号每周数据表", "", accountTable(S))}</div>`;
  }

  function section(title, hint, body) {
    return `<section class="section-card perf-trend-card"><div class="perf-card-head"><h2>${esc(title)}</h2>${hint ? `<span>${esc(hint)}</span>` : ""}</div>${body}</section>`;
  }

  function markup(ctx) {
    registry = new Map();
    serial = 0;
    const a = ctx.analytics;
    if (!a) return `<div class="empty"><h3>看板数据暂时读不到</h3><p>服务端还没有新的看板接口：请重启「案例工作台」服务后刷新页面。</p></div>`;
    if (!ctx.summary.weeks.length) return `<div class="empty"><h3>还没有可画的数据</h3><p>先同步或导入后台导出表格。</p></div>`;
    const S = scope(ctx);
    S.shopRatio = shopTotalRatio(a, S);
    const f = ctx.filters, filterText = [f.account, f.product.trim()].filter(Boolean).join(" · ");
    const gaps = ctx.summary.coverage.gaps.map(([x, y]) => (x === y ? x : `${x} 至 ${y}`)).join("、");
    const w = weeklyCards(S);
    const scopeText = `${filterText || "全部账号、全部产品"} · ${periods[f.period]?.[0] || "全部"}`;
    const empty = !S.total.videos;
    const head = `<div class="perf-board-note"><strong>口径说明</strong> 视频指标是后台导出里逐条视频的归因数据加总；店铺每日汇总来自另一份导出，口径更大，单独展示不与视频明细相加。点击率 = 商品点击 ÷ 播放，CTOR = 订单 ÷ 商品点击，GPM = 每千次播放的归因 GMV。${gaps ? `导出缺口：${esc(gaps)}，缺口所在周在走势里以短横表示。` : ""}<br><span class="perf-muted">当前范围：${esc(scopeText)}</span></div>`;
    if (empty) return `${head}<div class="empty"><h3>当前筛选下没有数据</h3><p>换个账号、产品或周期试试，或点「清除筛选」。</p></div>`;
    return `${head}${kpis(S)}
      ${section("销售", "归因 GMV 和订单，每周一格", `<div class="perf-trend">${w.gmv}${w.orders}${productRank(S)}</div>`)}
      ${section("转化效率", "三条比率，各用各的刻度", `<div class="perf-trend">${w.gpm}${w.ctr}${w.ctor}</div>`)}
      ${section("流量", "播放和商品点击", `<div class="perf-trend perf-trend-wide">${w.views}${w.clicks}</div>`)}
      ${section("账号与发布", "颜色固定对应账号，不随筛选变化", `<div class="perf-trend perf-trend-wide">${accountCards(S)}</div>`)}
      ${shopCards(S)}
      ${section("月度汇总", "按每月最全的报表口径，只受账号和产品筛选，不受周期影响", monthlyTable(S, a))}
      ${section("产品型号表现明细", "按归因 GMV 从高到低", productTable(S))}
      ${section("视频排行 Top 10", "按归因 GMV，完整列表见「视频明细」", `${topVideos(S, ctx)}<button type="button" class="secondary perf-more" data-perf-tab="list">查看全部视频明细</button>`)}
      <details class="perf-week-table section-card"><summary>查看每周数据表</summary>${weekTable(S)}</details>
      <details class="perf-week-table section-card"><summary>查看各账号每周数据表</summary>${accountTable(S)}</details>`;
  }

  // The shop's own daily export runs higher than the sum of its per-video rows; saying by how much keeps the two from being added up.
  function shopTotalRatio(a, S) {
    const weeks = new Set(S.weeks.filter((w) => w.complete).map((w) => w.week));
    const covered = new Set(S.shop.map((d) => mondayOf(d.date)).filter((week) => weeks.has(week)));
    const shop = S.shop.filter((d) => covered.has(mondayOf(d.date))).reduce((sum, d) => sum + d.gmv, 0);
    const perVideo = a.weekly.filter((r) => covered.has(r[1])).reduce((sum, r) => sum + r[7], 0);
    return shop && perVideo ? shop / perVideo : null;
  }

  // ---- hover ---------------------------------------------------------------
  function bind(root) {
    let current = null;
    const clear = () => {
      if (!current) return;
      const { card, hit } = current;
      $(".perf-tooltip", card).hidden = true;
      $$(".is-hover", card).forEach((el) => el.classList.remove("is-hover"));
      $$(".perf-cross, .perf-dot", card).forEach((el) => el.setAttribute("visibility", "hidden"));
      current = null;
    };
    const show = (hit) => {
      clear();
      const card = hit.closest(".perf-mini"), tip = $(".perf-tooltip", card), svg = $("svg", card);
      const data = registry.get(hit.dataset.chart)?.[Number(hit.dataset.i)];
      if (!data) return;
      $$(`[data-i="${hit.dataset.i}"]`, svg).forEach((el) => { if (!el.classList.contains("perf-hit")) el.classList.add("is-hover"); });
      if (hit.dataset.cx) {
        const cross = $(".perf-cross", svg), dot = $(".perf-dot", svg);
        cross.setAttribute("x1", hit.dataset.cx);
        cross.setAttribute("x2", hit.dataset.cx);
        cross.setAttribute("visibility", "visible");
        if (hit.dataset.cy) {
          dot.setAttribute("cx", hit.dataset.cx);
          dot.setAttribute("cy", hit.dataset.cy);
          dot.setAttribute("visibility", "visible");
        }
      }
      const head = document.createElement("strong"), when = document.createElement("span");
      head.textContent = data.value;
      when.textContent = data.when;
      const rows = data.lines.map(([label, text, slot]) => {
        const row = document.createElement("div"), key = document.createElement("i"), name = document.createElement("small"), value = document.createElement("b");
        row.className = "perf-tip-row";
        if (slot != null) { key.className = `perf-key cat-${slot}`; row.append(key); }
        name.textContent = label;
        value.textContent = text;
        row.append(name, value);
        return row;
      });
      tip.replaceChildren(head, when, ...rows);
      tip.hidden = false;
      const box = hit.getBoundingClientRect(), frame = card.getBoundingClientRect();
      const x = box.left + box.width / 2 - frame.left;
      tip.style.left = `${Math.min(Math.max(x, tip.offsetWidth / 2 + 4), frame.width - tip.offsetWidth / 2 - 4)}px`;
      tip.style.top = `${Math.max(box.top - frame.top, tip.offsetHeight + 8)}px`;
      current = { card, hit };
    };
    root.addEventListener("pointermove", (event) => {
      const hit = event.target.closest?.(".perf-hit");
      if (!hit) clear();
      else if (current?.hit !== hit) show(hit);
    });
    root.addEventListener("pointerleave", clear);
  }

  function homeMarkup(ctx) {
    registry = new Map(); serial = 0;
    if (!ctx.analytics || !ctx.summary?.weeks?.length) return '<p class="home-empty">尚无可用周数据，请先在视频数据中导入后台报表。</p>';
    const S = scope(ctx);
    if (!S.total.videos) return '<p class="home-empty">当前产品没有可用周数据。</p>';
    const charts = weeklyCards(S);
    return `<div class="home-weekly-charts">${charts.gmv}${charts.items}${charts.views}${charts.posted}</div>`;
  }
  window.PerfDashboard = { markup, board, bind, periods, homeMarkup, metrics };
})();
