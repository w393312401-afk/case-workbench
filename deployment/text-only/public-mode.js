/* Load this script before the copied workbench scripts. The public server must
 * independently reject mutations and serve only the exported text snapshot. */
"use strict";

(() => {
  const MEDIA_SELECTOR = "img,image,video,audio,source,picture,iframe,object,embed,track";
  const MEDIA_ATTRIBUTES = new Set(["src", "srcset", "poster", "preload", "data-src", "data", "href", "xlink:href"]);
  const WRITE_SELECTOR = [
    "#add-top", "#import-video-top", "#library-video-import", "#detail-video-import",
    "#edit-case", "#archive-case", "#delete-case", "#release-edit", "[data-inline-edit]",
    "#bulk-archive", "#bulk-restore", "#bulk-delete", ".bulk-actions", "[data-move-case]",
    "#library-save-view-button", "#library-manage-views", "#navigation-settings",
    "#files-nav", "[data-page='settings']", "[data-settings-open]", "[data-open-path]",
    "#open-case-folder", "#open-video-folder", "#open-source-folder", "#sync-button",
    "#settings-check-codex", "#settings-navigation", "#service-shutdown", "#shutdown-service",
    "#perf-upload", "#perf-sync", "#perf-file", "[data-collect]", "[data-video-hook]",
    "#rev-sync", "[data-edit-tags]", "[data-tag-edit]", "[data-pick]", "[data-review-theme]",
    "#rev-basket-toggle", "#materials-nav", "[data-material-open]", "[data-native-material]",
    "[data-material-save]", "[data-material-remove]", "[data-material-theme]",
    "[data-home-plan-write]", "[data-home-plan]", "[data-direction-plan]",
    "#direction-write", "#direction-edit", "#direction-add-variant", "[data-edit-point]", "[data-use-point]",
    "#script-new", "#script-derive", "#script-case", "#script-release", "#script-write-source",
    "#script-use-selection", "#ai-start-blank", "[data-ai-action]", "#ai-cancel-task",
    "#ai-delete-selected", "[data-adopt-script]", "[data-write-case]", "#ai-check-service",
    "#ai-continue-form", ".ai-continue-card", ".ai-selection-toolbar", ".ai-task-select",
    ".ai-candidate-actions", ".ai-run-settings", ".ai-service-row", "input[type='file']",
    "[data-upload]", "[data-video-drop] .video-dropzone", ".video-direction-editor",
  ].join(",");
  const WRITE_DIALOG_SELECTOR = [
    "#edit-dialog", "#discard-dialog", "#delete-dialog", "#bulk-dialog",
    "#ai-create-dialog", "#ai-delete-dialog", "#navigation-dialog",
    "#direction-plan-dialog", "#direction-create-dialog", "#direction-edit-dialog",
    "#direction-discard-dialog", "#selling-point-dialog", "#script-editor-dialog",
    "#script-discard-dialog", "#material-hook-dialog", "#material-theme-dialog",
  ].join(",");
  const READ_LABEL_SELECTOR = "[data-video-direction],.timestamp-chip[data-native-seek],.cue-time";
  const PLAYBACK_SELECTOR = ".native-play-actions,[data-native-seek]:not(.timestamp-chip),#toggle-transcript-follow,.segment-controls,.player-follow";
  const BROWSE_SELECTOR = [
    ".nav-item[data-page]:not([data-page='settings'])", "[data-view]", "[data-library-preset]",
    "[data-layout]", "[data-native-mode]", "[data-sort]", "[data-sort-header]", "[data-status-filter]",
    "[data-perf-tab]", "[data-direction-tab]", "[data-tier-filter]", "[data-direction-section]",
    "[data-detail-tab]", ".detail-tabs [data-tab]", "[data-section]", "[data-summary]",
    "[data-chip]", "[data-thread]", "[data-expand]", "[data-copy]", "[data-native-copy]",
    "[data-video]", ".ai-copy-round", "[data-close]", "#close-comparison",
  ].join(",");
  const ACTION_TEXT = /^(?:保存|新增|新建|编辑|收录|导入|上传|同步|重新同步|批量归档|批量恢复|彻底删除|删除本地|空白写稿|继续修改|取消处理|开始协作|关闭服务|停止服务|打开.*文件夹|关联主题|摘录钩子|采纳为|写回|手动保存)/;

  // Removing media from HTML before it enters the live DOM prevents a transient
  // fetch. The observer below also handles nodes inserted through DOM methods.
  function textOnlyMarkup(value) {
    return String(value)
      .replace(/<(video|audio|picture|iframe|object)\b[^>]*>[\s\S]*?<\/\1\s*>/gi, "")
      .replace(/<\/?(?:img|image|video|audio|source|picture|iframe|object|embed|track)\b[^>]*>/gi, "")
      .replace(/\s(?:poster|srcset|data-src)\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, "");
  }
  for (const name of ["innerHTML", "outerHTML"]) {
    const descriptor = Object.getOwnPropertyDescriptor(Element.prototype, name);
    if (!descriptor?.set || !descriptor.configurable) continue;
    Object.defineProperty(Element.prototype, name, {
      ...descriptor,
      set(value) { descriptor.set.call(this, textOnlyMarkup(value)); },
    });
  }
  const insertHTML = Element.prototype.insertAdjacentHTML;
  Element.prototype.insertAdjacentHTML = function(position, markup) {
    return insertHTML.call(this, position, textOnlyMarkup(markup));
  };
  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function(name, value) {
    if (this.matches(MEDIA_SELECTOR) && MEDIA_ATTRIBUTES.has(String(name).toLowerCase())) return;
    return setAttribute.call(this, name, value);
  };
  for (const [constructor, names] of [
    [window.HTMLImageElement, ["src", "srcset"]],
    [window.HTMLMediaElement, ["src", "preload"]],
    [window.HTMLVideoElement, ["poster"]],
    [window.HTMLSourceElement, ["src", "srcset"]],
    [window.HTMLIFrameElement, ["src", "srcdoc"]],
    [window.HTMLObjectElement, ["data"]],
    [window.HTMLEmbedElement, ["src"]],
    [window.HTMLTrackElement, ["src"]],
  ]) {
    if (!constructor) continue;
    for (const name of names) {
      const descriptor = Object.getOwnPropertyDescriptor(constructor.prototype, name);
      if (!descriptor?.set || !descriptor.configurable) continue;
      Object.defineProperty(constructor.prototype, name, {...descriptor, set() {}});
    }
  }
  function stripMedia(root) {
    if (!root?.querySelectorAll) return;
    const media = root.matches?.(MEDIA_SELECTOR) ? [root] : [...root.querySelectorAll(MEDIA_SELECTOR)];
    for (const node of media) {
      if (node.pause) { try { node.pause(); } catch {} }
      for (const name of MEDIA_ATTRIBUTES) node.removeAttribute(name);
      node.removeAttribute("autoplay");
      node.remove();
    }
    root.querySelectorAll("link[rel='preload'][as='image'],link[rel='preload'][as='video'],link[rel='preload'][as='audio'],link[rel='icon']").forEach(node => node.remove());
  }
  for (const method of ["appendChild", "insertBefore", "replaceChild"]) {
    const original = Node.prototype[method];
    Node.prototype[method] = function(node, ...args) {
      stripMedia(node);
      if (node?.matches?.(MEDIA_SELECTOR)) return node;
      return original.call(this, node, ...args);
    };
  }
  if (window.HTMLMediaElement) {
    HTMLMediaElement.prototype.load = function() {};
    HTMLMediaElement.prototype.play = function() { return Promise.resolve(); };
  }

  // This is a UI guard, not the permission boundary. The Worker uses a route
  // allowlist and never implements local editing, importing, or AI execution.
  const originalFetch = window.fetch.bind(window);
  // An exported task can retain "running" as historical evidence. It must never
  // establish a live event stream from the public copy.
  window.EventSource = undefined;
  const originalInterval = window.setInterval.bind(window);
  window.setInterval = function(callback, delay, ...args) {
    const source = typeof callback === "function" ? Function.prototype.toString.call(callback) : "";
    // These are the two existing local-state pollers. Ordinary UI timers remain
    // available; navigation still reads the published snapshot on demand.
    if (/\brefresh\(true\)/.test(source) || /\bpollTask\(\)/.test(source) && /\bloadTaskList\(\)/.test(source)) return 0;
    return originalInterval(callback, delay, ...args);
  };
  window.fetch = function(input, options = {}) {
    const method = String(options.method || input?.method || "GET").toUpperCase();
    const url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href);
    if (!["GET", "HEAD"].includes(method) || url.origin !== location.origin ||
        /^\/(?:media|attachments?)\//i.test(url.pathname) ||
        /\/(?:thumbnail|stream)(?:\/|$)/i.test(url.pathname)) {
      return Promise.resolve(new Response(JSON.stringify({error: "文字浏览版仅供查看，修改请在本机工作台完成。"}), {
        status: 403, headers: {"Content-Type": "application/json; charset=utf-8"},
      }));
    }
    return originalFetch(input, options);
  };

  function isWriteControl(node) {
    if (!node?.matches) return false;
    if (node.matches(WRITE_SELECTOR) || node.closest(WRITE_DIALOG_SELECTOR)) return true;
    if (node.matches(BROWSE_SELECTOR) || node.matches(READ_LABEL_SELECTOR)) return false;
    if (node.matches("[data-completion-section]")) return node.closest(".is-missing") !== null;
    if (!node.matches("button,a,input[type='submit']")) return false;
    return ACTION_TEXT.test((node.textContent || node.value || "").trim());
  }
  function hideWrite(node) {
    node.dataset.publicHidden = "true";
    node.setAttribute("aria-hidden", "true");
    node.tabIndex = -1;
    if ("disabled" in node) node.disabled = true;
  }
  function exportedTime() {
    const value = window.PUBLIC_EXPORT_META?.exported_at || window.PUBLIC_EXPORT_META?.generated_at ||
      document.querySelector("meta[name='public-exported-at']")?.content || "";
    if (!value) return "见导出记录";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(date) + "（北京时间）";
  }
  const READING_STATE_KEY = "public-text-reading-state-v1";
  const READING_FILTER_SELECTOR = [
    "#case-search", "#library-saved-view", "#product-filter", "#direction-filter",
    "#archive-filter", "#category-filter", "#source-filter", "#origin-filter", "#variant-filter",
    "#perf-q", "#perf-range", "#perf-period", "#perf-account", "#perf-status",
    "#rev-q", "#rev-brand", "#rev-empty", "#rev-irrelevant", "#rev-language", "#rev-rating",
    "#rev-replied", "#rev-role", "#rev-sku", "#rev-sort", "#rev-video", "#ai-group-by",
    "#script-status-filter", "#script-query", "#direction-filters input[name]:not([type='hidden'])",
    "#direction-filters select[name]", "#workbench-search-input",
  ].join(",");
  const publishedSync = {version: "", checking: false, started: false, timer: null, next: null, reloading: false,
    status: "自动同步已发布文字 · 每 30 秒检查更新"};
  let readingResume = null;
  function controlSelector(node) {
    const quote = value => String(value).replace(/\\/g, "\\\\").replace(/"/g, '\\"');
    return node.id ? `[id="${quote(node.id)}"]` : `#direction-filters [name="${quote(node.name)}"]`;
  }
  function captureReadingState() {
    const controls = [...document.querySelectorAll(READING_FILTER_SELECTOR)]
      .filter(node => !node.disabled && !node.closest("[data-public-hidden='true']"))
      .map(node => ({selector: controlSelector(node), value: String(node.value || ""),
        checked: node.type === "checkbox" ? Boolean(node.checked) : null}));
    const active = document.activeElement;
    return {hash: location.hash, saved_at: Date.now(), x: window.scrollX || 0, y: window.scrollY || 0,
      controls, focus: active?.matches?.(READING_FILTER_SELECTOR) ? controlSelector(active) : "",
      global_search: Boolean(document.querySelector("#workbench-search[open]"))};
  }
  function loadReadingState() {
    try {
      const saved = JSON.parse(window.sessionStorage.getItem(READING_STATE_KEY) || "null");
      window.sessionStorage.removeItem(READING_STATE_KEY);
      if (!saved || saved.hash !== location.hash || Date.now() - saved.saved_at > 5 * 60 * 1000 ||
          !Array.isArray(saved.controls)) return;
      readingResume = {...saved, pending: new Set(saved.controls.map(item => item.selector)), attempts: 0,
        searchOpened: false, scrollScheduled: false};
    } catch { /* Browsing and refreshing still work when browser storage is unavailable. */ }
  }
  function restoreReadingState() {
    const saved = readingResume;
    if (!saved || saved.hash !== location.hash) { readingResume = null; return; }
    if (saved.global_search && !saved.searchOpened && document.getElementById("global-search-open")) {
      saved.searchOpened = true;
      document.getElementById("global-search-open").click();
    }
    for (const item of saved.controls) {
      if (!saved.pending.has(item.selector)) continue;
      const node = document.querySelector(item.selector);
      if (!node || node.disabled || node.closest("[data-public-hidden='true']")) continue;
      saved.pending.delete(item.selector);
      if (item.checked !== null) node.checked = item.checked;
      else node.value = item.value;
      node.dispatchEvent(new Event("input", {bubbles: true}));
      node.dispatchEvent(new Event("change", {bubbles: true}));
    }
    if (saved.scrollScheduled) return;
    saved.attempts++;
    // Filters can redraw asynchronously. Wait briefly for those changes before
    // restoring the reading position, while keeping the current route intact.
    if ((saved.pending.size || document.querySelector("#content .skeleton-page")) && saved.attempts < 25) {
      window.setTimeout(restoreReadingState, 200);
      return;
    }
    saved.scrollScheduled = true;
    window.setTimeout(() => {
      if (readingResume !== saved || saved.hash !== location.hash) return;
      const focus = saved.focus && document.querySelector(saved.focus);
      if (focus && !focus.disabled) focus.focus({preventScroll: true});
      window.scrollTo(saved.x, saved.y);
      readingResume = null;
    }, 800);
  }
  function syncStatus(value) {
    publishedSync.status = value;
    setText(document.querySelector("[data-public-sync-status]"), value);
  }
  function refreshPublishedContent(metadata) {
    if (publishedSync.reloading) return;
    publishedSync.next = metadata;
    if (document.hidden) return;
    publishedSync.reloading = true;
    syncStatus("已发布文字有更新，正在自动刷新…");
    window.setTimeout(() => {
      if (document.hidden) { publishedSync.reloading = false; return; }
      try { window.sessionStorage.setItem(READING_STATE_KEY, JSON.stringify(captureReadingState())); } catch {}
      // reload preserves the current hash. The new document restores search
      // inputs, filters, and reading position from this tab's own storage.
      location.reload();
    }, 250);
  }
  async function checkPublishedVersion() {
    if (document.hidden || publishedSync.checking || publishedSync.reloading) return false;
    publishedSync.checking = true;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    const timeout = controller ? window.setTimeout(() => controller.abort(), 8000) : null;
    try {
      const response = await window.fetch("/api/public-version", {cache: "no-store", credentials: "same-origin",
        headers: {Accept: "application/json"}, ...(controller ? {signal: controller.signal} : {})});
      if (!response.ok) throw new Error("version unavailable");
      const metadata = await response.json();
      if (typeof metadata.version !== "string" || !metadata.version || metadata.version.length > 512) throw new Error("invalid version");
      if (!publishedSync.version) {
        publishedSync.version = metadata.version;
        const loadedAt = window.PUBLIC_EXPORT_META?.exported_at || window.PUBLIC_EXPORT_META?.generated_at ||
          document.querySelector("meta[name='public-exported-at']")?.content || "";
        const loadedTime = Date.parse(loadedAt), publishedTime = Date.parse(metadata.exported_at);
        if (loadedAt && metadata.exported_at &&
            (Number.isNaN(loadedTime) || Number.isNaN(publishedTime) ? loadedAt !== metadata.exported_at : loadedTime !== publishedTime)) {
          refreshPublishedContent(metadata);
          return true;
        }
      } else if (metadata.version !== publishedSync.version) {
        refreshPublishedContent(metadata);
        return true;
      }
      syncStatus("自动同步已发布文字 · 每 30 秒检查更新");
      return false;
    } catch {
      syncStatus("自动同步暂时无法检查，稍后重试");
      return false;
    } finally {
      if (timeout !== null) window.clearTimeout(timeout);
      publishedSync.checking = false;
    }
  }
  function resumeVersionChecks() {
    if (!publishedSync.started || document.hidden) return;
    if (publishedSync.timer === null) publishedSync.timer = originalInterval(checkPublishedVersion, 30000);
    if (publishedSync.next) refreshPublishedContent(publishedSync.next);
    else void checkPublishedVersion();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (publishedSync.timer !== null) window.clearInterval(publishedSync.timer);
      publishedSync.timer = null;
    } else resumeVersionChecks();
  });
  function setText(node, value) { if (node && node.textContent !== value) node.textContent = value; }
  function publicCopy() {
    setText(document.getElementById("connection-text"), "公网文字 · 自动同步");
    const connection = document.querySelector(".connection");
    if (connection) connection.title = "文字浏览版 · 数据截至导出时间：" + exportedTime();
    setText(document.getElementById("sync-detail"), "每 30 秒检查新发布的文字；修改在本机完成。");
    document.querySelectorAll(".perf-sources").forEach(node => setText(node, "自动同步已发布文字；数据时间以本次导出记录为准。"));
    setText(document.querySelector(".sidebar-bottom > p:last-of-type"), "浏览与复制文字；修改在本机工作台完成。");
    const total = document.querySelector(".total-note");
    if (total) setText(total, total.textContent.replace(/本地保存/g, "文字快照"));
    const aiNav = document.getElementById("ai-nav");
    if (aiNav) for (const text of aiNav.childNodes) {
      if (text.nodeType === Node.TEXT_NODE && text.textContent.trim() === "写稿") text.textContent = "写稿记录";
    }
    document.querySelectorAll(".ai-status,.ai-round-status").forEach(node => {
      if (node.classList.contains("running") || node.classList.contains("queued")) {
        const label = node.textContent.replace(/^导出时状态：\s*/, "");
        setText(node, `导出时状态：${label}`);
      }
    });
    const taskStatus = document.getElementById("ai-task-status");
    if (taskStatus) setText(document.getElementById("ai-task-progress"),
      `导出时状态：${taskStatus.textContent.replace(/^导出时状态：\s*/, "")}`);
    setText(document.getElementById("ai-stream-status"), "显示最新已发布的文字版本；新版本发布后会自动刷新。");
    document.querySelectorAll(".home-summary-strip span").forEach(node => {
      if (node.textContent === "任务运行中") setText(node, "导出时运行中");
    });
    const heading = document.querySelector(".page-heading");
    if (heading?.querySelector(".eyebrow")?.textContent === "WRITING WORKSPACE") {
      setText(heading.querySelector("h1"), "写稿记录");
      setText(heading.querySelector("p"), "查看已导出的协作任务与脚本版本，复制需要的文字。");
    }
    for (const node of document.querySelectorAll(".scripts-start-actions a")) {
      if (node.textContent.trim() === "从方向开始") setText(node, "查看方向");
      if (node.textContent.trim() === "从案例开始") setText(node, "查看案例");
    }
    setText(document.querySelector(".ai-candidate-note:not(#ai-create-dialog .ai-candidate-note)"),
      "以下为导出时保存的候选文字。继续修改、采纳和写回请在本机工作台完成。");
    const banner = document.getElementById("public-text-banner");
    if (banner) {
      setText(banner.querySelector("[data-public-export-time]"), `数据截至导出时间：${exportedTime()}`);
      setText(banner.querySelector("[data-public-sync-status]"), publishedSync.status);
    }
  }
  let scanning = false;
  function scan() {
    if (scanning || !document.body) return;
    scanning = true;
    try {
      stripMedia(document);
      document.body.classList.add("public-text-mode");
      document.querySelectorAll(WRITE_SELECTOR + "," + WRITE_DIALOG_SELECTOR + "," + PLAYBACK_SELECTOR).forEach(hideWrite);
      document.querySelectorAll("button,a,input[type='submit']").forEach(node => { if (isWriteControl(node)) hideWrite(node); });
      document.querySelectorAll(READ_LABEL_SELECTOR).forEach(node => {
        node.dataset.publicReadLabel = "true";
        node.disabled = true;
        node.title = "文字浏览版保留记录，不播放媒体或更改关联";
      });
      document.querySelectorAll("[draggable='true']").forEach(node => node.draggable = false);
      publicCopy();
    } finally { scanning = false; }
  }
  function blocked(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  document.addEventListener("click", event => {
    const target = event.target?.closest?.("button,a,input,select,textarea");
    if (isWriteControl(target) || target?.closest("[data-public-hidden='true']") || target?.matches(READ_LABEL_SELECTOR)) blocked(event);
  }, true);
  document.addEventListener("submit", event => {
    if (event.target?.closest(WRITE_DIALOG_SELECTOR + ",.ai-continue-card,.video-direction-editor") ||
        event.target?.querySelector("[data-public-hidden='true'][type='submit']")) blocked(event);
  }, true);
  document.addEventListener("change", event => {
    if (event.target?.matches(WRITE_SELECTOR)) blocked(event);
  }, true);
  document.addEventListener("drop", blocked, true);
  document.addEventListener("dragover", event => { if (event.dataTransfer?.types?.includes("Files")) blocked(event); }, true);
  document.addEventListener("keydown", event => {
    if (event.ctrlKey || event.metaKey) {
      if (event.key.toLowerCase() === "n" || event.key.toLowerCase() === "s") blocked(event);
    }
  }, true);
  const showModal = window.HTMLDialogElement?.prototype.showModal;
  if (showModal) HTMLDialogElement.prototype.showModal = function() {
    if (this.matches(WRITE_DIALOG_SELECTOR) || this.querySelector("form [type='submit']")) return;
    return showModal.call(this);
  };
  function safeRoute() {
    const hash = location.hash;
    if (/^#(?:\/(?:[^/]+)\/)?(?:settings|files)(?:[/?]|$)/.test(hash)) {
      const next = hash.replace(/(?:settings|files)(?:\/[^?]*)?/, "library");
      history.replaceState(null, "", next);
    }
  }
  safeRoute();
  window.addEventListener("hashchange", safeRoute, true);
  function start() {
    document.title = "案例工作台 · 自动同步文字版";
    if (!document.getElementById("public-text-banner")) {
      const banner = document.createElement("div");
      banner.id = "public-text-banner";
      banner.className = "public-text-banner";
      banner.setAttribute("role", "note");
      banner.innerHTML = '<strong>文字浏览版</strong><span data-public-export-time></span><span data-public-sync-status aria-live="polite"></span><span>修改在本机</span>';
      document.querySelector("main .topbar")?.after(banner);
    }
    loadReadingState();
    scan();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      queueMicrotask(() => { scheduled = false; observer.disconnect(); scan(); observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["src", "srcset", "poster", "preload", "hidden", "draggable"]}); });
    });
    observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["src", "srcset", "poster", "preload", "hidden", "draggable"]});
    if (readingResume) restoreReadingState();
    publishedSync.started = true;
    resumeVersionChecks();
  }
  window.PublicTextMode = Object.freeze({textOnlyMarkup, scan, checkVersion: checkPublishedVersion,
    captureReadingState, restoreReadingState, setMetadata(metadata) { window.PUBLIC_EXPORT_META = metadata; publicCopy(); }});
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, {once: true});
  else start();
})();
