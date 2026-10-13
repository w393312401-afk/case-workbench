"use strict";

// Product and URL context are shared by every page; no case records are changed.
(() => {
  const storageKey = "workbench-product-v1";
  let current = "202H";
  const products = new Map([["202H", "SHARPAL 202H"]]);
  const listContexts = new Map(), detailContexts = new Map();
  const navigationStorageKey = "workbench-navigation-v1";
  const pageLabels = { home: "工作概览", library: "案例库", directions: "卖点与方向", videos: "视频数据", reviews: "用户声音", ai: "内容创作", scripts: "脚本版本", production: "制作单", settings: "设置" };
  let navigationRoute = null, restoringList = null;
  let breadcrumbSource = null, breadcrumbHTML = null;
  try {
    const saved = localStorage.getItem(storageKey);
    if (saved !== null) current = normalizeProduct(saved);
  } catch { /* The URL remains the source of truth without storage. */ }

  function normalizeProduct(value) {
    const text = String(value || "").trim();
    if (!text || text === "all") return "";
    const codes = [...new Set((text.match(/(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])/gi) || []).map(code => code.toUpperCase()))];
    return codes.length === 1 ? codes[0] : text;
  }
  function productOf(value) {
    if (typeof value === "string") return normalizeProduct(value);
    if (!value) return "";
    return normalizeProduct(typeof productFilterGroup === "function" && value.current ? productFilterGroup(value) : value.product || "");
  }
  function matchesProduct(value, wanted = current) {
    return !wanted || productOf(value).toLocaleLowerCase() === normalizeProduct(wanted).toLocaleLowerCase();
  }
  function parse(hash = location.hash, fallback = current) {
    let raw = String(hash || "#home").replace(/^#/, ""), chosen = fallback;
    const split = raw.indexOf("?");
    const params = new URLSearchParams(split < 0 ? "" : raw.slice(split + 1));
    raw = split < 0 ? raw : raw.slice(0, split);
    const canonical = raw.startsWith("/");
    if (canonical) {
      const pieces = raw.slice(1).split("/");
      try { chosen = normalizeProduct(decodeURIComponent(pieces.shift() || "all")); }
      catch { chosen = fallback; }
      raw = pieces.join("/");
    } else if (params.has("product")) chosen = normalizeProduct(params.get("product"));
    params.delete("product");
    if (raw === "native") { raw = "library"; params.set("view", "native"); }
    let [page, ...tail] = raw.split("/");
    if (page === "case" && tail[1] === "analysis") tail[1] = "script";
    if (page === "files") page = "settings";
    if (!["home", "library", "case", "directions", "videos", "reviews", "ai", "scripts", "production", "settings"].includes(page)) { page = "library"; tail = []; }
    if (page === "case" && (!canonical || chosen) && typeof state !== "undefined") {
      const item = state.cases.find(row => row.case_id === tail[0]);
      if (item?.product) chosen = productOf(item);
    }
    return { page, tail, product: chosen, params, canonical };
  }
  function format(parsed) {
    const query = parsed.params.toString();
    return `#/${encodeURIComponent(parsed.product || "all")}/${parsed.page}${parsed.tail.length ? "/" + parsed.tail.join("/") : ""}${query ? "?" + query : ""}`;
  }
  function url(hash = "#library", selected) {
    const parsed = parse(hash);
    if (selected !== undefined) parsed.product = normalizeProduct(selected);
    return format(parsed);
  }
  const listKey = (page, product) => `${page}:${encodeURIComponent(normalizeProduct(product))}`;
  const detailKey = parsed => `${listKey(parsed.page, parsed.product)}:${parsed.tail[0] || ""}`;
  const moduleFor = page => ({case: "library"}[page] || page);
  function validListContext(saved, page, product, allowAll = false) {
    if (!saved || typeof saved.hash !== "string" || !/^#\/[^/]+\/[^/?]+(?:\?[^#]*)?$/.test(saved.hash)) return null;
    const parsed = parse(saved.hash);
    return parsed.page === page && !parsed.tail.length && (parsed.product === normalizeProduct(product) || (allowAll && !parsed.product))
      && format(parsed) === saved.hash && Number.isFinite(saved.scroll) && saved.scroll >= 0 ? saved : null;
  }
  function savedListContext(page, product = parse().product) {
    return validListContext(listContexts.get(listKey(page, product)), page, product);
  }
  function detailContext(parsed = parse()) {
    return validListContext(detailContexts.get(detailKey(parsed)), moduleFor(parsed.page), parsed.product, true);
  }
  function listURL(page, product = parse().product) {
    if (navigationRoute?.page === page && !navigationRoute.tail.length && navigationRoute.product === normalizeProduct(product)) {
      const mounted = captureList(navigationRoute);
      if (mounted) return mounted.hash;
    }
    return savedListContext(page, product)?.hash || url("#" + page, product);
  }
  function listState(page, product = parse().product) {
    return restoringList?.page === page && restoringList.product === normalizeProduct(product) ? restoringList.context.state || null : null;
  }
  function directionParent(product = parse().product) {
    return detailContext({ ...parse(), product })?.hash || listURL("directions", product);
  }
  function parentURL(parsed = parse()) {
    if (parsed.page === "case") {
      const saved = detailContext(parsed) || savedListContext("library", parsed.product);
      if (saved) return saved.hash;
      const remembered = typeof state !== "undefined" ? state.libraryHash : "";
      return typeof remembered === "string" && /^#(?:library|\/[^/]+\/library)(?:\?[^#]*)?$/.test(remembered) && (!parse(remembered).product || parse(remembered).product === parsed.product)
        ? url(remembered) : url("#library", parsed.product);
    }
    if (parsed.page === "directions" && parsed.tail.length) return directionParent(parsed.product);
    if (["ai", "scripts", "production"].includes(parsed.page) && parsed.tail.length) return detailContext(parsed)?.hash || listURL(parsed.page, parsed.product);
    return listURL(["scripts", "production"].includes(parsed.page) ? "ai" : "home", parsed.product);
  }
  function saveNavigation() {
    try { sessionStorage.setItem(navigationStorageKey, JSON.stringify({ version: 2, lists: [...listContexts].slice(-80), details: [...detailContexts].slice(-80) })); }
    catch { /* Return context still works without browser storage. */ }
  }
  function captureList(parsed) {
    const remembered = parse(parsed.hash);
    let snapshot = null;
    if (parsed.page === "directions") {
      const form = document.querySelector("#direction-filters");
      if (!form?.isConnected) return null;
      // Include input that has not reached its debounced request yet.
      for (const [key, value] of new FormData(form)) {
        const text = String(value).trim();
        if (text && !(key === "archive" && text === "active")) remembered.params.set(key, text);
        else remembered.params.delete(key);
      }
    } else if (parsed.page === "library") {
      if (!document.querySelector("#case-search")) return null;
      remembered.params = parse(state.libraryHash).params;
    } else {
      const module = { ai: window.CaseAI, scripts: window.CaseScripts, production: window.CaseProduction, videos: window.CasePerformance, reviews: window.CaseReviews, home: window.WorkbenchHome }[parsed.page];
      snapshot = module?.captureListState?.();
      if (!snapshot) return null;
      // A product switch can leave the previous list visible while fresh data loads.
      if (typeof snapshot.product === "string" && normalizeProduct(snapshot.product) !== parsed.product) return null;
      if (typeof snapshot.hash === "string") {
        const latest = parse(snapshot.hash);
        if (latest.page === parsed.page && !latest.tail.length && latest.product === parsed.product) remembered.params = latest.params;
      }
    }
    return { hash: format(remembered), scroll: Math.max(0, window.scrollY), state: snapshot };
  }
  function beginNavigation(incoming) {
    restoringList = null;
    const previous = navigationRoute;
    if (previous && !previous.tail.length) {
      const saved = captureList(previous);
      if (saved) {
        listContexts.set(listKey(previous.page, previous.product), saved);
        // Preserve the all-products source when a card points to a concrete product.
        if (incoming.tail.length && moduleFor(incoming.page) === previous.page && (!previous.product || incoming.product === previous.product)) detailContexts.set(detailKey(incoming), saved);
        saveNavigation();
      }
      if (previous.page !== incoming.page || incoming.tail.length || previous.product !== incoming.product) {
        ({ videos: window.CasePerformance, reviews: window.CaseReviews }[previous.page])?.leaveList?.();
      }
    } else if (previous?.tail.length && incoming.tail.length && incoming.page === previous.page && (incoming.product === previous.product || incoming.tail[0] === previous.tail[0])) {
      // Keep the source while browsing related versions or correcting a detail's product.
      const saved = detailContext(previous);
      if (saved) { detailContexts.set(detailKey(incoming), saved); saveNavigation(); }
    }
    if (!incoming.tail.length && (!previous || previous.tail.length || previous.page !== incoming.page || previous.product !== incoming.product)) {
      const saved = savedListContext(incoming.page, incoming.product);
      if (saved?.hash === format(incoming)) restoringList = { page: incoming.page, product: incoming.product, context: saved };
    }
    navigationRoute = { ...incoming, hash: format(incoming) };
  }
  function finishNavigation() {
    renderBreadcrumb();
    const scroll = restoringList?.context.scroll ?? null;
    restoringList = null;
    return scroll;
  }
  function renderBreadcrumb() {
    const node = document.querySelector("#breadcrumb");
    if (!node) return;
    const parsed = parse(), page = parsed.page === "case" ? "library" : parsed.page;
    const crumbs = [];
    if (["scripts", "production"].includes(page)) crumbs.push({ label: "内容创作", href: listURL("ai", parsed.product) });
    crumbs.push({ label: pageLabels[page] || "案例库", href: parsed.tail.length ? parentURL(parsed) : null });
    if (parsed.tail.length) {
      let label = parsed.page === "case" ? "案例详情" : page === "ai" ? "任务详情" : page === "scripts" ? "脚本详情" : page === "production" ? (parsed.tail[0].startsWith("CAR-") ? "图文制作单" : "制作单详情") : parsed.tail.join("/");
      try { label = decodeURIComponent(label); } catch { /* Show malformed identifiers as plain text. */ }
      crumbs.push({ label });
    }
    const markup = `<ol>${crumbs.map((crumb, index) => index === crumbs.length - 1
      ? `<li aria-current="page"><span class="breadcrumb-current" title="${esc(crumb.label)}">${esc(crumb.label)}</span></li>`
      : `<li><a href="${esc(crumb.href)}" title="返回${esc(crumb.label)}">${esc(crumb.label)}</a></li>`).join("")}</ol>`;
    if (breadcrumbSource !== markup || node.innerHTML !== breadcrumbHTML) {
      node.innerHTML = markup;
      breadcrumbSource = markup;
      breadcrumbHTML = node.innerHTML;
    }
    const up = document.querySelector("#navigate-up");
    if (up) {
      // Only detail pages offer a return control; module pages are reached from the sidebar and their own tabs.
      up.hidden = !parsed.tail.length;
      up.href = parentURL(parsed);
      const parentLabel = parsed.page === "case" ? "案例库" : pageLabels[page] || "上一级";
      up.title = `返回${parentLabel}`;
      up.setAttribute("aria-label", up.title);
    }
  }
  function initializePageNavigation() {
    const node = document.querySelector("#breadcrumb");
    if (!node) return;
    try {
      const saved = JSON.parse(sessionStorage.getItem(navigationStorageKey) || "{}");
      // Migrate the original direction-only session format.
      if (Array.isArray(saved)) for (const entry of saved.slice(-40)) {
        if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string") continue;
        const context = validListContext(entry[1], "directions", entry[0], true);
        if (!context) continue;
        const source = parse(context.hash), detail = parse();
        listContexts.set(listKey("directions", source.product), context);
        if (!source.product && detail.page === "directions" && detail.tail.length && detail.product === normalizeProduct(entry[0])) detailContexts.set(detailKey(detail), context);
      }
      else if (saved?.version === 2) {
        for (const [map, entries, isDetail] of [[listContexts, saved.lists, false], [detailContexts, saved.details, true]]) {
          if (!Array.isArray(entries)) continue;
          for (const entry of entries.slice(-80)) {
            if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string") continue;
            const [page, encodedProduct] = entry[0].split(":");
            let product;
            try { product = decodeURIComponent(encodedProduct || ""); } catch { continue; }
            if (!Object.hasOwn(pageLabels, moduleFor(page))) continue;
            if (validListContext(entry[1], moduleFor(page), product, isDetail)) map.set(entry[0], entry[1]);
          }
        }
      }
    } catch { /* Invalid browser state starts with the normal module parent. */ }
    // Page modules also refresh themselves without going through the main router.
    new MutationObserver(renderBreadcrumb).observe(node, { childList: true });
    renderBreadcrumb();
  }
  function legacyHash(hash = location.hash) {
    const parsed = parse(hash);
    if (["library", "directions", "videos", "reviews"].includes(parsed.page) && parsed.product) parsed.params.set("product", parsed.product);
    const query = parsed.params.toString();
    return `#${parsed.page === "settings" ? "files" : parsed.page}${parsed.tail.length ? "/" + parsed.tail.join("/") : ""}${query ? "?" + query : ""}`;
  }
  function rememberProduct(value) {
    const previous = current;
    current = normalizeProduct(value);
    if (current && !products.has(current)) products.set(current, /^\d{3}[A-Z]$/.test(current) ? `SHARPAL ${current}` : current);
    try { localStorage.setItem(storageKey, current); } catch { /* URL-only operation is supported. */ }
    if (typeof state !== "undefined") state.product = current;
    renderProducts();
    if (previous !== current) window.dispatchEvent(new CustomEvent("workbench:productchange", { detail: { product: current } }));
  }
  function canonicalize() {
    const parsed = parse();
    rememberProduct(parsed.product);
    const next = format(parsed);
    if (location.hash !== next) history.replaceState(null, "", next);
    return parsed;
  }
  function registerProducts(values = []) {
    for (const value of values) {
      const code = productOf(value);
      if (code && !products.has(code)) products.set(code, /^\d{3}[A-Z]$/.test(code) ? `SHARPAL ${code}` : code);
    }
    renderProducts();
  }
  function renderProducts() {
    const picker = document.querySelector("#global-product");
    if (!picker || typeof esc !== "function") return;
    const entries = [...products].sort(([a], [b]) => a.localeCompare(b, "zh-CN"));
    const markup = `<option value="">全部产品</option>${entries.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}`;
    if (picker.innerHTML !== markup) picker.innerHTML = markup;
    picker.value = current;
  }
  function setProduct(value) {
    const parsed = parse();
    parsed.product = normalizeProduct(value);
    if (parsed.page === "case") { parsed.page = "library"; parsed.tail = []; }
    if (["ai", "scripts", "production", "directions"].includes(parsed.page)) parsed.tail = [];
    if (parsed.page === "library" && typeof navigationItem === "function") {
      const view = navigationItem(parsed.params.get("view") || "reference-all");
      if (view?.filters) {
        if (view.filters.baseView === "reference-all") parsed.params.delete("view");
        else parsed.params.set("view", view.filters.baseView);
      }
    }
    ["direction", "point", "selling_point", "video", "sku"].forEach(key => parsed.params.delete(key));
    location.hash = format(parsed);
  }
  async function chooseWritingCase() {
    let dialog = document.querySelector("#writing-case-dialog");
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "writing-case-dialog";
      dialog.setAttribute("aria-labelledby", "writing-case-title");
      document.body.append(dialog);
    }
    const cases = state.cases.filter(item => !item.archived && matchesProduct(item));
    dialog.innerHTML = `<div class="dialog-heading"><h2 id="writing-case-title">选择案例写稿</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div><div class="writing-case-list">${cases.length ? cases.map(item => `<button type="button" class="writing-case-option" data-case-id="${esc(item.case_id)}"><strong>${esc(shortTitle(item))}</strong><small>${esc(item.current?.test_direction?.direction_id || "尚未记录方向")}</small></button>`).join("") : '<p>当前产品还没有案例，请先在案例库收录参考。</p>'}</div>`;
    dialog.querySelector("[data-close]").onclick = () => dialog.close();
    dialog.querySelectorAll("[data-case-id]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { dialog.close(); await window.CaseAI?.startWriting(button.dataset.caseId); }
      catch (error) { toast(error.message, true); }
      finally { button.disabled = false; }
    });
    dialog.showModal();
  }
  async function editCurrentCase() {
    const id = parse().tail[0], button = document.querySelector("#add-top");
    if (!id || state.editor) return;
    button.disabled = true;
    try {
      const detail = await api(`/api/cases/${encodeURIComponent(id)}`);
      if (parse().page !== "case" || parse().tail[0] !== id || state.editor) return;
      state.detail = detail;
      if (window.CaseInlineEditor) window.CaseInlineEditor.open(detail, "overview");
      else openEditor(detail);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }
  // The blue top-bar button only creates or starts something, and a module keeps the same one on all its tabs.
  // Sync, refresh and check actions sit in each page's own header, next to what they update.
  const actions = {
    home: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
    library: ["收录案例", "plus", () => openEditor(null)],
    case: ["用这个案例写稿", "spark", () => { const id = state.detail?.case_id; if (id) void window.CaseAI?.startWriting(id); else editCurrentCase(); }],
    directions: ["新建方向", "plus", () => window.CaseDirections?.openCreate()],
    ai: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
    scripts: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
    production: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
  };
  function initializeSidebar() {
    const sidebar = document.querySelector("#workbench-sidebar");
    const rail = document.querySelector("#sidebar-rail");
    const toggle = document.querySelector("#sidebar-toggle");
    const menu = document.querySelector("#sidebar-menu");
    const pin = document.querySelector("#sidebar-pin");
    if (!sidebar || !rail || !toggle || !menu) return;
    const root = document.documentElement;
    const preferenceKey = "workbench-sidebar-v1";
    const compactScreen = window.matchMedia("(max-width:850px)");
    const phoneScreen = window.matchMedia("(max-width:760px)");
    const shortcuts = [...rail.querySelectorAll("[data-sidebar-target]")];
    const connection = sidebar.querySelector("#connection-status");
    let preference = null, collapsed = false, peek = false, openTimer = 0, closeTimer = 0;
    let pendingTrigger = null;
    let lastTrigger = menu, ignoreFocus = false, keyboardMode = false;
    const supportsSelectOpen = CSS.supports("selector(select:open)");
    try {
      const saved = localStorage.getItem(preferenceKey);
      if (["collapsed", "expanded"].includes(saved)) preference = saved;
    } catch { /* The controls also work when browser storage is unavailable. */ }

    function syncRail() {
      for (const button of shortcuts) {
        const source = document.getElementById(button.dataset.sidebarTarget);
        if (!source) continue;
        button.hidden = source.hidden || source.dataset.publicHidden === "true";
        button.classList.toggle("active", source.classList.contains("active"));
        if (source.hasAttribute("aria-current")) button.setAttribute("aria-current", "page");
        else button.removeAttribute("aria-current");
        const label = source.getAttribute("aria-label") || source.textContent.trim();
        button.setAttribute("aria-label", label);
        button.title = label;
        const badge = button.querySelector("[data-sidebar-count]");
        const count = source.querySelector(".nav-count");
        if (badge) {
          const total = count?.textContent.trim() || "";
          badge.textContent = /^\d+$/.test(total) && Number(total) > 99 ? "99+" : total;
          badge.hidden = !count || count.hidden || !total || total === "0";
        }
      }
    }
    function renderSidebar() {
      root.classList.toggle("sidebar-collapsed", collapsed);
      root.classList.toggle("sidebar-peek", collapsed && peek);
      rail.hidden = !collapsed;
      if (pin) pin.hidden = !collapsed;
      sidebar.inert = collapsed && !peek;
      sidebar.setAttribute("aria-hidden", String(collapsed && !peek));
      toggle.setAttribute("aria-label", collapsed ? "固定边栏" : "隐藏边栏");
      toggle.title = collapsed ? "固定边栏" : "隐藏边栏";
      toggle.setAttribute("aria-expanded", String(!collapsed || peek));
      menu.setAttribute("aria-expanded", String(peek));
      sidebar.querySelectorAll(".nav-item").forEach(button => {
        button.classList.toggle("is-preview", collapsed && peek && button.id === lastTrigger.dataset.sidebarTarget);
      });
    }
    function cancelClose() { clearTimeout(closeTimer); }
    function cancelOpen() {
      clearTimeout(openTimer);
      pendingTrigger = null;
    }
    function closePeek(restoreFocus = false) {
      cancelOpen();
      cancelClose();
      if (!peek) return;
      peek = false;
      if (restoreFocus) {
        // Escape returns to the shortcut without immediately reopening the panel.
        ignoreFocus = true;
        lastTrigger.focus({ preventScroll: true });
        ignoreFocus = false;
      }
      renderSidebar();
    }
    function showPeek(trigger) {
      if (!collapsed) return;
      cancelOpen();
      cancelClose();
      lastTrigger = trigger;
      peek = true;
      renderSidebar();
    }
    function scheduleOpen(trigger) {
      if (!collapsed) return;
      cancelOpen();
      cancelClose();
      if (peek) { showPeek(trigger); return; }
      pendingTrigger = trigger;
      openTimer = setTimeout(() => {
        if (pendingTrigger === trigger && trigger.isConnected && !trigger.hidden && !document.querySelector("dialog[open]")) showPeek(trigger);
      }, 120);
    }
    function scheduleClose() {
      cancelClose();
      closeTimer = setTimeout(() => {
        const focused = document.activeElement;
        const keyboardFocus = keyboardMode && (sidebar.contains(focused) || rail.contains(focused));
        const choosingProduct = supportsSelectOpen && sidebar.contains(focused) && focused.matches("select:open");
        if (!rail.matches(":hover") && !sidebar.matches(":hover") && !keyboardFocus && !choosingProduct) closePeek();
      }, 220);
    }
    function applyPreference() {
      cancelOpen();
      cancelClose();
      // Phones keep their existing top product picker and bottom workflow tabs.
      collapsed = !phoneScreen.matches && (preference === "collapsed" || (preference === null && compactScreen.matches));
      peek = false;
      renderSidebar();
    }
    function toggleSidebar() {
      preference = collapsed ? "expanded" : "collapsed";
      try { localStorage.setItem(preferenceKey, preference); } catch { /* Keep the choice for this session. */ }
      if (collapsed) toggle.focus({ preventScroll: true });
      applyPreference();
    }
    toggle.addEventListener("click", toggleSidebar);
    pin?.addEventListener("click", toggleSidebar);
    for (const trigger of [menu, ...shortcuts]) {
      trigger.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") scheduleOpen(trigger); });
      trigger.addEventListener("pointerleave", () => { if (pendingTrigger === trigger) cancelOpen(); });
      trigger.addEventListener("focus", () => { if (!ignoreFocus) showPeek(trigger); });
    }
    menu.addEventListener("click", () => showPeek(menu));
    for (const button of shortcuts) button.addEventListener("click", () => {
      document.getElementById(button.dataset.sidebarTarget)?.click();
      // A modal takes over focus; do not leave an extra panel behind it.
      if (document.querySelector("dialog[open]")) closePeek();
    });
    for (const region of [rail, sidebar]) {
      // Keep the two columns on the same rows when a short window needs scrolling.
      region.addEventListener("scroll", () => {
        if (!collapsed) return;
        const other = region === rail ? sidebar : rail;
        if (Math.abs(other.scrollTop - region.scrollTop) > 1) other.scrollTop = region.scrollTop;
      }, {passive:true});
      region.addEventListener("pointerenter", cancelClose);
      region.addEventListener("pointerleave", () => { cancelOpen(); scheduleClose(); });
      region.addEventListener("focusin", cancelClose);
      region.addEventListener("focusout", event => {
        if (!rail.contains(event.relatedTarget) && !sidebar.contains(event.relatedTarget)) scheduleClose();
      });
    }
    document.addEventListener("pointerdown", event => {
      keyboardMode = false;
      if (!rail.contains(event.target) && !sidebar.contains(event.target) && !toggle.contains(event.target)) closePeek();
    });
    document.addEventListener("keydown", event => {
      keyboardMode = true;
      const connectionStatus = document.querySelector("#connection-status");
      if (event.key === "Escape" && connectionStatus?.open && connectionStatus.contains(event.target)) {
        event.preventDefault();
        event.stopPropagation();
        connectionStatus.open = false;
        connectionStatus.querySelector("summary").focus();
        return;
      }
      if (event.key === "Escape" && peek && !event.defaultPrevented && !document.querySelector("dialog[open]")) {
        event.preventDefault();
        // The page's Escape shortcut must not navigate while dismissing this panel.
        event.stopPropagation();
        closePeek(true);
      }
    }, true);
    const productPicker = sidebar.querySelector("select");
    for (const event of ["change", "click", "blur"]) productPicker?.addEventListener(event, scheduleClose);
    compactScreen.addEventListener("change", applyPreference);
    phoneScreen.addEventListener("change", applyPreference);
    window.addEventListener("storage", event => {
      if (event.key !== preferenceKey) return;
      preference = ["collapsed", "expanded"].includes(event.newValue) ? event.newValue : null;
      applyPreference();
    });
    // Counts and connection-driven labels can update without a route change.
    new MutationObserver(syncRail).observe(sidebar, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["class", "hidden", "aria-label", "aria-current", "data-public-hidden"] });
    if (connection) new ResizeObserver(() => {
      // Sync details can grow; reserve that same footer space under the icon tools.
      const height = connection.getBoundingClientRect().height + parseFloat(getComputedStyle(connection).marginTop || "0");
      root.style.setProperty("--sidebar-footer-space", `${height}px`);
    }).observe(connection);
    syncRail();
    applyPreference();
  }
  function update() {
    const parsed = parse();
    document.querySelectorAll("[data-page]").forEach(button => {
      const active = button.dataset.page === (parsed.page === "case" ? "library" : ["scripts", "production"].includes(parsed.page) ? "ai" : parsed.page);
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const columns = document.querySelector("#library-navigation");
    if (columns) columns.hidden = true;
    // Task, script and order pages have their own main action (采纳、继续制作…), so the top bar does not add a second one.
    const ownAction = parsed.tail.length && ["ai", "scripts", "production"].includes(parsed.page);
    const button = document.querySelector("#add-top"), action = ownAction ? null : actions[parsed.page];
    if (button) {
      button.hidden = !action;
      if (action) { button.innerHTML = `${icon(action[1])}${action[0]}`; button.onclick = action[2]; }
    }
    const importer = document.querySelector("#import-video-top");
    if (importer) importer.hidden = !["library", "case"].includes(parsed.page);
    registerProducts(state.cases);
    renderBreadcrumb();
  }
  function initialize() {
    initializeSidebar();
    initializePageNavigation();
    const picker = document.querySelector("#global-product");
    if (picker) picker.onchange = event => setProduct(event.target.value);
    document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => { location.hash = listURL(button.dataset.page); });
    update();
    // Include products that only have sales rows or user reviews, without opening those pages.
    void api("/api/products").then(data => registerProducts(data.products || [])).catch(async error => {
      // An already-running service may still use the earlier API until its
      // current editing task finishes and it can restart safely.
      if (error.status !== 404) return;
      const results = await Promise.allSettled([api("/api/performance/videos"), api("/api/reviews/summary"), api("/api/direction-catalog"), api("/api/creation")]);
      const [videos, reviews, catalog, creation] = results;
      if (videos.status === "fulfilled") registerProducts(videos.value.videos || []);
      if (reviews.status === "fulfilled") registerProducts(reviews.value.products || []);
      if (catalog.status === "fulfilled") registerProducts([...(catalog.value.selling_points || []), ...(catalog.value.directions || [])]);
      if (creation.status === "fulfilled") registerProducts([...(creation.value.scripts || []), ...(creation.value.materials || [])]);
    });
  }
  window.WorkbenchShell = { normalizeProduct, productOf, matchesProduct, parse, url, legacyHash, canonicalize, registerProducts, setProduct, chooseWritingCase, directionParent, parentURL, listURL, listState, beginNavigation, finishNavigation, product: () => current, update, initialize };
})();
