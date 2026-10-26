"use strict";

// Cases and commands provide immediate results. The catalog also finds directions with no cases yet.
window.WorkbenchSearch = (() => {
  const LIMIT = 6;
  const groups = [
    { key: "cases", label: "案例", icon: "folder" },
    { key: "videos", label: "视频", icon: "video" },
    { key: "directions", label: "测试方向", icon: "layers" },
    { key: "points", label: "卖点", icon: "target" },
    { key: "scripts", label: "采用稿", icon: "file" },
    { key: "tasks", label: "协作记录", icon: "bot" },
    { key: "orders", label: "制作单", icon: "monitor" },
    { key: "reviews", label: "评论", icon: "chat" },
    { key: "commands", label: "页面与操作", icon: "arrow" },
  ];
  // Pages and actions open from the same box, so Ctrl K also works as a launcher; extra words are what people tend to type.
  const commands = [
    { key: "page:home", title: "工作概览", words: "首页 本周 待办 home", route: "#home" },
    { key: "page:reviews", title: "用户声音", words: "评论 reviews", route: "#reviews" },
    { key: "page:directions", title: "卖点与方向", words: "方向 卖点 directions", route: "#directions" },
    { key: "page:content", title: "内容创作 · 内容列表", words: "内容 采用稿 脚本 制作 配音 剪辑 出图 图文 content scripts production", route: "#content" },
    { key: "page:ai", title: "内容创作 · 协作记录", words: "写稿 ai 任务 协作 历史", route: "#ai" },
    { key: "page:videos", title: "带货数据", words: "数据 销量 看板 视频数据 图文 跨境 本土 videos", route: "#videos" },
    { key: "page:library", title: "案例库", words: "案例 library", route: "#library" },
    { key: "page:settings", title: "设置", words: "模型 备份 导出 settings", route: "#settings" },
    { key: "action:write", title: "开始写稿", words: "新建 写稿 变体", run: () => window.CaseAI?.startBlank() },
    { key: "action:case", title: "收录案例", words: "新建 案例", run: () => { if (typeof openEditor === "function") openEditor(null); } },
    { key: "action:basket", title: "打开摘录篮", words: "素材 摘录", run: () => window.MaterialBasket?.open() },
    { key: "action:tasks", title: "查看任务与运行日志", words: "任务 提醒 日志", run: () => document.querySelector("#task-activity-open")?.click() },
  ].map(command => ({ ...command, command: true, description: "" }));
  // The public text copy is read-only: it offers no actions and no settings page.
  const available = () => document.body?.classList?.contains("public-text-mode") ? commands.filter(command => !command.run && command.key !== "page:settings") : commands;
  const platforms = { amazon: "Amazon", tiktok_shop: "TikTok Shop", tiktok_video: "TikTok 视频", reddit: "Reddit", facebook: "Facebook" };
  let dialog, input, list, status, scope, feedback, previousFocus;
  let query = "", sequence = 0, timer, controller, active = -1, results = [];
  let sections = {}, pending = false, failures = [], initialized = false;
  let videoCache = null, videoCacheTime = 0;

  const shell = () => window.WorkbenchShell;
  const product = () => shell()?.product() || "";
  const matchesProduct = item => !product() || Boolean(shell()?.matchesProduct(item));
  const url = legacy => shell()?.url(legacy) || legacy;
  const normalize = value => String(value || "").toLocaleLowerCase();
  const plain = value => String(value || "").replace(/\s+/g, " ").trim();
  const rank = (value, term) => normalize(value) === term ? 0 : normalize(value).startsWith(term) ? 1 : 2;
  const byRank = term => (a, b) => rank(a.title, term) - rank(b.title, term) || a.title.localeCompare(b.title);

  function excerpt(value, term, length = 130) {
    const text = plain(value), at = normalize(text).indexOf(normalize(term));
    const start = at > length / 2 ? Math.max(0, at - 35) : 0;
    return (start ? "…" : "") + text.slice(start, start + length) + (text.length > start + length ? "…" : "");
  }

  function highlight(value) {
    const text = String(value || ""), term = normalize(query);
    const at = term ? normalize(text).indexOf(term) : -1;
    return at < 0 ? esc(text) : `${esc(text.slice(0, at))}<mark>${esc(text.slice(at, at + query.length))}</mark>${esc(text.slice(at + query.length))}`;
  }

  function localResults(term) {
    const items = (state.cases || []).filter(matchesProduct);
    const cases = items.filter(item => normalize(`${item.case_id} ${item.title || item.current?.title || ""} ${item.search_text || ""}`).includes(term)).map(item => ({
      key: `case:${item.case_id}`, title: item.title || item.current?.title || item.case_id,
      description: [product() ? "" : item.product || "未标注产品",item.archived ? "已归档" : ""].filter(Boolean).join(" · "), identity: item.case_id,
      route: `#case/${encodeURIComponent(item.case_id)}`,
    })).sort(byRank(term)).slice(0, LIMIT);
    const directions = new Map();
    for (const item of items) {
      const direction = item.current?.test_direction;
      if (!direction?.direction_id) continue;
      const title = direction.direction_id, detail = [direction.main_difference, direction.concern, direction.closing].filter(Boolean).join(" → ");
      if (!normalize(`${title} ${detail}`).includes(term)) continue;
      const key = `${shell()?.productOf?.(item) || item.product || ""}:${title}`;
      if (directions.has(key)) { directions.get(key).count += 1; continue; }
      const params = new URLSearchParams({ archive: "all" });
      if (item.product) params.set("product", item.product);
      directions.set(key, { key: `direction:${key}`, title, description: detail || item.product || "测试方向", route: `#directions/${encodeURIComponent(title)}?${params}`, count: 1 });
    }
    const found = available().filter(command => normalize(`${command.title} ${command.words}`).includes(term));
    return { cases, directions: [...directions.values()].sort(byRank(term)).slice(0, LIMIT), commands: found };
  }

  // Lists that change less often than a keystroke are read once per half minute.
  const cache = new Map();
  async function cached(path, signal) {
    const hit = cache.get(path);
    if (hit && Date.now() - hit.at < 30000) return hit.data;
    const data = await api(path, { signal });
    if (!signal.aborted) cache.set(path, { at: Date.now(), data });
    return data;
  }
  const scoped = path => product() ? `${path}${path.includes("?") ? "&" : "?"}product=${encodeURIComponent(product())}` : path;

  async function scriptResults(term, signal) {
    const data = await cached(scoped("/api/creation"), signal);
    if (signal.aborted) return [];
    return (data.scripts || []).filter(matchesProduct).filter(item => normalize(`${item.id} ${item.title || ""} ${item.text || ""} ${item.direction_id || ""} ${item.video_id || ""}`).includes(term)).map(item => ({
      key: `script:${item.id}`, title: item.title || "未命名采用稿",
      description: [product() ? "" : item.product || "未标注产品",item.direction_id && !String(item.title || "").includes(item.direction_id) ? item.direction_id : "",item.version > 1 ? `第 ${item.version} 版` : "",item.created_at ? `${plain(item.created_at).slice(0,16).replace('T',' ')} 采纳` : ""].filter(Boolean).join(" · "), identity: item.id,
      route: `#content/${encodeURIComponent(item.id)}`,
    })).sort(byRank(term)).slice(0, LIMIT);
  }

  async function taskResults(term, signal) {
    const data = await cached(scoped("/api/ai/tasks"), signal);
    if (signal.aborted) return [];
    const title = task => window.CaseAI?.taskDisplayTitle?.(task) || task.display_title || task.case_title || task.context?.title || task.context?.direction_id || task.case_id || ({review:"审稿结论",retrospective:"复盘结论"}[task.action] || "空白写稿");
    return (data.tasks || []).filter(matchesProduct).filter(task => normalize(`${task.task_id} ${title(task)} ${task.case_title || ""} ${task.context?.direction_id || ""} ${task.instruction || ""}`).includes(term)).map(task => ({
      key: `task:${task.task_id}`, title: title(task),
      description: [product() ? "" : task.product || "未标注产品",{queued:"排队中",running:"进行中",failed:"失败",interrupted:"已中断",cancelled:"已停止"}[task.status] || window.CaseAI?.taskManagementLabel?.(task) || "查看结果",task.task_started_label || (task.created_at ? `${plain(task.created_at).slice(0,16).replace('T',' ')} 发起` : ""),task.round_count ? `${task.round_count} 轮` : "",task.archived ? "已归档" : ""].filter(Boolean).join(" · "), identity: task.task_id,
      route: `#ai/${encodeURIComponent(task.task_id)}`,
    })).sort(byRank(term)).slice(0, LIMIT);
  }

  // An order is found by its ID, title and script, and also by a DaVinci timeline name or ID or a registered
  // post ID; those open the step where the timeline or the publication is shown.
  function orderMatch(item, carousel, term) {
    if (normalize(`${item.id} ${item.title || ""} ${item.variant_label || ""} ${item.script_id || ""}`).includes(term)) return { note: "", step: "" };
    const timeline = (item.timelines || []).find(entry => normalize(`${entry.name || ""} ${entry.unique_id || ""}`).includes(term));
    if (timeline) return { note: `时间线 ${timeline.name || timeline.unique_id}`, step: "timeline" };
    if (normalize(item.display_title || "").includes(term)) return { note: "", step: "" };
    const post = [item.current_publication, item.publication, ...(item.publication_history || [])].filter(Boolean)
      .find(entry => normalize(`${entry.post_id || ""} ${entry.post_url || ""}`).includes(term));
    if (post) return { note: `${carousel ? "图文" : "视频"} ID ${post.post_id}`, step: carousel ? "delivery" : "publish" };
    return null;
  }

  async function orderResults(term, signal) {
    const data = await cached(scoped("/api/production"), signal);
    if (signal.aborted) return [];
    const orders = [...(data.jobs || []).map(item => ({ item, kind: "视频制作单" })), ...(data.carousels || []).map(item => ({ item, kind: "图文制作单" }))];
    return orders.filter(({ item }) => matchesProduct(item)).map(row => ({ ...row, match: orderMatch(row.item, row.kind === "图文制作单", term) })).filter(row => row.match).map(({ item, kind, match }) => ({
      key: `order:${item.id}`, title: item.display_title || item.title || item.id,
      description: `${item.product || "未标注产品"} · ${kind}${match.note ? ` · ${match.note}` : ""}${item.archived ? " · 已归档" : ""}`,
      route: `#production/${encodeURIComponent(item.id)}${match.step ? `?step=${match.step}` : ""}`,
    })).sort(byRank(term)).slice(0, LIMIT);
  }

  function pointResults(term, data) {
    return (data.selling_points || []).filter(matchesProduct).filter(point => normalize(`${point.code || ""} ${point.title || ""}`).includes(term)).map(point => {
      const params = new URLSearchParams({ q: point.title || point.code || "" });
      if (point.product) params.set("product", point.product);
      return { key: `point:${point.product || ""}:${point.code || point.title}`, title: point.title || point.code, description: `${point.code ? `${point.code} · ` : ""}${point.product || "卖点"}`, route: `#directions?${params}` };
    }).sort(byRank(term)).slice(0, LIMIT);
  }

  async function directionResults(term, signal, catalog) {
    const data = await catalog();
    if (signal.aborted) return [];
    const rows = new Map(localResults(term).directions.map(row => [row.key, row]));
    for (const direction of data.directions || []) {
      if (!matchesProduct(direction)) continue;
      const title = direction.direction_id;
      const detail = [direction.main_difference, direction.concern, direction.closing].filter(Boolean).join(" → ");
      const key = `direction:${shell()?.productOf?.(direction) || direction.product || ""}:${title}`;
      if (!normalize(`${title} ${detail} ${direction.notes || ""}`).includes(term) && !rows.has(key)) continue;
      const routeParams = new URLSearchParams({ product: direction.product, archive: "all" });
      rows.set(key, { key, title, description: detail || direction.product || "测试方向",
        route: `#directions/${encodeURIComponent(title)}?${routeParams}`,
        count: direction.case_count ?? rows.get(key)?.count ?? 0 });
    }
    return [...rows.values()].sort(byRank(term)).slice(0, LIMIT);
  }

  async function videoResults(term, signal) {
    if (!videoCache || Date.now() - videoCacheTime > 30000) {
      const data = await api("/api/performance/videos?shop=all", { signal });
      if (signal.aborted) return [];
      videoCache = data.videos || [];
      videoCacheTime = Date.now();
    }
    return videoCache.filter(matchesProduct).filter(item => normalize(`${item.video_id} ${item.title || ""} ${item.headline || ""}`).includes(term)).map(item => ({
      key: `video:${item.video_id}`, title: item.headline || item.title || item.video_id,
      description: `${item.product || "未标注产品"} · ${item.video_id}${item.account ? ` · @${item.account.replace(/^@/, "")}` : ""}`,
      route: `#videos?video=${encodeURIComponent(item.video_id)}`,
    })).sort(byRank(term)).slice(0, LIMIT);
  }

  async function reviewResults(text, signal) {
    // The review API's sku means package variant, not product. Also check returned
    // product fields for compatibility with servers running an older read API.
    const found = [];
    let offset = 0;
    while (!signal.aborted) {
      const params = new URLSearchParams({ q: text, limit: "200", offset: String(offset), sort: "likes" });
      if (product()) params.set("product", product());
      const data = await api(`/api/reviews?${params}`, { signal });
      if (signal.aborted) return [];
      const rows = data.reviews || [];
      found.push(...rows.filter(matchesProduct));
      offset += rows.length;
      if (found.length >= LIMIT || !rows.length || offset >= Number(data.total || 0)) break;
    }
    return found.slice(0, LIMIT).map(item => {
      const params = new URLSearchParams({ q: text });
      if (item.platform === "tiktok_video" && /^\d{6,25}$/.test(item.source_ref || "")) params.set("video", item.source_ref);
      return {
        key: `review:${item.id}`, title: excerpt(item.title || item.text, text, 95),
        description: `${platforms[item.platform] || item.platform || "评论"} · ${item.product || "未标注产品"} · ${item.title ? excerpt(item.text, text, 110) : "查看匹配评论"}`,
        route: `#reviews?${params}`, hint: params.has("video") ? "查看该视频的匹配评论" : "查看评论库中的匹配评论",
      };
    });
  }

  function select(index, scroll = false) {
    active = results.length ? (index + results.length) % results.length : -1;
    list.querySelectorAll("[data-search-index]").forEach((node, at) => {
      node.setAttribute("aria-selected", String(at === active));
      if (at === active && scroll) node.scrollIntoView({ block: "nearest" });
    });
    if (active >= 0) input.setAttribute("aria-activedescendant", `workbench-search-result-${active}`);
    else input.removeAttribute("aria-activedescendant");
  }

  function paint() {
    if (!dialog?.open) return;
    const selected = results[active]?.key;
    results = [];
    scope.textContent = product() ? `当前产品：${product()}` : "全部产品";
    list.setAttribute("aria-busy", String(pending));
    const markup = groups.map(group => {
      const rows = sections[group.key] || [];
      if (!rows.length) return "";
      return `<div role="group" aria-labelledby="workbench-search-group-${group.key}"><div class="global-search-group" id="workbench-search-group-${group.key}">${esc(group.label)}<span>${rows.length} 条</span></div>${rows.map(row => {
        const index = results.length;
        results.push(row);
        const inner = `<span class="global-search-result-icon">${icon(row.command && row.run ? "plus" : group.icon)}</span><span class="global-search-result-copy"><strong>${highlight(excerpt(row.title, query, 100))}</strong>${row.description || row.count ? `<small>${highlight(row.description)}${row.count ? ` · ${row.count} 条案例` : ""}</small>` : ""}${row.hint ? `<span class="global-search-result-hint">${esc(row.hint)}</span>` : ""}</span><span class="global-search-result-arrow" aria-hidden="true">↵</span>`;
        const attributes = `class="global-search-result${row.command ? ' is-command' : ''}" role="option" aria-selected="false" tabindex="-1" id="workbench-search-result-${index}" data-search-index="${index}"${row.identity ? ` title="${esc(row.identity)}"` : ''}`;
        // Actions run in place; everything else stays a real link so modified clicks still open a new tab.
        return row.run ? `<button type="button" ${attributes}>${inner}</button>` : `<a ${attributes} href="${esc(url(row.route))}"${row.hint ? ` aria-label="${esc(`${row.title}，${row.hint}`)}"` : ""}>${inner}</a>`;
      }).join("")}</div>`;
    }).join("");
    list.innerHTML = markup;
    select(Math.max(0, results.findIndex(row => row.key === selected)));
    feedback.hidden = !failures.length;
    feedback.innerHTML = failures.length ? `<span>${esc(failures.join("、"))}暂时无法搜索，其他结果仍可使用。</span><button type="button" class="subtle" id="workbench-search-retry">重试</button>` : "";
    status.textContent = !query ? "搜索内容或编号，也可选择页面与操作。" : pending ? `正在搜索${results.length ? `，已找到 ${results.length} 条` : ""}…` : results.length ? `显示 ${results.length} 条结果，每类最多 ${LIMIT} 条。` : failures.length ? "部分数据未能读取，请重试。" : "没有找到匹配结果，试试其他关键词或切换产品。";
  }

  function invalidate() {
    sequence += 1;
    clearTimeout(timer);
    controller?.abort();
    controller = null;
  }

  function search(delay = 180) {
    invalidate();
    const mine = sequence, selectedProduct = product();
    query = input.value.trim();
    sections = query ? localResults(normalize(query)) : { commands: available() };
    failures = [];
    active = -1;
    results = [];
    pending = Boolean(query);
    paint();
    if (!query) return;
    controller = new AbortController();
    const signal = controller.signal, text = query;
    timer = setTimeout(async () => {
      const current = () => !signal.aborted && mine === sequence && dialog.open && selectedProduct === product();
      const params = new URLSearchParams(), term = normalize(text);
      if (selectedProduct) params.set("product", selectedProduct);
      // Directions and selling points share one catalog read.
      let catalogRequest;
      const catalog = () => (catalogRequest ||= api(`/api/direction-catalog?${params}`, { signal }));
      const sources = [
        ["videos", "视频", () => videoResults(term, signal)],
        ["directions", "方向", () => directionResults(term, signal, catalog)],
        ["points", "卖点", async () => pointResults(term, await catalog())],
        ["scripts", "采用稿", () => scriptResults(term, signal)],
        ["tasks", "协作记录", () => taskResults(term, signal)],
        ["orders", "制作单", () => orderResults(term, signal)],
        ["reviews", "评论", () => reviewResults(text, signal)],
      ];
      let remaining = sources.length;
      await Promise.all(sources.map(async ([key, label, request]) => {
        try {
          const rows = await request();
          if (current()) sections[key] = rows;
        } catch (error) {
          if (current() && error.name !== "AbortError") failures.push(label);
        } finally {
          remaining -= 1;
          if (current()) { pending = remaining > 0; paint(); }
        }
      }));
    }, delay);
  }

  function close() {
    if (dialog?.open) dialog.close();
  }

  function open() {
    initialize();
    if (!dialog.open) {
      previousFocus = document.activeElement;
      dialog.showModal();
      search(0);
    }
    input.focus();
    input.select();
  }

  function isEditing(target) {
    return target?.isContentEditable || Boolean(target?.closest?.("input, textarea, select, [contenteditable='true'], [role='textbox']"));
  }

  function initialize() {
    if (initialized) return;
    initialized = true;
    dialog = document.createElement("dialog");
    dialog.id = "workbench-search";
    dialog.className = "global-search-dialog";
    dialog.setAttribute("aria-labelledby", "workbench-search-title");
    dialog.innerHTML = `<div class="global-search-header"><div><h2 id="workbench-search-title">搜索工作台</h2><p id="workbench-search-scope"></p></div><button type="button" class="subtle global-search-close" aria-label="关闭搜索" id="workbench-search-close">${icon("close")}</button></div><div class="global-search-input-wrap">${icon("search")}<input id="workbench-search-input" type="search" role="combobox" aria-label="全局搜索关键词" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="true" aria-controls="workbench-search-list" aria-describedby="workbench-search-scope workbench-search-help" placeholder="案例、视频、方向、评论…" autocomplete="off" spellcheck="false"></div><p class="global-search-status" id="workbench-search-status" role="status" aria-live="polite" aria-atomic="true"></p><div id="workbench-search-feedback" class="global-search-feedback" hidden></div><div id="workbench-search-list" class="global-search-list" role="listbox" aria-label="搜索结果"></div><div class="global-search-footer" id="workbench-search-help"><span><kbd>↑</kbd><kbd>↓</kbd> 选择 <kbd>Enter</kbd> 打开</span><span><kbd>Esc</kbd> 关闭</span></div>`;
    document.body.append(dialog);
    input = dialog.querySelector("#workbench-search-input");
    list = dialog.querySelector("#workbench-search-list");
    status = dialog.querySelector("#workbench-search-status");
    scope = dialog.querySelector("#workbench-search-scope");
    feedback = dialog.querySelector("#workbench-search-feedback");
    input.addEventListener("input", event => { if (!event.isComposing) search(); });
    input.addEventListener("compositionend", () => search());
    input.addEventListener("keydown", event => {
      if (event.isComposing) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      } else if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault();
        select(active + (event.key === "ArrowDown" ? 1 : -1), true);
      } else if (event.key === "Enter" && results[active]) {
        event.preventDefault();
        const row = results[active];
        close();
        if (row.run) row.run(); else if (!window.CasePerformance?.openVideoHash?.(url(row.route))) location.hash = url(row.route);
      }
    });
    dialog.addEventListener("click", event => {
      if (event.target.closest?.("#workbench-search-close")) close();
      if (event.target.closest?.("#workbench-search-retry")) search(0);
      const result = event.target.closest?.("[data-search-index]");
      if (result) {
        const row = results[Number(result.dataset.searchIndex)];
        close(); // Keep anchor navigation, including modified clicks.
        if (row?.run) row.run();
      }
      if (event.target === dialog) {
        const bounds = dialog.getBoundingClientRect();
        if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) close();
      }
    });
    list.addEventListener("pointermove", event => {
      const result = event.target.closest?.("[data-search-index]");
      if (result) select(Number(result.dataset.searchIndex));
    });
    dialog.addEventListener("close", () => {
      invalidate();
      input.removeAttribute("aria-activedescendant");
      if (previousFocus?.isConnected) previousFocus.focus();
    });
    document.addEventListener("click", event => {
      if (event.target.closest?.("#global-search-open")) open();
    });
    document.addEventListener("keydown", event => {
      if (event.defaultPrevented || event.isComposing || event.altKey) return;
      const command = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k";
      const slash = event.key === "/" && !event.ctrlKey && !event.metaKey && !isEditing(event.target);
      if (command || slash) { event.preventDefault(); open(); }
    });
    window.addEventListener("workbench:productchange", () => { if (dialog.open) search(0); });
    window.addEventListener("hashchange", () => { if (dialog.open) close(); });
  }

  // This file can precede app.js among deferred scripts. "interactive" still means
  // the shared esc/icon/state declarations may not have executed yet.
  if (document.readyState !== "complete") document.addEventListener("DOMContentLoaded", initialize, { once: true });
  else initialize();
  return { initialize, open, close };
})();
