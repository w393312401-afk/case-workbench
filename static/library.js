"use strict";

let searchTimer = null;
let cardThumbnailObserver = null;

function activateCardThumbnails() {
  cardThumbnailObserver?.disconnect();
  const images = $$("[data-card-thumbnail]", $("#case-results"));
  const load = (image) => {
    if (!image.dataset.src) return;
    image.addEventListener("error", () => {
      image.hidden = true;
      image.previousElementSibling?.querySelector("span")?.replaceChildren("视频封面暂不可用");
    }, { once: true });
    image.src = image.dataset.src;
    delete image.dataset.src;
  };
  if (!("IntersectionObserver" in window)) {
    images.forEach(load);
    return;
  }
  cardThumbnailObserver = new IntersectionObserver(
    (entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          load(entry.target);
          cardThumbnailObserver.unobserve(entry.target);
        }
      });
    },
    { rootMargin: "240px" },
  );
  images.forEach((image) => cardThumbnailObserver.observe(image));
}
function moveControl(item) {
  const choices = navigationMoveChoices().filter(
    (choice) => !item.archived || choice.content !== "archived",
  );
  return `<select class="move-case-select" data-move-case="${esc(item.case_id)}" aria-label="移动案例：${esc(shortTitle(item))}"><option value="">移动到…</option>${choices.map(({ id, label, content }) => `<option value="${esc(id)}">${esc(label)}</option>`).join("")}</select>`;
}
function bindMoveControls() {
  $$("[data-move-case]", $("#case-results")).forEach((control) =>
    control.addEventListener("change", async () => {
      const target = control.value;
      if (!target) return;
      control.disabled = true;
      try {
        await moveCaseToView(control.dataset.moveCase, target);
      } finally {
        if (control.isConnected) {
          control.value = "";
          control.disabled = false;
        }
      }
    }),
  );
}
// These are reading labels only. Saved classifications and conclusions never change.
function caseReadingState(item) {
  const cur = item.current || {};
  const systemPlaceholder = cur.category === "批量收录 · 待拆解" && /^从视频数据批量收录：[\s\S]*；拆解完成前不作结论。[。]?$/.test(cur.conclusion || "");
  const hasBreakdown = item.source_analysis ? item.source_analysis.exists === true : Boolean(cur.source_case?.file_path);
  return {neutral:systemPlaceholder && hasBreakdown, category:systemPlaceholder && hasBreakdown ? "已有拆解 · 待整理" : cur.category,
    conclusion:systemPlaceholder && hasBreakdown ? "已有拆解，结论尚待整理。" : cur.conclusion};
}
function caseReadingBadges(item) {
  const reading = caseReadingState(item), template = document.createElement("template");
  template.innerHTML = `${reading.neutral ? `<span class="badge pending">${esc(reading.category)}</span>` : badge(item)}${sourceBadges(item)}${item.archived ? archiveBadge(item) : ""}`;
  template.content.querySelectorAll(".source-tier").forEach(node => node.remove());
  if (item.current?.source_case?.case_type === "video_analysis") template.content.querySelectorAll(".source-type").forEach(node => node.remove());
  return template.innerHTML;
}
function caseMetricContext(item) {
  const e = salesEvidence(item), video = item.sales_data?.video || {};
  return `${video.views_source === "public" ? "播放：公开页累计；CTOR/售出：销售表" : "销售数据表"} · ${e.window || "窗口未提供"}${e.as_of ? ` · 销售截至 ${e.as_of}` : ""}`;
}
const librarySalesStatusNames = {all:"全部近期状态",selling:"还在卖",stopped:"已停",new:"刚起量",unknown:"数据不足"};
function librarySample(item, key) {
  if (!["ctor", "sold_per_k"].includes(key) || librarySortValue(item, key) === undefined) return null;
  const video = item.sales_data?.video || {}, coverage = video.metric_coverage || {}, m = salesMetrics(item);
  const fields = key === "ctor" ? ["orders", "clicks"] : ["items", "views"];
  const complete = fields.every(field => !coverage[field] || coverage[field].provided_rows === coverage[field].total_rows);
  const raw = key === "ctor" ? (video.clicks ?? m.product_clicks_display) : (video.views_source === "public" ? video.views_shop : video.views);
  const count = raw === undefined || raw === null || raw === "" ? NaN : numeric(raw);
  const minimum = key === "ctor" ? 100 : 10000, unit = key === "ctor" ? "商品点击" : "销售表播放";
  const quality = !complete || !Number.isFinite(count) ? "unknown" : count < minimum ? "small" : "enough";
  return {quality, title:`${key === "ctor" ? "CTOR" : "每千次播放售出"}样本门槛：${unit}至少 ${minimum.toLocaleString("en-US")}；${Number.isFinite(count) ? `当前 ${count.toLocaleString("en-US")}` : "当前数量未知"}${complete ? "" : "；指标覆盖不完整"}。`};
}
function librarySampleBadge(item) {
  const key = state.sort === "sold_per_k" ? "sold_per_k" : "ctor", sample = librarySample(item, key);
  if (!sample || sample.quality === "enough") return "";
  return `<span class="library-sample" title="${esc(sample.title)}">${key === "ctor" ? "CTOR" : "千播售出"} ${sample.quality === "small" ? "样本小" : "样本待核对"}</span>`;
}
function libraryCurrentMarkup(item) {
  const data = item.library_performance;
  if (!data) return "";
  const recent = data.recent || {}, status = data.status || "unknown", campaign = data.campaign || {};
  const age = Number.isInteger(data.age_days) ? `发布 ${data.age_days} 天` : "发布时间未知";
  const statusTitle = status === "new" ? "发布日期在 28 天内；此标签不代表已确认增长。" : status === "selling" && !recent.orders?.complete ? "近 28 天已观察到出单，但该时段的数据未完全覆盖。" : status === "stopped" ? "发布超过 28 天，近 28 天的数据完整且出单为 0。" : "近 28 天的销售数据不足以确认是否停卖。";
  const metric = (field, label, format) => {
    const entry = recent[field] || {}, present = typeof entry.value === "number";
    const amount = present ? format(entry.value) : "—";
    const coverage = `${recent.start || ""} 至 ${recent.end || ""}（美西时间，截至昨天）；覆盖 ${entry.covered_days || 0}/${recent.required_days || 0} 天。`;
    return `<span title="${esc(coverage)}">${label} ${esc(amount)}${present && !entry.complete ? "（不完整）" : ""}</span>`;
  };
  const campaignText = typeof campaign.percent === "number" ? `已录活动期出单占 ${campaign.percent}%${campaign.dates_unverified ? "（日期待核对）" : ""}` : `活动期占比${campaign.reason === "已导入数据没有可计算的出单" ? "暂无出单" : "数据不足"}`;
  const campaignTitle = typeof campaign.percent === "number" ? `按已导入覆盖期内的订单计算；活动日历按美西自然日，未覆盖日期不补零。${campaign.dates_unverified ? "部分活动日期尚待核对。" : ""}` : (campaign.reason || "销售或活动日期数据不足。");
  return `<div class="library-current"><span>${esc(age)}</span><span class="library-status is-${esc(status)}" title="${esc(statusTitle)}">${esc(librarySalesStatusNames[status] || librarySalesStatusNames.unknown)}</span>${librarySampleBadge(item)}</div><div class="library-recent">${metric("items", "近 28 天售出", value => `${value.toLocaleString("en-US")} 件`)}${metric("gmv", "近 28 天金额", value => `$${value.toLocaleString("en-US", {maximumFractionDigits:2})}`)}</div><div class="library-campaign" title="${esc(campaignTitle)}">${esc(campaignText)}</div>`;
}
// Why a case still needs work before it can be relied on; null when it is settled.
// Only machine stages count here: an empty test direction is common and is not a backlog.
function libraryTidyReason(item) {
  const current = item.current || {}, category = String(current.category || "");
  if (!item.current) return "缺当前版本";
  if (category.includes("待听校")) return "转写待听校";
  if (category === "批量收录 · 待拆解") {
    const hasBreakdown = item.source_analysis ? item.source_analysis.exists === true : Boolean(current.source_case?.file_path);
    if (!hasBreakdown) return "待拆解";
    // A breakdown with a real conclusion is settled even though the import label was never renamed.
    return caseReadingState(item).neutral || !String(current.conclusion || "").trim() ? "结论待整理" : null;
  }
  return null;
}
// One line under the badges: sales status, age and the last 28 days. The amount, campaign share and
// data source move into the hover text, so a card reads as title → conclusion → three figures.
function libraryStatusLine(item) {
  const data = item.library_performance;
  if (!data) return "";
  const recent = data.recent || {}, status = data.status || "unknown", campaign = data.campaign || {}, sold = recent.items || {};
  const parts = [];
  if (Number.isInteger(data.age_days)) parts.push(`发布 ${data.age_days} 天`);
  if (typeof sold.value === "number" && sold.value > 0) parts.push(`近 28 天 ${sold.value.toLocaleString("en-US")} 件${sold.complete ? "" : "（部分）"}`);
  const gmv = recent.gmv || {};
  const hover = [
    recent.start ? `近 28 天：${recent.start} 至 ${recent.end}（美西时间，截至昨天），覆盖 ${sold.covered_days || 0}/${recent.required_days || 0} 天` : "",
    typeof gmv.value === "number" ? `近 28 天金额 $${gmv.value.toLocaleString("en-US", {maximumFractionDigits:2})}${gmv.complete ? "" : "（部分）"}` : "",
    typeof campaign.percent === "number" ? `已录活动期出单占 ${campaign.percent}%${campaign.dates_unverified ? "（日期待核对）" : ""}` : "",
    caseMetricContext(item),
  ].filter(Boolean).join("\n");
  const pill = status === "unknown" ? "" : `<span class="library-status is-${esc(status)}">${esc(librarySalesStatusNames[status])}</span>`;
  return `<div class="card-status" title="${esc(hover)}">${pill}${parts.length ? `<span>${esc(parts.join(" · "))}</span>` : ""}${librarySampleBadge(item)}</div>`;
}
function libraryTerms() {
  return String(state.query || "").trim().toLowerCase().split(/\s+/u).filter(Boolean);
}
function librarySnippet(item) {
  const terms = libraryTerms();
  if (!terms.length) return "";
  const cur = item.current || {}, shown = `${shortTitle(item)} ${caseReadingState(item).conclusion || ""}`.toLowerCase();
  if (terms.every(term => shown.includes(term))) return "";
  const texts = [];
  const walk = value => {
    if (typeof value === "string") { if (value.trim()) texts.push(value); }
    else if (Array.isArray(value)) value.forEach(walk);
    else if (value && typeof value === "object") Object.entries(value).forEach(([key, inner]) => { if (!["assets", "history_note", "source_event_ids"].includes(key)) walk(inner); });
  };
  walk(cur);
  // Transcript timestamps such as [0.22s - 3.78s] are noise in a one-line hit.
  for (let index = 0; index < texts.length; index += 1) texts[index] = texts[index].replace(/\[\s*\d+(?:\.\d+)?s\s*-\s*\d+(?:\.\d+)?s\s*\]\s*/g, "");
  const term = terms.find(word => !shown.includes(word)) || terms[0];
  const text = texts.find(value => value.toLowerCase().includes(term));
  if (!text) return "";
  const at = text.toLowerCase().indexOf(term), start = Math.max(0, at - 40), end = Math.min(text.length, at + term.length + 60);
  const piece = `${start ? "…" : ""}${text.slice(start, end).replace(/\s+/g, " ")}${end < text.length ? "…" : ""}`;
  const pattern = new RegExp(`(${terms.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return `<p class="card-hit" title="搜索命中的原文">${esc(piece).replace(pattern, "<mark>$1</mark>")}</p>`;
}
// Settled cases do not repeat their breakdown stage on every card; judgements, backlog and archive state stay.
function cardBadges(item) {
  const reason = libraryTidyReason(item);
  if (reason) return `<span class="badge pending">${esc(reason)}</span>${item.archived ? archiveBadge(item) : ""}`;
  if (item.archived || libraryPerformance(item) !== "pending") return caseReadingBadges(item);
  return "";
}
function card(item) {
  const cur = item.current || {},
    m = { ...caseIdentity(item), ...salesMetrics(item) },
    validVideo = item.assets?.video?.exists,
    extra = librarySortExtras[state.sort],
    hasMetrics =[m.views_display, m.ctor_display, m.items_sold].some((value) => value !== undefined && value !== null && value !== "");
  const media = validVideo
    ? `<div class="card-media"><div class="missing-media thumbnail-fallback">${icon("video")}<span>正在读取视频封面…</span></div><img data-card-thumbnail data-src="${mediaURL(item.case_id, "thumbnail", item.current_event_id)}" loading="lazy" decoding="async" alt=""><span class="play-mark">${icon("play")}</span></div>`
    : `<div class="card-media is-missing">${icon("video")}<span>${item.assets?.video ? "视频文件暂不可用" : "尚未关联视频"}</span></div>`;
  const metric = (label, value, suffix = "", accent = false) => `<div><span class="metric-label">${label}</span><span class="metric-value${accent ? " accent" : ""}${value === undefined ? " missing-value" : ""}">${displayValue(value)}${value !== undefined && suffix ? `<small>${suffix}</small>` : ""}</span></div>`;
  const badges = cardBadges(item), status = libraryStatusLine(item);
  const direction = cur.test_direction ? directionLabel(item) : "";
  const foot = [item.product, m.creator_handle, direction].filter(Boolean).join(" · ");
  return `<a draggable="false" class="case-card" href="#case/${item.case_id}" aria-label="查看案例：${esc(shortTitle(item))}">${media}
    <div class="card-body">${badges || status ? `<div class="card-topline">${badges ? `<span class="case-badges">${badges}</span>` : ""}${status}</div>` : ""}<h2>${esc(shortTitle(item))}</h2><p class="card-description">${esc(caseReadingState(item).conclusion || "结论待整理")}</p>${librarySnippet(item)}
    ${hasMetrics ? `<div class="card-metrics${extra ? " has-sort-metric" : ""}" title="${esc(caseMetricContext(item))}">${metric("播放", m.views_display)}${metric("CTOR", m.ctor_display, "", true)}${metric("售出", m.items_sold, "件")}${extra ? metric(extra, librarySortDisplay(item, state.sort)) : ""}</div>` : item.sales_data?.status === "unavailable" ? "" : `<div class="card-metrics is-empty"><span>${esc(salesStatusLabel(item))}</span></div>`}
    <div class="card-foot"><span class="card-direction-name" title="${esc(cur.test_direction?.main_difference || foot)}">${esc(foot)}</span><span>${esc(String(readableDate(item.updated_at)).split(" ")[0])}</span></div></div></a>`;
}
function libraryPerformance(item) {
  const current=item.current;
  if(!current)return "pending";
  if(current.category === "转化正向参考")return "positive";
  if(String(current.category || "").includes("高播放低转化"))return "mixed";
  if(["待验证与整理","待验证"].includes(current.category))return "pending";
  return (current.assessments || []).some(value=>value.dimension === "conversion" && value.polarity === "positive") ? "positive" : "pending";
}
function librarySelectedStatuses() {
  return libraryStatusList(state.salesStatuses?.length ? state.salesStatuses : state.salesStatus);
}
function librarySetStatuses(values) {
  state.salesStatuses = libraryStatusList(values);
  state.salesStatus = state.salesStatuses.length === 1 ? state.salesStatuses[0] : "all";
}
function libraryCreator(item) {
  return String(caseIdentity(item).creator_handle || "").trim().replace(/^@+/, "").toLocaleLowerCase();
}
function libraryQuickMatches(item, key) {
  if (key === "mixed") return libraryPerformance(item) === "mixed";
  if (key === "sold100" || key === "sold10") return (librarySortValue(item, "sold") ?? -1) >= (key === "sold100" ? 100 : 10);
  if (key === "views100k") return (librarySortValue(item, "views") ?? -1) >= 100000;
  if (key === "tidy") return !!libraryTidyReason(item);
  if (key === "ready") {
    const current = item.current || {}, conclusion = String(current.conclusion || "").trim();
    // A conclusion drawn from an unchecked transcript or unverified data is not ready to reuse yet.
    if (libraryTidyReason(item) || /待听校|待核实|待补|数据不全|待验证/u.test(String(current.category || ""))) return false;
    const hasBreakdown = current.category !== "批量收录 · 待拆解" ||
      (item.source_analysis ? item.source_analysis.exists === true : Boolean(current.source_case?.file_path));
    return hasBreakdown && !!conclusion &&
      !/拆解完成前不作结论|^(?:结论待整理|暂无结论|待整理|已有拆解[，,]结论尚待整理)[。.!！]?$/u.test(conclusion);
  }
  return false;
}
function libraryStatusOptions() {
  const statuses = librarySelectedStatuses();
  const current = statuses.length > 1 ? "__multiple__" : statuses[0] || "all";
  return `${statuses.length > 1 ? `<option value="__multiple__" selected disabled>已选 ${statuses.length} 项</option>` : ""}${Object.entries(librarySalesStatusNames).map(([value, text]) => `<option value="${esc(value)}" ${value === current ? "selected" : ""}>${esc(text)}</option>`).join("")}`;
}
function libraryQuickMarkup() {
  const statuses = librarySelectedStatuses(), active = state.quickFilters || [];
  const statusScope = libraryMatchingCases({ignoreStatuses:true});
  const hints = {
    sold100:"销售表已导入的累计售出不少于 100 件。", sold10:"销售表已导入的累计售出不少于 10 件。",
    views100k:"案例当前显示的累计播放不少于 10 万次，可能来自公开页或销售表。",
    mixed:"使用案例已有的高播放低转化分类。", ready:"已有拆解和结论；转写待听校、数据待核实或待验证的不算。",
  };
  // A button that would only lead to an empty list is left out, unless it is on (so it can be turned off).
  const shown = (on, count) => on || count > 0;
  const statusButtons = ["selling", "new", "stopped"].map(key => {
    const count = statusScope.filter(item => (item.library_performance?.status || "unknown") === key).length;
    return shown(statuses.includes(key), count) ? `<button type="button" data-quick-status="${key}" aria-pressed="${statuses.includes(key)}">${librarySalesStatusNames[key]} <small>${count}</small></button>` : "";
  }).join("");
  const performanceButtons = ["sold100", "sold10", "views100k", "mixed", "ready"].map(key => {
    const on = key === "mixed" ? state.archiveCategory === "mixed" : active.includes(key);
    const candidates = libraryMatchingCases(key === "mixed" ? {ignoreCategory:true} : {ignoreQuick:key});
    const count = candidates.filter(item => libraryQuickMatches(item, key)).length;
    const label = key === "mixed" ? "高播放低转化" : libraryQuickFilterNames[key];
    return shown(on, count) ? `<button type="button" data-quick-filter="${key}" aria-pressed="${on}" title="${esc(hints[key])}">${label} <small>${count}</small></button>` : "";
  }).join("");
  const creators = new Map(scopedCases().map(libraryCreator).filter(Boolean).map(name => [name, 0]));
  const creatorScope = libraryMatchingCases({ignoreCreator:true});
  for (const item of creatorScope) { const name = libraryCreator(item); if (name) creators.set(name, (creators.get(name) || 0) + 1); }
  if (state.libraryCreator && !creators.has(state.libraryCreator)) creators.set(state.libraryCreator, 0);
  const options = [...creators].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return `${statusButtons ? `<span class="lib-quick-label">近期</span>${statusButtons}` : ""}${performanceButtons ? `<span class="lib-quick-label">表现</span>${performanceButtons}` : ""}<label class="lib-quick-creator"><span>达人账号</span><select id="library-creator-quick" aria-label="达人账号"><option value="">全部账号（${creatorScope.length}）</option>${options.map(([name, count]) => `<option value="${esc(name)}" ${name === state.libraryCreator ? "selected" : ""}>@${esc(name)}（${count}）</option>`).join("")}</select></label>`;
}
// 待整理 sits with the categories: it is a place to go and work through, not a measure of performance.
function libraryTidyMarkup() {
  const on = (state.quickFilters || []).includes("tidy");
  const count = libraryMatchingCases({ignoreQuick:"tidy"}).filter(item => libraryQuickMatches(item, "tidy")).length;
  if (!on && !count) return "";
  return `<button type="button" class="lib-tidy" data-quick-filter="tidy" aria-pressed="${on}" title="转写待听校、待拆解或结论待整理的案例；整理好后自动离开这里。">待整理<small>${count}</small></button>`;
}
const tidyHints = {
  "待拆解": "还没有拆解。可以一起排队 AI 拆解，后台一条一条跑，进度看任务面板。",
  "结论待整理": "拆解已经有了，结论还是导入时的占位。打开案例，在拆解基础上写结论。",
  "转写待听校": "口播是机器转写，没有人工听校。打开案例，对照视频改好文案，再改分类。",
  "缺当前版本": "这条案例没有当前版本，打开核对。",
};
function tidyGroups(items) {
  const order = ["待拆解", "结论待整理", "转写待听校", "缺当前版本"], groups = new Map();
  for (const item of items) { const reason = libraryTidyReason(item); if (!groups.has(reason)) groups.set(reason, []); groups.get(reason).push(item); }
  return [...groups].sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0])).map(([reason, list]) => `<section class="lib-tidy-group" data-tidy-reason="${esc(reason)}"><div class="lib-tidy-head"><h3>${esc(reason)}<small>${list.length}</small></h3><p>${esc(tidyHints[reason] || "")}</p>${reason === "待拆解" && !window.WorkbenchGuest ? `<button type="button" class="secondary compact" data-tidy-breakdown>批量排队 AI 拆解（${list.length} 条）</button>` : ""}</div><div class="case-grid">${list.map(selectableCard).join("")}</div></section>`).join("");
}
async function tidyBreakdown(button) {
  const list = filteredCases().filter(item => libraryTidyReason(item) === "待拆解");
  if (!list.length || !window.CaseAutoBreakdown) return;
  if (!window.confirm(`给 ${list.length} 条待拆解的案例排队 AI 拆解？用默认模型，后台一条一条跑，进度看任务面板。`)) return;
  button.disabled = true;
  let queued = 0, failed = 0, firstError = "";
  for (const item of list) {
    try { await window.CaseAutoBreakdown.start(item.case_id, caseIdentity(item).video_id || ""); queued += 1; }
    catch (error) { failed += 1; firstError ||= error.message; }
  }
  toast(`已排队拆解 ${queued} 条${failed ? `，${failed} 条没排上：${firstError}` : ""}。`, !!failed);
  if (button.isConnected) button.disabled = false;
}
function renderLibraryQuickFilters() {
  const bar = $("#library-quick-filters");
  if (bar) bar.innerHTML = libraryQuickMarkup();
  const tidy = $("#library-tidy");
  if (tidy) tidy.innerHTML = libraryTidyMarkup();
  const statusPicker = $("#sales-status-filter");
  if (statusPicker) {
    const options = libraryStatusOptions();
    if (statusPicker.innerHTML !== options) statusPicker.innerHTML = options;
  }
  const categoryPicker = $("#archive-category-filter");
  if (categoryPicker && categoryPicker.value !== (state.archiveCategory || "all")) categoryPicker.value = state.archiveCategory || "all";
}
function libraryArchiveStatus() {
  return state.archiveStatus || (navigationContent() === "archived" ? "archived" : "active");
}
function libraryFilterMarkup(id,label,values,current) {
  return `<label class="library-filter-field"><span>${label}</span><select id="${id}">${Object.entries(values).map(([value,text])=>`<option value="${esc(value)}" ${value===current?"selected":""}>${esc(text)}</option>`).join("")}</select></label>`;
}
function updateLibraryViewControls() {
  const picker=$("#library-saved-view");
  if(picker)picker.value=window.LibraryViews?.available().some(item=>item.id===state.view)?state.view:"";
  $$("[data-library-category]").forEach(button=>button.setAttribute("aria-pressed",String(state.view===button.dataset.libraryCategory && (state.view!=="reference-all" || !state.libraryProduct))));
}
function libraryProductOptions() {
  const shell = window.WorkbenchShell;
  const values = new Map([["", "全部产品"]]);
  // The shared catalogue also contains sales/review-only products. This filter
  // classifies cases, so keep only products represented in the library.
  const present = new Set(state.cases.map(item => shell?.productOf(item) ?? productFilterGroup(item)).filter(Boolean));
  const choices = shell?.getProductChoices?.() || [...present].map(value => ({ value, label: value }));
  for (const { value, label } of choices) if (present.has(value)) values.set(value, label);
  if (state.libraryProduct && !values.has(state.libraryProduct)) values.set(state.libraryProduct, shell?.productLabel(state.libraryProduct) || state.libraryProduct);
  return [...values].map(([value, label]) => `<option value="${esc(value)}" ${value === (state.libraryProduct || "") ? "selected" : ""}>${esc(label)}</option>`).join("");
}
function renderLibrary() {
  const content=navigationContent(),scope=scopedCases();
  const saved=window.LibraryViews?.available() || [];
  const categories = window.LibraryCategories?.available() || [];
  const editableCategories = !window.WorkbenchGuest && !window.PublicTextMode;
  const categoryCount = id => state.cases.filter(item => navigationMatches(item, id) && (id === "reference-all" || libraryProductMatches(item))).length;
  $("#breadcrumb").innerHTML="案例库";
  window.WorkbenchShell?.registerProducts(state.cases);
  const summary=content==="native"?nativeSummary(scope):"";
  const field=(id,label,values,current)=>libraryFilterMarkup(id,label,values,current);
  const drawerFields=`${field("archive-category-filter","表现分类",{all:"全部表现",positive:"转化正向参考",mixed:"高播放低转化",pending:"待验证与整理"},state.archiveCategory || "all")}<label class="library-filter-field"><span>近期状态</span><select id="sales-status-filter">${libraryStatusOptions()}</select></label>${field("content-origin-filter","内容来源",{all:"全部来源",self_produced:"自制内容",overseas_creator:"海外达人"},state.contentOrigin || "all")}${field("text-form-filter","文案形式",{all:"全部形式",screen_only:"纯钩子屏显（无口播）"},state.textForm || "all")}${field("source-type-filter","资料类型",sourceTypeNames,state.sourceType)}${field("archive-status-filter","归档状态",{active:"使用中",archived:"已归档",all:"全部状态"},libraryArchiveStatus())}<label class="library-filter-field"><span>测试方向</span><select id="direction-filter" aria-label="筛选测试方向">${directionFilterOptions(scope)}</select></label>${field("variant-filter","变体类型",{all:"全部变体",...directionVariantNames},state.variantType)}`;
  $("#content").innerHTML=`<div class="page-heading lib-heading"><div><h1>案例库</h1></div><div class="layout-switch lib-layout" role="group" aria-label="案例显示方式"><button type="button" data-layout="cards" class="${state.layout === "cards" ? "active" : ""}" aria-pressed="${state.layout === "cards"}">${icon("grid")}卡片</button><button type="button" data-layout="list" class="${state.layout === "list" ? "active" : ""}" aria-pressed="${state.layout === "list"}">${icon("file")}列表</button></div></div>
    <section class="library-filter-panel lib-toolbar" aria-label="案例库筛选"><div class="lib-search-row"><label class="search">${icon("search")}<input id="case-search" aria-label="搜索当前案例" placeholder="搜标题、结论、逐字稿…空格分开多个词" value="${esc(state.query)}"></label>
      <div class="library-presets lib-presets lib-category-tabs" role="group" aria-label="案例分类"><button type="button" data-library-category="reference-all" aria-pressed="${state.view === "reference-all" && !state.libraryProduct}">全部<small>${categoryCount("reference-all")}</small></button>${categories.map(item => `<button type="button" data-library-category="${esc(item.id)}" aria-pressed="${state.view === item.id}" title="${esc(item.description || item.label)}">${esc(item.label)}<small>${categoryCount(item.id)}</small></button>`).join("")}</div><span id="library-tidy" class="lib-tidy-slot"></span>
      <label class="lib-product-classify"><select id="library-product-filter" aria-label="案例库产品分类">${libraryProductOptions()}</select></label>
      <button type="button" class="secondary lib-filter-open" id="library-filter-open" aria-haspopup="dialog" aria-controls="library-filter-drawer"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>筛选<span class="rev-filter-count" id="library-filter-count" hidden></span></button>
      <details class="lib-views library-saved-row"><summary aria-label="视图与分类管理" title="视图与分类管理"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/></svg></summary><div><label><span>视图</span><select id="library-saved-view" aria-label="保存的视图"><option value="">当前筛选</option>${saved.map(item=>`<option value="${esc(item.id)}" ${state.view===item.id?"selected":""}>${esc(item.label)}</option>`).join("")}</select></label><button type="button" class="subtle" id="library-save-view-button">${icon("plus")}保存视图</button><button type="button" class="subtle" id="library-manage-views">管理视图</button>${editableCategories ? '<button type="button" class="subtle" id="library-category-add">新建分类</button><button type="button" class="subtle" id="library-category-manage">管理分类成员</button>' : ""}</div></details>
      <button type="button" class="subtle" id="library-select-toggle" aria-pressed="${!!state.selecting}">${state.selecting ? "完成" : "选择"}</button></div>
      <div class="lib-active-row"><div class="lib-quick-filters" id="library-quick-filters" role="group" aria-label="案例快捷筛选" title="数量随当前分类和其他筛选变化；近期状态可多选。"></div><div class="rev-active" id="library-active"></div><span id="result-count" class="result-count" aria-live="polite"></span><span class="lib-sort"><label><span>排序</span><select id="sort-filter" aria-label="案例排序">${Object.entries(librarySortNames).map(([value,text])=>`<option value="${value}" ${value===state.sort?"selected":""}>${text}</option>`).join("")}</select></label><button type="button" class="subtle" id="library-sort-dir"></button></span><button type="button" class="subtle" id="library-clear-filters">清除筛选</button></div></section>${summary}
    <div id="selection-toolbar" class="selection-toolbar"${state.selecting ? "" : " hidden"}></div><div id="bulk-last-result" class="bulk-last-result" hidden></div><div id="case-results"></div><details class="library-footnote"><summary>案例分类与数据来源说明</summary><p>表现分类保留案例判断；数字按视频 ID 读取销售数据表，鼠标停在卡片的状态行或数字上可看覆盖日期、金额和活动期占比。近 28 天按美西时间截至昨天，缺失日期不补零；未完整覆盖的数字标「部分」，并排在完整数据之后。刚起量指发布 28 天内，不代表已确认增长。CTOR 样本门槛为 100 次商品点击，每千次播放售出的门槛为 10,000 次销售表播放。活动期出单占比以已录入活动日历和已导入数据为准，跨活动边界的周表需要可核对的每日数据。待整理只收转写待听校、待拆解和结论待整理的案例。</p></details>
    <dialog id="library-filter-drawer" class="rev-drawer" aria-labelledby="library-filter-title"><div class="rev-drawer-head"><h2 id="library-filter-title">筛选与排序</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭筛选">${icon("close")}</button></div><div class="rev-drawer-body"><div class="library-filter-grid lib-drawer-grid">${drawerFields}</div></div><div class="rev-drawer-foot"><button type="button" class="subtle" id="library-drawer-clear">清除全部</button><button type="button" class="primary" data-drawer-close id="library-drawer-done">显示结果</button></div></dialog>`;
  $("#content").classList.toggle("lib-selecting",!!state.selecting);
  const drawer=$("#library-filter-drawer");
  $("#library-filter-open").onclick=()=>{state.libraryDrawer=true;drawer.showModal();};
  drawer.addEventListener("click",event=>{if(event.target===drawer||event.target.closest("[data-drawer-close]"))drawer.close();});
  drawer.addEventListener("close",()=>{state.libraryDrawer=false;});
  if(state.libraryDrawer)drawer.showModal();
  $("#library-select-toggle").onclick=()=>{state.selecting=!state.selecting;if(!state.selecting)state.selection.clear();renderLibrary();};
  $$(".lib-layout [data-layout]").forEach(button=>button.onclick=()=>{state.layout=button.dataset.layout;try{localStorage.setItem("case-library-layout",state.layout);}catch{}$$(".lib-layout [data-layout]").forEach(item=>{const on=item===button;item.classList.toggle("active",on);item.setAttribute("aria-pressed",String(on));});renderCards();});
  // The ··· menu is a <details>; close it on a click elsewhere, the way a menu behaves.
  const viewsMenu=$(".lib-views");
  const closeViewsMenu=event=>{if(!viewsMenu.isConnected){document.removeEventListener("click",closeViewsMenu);return;}if(viewsMenu.open&&!viewsMenu.contains(event.target))viewsMenu.open=false;};
  document.addEventListener("click",closeViewsMenu);
  $("#library-save-view-button").onclick=()=>window.LibraryViews?.saveCurrent();
  $("#library-manage-views").onclick=()=>window.LibraryViews?.openManager();
  $("#library-saved-view").onchange=event=>{if(event.target.value)window.LibraryViews?.open(event.target.value);};
  $("#library-category-add")?.addEventListener("click", () => window.LibraryCategories?.openEditor());
  $("#library-category-manage")?.addEventListener("click", () => window.LibraryCategories?.openManager());
  $$("[data-library-category]").forEach(button => button.onclick = () => {
    const id = button.dataset.libraryCategory, item = navigationItem(id);
    if (!item || item.deleted) return;
    const statuses = librarySelectedStatuses(), quick = [...(state.quickFilters || [])], creator = state.libraryCreator;
    const mixed = state.archiveCategory === "mixed";
    state.view = id;
    // Product, test direction and variant belong to one product; a category may span several, so
    // switching starts them over. Quick filters, statuses and the creator carry across. A saved view's
    // own product and direction are applied right after.
    Object.assign(state, {libraryProduct:"", direction:"", variantType:"all"});
    if (item.filters) {
      window.LibraryViews?.applyRouteFilters();
      if (statuses.length) librarySetStatuses(statuses);
      if (quick.length) state.quickFilters = quick;
      if (creator) state.libraryCreator = creator;
      if (mixed) state.archiveCategory = "mixed";
    }
    writeLibraryHash(); updateNav(); renderLibrary();
  });
  const productPicker = $("#library-product-filter");
  void window.WorkbenchShell?.refreshProducts?.().then(() => { if (productPicker.isConnected) { productPicker.innerHTML = libraryProductOptions(); productPicker.value = state.libraryProduct || ""; } }).catch(() => {});
  const change=(key,value,scopeChange=false)=>{
    window.LibraryViews?.detach();state[key]=value;
    if(key==="archiveStatus"&&navigationContent()==="archived")state.view="reference-all";
    writeLibraryHash();updateNav();
    if(scopeChange)renderLibrary();else{updateLibraryViewControls();renderCards();}
  };
  productPicker.onchange = event => change("libraryProduct", window.WorkbenchShell?.normalizeProduct(event.target.value) ?? event.target.value, true);
  for(const [id,key] of [["archive-category-filter","archiveCategory"],["content-origin-filter","contentOrigin"],["text-form-filter","textForm"],["source-type-filter","sourceType"],["archive-status-filter","archiveStatus"],["direction-filter","direction"],["variant-filter","variantType"]]){
    $("#"+id).onchange=event=>change(key,event.target.value,key==="archiveStatus");
  }
  $("#sales-status-filter").onchange=event=>{librarySetStatuses(event.target.value === "all" ? [] : [event.target.value]);change("salesStatus",state.salesStatus);};
  const quickBar = $("#library-quick-filters");
  $("#library-tidy").addEventListener("click", event => {
    if (!event.target.closest("[data-quick-filter=tidy]")) return;
    const selected = state.quickFilters || [];
    change("quickFilters", selected.includes("tidy") ? selected.filter(value => value !== "tidy") : [...selected, "tidy"]);
    $("#library-tidy [data-quick-filter=tidy]")?.focus({preventScroll:true});
  });
  quickBar.addEventListener("click", event => {
    const button = event.target.closest("[data-quick-status],[data-quick-filter]");
    if (!button) return;
    const status = button.dataset.quickStatus, key = button.dataset.quickFilter;
    if (status) {
      const selected = librarySelectedStatuses();
      librarySetStatuses(selected.includes(status) ? selected.filter(value => value !== status) : [...selected, status]);
      change("salesStatus", state.salesStatus);
    } else if (key === "mixed") change("archiveCategory", state.archiveCategory === "mixed" ? "all" : "mixed");
    else {
      const selected = state.quickFilters || [];
      change("quickFilters", selected.includes(key) ? selected.filter(value => value !== key) : [...selected, key]);
    }
    quickBar.querySelector(status ? `[data-quick-status="${status}"]` : `[data-quick-filter="${key}"]`)?.focus({preventScroll:true});
  });
  quickBar.addEventListener("change", event => {
    if (event.target.id === "library-creator-quick") change("libraryCreator", event.target.value);
  });
  $("#sort-filter").onchange=event=>change("sort",event.target.value);
  $("#library-sort-dir").onclick=()=>change("sortDir",state.sortDir==="desc"?"asc":"desc");
  $("#case-search").addEventListener("input",event=>{
    window.LibraryViews?.detach();state.query=event.target.value;writeLibraryHash();updateLibraryViewControls();clearTimeout(searchTimer);searchTimer=setTimeout(renderCards,150);
  });
  const reset=()=>Object.assign(state,{view:"reference-all",libraryProduct:"",query:"",direction:"",archiveCategory:"all",salesStatus:"all",salesStatuses:[],quickFilters:[],libraryCreator:"",sourceType:"all",contentOrigin:"all",textForm:"all",archiveStatus:"active",variantType:"all",sort:"updated",sortDir:"desc"});
  $("#library-clear-filters").onclick=()=>{reset();writeLibraryHash();updateNav();renderLibrary();};
  $("#library-drawer-clear").onclick=()=>{reset();writeLibraryHash();updateNav();renderLibrary();};
  $("#library-active").addEventListener("click",event=>{
    const token=event.target.closest("[data-lib-clear]");if(!token)return;
    const key=token.dataset.libClear,defaults={query:"",contentOrigin:"all",textForm:"all",sourceType:"all",archiveStatus:"active",direction:"",variantType:"all",sort:"updated",archiveCategory:"all",salesStatus:"all"};
    window.LibraryViews?.detach();state[key]=defaults[key];if(key==="salesStatus")librarySetStatuses([]);if(key==="sort")state.sortDir="desc";if(key==="archiveStatus")state.view="reference-all";
    writeLibraryHash();updateNav();renderLibrary();
  });
  bindNativeMode();renderCards();
}
// Every factual filter remains visible after the custom-category row replaces presets.
function libraryActiveTokens() {
  const tokens=[], names={self_produced:"自制内容",overseas_creator:"海外达人"};
  if ((state.archiveCategory || "all") !== "all") tokens.push(["archiveCategory", {positive:"转化正向参考",mixed:"高播放低转化",pending:"待验证与整理"}[state.archiveCategory] || state.archiveCategory]);
  const statuses = librarySelectedStatuses();
  if (statuses.length) tokens.push(["salesStatus", statuses.map(key => librarySalesStatusNames[key] || key).join("、")]);
  if(state.query.trim())tokens.push(["query",`“${state.query.trim()}”`]);
  if((state.contentOrigin||"all")!=="all")tokens.push(["contentOrigin",names[state.contentOrigin]||state.contentOrigin]);
  if((state.textForm||"all")!=="all")tokens.push(["textForm","纯钩子屏显"]);
  if(state.sourceType&&state.sourceType!=="all")tokens.push(["sourceType",sourceTypeNames[state.sourceType]||state.sourceType]);
  if(libraryArchiveStatus()!=="active")tokens.push(["archiveStatus",libraryArchiveStatus()==="all"?"含已归档":"已归档"]);
  if(state.direction)tokens.push(["direction",state.direction==="__unclassified__"?"未记录方向":state.direction]);
  if(state.variantType&&state.variantType!=="all")tokens.push(["variantType",directionVariantNames[state.variantType]||state.variantType]);
  return tokens;
}
// The sort is always on show beside the result count, so it is not repeated as a filter token.
function syncLibrarySortControls() {
  for (const [id, value] of [["sort-filter", state.sort]]) {
    const picker = $("#" + id);
    if (picker && picker.value !== value) picker.value = value;
  }
  const button = $("#library-sort-dir"), asc = state.sortDir === "asc";
  if (button) { button.textContent = asc ? "↑ 升序" : "↓ 降序"; button.setAttribute("aria-label", `当前${asc ? "升序" : "降序"}，点击改为${asc ? "降序" : "升序"}`); }
}
// What each sort key compares; undefined means "no figure", which always sorts last.
function librarySortValue(item, key) {
  if (key === "updated") return item.updated_at;
  const m = salesMetrics(item), video = (item.sales_data?.status === "matched" && item.sales_data.video) || {};
  const number = (value) => (value === undefined || value === null || value === "" ? undefined : numeric(value));
  if (key === "posted") return video.posted_on || undefined;
  if (key === "recent_sold" || key === "recent_gmv") {
    const value = item.library_performance?.recent?.[key === "recent_sold" ? "items" : "gmv"]?.value;
    return typeof value === "number" && Number.isFinite(value) ? value : undefined;
  }
  if (key === "gmv") return typeof video.gmv === "number" ? video.gmv : undefined;
  if (key === "sold_per_k") {
    // Sold items come from the sales table, so divide by the sales table's own views, not the public-page total.
    const views = video.views_source === "public" ? video.views_shop : video.views;
    return typeof views === "number" && views > 0 && m.items_sold !== undefined ? (m.items_sold * 1000) / views : undefined;
  }
  return number(m[{ views: "views_display", impressions: "product_impressions_display", clicks: "product_clicks_display", ctor: "ctor_display", sold: "items_sold" }[key]]);
}
// Sort keys that cards and the table do not already show; the figure appears only while sorting by it.
const librarySortExtras = { posted: "发布", impressions: "曝光", clicks: "点击", sold_per_k: "千播售出", gmv: "金额", recent_sold: "近 28 天售出", recent_gmv: "近 28 天金额" };
function librarySortDisplay(item, key) {
  const value = librarySortValue(item, key);
  if (value === undefined) return undefined;
  if (key === "recent_sold" || key === "recent_gmv") {
    const entry = item.library_performance.recent[key === "recent_sold" ? "items" : "gmv"];
    return `${key === "recent_gmv" ? "$" : ""}${value.toLocaleString("en-US", {maximumFractionDigits:2})}${entry.complete ? "" : "（不完整）"}`;
  }
  if (key === "gmv") return "$" + Math.round(value).toLocaleString("en-US");
  if (key === "sold_per_k") return String(+value.toFixed(2));
  if (key === "posted") return value;
  return salesMetrics(item)[{ impressions: "product_impressions_display", clicks: "product_clicks_display" }[key]];
}
function librarySortQuality(item, key) {
  if (key === "recent_sold" || key === "recent_gmv") return item.library_performance?.recent?.[key === "recent_sold" ? "items" : "gmv"]?.complete ? 0 : 1;
  const sample = librarySample(item, key);
  return sample?.quality === "small" ? 1 : sample?.quality === "unknown" ? 2 : 0;
}
function renderCards() {
  if (!$("#case-results")) return;
  syncLibrarySortControls();
  renderLibraryQuickFilters();
  const tokens = libraryActiveTokens(), active = $("#library-active");
  if (active) active.innerHTML = tokens.map(([key,label]) => `<button type="button" class="rev-token" data-lib-clear="${key}" aria-label="去掉筛选：${esc(label)}">${esc(label)}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M7 17 17 7"/></svg></button>`).join("");
  const count = $("#library-filter-count"), drawerCount = tokens.filter(([key]) => key !== "query").length + (state.quickFilters || []).length + (state.libraryCreator ? 1 : 0);
  if (count) { count.hidden = !drawerCount; count.textContent = drawerCount; }
  const content = navigationContent(),
    archived = libraryArchiveStatus() === "archived",
    scope = scopedCases(),
    category = state.archiveCategory || "all";
  const categoryItems = scope.filter(
    (item) => category === "all" || libraryPerformance(item) === category,
  );
  const items = filteredCases();
  const visibleIds = new Set(items.map((item) => item.case_id));
  updateLibraryViewControls();
  state.selection = new Set(
    [...state.selection].filter((id) => visibleIds.has(id)),
  );
  renderSelectionToolbar(items);
  const nativeRows =
    content === "native" && state.nativeMode === "entries"
      ? nativeEntryRows(items, state.query)
      : null;
  // An unreadable sales table affects every card alike, so it is said once here instead of on each card.
  const salesDown = items.some((item) => item.sales_data?.status === "unavailable");
  $("#result-count").textContent = (nativeRows
    ? `${nativeRows.length} 条表达 · ${items.length} 条视频`
    : `${items.length} 条案例`) + (salesDown ? " · 销售数据表暂不可用，卡片暂不显示表现" : "");
  if ($("#library-drawer-done")) $("#library-drawer-done").textContent = `显示 ${items.length} 条结果`;
  let emptyTitle, emptyText, action, actionLabel;
  if (scope.length && (tokens.length || state.libraryProduct || (state.quickFilters || []).length || state.libraryCreator)) {
    emptyTitle = "没有找到匹配的案例";
    emptyText = archived
      ? "换一个关键词，或清除筛选。当前只检索已归档案例。"
      : "换一个关键词，或清除筛选。当前只检索未归档案例。";
    action = "clear";
    actionLabel = "清除筛选";
  } else if (scope.length && !categoryItems.length) {
    emptyTitle = archived ? "这个分类暂无归档案例" : "这个分类还没有案例";
    emptyText = "可以查看其他分类中保留的参考。";
    action = "all";
    actionLabel = archived ? "查看全部归档" : "查看全部案例";
  } else if (archived) {
    emptyTitle = "还没有已归档案例";
    emptyText = "在案例详情中归档后，会保留在这里；需要时可以恢复。";
    action = "active";
    actionLabel = "返回全部案例";
  } else if (content === "legacy") {
    emptyTitle = "当前没有使用中的旧库资料";
    emptyText =
      "接入的历史拆解会显示在这里；已归档的资料可以在“已归档”中找到。";
    action = "active";
    actionLabel = "返回全部案例";
  } else if (content === "custom") {
    emptyTitle = "这个视图暂无匹配的案例";
    emptyText = "已保留原有收纳，可切换其他视图或清除附加筛选。";
    action = "active";
    actionLabel = "查看全部案例";
  } else if (["self_produced", "overseas_creator"].includes(content)) {
    emptyTitle = "当前没有符合内容来源的案例";
    emptyText = "在编辑案例中标注内容来源后，符合条件的案例会显示在这里。";
    action = "active";
    actionLabel = "查看全部案例";
  } else if (content !== "all") {
    emptyTitle = "这个栏目还没有使用中的案例";
    emptyText = "可以查看其他栏目，或在导航设置中调整显示内容。";
    action = "active";
    actionLabel = "查看全部案例";
  } else {
    emptyTitle = state.cases.length
      ? "当前没有使用中的案例"
      : "把第一个参考案例收进来";
    emptyText = state.cases.length
      ? "已归档案例可通过页内归档筛选查看和恢复。"
      : "关联本地视频，保存完整文案和你的判断。";
    action = state.cases.length ? "archived" : "add";
    actionLabel = state.cases.length ? "查看已归档" : "收录案例";
  }
  $("#case-results").innerHTML = items.length
    ? nativeRows
      ? nativeEntriesView(nativeRows)
      : state.layout === "list"
      ? listView(items)
      : (state.quickFilters || []).includes("tidy")
      ? tidyGroups(items)
      : `<div class="case-grid">${items.map(selectableCard).join("")}</div>`
    : `<div class="empty">${icon(archived ? "archive" : "search")}<h3>${emptyTitle}</h3><p>${emptyText}</p><button class="secondary" id="empty-action">${actionLabel}</button></div>`;
  activateCardThumbnails();
  $("#case-results [data-tidy-breakdown]")?.addEventListener("click", event => void tidyBreakdown(event.currentTarget));
  if (nativeRows) bindNativeList(nativeRows);
  $$("[data-sort-header]", $("#case-results")).forEach((button) =>
    button.addEventListener("click", () => {
      window.LibraryViews?.detach();
      const key = button.dataset.sortHeader;
      state.sortDir =
        state.sort === key
          ? state.sortDir === "desc"
            ? "asc"
            : "desc"
          : "desc";
      state.sort = key;
      writeLibraryHash();
      renderCards();
    }),
  );
  $$("[data-case-select]").forEach((input) =>
    input.addEventListener("change", () => {
      if (state.bulk || state.bulkOperation) return;
      if (input.checked) state.selection.add(input.dataset.caseSelect);
      else state.selection.delete(input.dataset.caseSelect);
      renderSelectionToolbar(items);
      $$("[data-selection-card]").forEach((el) =>
        el.classList.toggle(
          "is-selected",
          state.selection.has(el.dataset.selectionCard),
        ),
      );
    }),
  );
  $("#empty-action")?.addEventListener("click", () => {
    if (action === "add") {
      openEditor(null);
      return;
    }
    state.query = "";
    state.libraryProduct = "";
    state.direction = "";
    state.variantType = "all";
    state.archiveCategory = "all";
    librarySetStatuses([]);
    state.quickFilters = [];
    state.libraryCreator = "";
    state.sourceType = "all";
    state.contentOrigin = "all";
    state.textForm = "all";
    state.archiveStatus = action === "archived" ? "archived" : action === "active" ? "active" : libraryArchiveStatus();
    if (["archived","active","all"].includes(action))
      state.view = "reference-all";
    writeLibraryHash();
    updateNav();
    renderLibrary();
  });
}
function refreshLibraryData() {
  const scope = scopedCases();
  const picker = $("#library-product-filter");
  if (picker) {
    picker.innerHTML = libraryProductOptions();
    picker.value = state.libraryProduct || "";
  }
  if ($("#direction-filter")) $("#direction-filter").innerHTML = directionFilterOptions(scope);
  renderCards();
}
function directionFilterOptions(scope) {
  const ids = [...new Set(scope.map(item => item.current?.test_direction?.direction_id).filter(Boolean))].sort();
  if (state.direction && state.direction !== "__unclassified__" && !ids.includes(state.direction)) ids.push(state.direction);
  return '<option value="">全部测试方向</option>' + `<option value="__unclassified__" ${state.direction === "__unclassified__" ? "selected" : ""}>未记录方向</option>` + ids.map(id => `<option value="${esc(id)}" ${state.direction === id ? "selected" : ""}>${esc(id)}</option>`).join("");
}
// 纯钩子屏显：只有屏显文字（翻译来源为屏显），没有收录口播逐字稿。
function isScreenOnlyCase(item) {
  return !item.current?.transcript?.text?.trim() && item.current?.translation_zh?.channel === "on_screen";
}
function libraryMatchingCases(options = {}) {
  const category = state.archiveCategory || "all";
  // Words separated by spaces must all appear, in any order.
  const terms = state.query.trim().toLowerCase().split(/\s+/u).filter(Boolean);
  const statuses = librarySelectedStatuses(), quick = state.quickFilters || [];
  return scopedCases()
    .filter(
      (item) =>
        (options.ignoreCategory || category === "all" || libraryPerformance(item) === category) &&
        (options.ignoreStatuses || !statuses.length || statuses.includes(item.library_performance?.status || "unknown")) &&
        quick.every(key => key === options.ignoreQuick || libraryQuickMatches(item, key)) &&
        (options.ignoreCreator || !state.libraryCreator || libraryCreator(item) === state.libraryCreator) &&
        (!state.contentOrigin || state.contentOrigin === "all" || item.current?.content_origin === state.contentOrigin) &&
        (!state.textForm || state.textForm === "all" || isScreenOnlyCase(item)) &&
        (state.sourceType === "all" || sourceType(item) === state.sourceType) &&
        (!state.direction || (state.direction === "__unclassified__" ? !item.current?.test_direction : item.current?.test_direction?.direction_id === state.direction)) &&
        (state.variantType === "all" || item.current?.test_direction?.variant_type === state.variantType) &&
        (!terms.length || terms.every(term => (item.search_text || "").toLowerCase().includes(term))),
    );
}
function filteredCases() {
  return libraryMatchingCases().sort((a, b) => {
      const av = librarySortValue(a, state.sort), bv = librarySortValue(b, state.sort),
        recent = b.updated_at.localeCompare(a.updated_at);
      if (av === undefined || bv === undefined) return av === bv ? recent : av === undefined ? 1 : -1;
      const quality = librarySortQuality(a, state.sort) - librarySortQuality(b, state.sort);
      if (quality) return quality;
      const diff = typeof av === "string" ? av.localeCompare(bv) : av - bv;
      return (state.sortDir === "asc" ? 1 : -1) * diff || recent;
    });
}
function selectControl(item) {
  return `<label class="case-select"><input type="checkbox" data-case-select="${esc(item.case_id)}" ${state.selection.has(item.case_id) ? "checked" : ""} ${state.bulk ? "disabled" : ""} aria-label="选择案例：${esc(shortTitle(item))}"><span>选择</span></label>`;
}
function selectableCard(item) {
  return `<article class="selectable-card ${state.selection.has(item.case_id) ? "is-selected" : ""}" data-selection-card="${esc(item.case_id)}">${selectControl(item)}${card(item)}</article>`;
}
function sortHeading(key, label, className = "") {
  const active = state.sort === key;
  const direction = active
    ? state.sortDir === "asc"
      ? "ascending"
      : "descending"
    : "none";
  return `<th scope="col"${className ? ` class="${className}"` : ""} aria-sort="${direction}"><button type="button" class="sort-heading" data-sort-header="${key}">${label}<span aria-hidden="true">${active ? (state.sortDir === "asc" ? "↑" : "↓") : "↕"}</span></button></th>`;
}
// One line of badges per row: archive state first, then the category, the rest folded into "+N".
function tableBadges(item, limit = 1) {
  const template = document.createElement("template");
  template.innerHTML = caseReadingBadges(item);
  const all = [...template.content.children], rest = all.slice(limit);
  return `<span class="table-badges">${all.slice(0, limit).map((el) => el.outerHTML).join("")}${rest.length ? `<span class="badge table-badge-more" title="${esc(rest.map((el) => el.textContent).join("、"))}">+${rest.length}</span>` : ""}</span>`;
}
function listView(items) {
  const extra = librarySortExtras[state.sort];
  return `<div class="case-table-wrap"><table class="case-table"><colgroup>${state.selecting ? '<col class="col-select">' : ""}<col><col class="col-product"><col class="col-category"><col class="col-direction"><col class="col-views"><col class="col-ctor"><col class="col-sold">${extra ? '<col class="col-extra">' : ""}<col class="col-updated"></colgroup><thead><tr>
    ${state.selecting ? '<th scope="col" class="case-selection-column">选择</th>' : ""}<th scope="col">案例标题</th><th scope="col">产品</th><th scope="col">分类</th><th scope="col">测试方向</th>
    ${sortHeading("views", "播放量")}${sortHeading("ctor", "CTOR")}${sortHeading("sold", "售出件数")}${extra ? sortHeading(state.sort, librarySortNames[state.sort], "num") : ""}
    ${sortHeading("updated", "最近更新")}
    </tr></thead><tbody>${items
      .map((item) => {
        const m = { ...caseIdentity(item), ...salesMetrics(item) }, updated = readableDate(item.updated_at);
        return `<tr data-selection-card="${esc(item.case_id)}" class="${state.selection.has(item.case_id) ? "is-selected" : ""}">
    ${state.selecting ? `<td class="case-selection-column">${selectControl(item)}</td>` : ""}<td><a class="table-case-link" href="#case/${esc(item.case_id)}" title="${esc(item.current?.title || item.title || "")}">${esc(shortTitle(item))}</a>${libraryCurrentMarkup(item)}<details class="table-case-identity"><summary>来源与编号</summary><p>${esc([m.creator_handle, m.video_id ? `视频 ${m.video_id}` : "", item.case_id].filter(Boolean).join(" · "))}</p><p>${esc(caseMetricContext(item))}</p></details></td>
    <td class="table-product" title="${esc(item.product || "")}">${displayValue(item.product)}</td><td>${tableBadges(item)}</td><td class="table-direction" title="${esc(directionChain(item.current?.test_direction))}">${esc(directionLabel(item))}</td>
    ${[m.views_display, m.ctor_display, m.items_sold, ...(extra ? [librarySortDisplay(item, state.sort)] : [])].map((value) => `<td class="num" title="${esc(value ?? "")}">${displayValue(value)}</td>`).join("")}
    <td class="table-date" title="${esc(updated)}">${esc(String(updated).split(" ")[0])}</td>
    </tr>`;
      })
      .join("")}</tbody></table></div>`;
}
function renderSelectionToolbar(items = filteredCases()) {
  const box = $("#selection-toolbar");
  if (!box) return;
  const selected = items.filter((item) => state.selection.has(item.case_id)),
    count = selected.length;
  const busy = !!state.bulk || !!state.bulkOperation,
    allArchived = count > 0 && selected.every((item) => item.archived),
    allActive = count > 0 && selected.every((item) => !item.archived);
  box.innerHTML = `<div class="selection-main"><label class="select-all"><input type="checkbox" id="select-visible" ${items.length && count === items.length ? "checked" : ""} ${!items.length || busy ? "disabled" : ""}><span>全选当前结果</span></label><span class="selection-count" role="status">已选 ${count} 条</span><button type="button" class="subtle" id="clear-selection" ${!count || busy ? "disabled" : ""}>清空选择</button><button type="button" class="secondary" id="compare-selected" ${count < 2 || count > 4 || busy ? "disabled" : ""}>并排对比（2–4条）</button><div class="bulk-actions">${libraryArchiveStatus() === "archived" || allArchived ? `<button type="button" class="secondary" id="bulk-restore" ${!allArchived || busy ? "disabled" : ""}>${icon("restore")}批量恢复</button><button type="button" class="danger-outline" id="bulk-delete" ${!allArchived || busy ? "disabled" : ""}>${icon("trash")}彻底删除所选</button>` : `<button type="button" class="secondary" id="bulk-archive" ${!allActive || busy ? "disabled" : ""}>${icon("archive")}批量归档</button>`}</div></div>`;
  $("#select-visible").indeterminate = count > 0 && count < items.length;
  $("#select-visible").onchange = (event) => {
    state.selection = event.target.checked
      ? new Set(items.map((item) => item.case_id))
      : new Set();
    renderCards();
  };
  $("#clear-selection").onclick = () => {
    state.selection.clear();
    renderCards();
  };
  $("#compare-selected")?.addEventListener("click", openComparison);
  $("#bulk-archive")?.addEventListener("click", () => startBulk("archive"));
  $("#bulk-restore")?.addEventListener("click", () => startBulk("restore"));
  $("#bulk-delete")?.addEventListener("click", () => startBulk("delete"));
  const summary = $("#bulk-last-result");
  if (summary) {
    summary.hidden = !state.lastBulkResult;
    summary.textContent = state.lastBulkResult || "";
  }
}
