"use strict";

const directionVariantNames = { direction: "方向变体", execution: "执行变体" };
const directionFieldLabels = {
  direction_id: "方向编号", main_difference: "主打卖点", concern: "顾虑链", closing: "收口",
  execution_change: "执行改动", script_version: "方向对应文案版本",
};
// One line, e.g. 定角不用猜 → 装夹麻烦吗 → 旋钮锁角、上色看接触 → 值不值 → 修刃到精修整套用途
function directionChain(d) {
  return d ? [d.main_difference, d.concern, d.closing].map(value => String(value || "").trim()).filter(Boolean).join(" → ") : "";
}
function directionLabel(item) {
  const d = item.current?.test_direction;
  if (!d) return "未记录测试方向";
  const variant = directionVariantNames[d.variant_type] || d.variant_type;
  return variant ? `${d.direction_id} · ${variant}` : d.direction_id;
}
function directionEditor(current) {
  const d = current.test_direction || {};
  return `<section class="editor-section test-direction-editor"><h3>测试方向（可选）</h3><p>按一行方向链填写：主打卖点 → 顾虑和其他卖点 → 收口；卖点带产品分级表里的 S/A/B 编号，例如“S1 定角不用猜 → 装夹麻烦吗 → A3 一次装夹翻面继续 → 值不值 → B3 3 年有限质保”。方向编号写“产品-主打卖点编号”，如 202H-S1；同一主打卖点下换开头、顺序或 A/B 记为执行变体，换主打卖点才用新编号。全部清空可移除当前标记，历史仍保留。</p>${field("direction_direction_id", "方向编号", d.direction_id || "", { placeholder: "例如：202H-S1", help: "产品-主打卖点编号；同一主打卖点的案例共用这个编号。" })}${field("direction_variant_type", "变体类型", d.variant_type || "direction", { select: directionVariantNames })}${field("direction_main_difference", "主打卖点", d.main_difference || "", { placeholder: "例如：S1 定角不用猜", help: "方向链第一环：主打卖点，带编号，与方向编号末尾一致。" })}${field("direction_concern", "顾虑链", d.concern || "", { placeholder: "例如：装夹麻烦吗 → A3 一次装夹翻面继续 → 值不值", help: "中间环节：观众的顾虑和用来回应的卖点，卖点带编号，按出现顺序用 → 连接。" })}${field("direction_closing", "收口", d.closing || "", { placeholder: "例如：B3 3 年有限质保", help: "最后一环：实际收口内容，是卖点就带编号；没有购买收口写“无购买收口”。" })}${field("direction_execution_change", "执行改动", d.execution_change || "", { help: "执行变体说明改了钩子、顺序、语气或镜头中的哪一项。" })}${field("direction_script_version", "方向对应文案版本", d.script_version || "", { help: "通常在“发布与制作”填写文案版本；这里可单独绑定。两处不同会提示核对。" })}</section>`;
}
function collectTestDirection(current, value) {
  const d = {};
  Object.keys(directionFieldLabels).forEach(key => {
    const text = value("direction_" + key);
    if (text) d[key] = text;
  });
  if (!Object.keys(d).length) {
    delete current.test_direction;
    return;
  }
  for (const key of ["direction_id", "main_difference", "concern", "closing"])
    if (!d[key]) throw new Error(`测试方向请补齐“${directionFieldLabels[key]}”，或清空全部方向文字后保存。`);
  d.variant_type = value("direction_variant_type");
  if (!Object.hasOwn(directionVariantNames, d.variant_type)) throw new Error("请选择有效的变体类型。");
  current.test_direction = d;
}
function renderTestDirection(item) {
  const d = item.current?.test_direction;
  if (!d) return '<section class="section-card"><h2>测试方向</h2><p class="context-note">未记录测试方向。已有案例不从文案或销量自动推断，可在编辑案例中补充。</p></section>';
  const resolved = item.resolved_direction;
  const version = item.current?.production?.script_version;
  const mismatch = d.script_version && version && d.script_version !== version;
  const rows = [["方向编号", d.direction_id], ["变体类型", directionVariantNames[d.variant_type]], ["执行改动", d.execution_change], ["方向对应文案版本", d.script_version]];
  const directionURL = window.WorkbenchShell?.url("#directions/" + encodeURIComponent(d.direction_id), item.product) || "#directions/" + encodeURIComponent(d.direction_id);
  return `<section class="section-card"><div class="section-title-row"><h2>${icon("layers")}测试方向</h2><a class="secondary" href="${esc(directionURL)}">查看方向汇总</a></div>${resolved?`<div class="direction-current-definition"><h3>独立方向当前定义</h3><p class="direction-chain">${esc(directionChain(resolved))}</p></div>`:""}<h3>案例当时的执行记录</h3><p class="direction-chain">${esc(directionChain(d))}</p><div class="direction-facts">${rows.filter(([,value]) => value).map(([label,value]) => `<div><span class="metric-label">${esc(label)}</span><p>${esc(value)}</p></div>`).join("")}</div>${mismatch ? '<p class="data-warning">方向绑定版本与制作文案版本不同，请核对数据对应稿件。</p>' : ""}</section>`;
}

