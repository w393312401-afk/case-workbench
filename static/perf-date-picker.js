"use strict";

// Date range picker for the video dashboard: preset list on the left, two month calendars on the right.
// The chosen period is "all" or "YYYY-MM-DD~YYYY-MM-DD"; the dashboard counts every week the range touches.
(() => {
  const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];
  const MODES = [["day", "日"], ["week", "周"], ["month", "月"], ["custom", "自定义"]];
  const pad = (n) => String(n).padStart(2, "0");
  const iso = (y, m, d) => `${y}-${pad(m + 1)}-${pad(d)}`;
  const parse = (v) => { const [y, m, d] = v.split("-").map(Number); return { y, m: m - 1, d }; };
  const shift = (v, days) => { const { y, m, d } = parse(v); return new Date(Date.UTC(y, m, d + days)).toISOString().slice(0, 10); };
  const monthEnd = (y, m) => iso(y, m, new Date(Date.UTC(y, m + 1, 0)).getUTCDate());
  const mondayOf = (v) => { const { y, m, d } = parse(v); return shift(v, -((new Date(Date.UTC(y, m, d)).getUTCDay() + 6) % 7)); };
  const today = () => { const t = new Date(); return iso(t.getFullYear(), t.getMonth(), t.getDate()); };
  const anchorOf = (summary) => { const last = summary?.coverage?.last, now = today(); return last && last < now ? last : now; };
  const range = (period) => window.PerfDashboard.rangeOf(period);
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
  const idOf = (ctx) => ctx.id || "perf-date";
  const labelOf = (ctx) => ctx.label || "统计日期";
  const maxDate = (ctx) => (typeof ctx.maxDate === "function" ? ctx.maxDate() : ctx.maxDate) || today();

  const presets = (summary) => {
    const end = anchorOf(summary);
    const days = (n) => [shift(end, 1 - n), end], weeks = (n) => [shift(mondayOf(end), 1 - 7 * n), end];
    return [["近 7 天", days(7)], ["近 28 天", days(28)], ["近 8 周", weeks(8)], ["近 13 周", weeks(13)], ["近 26 周", weeks(26)],
      ["全部周期", null]];
  };
  const value = (r) => (r ? `${r[0]}~${r[1]}` : "all");

  function triggerLabel(period, summary) {
    const r = range(period);
    if (!r) return { name: "全部周期", from: "", to: "" };
    const hit = presets(summary).find(([name, pr]) => name !== "全部周期" && pr && pr[0] === r[0] && pr[1] === r[1]);
    return { name: hit ? hit[0] : "", from: r[0], to: r[1] };
  }

  function trigger(period, summary, options = {}) {
    const { name, from, to } = triggerLabel(period, summary);
    const text = from ? `${name ? `${name}：` : ""}<span>${from}</span><i>-</i><span>${to}</span>` : name;
    const id = escape(idOf(options));
    return `<div class="perf-date" id="${id}"><button type="button" class="perf-date-trigger${period !== "all" ? " is-set" : ""}" id="${id}-trigger" aria-haspopup="dialog" aria-expanded="false" aria-label="${escape(labelOf(options))}">${text}<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/></svg></button></div>`;
  }

  // ---- popover ---------------------------------------------------------------
  let state = null; // { mode, from, hover, view: {y, m}, ctx }

  function dayCell(v, month, s) {
    const out = parse(v).m !== month, disabled = v > maxDate(s.ctx);
    if (out) return `<span class="perf-cal-cell is-blank"></span>`;
    const lo = s.from && (s.hover && s.mode === "custom" && !s.done ? [s.from, s.hover].sort() : [s.from, s.to || s.from]);
    const inRange = lo && v >= lo[0] && v <= lo[1], edge = lo && (v === lo[0] || v === lo[1]);
    const cls = ["perf-cal-cell", inRange ? "is-in" : "", edge ? "is-edge" : "", disabled ? "is-off" : "", v === today() ? "is-today" : ""].join(" ");
    return `<button type="button" class="${cls}" data-date="${v}" ${disabled ? "disabled" : ""}>${parse(v).d}</button>`;
  }

  function monthMarkup(y, m, s, side) {
    const prev = '<button type="button" data-nav="-12" aria-label="上一年">«</button><button type="button" data-nav="-1" aria-label="上个月">‹</button>';
    const next = '<button type="button" data-nav="1" aria-label="下个月">›</button><button type="button" data-nav="12" aria-label="下一年">»</button>';
    const lead = (new Date(Date.UTC(y, m, 1)).getUTCDay() + 6) % 7, total = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
    const cells = Array.from({ length: lead }, () => '<span class="perf-cal-cell is-blank"></span>')
      .concat(Array.from({ length: total }, (_, i) => dayCell(iso(y, m, i + 1), m, s)));
    return `<div class="perf-cal-month"><div class="perf-cal-title">${side === "left" ? prev : '<span class="perf-cal-pad"></span>'}<b>${y} 年 ${m + 1} 月</b>${side === "right" ? next : '<span class="perf-cal-pad"></span>'}</div>
      <div class="perf-cal-grid">${WEEKDAYS.map((w) => `<span class="perf-cal-week">${w}</span>`).join("")}${cells.join("")}</div></div>`;
  }

  function panelMarkup() {
    const s = state, { y, m } = s.view, ny = m === 11 ? y + 1 : y, nm = (m + 1) % 12;
    const current = s.ctx.period(), chosen = triggerLabel(current, s.ctx.summary?.()).name || (current === "all" ? "全部周期" : "");
    return `<div class="perf-date-panel" role="dialog" aria-label="选择${escape(labelOf(s.ctx))}">
      <div class="perf-date-presets">${presets(s.ctx.summary?.()).map(([name, r]) => `<button type="button" data-preset="${value(r)}" class="${name === chosen ? "is-active" : ""}">${name}</button>`).join("")}</div>
      <div class="perf-date-main">
        <div class="perf-date-modes" role="group" aria-label="选择方式">${MODES.map(([k, label]) => `<button type="button" data-mode="${k}" aria-pressed="${k === s.mode}">${label}</button>`).join("")}</div>
        <div class="perf-cal-months">${monthMarkup(y, m, s, "left")}${monthMarkup(ny, nm, s, "right")}</div>
        <p class="perf-date-hint">${escape(s.ctx.hint || "看板按周、海外达人按导出月份汇总，所选日期碰到的整周或整月都会计入；视频明细按发布日期筛选。")}</p>
      </div></div>`;
  }

  function close() {
    state?.host.querySelector(".perf-date-panel")?.remove();
    state?.host.querySelector(".perf-date-trigger")?.setAttribute("aria-expanded", "false");
    state = null;
  }

  function paint() {
    const host = state?.host;
    if (!host || !state) return;
    host.querySelector(".perf-date-panel")?.remove();
    host.insertAdjacentHTML("beforeend", panelMarkup());
  }

  function apply(from, to) {
    const ctx = state.ctx;
    close();
    ctx.choose(from ? `${from}~${to}` : "all");
  }

  function pick(v) {
    const s = state;
    const last = maxDate(s.ctx);
    if (v > last) return;
    if (s.mode === "day") return apply(v, v);
    if (s.mode === "week") { const start = mondayOf(v), end = shift(start, 6); return apply(start, end > last ? last : end); }
    if (s.mode === "month") { const { y, m } = parse(v), end = monthEnd(y, m); return apply(iso(y, m, 1), end > last ? last : end); }
    if (!s.from || s.done) { s.from = v; s.to = null; s.done = false; s.hover = null; return paint(); }
    const [a, b] = [s.from, v].sort();
    apply(a, b);
  }

  function open(ctx, host) {
    close();
    const r = range(ctx.period()), end = r ? r[1] : anchorOf(ctx.summary?.()), { y, m } = parse(end);
    // Show the chosen end month on the right, the month before it on the left.
    state = { mode: "custom", from: r?.[0] || null, to: r?.[1] || null, done: Boolean(r), hover: null, ctx, host, view: m === 0 ? { y: y - 1, m: 11 } : { y, m: m - 1 } };
    host.querySelector(".perf-date-trigger").setAttribute("aria-expanded", "true");
    paint();
  }

  let wired = false;
  function wire() {
    if (wired) return;
    wired = true;
    document.addEventListener("click", (event) => {
      if (!state) return;
      if (!state.host.isConnected || !event.composedPath().includes(state.host)) close();
    });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && state) { const trigger = state.host.querySelector(".perf-date-trigger"); close(); trigger?.focus(); } });
  }

  function bind(ctx) {
    wire();
    const host = document.getElementById(idOf(ctx));
    if (state && (!state.host.isConnected || state.host === host)) close();
    if (!host) return;
    host.querySelector(".perf-date-trigger").onclick = () => (state?.host === host ? close() : open(ctx, host));
    host.onclick = (event) => {
      if (state?.host !== host) return;
      const el = event.target.closest("button");
      if (!el || el.classList.contains("perf-date-trigger")) return;
      if (el.dataset.preset) { const [a, b] = el.dataset.preset === "all" ? [null, null] : el.dataset.preset.split("~"); return apply(a, b); }
      if (el.dataset.date) return pick(el.dataset.date);
      if (el.dataset.mode) { state.mode = el.dataset.mode; state.from = state.to = state.hover = null; state.done = false; return paint(); }
      if (el.dataset.nav) {
        const total = state.view.y * 12 + state.view.m + Number(el.dataset.nav);
        state.view = { y: Math.floor(total / 12), m: ((total % 12) + 12) % 12 };
        paint();
      }
    };
    host.onmouseover = (event) => {
      const el = event.target.closest("[data-date]");
      if (state?.host !== host || state.mode !== "custom" || !state.from || state.done || !el || el.disabled || state.hover === el.dataset.date) return;
      state.hover = el.dataset.date;
      host.querySelectorAll("[data-date]").forEach((cell) => {
        const [a, b] = [state.from, state.hover].sort(), v = cell.dataset.date;
        cell.classList.toggle("is-in", v >= a && v <= b);
        cell.classList.toggle("is-edge", v === a || v === b);
      });
    };
  }

  window.PerfDatePicker = { trigger, bind };
})();
