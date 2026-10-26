/* Load first in the guest snapshot. The server independently limits /guest/api/
 * to sanitized read-only data; these guards keep unavailable actions out of UI. */
"use strict";

(() => {
  window.WorkbenchGuest = true;
  document.documentElement.classList.add("workbench-guest");
  const originalFetch = window.fetch.bind(window);
  // Read the public snapshot while the remaining page assets are downloading.
  // This is a one-use response, never a cache for later navigation or polling.
  const initialState = { controller: new AbortController(), expires: Date.now() + 15000, response: null, promise: null, timer: null };
  let pendingInitialState = initialState;
  function discardInitialState() {
    const pending = pendingInitialState;
    if (!pending) return;
    pendingInitialState = null;
    clearTimeout(pending.timer);
    pending.controller.abort();
    pending.response?.body?.cancel().catch(() => {});
    pending.response = null;
    pending.promise = null;
  }
  initialState.timer = setTimeout(() => {
    initialState.controller.abort();
    initialState.response?.body?.cancel().catch(() => {});
    initialState.response = null;
    initialState.promise = null;
    if (pendingInitialState === initialState) pendingInitialState = null;
  }, 15000);
  initialState.promise = originalFetch("/guest/api/state", {
    cache: "no-store", credentials: "same-origin", signal: initialState.controller.signal,
  }).then(async response => {
    initialState.response = response;
    if (!response.ok || initialState.controller.signal.aborted) {
      response.body?.cancel().catch(() => {});
      return null;
    }
    // Finish the bounded read before handing it off. Retaining the original
    // response preserves its URL and metadata, with an independent body owner.
    await response.clone().arrayBuffer();
    return initialState.controller.signal.aborted ? null : response;
  }).catch(() => null);
  window.addEventListener("pagehide", discardInitialState, { once: true });
  window.addEventListener("hashchange", discardInitialState);
  document.addEventListener("visibilitychange", () => { if (document.hidden) discardInitialState(); });
  const denied = () => Promise.resolve(new Response(JSON.stringify({ error: "访客浏览仅供查看，请登录后操作。" }), {
    status: 403, headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  }));
  window.fetch = function (input, options = {}) {
    const method = String(options.method || input?.method || "GET").toUpperCase();
    if (!["GET", "HEAD"].includes(method)) return denied();
    let url;
    try { url = new URL(typeof input === "string" || input instanceof URL ? input : input.url, location.href); }
    catch { return originalFetch(input, options); }
    let pathname = url.pathname;
    try { pathname = decodeURIComponent(pathname); } catch { /* Let the server reject invalid paths. */ }
    const stateRead = url.origin === location.origin && ["/api/state", "/guest/api/state"].includes(url.pathname);
    if (stateRead && pendingInitialState) {
      let plain = false;
      try {
        plain = method === "GET" && !url.search && !document.hidden && !(input instanceof Request)
          && !options.signal && Object.keys(options).every(key => ["method", "headers"].includes(key))
          && [...new Headers(options.headers)].length === 0;
      } catch { /* Invalid fetch options retain the normal fetch behavior. */ }
      const pending = pendingInitialState;
      if (plain && Date.now() < pending.expires) {
        const promise = pending.promise;
        pendingInitialState = null;
        return promise.then(response => {
          clearTimeout(pending.timer);
          pending.promise = null;
          pending.response = null;
          return response || originalFetch("/guest/api/state", options);
        });
      }
      discardInitialState();
    }
    if (url.origin === location.origin && pathname.startsWith("/api/")) {
      url.pathname = "/guest" + url.pathname;
      // Preserve Request headers, cancellation and other fetch options when a
      // module passes a Request instead of a URL string.
      return originalFetch(input instanceof Request ? new Request(url, input) : url, options);
    }
    return originalFetch(input, options);
  };

  const HIDDEN = [
    "[data-combo-manage]", "#product-manager-dialog",
    "#public-text-banner", "#files-nav", "[data-page='settings']", "[data-page='production']", "[data-page='content']",
    "[data-sidebar-target='files-nav']", "[data-sidebar-target='materials-nav']", "#materials-nav", "[data-material-open]",
    "#task-activity-open", "#task-activity-dialog", "#task-activity-log-dialog", ".task-activity-logs",
    "#add-top", "#import-video-top", "#library-video-import", "#detail-video-import", "#sync-button",
    "#library-select-toggle", ".case-select", "#select-all", ".selection-toolbar", ".bulk-actions",
    "#script-copy", "#script-production-toggle", "#script-production-note", "#script-production-choices", "#script-carousel-tables",
    "[data-adopted-copy]", "[data-adopted-edit]",
    ".script-more-actions", ".script-detail-actions", ".ai-candidate-actions", ".ai-continue-card", ".ai-run-settings",
    "[data-copy]", "[data-native-copy]", ".ai-copy-round", "[data-export]", "[data-download]", "a[download]",
    "[data-settings-open]", "[data-open-path]", "[data-inline-edit]", "[data-upload]", "input[type='file']",
    "#edit-dialog", "#discard-dialog", "#delete-dialog", "#bulk-dialog", "#ai-create-dialog", "#ai-delete-dialog",
    "#script-editor-dialog", "#script-discard-dialog", "#direction-plan-dialog", "#selling-point-dialog",
    "[data-home-tab='tasks']", "#home-tasks-title-panel", "[data-home-review]", "[data-home-batch]", "[data-home-start-writing]",
    "[data-followup-chain]", "[data-followup-edit]", "[data-followup-show-waiting]",
    ".home-menu", ".detail-menu", ".ai-menu", "button[aria-label='更多操作']", "summary[aria-label='更多操作']",
    ".ai-case-heading", ".detail-product-panel", ".video-copy-state", ".video-panel .missing-media > span:last-child",
  ].join(",");
  const READ_ONLY_LABEL = ".detail-product > summary";
  const BROWSE = [
    ".product-combo-trigger",
    "[data-view]", "[data-library-preset]", "[data-layout]", "[data-native-mode]", "[data-sort]", "[data-sort-header]",
    "[data-status-filter]", "[data-perf-tab]", "[data-direction-tab]", "[data-tier-filter]", "[data-direction-section]",
    "[data-detail-tab]", ".detail-tabs [data-tab]", "[data-section]", "[data-summary]", "[data-chip]", "[data-thread]",
    "[data-expand]", "[data-video]", "[data-close]", "#close-comparison", "#global-search-open", "#navigate-up",
    "#sidebar-toggle", "#sidebar-menu", "#sidebar-pin", "#guest-login-link", "[data-guest-browse]",
    "[data-native-seek]", ".cue-time", "#toggle-transcript-follow", ".player-loop", ".player-follow",
    "[data-segment-jump]", ".segment-marker", "#clear-playback-range", ".video-mode-switch [data-mode]",
  ].join(",");
  const ACTION_ATTRIBUTE = /(?:^|-)(?:copy|export|download|create|save|edit|delete|archive|restore|import|upload|generate|run|sync|adopt|produce|attach|detach|approve|reject|rework|collect|pick)(?:-|$)/;
  const ACTION_TEXT = /^(?:复制|导出|下载|生成|重新生成|继续生成|批量|保存|新增|新建|创建|编辑|修改|改成|手动录入|收录|导入|上传|同步|重新同步|归档|恢复|删除|彻底删除|写稿|空白写稿|开始协作|继续修改|取消任务|取消处理|运行|执行|重跑|重做|重新制作|制作|继续制作|启动|停止|暂停|提交|确认采用|采纳|采用|写回|发布|登记|补充案例|沉淀为案例|补录|关联|解绑|添加|移除|清理|摘录|打开.*文件夹)/;

  function safeHash(hash) {
    const match = /^#(\/[^/?]+\/)?(production|content|scripts|settings|files)(\/[^?]*)?(?:\?(.*))?$/.exec(hash);
    if (!match) return hash;
    if (["content", "scripts"].includes(match[2]) && /^\/SCR-[a-f0-9]{12}$/.test(match[3] || "")) return hash;
    const query = new URLSearchParams(match[4] || "");
    const product = !match[1] && query.has("product") ? "?product=" + encodeURIComponent(query.get("product")) : "";
    return "#" + (match[1] || "") + (["production", "content", "scripts"].includes(match[2]) ? "ai" : "library") + product;
  }
  function safeRoute() {
    const next = safeHash(location.hash);
    if (next !== location.hash) history.replaceState(null, "", next);
    const link = document.getElementById("guest-login-link");
    if (link) {
      const destination = new URL(location.href);
      destination.searchParams.delete("guest");
      link.href = "/auth/login?next=" + encodeURIComponent(destination.pathname + destination.search + destination.hash);
    }
  }
  safeRoute();
  window.addEventListener("hashchange", safeRoute, true);

  function filterForm(form) {
    return /(?:search|filter)/i.test(form.id || "") || form.getAttribute("role") === "search";
  }

  function unavailable(node) {
    if (!node?.matches) return false;
    if (node.matches("[data-select-trigger]")) {
      const source = window.WorkbenchShell?.selectForTrigger?.(node);
      return !source || source.hidden || !!source.closest('[data-guest-hidden="true"],[data-public-hidden="true"]') || unavailable(source);
    }
    if (node.matches(READ_ONLY_LABEL)) return true;
    if (node.matches(HIDDEN) || node.closest("[data-guest-hidden='true'],[data-public-hidden='true']")) return true;
    if (node.matches("a[href]")) {
      const href = node.getAttribute("href") || "";
      if (href.startsWith("#") && safeHash(href) !== href) return true;
      if (/^(?:blob:|data:)/i.test(href) || /^\/(?:api|guest\/api)\//.test(href)) return true;
    }
    if (node.matches(BROWSE)) return false;
    if (!node.matches("button,a,input[type='submit'],summary")) return false;
    if ([...node.attributes].some(attr => attr.name.startsWith("data-") && ACTION_ATTRIBUTE.test(attr.name.slice(5)))) return true;
    if (ACTION_ATTRIBUTE.test(node.id || "")) return true;
    // A case or script title may itself begin with an action verb. Classify
    // labels only on action controls, never on ordinary content-card links.
    if (node.matches("button,input[type='submit'],summary,a.primary,a.secondary,a.subtle,a.icon-button")) {
      return [node.textContent, node.value, node.getAttribute("aria-label"), node.title]
        .some(value => ACTION_TEXT.test(String(value || "").trim()));
    }
    return false;
  }
  function hide(node) {
    if (node.dataset.guestHidden === "true") return;
    node.dataset.guestHidden = "true";
    node.setAttribute("aria-hidden", "true");
    node.tabIndex = -1;
    if ("disabled" in node) node.disabled = true;
  }
  function mediaLinks() {
    document.querySelectorAll("a.history-asset[href]").forEach(node => {
      const url = new URL(node.getAttribute("href"), location.href);
      if (url.origin === location.origin && /^\/guest-media\/PVR-[a-f0-9]{12}\/(?:video|screenshot|contact_sheet)(?:$)/.test(url.pathname)) {
        if (!node.hasAttribute("data-guest-media-link")) node.setAttribute("data-guest-media-link", "true");
      } else hide(node);
    });
    // Current image attachments have no standalone viewer in the local UI.
    // Read its already loaded case so showing them does not add an API request.
    const item = typeof state === "object" ? state.detail : null;
    const host = document.querySelector(".detail-main");
    if (!host || !/^PVR-[a-f0-9]{12}$/.test(item?.case_id || "")) return;
    const images = [["screenshot", "表现数据截图"], ["contact_sheet", "画面缩略图"]]
      .filter(([kind]) => item.assets?.[kind]?.exists === true);
    if (!images.length || host.querySelector("[data-guest-media-gallery]")) return;
    const gallery = document.createElement("details");
    gallery.className = "section-card guest-media-gallery";
    gallery.setAttribute("data-guest-media-gallery", "true");
    const title = document.createElement("summary");
    title.textContent = "图片资料 · " + images.length;
    gallery.append(title);
    for (const [kind, label] of images) {
      const figure = document.createElement("figure");
      const link = document.createElement("a");
      link.href = mediaURL(item.case_id, kind, item.current_event_id);
      link.target = "_blank";
      link.rel = "noopener";
      const picture = document.createElement("img");
      picture.src = link.href;
      picture.alt = label;
      picture.loading = "lazy";
      picture.decoding = "async";
      link.append(picture);
      const caption = document.createElement("figcaption");
      caption.textContent = label;
      figure.append(link, caption);
      gallery.append(figure);
    }
    host.append(gallery);
  }
  function scan() {
    if (!document.body) return;
    document.querySelectorAll(HIDDEN).forEach(hide);
    document.querySelectorAll("button,a,input[type='submit'],summary").forEach(node => {
      if (!node.matches(READ_ONLY_LABEL) && unavailable(node)) hide(node);
    });
    document.querySelectorAll("form").forEach(form => { if (!filterForm(form)) hide(form); });
    document.querySelectorAll("[contenteditable]:not([contenteditable='false'])").forEach(node => node.setAttribute("contenteditable", "false"));
    document.querySelectorAll("video,audio").forEach(node => {
      if (!node.controlsList.contains("nodownload")) node.controlsList.add("nodownload");
    });
    mediaLinks();
    document.querySelectorAll(READ_ONLY_LABEL).forEach(node => {
      if (node.tabIndex !== -1) node.tabIndex = -1;
      if (node.getAttribute("aria-disabled") !== "true") node.setAttribute("aria-disabled", "true");
      if (node.title !== "所属产品") node.title = "所属产品";
      if (node.parentElement.open) node.parentElement.open = false;
    });
    // The task feed has no guest endpoint. Keep the other overview panels and
    // their read links usable when the normal home renderer selects it first.
    if (document.querySelector("[data-home-tab='tasks'][aria-selected='true']")) {
      document.querySelector("[data-home-tab='due']")?.click();
    }
    document.querySelectorAll("#home-page > .data-warning").forEach(node => {
      if (/^待处理内容暂时无法读取。/.test(node.textContent.trim())) hide(node);
    });
  }
  function block(event) { event.preventDefault(); event.stopImmediatePropagation(); }
  document.addEventListener("click", event => {
    const target = event.target?.closest?.("button,a,input,select,textarea,summary");
    if (unavailable(target)) block(event);
  }, true);
  document.addEventListener("submit", event => {
    const form = event.target;
    if (!filterForm(form) || [...form.querySelectorAll("button,input[type='submit']")].some(unavailable)) block(event);
  }, true);
  document.addEventListener("change", event => {
    if (unavailable(event.target) || event.target?.closest?.("[data-guest-hidden='true'],[data-public-hidden='true']")) block(event);
  }, true);
  document.addEventListener("drop", block, true);
  document.addEventListener("keydown", event => {
    if ((event.ctrlKey || event.metaKey) && ["s", "n"].includes(event.key.toLowerCase())) block(event);
  }, true);

  function start() {
    const title = () => { document.title = "TK工作台 · 访客浏览"; };
    // A browser may run microtasks between DOMContentLoaded listeners. Apply
    // the title after the shared viewer has finished its own startup listener.
    if (document.readyState === "complete") title();
    else window.addEventListener("load", title, { once: true });
    if (!document.getElementById("guest-banner")) {
      const banner = document.createElement("div");
      banner.id = "guest-banner";
      banner.className = "guest-banner";
      banner.setAttribute("role", "note");
      const label = document.createElement("strong");
      label.textContent = "访客浏览 · 仅供查看";
      const link = document.createElement("a");
      link.id = "guest-login-link";
      link.className = "guest-login-link";
      link.textContent = "登录工作台";
      link.href = "/auth/login";
      banner.append(label, link);
      const topbar = document.querySelector("main .topbar");
      if (topbar) topbar.after(banner);
      else document.body.prepend(banner);
    }
    safeRoute();
    scan();
    let scheduled = false;
    const observer = new MutationObserver(() => {
      if (scheduled) return;
      scheduled = true;
      queueMicrotask(() => { scheduled = false; observer.disconnect(); scan(); observer.observe(document.body, observerOptions); });
    });
    const observerOptions = { subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ["href", "id", "class", "aria-label", "aria-selected", "title", "contenteditable"] };
    observer.observe(document.body, observerOptions);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
