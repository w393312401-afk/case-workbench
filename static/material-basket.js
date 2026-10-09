"use strict";

// One persisted basket shared by the review, expression and video pages.
window.MaterialBasket = (() => {
  const kinds = { review: "用户评论", expression: "案例表达", hook: "视频钩子" };
  const sourceLabel = item => ({tiktok_video:"TikTok 视频评论",tiktok_shop:"TikTok Shop 评价",amazon:"亚马逊评价",reddit:"Reddit 讨论",facebook:"Facebook 评论"}[item.source_label] || item.source_label || item.source_id);
  const basket = { data: null, request: 0, busy: false, error: "", drafts: new Map(), excluded: new Set(), clearArmed: false };
  const product = () => window.WorkbenchShell?.product() || "";
  const pageURL = (hash, selected) => window.WorkbenchShell?.url(hash, selected) || hash;
  const matches = item => !window.WorkbenchShell || window.WorkbenchShell.matchesProduct(item.product);
  const visible = () => (basket.data?.items || []).filter(matches);
  const safeURL = value => /^(https?:\/\/|#)/i.test(String(value || "")) ? value : "";
  const emit = () => window.dispatchEvent(new CustomEvent("workbench:basketchange", { detail: { source: "global" } }));
  const requestID = () => crypto.randomUUID();
  function dialog() { return document.querySelector("#material-basket-dialog"); }
  function editingNote() { return Boolean(document.activeElement?.matches?.("[data-material-note]") && dialog()?.contains(document.activeElement)); }
  function collectVisibleDrafts() {
    dialog()?.querySelectorAll("[data-material-note]").forEach(field => {
      const item = visible().find(item => item.id === field.dataset.materialNote);
      if (item && (basket.drafts.has(item.id) || field.value !== (item.note || ""))) basket.drafts.set(item.id, field.value);
    });
  }
  function selection() { return visible().filter(item => !basket.excluded.has(item.id)).map(item => ({ ...item, note: basket.drafts.has(item.id) ? basket.drafts.get(item.id) : item.note || "" })); }
  function updateCount() {
    const count = visible().length;
    document.querySelectorAll("[data-material-count]").forEach(node => { node.textContent = basket.data ? String(count) : "—"; });
    document.querySelectorAll("[data-material-open]").forEach(node => node.setAttribute("aria-label", `素材篮${basket.data ? `（${count}）` : ""}`));
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
  function render() {
    updateCount();
    const modal = dialog();
    if (!modal?.open) return;
    // Focus can precede the browser's input event. Replacing this node in that
    // interval loses keystrokes before the draft cache has seen them.
    if (editingNote()) return;
    const rows = visible();
    const selected = selection().length;
    modal.innerHTML = `<div class="dialog-heading"><div><span class="eyebrow">WRITING MATERIALS</span><h2 id="material-basket-title">素材篮 <small>${rows.length}</small></h2><p class="context-note">${esc(product() || "全部产品")} · 页面切换后继续保留</p></div><button type="button" class="icon-button" data-material-close aria-label="关闭素材篮">${icon("close")}</button></div><div class="material-basket-body"><p class="material-note">勾选这次要带入写稿的素材。评论、参考表达与钩子会保留来源。</p><div id="material-basket-error" class="data-warning" role="alert" ${basket.error ? "" : "hidden"}>${esc(basket.error)} <button type="button" class="subtle" data-material-retry>重新读取</button></div><div class="material-list">${!basket.data ? '<p role="status">正在读取素材…</p>' : !rows.length ? '<div class="empty"><h3>还没有素材</h3><p>在用户声音摘录评论、案例表达加入原句，或在视频数据摘记钩子。</p></div>' : rows.map(item => {
      const links = (basket.data.theme_links || []).filter(link => link.review_id === item.source_id);
      return `<article class="material-item" data-material-id="${esc(item.id)}"><div class="material-item-heading"><label><input type="checkbox" data-material-select="${esc(item.id)}" ${basket.excluded.has(item.id) ? "" : "checked"}> <span>${esc(kinds[item.type] || item.type)}</span></label><button type="button" class="subtle" data-material-remove="${esc(item.id)}" ${basket.busy ? "disabled" : ""}>移出素材篮</button></div><blockquote>${esc(item.text || "原内容暂不可用，请检查来源。")}</blockquote><div class="material-source"><a href="${esc(sourceURL(item))}" ${/^https?:/i.test(sourceURL(item)) ? 'target="_blank" rel="noopener noreferrer"' : ""}>${esc(sourceLabel(item))}</a>${item.unverified ? '<span>未经核实</span>' : ""}</div><label class="material-note-field">写稿备注<textarea data-material-note="${esc(item.id)}" rows="2" maxlength="2000" placeholder="记下用途、画面或使用边界">${esc(basket.drafts.has(item.id) ? basket.drafts.get(item.id) : item.note || "")}</textarea></label><div class="material-item-actions"><button type="button" class="subtle" data-material-save="${esc(item.id)}" ${basket.busy ? "disabled" : ""}>保存备注</button>${item.type === "review" ? `<button type="button" class="subtle" data-material-theme="${esc(item.source_id)}">关联卖点或顾虑</button>` : ""}</div>${links.length ? `<div class="material-themes">${links.map(link => `<span>${link.role === "concern" ? "方向顾虑" : "产品卖点"} · ${esc(link.target_label || link.target_id)}</span>`).join("")}</div>` : ""}</article>`;
    }).join("")}</div></div><div class="dialog-footer material-basket-footer"><span id="material-selection-count">已选 ${selected} 条</span><div><button type="button" class="subtle" data-material-clear ${!rows.length || basket.busy ? "disabled" : ""}>${basket.clearArmed ? "再点一次，清空当前产品" : "清空当前产品"}</button><button type="button" class="secondary" data-material-copy ${!selected ? "disabled" : ""}>${icon("copy")}复制所选</button><button type="button" class="primary" data-material-write ${!selected || basket.busy ? "disabled" : ""}>带入写稿</button></div></div>`;
    bind(modal);
  }
  async function refresh() {
    const seq = ++basket.request;
    try {
      // Read all products so a shell switch never transfers another product's selection.
      const data = await api("/api/materials");
      if (seq !== basket.request) return basket.data;
      // Keep the revision of the displayed edit as well as its DOM. A concurrent
      // remote change must still conflict when this draft is explicitly saved.
      if (editingNote()) return basket.data;
      basket.data = { ...data, items: Array.isArray(data.items) ? data.items : [] };
      basket.error = "";
      const existing = new Set(basket.data.items.map(item => item.id));
      for (const id of basket.excluded) if (!existing.has(id)) basket.excluded.delete(id);
      render();
      return basket.data;
    } catch (error) {
      if (seq !== basket.request) return basket.data;
      basket.error = `素材暂时无法读取：${error.message || "请稍后重试"}`;
      render();
      throw error;
    }
  }
  async function mutate(operation, item) {
    if (basket.busy) throw new Error("上一项素材正在保存，请稍后再试。");
    basket.busy = true;
    try {
      if (!basket.data) await refresh();
      const result = await api("/api/materials", { method: "POST", body: { revision: basket.data.revision, request_id: requestID(), operation, item } });
      if (operation === "remove") basket.drafts.delete(item.id);
      if (operation === "note") {
        collectVisibleDrafts();
        // Only the submitted text is saved; later typing remains a local draft.
        if (basket.drafts.get(item.id) === item.note) basket.drafts.delete(item.id);
      }
      if (result.items) basket.data = result;
      else if (result.materials?.items) basket.data = result.materials;
      else await refresh();
      basket.error = "";
      emit();
      return result;
    } catch (error) {
      basket.error = error.status === 409 ? "素材已在其他页面更新。已读取最新内容，请再保存一次；你的备注仍然保留。" : error.message || "保存未完成，请重试。";
      if (error.status === 409) { const message = basket.error; await refresh().catch(() => {}); basket.error = message; }
      throw error;
    } finally { basket.busy = false; render(); }
  }
  async function add(item) {
    try { await mutate("add", item); toast("已加入素材篮"); return true; }
    catch (error) { toast(basket.error || error.message, true); return false; }
  }
  async function saveDrafts() {
    collectVisibleDrafts();
    for (const item of visible()) if (basket.drafts.has(item.id)) await mutate("note", { id: item.id, type: item.type, source_id: item.source_id, note: basket.drafts.get(item.id) });
  }
  function close() { dialog()?.close(); basket.clearArmed = false; }
  const hookDrafts = new Map();
  function openHook(video) {
    let modal = document.querySelector("#material-hook-dialog");
    if (!modal) { modal = document.createElement("dialog"); modal.id = "material-hook-dialog"; modal.setAttribute("aria-labelledby", "material-hook-title"); document.body.append(modal); }
    const saved = hookDrafts.get(video.video_id) || {text:"",note:""};
    modal.innerHTML = `<div class="dialog-heading"><h2 id="material-hook-title">摘录视频钩子</h2><button type="button" class="icon-button" data-hook-close aria-label="关闭钩子摘录">${icon("close")}</button></div><form class="material-theme-body" id="material-hook-form"><p>${esc(video.headline || video.title || video.video_id)}</p><p class="context-note">${esc(video.product || "")} · 视频 ${esc(video.video_id)}。观看原片后，填写你要参考的开头原句或画面。</p><label class="field"><span>原片开头或钩子</span><textarea name="text" required rows="5" maxlength="6000" placeholder="输入你在原片里实际看到或听到的钩子…">${esc(saved.text)}</textarea></label><label class="field"><span>备注与使用边界</span><textarea name="note" rows="2" maxlength="2000">${esc(saved.note)}</textarea></label><p class="data-warning" data-hook-error role="alert" hidden></p><div class="material-theme-actions"><button type="submit" class="primary" ${saved.text.trim() ? "" : "disabled"}>加入素材篮</button></div></form>`;
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
  function bind(modal) {
    modal.querySelector("[data-material-close]").onclick = close;
    modal.querySelector("[data-material-retry]").onclick = () => void refresh().catch(() => {});
    modal.querySelectorAll("[data-material-select]").forEach(field => field.onchange = () => {
      if (field.checked) basket.excluded.delete(field.dataset.materialSelect); else basket.excluded.add(field.dataset.materialSelect);
      const count = selection().length;
      modal.querySelector("#material-selection-count").textContent = `已选 ${count} 条`;
      modal.querySelector("[data-material-copy]").disabled = !count;
      modal.querySelector("[data-material-write]").disabled = !count;
    });
    modal.querySelectorAll("[data-material-note]").forEach(field => field.oninput = () => basket.drafts.set(field.dataset.materialNote, field.value));
    modal.querySelectorAll("[data-material-save]").forEach(button => button.onclick = async () => {
      const item = visible().find(item => item.id === button.dataset.materialSave);
      const field = button.closest(".material-item")?.querySelector("[data-material-note]");
      if (field) basket.drafts.set(item.id, field.value);
      try { await mutate("note", { id: item.id, type: item.type, source_id: item.source_id, note: basket.drafts.get(item.id) ?? item.note ?? "" }); toast("备注已保存"); } catch {}
    });
    modal.querySelectorAll("[data-material-remove]").forEach(button => button.onclick = async () => {
      const item = visible().find(item => item.id === button.dataset.materialRemove);
      try { await mutate("remove", { id: item.id, type: item.type, source_id: item.source_id }); } catch {}
    });
    modal.querySelectorAll("[data-material-theme]").forEach(button => button.onclick = () => {
      const item = visible().find(item => item.type === "review" && item.source_id === button.dataset.materialTheme);
      void openTheme({ id: item.source_id, product: item.product, text: item.text });
    });
    modal.querySelector("[data-material-copy]").onclick = () => copyText(markdown(), `已复制 ${selection().length} 条素材`);
    modal.querySelector("[data-material-clear]").onclick = async () => {
      if (!basket.clearArmed) { basket.clearArmed = true; render(); return; }
      basket.clearArmed = false;
      try { for (const item of [...visible()]) await mutate("remove", { id: item.id, type: item.type, source_id: item.source_id }); } catch {}
    };
    modal.querySelector("[data-material-write]").onclick = async () => {
      try {
        await saveDrafts();
        const items = selection();
        close();
        if (window.CaseAI?.startWithMaterials) await window.CaseAI.startWithMaterials(items);
        else { window.dispatchEvent(new CustomEvent("workbench:write-materials", { detail: { items } })); location.hash = pageURL("#ai"); }
      } catch {}
    };
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
      modal.querySelector(".material-theme-body").innerHTML = `<blockquote>${esc(review.text || "")}</blockquote><p class="context-note">评论主题可以启发表达；这项关联不把评论升级为已核实产品事实。</p><form id="material-theme-form"><label class="field"><span>用途</span><select name="role"><option value="selling_point">产品卖点</option><option value="concern">方向顾虑</option></select></label><label class="field"><span>关联到</span><select name="target" required></select></label><label class="field"><span>备注</span><textarea name="note" rows="2" maxlength="2000"></textarea></label><p class="data-warning" data-theme-error role="alert" hidden></p><div class="material-theme-actions"><button type="submit" class="primary">保存主题关联</button></div></form>`;
      const form = modal.querySelector("form"), role = form.elements.role, target = form.elements.target;
      const choices = () => {
        const rows = role.value === "concern" ? directions.map(item => [item.direction_id, `${item.direction_id} · ${item.concern || item.main_difference}`]) : points.map(item => [item.id, `${item.code} · ${item.title}`]);
        target.innerHTML = rows.length ? rows.map(([id, label]) => `<option value="${esc(id)}">${esc(label)}</option>`).join("") : '<option value="">当前产品暂无可关联条目</option>';
        form.querySelector("[type=submit]").disabled = !rows.length;
      };
      role.onchange = choices; choices();
      let pending = null, submitting = false;
      form.onsubmit = async event => {
        event.preventDefault(); if (submitting) return;
        const button = form.querySelector("[type=submit]"), error = form.querySelector("[data-theme-error]");
        if (!pending) pending = { revision: basket.data.revision, request_id: requestID(), review_id: review.id, product: requestedProduct, role: role.value, target_id: target.value, note: form.elements.note.value };
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
    document.addEventListener("click", event => { if (event.target.closest?.("[data-material-open]")) void open(); });
    window.addEventListener("workbench:basketchange", event => { if (event.detail?.source !== "global") void refresh().catch(() => {}); });
    window.addEventListener("workbench:productchange", () => { basket.clearArmed = false; render(); });
    window.addEventListener("beforeunload", event => { if (basket.drafts.size) { event.preventDefault(); event.returnValue = ""; } });
    void refresh().catch(() => {});
  }
  return { initialize, open, close, add, refresh, selection, markdown, openTheme, openHook, saveDrafts, modalOpen: () => Boolean(dialog()?.open || document.querySelector("#material-theme-dialog")?.open || document.querySelector("#material-hook-dialog")?.open), hasUnsavedInput: () => basket.drafts.size > 0 || [...hookDrafts.values()].some(item => item.text.trim() || item.note.trim()) };
})();
