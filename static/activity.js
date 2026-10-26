"use strict";

// Activity has its own read model. It never modifies a task's business version.
window.TaskActivity = (() => {
  const keys = { browser: "workbench-activity-browser-v1", cursor: "workbench-activity-cursor-v1", desktop: "workbench-activity-desktop-v1", focus: "workbench-activity-focus-v1", expanded: "workbench-activity-expanded-v1" };
  const activeStates = new Set(["queued", "running", "stopping", "cancelling", "cancel_requested", "saving", "recovering"]);
  const warningStates = new Set(["interrupted", "needs_attention", "awaiting_confirmation", "pending_review", "awaiting_review", "recovery_pending", "result_unpersisted"]);
  const labels = { queued: "排队", running: "进行中", stopping: "正在停止", cancelling: "取消中", cancel_requested: "取消中", saving: "保存中", recovering: "恢复中", completed: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消", needs_attention: "待处理", awaiting_confirmation: "待确认", pending_review: "待人工审查", awaiting_review: "待人工审查", recovery_pending: "待恢复", result_unpersisted: "结果待保存" };
  const phases = { queued: "等待执行", running: "正在执行", draft: "生成初稿", review: "复核", format: "整理交付格式", finished: "执行结束", save: "保存结果", reconciled: "核对恢复状态", stopping: "正在停止", voice: "生成配音", timeline: "建立时间线", edit: "AI 剪辑", validation: "核验工程", stills: "寻找底图", images: "生成图片", image_review: "核对图片", group_review: "整组验收", package: "打包成品", settled: "后续已处理" };
  const kinds = { ai: "AI 写稿", voice: "配音", timeline: "时间线", edit: "AI 剪辑", validation: "工程核验", stills: "找底图", images: "出图", image_review: "图片核对", group_review: "整组验收", package: "成品包", table: "逐张表", clips: "图文片段", data: "数据备份", case: "自动拆解", prepublish: "发布前审查" };
  const filters = [["current", "当前", "进行中和需处理的任务；今天已完成的折叠在最下方"], ["unread", "未读", ""], ["all", "全部", ""]];
  const modes = [["all", "完整"], ["ai", "AI 说明"], ["key", "关键节点"]];
  const icons = {
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    expand: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg>',
    collapse: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"/></svg>',
    go: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/></svg>',
    history: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
  };
  let initialized = false, timer, polling = false, refreshPending = false, failures = 0, claimsPaused = false;
  let cursor = null, epoch = null, browserId = "", counts = { active: 0, queued: 0, unread: 0 }, available = true;
  let dialog, notices, productPicker, kindPicker, previousFocus, panelSequence = 0, searchTimer, lastMode = "all";
  let panel, viewer = null;
  let requestTail = Promise.resolve(), pendingRead = new Set(), pendingVisit = null;
  const tabId = id(), known = new Map(), strips = new Set(), owners = new Map();
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const enabled = () => !window.PublicTextMode && !document.documentElement.classList.contains("public-text-mode");
  const product = () => window.WorkbenchShell?.product?.() || "";
  const foreground = () => document.visibilityState !== "hidden" && document.hasFocus();
  const safeUrl = value => typeof value === "string" && value.startsWith("#") ? value : "";
  const dockable = () => window.matchMedia("(min-width:1200px)").matches;
  const pad = value => String(value).padStart(2, "0");
  const storage = { get(key) { try { return localStorage.getItem(key); } catch { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch { /* In-memory operation remains available. */ } }, remove(key) { try { localStorage.removeItem(key); } catch { /* Optional browser coordination. */ } } };
  // Remembered between visits: the list filters, the log view mode and which days / the finished block are folded.
  const prefsKey = "workbench-activity-prefs-v1";
  const prefs = loadPrefs();
  lastMode = prefs.mode;
  panel = blankPanel();

  function loadPrefs() {
    let saved = {};
    try { saved = JSON.parse(storage.get(prefsKey) || "{}") || {}; } catch { /* Defaults apply. */ }
    const text = value => typeof value === "string" ? value : "";
    const filter = filters.some(([value]) => value === saved.filter) ? saved.filter : "current";
    // Each tab keeps its own filters and folded days. Legacy shared folds never hide unread tasks.
    const view = (value, folded) => ({ product: typeof value?.product === "string" ? value.product : null, kind: Object.hasOwn(kinds, value?.kind) ? value.kind : "", query: text(value?.query).slice(0, 100), collapsed: new Set(Array.isArray(folded) ? folded.filter(item => typeof item === "string") : []) });
    return {
      filter,
      views: Object.fromEntries(filters.map(([value]) => {
        const savedView = saved.views?.[value];
        const folded = savedView?.collapsed ?? (value === filter && value !== "unread" ? saved.collapsed : []);
        return [value, view(savedView ?? (value === filter ? saved : null), folded)];
      })),
      mode: modes.some(([value]) => value === saved.mode) ? saved.mode : "all",
      doneOpen: saved.doneOpen === true,
    };
  }
  // The scoped panel (one order's history) is a temporary view, so its filters are never stored.
  function savePrefs() {
    if (!panel.scope) { Object.assign(prefs, { filter: panel.filter, doneOpen: panel.doneOpen }); Object.assign(prefs.views[panel.filter], { product: panel.product, kind: panel.kind, query: panel.query }); }
    prefs.mode = lastMode;
    // Keep only the most recent folded days per tab so the lists cannot grow without bound.
    const views = Object.fromEntries(Object.entries(prefs.views).map(([key, value]) => [key, { ...value, collapsed: [...value.collapsed].slice(-60) }]));
    storage.set(prefsKey, JSON.stringify({ ...prefs, views }));
  }

  function id() { return globalThis.crypto?.randomUUID?.() || `activity-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  // A scoped panel (opened from an order's log strip) shows that order's whole history; the general panel opens on what is current.
  function blankPanel(scope = null) { return { items: [], product: scope ? "" : prefs.views[prefs.filter].product ?? product(), filter: scope ? "all" : prefs.filter, kind: scope ? "" : prefs.views[prefs.filter].kind, query: scope ? "" : prefs.views[prefs.filter].query, scope, since: "", open: new Set(), unfold: new Set(), collapsed: Object.fromEntries(filters.map(([value]) => [value, new Set()])), doneOpen: !scope && prefs.doneOpen, tableOpen: false, before: null, hasMore: false, loading: false, error: "" }; }
  function collapsedDays() { return panel.scope ? panel.collapsed[panel.filter] : prefs.views[panel.filter].collapsed; }
  function request(path, options) {
    const result = requestTail.catch(() => {}).then(() => api(path, options));
    requestTail = result.catch(() => {});
    return result;
  }
  function parse(value) { const parsed = value ? new Date(value) : null; return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null; }
  function date(value) {
    const parsed = parse(value);
    return parsed ? parsed.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "";
  }
  function dayKey(value) { const parsed = parse(value); return parsed ? `${parsed.getFullYear()}-${parsed.getMonth()}-${parsed.getDate()}` : ""; }
  const today = () => dayKey(new Date().toISOString());
  function clock(value, withDay = false) {
    const parsed = parse(value);
    if (!parsed) return "";
    const time = `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}:${pad(parsed.getSeconds())}`;
    return withDay ? `${pad(parsed.getMonth() + 1)}/${pad(parsed.getDate())} ${time}` : time;
  }
  function when(value) {
    const parsed = parse(value);
    if (!parsed) return "";
    return dayKey(value) === today() ? `${pad(parsed.getHours())}:${pad(parsed.getMinutes())}` : `${pad(parsed.getMonth() + 1)}/${pad(parsed.getDate())}`;
  }
  function dayLabel(value) {
    const parsed = parse(value);
    if (!parsed) return "更早";
    const start = new Date(); start.setHours(0, 0, 0, 0);
    const days = Math.round((new Date(parsed).setHours(0, 0, 0, 0) - start.getTime()) / 86400000);
    return days === 0 ? "今天" : days === -1 ? "昨天" : `${parsed.getMonth() + 1} 月 ${parsed.getDate()} 日`;
  }
  function seconds(run, end = run?.finished_at || (activeStates.has(run?.status) ? new Date().toISOString() : run?.updated_at)) {
    const elapsed = Math.max(0, new Date(end).getTime() - new Date(run?.started_at).getTime());
    return Number.isFinite(elapsed) ? Math.floor(elapsed / 1000) : null;
  }
  function duration(run, end) {
    const value = seconds(run, end);
    if (value === null) return "";
    return value < 60 ? `${value} 秒` : value < 3600 ? `${Math.floor(value / 60)} 分 ${value % 60} 秒` : `${Math.floor(value / 3600)} 小时 ${Math.floor(value / 60) % 60} 分`;
  }
  function brief(value) {
    if (value === null || value === undefined) return "";
    return value < 60 ? `${value} 秒` : value < 3600 ? `${Math.floor(value / 60)} 分` : `${Math.floor(value / 3600)} 小时 ${Math.floor(value / 60) % 60} 分`;
  }
  function status(run) { return run.owner_archived ? `${labels[run.status] || '已结束'} · 已归档` : run.attention_resolved ? `${labels[run.status] || '已结束'} · 后续已处理` : labels[run.status] || (run.finished_at ? "已结束" : "处理中"); }
  const isTable = run => run?.kind === "table" || /^table:TBL-[0-9a-f]{12}$/.test(run?.run_id || "");
  function phaseLabel(value) { return phases[value] || value || ""; }
  function tone(run) {
    if (activeStates.has(run?.status)) return "active";
    if (run?.status === "failed") return "danger";
    if (warningStates.has(run?.status)) return "warning";
    return run?.status === "completed" ? "done" : "muted";
  }
  function normalizeRun(run) {
    // Older services recorded these concrete resource waits as running.
    const waiting = /^(?:等待达芬奇(?:：|$)|等待同项目的其他剪辑选完素材(?:，|。|$)|waiting_same_project$)/.test(run?.phase || "");
    return run?.status === "running" && ["timeline", "edit"].includes(run.kind) && waiting ? { ...run, status: "queued" } : run;
  }
  function remember(items = []) { for (const item of items) if (item?.run_id) known.set(item.run_id, normalizeRun(item)); }
  function readCounts(value = {}) {
    const active = Math.max(0, Number(value.active) || 0);
    const queued = Object.hasOwn(value, "queued") ? Math.max(0, Number(value.queued) || 0) : [...known.values()].filter(run => run.status === "queued").length;
    return { active, queued: Math.min(active, queued), unread: Number(value.unread) || 0 };
  }
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
    if (!button.querySelector("[data-activity-queued]")) {
      const badge = document.createElement("span");
      badge.className = "activity-count"; badge.dataset.activityQueued = ""; badge.hidden = true;
      button.insertBefore(badge, button.querySelector("[data-activity-unread]"));
    }
    const displayed = { active: Math.max(0, counts.active - counts.queued), queued: counts.queued, unread: counts.unread };
    button.hidden = !enabled();
    button.setAttribute("aria-label", available ? `任务，${displayed.active} 项进行中${displayed.queued ? `，${displayed.queued} 项排队` : ""}，${displayed.unread} 条未读` : "任务，运行日志暂时不可用");
    button.title = available ? "任务提醒与运行日志" : "运行日志暂时不可用，任务可继续执行";
    for (const key of ["active", "queued", "unread"]) {
      const badge = button.querySelector(`[data-activity-${key}]`);
      // The word sits in its own span so the phone header can show the number alone as a corner badge.
      if (badge) { badge.innerHTML = `${displayed[key]}<span class="activity-count-label"> ${{ active: "进行中", queued: "排队", unread: "未读" }[key]}</span>`; badge.hidden = !displayed[key]; }
    }
  }
  function schedule(delay) { clearTimeout(timer); if (initialized && enabled()) timer = setTimeout(() => void poll(), delay); }
  function refresh() { if (!initialized || !enabled()) return; if (polling) refreshPending = true; else schedule(0); }

  async function poll() {
    if (polling || !enabled()) return;
    polling = true; clearTimeout(timer); focusPresence();
    let more = false, changed = null;
    try {
      const baseline = cursor === null;
      const data = await request(`/api/activity?limit=50${baseline ? "" : `&after=${cursor}`}`);
      if (data?.available === false) { available = false; throw new Error("运行日志暂时不可用"); }
      if (!data || !Number.isSafeInteger(data.cursor)) throw new Error("运行日志暂时无法读取");
      available = true;
      if (!baseline && epoch !== data.epoch) {
        cursor = null; epoch = null; known.clear(); owners.clear(); notices?.replaceChildren();
        more = true;
      } else {
        remember(data.items);
        changed = baseline ? null : new Set((data.items || []).map(item => item.run_id));
        for (const removed of data.removed || []) { known.delete(removed); changed?.add(removed); }
        // A focused tab receives the alert first. Deferred tabs retain their cursor,
        // and later pass these same events through the server's atomic claim.
        const events = baseline ? [] : data.events || [];
        if (claimsPaused && foreground()) claimsPaused = false;
        if (events.length && (anotherForegroundTab() || claimsPaused)) more = false;
        else {
          if (events.length) await notify(events);
          cursor = data.cursor; epoch = data.epoch;
          storage.set(keys.cursor, JSON.stringify({ cursor, epoch }));
          more = !baseline && !!data.has_more;
        }
      }
      counts = readCounts(data.counts);
      failures = 0;
      if (dialog?.open && !panel.loading) await loadPanel(false, true);
      if (viewer && dialog?.open) {
        if (known.has(viewer.runId)) viewer.run = known.get(viewer.runId);
        if (viewer.loaded && !viewer.loading && (!changed || changed.has(viewer.runId) || activeStates.has(viewer.run?.status))) void loadLog("after");
        else paintHead();
      }
    } catch { failures += 1; /* A transient failure never creates another alert. */ }
    finally {
      polling = false; paintBadge(); paintStrips();
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
    const claim = () => request("/api/activity/notify-claim", { method: "POST", body: { browser_id: browserId, event_ids: unique.map(item => item.event_id) } });
    let response;
    try { response = await claim(); }
    catch (error) {
      // A background tab skips the state refresh, so after a restart it still holds the old page token.
      // Renew it once and retry; if the claim is still refused, wait until this tab is in front again.
      if (error.status !== 403 || typeof renewPageToken !== "function") throw error;
      await renewPageToken();
      try { response = await claim(); }
      catch (again) { if (again.status === 403) claimsPaused = true; throw again; }
    }
    const claimed = new Set(response?.claimed || []);
    for (const event of unique.filter(item => claimed.has(item.event_id))) {
      addNotice(event);
      if (!foreground() && event.status !== "cancelled" && storage.get(keys.desktop) === "enabled" && window.Notification?.permission === "granted") {
        try {
          const notification = new Notification(event.title || "工作台任务", { body: isTable(event) ? "打开逐张表" : event.message || status(event), silent: true, tag: `workbench-${epoch || ""}-${event.event_id}` });
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
    node.innerHTML = `<button type="button" class="activity-notice-view"><strong>${escape(event.title || "工作台任务")}</strong>${isTable(event) ? '<small>打开逐张表</small>' : `<span>${escape(event.message || status(event))}</span><small>${escape(event.product || "")} · 查看结果</small>`}</button><button type="button" class="subtle activity-notice-close" aria-label="收起这条提醒">×</button>`;
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
    let destination = safeUrl(item.url);
    const tableId = /^table:(TBL-[0-9a-f]{12})$/.exec(item.run_id || '')?.[1] || (item.kind === 'table' && /^TBL-[0-9a-f]{12}$/.test(item.source_id || '') ? item.source_id : '');
    if (tableId && destination) {
      if (window.CaseScripts?.canResumeTableResult && !window.CaseScripts.canResumeTableResult()) {toast('请先完成或关闭当前编辑，再查看逐张表。',true);return;}
      const parsed = window.WorkbenchShell?.parse?.(destination);
      if (parsed?.tail.length && ['scripts','content'].includes(parsed.page) && !parsed.params.has('table')) destination += `${destination.includes('?') ? '&' : '?'}table=${encodeURIComponent(tableId)}`;
    }
    // A docked panel stays beside the page it opens; an overlay steps aside.
    if (dialog?.open && dialog.matches(":modal")) dialog.close();
    if (destination) {
      pendingVisit = { item, hash: window.WorkbenchShell?.url?.(destination) || destination };
      if (location.hash === pendingVisit.hash) {didNavigate(location.hash);if (tableId) void window.CaseScripts?.resumeTableResult?.(tableId,item.owner_id || '');}
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
  function markOpened(run) { if (run?.event_id && !run.read) void markRead([run.event_id]); }

  // The run list: one compact row per run, newest activity first, grouped by day.
  function productOptions() {
    const values = new Map([["", "全部产品"]]);
    const choices = window.WorkbenchShell?.getProductChoices?.();
    if (choices) for (const { value, label } of choices) values.set(value, label);
    else for (const option of document.querySelectorAll("#global-product option")) if (option.value) values.set(option.value, option.textContent);
    if (panel.product && !values.has(panel.product)) values.set(panel.product, panel.product);
    return [...values].map(([value, title]) => `<option value="${escape(value)}" ${panel.product === value ? "selected" : ""}>${escape(title)}</option>`).join("");
  }
  function kindChoices() { return [["", "全部类型"], ...Object.entries(kinds)].map(([value, label]) => ({ value, label })); }
  function kindOptions() { return kindChoices().map(({ value, label }) => `<option value="${value}" ${panel.kind === value ? "selected" : ""}>${escape(label)}</option>`).join(""); }
  function syncControls() {
    dialog.querySelector("#activity-product").innerHTML = productOptions();
    dialog.querySelector("#activity-kind").innerHTML = kindOptions();
    dialog.querySelector("#activity-search").value = panel.query;
    productPicker?.refresh();
    kindPicker?.refresh();
    dialog.querySelectorAll("[data-activity-filter]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.activityFilter === panel.filter)));
    const scope = dialog.querySelector("#activity-scope");
    scope.hidden = !panel.scope;
    scope.querySelector("span").textContent = panel.scope ? `仅显示：${panel.scope.label || "当前页面的任务"}` : "";
  }
  function paintConnection() {
    const target = dialog?.querySelector("#activity-connection");
    if (!target) return;
    target.textContent = panel.error || (!available ? "运行日志暂时不可用；已有任务仍可继续执行。" : failures ? "连接暂时中断，正在自动重试…" : "");
    target.hidden = !target.textContent;
  }
  // Runs are grouped by the order they belong to. A title reads "<step> · <order>": the order names the group, the step names the run inside it.
  const settled = run => run?.status === "completed" || run?.status === "cancelled";
  // A failure stops asking once the same step of the same order has run again; the later run carries the outcome.
  const superseded = run => !['images','image_review','stills','clips'].includes(run.kind) && (run.status === "failed" || run.status === "interrupted") && [...known.values()].some(other => other.owner_id === run.owner_id && other.kind === run.kind && String(other.started_at || "") > String(run.started_at || ""));
  const waiting = run => activeStates.has(run.status) || !run.owner_archived && !run.attention_resolved && !settled(run) && !superseded(run);
  const unread = run => !!run.event_id && !run.read && !run.owner_archived && !run.attention_resolved;
  const byUpdate = (a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
  function naming(run) {
    const title = String(run.title || "").trim(), cut = title.indexOf(" · ");
    if (run.kind === "data") return { head: title || kinds.data, label: kinds.data };
    if (cut < 0) return { head: kinds[run.kind] || "任务", label: title || "工作台任务" };
    let head = title.slice(0, cut), label = title.slice(cut + 3);
    const round = run.kind === "ai" ? / · (第 \d+ 轮)$/.exec(label) : null;
    if (round) { label = label.slice(0, round.index); head += ` · ${round[1]}`; }
    return { head, label };
  }
  function grouped(items) {
    const owners = new Map();
    for (const run of items) {
      const key = run.owner_id || run.run_id;
      if (!owners.has(key)) owners.set(key, []);
      owners.get(key).push(run);
    }
    // Marking a run read bumps its feed version, so orders sort by their runs' own last update instead.
    return [...owners].map(([key, runs]) => {
      runs.sort(byUpdate);
      const lead = runs.find(run => activeStates.has(run.status)) || runs.find(waiting) || runs[0];
      return { key, runs, lead, label: naming(runs[0]).label, at: runs[0].updated_at, open: runs.some(waiting), unread: runs.some(unread) };
    }).sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));
  }
  function goMarkup(run, name) {
    const label = isTable(run) ? "打开逐张表" : `查看结果：${name}`;
    return safeUrl(run.url) ? `<button type="button" class="activity-row-go" data-activity-result="${escape(run.run_id)}" aria-label="${escape(label)}" title="${isTable(run) ? '打开逐张表' : '查看结果'}">${icons.go}</button>` : "";
  }
  function rowMarkup(run, title = run.title || "工作台任务") {
    const fresh = unread(run), selected = viewer?.runId === run.run_id, level = tone(run), spent = brief(seconds(run)), { head } = naming(run), table = isTable(run);
    return `<article class="activity-row${fresh ? " is-unread" : ""}${selected ? " is-selected" : ""}" data-run-id="${escape(run.run_id)}"><button type="button" class="activity-row-main" data-activity-log="${escape(run.run_id)}"${selected ? ' aria-current="true"' : ""}>${table ? '<span aria-hidden="true"></span>' : `<span class="activity-dot is-${level}" aria-hidden="true"></span>`}<span class="activity-row-text"><span class="activity-row-title">${escape(title)}</span>${table ? '' : `<span class="activity-row-meta"><span class="activity-status is-${level}">${escape(status(run))}</span><span>${escape(`${head}：${run.message || phaseLabel(run.phase)}`)}</span></span>`}</span><span class="activity-row-side"><time datetime="${escape(run.updated_at || "")}">${escape(when(run.updated_at))}</time>${spent && !table ? `<small>${escape(spent)}</small>` : ""}</span>${fresh ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${goMarkup(run, title)}</article>`;
  }
  // One line per run inside an order: the step, its status and its last message. Identical runs in a row share one line.
  function runMarkup(run, repeat = null) {
    const fresh = repeat ? repeat.runs.some(unread) : unread(run), selected = viewer?.runId === run.run_id, level = tone(run), { head } = naming(run), table = isTable(run);
    return `<article class="activity-row activity-sub${fresh ? " is-unread" : ""}${selected ? " is-selected" : ""}" data-run-id="${escape(run.run_id)}"><button type="button" class="activity-row-main" data-activity-log="${escape(run.run_id)}"${selected ? ' aria-current="true"' : ""}>${table ? '<span aria-hidden="true"></span>' : `<span class="activity-dot is-${level}" aria-hidden="true"></span>`}<span class="activity-sub-text"><b>${escape(head)}</b>${table ? '' : `<span class="activity-status is-${level}">${escape(status(run))}</span><span class="activity-sub-message">${escape(run.message || phaseLabel(run.phase))}</span>`}</span><time class="activity-sub-time" datetime="${escape(run.updated_at || "")}">${escape(when(run.updated_at))}</time>${fresh ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${repeat ? `<button type="button" class="activity-sub-repeat" data-activity-unfold="${escape(repeat.key)}" title="展开这 ${repeat.runs.length} 次相同的运行">×${repeat.runs.length}</button>` : ""}${goMarkup(run, head)}</article>`;
  }
  function runsMarkup(group) {
    const out = [], same = (a, b) => naming(a).head === naming(b).head && a.status === b.status && a.message === b.message;
    for (let index = 0; index < group.runs.length;) {
      let end = index + 1;
      while (end < group.runs.length && same(group.runs[end], group.runs[index])) end += 1;
      const span = group.runs.slice(index, end), key = `${group.key}:${span[0].run_id}`;
      if (span.length >= 3 && !panel.unfold.has(key) && !span.slice(1).some(run => run.run_id === viewer?.runId)) out.push(runMarkup(span[0], { key, runs: span }));
      else out.push(...span.map(run => runMarkup(run)));
      index = end;
    }
    return out.join("");
  }
  function groupMarkup(group) {
    if (group.runs.length === 1) return rowMarkup(group.runs[0], group.label);
    const run = group.lead, otherRuns = group.runs.filter(item => !isTable(item)), table = !otherRuns.length;
    const summary = otherRuns.find(item => activeStates.has(item.status)) || otherRuns.find(waiting) || otherRuns[0] || run;
    const level = tone(summary), expanded = panel.open.has(group.key), { head } = naming(summary);
    const selected = !expanded && group.runs.some(item => item.run_id === viewer?.runId);
    return `<article class="activity-row activity-group-row${group.unread ? " is-unread" : ""}${selected ? " is-selected" : ""}"><button type="button" class="activity-row-main" data-activity-group="${escape(group.key)}" aria-expanded="${expanded}">${table ? '<span aria-hidden="true"></span>' : `<span class="activity-dot is-${level}" aria-hidden="true"></span>`}<span class="activity-row-text"><span class="activity-row-title">${escape(group.label)}</span>${table ? '' : `<span class="activity-row-meta"><span class="activity-status is-${level}">${escape(status(summary))}</span><span>${escape(`${head}：${summary.message || phaseLabel(summary.phase)}`)}</span></span>`}</span><span class="activity-row-side"><time datetime="${escape(group.at || "")}">${escape(when(group.at))}</time><small class="activity-group-count">${group.runs.length} 次${icons.chevron}</small></span>${group.unread ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${goMarkup(run, group.label)}</article>${expanded ? `<div class="activity-group-runs" role="group" aria-label="${escape(group.label)}">${runsMarkup(group)}</div>` : ""}`;
  }
  // Folded days are remembered only within their own tab; a new day always starts open.
  function dayed(entries, at, render) {
    const days = [];
    for (const entry of entries) {
      const key = dayKey(at(entry)) || "older";
      if (days.at(-1)?.key !== key) days.push({ key, label: dayLabel(at(entry)), entries: [] });
      days.at(-1).entries.push(entry);
    }
    return days.map(day => {
      const folded = collapsedDays().has(day.key);
      return `<h3 class="activity-day"><button type="button" data-activity-day="${escape(day.key)}" aria-expanded="${!folded}">${icons.chevron}<span>${escape(day.label)}</span><small>${day.entries.length}</small></button></h3>${folded ? "" : day.entries.map(render).join("")}`;
    }).join("");
  }
  // 当前 lists orders still running or waiting on someone; orders that finished today fold into one line below them.
  function visibleGroups() {
    const groups = grouped(panel.filter === 'current' ? panel.items.filter(run => !run.owner_archived || activeStates.has(run.status)) : panel.items);
    if (panel.filter !== "current") return { groups, shown: groups, done: [], tableDone: [] };
    const shown = groups.filter(group => group.open), finished = groups.filter(group => !group.open).flatMap(group => group.runs);
    const done = grouped(finished.filter(run => !isTable(run)));
    const tableDone = grouped(finished.filter(isTable)).map(group => ({ ...group, key: `table:${group.key}` }));
    return { groups: [...shown, ...done, ...tableDone], shown, done, tableDone };
  }
  function paintList() {
    const list = dialog.querySelector("#activity-list");
    const focusKey = ["data-activity-log", "data-activity-result", "data-activity-group", "data-activity-done", "data-activity-table-done", "data-activity-day"].find(name => list.contains(document.activeElement) && document.activeElement.hasAttribute(name));
    const focusValue = focusKey && document.activeElement.getAttribute(focusKey);
    const { groups, shown, done, tableDone } = visibleGroups();
    // A run opened from outside (a notice, a strip) shows inside its order once that order is listed.
    const reveal = viewer && !viewer.revealed && groups.find(group => group.runs.some(run => run.run_id === viewer.runId));
    if (reveal) { viewer.revealed = true; if (reveal.lead.run_id !== viewer.runId) panel.open.add(reveal.key); }
    let html = panel.scope ? dayed([...panel.items].sort(byUpdate), run => run.updated_at, runMarkup) : dayed(shown, group => group.at, groupMarkup);
    if (!html && done.length && !tableDone.length) html = `<p class="activity-empty">当前没有进行中或需处理的任务</p>`;
    if (done.length) html += `<button type="button" class="activity-done-fold" data-activity-done aria-expanded="${panel.doneOpen}">${icons.chevron}今天已完成 ${done.length} 项</button>${panel.doneOpen ? done.map(groupMarkup).join("") : ""}`;
    if (tableDone.length) html += `<button type="button" class="activity-done-fold" data-activity-table-done aria-expanded="${panel.tableOpen}">${icons.chevron}今天的逐张表 ${tableDone.length} 项</button>${panel.tableOpen ? tableDone.map(groupMarkup).join("") : ""}`;
    list.innerHTML = html || `<p class="activity-empty">${panel.loading ? "正在读取任务…" : panel.kind === "table" ? "暂无逐张表入口" : { unread: "没有未读提醒", current: "当前没有进行中或需处理的任务" }[panel.filter] || (panel.query || panel.kind ? "没有符合条件的任务" : "暂无运行记录")}</p>`;
    if (focusKey) [...list.querySelectorAll(`[${focusKey}]`)].find(node => node.getAttribute(focusKey) === focusValue)?.focus({ preventScroll: true });
    return shown;
  }
  function paintPanel() {
    const shown = paintList();
    const more = dialog.querySelector("#activity-more"); more.hidden = !panel.hasMore; more.disabled = panel.loading; more.textContent = panel.loading ? "正在读取…" : "加载更多";
    dialog.querySelector("#activity-read-all").disabled = !counts.unread;
    paintConnection();
    // Other task logs preview automatically; a table's log opens only when requested.
    const first = panel.scope ? [...panel.items].sort(byUpdate).find(run => !isTable(run)) : shown.map(group => !isTable(group.lead) ? group.lead : group.runs.find(run => !isTable(run))).find(Boolean);
    if (!viewer && first && !panel.loading) select(first.run_id, false);
    else if (!viewer) paintDetail();
  }
  function startOfDay() { const start = new Date(); start.setHours(0, 0, 0, 0); return start.toISOString(); }
  function listed(run) {
    if (!run) return false;
    if (panel.filter === "unread") return !run.read;
    if (panel.filter === "current") return waiting(run) || (parse(run.finished_at || run.updated_at) || 0) >= new Date(panel.since);
    return true;
  }
  async function loadPanel(append = false, quiet = false) {
    const sequence = ++panelSequence;
    if (!append && !quiet && viewer && !viewer.explicit) viewer = null;
    panel.loading = true; panel.error = "";
    if (!quiet) paintPanel();
    // Each page holds whole orders, so an order's run count is complete once it is listed.
    const params = new URLSearchParams({ limit: "50", filter: panel.filter, group: "owner" });
    if (panel.filter === "current") { panel.since = startOfDay(); params.set("since", panel.since); }
    if (panel.product) params.set("product", panel.product);
    if (panel.kind) params.set("kind", panel.kind);
    if (panel.query.trim()) params.set("q", panel.query.trim());
    if (panel.scope?.owner_id) params.set("owner_id", panel.scope.owner_id);
    if (panel.scope?.source_id) params.set("source_id", panel.scope.source_id);
    if (append && panel.before !== null) params.set("before", String(panel.before));
    try {
      const data = await request(`/api/activity?${params}`);
      if (sequence !== panelSequence || !dialog.open) return;
      if (data?.available === false) throw new Error("运行日志暂时不可用；已有任务仍可继续执行。");
      const items = (data.items || []).map(normalizeRun);
      remember(items);
      // A background refresh keeps older pages already loaded by the reader.
      const keep = row => known.has(row.run_id) && listed(known.get(row.run_id));
      const old = append ? panel.items : quiet ? panel.items.filter(row => !items.some(next => next.run_id === row.run_id) && keep(row)) : [];
      panel.items = append ? [...old, ...items.filter(item => !old.some(row => row.run_id === item.run_id))] : [...items, ...old].map(item => known.get(item.run_id) || item);
      if (!quiet || !old.length) { panel.hasMore = !!data.has_more; panel.before = data.next_before ?? null; }
      if (data.counts) counts = readCounts(data.counts);
      paintBadge();
    } catch (error) { if (sequence === panelSequence) panel.error = error.status === 400 ? "这项筛选需要重启工作台服务后才能使用。" : error.message || "任务记录暂时无法读取。"; }
    finally { if (sequence === panelSequence) { panel.loading = false; if (dialog.open) paintPanel(); } }
  }

  // The selected run's log: newest lines first loaded, grouped by phase, repeats folded.
  function select(runId, explicit = true) {
    if (!runId || !dialog) return;
    if (!explicit && isTable(known.get(runId))) return;
    if (viewer?.runId === runId) { if (explicit) { viewer.explicit = true; markOpened(viewer.run); } return; }
    viewer = { runId, run: known.get(runId) || null, rows: [], first: null, next: 0, earlier: false, loading: false, loaded: false, error: "", mode: lastMode, groups: new Map(), folds: new Set(), stick: true, unseen: 0, legacy: false, explicit, revealed: false };
    paintList();
    paintDetail(true);
    void loadLog("tail");
  }
  async function loadLog(kind) {
    const state = viewer;
    if (!state || state.loading) return;
    state.loading = true;
    if (kind !== "after") paintBody("earlier");
    const query = kind === "before" ? `before_seq=${state.first}&limit=300` : kind === "after" || state.legacy ? `after_seq=${state.next}&limit=${state.legacy ? 200 : 300}` : "tail=1&limit=300";
    let more = false;
    try {
      const result = await request(`/api/activity/${encodeURIComponent(state.runId)}/logs?${query}`);
      if (viewer !== state) return;
      if (result?.available === false) throw new Error("运行日志暂时不可用，请稍后重试。");
      if (result.run) { state.run = normalizeRun(result.run); remember([result.run]); }
      const seen = new Set(state.rows.map(row => row.seq)), fresh = (result.logs || []).filter(row => !seen.has(row.seq));
      state.rows.push(...fresh); state.rows.sort((a, b) => a.seq - b.seq);
      if (kind === "before") { state.earlier = !!result.has_earlier; state.first = result.first_seq ?? state.rows[0]?.seq ?? state.first; }
      else if (kind === "tail" && !state.legacy) { state.earlier = !!result.has_earlier; state.first = result.first_seq ?? state.rows[0]?.seq ?? null; state.next = result.next_seq ?? state.rows.at(-1)?.seq ?? 0; }
      else { state.next = result.next_seq ?? state.rows.at(-1)?.seq ?? state.next; more = !!result.has_more; if (state.loaded && !state.stick) state.unseen += fresh.length; }
      state.loaded = true; state.error = "";
      if (state.explicit) markOpened(state.run);
    } catch (error) {
      if (viewer !== state) return;
      // A service started before this page learned tail paging; read forward instead.
      if (kind === "tail" && error.status === 400 && !state.legacy) { state.legacy = true; state.loading = false; return loadLog("tail"); }
      state.error = error.status === 404 ? "记录已删除，或此前未记录运行日志。" : "运行日志暂时无法读取，请稍后重试。";
    } finally { if (viewer === state) state.loading = false; }
    if (viewer !== state || !dialog?.open) return;
    paintHead(); paintBody(kind === "before" ? "earlier" : "follow");
    if (more) void loadLog("after");
  }
  function paintDetail(rebuild = false) {
    const host = dialog?.querySelector("#activity-detail");
    if (!host) return;
    if (!viewer) { host.innerHTML = panel.loading ? "" : `<p class="activity-empty">选择一条任务查看运行日志</p>`; return; }
    if (rebuild || !host.querySelector(".activity-log-body")) {
      host.innerHTML = `<div class="activity-detail-head"></div><section class="activity-needs" hidden></section><div class="activity-log-tools" role="group" aria-label="日志显示范围">${modes.map(([value, title]) => `<button type="button" data-log-mode="${value}" aria-pressed="${viewer.mode === value}">${title}</button>`).join("")}</div><div class="activity-log-body" id="activity-log-body" tabindex="0" aria-label="运行日志"></div>`;
      const body = host.querySelector(".activity-log-body");
      body.addEventListener("scroll", () => {
        if (!viewer) return;
        viewer.stick = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
        if (viewer.stick && viewer.unseen) { viewer.unseen = 0; body.querySelector("[data-log-jump]")?.setAttribute("hidden", ""); }
      });
    }
    paintHead(); paintBody("follow");
  }
  function refreshNeeds() {
    if (!viewer) return;
    const run = viewer.run || known.get(viewer.runId), needs = dialog?.querySelector('.activity-needs');
    const signature = JSON.stringify([viewer.runId,run?.owner_id,run?.source_id,run?.status,run?.result_count,run?.event_id,run?.reconcile_pending]);
    if (run && needs && needs.dataset.needsSignature !== signature) {
      needs.dataset.needsSignature = signature;
      void window.ProductionNeeds?.mountRun(needs,run,{onVisit:(event,link) => {event.preventDefault();visit({...run,url:link.getAttribute('href')});}});
    }
  }
  function paintHead() {
    const head = dialog?.querySelector(".activity-detail-head");
    if (!head || !viewer) return;
    refreshNeeds();
    const run = viewer.run || known.get(viewer.runId) || { run_id: viewer.runId };
    const facts = [run.started_at ? `开始 ${date(run.started_at)}` : "", duration(run) ? `耗时 ${duration(run)}` : "", Number.isFinite(run.result_count) ? `${run.result_count} 项结果` : "", viewer.loaded ? `${viewer.rows.length}${viewer.earlier ? "+" : ""} 行` : ""].filter(Boolean);
    head.innerHTML = `<div class="activity-detail-text"><h3 id="activity-detail-title">${escape(run.title || "运行日志")}</h3><p class="activity-log-summary"><span class="activity-detail-status is-${tone(run)}">${escape(run.status ? status(run) : "")}</span>${escape(facts.join(" · "))}</p></div><div class="activity-detail-actions"><button type="button" class="subtle" data-log-copy title="复制已加载的日志文本"${viewer.rows.length ? "" : " disabled"}>${icons.copy}复制</button>${safeUrl(run.url) ? `<button type="button" class="secondary compact" data-activity-visit>查看结果</button>` : ""}</div>`;
  }
  function buildGroups(rows, run) {
    const groups = [], now = today();
    let previous = null;
    for (const row of rows) {
      const label = phaseLabel(row.phase) || labels[row.status] || "步骤";
      let group = groups.at(-1);
      if (!group || group.label !== label) { group = { key: String(row.seq), label, rows: [], start: row.at, warn: "" }; groups.push(group); }
      const level = row.status === "failed" ? "danger" : warningStates.has(row.status) || row.status === "cancelled" ? "warning" : "";
      if (level && group.warn !== "danger") group.warn = level;
      group.rows.push({ ...row, ai: /^AI\s*[：:]/.test(row.message || ""), key: !previous || previous.status !== row.status, warn: level,
        showDay: previous ? dayKey(previous.at) !== dayKey(row.at) : dayKey(row.at) !== now,
        counted: Number.isFinite(row.result_count) && row.result_count !== previous?.result_count });
      previous = row;
    }
    groups.forEach((group, index) => { group.end = groups[index + 1]?.start || run.finished_at || (activeStates.has(run.status) ? new Date().toISOString() : group.rows.at(-1).at); });
    return groups;
  }
  const plain = row => row && !row.ai && !row.key && !row.warn && !row.counted;
  // Progress callbacks often repeat one line or take turns between two; those runs collapse into one row.
  function foldEnd(rows, start) {
    let end = start;
    while (plain(rows[end + 1]) && rows[end + 1].message === rows[start].message) end += 1;
    if (end - start >= 2) return end;
    const first = rows[start].message, second = rows[start + 1]?.message;
    if (!plain(rows[start + 1]) || first === second) return start;
    end = start + 1;
    while (plain(rows[end + 1]) && (rows[end + 1].message === first || rows[end + 1].message === second)) end += 1;
    const span = rows.slice(start, end + 1);
    return span.length >= 4 && span.filter(row => row.message === first).length >= 2 && span.filter(row => row.message === second).length >= 2 ? end : start;
  }
  function lineMarkup(row, run) {
    const total = duration(run, row.at), classes = ["activity-log-line", row.ai && "is-ai", row.key && "is-key", row.warn && `is-${row.warn}`].filter(Boolean).join(" ");
    return `<li class="${classes}"><time datetime="${escape(row.at)}"${total ? ` title="开始后 ${escape(total)}"` : ""}>${escape(clock(row.at, row.showDay))}</time><span class="activity-log-text">${escape(row.message || labels[row.status] || "")}${row.counted ? `<small>${escape(row.result_count)} 项结果</small>` : ""}</span></li>`;
  }
  function linesMarkup(rows, state, folding) {
    const run = state.run || {}, out = [];
    for (let index = 0; index < rows.length;) {
      const end = folding && plain(rows[index]) ? foldEnd(rows, index) : index;
      if (end > index) {
        const span = rows.slice(index, end + 1), key = String(rows[index].seq), open = state.folds.has(key);
        const texts = [...new Set(span.map(row => row.message || ""))];
        out.push(`<li class="activity-log-fold${open ? " is-open" : ""}"><time datetime="${escape(rows[index].at)}">${escape(clock(rows[index].at, rows[index].showDay))}</time><button type="button" data-log-fold="${escape(key)}" aria-expanded="${open}" title="${open ? "收起重复进度" : "展开重复进度"}"><span>${escape(texts.join(" / "))}</span><b>×${span.length}</b></button></li>`);
        if (open) out.push(...span.map(row => lineMarkup(row, run)));
        index = end + 1;
      } else { out.push(lineMarkup(rows[index], run)); index += 1; }
    }
    return out.join("");
  }
  function groupsMarkup(state) {
    const run = state.run || {}, groups = buildGroups(state.rows, run), filtered = state.mode !== "all";
    return groups.map((group, index) => {
      const visible = !filtered ? group.rows : group.rows.filter(row => row.warn || row.key || (state.mode === "ai" ? row.ai : row === group.rows[0]));
      if (!visible.length) return "";
      if (group.rows.length === 1 && !group.warn) {
        return `<section class="activity-log-group activity-log-single" aria-label="${escape(group.label)}">${group.label === visible[0].message ? '' : `<small class="activity-single-phase">${escape(group.label)}</small>`}<ol class="activity-log-lines">${linesMarkup(visible,state,!filtered)}</ol></section>`;
      }
      const open = filtered || (state.groups.has(group.key) ? state.groups.get(group.key) : index === groups.length - 1 || !!group.warn || group.rows.length <= 3);
      const spent = brief(seconds({ started_at: group.start }, group.end));
      const meta = `${group.rows.length} 行${visible.length !== group.rows.length ? `，显示 ${visible.length}` : ""}${spent && spent !== "0 秒" ? ` · ${spent}` : ""}`;
      const head = `<button type="button" class="activity-log-phase" data-log-group="${escape(group.key)}" aria-expanded="${open}"${filtered ? " disabled" : ""}>${icons.chevron}<strong>${escape(group.label)}</strong><span class="activity-log-phase-meta">${escape(meta)}</span>${open ? "" : `<span class="activity-log-preview">${escape(group.rows.at(-1).message || "")}</span>`}</button>`;
      return `<section class="activity-log-group${group.warn ? ` is-${group.warn}` : ""}">${head}${open ? `<ol class="activity-log-lines">${linesMarkup(visible, state, !filtered)}</ol>` : ""}</section>`;
    }).join("");
  }
  function paintBody(anchor = "follow") {
    const body = dialog?.querySelector(".activity-log-body");
    if (!body || !viewer) return;
    const state = viewer, fromBottom = body.scrollHeight - body.scrollTop, top = body.scrollTop;
    const active = document.activeElement && body.contains(document.activeElement) ? ["data-log-group", "data-log-fold", "data-log-more", "data-log-retry"].find(name => document.activeElement.hasAttribute(name)) : null;
    const focusValue = active ? document.activeElement.getAttribute(active) : null;
    let html = state.earlier ? `<button type="button" class="subtle activity-log-earlier" data-log-more${state.loading ? " disabled" : ""}>${state.loading ? "正在读取…" : "加载更早的日志"}</button>` : "";
    if (state.rows.length) html += groupsMarkup(state);
    else if (!state.error) html += `<p class="activity-empty">${state.loading || !state.loaded ? "正在读取运行日志…" : "此前未记录运行日志"}</p>`;
    if (state.error) html += `<p class="activity-log-error" role="status">${escape(state.error)}</p><button type="button" class="subtle" data-log-retry>重新读取</button>`;
    html += `<button type="button" class="activity-log-jump" data-log-jump${state.unseen && !state.stick ? "" : " hidden"}>↓ ${state.unseen} 行新日志</button>`;
    body.innerHTML = html;
    if (anchor === "earlier") body.scrollTop = body.scrollHeight - fromBottom;
    else if (anchor === "follow" && state.stick) body.scrollTop = body.scrollHeight;
    else body.scrollTop = top;
    if (active) [...body.querySelectorAll(`[${active}]`)].find(node => node.getAttribute(active) === focusValue)?.focus({ preventScroll: true });
  }
  function plainLog(state) {
    const run = state.run || {};
    const head = [run.title || "运行日志", [status(run), run.started_at ? `开始 ${date(run.started_at)}` : "", duration(run) ? `耗时 ${duration(run)}` : ""].filter(Boolean).join(" · ")];
    if (state.earlier) head.push(`（只含已加载的最近 ${state.rows.length} 行）`);
    return [...head, "", ...state.rows.map(row => `${clock(row.at, true)}  [${phaseLabel(row.phase) || labels[row.status] || "步骤"}] ${row.message || ""}`)].join("\n");
  }
  async function copyLog(button) {
    const text = plainLog(viewer);
    try { await navigator.clipboard.writeText(text); }
    catch {
      const area = document.createElement("textarea"); area.value = text; area.className = "activity-copy-buffer"; dialog.append(area); area.select();
      try { document.execCommand("copy"); } finally { area.remove(); }
    }
    button.textContent = "已复制";
    setTimeout(() => { if (button.isConnected) paintHead(); }, 1500);
  }

  function show() {
    const expanded = storage.get(keys.expanded) === "1";
    dialog.classList.toggle("is-expanded", expanded);
    document.documentElement.classList.toggle("activity-expanded", expanded);
    paintExpand();
    if (dockable()) {
      // One docked panel at a time: the task panel takes the place of a docked basket.
      const basket = document.querySelector("#material-basket-dialog");
      if (basket?.open && !basket.matches(":modal")) basket.close();
      dialog.show();
      document.documentElement.classList.add("activity-docked");
      dialog.querySelector(`[data-activity-filter="${panel.filter}"]`)?.focus({ preventScroll: true });
    } else dialog.showModal();
  }
  function paintExpand() {
    const button = dialog.querySelector("[data-activity-expand]"), on = dialog.classList.contains("is-expanded");
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-label", on ? "收窄任务面板" : "展开任务面板");
    button.title = on ? "收窄" : "展开";
    button.innerHTML = on ? icons.collapse : icons.expand;
  }
  function toggleExpanded() {
    const on = !dialog.classList.contains("is-expanded");
    storage.set(keys.expanded, on ? "1" : "0");
    dialog.classList.toggle("is-expanded", on);
    document.documentElement.classList.toggle("activity-expanded", on);
    paintExpand();
  }
  function open(options = {}) {
    if (!enabled()) return;
    initialize();
    const scope = options.owner_id ? { owner_id: options.owner_id, source_id: options.source_id || "", label: options.label || "" } : null;
    const fresh = !dialog.open;
    if (fresh) previousFocus = document.activeElement;
    if (fresh || scope) {
      panel = blankPanel(scope);
      if (!fresh && viewer?.runId !== options.select) viewer = null;
      if (fresh) viewer = null;
      syncControls();
    }
    if (fresh) show();
    if (options.select) select(options.select, options.explicit !== false);
    if (fresh || scope) void loadPanel();
  }
  function showLogs(runId) {
    if (!enabled() || !runId) return;
    open({ select: runId });
  }
  function undock() { if (dialog?.open && !dialog.matches(":modal")) dialog.close(); }

  // On task pages each run leaves one status line that opens the panel at its log.
  function stripRuns(options) {
    return [...known.values()].filter(run => options.run_id ? run.run_id === options.run_id : run.owner_id === options.owner_id && (!options.source_id || run.source_id === options.source_id))
      .sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  }
  function ensureOwner(ownerId) {
    const entry = owners.get(ownerId);
    if (entry && (entry.state !== "failed" || Date.now() - entry.at < 15000)) return;
    owners.set(ownerId, { state: "loading", at: Date.now() });
    request(`/api/activity?${new URLSearchParams({ owner_id: ownerId, limit: "200" })}`).then(result => {
      if (result?.available === false) throw new Error("运行日志暂时不可用");
      remember(result.items || []);
      owners.set(ownerId, { state: "ready", at: Date.now() });
    }).catch(() => owners.set(ownerId, { state: "failed", at: Date.now() })).finally(() => paintStrips(ownerId));
  }
  function runStrip(runs) {
    const run = runs[0], level = tone(run), spent = brief(seconds(run));
    if (isTable(run)) return `<span class="activity-strip-text">${escape(run.title || '逐张表')}</span><span class="activity-strip-action">日志${icons.chevron}</span>`;
    const detail = [activeStates.has(run.status) && phaseLabel(run.phase) !== run.message ? phaseLabel(run.phase) : "", run.message].filter(Boolean).join(" · ");
    return `<span class="activity-dot is-${level}" aria-hidden="true"></span><span class="activity-strip-status is-${level}">${escape(status(run))}</span><span class="activity-strip-text">${escape(detail)}</span>${spent ? `<span class="activity-strip-time">${escape(spent)}</span>` : ""}<span class="activity-strip-action">日志${runs.length > 1 ? ` · ${runs.length} 次` : ""}${icons.chevron}</span>`;
  }
  // Only runs that still need someone are named; finished history stays behind 全部日志.
  function ownerStrip(runs, label) {
    const otherRuns = runs.filter(run => !isTable(run));
    const active = otherRuns.filter(run => activeStates.has(run.status));
    const running = active.filter(run => run.status !== "queued"), queued = active.filter(run => run.status === "queued");
    const attention = otherRuns.filter(run => !activeStates.has(run.status) && waiting(run));
    const kindsNamed = items => [...new Set(items.map(run => kinds[run.kind] || "任务"))].join("、");
    const summary = [[running, "进行中"], [queued, "排队"]].filter(([items]) => items.length);
    const details = (active.length ? summary : [[attention, "需处理"]]).filter(([items]) => items.length).map(([items, label]) => `${label}：${kindsNamed(items)}`).join("；");
    const lead = active.length ? '<span class="activity-dot is-active" aria-hidden="true"></span>' : attention.length ? '<span class="activity-dot is-warning" aria-hidden="true"></span>' : icons.history;
    return `${lead}<span class="activity-strip-status">${escape(label)} ${runs.length} 次运行${summary.map(([items, label]) => `，${items.length} 项${label}`).join("")}</span>${details ? `<span class="activity-strip-text">${escape(details)}</span>` : ""}<span class="activity-strip-action">全部日志${icons.chevron}</span>`;
  }
  // A quiet strip only speaks while its run is going or needs someone; a run that ended well leaves no line.
  const quietlyDone = run => !activeStates.has(run.status) && (run.owner_archived || run.attention_resolved || run.status === "completed" || run.status === "cancelled" || superseded(run));
  function paintStrip(strip) {
    const { options } = strip;
    if (options.owner_id) ensureOwner(options.owner_id);
    const runs = stripRuns(options);
    strip.host.hidden = !runs.length || (options.quiet && quietlyDone(runs[0]));
    if (strip.host.hidden) { strip.node.replaceChildren(); return; }
    const single = options.source_id || options.run_id;
    strip.node.innerHTML = single ? runStrip(runs) : ownerStrip(runs, options.label || "本制作单");
    strip.node.title = single ? `${runs[0].title || "运行日志"}：查看运行日志` : "在任务面板查看全部运行日志";
  }
  function paintStrips(ownerId) {
    (window.keepReadingPlace || (change => change()))(() => {
      for (const strip of [...strips]) {
        if (!strip.host.isConnected) { strips.delete(strip); continue; }
        if (!ownerId || strip.options.owner_id === ownerId) paintStrip(strip);
      }
    });
  }
  function mountLogs(host, options = {}) {
    if (!host || !enabled()) { if (host) host.hidden = true; return; }
    if (host.querySelector(":scope > .activity-strip")) return;
    initialize();
    const node = document.createElement("button"); node.type = "button"; node.className = "activity-strip";
    const strip = { host, node, options: { owner_id: options.owner_id || "", source_id: options.source_id || "", run_id: options.run_id || "", label: options.label || "", quiet: options.quiet === true } };
    node.addEventListener("click", () => {
      const runs = stripRuns(strip.options), single = strip.options.source_id || strip.options.run_id;
      open({ owner_id: strip.options.owner_id || runs[0]?.owner_id, source_id: strip.options.source_id, label: single ? runs[0]?.title || "本项任务" : strip.options.label || "本制作单", select: runs[0]?.run_id });
    });
    host.hidden = true; host.append(node); strips.add(strip);
    paintStrip(strip);
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

  function onClick(event) {
    const target = event.target;
    // Backdrop clicks target the modal dialog itself, so check the panel bounds.
    if (target === dialog && dialog.matches(":modal")) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) { dialog.close(); return; } }
    const filter = target.closest("[data-activity-filter]");
    if (filter) {
      savePrefs();
      panel.filter = filter.dataset.activityFilter;
      if (!panel.scope) { const view = prefs.views[panel.filter]; Object.assign(panel, { product: view.product ?? product(), kind: view.kind, query: view.query }); savePrefs(); syncControls(); }
      dialog.querySelectorAll("[data-activity-filter]").forEach(button => button.setAttribute("aria-pressed", String(button === filter))); void loadPanel(); return; }
    const day = target.closest("[data-activity-day]");
    if (day) { const key = day.dataset.activityDay, collapsed = collapsedDays(); if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key); savePrefs(); paintList(); return; }
    // Opening an order lists its runs and shows the run that matters most; closing it keeps the log in view.
    const order = target.closest("[data-activity-group]");
    if (order) {
      const key = order.dataset.activityGroup, group = visibleGroups().groups.find(item => item.key === key);
      if (panel.open.has(key)) panel.open.delete(key); else panel.open.add(key);
      if (group && panel.open.has(key) && !isTable(group.lead) && !group.runs.some(run => run.run_id === viewer?.runId)) select(group.lead.run_id, true);
      else paintList();
      return;
    }
    if (target.closest("[data-activity-done]")) { panel.doneOpen = !panel.doneOpen; savePrefs(); paintList(); return; }
    if (target.closest("[data-activity-table-done]")) { panel.tableOpen = !panel.tableOpen; paintList(); return; }
    const unfold = target.closest("[data-activity-unfold]");
    if (unfold) { panel.unfold.add(unfold.dataset.activityUnfold); paintList(); return; }
    const log = target.closest("[data-activity-log]"), result = target.closest("[data-activity-result]");
    if (log) { select(log.dataset.activityLog, true); return; }
    if (result) { visit(known.get(result.dataset.activityResult) || { run_id: result.dataset.activityResult }); return; }
    if (target.closest("[data-activity-visit]") && viewer) { visit(known.get(viewer.runId) || viewer.run || { run_id: viewer.runId }); return; }
    if (target.closest("[data-activity-close]")) { dialog.close(); return; }
    if (target.closest("[data-activity-expand]")) { toggleExpanded(); return; }
    if (target.closest("[data-activity-unscope]")) { panel = blankPanel(); syncControls(); void loadPanel(); return; }
    if (!viewer) return;
    const mode = target.closest("[data-log-mode]");
    if (mode) { viewer.mode = lastMode = mode.dataset.logMode; savePrefs(); dialog.querySelectorAll("[data-log-mode]").forEach(button => button.setAttribute("aria-pressed", String(button === mode))); paintBody("follow"); return; }
    const group = target.closest("[data-log-group]");
    if (group) { viewer.groups.set(group.dataset.logGroup, group.getAttribute("aria-expanded") !== "true"); viewer.stick = false; paintBody("keep"); return; }
    const fold = target.closest("[data-log-fold]");
    if (fold) { const key = fold.dataset.logFold; if (viewer.folds.has(key)) viewer.folds.delete(key); else viewer.folds.add(key); viewer.stick = false; paintBody("keep"); return; }
    if (target.closest("[data-log-more]")) { void loadLog("before"); return; }
    if (target.closest("[data-log-retry]")) { viewer.error = ""; void loadLog(viewer.loaded ? "after" : "tail"); return; }
    if (target.closest("[data-log-jump]")) { viewer.stick = true; viewer.unseen = 0; paintBody("follow"); return; }
    const copy = target.closest("[data-log-copy]");
    if (copy) void copyLog(copy);
  }
  function initialize() {
    if (!enabled()) { document.querySelector("#task-activity-open")?.setAttribute("hidden", ""); return; }
    if (initialized) return;
    initialized = true;
    browserId = storage.get(keys.browser) || id(); storage.set(keys.browser, browserId);
    dialog = document.createElement("dialog"); dialog.id = "task-activity-dialog"; dialog.className = "activity-dialog"; dialog.setAttribute("aria-labelledby", "activity-title");
    dialog.innerHTML = `<div class="activity-heading"><h2 id="activity-title">任务</h2><p id="activity-connection" class="activity-connection" role="status" hidden></p><div class="activity-heading-actions"><button type="button" class="subtle" id="activity-read-all">全部已读</button><button type="button" class="subtle activity-icon" data-activity-expand aria-pressed="false"></button><button type="button" class="subtle activity-icon" data-activity-close aria-label="关闭任务面板" title="关闭">${icons.close}</button></div></div><div class="activity-layout"><section class="activity-runs" aria-label="任务列表"><div class="activity-controls"><div class="activity-filters" role="group" aria-label="任务状态">${filters.map(([value, title, tip]) => `<button type="button" data-activity-filter="${value}" aria-pressed="${value === "current"}"${tip ? ` title="${tip}"` : ""}>${title}</button>`).join("")}</div><div class="activity-refine"><div class="activity-product-field"><span class="activity-field-label" id="activity-product-label">产品</span><select id="activity-product" aria-label="任务产品筛选" title="提醒覆盖全部产品；这里只筛选列表，不会清空未读"></select></div><div class="activity-kind-field"><span class="activity-field-label">任务类型</span><select id="activity-kind" aria-label="任务类型筛选"></select></div><div class="activity-search-field">${icons.search}<input type="search" id="activity-search" maxlength="100" placeholder="搜索任务标题" aria-label="搜索任务标题"></div></div><p class="activity-scope" id="activity-scope" hidden><span></span><button type="button" class="subtle" data-activity-unscope>显示全部任务</button></p></div><div class="activity-list" id="activity-list"></div><div class="activity-footer"><button type="button" class="subtle" id="activity-more" hidden>加载更多</button></div></section><section class="activity-detail" id="activity-detail" aria-label="运行日志"></section></div>`;
    notices = document.createElement("div"); notices.id = "task-activity-notices"; notices.className = "activity-notices"; notices.setAttribute("role", "region"); notices.setAttribute("aria-label", "任务提醒"); notices.setAttribute("aria-live", "polite");
    document.body.append(dialog, notices);
    paintExpand();
    dialog.addEventListener("close", () => {
      // An outside log launcher can reopen the panel before this queued event fires.
      if (dialog.open) return;
      ++panelSequence; panel.loading = false; viewer = null;
      document.documentElement.classList.remove("activity-docked", "activity-expanded");
      previousFocus?.isConnected && previousFocus.focus();
    });
    // A docked panel is not modal, so Esc only closes it while focus is inside; elsewhere Esc keeps its page meaning.
    dialog.addEventListener("keydown", event => { if (event.key === "Escape" && !dialog.matches(":modal")) { event.preventDefault(); dialog.close(); } });
    dialog.querySelector("#activity-product").onchange = event => { panel.product = event.target.value; savePrefs(); void loadPanel(); };
    productPicker = window.WorkbenchShell?.createProductPicker?.(dialog.querySelector("#activity-product"), { prefix: "activity-product", title: "筛选任务产品", label: "任务产品筛选", getValue: () => panel.product });
    dialog.querySelector("#activity-kind").onchange = event => { panel.kind = event.target.value; savePrefs(); void loadPanel(); };
    kindPicker = window.WorkbenchShell?.createFilterPicker?.(dialog.querySelector("#activity-kind"), {
      prefix: "activity-kind", title: "筛选任务类型", label: "任务类型筛选", getValue: () => panel.kind, getChoices: kindChoices,
      group: (code, selected) => !code ? "" : code === selected ? "当前类型" : "任务类型",
      searchPlaceholder: "搜索任务类型", searchLabel: "搜索任务类型", listLabel: "任务类型",
      emptyText: "没有找到匹配的任务类型", emptyHint: "试试配音、出图等关键词", countLabel: count => `${count} 种类型`,
    });
    dialog.querySelector("#activity-search").oninput = event => { panel.query = event.target.value; savePrefs(); clearTimeout(searchTimer); searchTimer = setTimeout(() => void loadPanel(), 250); };
    dialog.querySelector("#activity-more").onclick = () => void loadPanel(true);
    dialog.querySelector("#activity-read-all").onclick = () => void markRead([], true);
    dialog.addEventListener("click", onClick);
    document.addEventListener("click", event => {
      if (!dialog.open || dialog.matches(":modal") || dialog.contains(event.target) || event.target.closest("#task-activity-open, .activity-strip, .activity-notice-view")) return;
      // Keep focus on the page control the user just clicked.
      previousFocus = null;
      dialog.close();
    }, { capture: true });
    document.querySelector("#task-activity-open")?.addEventListener("click", () => { if (dialog.open) dialog.close(); else open(); });
    window.addEventListener("focus", () => { focusPresence(); refresh(); document.querySelectorAll(".activity-settings").forEach(mountSettings); });
    window.addEventListener("blur", focusPresence);
    window.addEventListener("pagehide", () => { try { if (JSON.parse(storage.get(keys.focus) || "null")?.tab === tabId) storage.remove(keys.focus); } catch { /* Presence expires if browser storage is unavailable. */ } clearTimeout(timer); });
    window.addEventListener("pageshow", refresh);
    document.addEventListener("visibilitychange", () => { focusPresence(); refresh(); });
    window.addEventListener("storage", event => { if (event.key === keys.desktop) document.querySelectorAll(".activity-settings").forEach(mountSettings); });
    window.addEventListener("workbench:productchange", () => { for (const view of Object.values(prefs.views)) view.product = product();
      if (!panel.scope) panel.product = product();
      savePrefs(); if (dialog.open && !panel.scope) { syncControls(); void loadPanel(); } });
    window.addEventListener("workbench:navigated", event => didNavigate(event.detail?.hash));
    paintBadge(); schedule(0);
  }
  return { initialize, open, showLogs, mountLogs, settingsMarkup, mountSettings, refresh, didNavigate, undock };
})();
