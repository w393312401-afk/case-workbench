"use strict";

// One persisted basket shared by the review, expression and video pages.
window.MaterialBasket = (() => {
  const kinds = { review: "用户评论", expression: "案例表达", hook: "视频钩子" };
  const sourceLabel = item => ({tiktok_video:"TikTok 视频评论",tiktok_shop:"TikTok Shop 评价",amazon:"亚马逊评价",reddit:"Reddit 讨论",facebook:"Facebook 评论"}[item.source_label] || item.source_label || item.source_id);
  // data is what the dialog shows; deferred holds a newer read that arrived while a note had focus.
  const basket = { data: null, deferred: null, stale: false, request: 0, busy: false, error: "", drafts: new Map(), excluded: new Set(), undo: null, filter: { type: "", query: "" } };
  // The note text and item version each textarea started from; a draft keeps that version until it is saved.
  const baselines = new Map();
  const product = () => window.WorkbenchShell?.product() || "";
  const pageURL = (hash, selected) => window.WorkbenchShell?.url(hash, selected) || hash;
  const matches = item => !window.WorkbenchShell || window.WorkbenchShell.matchesProduct(item.product);
  const visible = () => (basket.data?.items || []).filter(matches);
  const latest = () => basket.deferred || basket.data;
  const current = id => (latest()?.items || []).find(item => item.id === id);
  const noteOf = item => basket.drafts.get(item.id)?.note ?? item.note ?? "";
  const linksOf = item => item.type === "review" ? (basket.data?.theme_links || []).filter(link => link.review_id === item.source_id) : [];
  const safeURL = value => /^(https?:\/\/|#)/i.test(String(value || "")) ? value : "";
  const emit = () => window.dispatchEvent(new CustomEvent("workbench:basketchange", { detail: { source: "global" } }));
  const requestID = () => crypto.randomUUID();
  const post = (path, body) => api(path, { method: "POST", body });
  // In the all-products view visible() spans every product, so whole-basket actions need a chosen product.
  const scoped = () => Boolean(product());
  const maxLength = key => Number.isInteger(basket.data?.limits?.[key]) ? ` maxlength="${basket.data.limits[key]}"` : "";
  const themeLabel = link => `${link.role === "concern" ? "方向顾虑" : "产品卖点"} · ${link.target_label || link.target_id}`;
  // Every row has the same controls; screen readers tell them apart by this opening of the quote.
  const excerpt = item => { const text = String(item.text || "").replace(/\s+/g, " ").trim(); return text.length > 30 ? `${text.slice(0, 30)}…` : text || item.source_id; };
  const chars = count => count >= 10000 ? `${(count / 10000).toFixed(1)} 万字符` : `${count} 字符`;
  // The task freezes these items as JSON, so this is close to what the selection adds to the writing context.
  const contextSize = items => items.reduce((sum, item) => sum + JSON.stringify(item, null, 2).length, 0);
  const filtering = () => Boolean(basket.filter.type || basket.filter.query.trim());
  // Controls whose focus survives a row being rebuilt; a note textarea is never rebuilt while it has focus.
  const focusKeys = ["data-material-save", "data-material-remove", "data-material-theme", "data-material-select", "data-theme-remove", "data-material-filter", "data-material-all", "data-material-undo", "data-material-clear", "data-material-copy", "data-material-write", "data-material-retry", "data-material-close"];
  let pointerAt = 0, backdropDown = false, undoTimer = 0;
  // Undo outlives closing the basket for this long, so a quick close is not the end of it.
  const UNDO_MS = 60000;
  function offerUndo(message, run) {
    clearTimeout(undoTimer);
    basket.undo = { message, run };
    undoTimer = setTimeout(() => { basket.undo = null; render(); }, UNDO_MS);
  }
  // Notes in conflict are never saved on their own, so leaving them behind is said out loud.
  function warnConflicts() {
    const count = [...basket.drafts.values()].filter(draft => draft.conflictAt).length;
    if (count) toast(`${count} 条备注与其他页面的修改冲突，尚未保存；重新打开摘录篮核对后再保存。`, true);
  }
  // Only a press and release both on the backdrop closes; a text selection dragged out of the panel does not.
  function outside(event) {
    const rect = dialog().getBoundingClientRect();
    return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
  }
  // Unticked items stay unticked across reloads in this browser; ids are unique across products.
  const excludedKey = "workbench-material-excluded-v1";
  try { const saved = JSON.parse(localStorage.getItem(excludedKey) || "[]"); if (Array.isArray(saved)) saved.forEach(id => typeof id === "string" && basket.excluded.add(id)); } catch { /* Selection then starts fully ticked. */ }
  function saveExcluded() { try { localStorage.setItem(excludedKey, JSON.stringify([...basket.excluded])); } catch { /* Kept for this page only. */ } }
  function writeBlock(items = selection()) {
    if (new Set(items.map(item => String(item.product || "").toLocaleLowerCase())).size > 1) return "所选素材来自不同产品，请先选择一个具体产品再带入写稿。";
    const limit = basket.data?.limits?.prompt_chars;
    return limit && contextSize(items) >= limit ? `所选素材已超过写稿任务的上下文上限（${chars(limit)}），请减少勾选。` : "";
  }
  function shown(item) {
    const query = basket.filter.query.trim().toLocaleLowerCase();
    if (basket.filter.type && item.type !== basket.filter.type) return false;
    return !query || [item.text, noteOf(item), sourceLabel(item), item.source_id].some(value => String(value || "").toLocaleLowerCase().includes(query));
  }
  function updateFooter(modal) {
    const items = selection(), block = writeBlock(items), size = contextSize(items), limit = basket.data?.limits?.prompt_chars;
    const unsaved = visible().filter(item => basket.drafts.has(item.id)).length, outside = filtering() ? items.filter(item => !shown(item)).length : 0;
    const warning = block || (limit && size >= limit * 2 / 3 ? `素材约占 ${chars(size)}，接近写稿任务的上下文上限（${chars(limit)}），提交时可能超出。` : "");
    const summary = modal.querySelector("#material-selection-count"), html = `已选 ${items.length} 条${outside ? `（${outside} 条不在当前筛选中）` : ""}${items.length ? ` · 约 ${chars(size)}` : ""}${unsaved ? ` · ${unsaved} 条备注未保存` : ""}${warning ? `<small class="material-write-block">${esc(warning)}</small>` : ""}`;
    // This is a live region: rewriting it with the same text would be announced again on every render.
    if (summary.dataset.html !== html) { summary.dataset.html = html; summary.innerHTML = html; }
    modal.querySelector("[data-material-copy]").disabled = !items.length;
    modal.querySelector("[data-material-write]").disabled = !items.length || basket.busy || Boolean(block);
  }
  // Filtering only hides rows, so typing a search never rebuilds the list under the cursor.
  function applyFilter(modal) {
    const byId = new Map(visible().map(item => [item.id, item]));
    let count = 0;
    modal.querySelectorAll(".material-item").forEach(node => {
      const item = byId.get(node.dataset.materialId), show = Boolean(item && shown(item));
      node.hidden = !show;
      count += show;
    });
    modal.querySelectorAll("[data-material-filter]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.materialFilter === basket.filter.type)));
    const none = modal.querySelector("[data-material-nomatch]");
    if (none) none.hidden = count > 0;
    updateFooter(modal);
  }
  function refreshIndicators() {
    const modal = dialog();
    if (!modal?.open) return;
    modal.querySelectorAll("[data-material-unsaved]").forEach(node => {
      const draft = basket.drafts.get(node.dataset.materialUnsaved);
      node.hidden = !draft;
      node.textContent = draft?.conflictAt ? "备注冲突，未保存" : "未保存";
    });
    if (modal.querySelector("#material-selection-count")) updateFooter(modal);
  }
  function dialog() { return document.querySelector("#material-basket-dialog"); }
  function editing() { return Boolean(document.activeElement?.matches?.("[data-material-note], [data-material-search]") && dialog()?.contains(document.activeElement)); }
  function keepDraft(field) {
    const id = field.dataset.materialNote, baseline = baselines.get(id), prior = basket.drafts.get(id);
    if (!baseline) return;
    if (field.value === baseline.note && !prior?.conflictAt) basket.drafts.delete(id);
    else if (prior || field.value !== baseline.note) basket.drafts.set(id, { ...prior, note: field.value, base: prior?.base ?? baseline.revision });
  }
  function collectVisibleDrafts() { dialog()?.querySelectorAll("[data-material-note]").forEach(keepDraft); }
  function selection() { return visible().filter(item => !basket.excluded.has(item.id)).map(item => ({ ...item, note: noteOf(item) })); }
  function updateCount() {
    const count = visible().length;
    document.querySelectorAll("[data-material-count]").forEach(node => { node.textContent = basket.data ? String(count) : "—"; });
    document.querySelectorAll("[data-material-open]").forEach(node => node.setAttribute("aria-label", `摘录篮${basket.data ? `（${count}）` : ""}`));
  }
  function sourceURL(item) {
    if (item.type === "expression") return pageURL(`#case/${encodeURIComponent(item.source_id)}/script`, item.product);
    if (item.type === "hook") return pageURL(`#videos?tab=list&video=${encodeURIComponent(item.source_id)}`, item.product);
    return safeURL(item.source_url) || pageURL(`#reviews?q=${encodeURIComponent(item.text?.slice(0, 60) || "")}`, item.product);
  }
  function markdown(items = selection()) {
    const lines = [`# 写稿素材（${items.length} 条）`, "> 评论里的说法均未核实，评论者不一定是买家；不得写成说话者的亲历、粉丝提问或买家评价。案例表达和视频钩子仅供参考，使用时保留来源和适用边界。", ""];
    for (const [type, name] of Object.entries(kinds)) {
      const rows = items.filter(item => item.type === type);
      if (!rows.length) continue;
      lines.push(`## ${name}`);
      for (const item of rows) {
        lines.push(`- ${item.text || "（原内容暂不可用）"}`, `  - 来源：${sourceLabel(item)}｜编号 ${item.source_id}${item.source_url ? `｜${item.source_url}` : ""}`);
        if (item.note) lines.push(`  - 备注：${item.note}`);
        if (item.unverified) lines.push("  - 真实性：未经核实，不作为产品事实或亲历证明。");
      }
      lines.push("");
    }
    return lines.join("\n").trim();
  }
  // The frame is built once per opening; renders then patch it, so scrolling, focus and unchanged rows survive.
  function frame() {
    return `<div class="dialog-heading"><div><span class="eyebrow">WRITING MATERIALS</span><h2 id="material-basket-title">摘录篮 <small data-material-total></small></h2><p data-material-scope></p></div><button type="button" class="icon-button" data-material-close aria-label="关闭摘录篮">${icon("close")}</button></div><div class="material-basket-body"><p class="material-note">勾选这次要带入写稿的素材。备注离开输入框后自动保存；评论、参考表达与钩子会保留来源。</p><div id="material-basket-error" class="data-warning" role="alert" hidden><span data-material-error></span> <button type="button" class="subtle" data-material-retry>重新读取</button></div><div data-material-undo-host></div><div data-material-toolbar></div><div class="material-list"></div></div><div class="dialog-footer material-basket-footer"><span id="material-selection-count" aria-live="polite" aria-atomic="true"></span><div><button type="button" class="subtle" data-material-clear>清空当前产品</button><button type="button" class="secondary" data-material-copy>${icon("copy")}复制所选</button><button type="button" class="primary" data-material-write>带入写稿</button></div></div>`;
  }
  function row(item) {
    const links = linksOf(item), draft = basket.drafts.get(item.id), quote = esc(excerpt(item));
    return `<article class="material-item" data-material-id="${esc(item.id)}"><div class="material-item-heading"><label><input type="checkbox" data-material-select="${esc(item.id)}" aria-label="带入写稿：${esc(kinds[item.type] || item.type)} · ${quote}" ${basket.excluded.has(item.id) ? "" : "checked"}> <span>${esc(kinds[item.type] || item.type)}</span></label><button type="button" class="subtle" data-material-remove="${esc(item.id)}" aria-label="移出摘录篮：${quote}">移出摘录篮</button></div><blockquote>${esc(item.text || "原内容暂不可用，请检查来源。")}</blockquote><div class="material-source"><a href="${esc(sourceURL(item))}" ${/^https?:/i.test(sourceURL(item)) ? 'target="_blank" rel="noopener noreferrer"' : ""}>${esc(sourceLabel(item))}</a>${item.unverified ? '<span>未经核实</span>' : ""}</div><label class="material-note-field"><span>写稿备注<span class="material-unsaved" data-material-unsaved="${esc(item.id)}" ${draft ? "" : "hidden"}>${draft?.conflictAt ? "备注冲突，未保存" : "未保存"}</span></span><textarea data-material-note="${esc(item.id)}" rows="2"${maxLength("note")} placeholder="记下用途、画面或使用边界">${esc(noteOf(item))}</textarea></label><div class="material-item-actions"><button type="button" class="subtle" data-material-save="${esc(item.id)}" aria-label="保存备注：${quote}">保存备注</button>${item.type === "review" ? `<button type="button" class="subtle" data-material-theme="${esc(item.source_id)}" aria-label="关联卖点或顾虑：${quote}">关联卖点或顾虑</button>` : ""}</div>${links.length ? `<div class="material-themes">${links.map(link => `<span>${esc(themeLabel(link))}<button type="button" class="material-theme-remove" data-theme-remove="${esc(link.id)}" aria-label="删除主题关联：${esc(themeLabel(link))}">×</button></span>`).join("")}</div>` : ""}</article>`;
  }
  // Everything a row shows; a row is rebuilt only when this changes.
  function rowSignature(item) {
    const draft = basket.drafts.get(item.id);
    return JSON.stringify([item, noteOf(item), Boolean(draft), Boolean(draft?.conflictAt), basket.excluded.has(item.id), linksOf(item).map(link => [link.id, themeLabel(link)]), basket.data?.limits?.note]);
  }
  function patchList(modal, rows) {
    const list = modal.querySelector(".material-list");
    if (!basket.data || !rows.length) {
      const state = basket.data ? `empty:${product()}` : "loading";
      if (list.dataset.state !== state) {
        list.dataset.state = state;
        list.innerHTML = !basket.data ? '<p role="status">正在读取素材…</p>' : `<div class="empty"><h3>还没有素材</h3><p>可以从这三处加入：</p><div class="material-empty-links"><a href="${esc(pageURL("#reviews"))}">用户声音 · 摘录评论</a><a href="${esc(pageURL("#library?view=native"))}">案例库 · 加入原生表达</a><a href="${esc(pageURL("#videos?tab=list"))}">视频数据 · 摘录钩子</a></div></div>`;
      }
      return;
    }
    if (list.dataset.state !== "rows") { list.dataset.state = "rows"; list.innerHTML = ""; }
    const existing = new Map([...list.children].map(node => [node.dataset.materialId, node]));
    let previous = null;
    for (const item of rows) {
      const signature = rowSignature(item);
      let node = existing.get(item.id);
      existing.delete(item.id);
      if (!node || node.dataset.signature !== signature) {
        const holder = document.createElement("template");
        holder.innerHTML = row(item);
        const fresh = holder.content.firstElementChild;
        fresh.dataset.signature = signature;
        if (node) node.replaceWith(fresh);
        node = fresh;
      }
      const next = previous ? previous.nextElementSibling : list.firstElementChild;
      if (node !== next) list.insertBefore(node, next);
      previous = node;
    }
    existing.forEach(node => node.remove());
  }
  function patchToolbar(modal, rows) {
    const host = modal.querySelector("[data-material-toolbar]");
    const types = Object.keys(kinds).map(type => [type, rows.filter(item => item.type === type).length]).filter(([, count]) => count);
    const signature = JSON.stringify([rows.length, types]);
    if (host.dataset.signature === signature) return;
    host.dataset.signature = signature;
    host.innerHTML = rows.length ? `<div class="material-toolbar"><div class="material-filters" role="group" aria-label="按类型筛选"><button type="button" class="subtle" data-material-filter="" aria-pressed="${!basket.filter.type}">全部 ${rows.length}</button>${types.map(([type, count]) => `<button type="button" class="subtle" data-material-filter="${type}" aria-pressed="${basket.filter.type === type}">${kinds[type]} ${count}</button>`).join("")}</div><input type="search" class="material-search" data-material-search value="${esc(basket.filter.query)}" placeholder="搜索原文、备注或来源" aria-label="搜索素材"><div class="material-bulk" title="只作用于当前显示的素材"><button type="button" class="subtle" data-material-all="1">全选</button><button type="button" class="subtle" data-material-all="0">全不选</button></div></div><p class="material-nomatch" data-material-nomatch hidden>没有符合筛选的素材。</p>` : "";
  }
  function patchUndo(modal) {
    const host = modal.querySelector("[data-material-undo-host]"), message = basket.undo?.message || "";
    if (host.dataset.message === message) return;
    host.dataset.message = message;
    host.innerHTML = message ? `<div class="material-undo" role="status"><span>${esc(message)}</span><button type="button" class="subtle" data-material-undo>撤销</button></div>` : "";
  }
  function render() {
    updateCount();
    const modal = dialog();
    if (!modal?.open) return;
    // Focus can precede the browser's input event. Replacing this node in that
    // interval loses keystrokes before the draft cache has seen them.
    if (editing()) { basket.stale = true; refreshIndicators(); return; }
    basket.stale = false;
    const active = document.activeElement, key = modal.contains(active) && focusKeys.find(name => active.hasAttribute?.(name));
    const focused = key ? `[${key}="${CSS.escape(active.getAttribute(key))}"]` : "";
    if (!modal.querySelector(".material-list")) modal.innerHTML = frame();
    const rows = visible();
    for (const item of rows) baselines.set(item.id, { note: item.note || "", revision: item.item_revision });
    if (basket.filter.type && !rows.some(item => item.type === basket.filter.type)) basket.filter.type = "";
    modal.querySelector("[data-material-total]").textContent = String(rows.length);
    modal.querySelector("[data-material-scope]").textContent = `${product() || "全部产品"} · 页面切换后继续保留`;
    const error = modal.querySelector("#material-basket-error");
    error.hidden = !basket.error;
    error.querySelector("[data-material-error]").textContent = basket.error;
    patchUndo(modal);
    patchToolbar(modal, rows);
    patchList(modal, rows);
    const clear = modal.querySelector("[data-material-clear]");
    clear.disabled = !rows.length || basket.busy || !scoped();
    if (scoped()) clear.removeAttribute("title"); else clear.title = "请先选择一个具体产品再清空";
    modal.querySelectorAll("[data-material-remove], [data-theme-remove], [data-material-undo]").forEach(button => { button.disabled = basket.busy; });
    applyFilter(modal);
    // A rebuilt, removed or now disabled control loses focus; put it back, or on undo after a removal or clearing.
    // Chrome leaves focus on a button that was just disabled, so that counts as lost too.
    if (focused && (!modal.contains(document.activeElement) || document.activeElement.disabled)) [focused, "[data-material-undo]", "[data-material-close]"].map(selector => modal.querySelector(selector)).find(node => node && !node.disabled)?.focus({ preventScroll: true });
  }
  function apply(value) {
    basket.data = value;
    basket.deferred = null;
    const existing = new Set(value.items.map(item => item.id)), excluded = basket.excluded.size;
    for (const id of basket.excluded) if (!existing.has(id)) basket.excluded.delete(id);
    for (const id of basket.drafts.keys()) if (!existing.has(id)) basket.drafts.delete(id);
    if (basket.excluded.size !== excluded) saveExcluded();
  }
  function accept(data) {
    const value = { ...data, items: Array.isArray(data.items) ? data.items : [] };
    // Keep the displayed edit's DOM; drafts carry their own base version, so
    // a remote change to the same item still conflicts when the draft is saved.
    if (editing()) { basket.deferred = value; basket.stale = true; }
    else apply(value);
    // Source pages mark what is already in the basket.
    window.dispatchEvent(new CustomEvent("workbench:basketitems"));
  }
  function has({ type, source_id, text }) { return (latest()?.items || []).some(item => item.type === type && item.source_id === source_id && (text === undefined || item.text === text)); }
  function count({ type, source_id }) { return (latest()?.items || []).filter(item => item.type === type && item.source_id === source_id).length; }
  function flush() {
    if (editing()) return;
    if (basket.deferred) { apply(basket.deferred); basket.stale = true; }
    if (basket.stale) render();
  }
  async function refresh() {
    const seq = ++basket.request;
    try {
      // Read all products so a shell switch never transfers another product's selection.
      const data = await api("/api/materials");
      if (seq !== basket.request) return latest();
      basket.error = "";
      accept(data);
      render();
      return latest();
    } catch (error) {
      if (seq !== basket.request) return latest();
      basket.error = `素材暂时无法读取：${error.message || "请稍后重试"}`;
      render();
      throw error;
    }
  }
  // Saves run one after another, so an autosave and a click never race on one item's version.
  let queue = Promise.resolve(), queued = 0;
  function enqueue(task) {
    queued += 1;
    basket.busy = true;
    const run = queue.then(task);
    const done = () => { queued -= 1; basket.busy = queued > 0; render(); };
    queue = run.then(done, done);
    return run;
  }
  // After a save the saved text is the new baseline; typing that followed stays a draft on top of it.
  function settle(id, note) {
    const saved = current(id), later = basket.drafts.get(id);
    if (saved) baselines.set(id, { note, revision: saved.item_revision });
    if (later) { if (later.note === note) basket.drafts.delete(id); else basket.drafts.set(id, { note: later.note, base: saved?.item_revision }); }
  }
  // text is the field at click time for an explicit save; autosave saves whatever the draft holds.
  function saveNote(id, { text, deliberate = false } = {}) {
    return enqueue(async () => {
      if (!basket.data) await refresh();
      collectVisibleDrafts();
      const item = current(id), draft = basket.drafts.get(id);
      if (!item) return "missing";
      const note = text ?? draft?.note;
      if (note === undefined || note === (item.note || "")) {
        if (draft && draft.note === (item.note || "")) basket.drafts.delete(id);
        refreshIndicators();
        return "saved";
      }
      // A conflict waits for an explicit save that began after the message was shown.
      if (draft?.conflictAt && !deliberate) return "conflict";
      const base = deliberate ? item.item_revision : draft?.base ?? baselines.get(id)?.revision ?? item.item_revision;
      try {
        const result = await post("/api/materials", { revision: latest().revision, request_id: requestID(), operation: "note", item: { id, type: item.type, source_id: item.source_id, note, item_revision: base } });
        collectVisibleDrafts();
        accept(result);
        settle(id, note);
        basket.error = "";
        emit();
        return "saved";
      } catch (error) {
        if (error.status !== 409) { basket.error = error.message || "保存未完成，请重试。"; throw error; }
        collectVisibleDrafts();
        const later = basket.drafts.get(id);
        basket.drafts.set(id, { note: later?.note ?? note, base: later?.base ?? base, conflictAt: performance.now() });
        const message = "素材已在其他页面更新。已读取最新内容，你的备注仍然保留；核对后再点一次「保存备注」即可覆盖。";
        await refresh().catch(() => {});
        basket.error = message;
        // An autosave started by closing the basket finds the conflict after the panel is gone.
        if (!dialog()?.open) warnConflicts();
        return "conflict";
      }
    });
  }
  async function add(item) {
    try {
      const result = await enqueue(async () => {
        if (!basket.data) await refresh();
        const value = await post("/api/materials", { revision: latest().revision, request_id: requestID(), operation: "add", item });
        accept(value);
        basket.error = "";
        emit();
        return value;
      });
      toast(result.existing ? "这条素材已在摘录篮中" : "已加入摘录篮");
      return true;
    } catch (error) {
      basket.error = error.message || "加入未完成，请重试。";
      toast(basket.error, true);
      return false;
    }
  }
  async function removeItems(items, clear = false) {
    if (!items.length) return;
    collectVisibleDrafts();
    const ids = items.map(item => item.id), quotes = new Set(items.filter(item => item.type === "review").map(item => item.source_id));
    // Unsaved notes go back with their items if the removal is undone.
    const drafts = new Map(ids.filter(id => basket.drafts.has(id)).map(id => [id, basket.drafts.get(id)]));
    const kept = (latest()?.theme_links || []).filter(link => quotes.has(link.review_id)).length;
    try {
      await enqueue(async () => {
        const result = await post("/api/materials/remove", { request_id: requestID(), ids, ...(scoped() ? { product: product() } : {}) });
        ids.forEach(id => basket.drafts.delete(id));
        accept(result);
        const removed = result.removed?.length || 0, request = requestID();
        const message = `${clear ? `已清空当前产品的 ${removed} 条素材` : `已移出 ${removed} 条素材`}${kept ? `；${kept} 条主题关联仍保留，可在用户声音的「关联主题」里删除` : ""}`;
        if (removed) offerUndo(message, () => restore(result.removal_id, request, drafts));
        else basket.undo = null;
        basket.error = "";
        emit();
      });
    } catch (error) { basket.error = error.message || "移出未完成，请重试。"; render(); }
  }
  function restore(removal, request, drafts) {
    return enqueue(async () => {
      const result = await post("/api/materials/restore", { request_id: request, removal_id: removal });
      accept(result);
      for (const [id, draft] of drafts) if (current(id)) basket.drafts.set(id, draft);
      basket.undo = null;
      basket.error = "";
      emit();
      toast(result.skipped ? `已恢复 ${result.restored.length} 条；${result.skipped} 条来源已删除，无法恢复` : `已恢复 ${result.restored.length} 条素材`, Boolean(result.skipped));
    });
  }
  function removeTheme(link) {
    return enqueue(async () => {
      accept(await post("/api/material-themes", { request_id: requestID(), operation: "remove", id: link.id }));
      if (dialog()?.open) offerUndo(`已删除主题关联「${themeLabel(link)}」`, () => restoreTheme(link));
      basket.error = "";
      emit();
    });
  }
  // Undo saves the same link again; it gets a new id but keeps its target and note.
  function restoreTheme(link) {
    return enqueue(async () => {
      const body = () => ({ revision: latest().revision, request_id: requestID(), review_id: link.review_id, product: link.product, role: link.role, target_id: link.target_id, note: link.note || "" });
      let result;
      try { result = await post("/api/material-themes", body()); }
      catch (error) { if (error.status !== 409) throw error; await refresh(); result = await post("/api/material-themes", body()); }
      accept(result);
      basket.undo = null;
      basket.error = "";
      emit();
      toast("已恢复主题关联");
    });
  }
  async function undo() {
    if (!basket.undo) return;
    try { await basket.undo.run(); } catch (error) { basket.error = error.message || "撤销未完成，请重试。"; render(); }
  }
  // All unsaved notes go in one request before writing; if any changed elsewhere, none is saved.
  async function saveDrafts() {
    collectVisibleDrafts();
    const items = visible().filter(item => basket.drafts.has(item.id));
    if (!items.length) return;
    if (items.some(item => basket.drafts.get(item.id).conflictAt)) {
      basket.error = "有备注与其他页面的修改冲突，请先核对，再点那一条的「保存备注」。";
      render();
      throw new Error(basket.error);
    }
    await enqueue(async () => {
      collectVisibleDrafts();
      const notes = items.filter(item => basket.drafts.has(item.id)).map(item => ({ id: item.id, note: basket.drafts.get(item.id).note, item_revision: basket.drafts.get(item.id).base ?? item.item_revision }));
      if (!notes.length) return;
      try {
        const result = await post("/api/materials/notes", { request_id: requestID(), notes });
        collectVisibleDrafts();
        accept(result);
        for (const { id, note } of notes) settle(id, note);
        basket.error = "";
        emit();
      } catch (error) {
        if (error.status !== 409) { basket.error = error.message || "备注保存未完成，请重试。"; throw error; }
        await refresh().catch(() => {});
        // Exactly the drafts whose item changed elsewhere wait for an explicit save.
        for (const { id } of notes) {
          const draft = basket.drafts.get(id), item = current(id);
          if (draft && item && item.item_revision !== draft.base) basket.drafts.set(id, { ...draft, conflictAt: performance.now() });
        }
        basket.error = error.message;
        throw error;
      }
    });
  }
  function close() { dialog()?.close(); }
  const hookDrafts = new Map();
  function openHook(video) {
    let modal = document.querySelector("#material-hook-dialog");
    if (!modal) { modal = document.createElement("dialog"); modal.id = "material-hook-dialog"; modal.setAttribute("aria-labelledby", "material-hook-title"); document.body.append(modal); }
    const saved = hookDrafts.get(video.video_id) || {text:"",note:""};
    modal.innerHTML = `<div class="dialog-heading"><h2 id="material-hook-title">摘录视频钩子</h2><button type="button" class="icon-button" data-hook-close aria-label="关闭钩子摘录">${icon("close")}</button></div><form class="material-theme-body" id="material-hook-form"><p>${esc(video.headline || video.title || video.video_id)}</p><p class="context-note">${esc(video.product || "")} · 视频 ${esc(video.video_id)}。观看原片后，填写你要参考的开头原句或画面。</p><label class="field"><span>原片开头或钩子</span><textarea name="text" required rows="5"${maxLength("hook_text")} placeholder="输入你在原片里实际看到或听到的钩子…">${esc(saved.text)}</textarea></label><label class="field"><span>备注与使用边界</span><textarea name="note" rows="2"${maxLength("note")}>${esc(saved.note)}</textarea></label><p class="data-warning" data-hook-error role="alert" hidden></p><div class="material-theme-actions"><button type="submit" class="primary" ${saved.text.trim() ? "" : "disabled"}>加入摘录篮</button></div></form>`;
    const form = modal.querySelector("form"), button = form.querySelector("[type=submit]");
    modal.querySelector("[data-hook-close]").onclick = () => modal.close();
    form.oninput = () => { hookDrafts.set(video.video_id, {text:form.elements.text.value,note:form.elements.note.value}); button.disabled = !form.elements.text.value.trim(); };
    form.onsubmit = async event => {
      event.preventDefault(); button.disabled = true;
      const success = await add({type:"hook",source_id:video.video_id,product:video.product,text:form.elements.text.value.trim(),note:form.elements.note.value,source_label:video.headline || video.title || video.video_id});
      if (success) { hookDrafts.delete(video.video_id); modal.close(); }
      else { const error = form.querySelector("[data-hook-error]"); error.hidden = false; error.textContent = basket.error; button.disabled = false; }
    };
    if (!modal.open) modal.showModal();
    form.elements.text.focus();
  }
  async function open() {
    initialize();
    const modal = dialog();
    if (!modal.open) modal.showModal();
    render();
    await refresh().catch(() => {});
  }
  async function write() {
    // Check before closing so a mixed-product selection stays visible for the user to fix.
    if (writeBlock()) return;
    try {
      await saveDrafts();
      const items = selection();
      close();
      if (window.CaseAI?.startWithMaterials) {
        // Submitting moves to the new task's page before the settings close; cancelling stays put and returns here.
        // The settings open before their options finish loading, so this listens from the start.
        const settings = document.querySelector("#ai-create-dialog"), hash = location.hash;
        const back = () => { if (location.hash === hash) void open(); };
        settings?.addEventListener("close", back, { once: true });
        if (!await window.CaseAI.startWithMaterials(items) && settings?.open === false) settings.removeEventListener("close", back);
      } else { window.dispatchEvent(new CustomEvent("workbench:write-materials", { detail: { items } })); location.hash = pageURL("#ai"); }
    } catch {}
  }
  // One set of listeners for the dialog's lifetime; rows come and go without rebinding.
  function onClick(event) {
    const modal = dialog(), hit = name => event.target.closest?.(`[${name}]`);
    let node;
    // A click on the backdrop reaches the dialog itself, outside its box.
    if (event.target === modal) { if (backdropDown && outside(event)) close(); backdropDown = false; return; }
    if (hit("data-material-close")) close();
    else if (hit("data-material-retry")) void refresh().catch(() => {});
    else if (hit("data-material-undo")) void undo();
    else if ((node = hit("data-material-filter"))) { basket.filter.type = node.dataset.materialFilter; applyFilter(modal); }
    else if ((node = hit("data-material-all"))) {
      const tick = node.dataset.materialAll === "1";
      for (const item of visible().filter(shown)) { if (tick) basket.excluded.delete(item.id); else basket.excluded.add(item.id); }
      modal.querySelectorAll("[data-material-select]").forEach(field => { field.checked = !basket.excluded.has(field.dataset.materialSelect); });
      saveExcluded();
      updateFooter(modal);
    } else if ((node = hit("data-material-save"))) {
      const id = node.dataset.materialSave, field = node.closest(".material-item")?.querySelector("[data-material-note]");
      if (field) keepDraft(field);
      // A conflict counts as reviewed only when it was on screen before this click began.
      const started = event.detail === 0 ? performance.now() : pointerAt, conflictAt = basket.drafts.get(id)?.conflictAt;
      saveNote(id, { text: field?.value, deliberate: Boolean(conflictAt && conflictAt < started) }).then(outcome => { if (outcome === "saved") toast("备注已保存"); }, () => {});
    } else if ((node = hit("data-material-remove"))) {
      const item = visible().find(entry => entry.id === node.dataset.materialRemove);
      if (item) void removeItems([item]);
    } else if ((node = hit("data-material-theme"))) {
      const item = visible().find(entry => entry.type === "review" && entry.source_id === node.dataset.materialTheme);
      if (item) void openTheme({ id: item.source_id, product: item.product, text: item.text });
    } else if ((node = hit("data-theme-remove"))) {
      const link = (latest()?.theme_links || []).find(entry => entry.id === node.dataset.themeRemove);
      if (link) void removeTheme(link).catch(error => { basket.error = error.message || "删除未完成，请重试。"; render(); });
    } else if (hit("data-material-copy")) copyText(markdown(), `已复制 ${selection().length} 条素材`);
    // One click clears; the undo bar restores everything exactly, so no second confirmation.
    else if (hit("data-material-clear")) { if (scoped()) void removeItems([...visible()], true); } else if (hit("data-material-write")) void write();
    // Leaving for another page closes the basket rather than leaving it open over that page.
    else if (event.target.closest?.('a[href^="#"]')) close();
    // A rerender during a click could replace the button under the pointer, so deferred data waits for it.
    setTimeout(flush);
  }
  function onChange(event) {
    const field = event.target.closest?.("[data-material-select]");
    if (!field) return;
    if (field.checked) basket.excluded.delete(field.dataset.materialSelect); else basket.excluded.add(field.dataset.materialSelect);
    saveExcluded();
    updateFooter(dialog());
  }
  function onInput(event) {
    const note = event.target.closest?.("[data-material-note]"), search = event.target.closest?.("[data-material-search]");
    if (note) { keepDraft(note); refreshIndicators(); }
    if (search) { basket.filter.query = search.value; applyFilter(dialog()); }
  }
  let themeSequence = 0;
  async function openTheme(review) {
    const seq = ++themeSequence;
    let modal = document.querySelector("#material-theme-dialog");
    if (!modal) { modal = document.createElement("dialog"); modal.id = "material-theme-dialog"; modal.setAttribute("aria-labelledby", "material-theme-title"); document.body.append(modal); }
    const requestedProduct = review.product || product();
    modal.innerHTML = `<div class="dialog-heading"><h2 id="material-theme-title">关联评论主题</h2><button type="button" class="icon-button" data-theme-close aria-label="关闭主题关联">${icon("close")}</button></div><div class="material-theme-body"><blockquote>${esc(review.text || "")}</blockquote><p role="status">正在读取卖点和方向…</p></div>`;
    modal.querySelector("[data-theme-close]").onclick = () => modal.close();
    if (!modal.open) modal.showModal();
    try {
      const [catalog] = await Promise.all([api(`/api/direction-catalog?product=${encodeURIComponent(requestedProduct)}`), refresh()]);
      if (!modal.open || seq !== themeSequence) return;
      const points = catalog.selling_points || [], directions = catalog.directions || [];
      modal.querySelector(".material-theme-body").innerHTML = `<blockquote>${esc(review.text || "")}</blockquote><p class="context-note">评论主题可以启发表达；这项关联不把评论升级为已核实产品事实。</p><div class="material-theme-existing" data-theme-existing hidden></div><form id="material-theme-form"><label class="field"><span>用途</span><select name="role"><option value="selling_point">产品卖点</option><option value="concern">方向顾虑</option></select></label><label class="field"><span>关联到</span><select name="target" required></select></label><label class="field"><span>备注</span><textarea name="note" rows="2"${maxLength("note")}></textarea></label><p class="data-warning" data-theme-error role="alert" hidden></p><div class="material-theme-actions"><button type="submit" class="primary">保存主题关联</button></div></form>`;
      const form = modal.querySelector("form"), role = form.elements.role, target = form.elements.target;
      const choices = () => {
        const rows = role.value === "concern" ? directions.map(item => [item.direction_id, `${item.direction_id} · ${item.concern || item.main_difference}`]) : points.map(item => [item.id, `${item.code} · ${item.title}`]);
        target.innerHTML = rows.length ? rows.map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join("") : '<option value="">当前产品暂无可关联条目</option>';
        form.querySelector("[type=submit]").disabled = !rows.length;
      };
      role.onchange = choices; choices();
      // Links outlive the quote's place in the basket, so they are listed and removed here, from any page.
      let removedLink = null;
      const paintLinks = () => {
        const host = modal.querySelector("[data-theme-existing]"), links = (latest()?.theme_links || []).filter(link => link.review_id === review.id);
        const failed = message => { const error = form.querySelector("[data-theme-error]"); error.hidden = false; error.textContent = message; };
        host.hidden = !links.length && !removedLink;
        host.innerHTML = `<h3>已有关联</h3><ul>${links.map(link => `<li><span>${esc(themeLabel(link))}${link.note ? ` · ${esc(link.note)}` : ""}</span><button type="button" class="subtle" data-theme-delete="${esc(link.id)}">删除</button></li>`).join("")}${removedLink ? `<li class="material-theme-removed"><span>已删除「${esc(themeLabel(removedLink))}」</span><button type="button" class="subtle" data-theme-undo>撤销</button></li>` : ""}</ul>`;
        host.querySelectorAll("[data-theme-delete]").forEach(button => button.onclick = async () => {
          const link = links.find(entry => entry.id === button.dataset.themeDelete);
          button.disabled = true;
          try { await removeTheme(link); removedLink = link; } catch (error) { failed(error.message || "删除未完成，请重试。"); }
          paintLinks();
        });
        const restoreButton = host.querySelector("[data-theme-undo]");
        if (restoreButton) restoreButton.onclick = async () => {
          restoreButton.disabled = true;
          try { await restoreTheme(removedLink); removedLink = null; } catch (error) { failed(error.message || "撤销未完成，请重试。"); }
          paintLinks();
        };
      };
      paintLinks();
      let pending = null, submitting = false;
      form.onsubmit = async event => {
        event.preventDefault(); if (submitting) return;
        const button = form.querySelector("[type=submit]"), error = form.querySelector("[data-theme-error]");
        if (!pending) pending = { revision: latest().revision, request_id: requestID(), review_id: review.id, product: requestedProduct, role: role.value, target_id: target.value, note: form.elements.note.value };
        submitting = true; button.disabled = true;
        [role,target,form.elements.note].forEach(field => field.disabled = true);
        try {
          await api("/api/material-themes", { method: "POST", body: pending });
          pending = null;
          await refresh().catch(() => {}); emit(); modal.close(); toast("已关联评论主题");
        } catch (failure) {
          const definitive = Number.isInteger(failure.status) && failure.status >= 400 && failure.status < 500;
          if (definitive) pending = null;
          error.hidden = false;
          error.textContent = failure.status === 409 ? "关联资料已更新，请重新检查后再保存。" : definitive ? failure.message : `保存结果尚未确认：${failure.message || "连接中断"}。请重试同一请求，避免重复关联。`;
          if (failure.status === 409) await refresh().catch(() => {});
        } finally {
          submitting = false; button.disabled = false;
          button.textContent = pending ? "重试同一请求" : "保存主题关联";
          [role,target,form.elements.note].forEach(field => field.disabled = !!pending);
        }
      };
    } catch (error) { if (seq === themeSequence && modal.open) modal.querySelector(".material-theme-body").innerHTML = `<p class="data-warning">${esc(error.message || "暂时无法读取关联资料。请关闭后重试。")}</p>`; }
  }
  let initialized = false;
  function initialize() {
    if (initialized) return;
    initialized = true;
    const modal = document.createElement("dialog"); modal.id = "material-basket-dialog"; modal.setAttribute("aria-labelledby", "material-basket-title"); document.body.append(modal);
    // Esc, the backdrop and close() all end here; the next opening builds a fresh frame. Undo is kept (see offerUndo).
    modal.addEventListener("close", () => { basket.filter = { type: "", query: "" }; modal.innerHTML = ""; warnConflicts(); });
    modal.addEventListener("pointerdown", event => { pointerAt = event.timeStamp; backdropDown = event.target === modal && outside(event); }, true);
    modal.addEventListener("click", onClick);
    modal.addEventListener("change", onChange);
    modal.addEventListener("input", onInput);
    modal.addEventListener("focusout", event => {
      const field = event.target.closest?.("[data-material-note]");
      if (field) {
        keepDraft(field);
        if (basket.drafts.has(field.dataset.materialNote)) void saveNote(field.dataset.materialNote).catch(() => {});
      }
      // A rerender now could replace a button between its mousedown and click; those wait for the click handler.
      if (!event.relatedTarget || !modal.contains(event.relatedTarget)) setTimeout(flush);
    });
    document.addEventListener("click", event => { if (event.target.closest?.("[data-material-open]")) void open(); });
    window.addEventListener("workbench:basketchange", event => { if (event.detail?.source !== "global") void refresh().catch(() => {}); });
    // Undo messages name "the current product", so they do not carry over to another one.
    window.addEventListener("workbench:productchange", () => { clearTimeout(undoTimer); basket.undo = null; render(); });
    window.addEventListener("beforeunload", event => { if (basket.drafts.size) { event.preventDefault(); event.returnValue = ""; } });
    void refresh().catch(() => {});
  }
  return { initialize, open, close, add, has, count, refresh, selection, markdown, openTheme, openHook, saveDrafts, modalOpen: () => Boolean(dialog()?.open || document.querySelector("#material-theme-dialog")?.open || document.querySelector("#material-hook-dialog")?.open), hasUnsavedInput: () => basket.drafts.size > 0 || [...hookDrafts.values()].some(item => item.text.trim() || item.note.trim()) };
})();
