"use strict";

// Product and URL context are shared by every page; no case records are changed.
(() => {
  const storageKey = "workbench-product-v1";
  let current = "202H";
  const products = new Map([["202H", "SHARPAL 202H"]]);
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
    if (!["home", "library", "case", "directions", "videos", "reviews", "ai", "scripts", "settings"].includes(page)) { page = "library"; tail = []; }
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
    picker.innerHTML = `<option value="">全部产品</option>${entries.map(([value, label]) => `<option value="${esc(value)}">${esc(label)}</option>`).join("")}`;
    picker.value = current;
  }
  function setProduct(value) {
    const parsed = parse();
    parsed.product = normalizeProduct(value);
    if (parsed.page === "case") { parsed.page = "library"; parsed.tail = []; }
    if (["ai", "scripts", "directions"].includes(parsed.page)) parsed.tail = [];
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
  const actions = {
    home: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
    library: ["收录案例", "plus", () => openEditor(null)],
    case: ["编辑当前案例", "edit", editCurrentCase],
    directions: ["新建方向", "plus", () => window.CaseDirections?.openCreate()],
    videos: ["同步数据", "refresh", () => document.querySelector("#perf-sync")?.click()],
    reviews: ["重新同步", "refresh", () => document.querySelector("#rev-sync")?.click()],
    ai: ["开始写稿", "plus", () => window.CaseAI?.startBlank()],
    scripts: ["保存脚本", "plus", () => window.CaseScripts?.openManual()],
    settings: ["重新检查", "refresh", () => window.WorkbenchSettings?.render()],
  };
  function update() {
    const parsed = parse();
    document.querySelectorAll("[data-page]").forEach(button => {
      const active = button.dataset.page === (parsed.page === "case" ? "library" : parsed.page === "scripts" ? "ai" : parsed.page);
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const columns = document.querySelector("#library-navigation");
    if (columns) columns.hidden = true;
    const button = document.querySelector("#add-top"), action = actions[parsed.page];
    if (button && action) { button.innerHTML = `${icon(action[1])}${action[0]}`; button.onclick = action[2]; }
    const importer = document.querySelector("#import-video-top");
    if (importer) importer.hidden = !["library", "case"].includes(parsed.page);
    const brand = document.querySelector(".brand");
    if (brand) brand.href = url("#home");
    registerProducts(state.cases);
  }
  function initialize() {
    const picker = document.querySelector("#global-product");
    if (picker) picker.onchange = event => setProduct(event.target.value);
    document.querySelectorAll("[data-page]").forEach(button => button.onclick = () => { location.hash = url("#" + button.dataset.page); });
    update();
    // Include products that only have sales rows or user reviews, without opening those pages.
    Promise.allSettled([api("/api/performance/videos"), api("/api/reviews/summary"), api("/api/direction-catalog"), api("/api/creation")]).then(results => {
      const [videos, reviews, catalog, creation] = results;
      if (videos.status === "fulfilled") registerProducts(videos.value.videos || []);
      if (reviews.status === "fulfilled") registerProducts(reviews.value.products || []);
      if (catalog.status === "fulfilled") registerProducts([...(catalog.value.selling_points || []), ...(catalog.value.directions || [])]);
      if (creation?.status === "fulfilled") registerProducts([...(creation.value.scripts || []), ...(creation.value.materials || [])]);
    });
  }
  window.WorkbenchShell = { normalizeProduct, productOf, matchesProduct, parse, url, legacyHash, canonicalize, registerProducts, setProduct, chooseWritingCase, product: () => current, update, initialize };
})();