// 卖点与方向: one screen. On a wide window the directions sit on the left ranked by orders and the chosen one opens on the
// right without leaving the page; the selling-point table is its own tab. A narrow window shows the list, then the direction.
window.CaseDirections = (() => {
  let requestSequence = 0, paneSequence = 0;
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const statusNames = {testing:"待测试", active:"测试中", paused:"暂缓", archived:"已归档"};
  const taskStatusNames = {queued:"排队中",running:"处理中",completed:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
  const wide = window.matchMedia("(min-width:1181px)");
  // The mounted page: its list data is reused while only the search, the tab or the chosen direction changes.
  let listView = null, paneState = null;
  let activeDraft = null;
  // A dialog, or the form opened inside the selling-point table: whichever holds unsaved input.
  function modalOpen() { return !!document.querySelector('#direction-create-dialog[open],#direction-edit-dialog[open],#content .px-edit'); }
  function confirmDiscard(action) {
    if (!activeDraft?.dirty) { action(); return; }
    let dialog = $("#direction-discard-dialog");
    if (!dialog) { dialog = document.createElement("dialog"); dialog.id = "direction-discard-dialog"; document.body.append(dialog); }
    dialog.innerHTML = '<div class="confirm-body"><h2>保留这次方向编辑？</h2><p>还有未保存的内容。继续编辑可保留当前草稿。</p><div class="confirm-actions"><button type="button" class="secondary" data-discard>放弃修改</button><button type="button" class="primary" data-keep>继续编辑</button></div></div>';
    dialog.querySelector("[data-keep]").onclick = () => dialog.close();
    dialog.querySelector("[data-discard]").onclick = () => { dialog.close(); if (activeDraft) activeDraft.dirty = false; action(); };
    if (!dialog.open) dialog.showModal();
  }
  function trackDraft(dialog) {
    activeDraft = {dialog, hash: location.hash, dirty: false, pending: false};
    dialog.oninput = () => { if (activeDraft?.dialog === dialog) activeDraft.dirty = true; };
    dialog.oncancel = event => { event.preventDefault(); requestClose(dialog); };
    dialog.onclose = () => { if (activeDraft?.dialog === dialog) activeDraft = null; };
  }
  function requestClose(dialog) { if (!activeDraft?.pending) confirmDiscard(() => dialog.close()); }
  function beforeNavigate() {
    if (!activeDraft?.dialog.open) return true;
    const next = location.hash;
    if (next === activeDraft.hash) return true;
    if (activeDraft.pending) { history.replaceState(null, "", activeDraft.hash); return false; }
    if (!activeDraft.dirty) { activeDraft.dialog.close(); return true; }
    history.replaceState(null, "", activeDraft.hash);
    confirmDiscard(() => { activeDraft?.dialog.close(); location.hash = next; });
    return false;
  }
  function directionURL(product, id) {
    const hash = "#directions" + (id ? "/" + encodeURIComponent(id) : "");
    return window.WorkbenchShell?.url(hash, product) || hash;
  }
  function routeValues() {
    const hash = pageHash(), params = new URLSearchParams(hash.split("?")[1] || "");
    let id = "";
    try { id = decodeURIComponent(hash.split("?")[0].split("/").slice(1).join("/")); } catch {}
    const tier = ["S", "A", "B", "parked"].includes(params.get("tier")) ? params.get("tier") : "";
    return {hash, params, id, tier, q: (params.get("q") || "").trim(),
      product: window.WorkbenchShell ? window.WorkbenchShell.product() : params.get("product") || "",
      archive: ["active", "archived", "all"].includes(params.get("archive")) ? params.get("archive") : "active",
      // A tier filter only exists on the table, so an older link that carries one opens the table.
      tab: !id && (params.get("tab") === "points" || tier) ? "points" : "directions"};
  }
  function catalogURL(product, id = "") {
    const query = new URLSearchParams(); if (product) query.set("product", product);
    if (id) { query.set("view", "page"); query.set("include_tasks", "0"); }
    return "/api/direction-catalog" + (id ? "/" + encodeURIComponent(id) : "") + (query.size ? "?" + query : "");
  }
  function fieldText(value) { return typeof value === "string" ? value : ""; }
  const progressions = ["问题-解决", "结果倒叙", "逐件对照", "回应质疑", "过程仪式", "身份认同", "清单演示"];
  const codePattern = /(?<![A-Za-z0-9])([SAB][1-9]\d*)(?!\d)/g;
  // Same rule as the server: codes must exist in this product's table, no "未编号" links.
  function chainIssues(points, ...parts) {
    const codes = new Set(points.map(item => item.code)), joined = parts.filter(Boolean).join(" → "), issues = [];
    const unknown = [...new Set([...joined.matchAll(codePattern)].map(match => match[1]).filter(code => !codes.has(code)))];
    if (unknown.length) issues.push("卖点表里没有 " + unknown.join("、"));
    if (joined.includes("未编号")) issues.push("有未编号的环节，请改用卖点编号或删去");
    return issues;
  }
  function performanceText(perf) {
    if (!perf || !perf.video_count) return "";
    if (!perf.matched_count) return `${perf.video_count} 条视频，销售数据未匹配`;
    return `${perf.matched_count === perf.video_count ? `${perf.video_count} 条视频` : `${perf.matched_count} 条有销量（共关联 ${perf.video_count} 条视频）`} · ${perf.orders} 单 · 点击 ${perf.clicks}${perf.ctor_pct != null ? ` · CTOR ${perf.ctor_pct}%` : ""}${perf.data_through ? ` · 截至 ${perf.data_through}` : ""}`;
  }
  // Point status sits beside S/A/B: 有销量 comes from sales, the rest is set by hand.
  const pointStatusNames = {testing: "已核", candidate: "待核", merged: "已并入"};
  const pointStatusTips = {selling: "主打它的视频有订单", testing: "已核实，可以用来写稿", candidate: "还没核实或没有证明素材，写稿前先补证明", merged: "同一产品事实已并入其他编号，编号不复用"};
  const pointStatus = point => !point ? "" : point.status === "merged" ? "merged" : point.performance?.orders > 0 ? "selling" : point.status || "";
  const parkedPoint = point => ["candidate", "merged"].includes(pointStatus(point));
  const tag = (kind, label, title = "") => `<span class="state-tag${kind ? " " + kind : ""}"${title ? ` title="${esc(title)}"` : ""}>${esc(label)}</span>`;
  function pointStatusChip(point) {
    const key = pointStatus(point); if (!key) return "";
    const label = key === "selling" ? "有销量" : key === "merged" ? `已并入 ${point.merged_into || ""}`.trim() : pointStatusNames[key];
    return tag(key === "selling" ? "done" : key === "candidate" ? "review" : "", label, pointStatusTips[key]);
  }
  // Direction notes are "标签：内容" lines; only 核心证明 and 不能说 change what gets written, the rest folds.
  function notesMarkup(notes) {
    const lines = String(notes || "").split(/\n+/).map(line => line.trim()).filter(Boolean);
    if (!lines.length) return "";
    const key = lines.filter(line => /^(核心证明|不能说)[：:]/.test(line)), rest = lines.filter(line => !key.includes(line));
    const keyMarkup = key.map(line => { const [label, ...body] = line.split(/[：:]/); return `<p class="dir-note dir-note-key"><strong>${esc(label)}</strong> ${esc(body.join("："))}</p>`; }).join("");
    if (!key.length && lines.length === 1 && lines[0].length <= 80) return `<p class="dir-note"><strong>备注</strong> ${esc(lines[0])}</p>`;
    return keyMarkup + (rest.length ? `<details class="dir-notes-more"><summary>备注与依据 · ${rest.length} 条</summary>${rest.map(line => `<p>${esc(line)}</p>`).join("")}</details>` : "");
  }
  const gradingFields = {title: "标题", rationale: "分级理由", evidence: "证据与边界"};
  function gradingDiff(point) {
    const row = point?.grading_row || {};
    const lines = Object.entries(gradingFields).filter(([key]) => row[key] != null && String(row[key]).trim() !== String(point[key] || "").trim())
      .map(([key, label]) => `${label}\n  分级表：${plain(row[key])}\n  工作台：${plain(point[key]) || "（空）"}`);
    return lines.length ? `和分级表写法不一样的地方：\n${lines.join("\n")}` : "与分级表写法不一致";
  }
  const tierRank = code => "SAB".indexOf(String(code)[0]), codeNo = code => Number(String(code).slice(1)) || 0;
  const sortPoints = points => [...points].sort((a, b) => tierRank(a.code) - tierRank(b.code) || codeNo(a.code) - codeNo(b.code));
  // Selling-point codes inside a chain become small tags; hovering one shows the point it stands for.
  function chainText(text, points = []) {
    return esc(text || "").replace(codePattern, code => { const point = points.find(item => item.code === code); return `<span class="dir-ref tier-${code[0]}"${point ? ` title="${esc(point.title)}"` : ""}>${code}</span>`; });
  }
  // A chain reads as steps. Only a step that is plainly the viewer's question is set apart; the rest stays as written.
  const asking = step => /[吗呢？?]\s*$|会不会|能不能|是不是|值不值|要不要|用得着|怎么|为什么|多少/.test(step);
  const hasCode = step => { codePattern.lastIndex = 0; return codePattern.test(step); };
  function stepsMarkup(text, points) {
    return String(text || "").split(/\s*→\s*/).filter(Boolean)
      .map(step => `<span${!hasCode(step) && asking(step) ? ' class="is-ask"' : ""}>${chainText(step, points)}</span>`).join('<i aria-hidden="true">→</i>');
  }
  function chainStages(item, points, lead = true) {
    return [lead ? ["主打", item.main_difference] : null, ["顾虑与回应", item.concern], ["收口", item.closing]]
      .filter(stage => stage && String(stage[1] || "").trim())
      .map(([label, value]) => `<div class="dx-stage"><span>${label}</span><div class="dx-steps">${stepsMarkup(value, points)}</div></div>`).join("");
  }
  // Execution variants only: the 「方向定义」 entry is the direction itself, not a variant.
  const executionCount = direction => (direction.variants || []).filter(item => item.variant_type !== "direction").length;
  const planNames = {scale: "加码", execution: "换执行", stop: "停测", observe: "观察"};
  const daysSince = iso => { const time = Date.parse(iso || ""); return Number.isFinite(time) ? Math.max(0, Math.floor((Date.now() - time) / 86400000)) : null; };
  const liveDirection = direction => !!direction && ["testing", "active"].includes(direction.status);
  const shortMain = item => String(item?.main_difference || "").replace(/^[SAB][1-9]\d*\s*/, "").trim();
  const ordersOf = item => Number(item?.performance?.orders) || 0;
  const money = new Intl.NumberFormat("en-US");
  // A decision somebody is writing from is in progress; it is only called out when two weeks passed without a new script.
  const stalePlan = plan => !!plan && plan.scripts_since === 0 && (daysSince(plan.created_at) ?? 0) > 14;

  // ---- which direction the right side opens when the address names none ----
  const pickKey = "workbench-direction-pick-v1";
  const picks = () => { try { return JSON.parse(localStorage.getItem(pickKey) || "{}") || {}; } catch { return {}; } };
  const rememberPick = (product, id) => { try { localStorage.setItem(pickKey, JSON.stringify({...picks(), [product || "all"]: id})); } catch { /* opens the first direction next time */ } };
  function listModel(catalog, query = "") {
    const directions = catalog.directions || [], points = sortPoints(catalog.selling_points || []);
    const pointOf = item => points.find(point => point.id === item.selling_point_id && point.product === item.product) || null;
    const needle = query.toLocaleLowerCase();
    const text = (point, item) => `${point ? `${point.code} ${point.title} ${point.rationale || ""} ${point.evidence || ""}` : ""} ${item ? `${item.direction_id} ${directionChain(item)} ${item.notes || ""} ${item.audience || ""}` : ""}`.toLocaleLowerCase();
    const keep = (point, item) => !needle || text(point, item).includes(needle);
    const rows = directions.map(item => [pointOf(item), item]).filter(([point, item]) => keep(point, item));
    const live = rows.filter(([, item]) => liveDirection(item))
      .sort(([a, x], [b, y]) => ordersOf(y) - ordersOf(x) || (y.performance?.video_count || 0) - (x.performance?.video_count || 0) || x.direction_id.localeCompare(y.direction_id, "zh", {numeric: true}));
    const paused = rows.filter(([, item]) => !liveDirection(item));
    const unbuilt = points.filter(point => point.tier === "S" && !parkedPoint(point) && !directions.some(item => item.selling_point_id === point.id) && keep(point, null));
    return {live, paused, unbuilt, points, directions};
  }
  function defaultPick(values) {
    const model = listModel(listView.catalog), all = [...model.live, ...model.paused].map(([, item]) => item);
    const remembered = picks()[values.product || "all"];
    return (all.find(item => item.direction_id === remembered) || all[0])?.direction_id || "";
  }

  // ---- left side: directions ranked by orders ----
  function itemTags(point, direction) {
    const tags = [], plan = direction.pending_plan;
    if (!liveDirection(direction)) return "";
    if (plan?.action === "stop") tags.push(tag("review", "待停测"));
    else if (plan) tags.push(stalePlan(plan) ? tag("review", `${planNames[plan.action]} · ${daysSince(plan.created_at)} 天没写新稿`) : tag("running", `${planNames[plan.action]}中`));
    else if (point && pointStatus(point) === "candidate") tags.push(tag("review", "还缺证明"));
    else if (direction.script_count === 0) tags.push(tag("", "还没有稿"));
    if ((direction.chain_issues || []).length) tags.push(tag("problem", "方向链待修改"));
    return tags.join("");
  }
  function listItem(point, direction, top, selected, allProducts) {
    const perf = direction.performance || {}, count = executionCount(direction), orders = ordersOf(direction);
    const code = point ? point.code : direction.direction_id.split("-").pop() || "—";
    const facts = [allProducts ? direction.product : "", `${count} 个角度`, perf.video_count ? `${perf.video_count} 条视频` : "", direction.script_count ? `${direction.script_count} 稿` : ""].filter(Boolean).join(" · ");
    const live = liveDirection(direction);
    return `<li class="dir-row dx-item has-direction${selected ? " is-active" : ""}${live ? "" : " is-paused"}" data-direction-id="${esc(direction.direction_id)}"${point ? ` data-point-id="${esc(point.id)}"` : ""}>
      <span class="dir-code tier-${esc(point ? point.tier : code[0])}">${esc(code)}</span>
      <div class="dx-item-main"><div class="dx-item-top"><a class="dir-title direction-card-link" href="${esc(directionURL(direction.product, direction.direction_id))}"${selected ? ' aria-current="true"' : ""} title="${esc(point?.title || "")}">${esc(shortMain(direction) || point?.title || direction.direction_id)}</a>
          <span class="dx-num${orders ? "" : " is-zero"}" title="${esc(performanceText(perf) || "还没有关联到出单视频")}">${perf.matched_count ? esc(orders) : "—"}<small>单</small></span></div>
        ${live ? `<div class="dx-bar" aria-hidden="true"><i data-bar="${top ? Math.round(orders / top * 100) : 0}"></i></div>` : ""}
        <div class="dir-sub dx-item-sub"><span>${esc(direction.status === "archived" ? `已归档 · ${facts}` : facts)}</span>${itemTags(point, direction)}</div></div></li>`;
  }
  // Bar lengths are set here: the page's security policy refuses style attributes written into markup.
  const paintBars = root => root.querySelectorAll("[data-bar]").forEach(bar => { const share = Number(bar.dataset.bar) || 0; bar.style.width = (share ? Math.max(share, 2) : 0) + "%"; });
  function renderList(values, target) {
    const host = $("#dir-list"); if (!host) return;
    const model = listModel(listView.catalog, values.q), allProducts = !values.product;
    const top = Math.max(0, ...model.live.map(([, item]) => ordersOf(item)));
    const total = model.live.reduce((sum, [, item]) => sum + ordersOf(item), 0);
    const items = rows => rows.map(([point, item]) => listItem(point, item, top, item.direction_id === target, allProducts)).join("");
    const empty = !model.live.length && !model.paused.length && !model.unbuilt.length;
    host.innerHTML = `${model.live.length ? `<h2 class="dx-sec"><b>在测 ${model.live.length}</b><span>按订单排${total ? ` · 合计 ${total} 单` : ""}</span></h2><ul class="dir-list dx-list">${items(model.live)}</ul>` : ""}
      ${model.unbuilt.length ? `<h2 class="dx-sec"><b>还没建方向</b><span>S 级卖点 · ${model.unbuilt.length}</span></h2><ul class="dx-unbuilt">${model.unbuilt.map(point => `<li data-point-id="${esc(point.id)}"><span class="dir-code tier-S">${esc(point.code)}</span><span class="dx-unbuilt-title" title="${esc(point.title)}">${esc(point.title)}</span><button type="button" class="subtle compact dir-build" data-use-point="${esc(point.id)}">建方向 ›</button></li>`).join("")}</ul>` : ""}
      ${model.paused.length ? `<h2 class="dx-sec"><b>已暂停 ${model.paused.length}</b></h2><ul class="dir-list dx-list is-paused">${items(model.paused)}</ul>` : ""}
      ${empty ? `<section class="section-card empty direction-empty"><h3>${values.q ? "没有匹配的方向" : "当前产品还没有方向"}</h3><p>${values.q ? "换个关键词，或清空搜索。" : "到「卖点表」选一个 S 级卖点，从它建立方向。"}</p></section>` : ""}`;
    paintBars(host);
    host.querySelectorAll("[data-use-point]").forEach(button => button.onclick = () => openCreate(button.dataset.usePoint, startSource ? {source: startSource} : {}));
  }

  // ---- 卖点表: one dense table, a row opens in place to be edited ----
  // Which live directions answer a concern or close with this point.
  function pointUsage(directions) {
    const used = new Map();
    for (const direction of directions.filter(liveDirection)) {
      const text = [direction.concern, direction.closing, ...(direction.variants || []).flatMap(variant => [variant.concern, variant.closing])].filter(Boolean).join(" ");
      for (const match of text.matchAll(codePattern)) {
        if (!used.has(match[1])) used.set(match[1], []);
        if (!used.get(match[1]).includes(direction)) used.get(match[1]).push(direction);
      }
    }
    return used;
  }
  const plain = text => String(text || "").replace(/\*\*/g, "");
  function pointRow(point, directions, used, allProducts) {
    const direction = directions.find(item => item.product === point.product && item.selling_point_id === point.id);
    const users = (used.get(point.code) || []).filter(item => item.product === point.product && item !== direction);
    const where = direction
      ? `<a class="state-tag${liveDirection(direction) ? " running" : ""}" href="${esc(directionURL(direction.product, direction.direction_id))}" title="${esc(direction.direction_id)}">${liveDirection(direction) ? "方向在测" : "方向" + esc(statusNames[direction.status] || "")} ›</a>`
      : point.tier === "S" && point.status !== "merged" ? `<button type="button" class="subtle compact dir-build" data-use-point="${esc(point.id)}">建方向 ›</button>`
      : users.length ? users.map(item => `<a class="dir-code tier-S" href="${esc(directionURL(item.product, item.direction_id))}" title="${esc(shortMain(item) || item.direction_id)}">${esc(item.direction_id.split("-").pop())}</a>`).join("")
      : '<span class="px-none">还没用上</span>';
    const orders = point.performance?.orders;
    return `<li class="dir-row px-row${parkedPoint(point) ? " is-off" : ""}" data-point-id="${esc(point.id)}">
      <span class="dir-code tier-${esc(point.tier)}">${esc(point.code)}</span>
      <div class="px-title"><button type="button" class="px-open" data-edit-point="${esc(point.id)}" aria-expanded="false" title="点开直接改">${esc(point.title)}</button>${allProducts ? `<small>${esc(point.product)}</small>` : ""}${point.grading_mismatch ? tag("review", "与分级表不一致", gradingDiff(point)) : ""}</div>
      <div class="px-evidence" title="${esc(plain(point.evidence))}">${point.evidence ? esc(plain(point.evidence)) : '<span class="px-none">待补充证据</span>'}</div>
      <div class="px-status">${pointStatusChip(point)}</div>
      <div class="px-used">${where}</div>
      <div class="px-orders${orders ? " has-orders" : ""}" title="${esc(performanceText(point.performance) || "主打它的视频还没有销量")}">${orders ? esc(orders) : "—"}</div></li>`;
  }
  function renderPoints(values) {
    const host = $("#dir-points"); if (!host) return;
    const {catalog} = listView, directions = catalog.directions || [], points = sortPoints(catalog.selling_points || []);
    const needle = values.q.toLocaleLowerCase(), used = pointUsage(directions), allProducts = !values.product;
    const inTier = (point, tier) => !tier || (tier === "parked" ? parkedPoint(point) : point.tier === tier);
    const matches = point => !needle || `${point.code} ${point.title} ${point.rationale || ""} ${point.evidence || ""}`.toLocaleLowerCase().includes(needle);
    const shown = points.filter(point => matches(point) && inTier(point, values.tier));
    const count = tier => points.filter(point => inTier(point, tier)).length;
    const groups = [["S", "S 级", "主打"], ["A", "A 级", "支撑"], ["B", "B 级", "加分与收口"]];
    const filters = [["", "全部"], ["S", "S"], ["A", "A"], ["B", "B"], ["parked", "待核与已并入"]];
    host.innerHTML = `<div class="px-tools"><div class="dir-segment" role="group" aria-label="按分级筛选">${filters.map(([key, label]) => `<button type="button" data-tier-filter="${key}" aria-pressed="${values.tier === key}">${label}<small>${count(key)}</small></button>`).join("")}</div>
        <span class="px-hint">点卖点名直接改</span><button type="button" class="secondary" id="new-selling-point">${icon("plus")}新增卖点</button></div>
      <div id="dir-point-new"></div>
      ${shown.length ? `<div class="px-table"><div class="px-head" aria-hidden="true"><span>编号</span><span>卖点</span><span>证据与边界</span><span>状态</span><span>用在哪</span><span>订单</span></div>
        ${groups.map(([key, label, role]) => { const list = shown.filter(point => point.tier === key); return list.length ? `<h2 class="px-tier">${label}<small>${role} · ${list.length}</small></h2><ul class="dir-list px-list">${list.map(point => pointRow(point, directions, used, allProducts)).join("")}</ul>` : ""; }).join("")}</div>`
        : `<section class="section-card empty direction-empty"><h3>${values.q || values.tier ? "当前筛选没有匹配的卖点" : "当前产品还没有卖点"}</h3><p>${values.q || values.tier ? "换个关键词，或清除筛选。" : "先新增一个明确的主打卖点，再从它建立方向。"}</p>${values.q || values.tier ? "" : `<button type="button" class="primary" data-empty-new-point>${icon("plus")}新增卖点</button>`}</section>`}`;
    const form = listView.form;
    host.querySelectorAll("[data-tier-filter]").forEach(button => button.onclick = () => { form.elements.tab.value = "points"; form.elements.tier.value = button.dataset.tierFilter; form.requestSubmit(); });
    $("#new-selling-point").onclick = () => openPointEditor();
    host.querySelector("[data-empty-new-point]")?.addEventListener("click", () => openPointEditor());
    host.querySelectorAll("[data-edit-point]").forEach(button => button.onclick = () => openPointEditor(button.dataset.editPoint));
    host.querySelectorAll("[data-use-point]").forEach(button => button.onclick = () => openCreate(button.dataset.usePoint));
    if (pendingPoint) { const next = pendingPoint; pendingPoint = null; void openPointEditor(next.id, next.adoptGrading); }
  }

  // ---- the page frame: tabs, one search box, then the body of the chosen tab ----
  let filterTimer = 0, composing = false, pendingPoint = null;
  const cancelFilterTimer = () => { clearTimeout(filterTimer); filterTimer = 0; };
  function mountPage(catalog, summary, values, keptPane = null) {
    cancelFilterTimer(); composing = false;
    const points = catalog?.selling_points || [], directions = catalog?.directions || [];
    const mismatched = points.filter(point => point.grading_mismatch);
    const active = document.activeElement, refocus = active?.closest?.("#direction-filters") && active.name ? {name: active.name, start: active.selectionStart, end: active.selectionEnd} : null;
    $("#content").innerHTML = `<div class="dx-page" data-tab="" data-pane="${values.id ? "detail" : "list"}">
      <div class="dx-head"><h1>卖点与方向</h1>
        <div class="dir-tabs dx-tabs" role="tablist" aria-label="卖点与方向">
          <button type="button" role="tab" data-dx-tab="directions" aria-selected="false">方向<small>${directions.length}</small></button>
          <button type="button" role="tab" data-dx-tab="points" id="dir-points-jump" aria-selected="false">卖点表<small>${points.length}</small>${mismatched.length ? `<span class="dx-dot" title="${esc(mismatched.map(point => point.code).join("、"))} 和分级表写法不一致">${mismatched.length} 处待核对</span>` : ""}</button></div>
        <form id="direction-filters" class="dx-search" role="search"><label class="search">${icon("search")}<input name="q" value="${esc(values.q)}" placeholder="搜索方向、卖点、顾虑" aria-label="搜索"></label>
          <input type="hidden" name="tab" value=""><input type="hidden" name="tier" value=""><input type="hidden" name="direction" value="">${values.archive !== "active" ? `<input type="hidden" name="archive" value="${esc(values.archive)}">` : ""}</form></div>
      <div class="dx-body"></div></div>`;
    const root = $("#content .dx-page"), form = $("#direction-filters");
    listView = catalog ? {catalog, summary, root, form, keptPane, product: values.product, archive: values.archive, signature: state.signature, digest: state.digest} : null;
    if (!catalog) {
      // A narrow window opened straight on one direction: only that direction is read.
      root.querySelector(".dx-body").innerHTML = '<div class="dx"><div class="dx-main" id="direction-pane"></div></div>';
      return;
    }
    form.onsubmit = event => {
      event.preventDefault(); const next = new URLSearchParams();
      for (const [key, value] of new FormData(form)) { const text = String(value).trim(); if (text && !(key === "archive" && text === "active")) next.set(key, text); }
      const target = pageURL("#directions" + (next.size ? "?" + next : "")); if (location.hash === target) void route(); else location.hash = target;
    };
    const applyFilters = (delay = 0) => {
      cancelFilterTimer();
      const submit = () => { filterTimer = 0; if (composing || !form.isConnected || !onDirections()) return; form.requestSubmit(); };
      if (delay) filterTimer = setTimeout(submit, delay); else submit();
    };
    form.addEventListener("compositionstart", () => { composing = true; });
    form.addEventListener("compositionend", () => { composing = false; applyFilters(150); });
    form.addEventListener("input", event => { if (!composing && !event.isComposing && event.target.name === "q") applyFilters(event.target.value.trim() ? 150 : 0); });
    form.addEventListener("keydown", event => { if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); applyFilters(); } });
    root.querySelectorAll("[data-dx-tab]").forEach(tab => tab.onclick = () => {
      if (tab.getAttribute("aria-selected") === "true") return;
      form.elements.tab.value = tab.dataset.dxTab === "points" ? "points" : ""; form.elements.tier.value = ""; form.requestSubmit();
    });
    if (refocus) { const field = form.elements[refocus.name]; if (field?.focus) { field.focus(); if (field.setSelectionRange && refocus.start != null) field.setSelectionRange(refocus.start, refocus.end); } }
  }
  const onDirections = () => /^#directions(?:[/?]|$)/.test(pageHash());
  function buildBody(values) {
    const body = listView.root.querySelector(".dx-body"), {catalog} = listView, product = values.product;
    paneSequence += 1; paneState = null;
    if (values.tab === "points") { body.innerHTML = '<section id="dir-points" class="px"></section>'; return; }
    const directions = catalog.directions || [];
    const start = product && !directions.length && (catalog.selling_points || []).some(point => point.tier === "S");
    body.innerHTML = `${start ? '<section class="section-card dir-start" id="dir-start" aria-live="polite"><p class="context-note">正在读取这个产品出单最多的视频…</p></section>' : ""}
      <div class="dx${directions.length ? "" : " is-empty"}"><aside class="dx-side" id="dir-list" aria-label="方向"></aside>${directions.length ? '<div class="dx-main" id="direction-pane"></div>' : ""}</div>
      ${product ? '<div class="dir-tk-slot" id="tk-candidates-slot"></div>' : ""}`;
    // After a save the direction on screen stays in place until its fresh data arrives.
    const kept = listView.keptPane; listView.keptPane = null;
    if (kept?.firstElementChild && $("#direction-pane")) { $("#direction-pane").replaceWith(kept); paneState = {id: kept.dataset.directionId || ""}; }
    window.TKCandidates?.mount($("#tk-candidates-slot"), product, catalog);
    if (start) void fillStart(catalog, product);
  }
  // Brings the mounted page in line with the address: tab, search, and which direction is open on the right.
  function applyView(values, reload = false, early = null) {
    const root = listView.root, form = listView.form;
    root.dataset.pane = values.id ? "detail" : "list";
    if (root.dataset.tab !== values.tab) { root.dataset.tab = values.tab; buildBody(values); }
    root.querySelectorAll("[data-dx-tab]").forEach(tab => tab.setAttribute("aria-selected", String(tab.dataset.dxTab === values.tab)));
    if (form.elements.q.value.trim() !== values.q) form.elements.q.value = values.q;
    form.elements.tab.value = values.params.get("tab") === "points" ? "points" : ""; form.elements.tier.value = values.tier;
    if (values.tab === "points") { renderPoints(values); return; }
    const target = values.id || (wide.matches ? defaultPick(values) : "");
    renderList(values, target);
    const host = $("#direction-pane"); if (!host) return;
    if (!target) { paneSequence += 1; paneState = null; host.removeAttribute("aria-busy"); host.innerHTML = ""; return; }
    if (reload || values.id || paneState?.id !== target || !host.firstElementChild) void loadPane(values, target, !!values.id, early);
  }

  // The selling videos of a product with no direction yet, to be sorted onto its S points; the 新建方向 dialog carries
  // the chosen ones as a note and links them to the new direction once it is saved.
  let startSource = "", startVideos = [], startPicks = new Map();
  async function fillStart(catalog, product) {
    const host = $("#dir-start"); startSource = ""; startVideos = []; startPicks = new Map();
    const sPoints = (catalog.selling_points || []).filter(point => point.product === product && point.tier === "S" && point.status !== "merged")
      .sort((a, b) => codeNo(a.code) - codeNo(b.code));
    try {
      const data = await api("/api/performance/videos?shop=local");
      if (!host.isConnected) return;
      const linked = new Set((catalog.video_links || []).map(link => link.video_id));
      const matches = window.WorkbenchShell?.matchesProduct ? (value => window.WorkbenchShell.matchesProduct(value)) : (value => String(value || "") === product);
      const mine = (data.videos || []).filter(video => matches(video.product)), sold = mine.filter(video => (video.orders || 0) > 0);
      startVideos = sold.filter(video => !linked.has(video.video_id)).sort((a, b) => (b.gmv || 0) - (a.gmv || 0)).slice(0, 5);
      if (!startVideos.length) { host.innerHTML = '<p class="context-note">这个产品还没有方向，本土店销售数据里也还没有出单的视频；先从 S 级卖点「建方向」。</p>'; return; }
      const gmv = sold.reduce((sum, video) => sum + (video.gmv || 0), 0);
      startSource = `参考：${product} 本土店出单最多的视频——${startVideos.slice(0, 3).map(video => `「${video.headline || video.video_id}」${video.orders} 单`).join("；")}。先看它们讲的是哪个 S 卖点。`;
      const options = `<option value="">归到卖点…</option>${sPoints.map(point => `<option value="${esc(point.id)}">${esc(point.code)} ${esc(point.title.split(/[：:]/)[0])}</option>`).join("")}`;
      host.innerHTML = `<div class="dir-start-head"><h2>还没有方向</h2><p>本土店 ${mine.length} 条视频里 ${sold.length} 条出过单，合计 $${money.format(Math.round(gmv))}。把出单最多的几条归到它们在讲的 S 卖点，再用归来的视频建方向；建好后这些视频自动挂到新方向上。</p></div>
        <div class="dir-start-grid"><section class="dir-start-videos"><h3>出单最多、还没归方向的视频</h3><ol class="dir-start-list">${startVideos.map((video, index) => `<li><a href="${esc(pageURL("#videos?video=" + encodeURIComponent(video.video_id)))}">${esc(video.headline || video.video_id)}</a><span>${esc(video.account || "")}</span><strong>$${money.format(Math.round(video.gmv || 0))}</strong><span>${esc(video.orders)} 单</span><select data-start-pick="${index}" aria-label="把这条视频归到哪个卖点">${options}</select></li>`).join("")}</ol></section>
        <section class="dir-start-points">${sPoints.map(point => `<div class="dir-start-point" data-start-point="${esc(point.id)}"><p><span class="dir-code tier-S">${esc(point.code)}</span><strong>${esc(point.title.split(/[：:]/)[0])}</strong></p><p class="dir-start-detail">${esc(point.title.split(/[：:]/).slice(1).join("：") || point.rationale || "")}</p><div><span data-start-count="${esc(point.id)}">已归 0 条</span><button type="button" class="secondary compact" data-start-build="${esc(point.id)}">用归来的视频建方向</button></div></div>`).join("")}</section></div>`;
      host.addEventListener("change", event => {
        const select = event.target.closest("[data-start-pick]"); if (!select) return;
        const video = startVideos[Number(select.dataset.startPick)];
        if (select.value) startPicks.set(video.video_id, select.value); else startPicks.delete(video.video_id);
        for (const point of sPoints) { const n = [...startPicks.values()].filter(id => id === point.id).length; const label = host.querySelector(`[data-start-count="${CSS.escape(point.id)}"]`); if (label) label.textContent = `已归 ${n} 条`; }
      });
      host.addEventListener("click", event => {
        const button = event.target.closest("[data-start-build]"); if (!button) return;
        const pointId = button.dataset.startBuild, videos = startVideos.filter(video => startPicks.get(video.video_id) === pointId);
        const source = videos.length ? `用这几条出单视频建方向：${videos.map(video => `「${video.headline || video.video_id}」${video.orders} 单`).join("；")}。保存后它们会挂到新方向上。` : startSource;
        void openCreate(pointId, {source, onSaved: videos.length ? result => linkStartVideos(product, result.direction.direction_id, videos) : undefined});
      });
    } catch {
      if (host.isConnected) host.innerHTML = '<p class="context-note">这个产品还没有方向；销售数据暂时读不到，先从 S 级卖点「建方向」。</p>';
    }
  }
  // Ties the videos sorted onto a point to the direction just built on it, one link at a time on the fresh catalog.
  async function linkStartVideos(product, directionId, videos) {
    let revision = (await api(catalogURL(product))).revision, linked = 0;
    for (const video of videos) {
      try { const result = await api("/api/video-directions", {method: "POST", body: {revision, video_id: video.video_id, product, direction_id: directionId, request_id: crypto.randomUUID()}}); revision = result.catalog?.revision || revision; linked += 1; }
      catch (error) { toast(`「${video.headline || video.video_id}」没挂上：${error.message}`, true); }
    }
    if (linked) toast(`已把 ${linked} 条视频挂到 ${directionId}`);
  }

  // ---- right side: one direction ----
  // Only execution variants are numbered. The direction definition keeps its place without a number; the catalog only
  // appends, so a number never changes once given.
  const variantNumber = (variants, index) => variants[index] ? variants.slice(0, index + 1).filter(item => item.variant_type !== "direction").length : index + 1;
  const variantTag = (variant, index, variants) => variant.variant_type === "direction" ? "方向定义" : "#" + variantNumber(variants, index);
  // What changed, without the bookkeeping prefix ("发布后补录，无基准稿：") or a dated note about where it was moved from.
  const shortChange = text => String(text || "").replace(/^[^：:]{1,12}[：:]\s*/, "").replace(/\s*20\d\d-\d\d-\d\d\s*由.*$/, "") || String(text || "");
  const structureOf = variant => String(variant.progression || "").replace(/^其他[:：]/, "");
  let judging = false;
  // One step: the server settles the plans this judgement replaces and pauses or resumes the direction.
  async function judge(data, value, current, openPlan) {
    const d = data.direction;
    if (judging || value === current) return;
    const note = String(openPlan?.note || "").trim();
    const settles = openPlan && openPlan.action !== value ? `现在的判断「${planNames[openPlan.action]}」${note ? `（${note.slice(0, 60)}）` : ""}会记为${openPlan.status === "working" ? "完成" : "取消"}。` : "";
    if (value === "stop" && !confirm(`停测「${shortMain(d) || d.direction_id}」？方向会移到「已暂停」，之后选别的判断就能恢复。${settles}`)) return;
    if (value !== "stop" && note && settles && !confirm(`${settles}改成「${planNames[value]}」吗？`)) return;
    judging = true; $$("[data-direction-plan]").forEach(button => button.disabled = true);
    try {
      const result = await api("/api/direction-judgement", {method: "POST", body: {product: d.product, direction_id: d.direction_id, judgement: value, request_id: crypto.randomUUID()}});
      toast(value === "stop" ? `${d.direction_id} 已停测，移到已暂停` : `${d.direction_id} 这一轮：${planNames[value]}${result.status === "testing" && d.status === "paused" ? "，已恢复测试" : ""}`);
    } catch (error) { toast(error.message, true); }
    finally { judging = false; }
    void route(true);
  }
  // The direction keeps what writing reads (the chain, audience, 核心证明/不能说) and the angles already tried.
  function renderPane(host, data, values, points = []) {
    const d = data.direction, p = data.selling_point;
    const progressAvailable = data.progress_available === true && !window.PublicTextMode;
    const variants = d.variants || [], scripts = data.scripts || [], videos = data.videos || [], tasks = data.tasks || [], cases = data.cases || [];
    let relatedTouched = false;
    // The latest open decision is the direction's current judgement; test cards recorded earlier are not shown.
    const opened = (data.plans || []).filter(plan => !plan.experiment && ["pending", "working"].includes(plan.status))
      .sort((a, b) => String(a.created_at || "").localeCompare(String(b.created_at || "")));
    const openPlan = opened.length ? {...opened.at(-1), ...(d.pending_plan?.id === opened.at(-1).id ? d.pending_plan : {})} : Array.isArray(data.plans) ? null : d.pending_plan || null;
    const paused = !liveDirection(d), archived = d.status === "archived";
    // A 停测 decided in a review but not carried out yet presses nothing: 停测 confirms it, any other choice replaces it.
    const stopWaiting = !paused && openPlan?.action === "stop";
    const judged = paused ? "stop" : stopWaiting ? "" : openPlan ? openPlan.action : "observe";
    const days = openPlan ? daysSince(openPlan.created_at) : null, decided = openPlan?.created_at ? `${openPlan.created_at.slice(5, 10)} 定` : "";
    const since = !openPlan || openPlan.scripts_since == null ? "" : openPlan.scripts_since > 0 ? `之后写了 ${openPlan.scripts_since} 稿` : stalePlan(openPlan) ? `${days} 天没写新稿` : "还没写新稿";
    const planLine = archived ? "已归档，在「编辑方向」里恢复" : paused ? "已停测 · 选加码、换执行或观察就恢复测试"
      : stopWaiting ? `${decided ? decided + "了" : "定了"}停测，还没执行 · 点「停测」把方向移到已暂停`
      : openPlan ? [planNames[openPlan.action], decided, since].filter(Boolean).join(" · ") : "";
    const planNote = !paused && openPlan?.note ? String(openPlan.note).trim() : "";
    // A line about the round is optional and typed in place; it shows on 工作概览 with the plan.
    const noteMarkup = !paused && openPlan?.id ? `<button type="button" class="dx-plan-note${planNote ? "" : " is-empty"}" id="direction-plan-note" title="点一下${planNote ? "改" : "写"}备注">${planNote ? esc(planNote) : "写备注"}</button>` : "";
    const taskItems = items => items.map(task => `<a class="direction-related-item" href="${esc(pageURL("#ai/" + encodeURIComponent(task.task_id)))}"><strong>${esc(task.display_title || task.case_title || task.case_id || task.task_id)}</strong><span>${esc(taskStatusNames[task.status] || task.status || "状态未提供")} · ${esc(readableDate(task.updated_at))}</span></a>`).join("");
    const related = [
      ["scripts", "采用稿", scripts, scripts.map(script => script.script_id || script.id ? `<a class="direction-related-item" href="${esc(pageURL("#content/" + (script.script_id || script.id)))}"><strong>${esc(script.title)}</strong><span>第 ${esc(script.version || 1)} 版 · ${esc({draft: "草稿", adopted: "已采纳", filmed: "已拍摄", published: "已发布", reviewed: "已复盘", archived: "已归档"}[script.status] || script.status || "")}</span></a>` : `<details class="direction-script"><summary><strong>${esc(script.script_version || "版本未标记")}</strong> ${esc(script.title || script.case_id)}</summary><pre>${esc(script.text || "未记录文案")}</pre><a href="${esc(pageURL("#case/" + script.case_id + "/script"))}">查看案例文案</a></details>`).join(""), "还没有关联的采用稿。"],
      ["videos", "关联视频", videos, videos.map(video => `<a class="direction-related-item" href="${esc(pageURL("#videos?video=" + encodeURIComponent(video.video_id)))}"><strong>${esc(video.title || video.video_id)}</strong><span>${esc(video.video_id)}</span></a>`).join(""), "还没有关联视频。"],
      ["tasks", "AI 任务", tasks, taskItems(tasks), "还没有关联的 AI 协作任务。"],
      ["cases", "关联案例", cases, cases.map(item => `<a class="direction-related-item" href="${esc(pageURL("#case/" + item.case_id))}"><strong>${esc(item.current?.title || item.title || item.case_id)}</strong><span>${esc(item.case_id)}${item.archived ? " · 已归档" : ""}</span></a>`).join(""), "方向已经独立保存，后续案例可以使用这个编号。"],
    ];
    const firstRelated = (related.find(([, , list]) => list.length) || related[0])[0];
    // Empty tabs stay in the page (tasks arrive later) but are hidden; the card hides when every tab is empty.
    const relatedEmpty = ([key, , list]) => !list.length && !(key === "tasks" && data.tasks_pending === true);
    const relatedMarkup = ([key, , list, markup, emptyText]) => key === "tasks" && data.tasks_pending === true ? '<p class="context-note" role="status">正在读取 AI 任务…</p>' : list.length ? `<div class="direction-related-list">${markup}</div>${list.length > 5 ? `<button type="button" class="subtle dir-related-more" data-related-more>显示全部 ${list.length} 条</button>` : ""}` : `<p class="dir-empty">${esc(emptyText)}</p>`;
    const perf = d.performance || {}, numbered = executionCount(d);
    const stat = (label, value, note = "") => `<div class="dx-stat"><span>${label}</span><b>${value}${note ? `<small>${note}</small>` : ""}</b></div>`;
    const stats = perf.video_count ? `<div class="dx-stats" id="direction-sales" title="${esc(performanceText(perf))}">${[
        stat("订单", perf.matched_count ? esc(perf.orders) : "—"),
        perf.gmv ? stat("成交金额", "$" + money.format(Math.round(perf.gmv))) : "",
        stat("视频", esc(perf.video_count), perf.matched_count !== perf.video_count ? `${perf.matched_count || 0} 条有销量` : ""),
        perf.ctor_pct != null ? stat("CTOR", esc(perf.ctor_pct) + "%") : "",
        d.script_count != null ? stat("采用稿", esc(d.script_count)) : "",
        perf.data_through ? `<div class="dx-stat is-date"><span>数据截至</span><b>${esc(perf.data_through)}</b></div>` : ""].join("")}</div>`
      : `<p class="dx-stats is-empty" id="direction-sales">还没有关联视频${d.script_count ? ` · 已有 ${esc(d.script_count)} 稿` : ""}</p>`;
    const flags = `${d.lead_tier && d.lead_tier !== "S" ? `<span class="dir-flag" title="${esc(d.notes || "方向应由 S 级卖点主打")}">${esc(d.lead_tier)} 级主打</span>` : ""}`;
    host.dataset.directionId = d.direction_id;
    host.innerHTML = `<a class="back-link dx-back" href="${esc(window.WorkbenchShell?.parentURL?.() || directionURL(d.product))}">${icon("back")}返回卖点与方向</a>
      <section class="section-card dir-card dx-card" id="direction-definition">
        <div class="dx-title">${p ? `<span class="dir-code tier-${esc(p.tier)}">${esc(p.code)}</span>` : ""}<h2>${esc(shortMain(d) || p?.title || d.main_difference)}<small>${esc(d.direction_id)}</small></h2>${flags}
          <div class="dx-title-actions"><button type="button" class="secondary" id="direction-edit">编辑方向</button><button type="button" class="primary" id="direction-write" title="用这个方向写稿">${icon("edit")}写稿</button></div></div>
        <div class="dx-judge"><span class="dx-judge-label">这一轮</span><div class="dir-segment" id="direction-plans" role="group" aria-label="这一轮的判断">${["scale", "execution", "observe", "stop"].map(key => `<button type="button" data-direction-plan="${key}" aria-pressed="${judged === key}"${stopWaiting && key === "stop" ? ' class="is-waiting"' : ""}${archived ? " disabled" : ""}>${planNames[key]}</button>`).join("")}</div>
          ${planLine ? `<p class="dx-plan-line${stalePlan(openPlan) || stopWaiting ? " is-late" : ""}"><span>${esc(planLine)}</span>${noteMarkup}</p>` : ""}</div>
        ${stats}
        <div class="direction-chain dx-chain">${chainStages(d, points)}</div>
        ${(d.chain_issues || []).length ? `<p class="dir-note is-warn">方向链需要修改：${esc(d.chain_issues.join("；"))}</p>` : ""}${d.audience ? `<p class="dir-note dir-audience"><strong>讲给谁</strong> ${esc(d.audience)}</p>` : ""}${notesMarkup(d.notes)}
        ${p ? `<details class="dir-point-evidence"><summary>主打卖点 ${esc(p.code)} 的分级理由与证据${pointStatusChip(p)}</summary>${p.rationale ? `<p><strong>分级理由</strong> ${esc(p.rationale)}</p>` : ""}${p.evidence ? `<p><strong>证据与边界</strong> ${esc(p.evidence)}</p>` : '<p>待补充证据</p>'}</details>` : '<p class="dir-note">这个方向没有关联卖点。</p>'}</section>
      <section class="section-card dir-card dx-card" id="direction-variants"><div class="dir-card-head"><h2>已测角度<small>${numbered}</small></h2><span class="dx-angle-note" id="direction-angle-note"></span><div class="dir-card-actions"><button type="button" class="subtle" id="direction-add-variant">${icon("plus")}新增角度</button></div></div>
        <p class="dir-progress-summary" id="direction-progress-summary" role="status" hidden></p>
        <p class="dir-note is-warn" id="direction-workflow-target-note" role="status" hidden></p>
        ${variants.length ? '<div class="dir-variant-columns" aria-hidden="true"><span class="dir-variant-tag">角度</span><span class="dir-variant-structure">结构</span><span class="dir-variant-change">改了什么</span><span class="dir-variant-num is-videos">视频</span><span class="dir-variant-orders">订单</span><span class="dir-variant-num is-ctor">CTOR</span></div>' : ""}
        <div class="direction-variant-list">${variants.map((variant, index) => `<div class="dir-variant-row" data-variant-row="${index}"><details class="dir-variant" data-variant-index="${index}">
          <summary><strong class="dir-variant-tag">${variantTag(variant, index, variants)}</strong><span class="dir-variant-structure" title="${esc(variant.progression || "结构未写")}">${esc(structureOf(variant) || "—")}</span><span class="dir-variant-change" title="${esc(variant.execution_change || "")}">${esc(shortChange(variant.execution_change) || (variant.variant_type === "direction" ? "方向的默认链" : "没写改了什么"))}</span>
            <span class="dir-variant-num is-videos" data-variant-videos="${index}">${progressAvailable ? "…" : "—"}</span><span class="dir-variant-orders"><span class="dx-bar" aria-hidden="true"><i data-variant-bar="${index}"></i></span><b class="dir-variant-num" data-variant-orders="${index}" title="${progressAvailable ? "" : "重启工作台后可看各条订单"}">${progressAvailable ? "…" : "—"}</b></span><span class="dir-variant-num is-ctor" data-variant-ctor="${index}">${progressAvailable ? "…" : "—"}</span></summary>
          <div class="dir-variant-body"><div class="dx-chain is-angle">${chainStages({concern: variant.concern, closing: variant.closing}, points, false)}</div>${variant.execution_change ? `<p class="dir-change">改动：${esc(variant.execution_change)}</p>` : ""}${variant.audience ? `<p class="dir-change">讲给谁：${esc(variant.audience)}</p>` : ""}${(variant.chain_issues || []).length ? `<p class="dir-note">${esc(variant.chain_issues.join("；"))}（已发布的记录保持原样）</p>` : ""}
            ${variant.conflict ? '<p class="dir-note is-warn">这条的方向链有冲突，先核对。</p>' : `<div class="dir-variant-actions"><label class="dir-progression"><span>推进结构</span><input list="dir-progressions" maxlength="60" value="${esc(structureOf(variant))}" placeholder="例如：问题-解决" data-variant-progression="${index}" aria-label="${esc(variantTag(variant, index, variants))} 的推进结构"></label><button type="button" class="subtle" data-variant-progression-save="${index}">保存结构</button></div>`}</div></details>
          ${variant.conflict ? '<span class="dir-variant-write"></span>' : `<button type="button" class="subtle compact dir-variant-write" data-variant-write="${index}" aria-label="按这条再写一稿" title="按这条再写一稿">再写<span>一稿</span></button>`}</div>`).join("")}</div>
        <details class="dx-idle" id="direction-idle" hidden><summary></summary><div class="dx-idle-list"></div></details>
        <details class="dx-loose" id="direction-loose" hidden><summary></summary><ul></ul></details>
        <datalist id="dir-progressions">${progressions.map(item => `<option value="${esc(item)}"></option>`).join("")}</datalist>
        ${!variants.length ? '<p class="context-note">还没有已测角度。</p>' : ""}
        ${(d.history || []).length ? `<details class="direction-history"><summary>旧定义 · ${d.history.length}</summary>${d.history.map(item => `<div class="direction-definition"><small>${esc(readableDate(item.replaced_at))} 前</small><p class="direction-chain">${esc(directionChain(item))}</p></div>`).join("")}</details>` : ""}</section>
      <details class="section-card dir-card dx-card dir-related"${related.every(relatedEmpty) ? " hidden" : ""}><summary class="dir-related-summary">关联内容 · 采用稿 ${scripts.length} · 视频 ${videos.length}</summary><div class="dir-tabs" role="tablist" aria-label="关联内容">${related.map(([key, label, list]) => `<button type="button" role="tab" id="direction-${key}" aria-controls="direction-${key}-panel" aria-selected="${key === firstRelated}" data-related-tab="${key}"${relatedEmpty([key, label, list]) ? " hidden" : ""}>${label}<small>${key === "tasks" && data.tasks_pending === true ? "…" : list.length}</small></button>`).join("")}</div>
          ${related.map(item => `<div class="dir-related-panel" role="tabpanel" id="direction-${item[0]}-panel" aria-labelledby="direction-${item[0]}"${item[0] === "tasks" && data.tasks_pending === true ? ' aria-busy="true"' : ""}${item[0] === firstRelated ? "" : " hidden"}>${relatedMarkup(item)}</div>`).join("")}</details>`;
    const find = selector => host.querySelector(selector), all = selector => [...host.querySelectorAll(selector)];
    const variantDetails = all("details.dir-variant"), variantList = find(".direction-variant-list");
    find("#direction-add-variant").onclick = () => openCreate(d.selling_point_id);
    find("#direction-edit").onclick = () => openDirectionEditor(data);
    all("[data-direction-plan]").forEach(button => button.onclick = () => judge(data, button.dataset.directionPlan, judged, openPlan));
    const noteButton = find("#direction-plan-note");
    if (noteButton) noteButton.onclick = () => {
      const input = document.createElement("input");
      input.className = "dx-plan-note-input"; input.maxLength = 2000; input.value = planNote; input.placeholder = "这一轮打算怎么做，写一句"; input.setAttribute("aria-label", "这一轮的备注");
      noteButton.replaceWith(input); input.focus();
      let settled = false;
      const finish = async save => {
        if (settled) return; settled = true;
        const value = input.value.trim();
        if (!save || value === planNote) { input.replaceWith(noteButton); return; }
        input.disabled = true;
        try {
          const revision = (await api("/api/creation?product=" + encodeURIComponent(d.product))).revision;
          await api("/api/plans", {method: "POST", body: {revision, request_id: crypto.randomUUID(), plan: {id: openPlan.id, note: value}}});
          toast("备注已保存");
        } catch (error) { toast(error.message, true); }
        void route(true);
      };
      input.addEventListener("keydown", event => {
        if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); void finish(true); }
        else if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); void finish(false); }
      });
      input.addEventListener("blur", () => void finish(true));
    };
    const saveMeta = async meta => {
      try { await api("/api/direction-catalog", {method: "POST", body: {revision: data.revision, request_id: crypto.randomUUID(), operation: "variant_meta", direction: {product: d.product, direction_id: d.direction_id, ...meta}}}); toast("推进结构已保存"); }
      catch (error) { toast(error.message, true); }
      void route(true);
    };
    all("[data-variant-progression-save]").forEach(button => button.onclick = () => {
      const index = Number(button.dataset.variantProgressionSave), value = find(`[data-variant-progression="${index}"]`).value.trim();
      if (!value) { toast("先写推进结构，例如“问题-解决”。", true); return; }
      void saveMeta({index, progression: value});
    });
    all("[data-variant-progression]").forEach(input => input.addEventListener("keydown", event => { if (event.key === "Enter" && !event.isComposing) { event.preventDefault(); find(`[data-variant-progression-save="${input.dataset.variantProgression}"]`).click(); } }));
    all("[data-variant-write]").forEach(button => button.onclick = () => window.CaseAI?.startFromDirection(d, {variantIndex: Number(button.dataset.variantWrite)}));
    find("#direction-write").onclick = () => {
      if (p && parkedPoint(p) && !confirm(`${p.code} 还是${p.status === "merged" ? "已并入其他编号的卖点" : "待核候选"}，没有核实或缺证明素材。建议先拍到证明镜头再写稿。仍要现在写吗？`)) return;
      window.CaseAI?.startFromDirection(d);
    };
    const selectRelated = key => {
      all("[data-related-tab]").forEach(item => item.setAttribute("aria-selected", String(item.dataset.relatedTab === key)));
      all(".dir-related-panel").forEach(panel => panel.hidden = panel.id !== `direction-${key}-panel`);
    };
    find(".dir-related").addEventListener("click", event => {
      const more = event.target.closest("[data-related-more]"); if (!more) return;
      more.previousElementSibling.classList.add("is-all"); more.remove();
    });
    all("[data-related-tab]").forEach(tab => tab.onclick = () => { relatedTouched = true; selectRelated(tab.dataset.relatedTab); });
    const cells = kind => all(`[data-variant-${kind}]`);
    const blank = text => ["videos", "orders", "ctor"].forEach(kind => cells(kind).forEach(cell => { cell.textContent = text; cell.classList.remove("has-orders"); }));
    let workflowTargetApplied = false;
    // A link from elsewhere (?variant=…) opens that angle.
    const locateWorkflowTarget = progress => {
      if (workflowTargetApplied) return;
      workflowTargetApplied = true;
      const notices = [];
      if (values.params.has("variant")) {
        const key = values.params.get("variant"), matches = progress.variants.filter(item => key && item.key === key);
        if (matches.length !== 1) {
          notices.push(matches.length ? "当前方向有多个相同的执行变体，暂时无法唯一定位，请先核对变体。" : "这条执行变体已调整或不再存在，请核对当前方向。未自动定位到其他变体。");
        } else if (variants[matches[0].index]?.conflict) {
          notices.push("这条执行变体的方向链存在冲突，请先核对后再查看关联输出。");
        } else {
          const details = variantDetails.find(item => Number(item.dataset.variantIndex) === matches[0].index);
          if (details) {
            const fold = details.closest("details.dx-idle"); if (fold) fold.open = true;
            details.open = true; details.dataset.variantKey = key;
            details.querySelector(":scope > summary")?.focus({preventScroll: true});
            details.closest(".dir-variant-row").scrollIntoView({block: "start", behavior: "smooth"});
          }
        }
      }
      const note = find("#direction-workflow-target-note"); note.textContent = notices.join(" "); note.hidden = !notices.length;
    };
    // Angles that sold come first; those with no video yet step aside into one fold, unless somebody has one open.
    const arrange = progress => {
      const rows = progress.variants.map(item => ({index: item.index, orders: item.performance?.orders || 0, videos: item.performance?.video_count || 0}));
      const filmed = rows.filter(row => row.videos > 0).sort((a, b) => b.orders - a.orders || b.videos - a.videos || a.index - b.index);
      const note = find("#direction-angle-note"), fold = find("#direction-idle");
      const rowOf = index => find(`[data-variant-row="${index}"]`);
      if (!filmed.length) { note.textContent = ""; return; }
      const idle = rows.filter(row => !row.videos && !rowOf(row.index).querySelector("details.dir-variant").open);
      const shown = rows.filter(row => !filmed.includes(row) && !idle.includes(row));
      if (!variantList.contains(document.activeElement)) for (const row of [...filmed, ...shown]) variantList.append(rowOf(row.index));
      note.textContent = `${filmed.length} 个出过片 · 按订单排`;
      if (idle.length) {
        const list = fold.querySelector(".dx-idle-list");
        for (const row of idle) list.append(rowOf(row.index));
        fold.querySelector("summary").textContent = `还没出片的角度 · ${idle.length}（${idle.map(row => variantTag(variants[row.index], row.index, variants)).join(" ")}）`;
        fold.hidden = false;
      }
    };
    // Videos tied to the direction that no angle claims: their orders explain the gap between the total and the rows.
    const showLoose = progress => {
      const loose = progress.unassigned?.videos || [], box = find("#direction-loose");
      if (!loose.length) { box.hidden = true; return; }
      const orders = loose.reduce((sum, video) => sum + (Number(video.orders) || 0), 0);
      box.querySelector("summary").textContent = `${loose.length} 条视频${orders ? ` · ${orders} 单` : ""}还没归到角度`;
      box.querySelector("ul").innerHTML = loose.map(video => `<li><a href="${esc(pageURL("#videos?video=" + encodeURIComponent(video.video_id)))}">${esc(video.headline || video.video_id)}</a><span>${video.orders != null ? `${esc(Number(video.orders))} 单` : "销售数据未匹配"}</span></li>`).join("");
      box.hidden = false;
    };
    return {
      loadingProgress() {
        blank("…");
        variantList.classList.remove("has-progress");
        find("#direction-progress-summary").hidden = true;
      },
      setProgress(progress) {
        if (progress.catalog_revision !== data.revision || progress.direction_id !== d.direction_id || progress.product !== d.product || progress.variants?.length !== variants.length)
          throw new Error("方向定义已更新，请刷新方向后重新核对进展。");
        const indexes = new Set(progress.variants.map(item => item.index));
        if (indexes.size !== variants.length || variants.some((_, index) => !indexes.has(index))) throw new Error("变体进展不完整，请重新读取。");
        find("#direction-progress-summary").hidden = true;
        // Each angle keeps its own sales; writing, production and publishing live on their own pages.
        const best = Math.max(0, ...progress.variants.map(item => item.performance?.orders || 0));
        for (const item of progress.variants) {
          const sales = item.performance || {}, orders = find(`[data-variant-orders="${item.index}"]`), bar = find(`[data-variant-bar="${item.index}"]`);
          orders.textContent = sales.matched_count ? sales.orders : "—"; orders.classList.toggle("has-orders", sales.orders > 0);
          orders.title = sales.video_count ? `${sales.video_count} 条视频，${sales.matched_count || 0} 条有销售数据${sales.data_through ? `，截至 ${sales.data_through}` : ""}` : "还没有关联视频";
          bar.dataset.bar = best && sales.orders ? Math.round(sales.orders / best * 100) : 0;
          find(`[data-variant-videos="${item.index}"]`).textContent = sales.video_count || "—";
          find(`[data-variant-ctor="${item.index}"]`).textContent = sales.ctor_pct != null ? sales.ctor_pct + "%" : "—";
        }
        paintBars(host);
        arrange(progress); showLoose(progress);
        variantList.classList.add("has-progress");
        locateWorkflowTarget(progress);
      },
      progressError(error, retry) {
        blank("—");
        variantList.classList.remove("has-progress");
        const node = find("#direction-progress-summary"); node.hidden = false;
        node.innerHTML = `<span class="dir-note is-warn">${esc(error.message || "各条订单暂时读取失败。")}</span> <button type="button" class="subtle" id="direction-progress-retry">${error.message?.includes("方向定义已更新") ? "刷新方向" : "重新读取"}</button>`;
        find("#direction-progress-retry").onclick = error.message?.includes("方向定义已更新") ? () => void route(true) : retry;
      },
      loadingTasks() {
        find("#direction-tasks small").textContent = "…";
        const panel = find("#direction-tasks-panel"); panel.setAttribute("aria-busy", "true"); panel.innerHTML = '<p class="context-note" role="status">正在读取 AI 任务…</p>';
      },
      setTasks(items) {
        data.tasks = Array.isArray(items) ? items : []; delete data.tasks_pending;
        related[2][2] = data.tasks; related[2][3] = taskItems(data.tasks);
        find("#direction-tasks small").textContent = String(data.tasks.length);
        find("#direction-tasks").hidden = !data.tasks.length;
        find(".dir-related").hidden = related.every(([, , list]) => !list.length);
        const panel = find("#direction-tasks-panel"); panel.removeAttribute("aria-busy"); panel.innerHTML = relatedMarkup(related[2]);
        if (!relatedTouched && data.tasks.length && !scripts.length && !videos.length) selectRelated("tasks");
      },
      taskError(error, retry) {
        find("#direction-tasks small").textContent = "—";
        const panel = find("#direction-tasks-panel"); panel.removeAttribute("aria-busy"); panel.innerHTML = `<p class="dir-note is-warn" role="alert">${esc(error.message || "AI 任务读取失败")}</p><button type="button" class="secondary" id="direction-task-retry">重新读取</button>`;
        find("#direction-task-retry").onclick = retry;
      },
    };
  }
  // Reads one direction into the right side. What is on screen stays until the new data arrives.
  async function loadPane(values, id, explicit, early = null) {
    const host = $("#direction-pane"); if (!host) return;
    const sequence = ++paneSequence, current = () => sequence === paneSequence && host.isConnected && onDirections();
    const product = listView?.catalog.directions?.find(item => item.direction_id === id && (!values.product || item.product === values.product))?.product || values.product;
    const moved = paneState?.id && paneState.id !== id;
    paneState = {id};
    host.setAttribute("aria-busy", "true");
    if (!host.firstElementChild) host.innerHTML = '<p role="status">正在读取卖点与方向…</p>';
    const settle = promise => promise.then(value => ({value}), error => ({error}));
    // Per-angle sales are read alongside the detail once the list has said this server offers them.
    const canProgress = listView?.catalog.progress_available === true && !window.PublicTextMode;
    const progressURL = "/api/direction-progress/" + encodeURIComponent(id) + "?" + new URLSearchParams({product});
    const earlyProgress = canProgress && product ? settle(api(progressURL)) : null;
    const answer = await (early || settle(api(catalogURL(product, id))));
    if (!current()) return;
    host.removeAttribute("aria-busy");
    if (answer.error) {
      host.innerHTML = `<section class="section-card dir-card dx-card"><p class="dir-note is-warn" role="alert">${esc(answer.error.message || "方向读取失败")}</p><button type="button" class="secondary" id="direction-pane-retry">重新读取</button></section>`;
      host.querySelector("#direction-pane-retry").onclick = () => void loadPane(values, id, explicit);
      return;
    }
    const data = answer.value;
    // New servers return the names from the same snapshot as the detail; the mounted list has them too.
    let points = Array.isArray(data.selling_points) ? data.selling_points : listView?.catalog.selling_points;
    if (!points) { points = (await api(catalogURL(product)).catch(() => null))?.selling_points || []; if (!current()) return; }
    if (explicit) rememberPick(values.product, id);
    const detail = renderPane(host, data, values, points);
    if (moved && host.getBoundingClientRect().top < 0) host.scrollIntoView({block: "start"});
    const afterPaint = action => requestAnimationFrame(() => setTimeout(() => { if (current()) action(); }, 0));
    if (data.progress_available === true && !window.PublicTextMode) {
      const loadProgress = async first => {
        if (!current()) return;
        detail.loadingProgress();
        const result = await (first || settle(api("/api/direction-progress/" + encodeURIComponent(data.direction.direction_id) + "?" + new URLSearchParams({product: data.direction.product}))));
        if (!current()) return;
        try { if (result.error) throw result.error; detail.setProgress(result.value); }
        catch (error) { detail.progressError(error, () => void loadProgress()); }
      };
      if (earlyProgress) void loadProgress(earlyProgress); else afterPaint(() => void loadProgress());
    }
    if (data.tasks_pending === true) {
      const loadTasks = async () => {
        if (!current()) return;
        detail.loadingTasks();
        try {
          const response = await api("/api/ai/tasks?" + new URLSearchParams({product: data.direction.product, direction_id: data.direction.direction_id}));
          if (current()) detail.setTasks(response.tasks || []);
        } catch (error) { if (current()) detail.taskError(error, () => void loadTasks()); }
      };
      // Let the complete direction and its controls paint before reading task history.
      afterPaint(() => void loadTasks());
    }
  }
  async function openCreate(preselected="",options={}) {
    const product=window.WorkbenchShell?.product() || routeValues().product;
    if(!product){toast("请先在左侧选择一个产品，再新建方向。",true);return;}
    let dialog=$("#direction-create-dialog");
    if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-create-dialog";dialog.setAttribute("aria-labelledby","direction-create-title");document.body.append(dialog);}
    dialog.innerHTML=`<div class="dialog-heading"><h2 id="direction-create-title">新建方向</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div><p class="direction-dialog-loading" role="status">正在读取产品卖点…</p>`;
    dialog.querySelector("[data-close]").onclick=()=>requestClose(dialog);dialog.showModal();trackDraft(dialog);
    let catalog,materials;
    try {[catalog,materials]=await Promise.all([api(catalogURL(product)),api("/api/materials?product="+encodeURIComponent(product)).catch(()=>null)]);}catch(error){dialog.querySelector("[role=status]").textContent=error.message;return;}
    if(!dialog.open)return;
    const points=catalog.selling_points || [],directions=catalog.directions || [];
    // Merged codes are kept for old records only; they are never offered as a lead or chain link.
    const leads=points.filter(item=>item.status!=="merged"),chainPoints=leads;
    const draft={point_id:preselected,concern:"",closing:"",lead_reason:"",progression:"",execution_change:"",audience:"",...(options.prefill||{})};let pending=false,requestId=crypto.randomUUID(),target="concern";
    const point=()=>points.find(item=>item.id===draft.point_id);
    const collect=()=>{
      const read=name=>dialog.querySelector(`[name="${name}"]`)?.value.trim() ?? draft[name];
      draft.point_id=dialog.querySelector('[name="lead_point"]:checked')?.value || draft.point_id;
      for(const name of ["lead_reason","concern","closing","progression","execution_change","audience"])draft[name]=read(name);
    };
    const choice=item=>`<label class="direction-point-choice"><input type="radio" name="lead_point" value="${esc(item.id)}" ${draft.point_id===item.id?"checked":""}><span><strong>${esc(item.code)} · ${esc(item.title)}</strong>${pointStatusChip(item)}${item.rationale?`<small>${esc(item.rationale)}</small>`:""}</span></label>`;
    function render(){
      const chosen=point(),id=chosen?`${product}-${chosen.code}`:"选择主打后自动生成",existing=directions.find(item=>item.direction_id===id);
      const main=existing?.main_difference || (chosen?chosen.code+" "+chosen.title:"");
      const linkedThemes=(materials?.theme_links || []).filter(link=>link.selling_point_id===chosen?.id || link.direction_id===id || link.target_id===chosen?.id || link.target_id===id);
      const reviewConcerns=linkedThemes.map(link=>link.concern || link.source_quote || link.theme).filter(Boolean);
      const known=key=>[...new Set(directions.flatMap(item=>[item,...(item.variants || [])]).map(item=>fieldText(item[key]).trim()).filter(Boolean))];
      const concerns=known("concern"),closings=known("closing");
      const suggestions=(label,items,attr)=>items.length?`<details class="direction-suggestions"><summary>${label} · ${items.length}</summary>${items.map((text,index)=>`<button type="button" class="secondary" ${attr}="${index}">${esc(text)}</button>`).join("")}</details>`:"";
      // With a point chosen up front the picker folds to one line; it can still be changed.
      const picker=`<fieldset class="direction-lead-picker"><legend>哪个 S 级卖点主打？</legend>${points.length?leads.filter(item=>item.tier==="S").map(choice).join("")+(leads.some(item=>item.tier!=="S")?`<details class="direction-non-s" ${chosen&&chosen.tier!=="S"?"open":""}><summary>用 A / B 级卖点主打（需写理由）</summary>${leads.filter(item=>item.tier!=="S").map(choice).join("")}</details>`:""):'<p class="context-note">这个产品还没有卖点，请先到卖点分级页新增。</p>'}</fieldset>`;
      dialog.innerHTML=`<form><div class="dialog-heading"><div><h2 id="direction-create-title">${existing?"新增已测角度":"新建方向"}</h2><p>${esc(product)} · 方向编号 <strong id="direction-generated-id">${esc(id)}</strong></p></div><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div><div class="direction-wizard-body">${options.source?`<p class="direction-source-note">${esc(options.source)}</p>`:""}
        ${chosen&&preselected===chosen.id?`<details class="direction-lead-fold"><summary>主打 <strong>${esc(chosen.code)} · ${esc(chosen.title)}</strong> · 换一个</summary>${picker}</details>`:picker}
        ${chosen&&chosen.tier!=="S"&&!existing?`<label class="field"><span>为什么用 ${esc(chosen.code)} 主打</span><textarea name="lead_reason" rows="2" maxlength="2000" placeholder="分级表要求每条内容由一个 S 主导；写明这次为什么例外">${esc(draft.lead_reason)}</textarea></label>`:""}
        ${existing?`<label class="field"><span>和已有角度比，这次改了什么</span><textarea name="execution_change" rows="2" maxlength="4000" placeholder="例如：先给一排刀切纸的结果，再倒回讲以前用几样东西">${esc(draft.execution_change)}</textarea></label>`:""}
        <label class="field"><span>观众顾虑与回应顺序</span><textarea name="concern" rows="3" required maxlength="4000" data-chain-target placeholder="写观众买前会问的，不写操作步骤。例如：会不会磨坏好刀 → B1 上色看得见接触 → 值不值">${esc(draft.concern)}</textarea></label>
        ${reviewConcerns.length?`<div class="direction-suggestions"><span>已关联评论顾虑 · 评论说法待核实</span>${reviewConcerns.map((text,index)=>`<button type="button" class="secondary" data-review-concern="${index}">${esc(text)}</button>`).join("")}</div>`:""}${suggestions("点选已有顾虑链",concerns,"data-suggestion")}
        <label class="field"><span>最后怎样收口？</span><textarea name="closing" rows="2" required maxlength="4000" data-chain-target placeholder="例如：B3 3 年有限质保，或无购买收口">${esc(draft.closing)}</textarea></label>${suggestions("点选已有收口",closings,"data-closing-suggestion")}
        <details class="direction-suggestions"><summary>点选卖点，追加到正在填的那一栏</summary>${chainPoints.map((item,index)=>`<button type="button" class="secondary" data-chain-point="${index}">${esc(item.code)} · ${esc(item.title)}</button>`).join("")}</details>
        <div class="direction-progression"><label class="field"><span>推进结构（可选）</span><input name="progression" list="dir-create-progressions" maxlength="60" value="${esc(draft.progression)}" placeholder="例如：问题-解决、开箱逐件讲"><datalist id="dir-create-progressions">${progressions.map(item=>`<option value="${esc(item)}"></option>`).join("")}</datalist></label><label class="field"><span>目标人群（可选）</span><input name="audience" maxlength="2000" value="${esc(draft.audience)}" placeholder="例如：从 Lansky 换来的人；刀座一排钝刀的家庭厨师"></label></div>
        <div class="direction-chain-preview"><strong>完整方向链</strong><p>${esc([main,draft.concern,draft.closing].filter(Boolean).join(" → "))}</p></div>
        ${existing?'<p class="context-note">这个主打卖点已有方向。本次顾虑与收口会作为一条已测角度保留。</p>':""}<p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span>独立保存方向，后续再关联案例与视频。</span><div><button type="button" class="secondary" data-back>取消</button><button type="submit" class="primary" ${!points.length?"disabled":""}>${existing?"保存角度":"保存方向"}</button></div></div></form>`;
      dialog.querySelector("[data-close]").onclick=()=>{if(!pending)requestClose(dialog);};
      dialog.querySelector("[data-back]").onclick=()=>{if(!pending)requestClose(dialog);};
      dialog.querySelectorAll('[name="lead_point"]').forEach(input=>input.onchange=()=>{
        collect();draft.point_id=input.value;preselected="";render();
        dialog.querySelector('[name="lead_point"]:checked')?.focus({preventScroll:true});
      });
      const field=name=>dialog.querySelector(`textarea[name="${name}"]`);
      const preview=()=>{dialog.querySelector(".direction-chain-preview p").textContent=[main,field("concern").value.trim(),field("closing").value.trim()].filter(Boolean).join(" → ");};
      const put=(name,value)=>{const textarea=field(name);textarea.value=value;textarea.dispatchEvent(new Event("input",{bubbles:true}));preview();};
      dialog.querySelectorAll("[data-chain-target]").forEach(textarea=>{textarea.addEventListener("focus",()=>{target=textarea.name;});textarea.addEventListener("input",preview);});
      dialog.querySelectorAll("[data-review-concern]").forEach(button=>button.onclick=()=>put("concern",[field("concern").value.trim(),reviewConcerns[Number(button.dataset.reviewConcern)]].filter(Boolean).join(" → ")));
      dialog.querySelectorAll("[data-suggestion]").forEach(button=>button.onclick=()=>put("concern",concerns[Number(button.dataset.suggestion)]));
      dialog.querySelectorAll("[data-closing-suggestion]").forEach(button=>button.onclick=()=>put("closing",closings[Number(button.dataset.closingSuggestion)]));
      dialog.querySelectorAll("[data-chain-point]").forEach(button=>button.onclick=()=>{const item=chainPoints[Number(button.dataset.chainPoint)];put(target,[field(target).value.trim(),item.code+" "+item.title].filter(Boolean).join(" → "));});
      dialog.querySelector("form").onsubmit=async event=>{
        event.preventDefault();if(pending)return;collect();
        const alert=dialog.querySelector("[role=alert]");
        if(!point()){alert.textContent="请选择一个主打卖点。";return;}
        const isExisting=directions.some(item=>item.direction_id===`${product}-${point().code}`);
        if(point().tier!=="S"&&!isExisting&&!draft.lead_reason){alert.textContent=`${point().code} 不是 S 级卖点，请写明用它主打的理由。`;return;}
        if(isExisting&&!draft.execution_change){alert.textContent="请写明这次和已有角度比改了什么。";return;}
        const issues=[...chainIssues(points,draft.concern).map(issue=>"顾虑链："+issue),...chainIssues(points,draft.closing).map(issue=>"收口："+issue)];
        if(issues.length){alert.textContent="需要修改："+issues.join("；");return;}
        pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
        try {
          const result=await api("/api/direction-catalog",{method:"POST",body:{revision:catalog.revision,request_id:requestId,operation:"add_variant",direction:{product,selling_point_id:point().id,main_difference:main,concern:draft.concern,closing:draft.closing,status:"testing",...(draft.progression?{progression:draft.progression}:{}),...(isExisting&&draft.execution_change?{execution_change:draft.execution_change}:{}),...(draft.lead_reason?{lead_reason:draft.lead_reason}:{}),...(draft.audience?{audience:draft.audience}:{})}}});
          dialog.close();toast(result.existing?"已测角度已保存":"方向已保存");if(options.onSaved)await options.onSaved(result);
          listStale=true;const next=directionURL(product,result.direction.direction_id);if(location.hash===next)void route(true);else location.hash=next;
        }catch(error){alert.textContent=error.message;}
        finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
      };
    }
    render();
  }
  function openDirectionEditor(data) {
    const direction=data.direction,requestId=crypto.randomUUID();
    let dialog=$("#direction-edit-dialog");if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-edit-dialog";document.body.append(dialog);}
    dialog.innerHTML=`<form><div class="dialog-heading"><div><h2>编辑方向</h2><p>${esc(direction.direction_id)} · ${esc(direction.main_difference)}</p></div><button type="button" class="icon-button" data-close aria-label="关闭">${icon("close")}</button></div><div class="direction-wizard-body"><label class="field"><span>默认顾虑链</span><textarea name="concern" rows="3" required maxlength="4000">${esc(direction.concern || "")}</textarea></label><label class="field"><span>默认收口</span><textarea name="closing" rows="3" required maxlength="4000">${esc(direction.closing || "")}</textarea></label><label class="field"><span>人工状态</span><select name="status">${Object.entries(statusNames).map(([value,label])=>`<option value="${value}" ${direction.status===value?"selected":""}>${label}</option>`).join("")}</select></label><label class="field"><span>目标人群</span><textarea name="audience" rows="2" maxlength="2000" placeholder="这条方向讲给谁听；卖点理由里不再写人群">${esc(direction.audience || "")}</textarea></label><label class="field"><span>方向备注</span><textarea name="notes" rows="3" maxlength="4000">${esc(direction.notes || "")}</textarea></label><p class="context-note">直接改方向的默认定义。旧定义进入“旧定义”记录，不会变成执行变体；案例当时的执行记录不变。</p><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存定义</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));
    let pending=false;
    dialog.querySelector("form").onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      const values=Object.fromEntries(new FormData(event.currentTarget));for(const key of Object.keys(values))values[key]=String(values[key]).trim();
      pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
      try {await api("/api/direction-catalog",{method:"POST",body:{revision:data.revision,request_id:requestId,operation:"update",direction:{...direction,...values}}});dialog.close();toast("方向定义已更新，旧定义已留档");listStale=true;void route(true);}
      catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
      finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
    };
    dialog.showModal();trackDraft(dialog);
  }
  // A selling point is edited where it stands: the form opens under its row, a new one opens above the table.
  async function openPointEditor(id = "", adoptGrading = false) {
    const product = window.WorkbenchShell?.product() || routeValues().product;
    if (!product) { toast("请先选择一个产品，再维护卖点。", true); return; }
    if (!$("#dir-points")) {
      // Asked from the directions tab or another page: open the table first, then the form.
      pendingPoint = {id, adoptGrading};
      const target = pageURL("#directions?tab=points");
      if (location.hash === target) void route(true); else location.hash = target;
      return;
    }
    const existing = $("#content .px-edit");
    if (existing) {
      const same = !!id && existing.dataset.pointId === id;
      if (activeDraft?.dirty) { confirmDiscard(() => { existing.close(); if (!same) void openPointEditor(id, adoptGrading); }); return; }
      existing.close();
      if (same) return;  // clicking the open row again closes it
    }
    const openingHash = location.hash;
    let catalog; try { catalog = await api(catalogURL(product)); } catch (error) { toast(error.message, true); return; }
    if (location.hash !== openingHash || !$("#dir-points") || $("#content .px-edit")) return;
    const found = (catalog.selling_points || []).find(item => item.id === id) || {}, requestId = crypto.randomUUID();
    const row = id ? $(`#dir-points [data-point-id="${CSS.escape(id)}"]`) : null;
    if (id && (!found.id || !row)) { toast("这个卖点已不在列表里，已重新读取。", true); listStale = true; void route(true); return; }
    const grading = found.grading_mismatch && found.grading_row ? found.grading_row : null;
    const prior = {...found}; delete prior.grading_row; delete prior.grading_mismatch; delete prior.performance;
    const shown = adoptGrading && grading ? {...prior, title: grading.title, rationale: grading.rationale, evidence: grading.evidence} : prior;
    const others = sortPoints((catalog.selling_points || []).filter(item => item.id !== found.id));
    const mergeOptions = others.filter(item => item.status !== "merged").map(item => `<option value="${esc(item.code)}" ${prior.merged_into === item.code ? "selected" : ""}>${esc(`${item.code} ${String(item.title || "").slice(0, 22)}`)}</option>`).join("");
    const former = (prior.former_codes || []).length ? ` · 曾用编号 ${esc(prior.former_codes.join("、"))}，不再复用` : "";
    const direction = (catalog.directions || []).find(item => item.product === found.product && item.selling_point_id === found.id);
    const form = document.createElement("form"); form.className = "px-edit"; form.dataset.pointId = id;
    form.innerHTML = `${grading && !adoptGrading ? `<div class="px-diff"><p>${esc(gradingDiff(found))}</p><button type="button" class="secondary compact" data-adopt-grading>采用分级表写法</button></div>` : ""}
      <div class="px-edit-grid"><label class="field"><span>卖点编号</span><input name="code" pattern="[SABsab][1-9][0-9]*" placeholder="例如 S1、A2、B3" required value="${esc(shown.code || "")}"><small>首字母就是 S / A / B 分级${id ? "；改编号或升降级时，其他卖点和方向定义里的引用会跟着改，方向编号不变" : ""}${former}</small></label>
        <label class="field"><span>状态</span><select name="status" required><option value="">请选择</option>${Object.entries({testing: "在测 / 已核", candidate: "待核候选", merged: "已并入"}).map(([value, label]) => `<option value="${value}" ${shown.status === value ? "selected" : ""}>${label}</option>`).join("")}</select><small>有销量由销售数据自动显示，不用选。没核实、没证明素材的选待核候选。</small></label>
        <label class="field" data-merge-field ${shown.status === "merged" ? "" : "hidden"}><span>并入哪个卖点</span><select name="merged_into"><option value="">请选择</option>${mergeOptions}</select></label>
        <label class="field px-wide"><span>卖点标题</span><input name="title" required maxlength="200" value="${esc(shown.title || "")}"></label>
        <label class="field"><span>分级理由</span><textarea name="rationale" rows="3" maxlength="4000">${esc(shown.rationale || "")}</textarea></label>
        <label class="field"><span>证据与边界</span><textarea name="evidence" rows="3" maxlength="4000">${esc(shown.evidence || "")}</textarea></label></div>
      <p class="direction-form-error" role="alert"></p>
      <div class="px-edit-actions">${id ? '<button type="button" class="danger-outline" data-delete-point>删除卖点</button>' : ""}${id && !direction && found.tier !== "S" && found.status !== "merged" ? '<button type="button" class="subtle" data-lead-point>用它主打建方向（需写理由）</button>' : ""}<span></span><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存卖点</button></div>`;
    const slot = document.createElement(row ? "li" : "div"); slot.className = "px-edit-slot"; slot.append(form);
    if (row) row.after(slot); else $("#dir-point-new").append(slot);
    const opener = row?.querySelector(".px-open"); opener?.setAttribute("aria-expanded", "true");
    // The draft guard treats the form like a dialog: it is open while it is on the page.
    Object.defineProperty(form, "open", {get: () => form.isConnected});
    form.close = () => { slot.remove(); opener?.setAttribute("aria-expanded", "false"); form.onclose?.(); };
    trackDraft(form);
    if (adoptGrading && grading && activeDraft) activeDraft.dirty = true;
    const alert = form.querySelector("[role=alert]"), mergeField = form.querySelector("[data-merge-field]");
    form.querySelector("[data-close]").onclick = () => requestClose(form);
    form.addEventListener("keydown", event => { if (event.key === "Escape") { event.preventDefault(); requestClose(form); } });
    form.elements.status.onchange = () => { mergeField.hidden = form.elements.status.value !== "merged"; if (mergeField.hidden) form.elements.merged_into.value = ""; };
    form.querySelector("[data-adopt-grading]")?.addEventListener("click", event => {
      for (const key of Object.keys(gradingFields)) if (grading[key] != null) form.elements[key].value = grading[key];
      if (activeDraft?.dialog === form) activeDraft.dirty = true;
      event.currentTarget.closest(".px-diff").remove();
    });
    form.querySelector("[data-lead-point]")?.addEventListener("click", () => confirmDiscard(() => { form.close(); void openCreate(found.id); }));
    let pending = false;
    const busy = value => { pending = value; if (activeDraft) activeDraft.pending = value; form.querySelectorAll("button").forEach(button => button.disabled = value); };
    const remove = form.querySelector("[data-delete-point]");
    if (remove) remove.onclick = async () => {
      if (pending) return;
      // First click asks in place, so no dialog is needed.
      if (!remove.dataset.confirm) { remove.dataset.confirm = "1"; remove.className = "danger"; remove.textContent = `确认删除 ${found.code}`; alert.textContent = "删除后不能恢复；分级表文件不会改动，也不会再把它导回来。"; return; }
      busy(true);
      try { await api("/api/selling-points", {method: "POST", body: {revision: catalog.revision, request_id: crypto.randomUUID(), operation: "delete", point: {id: found.id, product}}}); form.close(); toast(`已删除 ${found.code}`); listStale = true; void route(true); }
      catch (error) { alert.textContent = error.message; delete remove.dataset.confirm; remove.className = "danger-outline"; remove.textContent = "删除卖点"; }
      finally { busy(false); }
    };
    form.onsubmit = async event => {
      event.preventDefault(); if (pending) return;
      const values = Object.fromEntries(new FormData(form)); for (const key of Object.keys(values)) values[key] = String(values[key]).trim();
      values.code = values.code.toUpperCase();
      busy(true);
      try { await api("/api/selling-points", {method: "POST", body: {revision: catalog.revision, request_id: requestId, point: {...prior, ...values, product, tier: values.code[0]}}}); form.close(); toast(found.code && found.code !== values.code ? `已改为 ${values.code}，引用已同步` : "卖点已保存"); listStale = true; void route(true); }
      catch (error) { alert.textContent = error.message; }
      finally { busy(false); }
    };
    slot.scrollIntoView({block: "nearest"});
    form.elements[id ? "title" : "code"].focus({preventScroll: true});
  }
  let listStale = false;
  async function route(force = false) {
    cancelFilterTimer(); composing = false;
    const values = routeValues();
    // An older link that narrowed the list to one direction opens that direction.
    const only = !values.id && values.params.get("direction");
    if (only) { location.replace(directionURL(values.product, only)); return; }
    $("#breadcrumb").innerHTML = values.id ? `卖点与方向 <span>/</span> ${esc(values.id)}` : "卖点与方向";
    // Only the data behind the page now on screen is reused. Opening the page again, changing product or scope, a save
    // or a refresh still reads fresh data.
    const mounted = !!listView && listView.root.isConnected && listView.form === $("#direction-filters")
      && listView.product === values.product && listView.archive === values.archive;
    if (!force && !listStale && mounted && listView.signature === state.signature && listView.digest === state.digest) { applyView(values); return; }
    listStale = false;
    const sequence = ++requestSequence;
    if (values.id && !wide.matches) {
      // A narrow window shows one thing at a time: the direction alone, without reading the list behind it.
      mountPage(null, null, values); paneState = null; paneSequence += 1;
      await loadPane(values, values.id, true);
      return;
    }
    // While re-reading, what is on screen stays; the open direction is carried into the fresh page.
    const keptPane = mounted && values.tab === "directions" ? $("#direction-pane") : null;
    if (!mounted) { listView = null; paneState = null; paneSequence += 1; $("#content").innerHTML = '<p role="status">正在读取卖点与方向…</p>'; }
    try {
      // The direction named by the address is read alongside the list, not after it.
      const early = values.id ? api(catalogURL(values.product, values.id)).then(value => ({value}), error => ({error})) : null;
      const query = new URLSearchParams({archive: values.archive}); if (values.product) query.set("product", values.product);
      const [catalog, summary] = await Promise.all([api(catalogURL(values.product)), api("/api/directions?" + query)]);
      const now = routeValues();
      if (sequence !== requestSequence || !onDirections() || now.product !== values.product || now.archive !== values.archive) return;
      window.WorkbenchShell?.registerProducts([...(catalog.selling_points || []), ...(catalog.directions || []), ...(summary.products || [])]);
      mountPage(catalog, summary, now, keptPane);
      applyView(now, true, now.id === values.id ? early : null);
    } catch (error) { if (sequence === requestSequence && pageHash() === values.hash) showError(error.message); }
  }
  window.addEventListener("hashchange", cancelFilterTimer);
  window.addEventListener("workbench:directionchange", () => { listStale = true; if (state.routeKind === "directions" && !modalOpen()) void route(true); });
  // Crossing the width where the two sides fit: open a direction beside the list, or fold back to one column.
  wide.addEventListener("change", () => { if (state.routeKind === "directions" && onDirections() && !modalOpen()) { if (listView?.root.isConnected) applyView(routeValues()); else void route(true); } });
  return {route, openCreate, openPointEditor, modalOpen, beforeNavigate};
})();
