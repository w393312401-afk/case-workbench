"use strict";

// WebKit (every iPhone browser) has no scroll anchoring: a notice that appears or disappears above the spot being
// read moves the text under the reader's eyes. Keep the element at that spot where it was.
window.keepReadingPlace = change => {
  const root = document.scrollingElement;
  if (!root || root.scrollTop <= 0 || CSS.supports?.("overflow-anchor", "auto") && getComputedStyle(root).overflowAnchor !== "none") return change();
  const hit = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 3);
  const anchor = hit?.closest("#content") ? hit : null, before = anchor?.getBoundingClientRect().top;
  const result = change();
  const shift = anchor?.isConnected ? anchor.getBoundingClientRect().top - before : 0;
  if (Math.abs(shift) >= 1) window.scrollBy(0, shift);
  return result;
};

// Product and URL context are shared by every page; no case records are changed.
(() => {
  const storageKey = "workbench-product-v1";
  let current = "202H";
  const products = new Map([["202H", "SHARPAL 202H"]]);
  const listContexts = new Map(), detailContexts = new Map();
  const navigationStorageKey = "workbench-navigation-v1";
  // The library keeps one independent filter context; other lists keep theirs per global product.
  const listMemoryKey = "workbench-list-filters-v1";
  const pageLabels = { home: "工作概览", library: "案例库", directions: "卖点与方向", videos: "带货数据", reviews: "用户声音", content: "内容列表", ai: "协作记录", scripts: "采用稿", production: "制作详情", settings: "设置" };
  let navigationRoute = null, restoringList = null;
  let breadcrumbSource = null, breadcrumbHTML = null;
  let actionSource = null, actionHTML = null;
  let productMarkup = null;
  let productCatalogVersion = 0;
  let catalogProducts = null;
  let productCatalogPending = null;
  let productDiscoveryPending = null;
  // Names removed in 管理产品 stay out of the list even when cases or videos still use them.
  let removedProducts = new Set(), deletedProducts = new Set();
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
    // Saved script links now belong to their content; identifiers and task parameters stay intact.
    if (page === "scripts") page = "content";
    // The content list owns production management; old order-detail links stay intact.
    if (page === "production" && !tail.length) {
      page = "content";
      if (["history", "all"].includes(params.get("scope"))) params.set("view", "all");
      const status = params.get("status");
      if (status && status !== "all") { params.set("view", "all"); params.set("kind", "video"); }
      else params.delete("status");
      if (params.get("archived") === "true" || params.get("archived") === "1") { params.set("view", "all"); params.set("status", "archived"); params.set("kind", "video"); }
      params.delete("scope");
      params.delete("archived");
    }
    if (!["home", "library", "case", "directions", "videos", "reviews", "content", "ai", "scripts", "production", "settings"].includes(page)) { page = "library"; tail = []; }
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
  const listKey = (page, product) => `${page}:${page === "library" ? "" : encodeURIComponent(normalizeProduct(product))}`;
  const detailKey = parsed => `${listKey(parsed.page, parsed.product)}:${parsed.tail[0] || ""}`;
  const moduleFor = page => ({case: "library"}[page] || page);
  function validListContext(saved, page, product, allowAll = false) {
    if (!saved || typeof saved.hash !== "string" || !/^#\/[^/]+\/[^/?]+(?:\?[^#]*)?$/.test(saved.hash)) return null;
    const parsed = parse(saved.hash);
    return parsed.page === page && !parsed.tail.length && (page === "library" || parsed.product === normalizeProduct(product) || (allowAll && !parsed.product))
      && format(parsed) === saved.hash && Number.isFinite(saved.scroll) && saved.scroll >= 0 ? saved : null;
  }
  function savedListContext(page, product = parse().product) {
    const saved = validListContext(listContexts.get(listKey(page, product)), page, product);
    return saved && page === "library" ? { ...saved, hash: url(saved.hash, product) } : saved;
  }
  function detailContext(parsed = parse()) {
    const saved = detailContexts.get(detailKey(parsed));
    const parent = saved && ["ai", "scripts", "production"].includes(parsed.page) && parse(saved.hash).page === "content" ? "content" : moduleFor(parsed.page);
    return validListContext(saved, parent, parsed.product, true);
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
      return typeof remembered === "string" && /^#(?:library|\/[^/]+\/library)(?:\?[^#]*)?$/.test(remembered)
        ? url(remembered) : url("#library", parsed.product);
    }
    if (parsed.page === "directions" && parsed.tail.length) return directionParent(parsed.product);
    if (parsed.page === "content" && parsed.tail.length) return detailContext(parsed)?.hash || listURL("content", parsed.product);
    if (["ai", "scripts", "production"].includes(parsed.page) && parsed.tail.length) return detailContext(parsed)?.hash || listURL(parsed.page === "ai" ? "ai" : "content", parsed.product);
    return listURL(["ai", "scripts", "production"].includes(parsed.page) ? "content" : "home", parsed.product);
  }
  function saveNavigation() {
    try { sessionStorage.setItem(navigationStorageKey, JSON.stringify({ version: 2, lists: [...listContexts].slice(-80), details: [...detailContexts].slice(-80) })); }
    catch { /* Return context still works without browser storage. */ }
    try { localStorage.setItem(listMemoryKey, JSON.stringify({ version: 1, lists: [...listContexts].slice(-80) })); }
    catch { /* Filters then last for this session only. */ }
  }
  function loadListMemory() {
    try {
      const saved = JSON.parse(localStorage.getItem(listMemoryKey) || "{}");
      if (saved?.version !== 1 || !Array.isArray(saved.lists)) return;
      for (const entry of saved.lists.slice(-80)) {
        if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== "string") continue;
        const [page, encodedProduct] = entry[0].split(":");
        let product;
        try { product = decodeURIComponent(encodedProduct || ""); } catch { continue; }
        if (!Object.hasOwn(pageLabels, moduleFor(page))) continue;
        // A reading position from an earlier visit means nothing now; the filters are what carries over.
        const context = entry[1] && { ...entry[1], scroll: 0 };
        if (validListContext(context, moduleFor(page), product)) listContexts.set(listKey(page, product), context);
      }
    } catch { /* Lists open unfiltered without browser storage. */ }
  }
  // Closing the tab on a list never goes through beginNavigation, so keep its filters here.
  function rememberCurrentList() {
    if (!navigationRoute || navigationRoute.tail.length) return;
    const saved = captureList(navigationRoute);
    if (!saved) return;
    listContexts.set(listKey(navigationRoute.page, navigationRoute.product), saved);
    saveNavigation();
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
      const module = { content: window.ContentFlow, ai: window.CaseAI, scripts: window.CaseScripts, production: window.CaseProduction, videos: window.CasePerformance, reviews: window.CaseReviews, home: window.WorkbenchHome }[parsed.page];
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
    // Opening a bare list address in a fresh page brings back the filters that list was last left with.
    if (!previous && !incoming.tail.length && ![...incoming.params].length) {
      const remembered = savedListContext(incoming.page, incoming.product);
      if (remembered && remembered.hash !== format(incoming)) {
        incoming.params = parse(remembered.hash).params;
        try { history.replaceState(null, "", remembered.hash); } catch { /* The list still opens, only unfiltered. */ }
      }
    }
    if (previous && !previous.tail.length) {
      const saved = captureList(previous);
      if (saved) {
        listContexts.set(listKey(previous.page, previous.product), saved);
        // Preserve the all-products source when a card points to a concrete product.
        if (incoming.tail.length && (moduleFor(incoming.page) === previous.page || previous.page === "content" && ["ai", "scripts", "production"].includes(incoming.page)) && (previous.page === "library" || !previous.product || incoming.product === previous.product)) detailContexts.set(detailKey(incoming), saved);
        saveNavigation();
      }
      if (previous.page !== incoming.page || incoming.tail.length || previous.product !== incoming.product) {
        ({ content: window.ContentFlow, videos: window.CasePerformance, reviews: window.CaseReviews }[previous.page])?.leaveList?.();
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
    if (["content", "ai", "scripts", "production"].includes(page)) crumbs.push({ label: "内容创作", href: listURL("content", parsed.product) });
    const parentPage = parsed.tail.length && ["content", "ai", "production"].includes(page) ? parse(parentURL(parsed)).page : page;
    crumbs.push({ label: pageLabels[parentPage] || "案例库", href: parsed.tail.length ? parentURL(parsed) : null });
    if (parsed.tail.length) {
      let label = parsed.page === "case" ? "案例详情" : page === "ai" ? "任务详情" : ["content", "scripts"].includes(page) ? "采用稿" : page === "production" ? (parsed.tail[0].startsWith("CAR-") ? "图文制作单" : "制作单详情") : parsed.tail.join("/");
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
      // Detail pages and collaboration records can return to the unified content list.
      up.hidden = !parsed.tail.length && parsed.page !== "ai";
      up.href = parentURL(parsed);
      const actualParent = parse(up.href.split('#')[1] ? '#' + up.href.split('#')[1] : parentURL(parsed));
      const parentLabel = parsed.page === "case" ? "案例库" : pageLabels[actualParent.page] || "上一级";
      up.title = `返回${parentLabel}`;
      up.setAttribute("aria-label", up.title);
      if (parsed.tail.length && actualParent.page === "content") {
        const back = document.querySelector("#content .back-link");
        if (back) {
          back.setAttribute("aria-label", "返回内容列表");
          for (const child of back.childNodes) if (child.nodeType === 3 && child.textContent.includes("返回")) child.textContent = "返回内容列表";
        }
      }
    }
  }
  function initializePageNavigation() {
    loadListMemory();
    window.addEventListener("pagehide", rememberCurrentList);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") rememberCurrentList(); });
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
            const parentPage = isDetail && ["ai", "scripts", "production"].includes(page) && parse(entry[1]?.hash).page === "content" ? "content" : moduleFor(page);
            if (validListContext(entry[1], parentPage, product, isDetail)) map.set(isDetail ? entry[0] : listKey(page, product), entry[1]);
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
    if (current && !products.has(current)) {
      products.set(current, /^\d{3}[A-Z]$/.test(current) ? `SHARPAL ${current}` : current);
      productMarkup = null;
    }
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
    let discover = false;
    for (const value of values) {
      const code = productOf(value);
      if (code && catalogProducts && !catalogProducts.has(code) && !removedProducts.has(code) && !deletedProducts.has(code)) discover = true;
      if (code && !products.has(code) && (!catalogProducts || catalogProducts.has(code)) && !removedProducts.has(code) && !deletedProducts.has(code)) {
        products.set(code, /^\d{3}[A-Z]$/.test(code) ? `SHARPAL ${code}` : code);
        productMarkup = null;
      }
    }
    renderProducts();
    // New imports can introduce products after startup. Confirm them against
    // the catalogue instead of reintroducing names from stale case records.
    if (discover && !productDiscoveryPending) {
      productDiscoveryPending = Promise.resolve().then(refreshProducts).catch(() => {}).finally(() => { productDiscoveryPending = null; });
    }
  }
  function renderProducts() {
    const picker = document.querySelector("#global-product");
    if (!picker || typeof esc !== "function") return;
    if (productMarkup === null) {
      const entries = [...products].sort(([a], [b]) => a.localeCompare(b, "zh-CN"));
      productMarkup = `<option value="">全部产品</option>${entries.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}`;
    }
    if (picker.innerHTML !== productMarkup) picker.innerHTML = productMarkup;
    picker.value = current;
    renderProductCombo();
  }
  function applyProductCatalog(data) {
    catalogProducts = new Set((data.products || []).map(normalizeProduct).filter(Boolean));
    removedProducts = new Set((data.removed || []).map(normalizeProduct));
    deletedProducts = new Set((data.deleted || []).map(normalizeProduct));
    for (const code of products.keys()) if (code !== current && !catalogProducts.has(code)) products.delete(code);
    productMarkup = null;
    registerProducts(data.products || []);
  }
  function getProductChoices() { return visibleProducts().map(value => ({ value, label: productLabel(value) })); }
  function refreshProducts() {
    if (productCatalogPending) return productCatalogPending;
    const version = productCatalogVersion;
    productCatalogPending = api("/api/products").then(data => {
      if (version === productCatalogVersion) applyProductCatalog(data);
      return getProductChoices();
    }).finally(() => { productCatalogPending = null; });
    return productCatalogPending;
  }

  // Switching is a small floating list; catalog edits have their own dialog.
  // Keep the native select for integrations that set the product directly.
  let combo = { open: false, panel: null };
  const filterPickers = new Set();
  const selectControllers = new WeakMap();
  const selectTriggerOwners = new WeakMap();
  const enhancedSelects = new Map();
  let selectSequence = 0, selectObserver = null;
  const manager = { tab: "active", busy: false, dialog: null, pendingDelete: null };
  const modelProduct = value => /^\d{3}[A-Z]$/.test(value);
  function canManageProducts() { return !window.WorkbenchGuest && !window.PublicTextMode; }
  function productLabel(value) { return value ? products.get(value) || (modelProduct(value) ? `SHARPAL ${value}` : value) : "全部产品"; }
  function sortedProducts(values) {
    return [...values].sort((a, b) => Number(modelProduct(b)) - Number(modelProduct(a)) || productLabel(a).localeCompare(productLabel(b), "zh-CN", { numeric: true }));
  }
  function visibleProducts() { return sortedProducts([...products.keys()].filter(value => value && (!catalogProducts || catalogProducts.has(value)) && !removedProducts.has(value) && !deletedProducts.has(value))); }
  function productMatchesQuery(value, query) {
    const text = `${value} ${productLabel(value)}`.toLocaleLowerCase();
    return query.toLocaleLowerCase().split(/\s+/).filter(Boolean).every(part => text.includes(part));
  }
  function renderProductCombo() {
    filterPickers.forEach(picker => picker.refresh());
    if (manager.dialog?.open) renderProductManager();
  }
  function openProductCombo(open, restoreFocus = false) {
    combo.setOpen?.(open, restoreFocus);
  }
  function managerMessage(text = "", error = false) {
    const dialog = manager.dialog;
    for (const [id, message] of [["product-manager-error", error ? text : ""], ["product-manager-status", error ? "" : text]]) {
      const node = dialog.querySelector(`#${id}`);
      node.textContent = message;
      node.hidden = !message;
    }
  }
  function renderProductManager() {
    const dialog = manager.dialog;
    if (!dialog || !dialog.open) return;
    const query = dialog.querySelector("[data-product-manager-search]").value.trim();
    const active = visibleProducts(), hidden = sortedProducts(removedProducts);
    for (const [tab, label, values] of [["active", "使用中", active], ["hidden", "已隐藏", hidden]]) {
      const button = dialog.querySelector(`[data-product-tab="${tab}"]`);
      button.textContent = `${label} ${values.length}`;
      button.setAttribute("aria-pressed", String(manager.tab === tab));
    }
    const values = (manager.tab === "hidden" ? hidden : active).filter(value => productMatchesQuery(value, query));
    const list = dialog.querySelector("[data-product-manager-list]");
    const action = manager.tab === "hidden" ? "restore" : "remove";
    const markup = values.length ? values.map(value => `<li data-product-row="${esc(value)}"><div class="product-manager-name"><span title="${esc(productLabel(value))}">${esc(productLabel(value))}</span>${value === current ? '<small>当前产品</small>' : ""}</div><div class="product-manager-actions"><button type="button" class="secondary compact" data-${action}="${esc(value)}" aria-label="${action === "restore" ? "恢复" : "隐藏"} ${esc(productLabel(value))}"${manager.busy ? " disabled" : ""}>${action === "restore" ? "恢复" : "隐藏"}</button><button type="button" class="product-manager-delete" data-delete="${esc(value)}" aria-label="删除 ${esc(productLabel(value))}"${manager.busy ? " disabled" : ""}>删除</button></div></li>`).join("") : `<li class="product-manager-empty">${query ? "没有找到匹配的产品" : manager.tab === "hidden" ? "暂无已隐藏的产品" : "还没有产品，可以在上方新增"}</li>`;
    if (list.innerHTML !== markup) list.innerHTML = markup;
    dialog.querySelector("[data-product-add]").disabled = manager.busy;
    dialog.querySelector("[data-product-add]").textContent = manager.busy ? "处理中…" : "新增产品";
    dialog.querySelector("[data-product-add-input]").disabled = manager.busy;
    list.setAttribute("aria-busy", String(manager.busy));
    dialog.querySelector("[data-product-manager-bulk]").hidden = manager.tab !== "hidden" || !hidden.length;
    const bulk = dialog.querySelector("[data-product-delete-hidden]");
    bulk.textContent = `删除全部隐藏（${hidden.length}）`;
    bulk.disabled = manager.busy;
    const confirmation = dialog.querySelector("[data-product-delete-confirmation]");
    confirmation.hidden = !manager.pendingDelete;
    if (manager.pendingDelete) {
      const { action, product } = manager.pendingDelete;
      const title = confirmation.querySelector("#product-delete-title");
      const label = productLabel(product);
      title.textContent = action === "delete_hidden" ? `删除全部 ${hidden.length} 个已隐藏产品？包括当前搜索未显示的产品。` : `从产品列表删除「${shortProductLabel(label)}」？`;
      title.title = action === "delete_hidden" ? "" : label;
      confirmation.querySelector("[data-product-delete-confirm]").disabled = manager.busy;
      confirmation.querySelector("[data-product-delete-cancel]").disabled = manager.busy;
      confirmation.querySelector("[data-product-delete-confirm]").textContent = manager.busy ? "删除中…" : "确认删除";
    }
  }
  function shortProductLabel(label) {
    const letters = Array.from(label);
    return letters.length > 80 ? letters.slice(0, 80).join("") + "…" : label;
  }
  function requestProductDeletion(action, product) {
    if (manager.busy) return;
    manager.pendingDelete = { action, product };
    managerMessage();
    renderProductManager();
    const confirmation = manager.dialog.querySelector("[data-product-delete-confirmation]");
    confirmation.scrollIntoView({ block: "nearest" });
    confirmation.querySelector("[data-product-delete-cancel]").focus({ preventScroll: true });
  }
  async function changeProductCatalog(action, product) {
    if (manager.busy || !canManageProducts()) return;
    const dialog = manager.dialog, list = dialog.querySelector("[data-product-manager-list]");
    const focusIndex = [...list.querySelectorAll("button")].indexOf(document.activeElement);
    const label = shortProductLabel(productLabel(normalizeProduct(product)));
    manager.busy = true;
    managerMessage();
    renderProductManager();
    let succeeded = false;
    try {
      const data = await api("/api/products/catalog", { method: "POST", body: { action, product } });
      productCatalogVersion += 1;
      applyProductCatalog(data);
      manager.pendingDelete = null;
      if (action === "add") {
        dialog.querySelector("[data-product-add-input]").value = "";
        dialog.querySelector("[data-product-manager-search]").value = "";
        manager.tab = "active";
      }
      const messages = {
        add: `已新增「${productLabel(data.product)}」`,
        remove: `已隐藏「${label}」，可在「已隐藏」中恢复`,
        restore: `已恢复「${label}」`,
        delete: `已删除产品列表项「${label}」，案例保持不变`,
        delete_hidden: `已删除 ${data.deleted_count || 0} 个已隐藏产品列表项，案例保持不变`,
      };
      managerMessage(messages[action]);
      succeeded = true;
    } catch (error) {
      managerMessage(error.message || "暂时无法保存，请重试。", true);
    } finally {
      manager.busy = false;
      renderProductManager();
      if (dialog.open) {
        const buttons = [...list.querySelectorAll("button")];
        const target = action === "add" ? dialog.querySelector("[data-product-add-input]") : buttons[Math.min(Math.max(0, focusIndex), buttons.length - 1)] || dialog.querySelector("[data-product-manager-search]");
        target.focus({ preventScroll: true });
      }
    }
    return succeeded;
  }
  function openProductManager() {
    if (!canManageProducts()) return;
    const query = combo.panel.querySelector(".product-combo-search").value.trim();
    openProductCombo(false);
    let dialog = manager.dialog;
    if (!dialog) {
      dialog = document.createElement("dialog");
      dialog.id = "product-manager-dialog";
      dialog.className = "product-manager";
      dialog.setAttribute("aria-labelledby", "product-manager-title");
      dialog.innerHTML = `<header class="product-manager-heading"><div><h2 id="product-manager-title">产品型号管理</h2><p>隐藏和删除仅作用于产品列表，不改动案例、视频和脚本。</p></div><button type="button" class="icon-button" data-product-manager-close aria-label="关闭产品管理">${icon("close")}</button></header><div class="product-manager-add"><label for="product-manager-add-input">新增产品</label><div><input id="product-manager-add-input" data-product-add-input maxlength="120" placeholder="输入型号或名称，如 198H" autocomplete="off"><button type="button" class="primary" data-product-add>新增产品</button></div></div><div class="product-manager-filter"><div class="product-manager-tabs" role="group" aria-label="产品显示状态"><button type="button" data-product-tab="active" aria-pressed="true"></button><button type="button" data-product-tab="hidden" aria-pressed="false"></button></div><input type="search" data-product-manager-search aria-label="搜索管理列表" placeholder="搜索型号或产品名称" autocomplete="off"></div><div class="product-manager-bulk" data-product-manager-bulk hidden><span>已隐藏的产品可恢复，也可删除列表项</span><button type="button" class="product-manager-delete" data-product-delete-hidden>删除全部隐藏</button></div><div class="product-manager-confirm" data-product-delete-confirmation role="group" aria-labelledby="product-delete-title" hidden><strong id="product-delete-title"></strong><p>仅删除产品列表项，案例及其归属保持不变。删除后不再显示在「使用中」或「已隐藏」列表。</p><div><button type="button" class="secondary compact" data-product-delete-cancel>取消</button><button type="button" class="danger compact" data-product-delete-confirm>确认删除</button></div></div><p id="product-manager-error" role="alert" hidden></p><p id="product-manager-status" role="status" hidden></p><ul class="product-manager-list" data-product-manager-list aria-label="产品管理列表"></ul><footer class="product-manager-foot"><span>仅管理产品列表，案例不受影响</span><button type="button" class="secondary" data-product-manager-close>完成</button></footer>`;
      document.body.append(dialog);
      manager.dialog = dialog;
      dialog.querySelectorAll("[data-product-manager-close]").forEach(button => button.addEventListener("click", () => dialog.close()));
      dialog.querySelector("[data-product-manager-search]").addEventListener("input", renderProductManager);
      dialog.querySelectorAll("[data-product-tab]").forEach(button => button.addEventListener("click", () => {
        if (manager.busy) return;
        manager.pendingDelete = null;
        manager.tab = button.dataset.productTab;
        renderProductManager();
      }));
      const add = dialog.querySelector("[data-product-add-input]");
      const submit = () => {
        if (manager.busy) return;
        const value = add.value.trim(), code = normalizeProduct(value);
        const same = values => [...values].some(item => item.toLocaleLowerCase() === code.toLocaleLowerCase());
        if (!code) { managerMessage("请填写具体的产品型号或名称。", true); add.focus(); return; }
        if (same(removedProducts)) { managerMessage("该产品已隐藏，请在「已隐藏」中恢复。", true); return; }
        if (same(products.keys()) && !same(deletedProducts)) { managerMessage("该产品已存在，无需重复新增。", true); return; }
        void changeProductCatalog("add", value);
      };
      dialog.querySelector("[data-product-add]").addEventListener("click", submit);
      add.addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.isComposing && event.keyCode !== 229) { event.preventDefault(); submit(); }
      });
      dialog.querySelector("[data-product-manager-list]").addEventListener("click", event => {
        const deletion = event.target.closest("[data-delete]");
        if (deletion) { requestProductDeletion("delete", deletion.dataset.delete); return; }
        const button = event.target.closest("[data-remove],[data-restore]");
        if (button) void changeProductCatalog(button.hasAttribute("data-remove") ? "remove" : "restore", button.dataset.remove || button.dataset.restore);
      });
      dialog.querySelector("[data-product-delete-hidden]").addEventListener("click", () => requestProductDeletion("delete_hidden"));
      dialog.querySelector("[data-product-delete-cancel]").addEventListener("click", () => {
        const pending = manager.pendingDelete;
        manager.pendingDelete = null;
        renderProductManager();
        const trigger = pending?.action === "delete_hidden" ? dialog.querySelector("[data-product-delete-hidden]") : [...dialog.querySelectorAll("[data-delete]")].find(button => button.dataset.delete === pending?.product);
        (trigger || dialog.querySelector("[data-product-manager-search]")).focus({ preventScroll: true });
      });
      dialog.querySelector("[data-product-delete-confirm]").addEventListener("click", () => {
        if (manager.pendingDelete) void changeProductCatalog(manager.pendingDelete.action, manager.pendingDelete.product);
      });
      dialog.addEventListener("close", () => {
        const collapsed = document.documentElement.classList.contains("sidebar-collapsed") && !document.documentElement.classList.contains("sidebar-peek");
        document.querySelector(collapsed ? "#sidebar-menu" : ".product-combo-trigger")?.focus({ preventScroll: true });
      });
    }
    manager.tab = "active";
    manager.pendingDelete = null;
    dialog.querySelector("[data-product-manager-search]").value = "";
    dialog.querySelector("[data-product-add-input]").value = query;
    managerMessage();
    dialog.showModal();
    renderProductManager();
    dialog.querySelector(query ? "[data-product-add-input]" : "[data-product-manager-search]").focus();
  }
  // All floating filters share focus, keyboard navigation and viewport placement.
  // Keep each select as an integration point, with the visible list in the top layer.
  function createFilterPicker(picker, options = {}) {
    if (selectControllers.has(picker)) return selectControllers.get(picker);
    const prefix = options.prefix || `filter-picker-${++selectSequence}`, listId = `${prefix}-options`;
    const title = options.title || "选择筛选项", label = options.label || title;
    const value = options.getValue || (() => picker.value);
    const choices = options.getChoices || (() => [...picker.options].filter(row => !row.hidden && !row.closest("optgroup[hidden]")).map(row => ({ value: row.value, label: row.label, disabled: row.disabled || !!row.closest("optgroup[disabled]"), group: row.closest("optgroup")?.label || "" })));
    const choiceLabel = options.getLabel || (code => choices().find(row => row.value === code)?.label || code);
    let opened = false, active = -1, rows = [], destroyed = false, invalid = false, resetTimer;
    const listeners = new AbortController();
    const listen = (target, type, callback, settings = {}) => target?.addEventListener(type, callback, { ...settings, signal: listeners.signal });
    const original = { tabIndex: picker.getAttribute("tabindex"), ariaHidden: picker.getAttribute("aria-hidden") };
    const classes = name => `${prefix}${name ? `-${name}` : ""} product-picker${name ? `-${name}` : ""}`;
    const root = document.createElement(options.preserveNative ? "span" : "div");
    root.className = classes("");
    root.innerHTML = `<button type="button" class="${classes("trigger")}" aria-haspopup="listbox" aria-controls="${listId}" aria-expanded="false"><span></span><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>`;
    if (options.preserveNative) {
      root.classList.add("workbench-select");
      root.querySelector("button").dataset.selectTrigger = "";
      root.querySelector("button").tabIndex = original.tabIndex === null ? 0 : Number(original.tabIndex);
      selectTriggerOwners.set(root.querySelector("button"), picker);
      root.insertAdjacentHTML("beforeend", '<span class="product-picker-validation" aria-live="polite" aria-atomic="true" hidden></span>');
    }
    picker.after(root);
    picker.classList.add(`${prefix}-native`, "product-picker-native");
    picker.tabIndex = -1;
    picker.setAttribute("aria-hidden", "true");
    const panel = document.createElement("div");
    panel.className = classes("panel");
    if (options.preserveNative) panel.classList.add("workbench-select-panel");
    panel.setAttribute("popover", "manual");
    panel.hidden = true;
    panel.innerHTML = `<div class="${classes("heading")}"><strong>${esc(title)}</strong><span data-picker-result-count${options.product ? " data-product-result-count" : ""} role="status"></span></div><div class="${classes("search-wrap")}">${icon("search")}<input type="search" class="${classes("search")}" role="combobox" aria-expanded="false" aria-autocomplete="list" aria-controls="${listId}" placeholder="${esc(options.searchPlaceholder || "搜索选项")}" aria-label="${esc(options.searchLabel || "搜索筛选项")}" autocomplete="off"></div><ul id="${listId}" class="${classes("list")}" role="listbox" aria-label="${esc(options.listLabel || label)}"></ul><div class="${classes("foot")}"><span>↑↓ 选择 · Enter 确认</span>${options.manage ? `<button type="button" class="${classes("link")}" data-combo-manage>管理产品</button>` : '<small>Esc 收起</small>'}</div>`;
    const ownerDialog = picker.closest("dialog");
    (ownerDialog || document.body).append(panel);
    const trigger = root.querySelector("button"), search = panel.querySelector("input"), list = panel.querySelector("ul");
    function setActive(index, scroll = false) {
      active = index;
      const items = [...list.querySelectorAll('[data-picker-value]:not([aria-disabled="true"])')];
      items.forEach((row, i) => row.classList.toggle("is-active", i === index));
      const item = items[index];
      if (item) {
        search.setAttribute("aria-activedescendant", item.id);
        if (scroll) item.scrollIntoView({ block: "nearest" });
      } else search.removeAttribute("aria-activedescendant");
    }
    function renderList(reset = false) {
      const query = search.value.trim(), selected = value();
      const parts = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
      const entries = choices().filter(row => (options.preserveNative || row.value) && parts.every(part => `${row.value} ${row.label}`.toLocaleLowerCase().includes(part)));
      const codes = entries.map(row => row.value);
      const ordered = options.preserveNative ? codes : codes.includes(selected) ? [selected, ...codes.filter(code => code !== selected)] : codes;
      const previous = rows[active];
      const visible = options.preserveNative || query ? ordered : [...(choices().some(row => row.value === "") ? [""] : []), ...ordered];
      const disabled = new Set(choices().filter(row => row.disabled).map(row => row.value));
      rows = visible.filter(code => !disabled.has(code));
      let group = "", markup = "";
      visible.forEach((code, index) => {
        const heading = options.group?.(code, selected) || (options.preserveNative ? entries.find(row => row.value === code)?.group : "") || "";
        if (heading && heading !== group) markup += `<li class="${classes("group")}" role="presentation">${heading}</li>`;
        group = heading;
        markup += `<li id="${prefix}-option-${index}" role="option" class="${classes("option")}${code === selected ? " is-current" : ""}" data-picker-value="${esc(code)}"${options.product ? ` data-product="${esc(code)}"` : ""} aria-selected="${code === selected}"${disabled.has(code) ? ' aria-disabled="true"' : ""} title="${esc(choiceLabel(code))}"><span>${esc(choiceLabel(code))}</span><span class="${classes("check")}" aria-hidden="true">${code === selected ? "✓" : ""}</span></li>`;
      });
      if (!markup) markup = `<li class="${classes("empty")}" role="presentation">${esc(options.emptyText || "没有找到匹配的选项")}<br><small>${esc(options.emptyHint || "试试其他关键词")}</small></li>`;
      if (list.innerHTML !== markup) list.innerHTML = markup;
      panel.querySelector("[data-picker-result-count]").textContent = options.countLabel?.(entries.length) || `${entries.length} 个选项`;
      const manage = panel.querySelector("[data-combo-manage]");
      if (manage) manage.hidden = !canManageProducts();
      let index = reset ? rows.indexOf(query ? rows[0] : selected) : rows.indexOf(previous);
      if (index < 0 && rows.length) index = 0;
      setActive(index, reset);
    }
    function position() {
      if (!opened) return;
      const box = trigger.getBoundingClientRect(), viewport = window.visualViewport;
      const left = viewport?.offsetLeft || 0, top = viewport?.offsetTop || 0;
      const width = viewport?.width || innerWidth, height = viewport?.height || innerHeight;
      panel.style.width = `${Math.min(360, width - 24)}px`;
      panel.style.left = `${Math.max(left + 12, Math.min(box.left, left + width - panel.offsetWidth - 12))}px`;
      const below = top + height - box.bottom - 20;
      const above = box.top - top - 20;
      // A short viewport (or the on-screen keyboard) needs room for full options.
      if (Math.max(above, below) < 260) {
        panel.style.maxHeight = `${Math.max(0, height - 24)}px`;
        panel.style.top = `${top + 12}px`;
        return;
      }
      const upward = below < 220 && box.top > top + height / 2;
      panel.style.maxHeight = `${Math.max(0, upward ? above : below)}px`;
      panel.style.top = `${upward ? Math.max(top + 12, box.top - panel.offsetHeight - 8) : box.bottom + 8}px`;
    }
    function refresh() {
      if (destroyed) return;
      const selected = value(), entries = choices().map(row => [row.value, row.label]);
      // A saved historical filter can still be displayed without adding it to the catalogue.
      if (selected && !entries.some(([code]) => code === selected)) entries.push([selected, choiceLabel(selected)]);
      if (!options.preserveNative) {
        const markup = entries.map(([code, text]) => `<option value="${esc(code)}">${esc(text)}</option>`).join("");
        if (picker.innerHTML !== markup) picker.innerHTML = markup;
        picker.value = selected;
      }
      const unavailable = picker.hidden || picker.dataset.guestHidden === "true" || picker.dataset.publicHidden === "true" || getComputedStyle(picker).display === "none";
      if (root.hidden !== unavailable) root.hidden = unavailable;
      if (trigger.disabled !== picker.matches(":disabled")) trigger.disabled = picker.matches(":disabled");
      trigger.querySelector("span").textContent = choiceLabel(selected);
      // A phone's top bar has room for the model only; the full name stays in the title and label.
      if (options.getShortLabel) trigger.querySelector("span").dataset.short = options.getShortLabel(selected);
      trigger.title = choiceLabel(selected);
      const controlLabel = options.getControlLabel?.() || label;
      trigger.setAttribute("aria-label", `${controlLabel}，当前：${choiceLabel(selected)}`);
      if (options.preserveNative) {
        panel.querySelector("strong").textContent = controlLabel;
        search.placeholder = `搜索${controlLabel}`;
        search.setAttribute("aria-label", `搜索${controlLabel}`);
        list.setAttribute("aria-label", controlLabel);
        for (const attribute of ["aria-describedby", "aria-details"]) {
          const description = picker.getAttribute(attribute);
          if (description) trigger.setAttribute(attribute, description); else trigger.removeAttribute(attribute);
        }
        trigger.setAttribute("aria-required", String(picker.required));
        if (invalid && picker.validity.valid) invalid = false;
        trigger.setAttribute("aria-invalid", String(invalid));
        const message = root.querySelector(".product-picker-validation");
        message.hidden = !invalid;
        if (invalid) message.textContent = picker.validationMessage || "请选择一项";
      }
      if (opened && (unavailable || trigger.disabled || !root.getClientRects().length || picker.parentElement?.closest('[hidden],[inert],[data-guest-hidden="true"],[data-public-hidden="true"]'))) setOpen(false);
      if (opened) { renderList(); position(); }
    }
    function setOpen(open, restoreFocus = false) {
      if (destroyed) return;
      if (open) {
        refresh();
        if (!picker.isConnected || trigger.disabled || root.hidden || !root.getClientRects().length || picker.parentElement?.closest('[hidden],[inert],[data-guest-hidden="true"],[data-public-hidden="true"]')) return;
      }
      if (open) filterPickers.forEach(other => { if (other !== controller && other.open) other.setOpen(false); });
      opened = open;
      root.classList.toggle("is-open", open);
      trigger.setAttribute("aria-expanded", String(open));
      search.setAttribute("aria-expanded", String(open));
      if (open) {
        panel.hidden = false;
        panel.showPopover();
        search.value = "";
        renderList(true); position();
        search.focus({ preventScroll: true });
      } else {
        if (panel.matches(":popover-open")) panel.hidePopover();
        panel.hidden = true;
        search.removeAttribute("aria-activedescendant");
        if (restoreFocus) trigger.focus({ preventScroll: true });
        if (options.manage) window.dispatchEvent(new Event("workbench:productpickerclose"));
      }
    }
    function choose(code) {
      if (picker.matches(":disabled") || choices().find(row => row.value === code)?.disabled) return;
      setOpen(false, true);
      if (code === value()) return;
      picker.value = code;
      picker.dispatchEvent(new Event("input", { bubbles: true }));
      picker.dispatchEvent(new Event("change", { bubbles: true }));
      refresh();
    }
    function destroy() {
      if (destroyed) return;
      setOpen(false);
      destroyed = true;
      clearTimeout(resetTimer);
      listeners.abort();
      filterPickers.delete(controller);
      selectControllers.delete(picker);
      picker.classList.remove(`${prefix}-native`, "product-picker-native");
      for (const [name, saved] of [["tabindex", original.tabIndex], ["aria-hidden", original.ariaHidden]]) {
        if (saved === null) picker.removeAttribute(name); else picker.setAttribute(name, saved);
      }
      root.remove(); panel.remove();
    }
    const controller = { get open() { return opened; }, root, trigger, panel, ownerDialog, refresh, setOpen, destroy };
    filterPickers.add(controller);
    selectControllers.set(picker, controller);
    listen(trigger, "click", () => setOpen(!opened));
    listen(trigger, "keydown", event => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) { event.preventDefault(); setOpen(true); }
    });
    listen(search, "input", () => renderList(true));
    listen(search, "keydown", event => {
      if (event.isComposing || event.keyCode === 229) return;
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const index = event.key === "Home" ? 0 : event.key === "End" ? rows.length - 1 : active + (event.key === "ArrowDown" ? 1 : -1);
        setActive(Math.max(0, Math.min(rows.length - 1, index)), true);
      } else if (event.key === "Enter" && rows[active] !== undefined) {
        event.preventDefault();
        choose(rows[active]);
      }
    });
    // Non-focusable options must not send focus back to the owning dialog
    // before their click is delivered (which would close the floating list).
    listen(list, "pointerdown", event => {
      if (event.target.closest("[data-picker-value]")) event.preventDefault();
    });
    listen(list, "click", event => {
      const row = event.target.closest("[data-picker-value]");
      if (row) choose(row.dataset.pickerValue);
    });
    if (options.manage) listen(panel.querySelector("[data-combo-manage]"), "click", options.manage);
    listen(document, "keydown", event => {
      if (event.key === "Escape" && opened && !event.isComposing) {
        event.preventDefault(); event.stopPropagation(); setOpen(false, true);
      }
    }, { capture: true });
    listen(document, "pointerdown", event => { if (opened && !root.contains(event.target) && !panel.contains(event.target)) setOpen(false); });
    listen(document, "focusin", event => { if (opened && !root.contains(event.target) && !panel.contains(event.target)) setOpen(false); });
    listen(picker, "change", refresh);
    if (options.preserveNative) {
      listen(picker, "focus", () => trigger.focus());
      listen(picker, "click", event => { event.preventDefault(); trigger.focus(); });
    }
    listen(picker, "invalid", event => {
      if (!options.preserveNative) return;
      event.preventDefault(); invalid = true; refresh(); trigger.focus();
    });
    listen(picker.form, "reset", () => {
      clearTimeout(resetTimer);
      resetTimer = setTimeout(() => { invalid = false; refresh(); }, 0);
    });
    listen(ownerDialog, "close", () => setOpen(false));
    listen(window, "resize", position);
    listen(window.visualViewport, "resize", position);
    listen(window.visualViewport, "scroll", position);
    listen(document, "scroll", event => { if (opened && !panel.contains(event.target)) position(); }, { capture: true });
    listen(window, "hashchange", () => { if (opened) setOpen(false); });
    refresh();
    return controller;
  }
  function createProductPicker(picker, options = {}) {
    return createFilterPicker(picker, {
      prefix: "product-combo", title: "切换产品", product: true,
      getChoices: () => [{ value: "", label: "全部产品" }, ...getProductChoices()],
      getLabel: productLabel,
      getShortLabel: value => modelProduct(value) ? value : productLabel(value),
      group: (code, selected) => !code ? "" : code === selected ? "当前产品" : modelProduct(code) ? "产品型号" : "其他产品",
      searchPlaceholder: "搜索型号或产品名称", searchLabel: "搜索产品", listLabel: "产品",
      emptyText: "没有找到匹配的产品", emptyHint: "试试型号或名称中的关键词",
      countLabel: count => `${count} 个产品`, ...options,
    });
  }
  function mountProductCombo() {
    const picker = document.querySelector("#global-product");
    if (!picker || document.querySelector(".product-combo")) return;
    combo = createProductPicker(picker, { getValue: () => current, manage: openProductManager });
    renderProductCombo();
  }
  function selectLabel(picker) {
    const explicit = picker.getAttribute("aria-label") || (picker.getAttribute("aria-labelledby") || "").split(/\s+/).map(id => document.getElementById(id)?.textContent || "").join(" ").trim();
    if (explicit) return explicit;
    for (const node of picker.labels || []) {
      const copy = node.cloneNode(true);
      copy.querySelectorAll("select,input,textarea,button,small,.product-picker").forEach(child => child.remove());
      const text = copy.textContent.replace(/\s+/g, " ").replace(/[：:*＊]\s*$/, "").trim();
      if (text) return text.slice(0, 60);
    }
    const names = { "production-status": "制作状态", "production-scope": "制作范围", "sort-filter": "排序", "sort-direction": "排序顺序", "source-type-filter": "资料类型" };
    return names[picker.id] || picker.title || "选择选项";
  }
  function enhanceSelects(scope = document) {
    const candidates = [...(scope.matches?.("select") ? [scope] : []), ...scope.querySelectorAll("select")];
    for (const picker of candidates) {
      if (!picker.isConnected || picker.multiple || picker.size > 1 || selectControllers.has(picker)) continue;
      const computed = getComputedStyle(picker), box = picker.getBoundingClientRect();
      const fontSize = computed.fontSize, controlHeight = Math.max(30, Math.min(42, parseFloat(computed.height) || 38));
      const parentStyle = getComputedStyle(picker.parentElement);
      const controller = createFilterPicker(picker, {
        prefix: `workbench-select-${++selectSequence}`, preserveNative: true,
        title: selectLabel(picker), getControlLabel: () => selectLabel(picker),
        getLabel: code => [...picker.options].find(row => row.value === code)?.label || code,
      });
      if (box.width > 0) controller.root.style.setProperty("--select-width", `${Math.ceil(box.width)}px`);
      if (parentStyle.display.includes("flex") && parentStyle.flexDirection.startsWith("row")) controller.root.classList.add("is-inline");
      if (picker.parentElement.matches("label")) picker.parentElement.classList.add("workbench-select-label");
      controller.root.style.setProperty("--select-font-size", fontSize);
      controller.root.style.setProperty("--select-height", `${controlHeight}px`);
      const restorers = [], selectedOptions = new Map();
      function watchProperty(node, name, prototype) {
        const own = Object.getOwnPropertyDescriptor(node, name), descriptor = own || Object.getOwnPropertyDescriptor(prototype, name);
        if (!descriptor?.get || !descriptor.set || own?.configurable === false) return () => {};
        const setter = function (next) { descriptor.set.call(this, next); controller.refresh(); };
        Object.defineProperty(node, name, { configurable: true, enumerable: descriptor.enumerable, get() { return descriptor.get.call(this); }, set: setter });
        return () => { if (Object.getOwnPropertyDescriptor(node, name)?.set === setter) { if (own) Object.defineProperty(node, name, own); else delete node[name]; } };
      }
      function watchOptions() {
        for (const [option, restore] of selectedOptions) if (!picker.contains(option)) { restore(); selectedOptions.delete(option); }
        for (const option of picker.options) if (!selectedOptions.has(option)) selectedOptions.set(option, watchProperty(option, "selected", HTMLOptionElement.prototype));
      }
      for (const name of ["value", "selectedIndex"]) restorers.push(watchProperty(picker, name, HTMLSelectElement.prototype));
      const ownFocus = Object.getOwnPropertyDescriptor(picker, "focus");
      if (ownFocus?.configurable !== false) {
        const focus = settings => controller.trigger.focus(settings);
        Object.defineProperty(picker, "focus", { configurable: true, value: focus });
        restorers.push(() => { if (picker.focus === focus) { if (ownFocus) Object.defineProperty(picker, "focus", ownFocus); else delete picker.focus; } });
      }
      watchOptions();
      enhancedSelects.set(picker, {
        controller,
        refresh() { watchOptions(); controller.refresh(); },
        destroy() { restorers.forEach(restore => restore()); selectedOptions.forEach(restore => restore()); controller.destroy(); enhancedSelects.delete(picker); },
      });
    }
  }
  function initializeSelects() {
    if (selectObserver) return;
    enhanceSelects();
    selectObserver = new MutationObserver(records => {
      const pending = new Set(), added = new Set();
      let removed = false;
      for (const record of records) {
        const target = record.target.nodeType === Node.ELEMENT_NODE ? record.target : record.target.parentElement;
        if (!target || target.closest(".product-picker,.product-picker-panel")) continue;
        const picker = target.closest("select");
        if (picker) pending.add(picker);
        if (record.type === "childList") {
          for (const node of record.addedNodes) if (node.nodeType === Node.ELEMENT_NODE) added.add(node);
          if ([...record.removedNodes].some(node => node.nodeType === Node.ELEMENT_NODE)) removed = true;
        } else if (!picker) {
          for (const select of target.querySelectorAll("select")) pending.add(select);
        }
      }
      if (removed) for (const [picker, enhanced] of enhancedSelects) {
        if (!picker.isConnected || picker.nextElementSibling !== enhanced.controller.root || picker.closest("dialog") !== enhanced.controller.ownerDialog) {
          enhanced.destroy();
          if (picker.isConnected) added.add(picker);
        }
      }
      for (const node of added) if (node.isConnected) enhanceSelects(node);
      for (const picker of pending) {
        const enhanced = enhancedSelects.get(picker);
        if (!enhanced) { enhanceSelects(picker); continue; }
        if (picker.multiple || picker.size > 1) enhanced.destroy();
        else enhanced.refresh();
      }
    });
    selectObserver.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["disabled", "hidden", "required", "multiple", "size", "value", "selected", "label", "class", "style", "aria-label", "aria-labelledby", "aria-describedby", "aria-details", "data-guest-hidden", "data-public-hidden"] });
  }
  function setProduct(value) {
    let parsed = parse();
    if (parsed.page === "case" || parsed.page === "production") parsed = parse(parentURL(parsed));
    parsed.product = normalizeProduct(value);
    if (["content", "ai", "scripts", "production", "directions"].includes(parsed.page)) parsed.tail = [];
    if (parsed.page !== "library") ["direction", "point", "selling_point", "video", "sku"].forEach(key => parsed.params.delete(key));
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
    content: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
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
        const labelNode = source.cloneNode(true);
        labelNode.querySelectorAll('.nav-count').forEach(count => count.remove());
        const label = source.getAttribute("aria-label") || labelNode.textContent.trim();
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
      toggle.hidden = collapsed;
      if (pin) pin.hidden = !collapsed;
      sidebar.inert = collapsed && !peek;
      sidebar.setAttribute("aria-hidden", String(collapsed && !peek));
      toggle.setAttribute("aria-label", collapsed ? "固定边栏" : "隐藏边栏");
      toggle.title = collapsed ? "固定边栏" : "隐藏边栏";
      toggle.setAttribute("aria-expanded", String(!collapsed || peek));
      menu.setAttribute("aria-expanded", String(peek));
      menu.setAttribute("aria-label", peek ? "收起边栏" : "打开边栏");
      menu.title = peek ? "收起边栏" : "打开边栏";
      sidebar.querySelectorAll(".nav-item").forEach(button => {
        button.classList.toggle("is-preview", collapsed && peek && button.id === lastTrigger.dataset.sidebarTarget);
      });
    }
    function cancelClose() { clearTimeout(closeTimer); }
    function cancelOpen() {
      clearTimeout(openTimer);
      openTimer = 0;
      pendingTrigger = null;
    }
    function closePeek(restoreFocus = false) {
      cancelOpen();
      cancelClose();
      if (!peek) return;
      if (combo.open) openProductCombo(false);
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
      cancelClose();
      if (peek) { showPeek(trigger); return; }
      pendingTrigger = trigger;
      // Moving between icons and the rail's padding is one continuous hover.
      if (openTimer) return;
      openTimer = setTimeout(() => {
        const target = pendingTrigger;
        cancelOpen();
        if (target?.isConnected && !target.hidden && !document.querySelector("dialog[open]")) showPeek(target);
      }, 120);
    }
    function scheduleClose() {
      cancelClose();
      closeTimer = setTimeout(() => {
        if (combo.open) return;
        const focused = document.activeElement;
        const keyboardFocus = keyboardMode && (sidebar.contains(focused) || rail.contains(focused));
        const choosingProduct = supportsSelectOpen && sidebar.contains(focused) && focused.matches("select:open");
        if (!rail.matches(":hover") && !sidebar.matches(":hover") && !keyboardFocus && !choosingProduct) closePeek();
      }, 220);
    }
    function applyPreference() {
      cancelOpen();
      cancelClose();
      if (combo.open) openProductCombo(false);
      // Phones keep their existing top product picker and bottom workflow tabs.
      collapsed = !phoneScreen.matches && (preference === "collapsed" || (preference === null && compactScreen.matches));
      peek = false;
      renderSidebar();
    }
    function toggleSidebar() {
      preference = collapsed ? "expanded" : "collapsed";
      try { localStorage.setItem(preferenceKey, preference); } catch { /* Keep the choice for this session. */ }
      applyPreference();
      ignoreFocus = true;
      (collapsed ? menu : toggle).focus({ preventScroll: true });
      ignoreFocus = false;
    }
    toggle.addEventListener("click", toggleSidebar);
    pin?.addEventListener("click", toggleSidebar);
    for (const trigger of [menu, ...shortcuts]) {
      trigger.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") scheduleOpen(trigger); });
      trigger.addEventListener("focus", () => { if (keyboardMode && !ignoreFocus) showPeek(trigger); });
    }
    rail.addEventListener("pointerenter", event => { if (event.pointerType !== "touch") scheduleOpen(menu); });
    menu.addEventListener("click", () => {
      if (peek) closePeek();
      else showPeek(menu);
    });
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
      if (combo.panel?.contains(event.target)) return;
      if (!rail.contains(event.target) && !sidebar.contains(event.target) && !toggle.contains(event.target)) closePeek();
    });
    document.addEventListener("keydown", event => {
      keyboardMode = true;
      if (event.key === "Escape" && combo.open && !event.isComposing) {
        event.preventDefault();
        event.stopPropagation();
        openProductCombo(false, true);
        return;
      }
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
    window.addEventListener("workbench:productpickerclose", scheduleClose);
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
      const workflow = ["content", "ai", "scripts", "production"].includes(parsed.page);
      const active = button.dataset.page === (parsed.page === "case" ? "library" : workflow ? "content" : parsed.page) || button.dataset.page === "ai" && workflow;
      button.classList.toggle("active", active);
      if (active && button.getAttribute("aria-current") !== "page") button.setAttribute("aria-current", "page");
      else if (!active && button.hasAttribute("aria-current")) button.removeAttribute("aria-current");
    });
    const columns = document.querySelector("#library-navigation");
    if (columns) columns.hidden = true;
    // Task, script and order pages have their own main action (采纳、继续制作…), so the top bar does not add a second one.
    const ownAction = parsed.tail.length && ["content", "ai", "scripts", "production"].includes(parsed.page);
    const button = document.querySelector("#add-top"), action = ownAction ? null : actions[parsed.page];
    if (button) {
      button.hidden = !action;
      if (action) {
        const markup = `${icon(action[1])}${action[0]}`;
        if (actionSource !== markup || button.innerHTML !== actionHTML) {
          button.innerHTML = markup;
          actionSource = markup;
          actionHTML = button.innerHTML;
        }
        button.onclick = action[2];
      }
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
    mountProductCombo();
    initializeSelects();
    document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => { location.hash = listURL(button.dataset.page); });
    update();
    // Include products that only have sales rows or user reviews, without opening those pages.
    const catalogVersion = productCatalogVersion;
    void refreshProducts().catch(async error => {
      // An already-running service may still use the earlier API until its
      // current editing task finishes and it can restart safely.
      if (error.status !== 404 || catalogVersion !== productCatalogVersion || catalogProducts) return;
      const results = await Promise.allSettled([api("/api/performance/videos?shop=all"), api("/api/reviews/summary"), api("/api/direction-catalog"), api("/api/creation")]);
      if (catalogVersion !== productCatalogVersion || catalogProducts) return;
      const [videos, reviews, catalog, creation] = results;
      if (videos.status === "fulfilled") registerProducts(videos.value.videos || []);
      if (reviews.status === "fulfilled") registerProducts(reviews.value.products || []);
      if (catalog.status === "fulfilled") registerProducts([...(catalog.value.selling_points || []), ...(catalog.value.directions || [])]);
      if (creation.status === "fulfilled") registerProducts([...(creation.value.scripts || []), ...(creation.value.materials || [])]);
    });
  }
  window.WorkbenchShell = { normalizeProduct, productOf, matchesProduct, productLabel, getProductChoices, refreshProducts, createProductPicker, createFilterPicker, enhanceSelects, selectForTrigger: trigger => selectTriggerOwners.get(trigger), parse, url, legacyHash, canonicalize, registerProducts, setProduct, chooseWritingCase, directionParent, parentURL, listURL, listState, beginNavigation, finishNavigation, product: () => current, update, initialize };
})();
