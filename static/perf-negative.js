"use strict";

// 反面素材库: the 带货数据 page's 「反面素材」 tab. Our own videos that did badly — low play, or high play with few
// orders — picked from the sales exports by thresholds a person can change, plus videos added by hand.
// Data and rules live in negative_library.py; this module only draws and edits them.
(() => {
  // One formatter for the page: toLocaleString builds a new one on every call, and the boards format tens of thousands of numbers.
  const usWhole = new Intl.NumberFormat("en-US");
  const count = (v) => (v == null ? "—" : usWhole.format(Math.round(v)));
  const pct = (v) => (v == null ? "—" : Number(v).toFixed(2) + "%");
  const sections = [["weak", "高播放低转化"], ["low", "低播放"], ["manual", "手动加入"], ["hidden", "已移出"]];
  const settingFields = [
    ["low_views", "低播放：播放低于", "次"],
    ["high_views", "高播放：播放不低于", "次"],
    ["max_orders", "且出单不超过", "单"],
    ["orders_per_10k", "或每万播放出单不超过", "单"],
    ["min_age_days", "发布满", "天才算（数据要沉淀）"],
    ["recent_days", "只看最近", "天发布的（0 = 全部）"],
  ];
  const state = { data: null, shop: "", loading: null, error: "", section: "weak", editing: "", adding: false, sequence: 0 };
  let root = null, onProduct = () => true;

  const shown = (rows) => rows.filter((row) => onProduct(row.product));
  const videoLink = (id) => `#videos?tab=list&video=${encodeURIComponent(id)}`;

  function load(shop, force = false) {
    if (!force && state.shop === shop && (state.data || state.loading)) return state.loading || Promise.resolve();
    const sequence = ++state.sequence;
    state.shop = shop;
    state.error = "";
    if (force) state.data = null;
    state.loading = api(`/api/negative-library?shop=${encodeURIComponent(shop)}`).then((data) => {
      if (sequence === state.sequence) state.data = data;
    }, (error) => {
      if (sequence === state.sequence) state.error = error.message;
    }).finally(() => {
      if (sequence === state.sequence) { state.loading = null; draw(); }
    });
    return state.loading;
  }

  async function send(body, button) {
    if (button) button.disabled = true;
    try {
      const data = await api("/api/negative-library", { method: "POST", body: { ...body, shop: state.shop } });
      state.data = data;
      return true;
    } catch (error) {
      toast(error.message, true);
      return false;
    } finally {
      if (button) button.disabled = false;
    }
  }

  function settingsMarkup(d) {
    const s = d.settings;
    const typical = d.medians.ctr != null && d.medians.ctor != null ? s.high_views * d.medians.ctr / 100 * d.medians.ctor / 100 : null;
    return `<details class="neg-settings"${state.settingsOpen ? " open" : ""}><summary>门槛：低播放 &lt; ${count(s.low_views)}；高播放 ≥ ${count(s.high_views)} 且出单 ≤ ${count(s.max_orders)}（或每万播放 ≤ ${s.orders_per_10k} 单）；发布满 ${s.min_age_days} 天${s.recent_days ? `、最近 ${s.recent_days} 天内` : ""}<span>调整</span></summary>
      <form class="neg-settings-form">${settingFields.map(([key, label, unit]) => `<label><span>${label}</span><input type="number" min="0" step="${key === "orders_per_10k" ? "0.1" : "1"}" name="${key}" value="${esc(s[key])}"><small>${unit}</small></label>`).join("")}
        <div class="neg-settings-actions"><button type="submit" class="primary compact">保存门槛</button><button type="button" class="subtle compact" data-neg-reset>恢复默认</button></div></form>
      ${typical != null ? `<p class="neg-hint">参考：播放 ≥ ${count(s.high_views)} 的同类视频，点击率中位 ${pct(d.medians.ctr)}、点进下单中位 ${pct(d.medians.ctor)}，按这个水平 ${count(s.high_views)} 播放大约出 ${typical.toFixed(1)} 单。出单门槛定得比这个低，挑出来的才是真正转化差的。</p>` : ""}</details>`;
  }

  function reasonCell(row, key) {
    if (state.editing === key) {
      return `<form class="neg-reason-form" data-neg-reason="${esc(key)}"><textarea name="reason" rows="3" maxlength="1000" placeholder="${esc(row.auto_reason || "写下你判断的原因")}">${esc(row.reason || "")}</textarea><div><button type="submit" class="primary compact">保存</button><button type="button" class="subtle compact" data-neg-cancel>取消</button></div></form>`;
    }
    const own = row.reason ? `<p class="neg-reason">${esc(row.reason)}</p>` : "";
    const auto = row.auto_reason ? `<p class="neg-auto${row.reason ? " is-muted" : ""}">${row.reason ? "自动判断：" : ""}${esc(row.auto_reason)}</p>` : "";
    return `${own}${auto}<button type="button" class="subtle compact" data-neg-edit="${esc(key)}">${row.reason ? "改原因" : "写原因"}</button>`;
  }

  function videoTable(rows, hidden) {
    if (!rows.length) return `<p class="context-note">${hidden ? "没有移出的视频。" : "按当前门槛没有符合的视频。"}</p>`;
    return `<div class="perf-table-wrap"><table class="perf-table neg-table"><thead><tr><th>发布</th><th>产品</th><th>视频</th><th class="num">播放</th><th class="num">出单</th><th class="num">点击率</th><th class="num">点进下单</th><th>疑似原因</th><th></th></tr></thead><tbody>${rows.map((row) => `<tr>
      <td class="neg-date">${esc(row.posted_on || "—")}</td><td>${esc(row.product || "—")}</td>
      <td class="neg-video"><a href="${videoLink(row.video_id)}">${esc(row.headline || row.video_id)}</a><small>${esc(row.account || "")}${row.source === "pinned" ? " · 手动加入" : ""}${hidden ? ` · ${esc(row.kind_label)}` : ""}</small></td>
      <td class="num">${count(row.views)}</td><td class="num">${count(row.orders)}</td><td class="num">${pct(row.ctr_pct)}</td><td class="num">${pct(row.ctor_pct)}</td>
      <td class="neg-reason-cell">${reasonCell(row, row.video_id)}</td>
      <td class="neg-row-actions">${hidden ? `<button type="button" class="secondary compact" data-neg-hide="${esc(row.video_id)}" data-hidden="0">放回</button>`
        : `<button type="button" class="subtle compact" data-neg-hide="${esc(row.video_id)}" data-hidden="1" title="不算反面，从库里移出（可在「已移出」放回）">移出</button>`}${row.source === "pinned" ? `<button type="button" class="subtle compact" data-neg-unpin="${esc(row.video_id)}" title="取消手动加入，按门槛决定是否列出">取消加入</button>` : ""}</td></tr>`).join("")}</tbody></table></div>`;
  }

  function manualForm(entry = {}) {
    const kinds = state.data.kinds, kind = entry.kind || "weak";
    return `<form class="neg-manual-form" data-neg-manual="${esc(entry.id || "")}">
      <label><span>类别</span><select name="kind">${Object.entries(kinds).map(([k, label]) => `<option value="${k}"${kind === k ? " selected" : ""}>${label}</option>`).join("")}</select></label>
      <label class="wide"><span>视频 ID 或链接</span><input name="link" maxlength="500" value="${esc(entry.link || "")}" placeholder="后台数据里有的视频 ID 会直接带出播放和出单"></label>
      <label class="wide"><span>标题</span><input name="title" maxlength="200" value="${esc(entry.title || "")}"></label>
      <label><span>产品</span><input name="product" maxlength="40" value="${esc(entry.product || window.WorkbenchShell?.product() || "")}"></label>
      <label><span>发布日期</span><input name="posted_on" type="date" value="${esc(entry.posted_on || "")}"></label>
      <label><span>播放</span><input name="views" type="number" min="0" value="${esc(entry.views ?? "")}"></label>
      <label><span>出单数</span><input name="orders" type="number" min="0" value="${esc(entry.orders ?? "")}"></label>
      <label class="wide"><span>疑似原因</span><textarea name="reason" rows="2" maxlength="1000">${esc(entry.reason || "")}</textarea></label>
      <div class="neg-settings-actions wide"><button type="submit" class="primary compact">${entry.id ? "保存" : "加入反面库"}</button><button type="button" class="subtle compact" data-neg-manual-cancel>取消</button></div></form>`;
  }

  function manualTable(rows) {
    const list = rows.length ? `<div class="perf-table-wrap"><table class="perf-table neg-table"><thead><tr><th>类别</th><th>发布</th><th>产品</th><th>视频</th><th class="num">播放</th><th class="num">出单</th><th>疑似原因</th><th></th></tr></thead><tbody>${rows.map((row) => state.editing === row.id
      ? `<tr><td colspan="8">${manualForm(row)}</td></tr>`
      : `<tr><td>${esc(row.kind_label)}</td><td class="neg-date">${esc(row.posted_on || "—")}</td><td>${esc(row.product || "—")}</td>
        <td class="neg-video">${/^https?:\/\//.test(row.link || "") ? `<a href="${esc(row.link)}" target="_blank" rel="noopener">${esc(row.title || row.link)}</a>` : `<span>${esc(row.title || row.link)}</span>`}${row.title && row.link && !/^https?:\/\//.test(row.link) ? `<small>${esc(row.link)}</small>` : ""}</td>
        <td class="num">${count(row.views)}</td><td class="num">${count(row.orders)}</td><td class="neg-reason-cell">${row.reason ? `<p class="neg-reason">${esc(row.reason)}</p>` : '<p class="neg-auto">没写原因</p>'}</td>
        <td class="neg-row-actions"><button type="button" class="subtle compact" data-neg-manual-edit="${esc(row.id)}">编辑</button><button type="button" class="subtle compact" data-neg-manual-delete="${esc(row.id)}">删除</button></td></tr>`).join("")}</tbody></table></div>`
      : '<p class="context-note">还没有手动加入的条目。后台数据里没有的视频（例如别的店、还没导出的），可以在这里手动登记。</p>';
    return `${state.adding ? manualForm() : `<button type="button" class="secondary compact neg-add" data-neg-add>${icon("plus")}手动加入</button>`}${list}`;
  }

  function copyMaterial() {
    const d = state.data, lines = ["反面素材库（自制视频里效果差的，写新内容时避开这些做法）", ""];
    for (const [key, label] of sections.slice(0, 3)) {
      const rows = shown(d[key] || []);
      if (!rows.length) continue;
      lines.push(`## ${label}（${rows.length} 条）`);
      for (const row of rows) {
        const title = row.headline || row.title || row.link || row.video_id;
        lines.push(`- ${row.posted_on || "日期不详"} · ${row.product || "产品不详"} · ${title} · 播放 ${count(row.views)} · 出单 ${count(row.orders)}${key === "manual" ? ` · ${row.kind_label}` : ""}`);
        const reason = row.reason || row.auto_reason;
        if (reason) lines.push(`  原因：${reason}`);
      }
      lines.push("");
    }
    return lines.join("\n");
  }

  function markup() {
    if (state.error) return `<div class="empty">${icon("chart")}<h3>反面素材库暂不可用</h3><p>${esc(state.error)}</p></div>`;
    const d = state.data;
    if (!d) return '<p class="context-note" role="status">正在读取反面素材…</p>';
    const c = d.counts;
    const tabs = sections.map(([key, label]) => `<button type="button" role="tab" data-neg-section="${key}" aria-selected="${key === state.section}" class="${key === state.section ? "is-active" : ""}">${label}<small>${shown(d[key] || []).length}</small></button>`).join("");
    const body = state.section === "manual" ? manualTable(shown(d.manual)) : videoTable(shown(d[state.section] || []), state.section === "hidden");
    return `<div class="neg-head"><p>从后台视频数据自动挑出效果差的自制视频，${d.data_until ? `数据到 ${esc(d.data_until)}` : "还没有导入视频数据"}；本次看了 ${count(c.in_scope)} 条（${count(c.too_new)} 条发布未满 ${d.settings.min_age_days} 天、${count(c.older)} 条更早的没算）。原因先按数据自动判断，你写的原因优先。</p><button type="button" class="secondary compact" data-neg-copy title="复制当前产品的反面条目，写稿时可以贴给 AI 对照">复制给 AI 参考</button></div>
      ${settingsMarkup(d)}
      <div class="neg-sections" role="tablist" aria-label="反面素材分类">${tabs}</div>
      <div class="neg-body">${body}</div>`;
  }

  function draw() {
    if (!root || !root.isConnected) return;
    root.innerHTML = markup();
    bind();
  }

  function bind() {
    root.querySelectorAll("[data-neg-section]").forEach((button) => (button.onclick = () => { state.section = button.dataset.negSection; state.editing = ""; state.adding = false; draw(); }));
    const settings = root.querySelector(".neg-settings");
    settings?.addEventListener("toggle", () => { state.settingsOpen = settings.open; });
    const form = root.querySelector(".neg-settings-form");
    if (form) form.onsubmit = async (event) => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form).entries());
      if (await send({ action: "settings", settings: values }, form.querySelector("[type=submit]"))) { draw(); toast("门槛已保存"); }
    };
    const reset = root.querySelector("[data-neg-reset]");
    if (reset) reset.onclick = async () => { if (await send({ action: "reset_settings" }, reset)) draw(); };
    root.querySelector("[data-neg-copy]")?.addEventListener("click", () => copyText(copyMaterial(), "反面素材已复制"));
    root.querySelectorAll("[data-neg-edit]").forEach((button) => (button.onclick = () => { state.editing = button.dataset.negEdit; draw(); root.querySelector(".neg-reason-form textarea")?.focus(); }));
    root.querySelectorAll("[data-neg-cancel],[data-neg-manual-cancel]").forEach((button) => (button.onclick = () => { state.editing = ""; state.adding = false; draw(); }));
    root.querySelectorAll("[data-neg-reason]").forEach((reasonForm) => (reasonForm.onsubmit = async (event) => {
      event.preventDefault();
      const reason = reasonForm.elements.reason.value;
      if (await send({ action: "note", video_id: reasonForm.dataset.negReason, reason }, reasonForm.querySelector("[type=submit]"))) { state.editing = ""; draw(); }
    }));
    root.querySelectorAll("[data-neg-hide]").forEach((button) => (button.onclick = async () => {
      if (await send({ action: "note", video_id: button.dataset.negHide, hidden: button.dataset.hidden === "1" }, button)) draw();
    }));
    root.querySelectorAll("[data-neg-unpin]").forEach((button) => (button.onclick = async () => {
      if (await send({ action: "note", video_id: button.dataset.negUnpin, pinned: null }, button)) draw();
    }));
    const add = root.querySelector("[data-neg-add]");
    if (add) add.onclick = () => { state.adding = true; state.editing = ""; draw(); root.querySelector(".neg-manual-form [name=link]")?.focus(); };
    root.querySelectorAll("[data-neg-manual-edit]").forEach((button) => (button.onclick = () => { state.editing = button.dataset.negManualEdit; state.adding = false; draw(); }));
    root.querySelectorAll("[data-neg-manual-delete]").forEach((button) => (button.onclick = async () => {
      if (!confirm("删除这条手动条目？")) return;
      if (await send({ action: "delete_manual", id: button.dataset.negManualDelete }, button)) draw();
    }));
    root.querySelectorAll(".neg-manual-form").forEach((manual) => (manual.onsubmit = async (event) => {
      event.preventDefault();
      const entry = Object.fromEntries(new FormData(manual).entries()), id = manual.dataset.negManual;
      const before = state.data;
      if (!(await send(id ? { action: "update_manual", id, entry } : { action: "add_manual", entry }, manual.querySelector("[type=submit]")))) return;
      state.editing = "";
      state.adding = false;
      // A video the exports already have is pinned instead of copied; it then shows in its own section.
      const pinned = !id && state.data.manual.length === before.manual.length;
      if (pinned) { state.section = entry.kind; toast("后台数据里有这条视频，已按数据加入，播放和出单自动带出。"); }
      draw();
    }));
  }

  // Draw the tab into `node` for one shop; `matches(product)` keeps rows to the page's chosen product.
  function mount(node, shop, matches) {
    root = node;
    onProduct = matches || (() => true);
    if (state.shop !== shop) { state.editing = ""; state.adding = false; }
    draw();
    load(shop);
  }

  window.PerfNegative = { mount, reload: () => state.shop && load(state.shop, true) };
})();
