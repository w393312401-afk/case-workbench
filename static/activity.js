"use strict";

// Activity has its own read model. It never modifies a task's business version.
window.TaskActivity = (() => {
  const keys = { browser: "workbench-activity-browser-v1", cursor: "workbench-activity-cursor-v1", desktop: "workbench-activity-desktop-v1", sound: "workbench-activity-sound-v1", stay: "workbench-activity-stay-v1", focus: "workbench-activity-focus-v1", expanded: "workbench-activity-expanded-v1", pinned: "workbench-activity-pinned-v1" };
  const activeStates = new Set(["queued", "running", "stopping", "cancelling", "cancel_requested", "saving", "recovering"]);
  const warningStates = new Set(["interrupted", "needs_attention", "awaiting_confirmation", "pending_review", "awaiting_review", "recovery_pending", "result_unpersisted"]);
  const labels = { queued: "排队", running: "进行中", stopping: "正在停止", cancelling: "取消中", cancel_requested: "取消中", saving: "保存中", recovering: "恢复中", completed: "已完成", failed: "失败", interrupted: "已中断", cancelled: "已取消", needs_attention: "待处理", awaiting_confirmation: "待确认", pending_review: "待人工审查", awaiting_review: "待人工审查", recovery_pending: "待恢复", result_unpersisted: "结果待保存" };
  const phases = { queued: "等待执行", running: "正在执行", draft: "生成初稿", review: "复核", format: "整理交付格式", finished: "执行结束", save: "保存结果", reconciled: "核对恢复状态", stopping: "正在停止", voice: "生成配音", timeline: "建立时间线", edit: "AI 剪辑", validation: "核验工程", settled: "后续已处理" };
  const kinds = { ai: "AI 写稿", voice: "配音", timeline: "时间线", edit: "AI 剪辑", validation: "工程核验", data: "数据备份", case: "自动拆解", photo: "图文" };
  // Filters are one row of buttons. The reader can hide the built-in ones and add their own from these kinds and states.
  const kindOptions = [["ai", "写稿"], ["voice", "配音"], ["timeline", "时间线"], ["edit", "剪辑"], ["validation", "核验"], ["photo", "图文"], ["case", "拆解"], ["data", "备份"]];
  const stateOptions = [["needs", "等你处理"], ["running", "进行中"], ["failed", "出错"], ["done", "已完成"]];
  const videoKinds = ["voice", "timeline", "edit", "validation"];
  const builtinFilters = [
    { id: "all", label: "全部" },
    { id: "needs", label: "等你处理", states: ["needs"] },
    { id: "running", label: "进行中", states: ["running"] },
    { id: "failed", label: "出错", states: ["failed"] },
    { id: "video", label: "视频", kinds: videoKinds },
    { id: "photo", label: "图文", kinds: ["photo"] },
    { id: "writing", label: "写稿", kinds: ["ai"] },
  ];
  const icons = {
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6 6 18"/></svg>',
    expand: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M10 20H4v-6M20 4l-7 7M4 20l7-7"/></svg>',
    collapse: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 10h-6V4M4 14h6v6M14 10l7-7M10 14l-7 7"/></svg>',
    go: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8"/></svg>',
    chevron: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 6 6-6 6"/></svg>',
    copy: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a1 1 0 0 1 1-1h9"/></svg>',
    history: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5M12 7v5l3 2"/></svg>',
    search: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/></svg>',
    back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m15 6-6 6 6 6"/></svg>',
    pin: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 4.5 19.5 9l-3.2 1.8-3.1 3.1-.4 3.6-1.3 1.3-6.4-6.4 1.3-1.3 3.6-.4 3.1-3.1z"/><path d="m8.5 15.5-4 4"/></svg>',
    readAll: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 12 5 5L22 7"/><path d="m2 12 5 5m5-5 5-5"/></svg>',
    tune: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg>',
    plus: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  };
  let initialized = false, timer, polling = false, refreshPending = false, failures = 0, claimsPaused = false;
  let cursor = null, epoch = null, browserId = "", counts = { active: 0, queued: 0, unread: 0 }, available = true;
  let dialog, notices, previousFocus, panelSequence = 0, searchTimer, lastMode = "key";
  let panel, viewer = null;
  // The filter row's edit mode, and the filter being written in it (null when the form is closed).
  let chipEdit = { on: false, draft: null };
  // What waits on the reader, from the same source as the home page's 待我处理 plus 待发布 and 图文: keyed by order.
  let work = { items: [], owners: new Map(), product: null, at: 0 };
  let workRequest = null, workSequence = 0;
  let writeTail = Promise.resolve(), pendingRead = new Set(), pendingVisit = null;
  const tabId = id(), desktopNotices = new Map(), known = new Map(), strips = new Set(), owners = new Map();
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const enabled = () => !window.PublicTextMode && !document.documentElement.classList.contains("public-text-mode");
  const product = () => window.WorkbenchShell?.product?.() || "";
  const foreground = () => document.visibilityState !== "hidden" && document.hasFocus();
  const safeUrl = value => typeof value === "string" && value.startsWith("#") ? value : "";
  const dockable = () => window.matchMedia("(min-width:1200px)").matches;
  const pad = value => String(value).padStart(2, "0");
  const storage = { get(key) { try { return localStorage.getItem(key); } catch { return null; } }, set(key, value) { try { localStorage.setItem(key, value); } catch { /* In-memory operation remains available. */ } }, remove(key) { try { localStorage.removeItem(key); } catch { /* Optional browser coordination. */ } } };
  let pinned = storage.get(keys.pinned) === "1";
  // Remembered between visits: the day view (清单 or 时间轴), the search and the log view mode.
  const prefsKey = "workbench-activity-prefs-v1";
  const prefs = loadPrefs();
  lastMode = prefs.mode;
  panel = blankPanel();

  function loadPrefs() {
    let saved = {};
    try { saved = JSON.parse(storage.get(prefsKey) || "{}") || {}; } catch { /* Defaults apply. */ }
    const known = (values, options) => Array.isArray(values) ? [...new Set(values.filter(value => options.some(([key]) => key === value)))] : [];
    const custom = (Array.isArray(saved.custom) ? saved.custom : []).slice(0, 20).map(item => ({
      id: typeof item?.id === "string" && /^c-[a-z0-9-]{1,40}$/.test(item.id) ? item.id : "", label: String(item?.label || "").trim().slice(0, 12),
      kinds: known(item?.kinds, kindOptions), states: known(item?.states, stateOptions), q: String(item?.q || "").trim().slice(0, 40),
    })).filter(item => item.id && item.label && (item.kinds.length || item.states.length || item.q));
    const hidden = Array.isArray(saved.hidden) ? saved.hidden.filter(key => key !== "all" && builtinFilters.some(item => item.id === key)) : [];
    return { view: saved.view === "timeline" ? "timeline" : "list", query: typeof saved.query === "string" ? saved.query.slice(0, 100) : "", mode: saved.mode === "all" ? "all" : "key",
      filter: typeof saved.filter === "string" ? saved.filter : "all", custom, hidden };
  }
  // The scoped panel (one order's history) is a temporary view, so its search is never stored.
  function savePrefs() {
    if (!panel.scope) Object.assign(prefs, { view: panel.view, query: panel.query });
    prefs.custom = prefs.custom.slice(0, 20);
    prefs.mode = lastMode;
    storage.set(prefsKey, JSON.stringify(prefs));
  }

  function id() { return globalThis.crypto?.randomUUID?.() || `activity-${Date.now()}-${Math.random().toString(36).slice(2)}`; }
  // Calendar days are local days; a week starts on Monday.
  function midnight(value = new Date()) { const day = new Date(value); day.setHours(0, 0, 0, 0); return day; }
  function shift(day, days) { const next = new Date(day); next.setDate(next.getDate() + days); return midnight(next); }
  function weekOf(day) { return shift(day, -((day.getDay() + 6) % 7)); }
  // The general panel opens on today's page of the calendar; a scoped panel (opened from an order's log strip) lists that order's whole history.
  function blankPanel(scope = null) { const query = scope ? "" : prefs.query, day = midnight(); return { items: [], pending: [], product: scope ? "" : product(), query, searchOpen: !!query, scope, view: prefs.view, day, week: weekOf(day), autoDay: !scope, autoNote: false, open: new Set(), unfold: new Set(), collapsed: new Set(), before: null, hasMore: false, loading: false, error: "" }; }
  function collapsedDays() { return panel.collapsed; }
  const calendar = () => !panel.scope && !panel.query.trim();
  function request(path, options) {
    // A slow background read must not hold up the list or a selected run's log.
    // Writes still keep their order for notification claims and read receipts.
    if (!options?.method || options.method === "GET") return api(path, options);
    const result = writeTail.catch(() => {}).then(() => api(path, options));
    writeTail = result.catch(() => {});
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
  function status(run) { return run.owner_archived ? `${labels[run.status] || '已结束'} · 已归档` : run.attention_resolved ? run.status === "needs_attention" ? "已处理" : `${labels[run.status] || '已结束'} · 后续已处理` : labels[run.status] || (run.finished_at ? "已结束" : "处理中"); }
  function phaseLabel(value) { return phases[value] || value || ""; }
  function tone(run) {
    if (activeStates.has(run?.status)) return "active";
    // A request for attention that a later step already settled no longer asks anything of the reader.
    if (run?.owner_archived || run?.attention_resolved) return "muted";
    if (run?.status === "failed") return "danger";
    if (warningStates.has(run?.status)) return "warning";
    return run?.status === "completed" ? "done" : "muted";
  }
  function normalizeRun(run) {
    // Older services recorded these concrete resource waits as running.
    const waiting = /^(?:等待达芬奇(?:：|$)|等待同项目的其他剪辑选完素材(?:，|。|$)|waiting_same_project$)/.test(run?.phase || "");
    return run?.status === "running" && ["timeline", "edit"].includes(run.kind) && waiting ? { ...run, status: "queued" } : run;
  }
  function remember(items = []) {
    for (const item of items) if (item?.run_id) {
      const previous = known.get(item.run_id);
      // Independent reads may finish out of order; a late log must not undo a newer status.
      if (Number.isSafeInteger(previous?.version) && Number.isSafeInteger(item.version) && previous.version > item.version) continue;
      known.set(item.run_id, normalizeRun(item));
    }
  }
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
    paintReadAll();
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
    const fresh = unique.filter(item => claimed.has(item.event_id));
    // One chime per batch; a failure in the batch wins. A run the reader cancelled makes no sound.
    const audible = fresh.filter(item => item.status !== "cancelled");
    if (audible.length && soundOn()) chime(audible.some(item => item.status === "failed") ? "failed" : audible.some(item => item.status !== "completed") ? "warning" : "done");
    for (const event of fresh) {
      addNotice(event);
      if (!foreground() && event.status !== "cancelled" && storage.get(keys.desktop) === "enabled" && window.Notification?.permission === "granted") {
        try {
          // 一直留着: the notice stays on the desktop until it is clicked, closed, or the result is read here.
          const stay = storage.get(keys.stay) !== "off";
          const notification = new Notification(`${labels[event.status] || "已结束"} · ${event.title || "工作台任务"}`, { body: event.message || status(event), silent: true, requireInteraction: stay, tag: `workbench-${epoch || ""}-${event.event_id}` });
          desktopNotices.set(event.event_id, notification);
          notification.onclose = () => desktopNotices.delete(event.event_id);
          notification.onclick = () => { window.focus(); notification.close(); visit(event); };
          if (!stay) setTimeout(() => notification.close(), 20000);
        } catch { /* Browser restrictions never prevent the in-app alert. */ }
      }
    }
  }
  function addNotice(event) {
    if (!notices || notices.querySelector(`[data-event-id="${event.event_id}"]`)) return;
    const node = document.createElement("article");
    node.className = `activity-notice is-${event.status === "cancelled" ? "muted" : eventTone(event)}`; node.dataset.eventId = event.event_id;
    node.innerHTML = `<button type="button" class="activity-notice-view"><b class="activity-notice-status">${escape(labels[event.status] || "已结束")}</b><strong>${escape(event.title || "工作台任务")}</strong><span>${escape(event.message || status(event))}</span><small>${escape(event.product || "")} · 查看结果</small></button><button type="button" class="icon-button activity-notice-close" aria-label="收起这条提醒">${icons.close}</button>`;
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
    // A docked panel stays beside the page it opens; an overlay steps aside.
    if (dialog?.open && dialog.matches(":modal")) dialog.close();
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
      for (const node of [...notices?.children || []]) if (all || ids.includes(Number(node.dataset.eventId))) node.remove();
      for (const [eventId, notification] of desktopNotices) if (all || ids.includes(eventId)) { try { notification.close(); } catch { /* Already gone. */ } desktopNotices.delete(eventId); }
      if (dialog?.open) await loadPanel(false, true);
      refresh();
    } catch { if (dialog?.open) { panel.error = "已读状态暂未保存，请重试。"; paintConnection(); } }
    finally { ids.forEach(value => pendingRead.delete(value)); }
  }
  function markOpened(run) { if (run?.event_id && !run.read) void markRead([run.event_id]); }

  // A short chime, made on the spot so no audio file is needed. On by default; the browser only lets it play
  // after the page has had one click or key press, so the context is opened on the first one.
  let audio = null;
  const soundOn = () => storage.get(keys.sound) !== "off";
  function audioContext() {
    const Context = window.AudioContext || window.webkitAudioContext;
    if (!Context) return null;
    try { audio ||= new Context(); if (audio.state === "suspended") void audio.resume().catch(() => {}); } catch { audio = null; }
    return audio;
  }
  function chime(kind = "done") {
    const context = audioContext();
    if (!context || context.state !== "running") return;
    // 完成: two rising notes. 出错: two falling, lower notes. 等你处理: one note twice.
    const notes = kind === "failed" ? [[523.25, 0], [392, 0.16]] : kind === "warning" ? [[659.25, 0], [659.25, 0.2]] : [[659.25, 0], [987.77, 0.14]];
    const start = context.currentTime + 0.02;
    for (const [frequency, at] of notes) {
      const tone = context.createOscillator(), gain = context.createGain();
      tone.type = "sine"; tone.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, start + at);
      gain.gain.exponentialRampToValueAtTime(0.25, start + at + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + at + 0.38);
      tone.connect(gain).connect(context.destination);
      tone.start(start + at); tone.stop(start + at + 0.4);
    }
  }
  const eventTone = event => event.status === "failed" ? "failed" : event.status === "completed" ? "done" : "warning";

  // The run list: one compact row per run, newest activity first. 当前 splits into what needs you and what is running.
  function syncControls() {
    const search = dialog.querySelector("#activity-search"), field = dialog.querySelector(".activity-search-field"), toggle = dialog.querySelector("[data-activity-search]");
    search.value = panel.query;
    field.hidden = !panel.searchOpen && !panel.query;
    toggle.setAttribute("aria-pressed", String(!field.hidden));
    toggle.classList.toggle("is-on", !!panel.query);
    dialog.querySelector("#activity-week").hidden = !calendar();
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
  const superseded = run => (run.status === "failed" || run.status === "interrupted") && [...known.values()].some(other => other.owner_id === run.owner_id && other.kind === run.kind && String(other.started_at || "") > String(run.started_at || ""));
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
  function filterList() { return [...builtinFilters.filter(item => item.id === "all" || !prefs.hidden.includes(item.id)), ...prefs.custom]; }
  // An order's history (opened from a page strip) is always shown whole.
  function activeFilter() { return panel.scope ? builtinFilters[0] : filterList().find(item => item.id === prefs.filter) || builtinFilters[0]; }
  const kindOk = (kind, f) => !f.kinds?.length || f.kinds.includes(kind);
  const textOk = (text, f) => !f.q || String(text || "").toLowerCase().includes(f.q.toLowerCase());
  const runOk = (run, f) => kindOk(run.kind, f) && textOk(run.title, f);
  const failedNow = run => (run.status === "failed" || run.status === "interrupted") && !superseded(run) && !run.attention_resolved && !run.owner_archived;
  const running = group => group.runs.some(run => activeStates.has(run.status));
  function groupOk(group, f) {
    if (!f.states?.length) return true;
    return f.states.some(state => state === "needs" ? needsYou(group, f) : state === "running" ? running(group) : state === "failed" ? group.runs.some(failedNow) : !running(group) && !needsYou(group, f));
  }
  // Each to-do carries the kinds it belongs to: an order's video steps, its 图文, or a draft's 写稿.
  // The line about undecided 图文 shows only under a filter that asks for 图文.
  const workKindOk = (item, f) => item.digest ? !!f.kinds?.includes("photo") : !f.kinds?.length || item.kinds.some(kind => f.kinds.includes(kind));
  const workOk = (item, f) => workKindOk(item, f) && textOk([item.timeline_name, item.title].join(" "), f) && (!f.states?.length || f.states.includes("needs"));
  function picked(items, f) { return grouped(items.filter(run => runOk(run, f))).filter(group => groupOk(group, f)); }
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
  // An order with nothing left waiting reads as what it needs now; its settled failures become one line of history.
  const ownerStates = new Map(), stateTones = { success: "done", warning: "warning", running: "active", neutral: "muted", muted: "muted" };
  function orderNow(group) {
    const state = ownerStates.get(group.key);
    if (!state || !state.label || group.runs.some(waiting)) return null;
    const failed = group.runs.filter(run => run.status === "failed" || run.status === "interrupted");
    const latest = group.runs[0], note = failed.length ? `历史：${failed.length} 次失败已处理` : `${naming(latest).head}：${latest.message || phaseLabel(latest.phase)}`;
    const pending = Number(state.carousel_pending) > 0 ? `${state.carousel_pending} 条待决定图文` : "";
    return { label: pending ? `${state.label} · ${pending}` : state.label, level: stateTones[state.tone] || "muted", note };
  }
  function goMarkup(run, name) {
    const label = `查看结果：${name}`;
    return safeUrl(run.url) ? `<button type="button" class="activity-row-go" data-activity-result="${escape(run.run_id)}" aria-label="${escape(label)}" title="查看结果">${icons.go}</button>` : "";
  }
  // A failure something later settled says why, once, instead of repeating the original error.
  const settledNote = run => run.attention_resolved && !run.owner_archived ? String(run.attention_note || "") : "";
  function rowMarkup(run, title = run.title || "工作台任务", now = null) {
    const fresh = unread(run), selected = viewer?.runId === run.run_id, level = now ? now.level : tone(run), spent = brief(seconds(run)), { head } = naming(run);
    return `<article class="activity-row${fresh ? " is-unread" : ""}${selected ? " is-selected" : ""}" data-run-id="${escape(run.run_id)}"><button type="button" class="activity-row-main" data-activity-log="${escape(run.run_id)}"${selected ? ' aria-current="true"' : ""}><span class="activity-dot is-${level}" aria-hidden="true"></span><span class="activity-row-text"><span class="activity-row-title">${escape(title)}</span><span class="activity-row-meta"><span class="activity-status is-${level}">${escape(now ? now.label : status(run))}</span><span>${escape(now ? now.note : settledNote(run) || `${head}：${run.message || phaseLabel(run.phase)}`)}</span></span></span><span class="activity-row-side"><time datetime="${escape(run.updated_at || "")}">${escape(when(run.updated_at))}</time>${spent ? `<small>${escape(spent)}</small>` : ""}</span>${fresh ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${goMarkup(run, title)}</article>`;
  }
  // One line per run inside an order: the step, its status and its last message. Identical runs in a row share one line.
  function runMarkup(run, repeat = null) {
    const fresh = repeat ? repeat.runs.some(unread) : unread(run), selected = viewer?.runId === run.run_id, level = tone(run), { head } = naming(run);
    return `<article class="activity-row activity-sub${fresh ? " is-unread" : ""}${selected ? " is-selected" : ""}" data-run-id="${escape(run.run_id)}"><button type="button" class="activity-row-main" data-activity-log="${escape(run.run_id)}"${selected ? ' aria-current="true"' : ""}><span class="activity-dot is-${level}" aria-hidden="true"></span><span class="activity-sub-text"><b>${escape(head)}</b><span class="activity-status is-${level}">${escape(status(run))}</span><span class="activity-sub-message">${escape(settledNote(run) || run.message || phaseLabel(run.phase))}</span></span><time class="activity-sub-time" datetime="${escape(run.updated_at || "")}">${escape(when(run.updated_at))}</time>${fresh ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${repeat ? `<button type="button" class="activity-sub-repeat" data-activity-unfold="${escape(repeat.key)}" title="展开这 ${repeat.runs.length} 次相同的运行">×${repeat.runs.length}</button>` : ""}${goMarkup(run, head)}</article>`;
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
  // An order the reader still has to act on says what to do next, whatever its last run did.
  function workFor(key, f = activeFilter()) { return (work.owners.get(key) || []).find(item => workKindOk(item, f)) || null; }
  function nowOf(group) {
    const need = workFor(group.key);
    if (!need || group.runs.some(run => activeStates.has(run.status))) return orderNow(group);
    const lead = group.runs[0];
    return { label: need.next_label || need.stage_label || "待处理", level: "warning", note: need.stage_label || `${naming(lead).head}：${lead.message || phaseLabel(lead.phase)}` };
  }
  const needsYou = (group, f = activeFilter()) => !running(group) && (group.open || !!workFor(group.key, f));
  function workRow(item) {
    const hash = typeof item.next_hash === "string" && item.next_hash.startsWith("#") ? item.next_hash : "";
    const href = hash ? window.WorkbenchShell?.url?.(hash, item.product) || hash : "";
    const title = [item.timeline_name, item.title].filter(Boolean).join(" · ") || "待处理内容";
    const body = `<span class="activity-dot is-warning" aria-hidden="true"></span><span class="activity-row-text"><span class="activity-row-title">${escape(title)}</span><span class="activity-row-meta"><span class="activity-status is-warning">${escape(item.next_label || "待处理")}</span><span>${escape(item.stage_label || item.subtitle || "")}</span></span></span><span class="activity-row-side"><time datetime="${escape(item.updated_at || "")}">${escape(when(item.updated_at))}</time></span>`;
    const finish = item.job_id && item.timeline_uid ? `<span class="activity-work-actions"><button type="button" class="activity-work-finish" data-activity-work-finish="${escape(item.id)}" aria-label="完结：${escape(title)}" title="不再提醒这条图文，做好的内容都保留">完结</button>${item.deletable ? `<button type="button" class="activity-work-finish is-danger" data-activity-work-delete="${escape(item.id)}" aria-label="删除：${escape(title)}" title="永久删除这条图文的本地数据，点了会先问你">删除</button>` : ""}</span>` : "";
    return `<article class="activity-row activity-work-row">${href ? `<a class="activity-row-main" href="${escape(href)}" data-activity-work>${body}</a>` : `<div class="activity-row-main">${body}</div>`}${finish}</article>`;
  }
  function finishedNote() {
    if (!justFinished) return "";
    const name = justFinished.timeline_name || justFinished.title || "这条图文";
    return `<article class="activity-finished-note"><span>已完结 ${escape(name)}，不再提醒</span><button type="button" class="subtle compact" data-activity-work-reopen>撤销</button></article>`;
  }
  // 完结 is one click because it destroys nothing; the note right after it is the way back.
  async function changeFinished(button, item, decision) {
    button.disabled = true;
    try {
      await api(`/api/production/jobs/${encodeURIComponent(item.job_id)}/photo-decision`, { method: "POST", body: { timeline: item.timeline_uid, decision } });
      justFinished = decision === "finish" ? item : null;
      await loadWork(true, true);
      paintPanel();
    } catch (error) { toast(error?.message || "操作暂未完成", true); button.disabled = false; }
  }
  function groupMarkup(group) {
    const now = nowOf(group);
    if (group.runs.length === 1) return rowMarkup(group.runs[0], group.label, now);
    const run = group.lead;
    const summary = group.runs.find(item => activeStates.has(item.status)) || group.runs.find(waiting) || group.runs[0] || run;
    const level = now ? now.level : tone(summary), expanded = panel.open.has(group.key), { head } = naming(summary);
    const selected = !expanded && group.runs.some(item => item.run_id === viewer?.runId);
    return `<article class="activity-row activity-group-row${group.unread ? " is-unread" : ""}${selected ? " is-selected" : ""}"><button type="button" class="activity-row-main" data-activity-group="${escape(group.key)}" aria-expanded="${expanded}"><span class="activity-dot is-${level}" aria-hidden="true"></span><span class="activity-row-text"><span class="activity-row-title">${escape(group.label)}</span><span class="activity-row-meta"><span class="activity-status is-${level}">${escape(now ? now.label : status(summary))}</span><span>${escape(now ? now.note : `${head}：${summary.message || phaseLabel(summary.phase)}`)}</span></span></span><span class="activity-row-side"><time datetime="${escape(group.at || "")}">${escape(when(group.at))}</time><small class="activity-group-count">${group.runs.length} 次${icons.chevron}</small></span>${group.unread ? '<span class="activity-unread-dot" aria-label="未读"></span>' : ""}</button>${goMarkup(run, group.label)}</article>${expanded ? `<div class="activity-group-runs" role="group" aria-label="${escape(group.label)}">${runsMarkup(group)}</div>` : ""}`;
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
  // A run belongs to every day it was going on: from its start until it ended, or until now while it still runs.
  const runStart = run => parse(run.started_at) || parse(run.updated_at);
  const runEnd = run => activeStates.has(run.status) ? new Date() : parse(run.finished_at) || parse(run.updated_at) || runStart(run);
  function onDay(run, day) { const start = runStart(run), end = runEnd(run); return !!start && start < shift(day, 1) && end >= day; }
  const dayGroups = (day, f = activeFilter()) => picked(panel.items.filter(run => onDay(run, day)), f);
  // What still waits on the reader stays pinned below whichever day is open, so nothing unfinished hides on an older page:
  // the orders and drafts the home page lists as 待我处理 (plus 待发布 and 图文), then failed or stuck runs none of those cover.
  function pendingGroups(shown, f = activeFilter()) {
    const listed = new Set(shown.map(group => group.key));
    return picked(panel.pending.filter(run => !run.owner_archived || activeStates.has(run.status)), f).filter(group => group.open && !listed.has(group.key) && !workFor(group.key, f));
  }
  function pendingWork(shown, f = activeFilter()) {
    const listed = new Set(shown.map(group => group.key));
    return work.items.filter(item => workOk(item, f) && !(item.keys || []).some(key => listed.has(key)));
  }
  function listedGroups() {
    if (!calendar()) return picked(panel.items, activeFilter());
    const day = dayGroups(panel.day);
    return [...day, ...pendingGroups(day)];
  }
  const weekdays = ["日", "一", "二", "三", "四", "五", "六"];
  const dayName = day => `${day.getMonth() + 1} 月 ${day.getDate()} 日 周${weekdays[day.getDay()]}`;
  // Each day shows how many orders ran that day, a yellow dot when one still waits on the reader and a blue one while one runs.
  function dayStates(groups) {
    return { count: groups.length, needs: groups.filter(group => needsYou(group)).length, running: groups.filter(running).length };
  }
  function paintWeek() {
    const host = dialog.querySelector("#activity-week");
    if (!calendar()) { host.hidden = true; return; }
    host.hidden = false;
    const today = midnight(), end = shift(panel.week, 6), current = panel.week.getTime() === weekOf(today).getTime();
    const range = panel.week.getMonth() === end.getMonth() ? `${panel.week.getMonth() + 1} 月 ${panel.week.getDate()}–${end.getDate()} 日` : `${panel.week.getMonth() + 1}/${panel.week.getDate()} – ${end.getMonth() + 1}/${end.getDate()}`;
    const days = Array.from({ length: 7 }, (_, index) => shift(panel.week, index)).map(day => {
      const future = day > today, chosen = day.getTime() === panel.day.getTime(), state = panel.loading && !panel.items.length ? { count: 0, needs: 0, running: 0 } : dayStates(dayGroups(day));
      const label = `${dayName(day)}，${state.count ? `${state.count} 项任务${state.needs ? `，${state.needs} 项等你处理` : ""}${state.running ? `，${state.running} 项进行中` : ""}` : "没有任务"}`;
      const marks = `${state.needs ? '<i class="is-warning"></i>' : ""}${state.running ? '<i class="is-active"></i>' : ""}`;
      return `<button type="button" data-activity-date="${day.getTime()}" aria-pressed="${chosen}" aria-label="${escape(label)}" title="${escape(label)}"${future ? " disabled" : ""}${day.getTime() === today.getTime() ? ' class="is-today"' : ""}><small>${day.getTime() === today.getTime() ? "今天" : weekdays[day.getDay()]}</small><b>${day.getDate()}</b><span class="activity-week-count">${state.count ? `${state.count} 项` : ""}${marks}</span></button>`;
    }).join("");
    host.innerHTML = `<div class="activity-week-head"><button type="button" class="subtle activity-icon" data-activity-week="-1" aria-label="上一周" title="上一周">${icons.back}</button><span class="activity-week-label">${escape(range)}</span><button type="button" class="subtle activity-icon" data-activity-week="1" aria-label="下一周" title="下一周"${current ? " disabled" : ""}>${icons.chevron}</button>${panel.day.getTime() === today.getTime() ? "" : '<button type="button" class="subtle activity-week-today" data-activity-today>回到今天</button>'}<div class="activity-filters activity-view-switch" role="group" aria-label="当天显示方式">${[["list", "清单"], ["timeline", "时间轴"]].map(([value, title]) => `<button type="button" data-activity-view="${value}" aria-pressed="${panel.view === value}">${title}</button>`).join("")}</div></div><div class="activity-week-days" role="group" aria-label="选择日期">${days}</div>`;
  }
  // How many orders and to-dos each filter would list for the open page (the day plus what still waits on the reader).
  function filterCount(f) {
    if (!calendar()) return picked(panel.items, f).length;
    const shown = dayGroups(panel.day, f);
    return shown.length + pendingGroups(shown, f).length + pendingWork(shown, f).length;
  }
  function chipsMarkup() {
    const current = activeFilter(), editing = chipEdit.on;
    const chips = filterList().map(item => {
      const count = panel.loading && !panel.items.length ? 0 : filterCount(item), custom = item.id.startsWith("c-");
      const remove = editing && item.id !== "all" ? `<button type="button" class="activity-chip-remove" data-chip-remove="${escape(item.id)}" aria-label="${custom ? "删除" : "隐藏"}「${escape(item.label)}」" title="${custom ? "删除" : "隐藏"}">${icons.close}</button>` : "";
      const action = editing && custom ? `data-chip-open="${escape(item.id)}" title="修改这个筛选"` : `data-activity-filter="${escape(item.id)}" aria-pressed="${item.id === current.id}"`;
      return `<span class="activity-chip${custom ? " is-custom" : ""}${item.id === current.id ? " is-on" : ""}${count ? "" : " is-empty"}"><button type="button" ${action}>${escape(item.label)}${count ? `<small>${count}</small>` : ""}</button>${remove}</span>`;
    }).join("");
    const tail = editing
      ? `<button type="button" class="activity-chip-add" data-chip-new>${icons.plus}新建筛选</button>${prefs.hidden.length ? '<button type="button" class="subtle activity-chip-link" data-chip-reset>恢复默认按键</button>' : ""}<button type="button" class="subtle activity-chip-link" data-chip-done>完成</button>`
      : `<button type="button" class="subtle activity-icon activity-chip-tune" data-chip-edit aria-label="自定义筛选" title="自定义筛选">${icons.tune}</button>`;
    return `<div class="activity-chip-row${editing ? " is-editing" : ""}" role="group" aria-label="筛选任务">${chips}${tail}</div>${chipEdit.draft ? editorMarkup(chipEdit.draft) : ""}`;
  }
  function editorMarkup(draft) {
    const toggles = (options, chosen, name) => options.map(([key, label]) => `<button type="button" data-chip-${name}="${key}" aria-pressed="${chosen.includes(key)}">${label}</button>`).join("");
    return `<form class="activity-chip-editor" data-chip-form><div class="activity-chip-field"><span>名字</span><input name="label" maxlength="12" placeholder="不填就按条件取名" value="${escape(draft.label)}" aria-label="筛选名字"></div><div class="activity-chip-field"><span>类型</span><div class="activity-chip-toggles" role="group" aria-label="任务类型">${toggles(kindOptions, draft.kinds, "kind")}</div></div><div class="activity-chip-field"><span>状态</span><div class="activity-chip-toggles" role="group" aria-label="任务状态">${toggles(stateOptions, draft.states, "state")}</div></div><div class="activity-chip-field"><span>标题含</span><input name="q" maxlength="40" placeholder="可不填，如 202H、Lab-1" value="${escape(draft.q)}" aria-label="标题含"></div><p class="activity-chip-hint">同一行选多个是「任一」，类型、状态、标题之间是「同时满足」。</p><div class="activity-chip-actions">${draft.id ? `<button type="button" class="subtle danger" data-chip-remove="${escape(draft.id)}">删除</button>` : ""}<button type="button" class="subtle" data-chip-cancel>取消</button><button type="submit" class="primary compact">保存</button></div></form>`;
  }
  function paintChips() {
    const host = dialog.querySelector("#activity-chips");
    host.hidden = !!panel.scope;
    if (panel.scope) return;
    const form = host.querySelector("[data-chip-form]"), keep = form && form.contains(document.activeElement) ? document.activeElement.name : "";
    if (form && chipEdit.draft) { chipEdit.draft.label = form.elements.label.value; chipEdit.draft.q = form.elements.q.value; }
    host.innerHTML = chipsMarkup();
    if (keep) host.querySelector(`[data-chip-form] [name="${keep}"]`)?.focus();
  }
  function chooseFilter(key) {
    prefs.filter = key; savePrefs();
    viewer = viewer?.explicit ? viewer : null;
    paintPanel();
  }
  function saveDraft() {
    const draft = chipEdit.draft, form = dialog.querySelector("[data-chip-form]");
    draft.label = form.elements.label.value.trim(); draft.q = form.elements.q.value.trim();
    if (!draft.kinds.length && !draft.states.length && !draft.q) { form.querySelector(".activity-chip-hint").textContent = "至少选一个类型或状态，或者填标题含。"; form.querySelector(".activity-chip-hint").classList.add("is-error"); return; }
    const name = (options, keys) => options.filter(([value]) => keys.includes(value)).map(([, title]) => title).join("/");
    const label = (draft.label || [name(kindOptions, draft.kinds), name(stateOptions, draft.states), draft.q].filter(Boolean).join("·")).slice(0, 12);
    const order = (options, keys) => options.map(([value]) => value).filter(value => keys.includes(value));
    const item = { id: draft.id || `c-${Date.now().toString(36)}`, label, kinds: order(kindOptions, draft.kinds), states: order(stateOptions, draft.states), q: draft.q.slice(0, 40) };
    const index = prefs.custom.findIndex(row => row.id === item.id);
    if (index >= 0) prefs.custom[index] = item; else prefs.custom.push(item);
    prefs.filter = item.id; chipEdit.draft = null;
    savePrefs(); paintPanel();
  }
  function onChip(target) {
    const pick = target.closest("[data-activity-filter]");
    if (pick) { chooseFilter(pick.dataset.activityFilter); return true; }
    if (target.closest("[data-chip-edit]")) { chipEdit = { on: true, draft: null }; paintChips(); return true; }
    if (target.closest("[data-chip-done]")) { chipEdit = { on: false, draft: null }; paintChips(); return true; }
    if (target.closest("[data-chip-new]")) { chipEdit.draft = { id: "", label: "", kinds: [], states: [], q: "" }; paintChips(); dialog.querySelector('[data-chip-form] [name="label"]')?.focus(); return true; }
    const edit = target.closest("[data-chip-open]");
    if (edit) { const item = prefs.custom.find(row => row.id === edit.dataset.chipOpen); if (item) chipEdit.draft = { ...item, kinds: [...item.kinds], states: [...item.states] }; paintChips(); return true; }
    if (target.closest("[data-chip-cancel]")) { chipEdit.draft = null; paintChips(); return true; }
    if (target.closest("[data-chip-reset]")) { prefs.hidden = []; savePrefs(); paintPanel(); return true; }
    const remove = target.closest("[data-chip-remove]");
    if (remove) {
      const key = remove.dataset.chipRemove;
      if (key.startsWith("c-")) prefs.custom = prefs.custom.filter(row => row.id !== key); else prefs.hidden = [...new Set([...prefs.hidden, key])];
      if (chipEdit.draft?.id === key) chipEdit.draft = null;
      if (prefs.filter === key) prefs.filter = "all";
      savePrefs(); paintPanel(); return true;
    }
    const toggle = target.closest("[data-chip-kind], [data-chip-state]");
    if (toggle && chipEdit.draft) {
      const [field, key] = toggle.dataset.chipKind ? ["kinds", toggle.dataset.chipKind] : ["states", toggle.dataset.chipState];
      const list = chipEdit.draft[field];
      chipEdit.draft[field] = list.includes(key) ? list.filter(value => value !== key) : [...list, key];
      toggle.setAttribute("aria-pressed", String(chipEdit.draft[field].includes(key)));
      return true;
    }
    return false;
  }
  // The timeline lays the open day's runs out by clock time. Each start gets its own slot in time order, hours with
  // nothing running fold into one thin row, and only runs that truly overlap sit side by side.
  function timelineMarkup(day) {
    const start = +day, end = +shift(day, 1), now = Date.now(), hour = 3600000, scale = 44, gapHeight = 20, least = 22, slot = least + 2;
    // Back-to-back steps of one order (配音, then 建线, then 剪辑) become one block; a pause over five minutes starts a new one.
    const f = activeFilter(), kept = new Set(dayGroups(day, f).map(group => group.key));
    const pieces = panel.items.filter(run => onDay(run, day) && runOk(run, f) && kept.has(run.owner_id || run.run_id)).map(run => ({ run, from: Math.max(+runStart(run), start), to: Math.min(+runEnd(run), end) })).sort((a, b) => a.from - b.from);
    const chains = new Map(), runs = [];
    for (const piece of pieces) {
      const key = piece.run.owner_id || piece.run.run_id, chain = chains.get(key);
      if (chain && piece.from <= chain.to + 300000) { chain.to = Math.max(chain.to, piece.to); chain.steps.push(piece.run); continue; }
      const next = { from: piece.from, to: piece.to, steps: [piece.run] };
      chains.set(key, next); runs.push(next);
    }
    if (!runs.length) return "";
    runs.sort((a, b) => a.from - b.from || b.to - a.to);
    for (const item of runs) item.run = item.steps.find(run => activeStates.has(run.status)) || item.steps.find(waiting) || item.steps.at(-1);
    const today = now >= start && now < end, busy = new Set(), hourOf = time => Math.min(23, Math.floor((time - start) / hour));
    for (const { from, to } of runs) for (let h = hourOf(from); h <= hourOf(Math.max(to - 1, from)); h += 1) busy.add(h);
    if (today) busy.add(hourOf(now));
    // Time maps to height through knots: every hour edge and every distinct start time.
    const first = Math.min(...busy), last = Math.max(...busy) + 1, rows = [], knots = [];
    const knot = (time, y) => { const prev = knots.at(-1); if (prev && time <= prev[0]) prev[1] = Math.max(prev[1], y); else knots.push([time, y]); };
    let y = 0;
    for (let h = first; h < last;) {
      const from = start + h * hour;
      if (!busy.has(h)) {
        let to = h; while (to < last && !busy.has(to)) to += 1;
        if (to - h >= 2) { knot(from, y); rows.push(`<div class="activity-hour is-gap" data-top="${y}" data-height="${gapHeight}"><span>${pad(h)}–${pad(to)} 点没有任务</span></div>`); y += gapHeight; h = to; continue; }
      }
      const begins = [...new Set(runs.map(item => item.from).filter(time => time >= from && time < from + hour))];
      knot(from, y);
      begins.forEach((time, index) => knot(time, y + 2 + index * slot));
      rows.push(`<div class="activity-hour" data-top="${y}"><span>${pad(h)}:00</span></div>`);
      y += Math.max(scale, begins.length * slot + 4); h += 1;
    }
    knot(start + last * hour, y);
    const place = time => {
      let index = knots.findIndex(([at]) => at > time);
      if (index <= 0) return index === 0 ? knots[0][1] : y;
      const [t0, y0] = knots[index - 1], [t1, y1] = knots[index];
      return y0 + (time - t0) / (t1 - t0) * (y1 - y0);
    };
    for (const item of runs) { item.top = place(item.from); item.height = Math.max(least, place(item.to) - item.top); }
    // Lanes are counted per cluster of overlapping blocks, so one crowded moment does not narrow the whole day.
    for (let index = 0; index < runs.length;) {
      let bottom = runs[index].top + runs[index].height, next = index + 1;
      while (next < runs.length && runs[next].top < bottom - 0.5) { bottom = Math.max(bottom, runs[next].top + runs[next].height); next += 1; }
      const cluster = runs.slice(index, next), lanes = [];
      for (const item of cluster) {
        item.lane = lanes.findIndex(edge => edge <= item.top + 0.5);
        if (item.lane < 0) { item.lane = lanes.length; lanes.push(0); }
        lanes[item.lane] = item.top + item.height;
      }
      for (const item of cluster) item.lanes = lanes.length;
      index = next;
    }
    y = Math.max(y, ...runs.map(item => item.top + item.height));
    const time = value => clock(new Date(value).toISOString()).slice(0, 5);
    const blocks = runs.map(({ run, steps, from, to, top, height, lane, lanes }) => {
      const { label } = naming(run), level = tone(run), selected = steps.some(step => step.run_id === viewer?.runId), span = from === to || time(from) === time(to) ? time(from) : `${time(from)}–${activeStates.has(run.status) && to >= now ? "现在" : time(to)}`;
      // The order's own code leads its title; a generic lead such as 「1:1 复刻」 takes the next part too.
      const parts = label.split(/\s*[|｜]\s*/).filter(Boolean), short = (parts[0]?.length < 8 && parts[1] ? `${parts[0]}｜${parts[1]}` : parts[0]) || label, heads = [...new Set(steps.map(step => naming(step).head))];
      const path = heads.length > 3 ? `${heads[0]} → … → ${heads.at(-1)}（${steps.length} 步）` : heads.join(" → ");
      const detail = [label, ...steps.map(step => `${time(Math.max(+runStart(step), start))} ${naming(step).head} · ${status(step)}`)].join("\n");
      return `<button type="button" class="activity-block is-${level}${selected ? " is-selected" : ""}${steps.some(unread) ? " is-unread" : ""}" data-activity-log="${escape(run.run_id)}" data-run-id="${escape(run.run_id)}" data-top="${top.toFixed(1)}" data-height="${height.toFixed(1)}" data-lane="${lane}" data-lanes="${lanes}" title="${escape(detail)}"${selected ? ' aria-current="true"' : ""}><b>${escape(short)}</b><span>${escape(path)}</span><small>${escape(span)}</small></button>`;
    }).join("");
    const line = today ? `<div class="activity-now" data-top="${place(now).toFixed(1)}" aria-hidden="true"></div>` : "";
    return `<div class="activity-timeline" data-height="${y.toFixed(1)}">${rows.join("")}<div class="activity-lanes">${blocks}${line}</div></div>`;
  }
  // The page's security policy refuses inline style attributes, so positions are applied through the DOM.
  function placeTimeline(list) {
    for (const node of list.querySelectorAll(".activity-timeline [data-top], .activity-timeline[data-height]")) {
      if (node.dataset.top) node.style.top = `${node.dataset.top}px`;
      if (node.dataset.height) node.style.height = `${node.dataset.height}px`;
      if (node.dataset.lanes) { const lanes = Number(node.dataset.lanes); node.style.left = `calc(${node.dataset.lane} * 100% / ${lanes})`; node.style.width = `calc(100% / ${lanes} - 3px)`; }
    }
  }
  function dayMarkup() {
    const shown = dayGroups(panel.day), pending = pendingGroups(shown), waitingWork = pendingWork(shown), count = shown.length, total = pending.length + waitingWork.length;
    let html = `<h3 class="activity-section">${escape(dayName(panel.day))}<small>${count ? `${count} 项` : ""}</small></h3>`;
    if (panel.autoNote) html += `<p class="activity-day-note">今天还没有任务，先显示最近有任务的一天。</p>`;
    const f = activeFilter();
    if (!count) html += `<p class="activity-empty">${panel.loading ? "正在读取任务…" : f.id === "all" ? "这天没有任务" : `这天没有「${escape(f.label)}」的任务`}</p>`;
    else html += panel.view === "timeline" ? timelineMarkup(panel.day) : shown.map(groupMarkup).join("");
    if (total) html += `<h3 class="activity-section is-pending">等你处理<small>${total} 项</small></h3>${waitingWork.map(workRow).join("")}${pending.map(groupMarkup).join("")}`;
    html += finishedNote();
    return { html, shown: [...shown, ...pending] };
  }
  function paintList() {
    const list = dialog.querySelector("#activity-list");
    const focusKey = ["data-activity-log", "data-activity-result", "data-activity-group", "data-activity-day"].find(name => list.contains(document.activeElement) && document.activeElement.hasAttribute(name));
    const focusValue = focusKey && document.activeElement.getAttribute(focusKey);
    const groups = listedGroups();
    // A run opened from outside (a notice, a strip) shows inside its order once that order is listed.
    const reveal = viewer && !viewer.revealed && groups.find(group => group.runs.some(run => run.run_id === viewer.runId));
    if (reveal) { viewer.revealed = true; if (reveal.lead.run_id !== viewer.runId) panel.open.add(reveal.key); }
    let html, shown = groups;
    if (panel.scope) html = dayed([...panel.items].sort(byUpdate), run => run.updated_at, runMarkup);
    else if (!calendar()) html = dayed(groups, group => group.at, groupMarkup);
    else ({ html, shown } = dayMarkup());
    list.innerHTML = html || `<p class="activity-empty">${panel.loading ? "正在读取任务…" : panel.query ? "没有符合条件的任务" : "暂无运行记录"}</p>`;
    placeTimeline(list);
    if (focusKey) [...list.querySelectorAll(`[${focusKey}]`)].find(node => node.getAttribute(focusKey) === focusValue)?.focus({ preventScroll: true });
    return shown;
  }
  function paintPanel() {
    dialog.classList.toggle("is-timeline", calendar() && panel.view === "timeline");
    paintChips();
    paintWeek();
    const shown = paintList();
    const more = dialog.querySelector("#activity-more"); more.hidden = !panel.hasMore; more.disabled = panel.loading; more.textContent = panel.loading ? "正在读取…" : "加载更多";
    paintReadAll();
    paintConnection();
    // Opening the panel previews what still needs someone before what has finished.
    const first = panel.scope ? [...panel.items].sort(byUpdate)[0] : (shown.find(group => needsYou(group)) || shown.find(running) || shown[0])?.lead;
    if (!viewer && first && !panel.loading) select(first.run_id, false);
    else if (!viewer) paintDetail();
  }
  // Read through the content views, not the run log; it is a reading aid, so a failure keeps what was shown before.
  // 待我处理 and 待发布 come from the content list, 图文 from the 图文 overview, and stuck failures from 需要你出手.
  let justFinished = null;
  const photoSteps = { want: ["开始做图文", "图文：决定要做"], doing: ["继续做图文", "图文：做到一半"], exported: ["上传图文到 Notion", "图文：已导出，还没上传"] };
  function loadWork(force, restart = false) {
    const product = panel.product;
    if (!restart && workRequest?.product === product) return workRequest.promise;
    if (!force && work.product === product && Date.now() - work.at < 30000) return Promise.resolve();
    const sequence = ++workSequence, request = { product };
    request.promise = readWork(product, sequence).finally(() => { if (workRequest === request) workRequest = null; });
    workRequest = request;
    return request.promise;
  }
  async function readWork(product, sequence) {
    const scoped = query => `${new URLSearchParams({ ...query, ...(product ? { product } : {}) })}`;
    const [attention, publish, photos, stuck] = await Promise.all([
      ...["attention", "publish"].map(view => api(`/api/content?${scoped({ view, page_size: "50" })}`).catch(() => null)),
      api(`/api/photo-overview?${scoped({})}`).catch(() => null), api(`/api/needs-you?${scoped({})}`).catch(() => null)]);
    if (sequence !== workSequence || panel.product !== product || ![attention, publish, photos, stuck].some(Boolean)) return;
    const items = [], owners = new Map(), seen = new Set();
    const add = item => {
      if (!item?.id || item.archived || seen.has(item.id)) return;
      seen.add(item.id);
      // A 图文 to-do names one timeline, so it keeps its own row instead of borrowing an order row titled by another timeline.
      item.keys = item.kinds.includes("photo") ? [] : [...new Set([...(item.production_ids || []), ...(item.task_ids || []), String(item.id).split(":").slice(1).join(":")].filter(Boolean))];
      items.push(item);
      for (const key of item.keys) owners.set(key, [...(owners.get(key) || []), item]);
    };
    for (const data of [attention, publish]) for (const item of data?.items || []) add({ ...item, kinds: String(item.id || "").startsWith("video:") ? videoKinds : ["ai"] });
    // Posted but still 未上传 on the publish calendar (oldest post first); the row opens the publish step where the mark is set.
    const unposted = attention?.calendar_pending, posted = unposted?.items || [];
    for (const item of posted) add({ ...item, id: `calendar:${item.id}`, next_label: "标记已上传发布日历", stage_label: [item.published_on ? `发布于 ${item.published_on}` : "", "还没上传到发布日历"].filter(Boolean).join(" · "), kinds: videoKinds });
    if (unposted?.total > posted.length) add({ id: "calendar:more", title: `另有 ${unposted.total - posted.length} 条已发布、还没上传到发布日历`, next_label: "去内容进程查看", stage_label: "内容进程 · 全部", next_hash: "#content?view=all", product, kinds: videoKinds });
    for (const row of stuck?.items || []) add({ id: `stuck:${row.kind}:${row.job_id}:${row.timeline_uid}`, title: row.job_title, timeline_name: row.timeline_name, product: row.product,
      next_label: row.kind_label, stage_label: row.message, next_hash: row.hash, updated_at: row.since, production_ids: [row.job_id], kinds: row.kind === "carousel" ? ["photo"] : videoKinds, ...(row.kind === "carousel" ? { job_id: row.job_id, timeline_uid: row.timeline_uid, deletable: true } : {}) });
    for (const row of photos?.items || []) {
      const step = photoSteps[row.state];
      if (step) add({ id: `photo:${row.job_id}:${row.timeline_uid}`, title: row.job_title, timeline_name: row.timeline_name, product: row.product, next_label: step[0], stage_label: step[1],
        next_hash: `#production/${encodeURIComponent(row.job_id)}?step=publish`, updated_at: row.since, production_ids: [row.job_id], kinds: ["photo"], job_id: row.job_id, timeline_uid: row.timeline_uid, deletable: row.state !== "want" });
    }
    // Finished videos nobody has decided on yet are one line under the 图文 filter, not one row each.
    const undecided = Number(photos?.counts?.pending) || 0;
    if (undecided) add({ id: "photo:undecided", digest: true, title: `${undecided} 条成片还没决定做不做图文`, next_label: "去决定", stage_label: "内容进程 · 图文", next_hash: "#content?view=carousel", product, kinds: ["photo"] });
    work = { items, owners, product, at: Date.now() };
    if (dialog?.open && calendar() && !panel.loading) paintPanel();
  }
  async function loadPanel(append = false, quiet = false) {
    const sequence = ++panelSequence, state = panel, days = calendar();
    const current = () => sequence === panelSequence && panel === state && dialog.open;
    if (!append && !quiet && viewer && !viewer.explicit) viewer = null;
    panel.loading = true; panel.error = "";
    if (!quiet) paintPanel();
    // The calendar reads one week of orders at a time plus whatever is still open; search and an order's history page by whole orders.
    const params = new URLSearchParams({ limit: days ? "200" : "50", filter: days ? "range" : "all", group: "owner" });
    if (days) { params.set("since", panel.week.toISOString()); params.set("until", shift(panel.week, 7).toISOString()); }
    if (panel.product) params.set("product", panel.product);
    if (!days && panel.query.trim()) params.set("q", panel.query.trim());
    if (panel.scope?.owner_id) params.set("owner_id", panel.scope.owner_id);
    if (panel.scope?.source_id) params.set("source_id", panel.scope.source_id);
    if (append && panel.before !== null) params.set("before", String(panel.before));
    try {
      const readPage = async () => {
        let data = await request(`/api/activity?${params}`);
        if (!current()) return null;
        // Opened on an empty today: jump once to the latest day that has runs, reading that week if it is an earlier one.
        if (days && state.autoDay && !append && !quiet) {
          const day = state.day.getTime();
          const today = (data.items || []).map(normalizeRun).some(run => onDay(run, state.day));
          if (!today) {
            const query = new URLSearchParams({ limit: "1", filter: "all" });
            if (state.product) query.set("product", state.product);
            const latest = (await request(`/api/activity?${query}`))?.items?.[0];
            if (!current()) return null;
            const target = latest ? midnight(runEnd(normalizeRun(latest))) : null;
            if (state.autoDay && state.day.getTime() === day && target && target < state.day) {
              state.day = target; state.autoNote = true;
              if (weekOf(target).getTime() !== state.week.getTime()) {
                state.week = weekOf(target);
                params.set("since", state.week.toISOString()); params.set("until", shift(state.week, 7).toISOString());
                data = await request(`/api/activity?${params}`);
              }
            }
          }
          if (current()) state.autoDay = false;
        }
        return data;
      };
      const pageRead = readPage();
      let openRead = Promise.resolve(null);
      if (days) {
        const query = new URLSearchParams({ limit: "50", filter: "current", group: "owner" });
        if (state.product) query.set("product", state.product);
        openRead = request(`/api/activity?${query}`);
      }
      // Start the run-list reads first, then overlap the slower supporting content views.
      if (days) void loadWork(!quiet);
      const [data, open] = await Promise.all([pageRead, openRead]);
      if (!current()) return;
      if (data?.available === false || open?.available === false) throw new Error("运行日志暂时不可用；已有任务仍可继续执行。");
      const items = (data.items || []).map(normalizeRun), pending = (open?.items || []).map(normalizeRun);
      remember(items); remember(pending);
      for (const source of [data, open]) for (const [owner, state] of Object.entries(source?.owner_states || {})) ownerStates.set(owner, state);
      // A background refresh of a search keeps older pages already loaded by the reader.
      const old = append ? panel.items : quiet && !days ? panel.items.filter(row => !items.some(next => next.run_id === row.run_id) && known.has(row.run_id)) : [];
      panel.items = append ? [...old, ...items.filter(item => !old.some(row => row.run_id === item.run_id))] : [...items, ...old].map(item => known.get(item.run_id) || item);
      panel.pending = pending.map(item => known.get(item.run_id) || item);
      if (days) { panel.hasMore = false; panel.before = null; }
      else if (!quiet || !old.length) { panel.hasMore = !!data.has_more; panel.before = data.next_before ?? null; }
      const counted = open?.counts || data.counts;
      if (counted) counts = readCounts(counted);
      paintBadge();
    } catch (error) { if (sequence === panelSequence) panel.error = error.status === 400 ? "日历视图需要重启工作台服务后才能使用。" : error.message || "任务记录暂时无法读取。"; }
    finally { if (sequence === panelSequence) { panel.loading = false; if (dialog.open) paintPanel(); } }
  }

  // The selected run's log: newest lines first loaded, grouped by phase, repeats folded.
  function select(runId, explicit = true) {
    if (!runId || !dialog) return;
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
      if (result.run) { remember([result.run]); state.run = known.get(result.run.run_id); }
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
      host.innerHTML = `<div class="activity-detail-head"></div><section class="activity-needs" hidden></section><div class="activity-log-tools"><button type="button" data-log-mode title="默认只显示状态变化、提醒和 AI 说明"></button></div><div class="activity-log-body" id="activity-log-body" tabindex="0" aria-label="运行日志"></div>`;
      const body = host.querySelector(".activity-log-body");
      body.addEventListener("scroll", () => {
        if (!viewer) return;
        viewer.stick = body.scrollHeight - body.scrollTop - body.clientHeight < 24;
        if (viewer.stick && viewer.unseen) { viewer.unseen = 0; body.querySelector("[data-log-jump]")?.setAttribute("hidden", ""); }
      });
    }
    paintHead(); paintBody("follow");
  }
  function paintMode() {
    const button = dialog?.querySelector("[data-log-mode]");
    if (!button || !viewer) return;
    const full = viewer.mode === "all";
    button.textContent = full ? "只看关键节点" : `看完整日志${viewer.loaded ? `（${viewer.rows.length}${viewer.earlier ? "+" : ""} 行）` : ""}`;
    button.setAttribute("aria-pressed", String(full));
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
    paintMode();
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
      const visible = !filtered ? group.rows : group.rows.filter(row => row.warn || row.key || row.ai || row === group.rows[0]);
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
      dialog.querySelector('[data-activity-date][aria-pressed="true"]')?.focus({ preventScroll: true });
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
  // 全部已读 only appears while something is unread, with the count on its corner.
  function paintReadAll() {
    const button = dialog?.querySelector("#activity-read-all");
    if (!button) return;
    button.hidden = !counts.unread;
    button.querySelector("small").textContent = counts.unread > 99 ? "99+" : String(counts.unread || "");
    button.title = `全部标为已读（${counts.unread} 条未读）`;
  }
  function paintPin() {
    const button = dialog.querySelector("[data-activity-pin]");
    button.setAttribute("aria-pressed", String(pinned));
    button.setAttribute("aria-label", pinned ? "取消置顶任务面板" : "置顶任务面板");
    button.title = pinned ? "取消置顶，恢复点击外部收起" : "置顶，点击外部不收起";
  }
  function togglePinned() {
    pinned = !pinned;
    storage.set(keys.pinned, pinned ? "1" : "0");
    paintPin();
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
    const detail = [activeStates.has(run.status) && phaseLabel(run.phase) !== run.message ? phaseLabel(run.phase) : "", run.message].filter(Boolean).join(" · ");
    return `<span class="activity-dot is-${level}" aria-hidden="true"></span><span class="activity-strip-status is-${level}">${escape(status(run))}</span><span class="activity-strip-text">${escape(detail)}</span>${spent ? `<span class="activity-strip-time">${escape(spent)}</span>` : ""}<span class="activity-strip-action">日志${runs.length > 1 ? ` · ${runs.length} 次` : ""}${icons.chevron}</span>`;
  }
  // Only runs that still need someone are named; finished history stays behind 全部日志.
  function ownerStrip(runs, label) {
    const active = runs.filter(run => activeStates.has(run.status));
    const running = active.filter(run => run.status !== "queued"), queued = active.filter(run => run.status === "queued");
    const attention = runs.filter(run => !activeStates.has(run.status) && waiting(run));
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
    return '<section class="section-card settings-card activity-settings" aria-labelledby="settings-activity-heading"><div class="settings-card-heading"><h2 id="settings-activity-heading">任务完成提醒</h2></div><p class="settings-note">任务完成、失败或需要你处理时，页面右下角弹出提醒并响一声（完成、出错、待处理三种声音）。开启桌面提醒后，切到别的窗口时也会弹系统通知。网页要保持打开；声音在页面上点过一下之后才能播放。</p><p class="settings-note" id="settings-activity-status" role="status" aria-live="polite"></p><div class="settings-actions"><button class="secondary compact" id="settings-activity-enable" type="button">开启桌面提醒</button><button class="subtle" id="settings-activity-disable" type="button" hidden>关闭桌面提醒</button><label class="activity-sound-toggle" id="settings-activity-stay-row" hidden><input type="checkbox" id="settings-activity-stay"> 一直留在桌面，点掉才消失</label><button class="subtle" id="settings-activity-desktop-try" type="button" hidden>发一条试试</button></div><div class="settings-actions activity-sound-row"><label class="activity-sound-toggle"><input type="checkbox" id="settings-activity-sound"> 提醒声音</label><button class="subtle" id="settings-activity-sound-try" type="button">试听</button></div></section>';
  }
  function mountSettings(panel) {
    if (!enabled()) return;
    const button = panel.querySelector("#settings-activity-enable"), off = panel.querySelector("#settings-activity-disable"), text = panel.querySelector("#settings-activity-status");
    if (!button || !off || !text) return;
    const paint = () => {
      const support = "Notification" in window, permission = support ? Notification.permission : "unsupported", on = storage.get(keys.desktop) === "enabled" && permission === "granted";
      button.hidden = on; off.hidden = !on; button.disabled = !support || permission === "denied";
      const stayRow = panel.querySelector("#settings-activity-stay-row"), stay = panel.querySelector("#settings-activity-stay");
      const tryDesktop = panel.querySelector("#settings-activity-desktop-try");
      if (tryDesktop) {
        tryDesktop.hidden = !on;
        // The browser cannot tell whether Windows actually showed it, so say where to look when nothing appears.
        tryDesktop.onclick = () => {
          try {
            const sample = new Notification("已完成 · 桌面提醒试一下", { body: "任务结束时会这样提醒你。点一下或点关闭就会消失。", silent: true, requireInteraction: storage.get(keys.stay) !== "off", tag: "workbench-desktop-try" });
            sample.onclick = () => { window.focus(); sample.close(); };
            sample.onerror = () => { text.textContent = "浏览器没能发出通知。请在浏览器的网站权限里允许本站通知。"; };
            if (soundOn()) chime("done");
            text.textContent = "已发出一条试用通知。桌面右下角没看到的话：打开 Windows 设置 → 系统 → 通知，打开「通知」总开关，并允许 Google Chrome（或你用的浏览器）发通知；同时关掉「勿扰」。";
          } catch { text.textContent = "浏览器没能发出通知。请在浏览器的网站权限里允许本站通知。"; }
        };
      }
      if (stayRow && stay) { stayRow.hidden = !on; stay.checked = storage.get(keys.stay) !== "off"; stay.onchange = () => storage.set(keys.stay, stay.checked ? "on" : "off"); }
      text.textContent = !support ? "此浏览器不支持桌面提醒。站内提示和运行日志仍可使用。" : permission === "denied" ? "浏览器已阻止桌面提醒。可在浏览器的网站权限中允许通知；站内提示和日志仍可使用。" : on ? "桌面提醒已开启。切到别的窗口时弹系统通知；关闭网页期间结束的任务可在任务面板查看，不会补弹。" : "桌面提醒未开启。站内提示和运行日志已启用。";
    };
    button.onclick = async () => {
      button.disabled = true;
      try { const permission = await Notification.requestPermission(); if (permission === "granted") storage.set(keys.desktop, "enabled"); else storage.remove(keys.desktop); }
      catch { storage.remove(keys.desktop); }
      paint();
    };
    off.onclick = () => { storage.remove(keys.desktop); paint(); };
    const sound = panel.querySelector("#settings-activity-sound"), trial = panel.querySelector("#settings-activity-sound-try");
    if (sound && trial) {
      sound.checked = soundOn();
      sound.onchange = () => { storage.set(keys.sound, sound.checked ? "on" : "off"); if (sound.checked) chime("done"); };
      trial.onclick = () => { chime("done"); setTimeout(() => chime("failed"), 700); setTimeout(() => chime("warning"), 1400); };
    }
    paint();
  }

  function onClick(event) {
    const target = event.target;
    // Backdrop clicks target the modal dialog itself, so check the panel bounds.
    if (!pinned && target === dialog && dialog.matches(":modal")) { const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) { dialog.close(); return; } }
    // Moving a week keeps the weekday but never opens a day that has not happened yet.
    if (target.closest("#activity-chips") && onChip(target)) return;
    const week = target.closest("[data-activity-week]");
    if (week) { const today = midnight(); let next = shift(panel.day, 7 * Number(week.dataset.activityWeek)); if (next > today) next = today; moveTo(next); return; }
    if (target.closest("[data-activity-today]")) { moveTo(midnight()); return; }
    const date = target.closest("[data-activity-date]");
    if (date) { moveTo(new Date(Number(date.dataset.activityDate))); return; }
    const view = target.closest("[data-activity-view]");
    if (view) { panel.view = view.dataset.activityView; savePrefs(); paintPanel(); return; }
    // A to-do row is a plain link; an overlay steps aside for the page it opens.
    const finishWork = target.closest("[data-activity-work-finish]");
    if (finishWork) { const item = work.items.find(row => row.id === finishWork.dataset.activityWorkFinish); if (item) void changeFinished(finishWork, item, "finish"); return; }
    const deleteWork = target.closest("[data-activity-work-delete]");
    if (deleteWork) {
      const item = work.items.find(row => row.id === deleteWork.dataset.activityWorkDelete);
      if (item) void window.PhotoCarousel?.askDelete({ host: deleteWork.closest(".activity-work-row"), api, jobId: item.job_id, uid: item.timeline_uid, onDone: () => loadWork(true) });
      return;
    }
    const reopenWork = target.closest("[data-activity-work-reopen]");
    if (reopenWork && justFinished) { void changeFinished(reopenWork, justFinished, "reopen"); return; }
    if (target.closest("[data-activity-work]") && dialog.matches(":modal")) { dialog.close(); return; }
    const day = target.closest("[data-activity-day]");
    if (day) { const key = day.dataset.activityDay, collapsed = collapsedDays(); if (collapsed.has(key)) collapsed.delete(key); else collapsed.add(key); savePrefs(); paintList(); return; }
    // Opening an order lists its runs and shows the run that matters most; closing it keeps the log in view.
    const order = target.closest("[data-activity-group]");
    if (order) {
      const key = order.dataset.activityGroup, group = listedGroups().find(item => item.key === key);
      if (panel.open.has(key)) panel.open.delete(key); else panel.open.add(key);
      if (group && panel.open.has(key) && !group.runs.some(run => run.run_id === viewer?.runId)) select(group.lead.run_id, true);
      else paintList();
      return;
    }
    const unfold = target.closest("[data-activity-unfold]");
    if (unfold) { panel.unfold.add(unfold.dataset.activityUnfold); paintList(); return; }
    const log = target.closest("[data-activity-log]"), result = target.closest("[data-activity-result]");
    if (log) { select(log.dataset.activityLog, true); return; }
    if (result) { visit(known.get(result.dataset.activityResult) || { run_id: result.dataset.activityResult }); return; }
    if (target.closest("[data-activity-visit]") && viewer) { visit(known.get(viewer.runId) || viewer.run || { run_id: viewer.runId }); return; }
    if (target.closest("[data-activity-close]")) { dialog.close(); return; }
    if (target.closest("[data-activity-search]")) { toggleSearch(); return; }
    if (target.closest("[data-activity-pin]")) { togglePinned(); return; }
    if (target.closest("[data-activity-expand]")) { toggleExpanded(); return; }
    if (target.closest("[data-activity-unscope]")) { panel = blankPanel(); syncControls(); void loadPanel(); return; }
    if (!viewer) return;
    const mode = target.closest("[data-log-mode]");
    if (mode) { viewer.mode = lastMode = viewer.mode === "all" ? "key" : "all"; savePrefs(); paintMode(); paintBody("follow"); return; }
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
  // Another day of the same week is already loaded; another week is read from the service.
  function moveTo(day) {
    const sameWeek = weekOf(day).getTime() === panel.week.getTime();
    panel.day = midnight(day); panel.week = weekOf(panel.day); panel.autoDay = panel.autoNote = false;
    if (viewer && !viewer.explicit) viewer = null;
    if (sameWeek) { paintPanel(); dialog.querySelector(`[data-activity-date="${panel.day.getTime()}"]`)?.focus({ preventScroll: true }); }
    else void loadPanel().then(() => dialog.querySelector(`[data-activity-date="${panel.day.getTime()}"]`)?.focus({ preventScroll: true }));
  }
  // Search hides behind an icon; closing it drops the search so the list never stays filtered out of sight.
  function toggleSearch(force) {
    const open = force ?? dialog.querySelector(".activity-search-field").hidden;
    const had = !!panel.query;
    panel.searchOpen = open;
    if (!open) panel.query = "";
    syncControls(); savePrefs();
    if (open) dialog.querySelector("#activity-search").focus();
    else { dialog.querySelector("[data-activity-search]").focus(); if (had) void loadPanel(); }
  }
  function initialize() {
    if (!enabled()) { document.querySelector("#task-activity-open")?.setAttribute("hidden", ""); return; }
    if (initialized) return;
    initialized = true;
    browserId = storage.get(keys.browser) || id(); storage.set(keys.browser, browserId);
    dialog = document.createElement("dialog"); dialog.id = "task-activity-dialog"; dialog.className = "activity-dialog"; dialog.setAttribute("aria-labelledby", "activity-title");
    dialog.innerHTML = `<div class="activity-heading"><h2 id="activity-title">任务</h2><p id="activity-connection" class="activity-connection" role="status" hidden></p><div class="activity-heading-actions"><button type="button" class="subtle activity-icon activity-read-all" id="activity-read-all" aria-label="全部已读" hidden>${icons.readAll}<small aria-hidden="true"></small></button><button type="button" class="subtle activity-icon" data-activity-search aria-pressed="false" aria-label="搜索任务标题" title="搜索任务标题">${icons.search}</button><span class="activity-heading-divider" aria-hidden="true"></span><button type="button" class="subtle activity-icon" data-activity-pin aria-pressed="false">${icons.pin}</button><button type="button" class="subtle activity-icon" data-activity-expand aria-pressed="false"></button><button type="button" class="subtle activity-icon" data-activity-close aria-label="关闭任务面板" title="关闭">${icons.close}</button></div></div><div class="activity-layout"><section class="activity-runs" aria-label="任务列表"><div class="activity-controls"><div class="activity-chips" id="activity-chips"></div><div class="activity-week" id="activity-week"></div><div class="activity-search-field" hidden>${icons.search}<input type="search" id="activity-search" maxlength="100" placeholder="搜索任务标题" aria-label="搜索任务标题"></div><p class="activity-scope" id="activity-scope" hidden><span></span><button type="button" class="subtle" data-activity-unscope>显示全部任务</button></p></div><div class="activity-list" id="activity-list"></div><div class="activity-footer"><button type="button" class="subtle" id="activity-more" hidden>加载更多</button></div></section><section class="activity-detail" id="activity-detail" aria-label="运行日志"></section></div>`;
    notices = document.createElement("div"); notices.id = "task-activity-notices"; notices.className = "activity-notices"; notices.setAttribute("role", "region"); notices.setAttribute("aria-label", "任务提醒"); notices.setAttribute("aria-live", "polite");
    document.body.append(dialog, notices);
    paintExpand();
    paintPin();
    dialog.addEventListener("close", () => {
      // An outside log launcher can reopen the panel before this queued event fires.
      if (dialog.open) return;
      ++panelSequence; panel.loading = false; viewer = null;
      document.documentElement.classList.remove("activity-docked", "activity-expanded");
      previousFocus?.isConnected && previousFocus.focus();
    });
    // A docked panel is not modal, so Esc only closes it while focus is inside; elsewhere Esc keeps its page meaning.
    dialog.addEventListener("keydown", event => { if (event.key === "Escape" && !dialog.matches(":modal")) { event.preventDefault(); dialog.close(); } });
    dialog.querySelector("#activity-search").oninput = event => { panel.query = event.target.value; savePrefs(); syncControls(); clearTimeout(searchTimer); searchTimer = setTimeout(() => void loadPanel(), 250); };
    // Esc inside the search closes the search, not the panel.
    dialog.querySelector("#activity-search").addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); toggleSearch(false); } });
    dialog.querySelector("#activity-more").onclick = () => void loadPanel(true);
    dialog.querySelector("#activity-read-all").onclick = () => void markRead([], true);
    dialog.addEventListener("click", onClick);
    // Esc inside the filter form closes the form, not the panel.
    dialog.querySelector("#activity-chips").addEventListener("keydown", event => { if (event.key === "Escape" && chipEdit.draft) { event.preventDefault(); event.stopPropagation(); chipEdit.draft = null; paintChips(); dialog.querySelector("[data-chip-new]")?.focus(); } });
    dialog.addEventListener("submit", event => { if (event.target.matches("[data-chip-form]")) { event.preventDefault(); saveDraft(); } });
    document.addEventListener("click", event => {
      if (!dialog.open || pinned || dialog.matches(":modal") || dialog.contains(event.target) || event.target.closest("#task-activity-open, .activity-strip, .activity-notice-view")) return;
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
    // The list always shows the product picked in the top bar; reminders still cover every product.
    window.addEventListener("workbench:productchange", () => { if (!panel.scope) { panel.product = product(); work = { items: [], owners: new Map(), product: null, at: 0 }; if (dialog.open) void loadPanel(); } });
    window.addEventListener("workbench:navigated", event => didNavigate(event.detail?.hash));
    const unlock = () => { if (soundOn()) audioContext(); if (audio?.state === "running") { window.removeEventListener("pointerdown", unlock, true); window.removeEventListener("keydown", unlock, true); } };
    window.addEventListener("pointerdown", unlock, true); window.addEventListener("keydown", unlock, true);
    paintBadge(); schedule(0);
  }
  return { initialize, open, showLogs, mountLogs, settingsMarkup, mountSettings, refresh, didNavigate, undock, refreshWork: () => loadWork(true, true) };
})();
