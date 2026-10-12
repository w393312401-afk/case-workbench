"use strict";

// Activity has its own read model. It never modifies a task's business version.
window.TaskActivity = (() => {
  const keys = { browser: "workbench-activity-browser-v1", cursor: "workbench-activity-cursor-v1", desktop: "workbench-activity-desktop-v1", focus: "workbench-activity-focus-v1" };
  const activeStates = new Set(["queued", "running", "stopping", "cancelling", "cancel_requested", "saving", "recovering"]);
  const labels = { queued: "排队中", running: "进行中", stopping: "正在停止", cancelling: "取消中", cancel_requested: "取消中", saving: "保存中", recovering: "恢复中", completed: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消", needs_attention: "待处理", awaiting_confirmation: "待确认", pending_review: "待人工审查", awaiting_review: "待人工审查", recovery_pending: "待恢复", result_unpersisted: "结果待保存" };
  const phases = { queued: "等待执行", running: "正在执行", draft: "生成初稿", review: "复核", format: "整理交付格式", finished: "执行结束", save: "保存结果", reconciled: "核对恢复状态", stopping: "正在停止", voice: "生成配音", timeline: "建立时间线", edit: "AI 剪辑", validation: "核验工程", stills: "寻找底图", images: "生成图片", image_review: "核对图片", package: "打包成品" };
  let initialized = false, timer, polling = false, refreshPending = false, failures = 0;
  let cursor = null, epoch = null, browserId = "", counts = { active: 0, unread: 0 }, available = true;
  let dialog, logDialog, notices, previousFocus, logPreviousFocus, panelSequence = 0;
  let panel = { items: [], product: "", filter: "all", before: null, hasMore: false, loading: false, error: "" };
  let requestTail = Promise.resolve(), pendingRead = new Set(), pendingVisit = null;
  const tabId = id(), known = new Map(), logMounts = new Set();
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const enabled = () => !window.PublicTextMode && !document.documentElement.classList.contains("public-text-mode");
  const product = () => window.WorkbenchShell?.product?.() || "";
  const foreground = () => document.visibilityState !== "hidden" && document.hasFocus();
  const safeUrl = value => typeof value === "string" && value.startsWith("#") ? value : "";
  const storage = { get(key) { try { return localStorage.getItem(key); } catch { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch { /* In-memory operation remains available. */ } }, remove(key) { try { localStorage.removeItem(key); } catch { /* Optional browser coordination. */ } } };

  function id() { return globalThis.crypto?.randomUUID?.() || `activity-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  function request(path, options) {
    const result = requestTail.catch(() => {}).then(() => api(path, options));
    requestTail = result.catch(() => {});
    return result;
  }
  function date(value) {
    if (!value) return "";
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? "" : parsed.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
  }
  function duration(run, end = run?.finished_at || (activeStates.has(run?.status) ? new Date().toISOString() : run?.updated_at)) {
    const elapsed = Math.max(0, new Date(end).getTime() - new Date(run?.started_at).getTime());
    if (!Number.isFinite(elapsed)) return "";
    const seconds = Math.floor(elapsed / 1000);
    return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds / 60) % 60} 分`;
  }
  function status(run) { return labels[run.status] || (run.finished_at ? "已结束" : "处理中"); }
  function remember(items = []) { for (const item of items) if (item?.run_id) known.set(item.run_id, item); }
  function focusPresence() {
    if (foreground()) storage.set(keys.focus, JSON.stringify({ tab: tabId, at: Date.now() }));
    else { try { if (JSON.parse(storage.get(keys.focus) || "null")?.tab === tabId) storage.remove(keys.focus); } catch { /* Stale presence can expire. */ } }
  }
  function anotherForegroundTab() {
    try { const owner = JSON.parse(storage.get(keys.focus) || "null"); return !foreground() && owner?.tab !== tabId && Date.now() - owner?.at < 35000; } catch { return false; }
  }
  function paintBadge() {
    const button = document.querySelector("#task-activity-open");
    if (!button) return;
    button.hidden = !enabled();
    button.setAttribute("aria-label", available ? `任务，${counts.active} 项进行中，${counts.unread} 条未读` : "任务，运行日志暂时不可用");
    button.title = available ? "任务提醒与运行日志" : "运行日志暂时不可用，任务可继续执行";
    for (const key of ["active", "unread"]) {
      const badge = button.querySelector(`[data-activity-${key}]`);
      if (badge) { badge.textContent = key === "active" ? `${counts[key]} 进行中` : `${counts[key]} 未读`; badge.hidden = !counts[key]; }
    }
  }
  function schedule(delay) { clearTimeout(timer); if (initialized && enabled()) timer = setTimeout(() => void poll(), delay); }
  function refresh() { if (!initialized || !enabled()) return; if (polling) refreshPending = true; else schedule(0); }

  async function poll() {
    if (polling || !enabled()) return;
    polling = true; clearTimeout(timer); focusPresence();
    let more = false;
    try {
      const baseline = cursor === null;
      const data = await request(`/api/activity?limit=50${baseline ? "" : `&after=${cursor}`}`);
      if (data?.available === false) { available = false; throw new Error("运行日志暂时不可用"); }
      if (!data || !Number.isSafeInteger(data.cursor)) throw new Error("运行日志暂时无法读取");
      available = true;
      counts = { active: Number(data.counts?.active) || 0, unread: Number(data.counts?.unread) || 0 };
      if (!baseline && epoch !== data.epoch) {
        cursor = null; epoch = null; known.clear(); notices?.replaceChildren();
        more = true;
      } else {
        remember(data.items);
        for (const removed of data.removed || []) known.delete(removed);
        // A focused tab receives the alert first. Deferred tabs retain their cursor,
        // and later pass these same events through the server's atomic claim.
        const events = baseline ? [] : data.events || [];
        if (events.length && anotherForegroundTab()) more = false;
        else {
          if (events.length) await notify(events);
          cursor = data.cursor; epoch = data.epoch;
          storage.set(keys.cursor, JSON.stringify({ cursor, epoch }));
          more = !baseline && !!data.has_more;
        }
      }
      failures = 0;
      if (dialog?.open && !panel.loading) await loadPanel(false, true);
      for (const mount of [...logMounts]) {
        if (!mount.details.isConnected) logMounts.delete(mount);
        else if (mount.details.open && mount.loaded && !mount.loading) void mount.refresh();
      }
    } catch { failures += 1; /* A transient failure never creates another alert. */ }
    finally {
      polling = false; paintBadge();
      if (dialog?.open) paintConnection();
      const immediate = refreshPending; refreshPending = false;
      schedule(immediate || more ? 0 : failures ? Math.min(60000, 3000 * 2 ** Math.min(failures - 1, 5)) : counts.active ? 3000 : 15000);
    }
  }

  async function notify(events) {
    const unique = [...new Map(events.filter(item => Number.isSafeInteger(item.event_id)).map(item => [item.event_id, item])).values()];
    if (!unique.length) return;
    // Re-read immediately before claiming: another tab may have established the
    // shared browser identity during simultaneous first-page initialization.
    browserId = storage.get(keys.browser) || browserId;
    const response = await request("/api/activity/notify-claim", { method: "POST", body: { browser_id: browserId, event_ids: unique.map(item => item.event_id) } });
    const claimed = new Set(response?.claimed || []);
    for (const event of unique.filter(item => claimed.has(item.event_id))) {
      addNotice(event);
      if (!foreground() && event.status !== "cancelled" && storage.get(keys.desktop) === "enabled" && window.Notification?.permission === "granted") {
        try {
          const notification = new Notification(event.title || "工作台任务", { body: event.message || status(event), silent: true, tag: `workbench-${epoch || ""}-${event.event_id}` });
          notification.onclick = () => { window.focus(); notification.close(); visit(event); };
          setTimeout(() => notification.close(), 20000);
        } catch { /* Browser restrictions never prevent the in-app alert. */ }
      }
    }
  }
  function addNotice(event) {
    if (!notices || notices.querySelector(`[data-event-id="${event.event_id}"]`)) return;
    const node = document.createElement("article");
    node.className = "activity-notice"; node.dataset.eventId = event.event_id;
    node.innerHTML = `<button type="button" class="activity-notice-view"><strong>${escape(event.title || "工作台任务")}</strong><span>${escape(event.message || status(event))}</span><small>${escape(event.product || "")} · 查看结果</small></button><button type="button" class="subtle activity-notice-close" aria-label="收起这条提醒">×</button>`;
    node.querySelector(".activity-notice-view").onclick = () => { node.remove(); visit(event); };
    node.querySelector(".activity-notice-close").onclick = () => node.remove();
    notices.append(node);
    while (notices.children.length > 3) notices.firstElementChild.remove();
    let timeout;
    const expire = () => { clearTimeout(timeout); timeout = setTimeout(() => { if (!node.contains(document.activeElement)) node.remove(); }, 15000); };
    node.addEventListener("mouseenter", () => clearTimeout(timeout)); node.addEventListener("mouseleave", expire);
    node.addEventListener("focusin", () => clearTimeout(timeout)); node.addEventListener("focusout", expire); expire();
  }
  function visit(item) {
    const destination = safeUrl(item.url);
    if (dialog?.open) dialog.close();
    if (logDialog?.open) logDialog.close();
    if (destination) {
      pendingVisit = { item, hash: window.WorkbenchShell?.url?.(destination) || destination };
      if (location.hash === pendingVisit.hash) didNavigate(location.hash);
      else location.hash = destination;
    }
    else if (item.run_id) showLogs(item.run_id);
    else open();
  }
  function didNavigate(hash = location.hash) {
    if (!pendingVisit) return;
    if (new URL(pendingVisit.hash, location.href).hash !== new URL(hash, location.href).hash) return;
    const eventId = pendingVisit.item.event_id; pendingVisit = null;
    if (eventId) void markRead([eventId]);
  }
  async function markRead(eventIds, all = false) {
    const ids = eventIds.filter(value => Number.isSafeInteger(value) && !pendingRead.has(value));
    if (!all && !ids.length) return;
    ids.forEach(value => pendingRead.add(value));
    try {
      await request("/api/activity/read", { method: "POST", body: all ? { all: true, before_event_id: cursor || 0 } : { event_ids: ids } });
      for (const run of known.values()) if (all || ids.includes(run.event_id)) run.read = true;
      for (const row of panel.items) if (all || ids.includes(row.event_id)) row.read = true;
      for (const node of notices?.children || []) if (all || ids.includes(Number(node.dataset.eventId))) node.remove();
      if (dialog?.open) await loadPanel(false, true);
      refresh();
    } catch { if (dialog?.open) { panel.error = "已读状态暂未保存，请重试。"; paintConnection(); } }
    finally { ids.forEach(value => pendingRead.delete(value)); }
  }

  function productOptions() {
    const values = new Map([["", "全部产品"]]);
    for (const option of document.querySelectorAll("#global-product option")) if (option.value) values.set(option.value, option.textContent);
    for (const run of known.values()) if (run.product) values.set(run.product, run.product);
    if (panel.product && !values.has(panel.product)) values.set(panel.product, panel.product);
    return [...values].map(([value, title]) => `<option value="${escape(value)}" ${panel.product === value ? "selected" : ""}>${escape(title)}</option>`).join("");
  }
  function paintConnection() {
    const target = dialog?.querySelector("#activity-connection");
    if (!target) return;
    target.textContent = panel.error || (!available ? "运行日志暂时不可用；已有任务仍可继续执行。" : failures ? "连接暂时中断，正在自动重试…" : "提醒覆盖全部产品；这里只筛选记录，不会清空未读。");
  }
  function rowMarkup(run) {
    const unread = run.event_id && !run.read, elapsed = duration(run);
    return `<article class="activity-row${unread ? " is-unread" : ""}" data-run-id="${escape(run.run_id)}"><div class="activity-row-heading"><h3>${escape(run.title || "工作台任务")}</h3><span class="activity-status ${activeStates.has(run.status) ? "is-active" : ["failed", "interrupted"].includes(run.status) ? "is-warning" : ""}">${escape(status(run))}</span></div><p>${escape(run.message || run.phase || "")}</p><small>${escape([run.product || "未指定产品", date(run.updated_at), elapsed ? `耗时 ${elapsed}` : "", Number.isFinite(run.result_count) ? `${run.result_count} 项结果` : ""].filter(Boolean).join(" · "))}</small><div class="activity-row-actions"><button type="button" class="subtle" data-activity-log="${escape(run.run_id)}">运行日志</button><button type="button" class="secondary compact" data-activity-result="${escape(run.run_id)}">查看结果</button>${unread ? `<span class="activity-unread-dot" aria-label="未读"></span>` : ""}</div></article>`;
  }
  function paintPanel() {
    const list = dialog.querySelector("#activity-list");
    const focused = document.activeElement?.closest?.("[data-activity-log],[data-activity-result]");
    const focusId = focused?.dataset.activityLog || focused?.dataset.activityResult, focusType = focused?.hasAttribute("data-activity-log") ? "log" : "result";
    list.innerHTML = panel.items.length ? panel.items.map(rowMarkup).join("") : `<p class="activity-empty">${panel.loading ? "正在读取任务…" : panel.filter === "unread" ? "没有未读提醒" : panel.filter === "active" ? "当前没有进行中的任务" : "暂无运行记录"}</p>`;
    const more = dialog.querySelector("#activity-more"); more.hidden = !panel.hasMore; more.disabled = panel.loading; more.textContent = panel.loading ? "正在读取…" : "加载更多";
    dialog.querySelector("#activity-read-all").disabled = !counts.unread;
    if (focusId) [...list.querySelectorAll(`[data-activity-${focusType}]`)].find(node => node.dataset[focusType === "log" ? "activityLog" : "activityResult"] === focusId)?.focus({ preventScroll: true });
    paintConnection();
  }
  async function loadPanel(append = false, quiet = false) {
    const sequence = ++panelSequence;
    panel.loading = true; panel.error = "";
    if (!quiet) paintPanel();
    const params = new URLSearchParams({ limit: "50", filter: panel.filter });
    if (panel.product) params.set("product", panel.product);
    if (append && panel.before !== null) params.set("before", String(panel.before));
    try {
      const data = await request(`/api/activity?${params}`);
      if (sequence !== panelSequence || !dialog.open) return;
      if (data?.available === false) throw new Error("运行日志暂时不可用；已有任务仍可继续执行。");
      const items = data.items || [];
      remember(items);
      // A background refresh keeps older pages already loaded by the reader.
      const old = append ? panel.items : quiet ? panel.items.filter(row => !items.some(next => next.run_id === row.run_id) && known.has(row.run_id) && (panel.filter !== "unread" || !known.get(row.run_id)?.read) && (panel.filter !== "active" || activeStates.has(known.get(row.run_id)?.status))) : [];
      panel.items = append ? [...old, ...items.filter(item => !old.some(row => row.run_id === item.run_id))] : [...items, ...old].map(item => known.get(item.run_id) || item);
      if (!quiet || !old.length) { panel.hasMore = !!data.has_more; panel.before = data.next_before ?? null; }
      counts = data.counts || counts; paintBadge();
    } catch (error) { if (sequence === panelSequence) panel.error = error.message || "任务记录暂时无法读取。"; }
    finally { if (sequence === panelSequence) { panel.loading = false; if (dialog.open) paintPanel(); } }
  }
  function open() {
    if (!enabled()) return;
    initialize();
    if (!dialog.open) {
      previousFocus = document.activeElement;
      panel = { items: [], product: product(), filter: "all", before: null, hasMore: false, loading: false, error: "" };
      dialog.querySelector("#activity-product").innerHTML = productOptions();
      dialog.querySelectorAll("[data-activity-filter]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.activityFilter === panel.filter)));
      dialog.showModal(); void loadPanel();
    }
  }

  function logMarkup(run, rows) {
    return `<p class="activity-log-summary">${escape([status(run), duration(run) ? `耗时 ${duration(run)}` : "", Number.isFinite(run.result_count) ? `${run.result_count} 项结果` : ""].filter(Boolean).join(" · "))}</p><ol class="activity-log-lines">${rows.map(row => `<li><time>${escape(date(row.at))}</time><div><strong>${escape(phases[row.phase] || row.phase || labels[row.status] || "步骤")}</strong><p>${escape(row.message || "")}</p><small>${escape([duration(run, row.at) ? `累计 ${duration(run, row.at)}` : "", Number.isFinite(row.result_count) ? `${row.result_count} 项结果` : ""].filter(Boolean).join(" · "))}</small></div></li>`).join("")}</ol>`;
  }
  function mountRunLog(host, runId) {
    const state = { run: known.get(runId), rows: [], seq: 0, more: false, loading: false, loaded: false };
    const draw = error => {
      if (!host.isConnected) return;
      host.innerHTML = `${state.run ? logMarkup(state.run, state.rows) : ""}${!state.rows.length ? `<p class="activity-empty">${state.loading ? "正在读取运行日志…" : error ? "" : "此前未记录运行日志"}</p>` : ""}${error ? `<p class="activity-log-error" role="status">${escape(error)}</p>` : ""}<button type="button" class="subtle" data-log-more ${!state.more && !error ? "hidden" : ""} ${state.loading ? "disabled" : ""}>${error ? "重新读取" : "加载更多日志"}</button>`;
      host.querySelector("[data-log-more]").onclick = () => void load();
    };
    const load = async () => {
      if (state.loading || !host.isConnected) return;
      state.loading = true; if (!state.loaded) draw();
      try {
        const result = await request(`/api/activity/${encodeURIComponent(runId)}/logs?after_seq=${state.seq}&limit=100`);
        if (result?.available === false) throw new Error("运行日志暂时不可用，请稍后重试。");
        if (!host.isConnected) return;
        state.run = result.run || state.run; if (state.run) { remember([state.run]); if (state.run.event_id && !state.run.read) void markRead([state.run.event_id]); }
        const knownSeqs = new Set(state.rows.map(row => row.seq));
        state.rows.push(...(result.logs || []).filter(row => !knownSeqs.has(row.seq)));
        state.rows.sort((a, b) => a.seq - b.seq); state.seq = result.next_seq ?? state.rows.at(-1)?.seq ?? state.seq;
        state.more = !!result.has_more; state.loaded = true; state.loading = false; draw();
      } catch (error) { state.loading = false; draw(error.status === 404 ? "记录已删除，或此前未记录运行日志。" : "运行日志暂时无法读取，请稍后重试。"); }
    };
    void load();
    return { refresh: () => !state.more && state.loaded ? load() : Promise.resolve() };
  }
  function showLogs(runId) {
    if (!enabled() || !runId) return;
    initialize();
    logPreviousFocus = document.activeElement;
    const run = known.get(runId);
    logDialog.querySelector("#activity-log-title").textContent = run?.title ? `${run.title} · 运行日志` : "运行日志";
    const body = logDialog.querySelector("#activity-log-body"); body.replaceChildren();
    const host = document.createElement("div"); body.append(host);
    logDialog.querySelector("#activity-log-result").onclick = () => visit(known.get(runId) || { run_id: runId });
    logDialog.querySelector("#activity-log-result").hidden = !safeUrl(run?.url);
    if (!logDialog.open) logDialog.showModal();
    const reader = mountRunLog(host, runId);
    const mount = { details: logDialog, loaded: true, loading: false, refresh: reader.refresh }; logMounts.add(mount);
    logDialog.addEventListener("close", () => logMounts.delete(mount), { once: true });
  }
  function mountLogs(host, options = {}) {
    if (!host || !enabled()) { if (host) host.hidden = true; return; }
    if (host.querySelector(":scope > .activity-inline-logs")) return;
    const details = document.createElement("details"); details.className = "activity-inline-logs";
    details.innerHTML = '<summary>运行日志</summary><div class="activity-inline-body"></div>';
    host.append(details);
    const body = details.querySelector(".activity-inline-body"), readers = [];
    const mount = { details, loaded: false, loading: false, refresh: async () => { for (const reader of readers) await reader.refresh(); } };
    logMounts.add(mount);
    const load = async (before = null) => {
      if (mount.loading) return;
      mount.loading = true;
      if (!mount.loaded) body.innerHTML = '<p class="activity-empty">正在读取运行日志…</p>';
      try {
        let runs;
        let more = false, next;
        if (options.run_id) runs = [{ run_id: options.run_id }];
        else {
          const params = new URLSearchParams({ owner_id: options.owner_id || "", limit: "50" });
          if (options.source_id) params.set("source_id", options.source_id);
          if (before !== null) params.set("before", String(before));
          const result = await request(`/api/activity?${params}`);
          if (result?.available === false) throw new Error("日志暂时不可用");
          runs = (result.items || []).filter(run => !options.source_id || run.source_id === options.source_id); more = !!result.has_more; next = result.next_before;
        }
        if (!host.isConnected) return;
        if (!mount.loaded) body.replaceChildren();
        body.querySelector("[data-log-runs-more]")?.remove();
        remember(runs); mount.loaded = true;
        for (const run of runs) {
          const section = document.createElement("section"); section.className = "activity-log-group";
          if (!options.source_id && !options.run_id) { const title = document.createElement("h4"); title.textContent = run.title || "运行记录"; section.append(title); }
          const lines = document.createElement("div"); section.append(lines); body.append(section); readers.push(mountRunLog(lines, run.run_id));
        }
        if (!readers.length) body.innerHTML = '<p class="activity-empty">此前未记录运行日志</p>';
        if (more && next != null) { const button = document.createElement("button"); button.type = "button"; button.className = "subtle"; button.dataset.logRunsMore = ""; button.textContent = "加载更早记录"; button.onclick = () => void load(next); body.append(button); }
      } catch { body.innerHTML = '<p class="activity-log-error" role="status">运行日志暂时无法读取。</p><button class="subtle" type="button">重新读取</button>'; body.querySelector("button").onclick = () => void load(before); }
      finally { mount.loading = false; }
    };
    details.addEventListener("toggle", () => { if (details.open) { if (!mount.loaded) void load(); else void mount.refresh(); } });
  }

  function settingsMarkup() {
    if (!enabled()) return "";
    return '<section class="section-card settings-card activity-settings" aria-labelledby="settings-activity-heading"><div class="settings-card-heading"><h2 id="settings-activity-heading">任务完成提醒</h2></div><p class="settings-note">任务完成、失败或需要人工处理时，工作台会保留提醒与运行日志。桌面提醒仅在此浏览器中生效，网页保持打开并处于后台时静音显示。</p><p class="settings-note" id="settings-activity-status" role="status" aria-live="polite"></p><div class="settings-actions"><button class="secondary compact" id="settings-activity-enable" type="button">开启桌面提醒</button><button class="subtle" id="settings-activity-disable" type="button" hidden>关闭桌面提醒</button></div></section>';
  }
  function mountSettings(panel) {
    if (!enabled()) return;
    const button = panel.querySelector("#settings-activity-enable"), off = panel.querySelector("#settings-activity-disable"), text = panel.querySelector("#settings-activity-status");
    if (!button || !off || !text) return;
    const paint = () => {
      const support = "Notification" in window, permission = support ? Notification.permission : "unsupported", on = storage.get(keys.desktop) === "enabled" && permission === "granted";
      button.hidden = on; off.hidden = !on; button.disabled = !support || permission === "denied";
      text.textContent = !support ? "此浏览器不支持桌面提醒。站内提示和运行日志仍可使用。" : permission === "denied" ? "浏览器已阻止桌面提醒。可在浏览器的网站权限中允许通知；站内提示和日志仍可使用。" : on ? "桌面提醒已开启（静音）。关闭网页后的历史结果可在任务面板查看，不会补弹桌面通知。" : "桌面提醒未开启。站内提示和运行日志已启用。";
    };
    button.onclick = async () => {
      button.disabled = true;
      try { const permission = await Notification.requestPermission(); if (permission === "granted") storage.set(keys.desktop, "enabled"); else storage.remove(keys.desktop); }
      catch { storage.remove(keys.desktop); }
      paint();
    };
    off.onclick = () => { storage.remove(keys.desktop); paint(); };
    paint();
  }

  function initialize() {
    if (!enabled()) { document.querySelector("#task-activity-open")?.setAttribute("hidden", ""); return; }
    if (initialized) return;
    initialized = true;
    browserId = storage.get(keys.browser) || id(); storage.set(keys.browser, browserId);
    dialog = document.createElement("dialog"); dialog.id = "task-activity-dialog"; dialog.className = "activity-dialog"; dialog.setAttribute("aria-labelledby", "activity-title");
    dialog.innerHTML = '<div class="activity-heading"><div><h2 id="activity-title">任务</h2><p>进度、提醒与运行日志</p></div><button type="button" class="subtle" data-activity-close aria-label="关闭任务面板">×</button></div><div class="activity-controls"><div class="activity-filters" role="group" aria-label="任务状态"><button type="button" data-activity-filter="active" aria-pressed="false">进行中</button><button type="button" data-activity-filter="unread" aria-pressed="false">未读</button><button type="button" data-activity-filter="all" aria-pressed="true">全部</button></div><label>产品<select id="activity-product" aria-label="任务产品筛选"></select></label><button type="button" class="subtle" id="activity-read-all">全部已读</button></div><p id="activity-connection" class="activity-connection" role="status"></p><div class="activity-list" id="activity-list"></div><div class="activity-footer"><button type="button" class="subtle" id="activity-more" hidden>加载更多</button></div>';
    logDialog = document.createElement("dialog"); logDialog.id = "task-activity-log-dialog"; logDialog.className = "activity-dialog activity-log-dialog"; logDialog.setAttribute("aria-labelledby", "activity-log-title");
    logDialog.innerHTML = '<div class="activity-heading"><h2 id="activity-log-title">运行日志</h2><button type="button" class="subtle" data-log-close aria-label="关闭运行日志">×</button></div><div id="activity-log-body" class="activity-log-body"></div><div class="activity-footer"><button type="button" class="secondary compact" id="activity-log-result">查看结果</button></div>';
    notices = document.createElement("div"); notices.id = "task-activity-notices"; notices.className = "activity-notices"; notices.setAttribute("role", "region"); notices.setAttribute("aria-label", "任务提醒"); notices.setAttribute("aria-live", "polite");
    document.body.append(dialog, logDialog, notices);
    dialog.querySelector("[data-activity-close]").onclick = () => dialog.close();
    logDialog.querySelector("[data-log-close]").onclick = () => logDialog.close();
    dialog.addEventListener("close", () => { ++panelSequence; panel.loading = false; previousFocus?.isConnected && previousFocus.focus(); });
    logDialog.addEventListener("close", () => { logDialog.querySelector("#activity-log-body").replaceChildren(); logPreviousFocus?.isConnected && logPreviousFocus.focus(); });
    dialog.querySelector("#activity-product").onchange = event => { panel.product = event.target.value; void loadPanel(); };
    dialog.querySelector("#activity-more").onclick = () => void loadPanel(true);
    dialog.querySelector("#activity-read-all").onclick = () => void markRead([], true);
    dialog.addEventListener("click", event => {
      const filter = event.target.closest("[data-activity-filter]");
      if (filter) { panel.filter = filter.dataset.activityFilter; dialog.querySelectorAll("[data-activity-filter]").forEach(button => button.setAttribute("aria-pressed", String(button === filter))); void loadPanel(); }
      const log = event.target.closest("[data-activity-log]"), result = event.target.closest("[data-activity-result]");
      if (log) showLogs(log.dataset.activityLog);
      if (result) visit(known.get(result.dataset.activityResult) || { run_id: result.dataset.activityResult });
    });
    document.querySelector("#task-activity-open")?.addEventListener("click", open);
    window.addEventListener("focus", () => { focusPresence(); refresh(); document.querySelectorAll(".activity-settings").forEach(mountSettings); });
    window.addEventListener("blur", focusPresence);
    window.addEventListener("pagehide", () => { try { if (JSON.parse(storage.get(keys.focus) || "null")?.tab === tabId) storage.remove(keys.focus); } catch { /* Presence expires if browser storage is unavailable. */ } clearTimeout(timer); });
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", () => { focusPresence(); refresh(); });
    window.addEventListener("storage", event => { if (event.key === keys.desktop) document.querySelectorAll(".activity-settings").forEach(mountSettings); });
    window.addEventListener("workbench:productchange", () => { if (dialog.open) { panel.product = product(); dialog.querySelector("#activity-product").innerHTML = productOptions(); void loadPanel(); } });
    window.addEventListener("workbench:navigated", event => didNavigate(event.detail?.hash));
    paintBadge(); schedule(0);
  }
  return { initialize, open, showLogs, mountLogs, settingsMarkup, mountSettings, refresh, didNavigate };
})();
