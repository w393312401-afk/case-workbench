"use strict";

function list(items) {
  return items?.length
    ? `<ul class="bullet-list">${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>`
    : '<p class="context-note">暂未记录，可在编辑中补充。</p>';
}
function originalVideoLink(item) {
  const m = caseIdentity(item);
  const id = String(m.video_id || "").trim();
  const handle = String(m.creator_handle || "")
    .trim()
    .replace(/^@/, "");
  if (/^\d+$/.test(id)) {
    const account = /^[A-Za-z0-9._]+$/.test(handle) ? handle : "_";
    return `https://www.tiktok.com/@${account}/video/${id}`;
  }
  try {
    const url = new URL(item.original?.url || "");
    if (url.protocol === "https:" || url.protocol === "http:") return url.href;
  } catch {}
  return "";
}
function originalVideoAction(item) {
  const url = originalVideoLink(item);
  const isTikTok = url && /(^|\.)tiktok\.com$/i.test(new URL(url).hostname);
  return url
    ? `<a class="secondary original-video-link" href="${esc(url)}" target="_blank" rel="noopener noreferrer" title="在新标签页打开原视频">${icon("arrow")}${isTikTok ? "TikTok 原视频" : "打开原视频"}</a>`
    : '<span class="original-video-unavailable" title="当前案例未保存原帖链接或视频 ID，可在编辑案例中补充。">未记录原视频链接</span>';
}

function metricNumber(value) {
  const match = String(value ?? "")
    .trim()
    .replaceAll(",", "")
    .match(/^([\d.]+)\s*([KMW万亿%]?)$/i);
  if (!match) return null;
  return (
    Number(match[1]) *
    ({ K: 1000, M: 1000000, W: 10000, 万: 10000, 亿: 100000000 }[
      match[2].toUpperCase()
    ] || 1)
  );
}
function dataConsistency(item) {
  const m = salesMetrics(item), notes = [];
  const labels = { views_display: "播放量", items_sold: "售出件数", ctor_display: "CTOR" };
  const patterns = { views_display: /播放(?:量)?\s*([\d.]+\s*[KMW万亿]?)/gi, items_sold: /售出\s*([\d.]+)\s*件/gi, ctor_display: /CTOR\s*([\d.]+%)/gi };
  for (const [key, pattern] of Object.entries(patterns))
    for (const match of (item.current?.conclusion || "").matchAll(pattern))
      if (m[key] !== undefined && metricNumber(match[1]) !== metricNumber(m[key]))
        notes.push(`${labels[key]}：既有结论写 ${match[1]}，销售数据表当前为 ${m[key]}。`);
  if (!notes.length) return "";
  return `<section class="section-card consistency-note"><h2>结论中的历史数据</h2>${notes.map(n => `<p class="data-warning">${esc(n)}</p>`).join("")}<p>结论保留原判断；引用当前表现时，以销售数据表及其统计窗口为准。</p></section>`;
}

function normalizedScript(text) {
  return String(text || "")
    .replace(
      /^\s*\[(?:\d+(?:\.\d+)?s?|\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\s*[-–—]\s*(?:\d+(?:\.\d+)?s?|\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?)\]\s*/gm,
      "",
    )
    .replace(/\s+/g, " ")
    .trim();
}
function scriptSentences(text) {
  const clean = normalizedScript(text);
  if (!clean) return [];
  return [
    ...new Intl.Segmenter("en", { granularity: "sentence" }).segment(clean),
  ]
    .map((x) => x.segment.trim())
    .filter(Boolean);
}
function comparisonMetric(key, value) {
  if (value === undefined || value === null || value === "") return "未提供";
  if (
    [
      "views_display",
      "product_impressions_display",
      "product_clicks_display",
    ].includes(key)
  ) {
    const n = metricNumber(value);
    if (n !== null)
      return n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  }
  if (key === "ctor_display") {
    const n = metricNumber(value);
    if (n !== null) return n.toFixed(2) + "%";
  }
  if (key === "ticket_value") {
    const match = String(value).match(/^\$?([\d.]+)\s*(USD|美元)?$/i);
    if (match && (match[2] || String(value).startsWith("$")))
      return "$" + Number(match[1]).toFixed(2) + " USD";
  }
  return String(value);
}

async function openComparison() {
  let items = state.cases.filter((i) => state.selection.has(i.case_id));
  if (items.length < 2 || items.length > 4) return;
  const products = items.map(productFilterGroup);
  if (products.some((p) => !p) || new Set(products).size !== 1) {
    toast("请选择同一产品的 2–4 条案例进行对比。", true);
    return;
  }
  try {
    items = await Promise.all(items.map((item) => api("/api/cases/" + encodeURIComponent(item.case_id))));
  } catch (error) {
    toast(`案例读取失败：${error.message}`, true);
    return;
  }
  const sourceText = (item, opening = false) => {
    const display = transcriptDisplay(item);
    if (!display.text) return "未提供";
    const text = opening
      ? scriptSentences(display.text)[0] || display.text
      : display.heading === "完整文案"
        ? scriptSentences(display.text).join("\n\n")
        : display.text;
    return display.heading === "完整文案" ? text : `${display.heading}\n${text}`;
  };
  const chineseText = (item) => {
    const rows = transcriptComparison(item).filter((row) => row.chinese);
    if (!rows.length) return "未提供";
    const label = transcriptDisplay(item).heading;
    const text = rows.map((row) => row.chinese).join("\n\n");
    return label === "完整文案" ? text : `${label}\n${text}`;
  };
  document.querySelector("#case-comparison")?.remove();
  const dialog = document.createElement("dialog");
  dialog.id = "case-comparison";
  const rows = [
    ["视频 ID", (i) => caseIdentity(i).video_id],
    ["内容来源", contentOriginLabel],
    [
      "开头原文（首句）",
      (i) => sourceText(i, true),
    ],
    ["购买理由", (i) => i.current?.purchase_reason || "未单独整理"],
    ["测试方向", directionLabel],
    ["方向链", (i) => directionChain(i.current?.test_direction)],
    ["执行改动", (i) => i.current?.test_direction?.execution_change],
    ["文案版本", (i) => i.current?.test_direction?.script_version || i.current?.production?.script_version],
    ["当前结论", (i) => i.current?.conclusion],
    ...metrics.map(([k, l]) => [
      l,
      (i) => comparisonMetric(k, salesMetrics(i)[k]),
    ]),
    [
      "统计窗口",
      (i) => {
        const w = salesEvidence(i).window;
        return !w || /未显示|未提供|未知/.test(w) ? "未提供" : w;
      },
    ],
    [
      "证据状态",
      (i) =>
        ({
          source_checked: "已核对来源",
          user_reported: "用户提供",
          unverified: "待核实",
        })[salesEvidence(i).status] || salesStatusLabel(i),
    ],
    ["数据截至", (i) => salesEvidence(i).as_of],
    ["销售数据", salesStatusLabel],
    [
      "当前原文",
      (i) => sourceText(i),
    ],
    ["中文对照", chineseText],
  ];
  dialog.innerHTML = `<div class="dialog-heading"><h2>同产品案例对比</h2><button class="secondary" id="close-comparison">关闭</button></div><p class="comparison-note">按当前记录并排展示。表现统一读取销售数据表，未匹配时不显示旧截图数值。统计窗口或投流条件不同，不能直接归因于文案优劣；原文与中文对照保留各自来源，屏显文字和历史片段均单独标明。播放、曝光、点击统一显示实际数量。</p><div class="comparison-scroll"><table class="comparison-table"><thead><tr><th>对比项</th>${items.map((i) => `<th><a href="#case/${esc(i.case_id)}" data-comparison-link>${esc(shortTitle(i))}</a></th>`).join("")}</tr></thead><tbody>${rows.map(([label, get]) => `<tr><th>${esc(label)}</th>${items.map((i) => `<td>${label === "证据与条件" ? `<details><summary>查看来源与条件</summary>${esc(get(i) || "未提供")}</details>` : esc(get(i) ?? "未提供")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  document.body.append(dialog);
  dialog.querySelector("#close-comparison").onclick = () => dialog.close();
  dialog
    .querySelectorAll("[data-comparison-link]")
    .forEach((a) => (a.onclick = () => dialog.close()));
  dialog.addEventListener("close", () => dialog.remove());
  dialog.showModal();
}

function hasPerformanceData(item) {
  return item.sales_data?.status === "matched";
}
function renderPerformanceSection(item) {
  const m = salesMetrics(item), e = salesEvidence(item), v = item.sales_data?.video || {};
  const action = `<a class="secondary" href="${esc(salesDataURL(item))}">${icon("chart")}查看销售数据表</a>`;
  if (!hasPerformanceData(item))
    return `<section class="section-card performance-empty"><h2>表现数据</h2><div class="performance-empty-row"><p>${esc(salesStatusLabel(item))}</p>${action}</div><p class="context-note">${item.sales_data?.status === "missing_video_id" ? "在编辑案例中填写视频 ID 后，按同一视频匹配销售数据。" : "导入或同步销售数据表后，此处显示对应视频的表现。"}</p></section>`;
  const figures = [...metrics.map(([key,label]) => [label,m[key]]), ...(v.orders != null ? [["订单数",v.orders]] : []), ...(v.gmv != null ? [["GMV", "$" + Number(v.gmv).toLocaleString("en-US", {maximumFractionDigits:2})]] : [])];
  return `<section class="section-card"><div class="section-title-row"><h2>${icon("chart")}表现数据</h2>${action}</div><div class="facts-grid">${figures.map(([label,value]) => `<div><span class="metric-label">${esc(label)}</span><span class="metric-value ${label === "CTOR" ? "accent" : ""}">${displayValue(value)}</span></div>`).join("")}</div><p class="context-note">销售数据表 · ${esc(e.window || "统计窗口未提供")}${e.as_of ? ` · 截至 ${esc(e.as_of)}` : ""}</p>${e.context ? `<p class="context-note">${esc(e.context)}</p>` : ""}${v.status_label ? `<p class="context-note">跟进状态：${esc(v.status_label)}</p>` : ""}</section>`;
}
function renderPerformanceSummary(item) {
  if (!hasPerformanceData(item))
    return `<button class="case-summary-item is-empty" data-summary="表现数据"><span class="case-summary-label">表现数据</span><p>${esc(salesStatusLabel(item))}</p></button>`;
  const m = salesMetrics(item), e = salesEvidence(item);
  return `<button class="case-summary-item" data-summary="表现数据"><span class="case-summary-label">销售数据表</span><div class="case-summary-metrics"><span>播放 <strong>${displayValue(m.views_display)}</strong></span><span>售出 <strong>${displayValue(m.items_sold)}</strong></span><span>CTOR <strong>${displayValue(m.ctor_display)}</strong></span></div><small>${esc(e.as_of ? "截至 " + e.as_of : e.window || "统计窗口未提供")}</small></button>`;
}

function refreshDetailSalesData(summary) {
  const item = state.detail;
  if (!item || item.case_id !== summary.case_id) return;
  item.sales_data = structuredClone(summary.sales_data);
  const section = $("#detail-section-performance");
  const template = document.createElement("template");
  template.innerHTML = renderPerformanceSection(item);
  if (section) {
    section.className = template.content.firstElementChild.className;
    section.innerHTML = template.content.firstElementChild.innerHTML;
  }
  template.innerHTML = renderPerformanceSummary(item);
  for (const button of $$('.case-summary-item[data-summary="表现数据"]')) {
    button.className = template.content.firstElementChild.className;
    button.innerHTML = template.content.firstElementChild.innerHTML;
  }
  const release = $("#detail-section-release");
  if (release) {
    template.innerHTML = renderReleaseSection(item);
    release.className = template.content.firstElementChild.className;
    release.innerHTML = template.content.firstElementChild.innerHTML;
    bindReleaseEditor(item);
  }
  const consistency = $("#detail-section-consistency");
  const note = dataConsistency(item);
  if (note) {
    template.innerHTML = note;
    const updated = template.content.firstElementChild;
    updated.id = "detail-section-consistency";
    updated.dataset.detailGroup = "overview";
    updated.dataset.detailLabel = "数据一致性";
    if (consistency) consistency.replaceWith(updated);
    else release?.after(updated);
  } else consistency?.remove();
  bindDetailInlineEditors(item);
  buildDetailNav();
}

function bindReleaseEditor(item) {
  if ($("#release-edit")) $("#release-edit").onclick = () => {
    if (window.CaseInlineEditor) return window.CaseInlineEditor.open(item, "release");
    if (!openEditor(item)) return;
    const field = $('[name="publish_published_on"]');
    field.scrollIntoView({ block: "center" });
    field.focus();
  };
}

const productionLabels = {
  script_version: "文案版本", davinci_project: "达芬奇项目", davinci_timeline: "达芬奇时间线",
  voiceover: "配音文件", source_ledger: "源区间账本",
};
function renderReleaseSection(item) {
  const cur = item.current, production = cur.production || {};
  if (!cur.publish && !cur.production) {
    return `<section class="section-card performance-empty"><h2 class="visually-hidden">发布与制作</h2><div class="performance-empty-row"><p>未记录发布日期与制作关联</p><button type="button" class="secondary" id="release-edit">补发布信息</button></div></section>`;
  }
  const rows = [
    ["发布日期", cur.publish?.published_on],
    ["销售数据截至", salesEvidence(item).as_of],
    ...Object.entries(productionLabels).map(([key, label]) => [label, production[key]]),
  ].filter(([, value]) => value);
  return `<section class="section-card"><div class="section-title-row"><h2>${icon("video")}发布与制作</h2><button type="button" class="secondary" id="release-edit">编辑发布信息</button></div><dl class="release-list">${rows.map(([label, value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl></section>`;
}

function renderTranscriptSection(item) {
  const cur = item.current, display = transcriptDisplay(item);
  return `<section class="section-card"><div class="section-title-row"><h2>${icon("file")}${esc(display.heading)}</h2><div class="transcript-actions"><button type="button" class="subtle" id="toggle-transcript-zh" aria-expanded="false" aria-controls="transcript-comparison">中文对照</button><button class="subtle" id="copy-transcript">${icon("copy")}复制</button></div></div><div class="transcript" id="transcript-original">${esc(display.text || "暂未整理当前有效文案。")}</div><div id="transcript-comparison" hidden></div><p class="transcript-status">${display.heading !== "完整文案" ? esc(display.status) : cur.source_case ? `文案状态：${esc(historicalTranscriptStatus(cur.transcript?.status || cur.source_case.transcript_status))}，以来源记录为准。<br>${esc(cur.transcript?.source || "历史转写，本次未重新听辨。")}` : esc(cur.transcript?.status || "核实状态未提供。")}</p></section>`;
}

// A section owns its label, outline group, visibility and renderer in one place.
const detailSectionDefinitions = [
  {
    id: "conclusion", label: "当前结论", group: "overview", tab: "current", edit: "overview",
    when: item => Boolean(item.current),
    render: item => `<section class="section-card"><h2>${icon("spark")}当前结论</h2><p class="conclusion">${esc(item.current.conclusion)}</p></section>`,
  },
  {
    id: "purchase-reason", label: "主要购买理由", group: "overview", tab: "current",
    when: item => Boolean(item.current),
    render: item => `<section class="section-card"><h2>${icon("check")}主要购买理由</h2><p class="conclusion">${esc(item.current.purchase_reason || "未单独整理")}</p></section>`,
  },
  {
    id: "performance", label: "表现数据", group: "overview", tab: "data", edit: "metrics",
    when: item => Boolean(item.current), render: renderPerformanceSection,
  },
  {
    id: "test-direction", label: "测试方向", group: "overview", tab: "current", edit: "direction",
    when: item => Boolean(item.current), render: renderTestDirection,
  },
  {
    id: "release", label: "发布与制作", group: "overview", tab: "data", edit: "release",
    when: item => Boolean(item.current), render: renderReleaseSection,
  },
  {
    id: "consistency", label: "数据一致性", group: "overview", tab: "data",
    when: item => Boolean(dataConsistency(item)), render: dataConsistency,
  },
  {
    id: "native", label: "本土表达与场景", group: "deep", tab: "script", edit: "native",
    when: item => Boolean(item.current?.native_reference), render: nativeDetail,
  },
  {
    id: "transcript", label: item => transcriptDisplay(item).heading, group: "deep", tab: "script", edit: "transcript",
    when: item => Boolean(item.current), render: renderTranscriptSection,
  },
  {
    id: "highlights", label: "可借鉴的表达", group: "deep", tab: "script",
    when: item => Boolean(item.current && !item.current.native_reference),
    render: item => `<section class="section-card"><h2>${icon("check")}可借鉴的表达</h2>${list(item.current.highlights)}</section>`,
  },
  {
    id: "limitations", label: "问题与适用边界", group: "deep", tab: "script",
    when: item => Boolean(item.current),
    render: item => `<section class="section-card"><h2>${icon("search")}问题与适用边界</h2>${list(item.current.limitations)}</section>`,
  },
];

function renderDetailSections(item, tab = "current") {
  const container = document.createElement("div");
  container.className = "detail-info";
  container.id = `detail-panel-${tab}`;
  container.setAttribute("role", "tabpanel");
  container.setAttribute("aria-labelledby", `detail-tab-${tab}`);
  for (const definition of detailSectionDefinitions) {
    if (definition.tab !== tab || !definition.when(item)) continue;
    const template = document.createElement("template");
    template.innerHTML = definition.render(item);
    const section = template.content.firstElementChild;
    if (!section) continue;
    section.id = `detail-section-${definition.id}`;
    section.dataset.detailGroup = definition.group;
    section.dataset.detailTab = tab;
    section.dataset.detailLabel = typeof definition.label === "function" ? definition.label(item) : definition.label;
    if (definition.edit) section.dataset.inlineSection = definition.edit;
    container.append(section);
  }
  if (tab === "script") {
    const insights = document.createElement("div");
    insights.dataset.inlineSection = "insights";
    insights.className = "detail-insights";
    for (const id of ["highlights", "limitations"]) {
      const section = container.querySelector(`#detail-section-${id}`);
      if (section) insights.append(section);
    }
    container.append(insights);
    const source = document.createElement("div");
    source.id = "detail-source-analysis";
    container.append(source);
  }
  if (tab === "current") {
    const metrics = document.createElement("div");
    metrics.className = "detail-overview-metrics";
    metrics.innerHTML = `<div class="section-title-row"><h2>核心表现</h2><button type="button" class="subtle" data-detail-tab="data">查看数据</button></div>${renderPerformanceSummary(item)}`;
    container.querySelector("#detail-section-conclusion")?.after(metrics);
  }
  return container.outerHTML;
}

const detailTabs = { current: "概要", script: "文案与拆解", data: "数据", history: "历史" };
const detailEditLabels = { overview: "编辑概要", transcript: "编辑文案", insights: "编辑拆解记录", direction: "编辑方向", release: "编辑发布信息", metrics: "编辑数据关联", assets: "编辑素材关联", native: "编辑本土表达" };
function detailTabURL(item, tab) {
  const hash = `#case/${item.case_id}${tab === "current" ? "" : "/" + tab}`;
  return window.WorkbenchShell?.url(hash) || hash;
}
function activateDetailTab(item, tab) {
  state.detailTab = tab === "analysis" ? "script" : tab;
  history.replaceState(null, "", detailTabURL(item, state.detailTab));
  if (state.editor?.mode === "inline" && state.editor.item?.case_id === item.case_id)
    state.editor.originHash = location.hash;
  switchDetailTab(item);
}
window.CaseDetailTabs = {
  showSection(sectionId) {
    const item = state.detail;
    if (!item) return;
    const tab = ["transcript", "insights", "native"].includes(sectionId) ? "script" : ["release", "metrics"].includes(sectionId) ? "data" : "current";
    activateDetailTab(item, tab);
    const target = $(`[data-inline-section="${sectionId}"]`);
    if (sectionId === "assets") {
      $(".video-panel [data-mode='fixed']")?.click();
      let details = target?.closest("details");
      while (details) { details.open = true; details = details.parentElement?.closest("details"); }
    }
    scrollDetailSection(target);
  },
};
function detailCompletion(item) {
  const cur = item.current || {}, direction = cur.test_direction || {};
  return [
    { section: "transcript", label: "文案", tab: "script", ready: Boolean(transcriptDisplay(item).text?.trim()) },
    { section: "insights", label: "拆解", tab: "script", ready: Boolean(item.source_analysis?.text?.trim() || cur.highlights?.length || cur.native_reference?.entries?.length) },
    { section: "direction", label: "方向", tab: "current", ready: ["direction_id", "main_difference", "concern", "closing"].every(key => String(direction[key] || "").trim()) },
    { section: "release", label: "发布", tab: "data", ready: Boolean(cur.publish?.published_on) },
  ];
}
function renderDetailSide(item) {
  const tasks = detailCompletion(item), missing = tasks.filter(task => !task.ready).length;
  return `<aside class="detail-side" aria-label="案例协作与完整度"><div id="detail-ai-panel"></div><section class="detail-completion"><div class="section-title-row"><h2>待补全</h2><span>${missing ? `${missing} 项` : "已齐备"}</span></div><ul>${tasks.map(task => `<li class="${task.ready ? "is-ready" : "is-missing"}"><span>${icon(task.ready ? "check" : "edit")}${esc(task.label)}</span><button type="button" class="subtle" data-completion-section="${task.section}" data-completion-tab="${task.tab}">${task.ready ? "查看" : "补充"}</button></li>`).join("")}</ul></section><div id="detail-outline"></div></aside>`;
}
function bindDetailInlineEditors(item) {
  $$("[data-inline-section]").forEach(section => {
    const name = section.dataset.inlineSection;
    if (section.querySelector(`[data-inline-edit="${name}"]`) || name === "release") return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "subtle detail-inline-edit";
    button.dataset.inlineEdit = name;
    button.innerHTML = `${icon("edit")}${esc(detailEditLabels[name])}`;
    section.prepend(button);
  });
  $$("[data-inline-edit]").forEach(button => button.onclick = () => {
    if (window.CaseInlineEditor) window.CaseInlineEditor.open(item, button.dataset.inlineEdit);
    else openEditor(item);
  });
  $$("[data-detail-tab]").forEach(button => button.onclick = () => activateDetailTab(item, button.dataset.detailTab));
  $$("[data-summary='表现数据']").forEach(button => button.onclick = () => {
    activateDetailTab(item, "data");
    scrollDetailSection($("#detail-section-performance"));
  });
  $$("[data-completion-section]").forEach(button => button.onclick = () => {
    activateDetailTab(item, button.dataset.completionTab);
    const target = $(`[data-inline-section="${button.dataset.completionSection}"]`);
    scrollDetailSection(target);
    const task = detailCompletion(item).find(task => task.section === button.dataset.completionSection);
    if (!task.ready) window.CaseInlineEditor?.open(item, task.section);
  });
}

function renderDetail(item) {
  const cur = item.current,
    m = caseIdentity(item);
  const archiveBusy = state.archiveOperation?.caseId === item.case_id;
  const busy = archiveBusy || state.deleteOperation?.caseId === item.case_id;
  const archiveError =
    state.archiveError?.caseId === item.case_id
      ? state.archiveError.message
      : "";
  $("#breadcrumb").innerHTML =
    "工作空间 <span>/</span> 案例库 <span>/</span> 案例详情";
  $("#content").innerHTML =
    `<a class="back-link" href="${esc(state.libraryHash)}">${icon("back")}返回${esc(navigationLabel())}</a><div class="detail-heading"><div><div class="case-badges">${badge(item)}${sourceBadges(item, true)}${archiveBadge(item)}</div><h1>${esc(cur?.title || item.title)}</h1><p class="detail-meta">${esc(item.product || "产品未提供")} · ${esc(m.creator_handle || "账号未提供")} · ${esc(item.case_id)}</p></div><div class="detail-tools"><details class="detail-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" id="open-case-folder">${icon("folder")}案例文件夹</button><button type="button" role="menuitem" id="archive-case" ${busy ? 'disabled aria-busy="true"' : ""}>${icon(item.archived ? "restore" : "archive")}${archiveBusy ? (state.archiveOperation.archived ? "正在归档…" : "正在恢复…") : item.archived ? "恢复案例" : "归档案例"}</button>${item.archived ? `<button type="button" role="menuitem" class="is-danger" id="delete-case" ${busy ? "disabled" : ""}>${icon("trash")}彻底删除</button>` : ""}</div></details><button class="primary compact" id="edit-case" ${busy ? "disabled" : ""}>${icon("edit")}编辑案例</button></div></div><div class="archive-notice ${item.archived ? "is-archived" : ""}">${icon(item.archived ? "archive" : "restore")}<p>${item.archived ? "此案例已归档。视频、文案和修订历史仍可查看、编辑；恢复后回到原表现分类。" : "归档后可在“已归档”中查看并恢复，原表现分类和修订历史继续保留。"}</p></div>${archiveError ? `<div class="archive-error" role="alert">${esc(archiveError)}</div>` : ""}<div class="detail-tabs" role="tablist" aria-label="案例内容">${Object.entries(detailTabs).map(([tab, label]) => `<button type="button" role="tab" id="detail-tab-${tab}" class="tab" data-tab="${tab}" aria-controls="${tab === "history" ? "detail-other" : "detail-panel-" + tab}" aria-selected="false">${esc(label)}${tab === "history" ? ` <small>${item.events.length}</small>` : ""}</button>`).join("")}</div><div id="detail-body"></div><div id="detail-other" hidden></div>`;
  mountDetailWorkspace(item);
  $("#edit-case").onclick = () => openEditor(item);
  $("#archive-case").onclick = () => setArchived(item, !item.archived);
  $("#delete-case")?.addEventListener("click", () => openDelete(item));
  $(".detail-menu")?.addEventListener("click", event => { if (event.target.closest("[role=menuitem]")) event.currentTarget.open = false; });
  $("#open-case-folder").onclick = () =>
    openFolder({ target: "case", case_id: item.case_id });
  $$("[data-tab]").forEach(
    (el) =>
      (el.onclick = () => {
        location.hash = detailTabURL(item, el.dataset.tab);
      }),
  );
  $(".detail-tabs").addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = $$(".detail-tabs [data-tab]"), index = tabs.indexOf(event.target);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    activateDetailTab(item, tabs[next].dataset.tab);
    tabs[next].focus();
  });
  if (!cur) {
    $("#detail-other").remove();
    $("#detail-body").innerHTML =
      `<div class="detail-layout detail-no-video"><div class="detail-main">${["current", "script", "data"].map(tab => `<div class="detail-info" id="detail-panel-${tab}" role="tabpanel" aria-labelledby="detail-tab-${tab}"><section class="section-card" data-inline-section="${tab === "current" ? "overview" : tab === "script" ? "transcript" : "release"}"><h2>这条案例等待整理</h2><p>编辑并确认当前有效视频、文案与结论后，即可进入参考库。首次收录内容保留在历史中。</p></section></div>`).join("")}<div id="detail-other" role="tabpanel" aria-labelledby="detail-tab-history" hidden></div></div>${renderDetailSide(item)}</div>`;
    $("#detail-body").dataset.rendered = "true";
    window.CaseAI?.mountCaseActions(item);
    const aiPanel = $(".ai-case-actions");
    if (aiPanel) $("#detail-ai-panel").append(aiPanel);
    bindDetailInlineEditors(item);
    switchDetailTab(item);
    return;
  }
  const validVideo = item.assets?.video?.exists;
  const displayedTranscript = transcriptDisplay(item);
  $("#detail-other").remove();
  $("#detail-body").innerHTML =
    `<div class="detail-layout"><div class="video-panel" data-video-drop><div class="player-wrap">${validVideo ? `<video id="case-player" controls playsinline preload="metadata" src="${mediaURL(item.case_id, "video", item.current_event_id)}"></video>` : `<div class="missing-media">${icon("video")}<span>${item.assets?.video ? "原视频已移动或暂不可用" : "尚未关联视频"}</span><span>拖入视频或点击下方导入按钮</span></div>`}</div><div class="panel-caption"><span>当前有效视频</span>${validVideo ? `<button class="subtle" id="open-video-folder">${icon("folder")}打开所在文件夹</button>` : ""}</div>${originalVideoAction(item)}<p class="asset-name">${esc(item.assets?.video?.name || "")}</p><p class="video-copy-state ${validVideo && item.assets.video.owned ? "is-saved" : ""}">${item.assets?.video?.owned ? (validVideo ? "已保存案例库副本" : "案例库视频副本暂不可用，请重新导入。") : item.assets?.video ? "当前视频尚未备份到本案例；保存或重新导入可备份。" : "导入视频后，自动复制一份到案例库。"}</p><button type="button" class="secondary detail-video-import" id="detail-video-import">${icon("video")}${validVideo ? "导入或更换视频" : "导入视频"}<span>也可拖到这里</span></button><div data-inline-section="assets" class="detail-assets-inline"></div><div class="context-note media-advice" hidden>当前浏览器无法播放此视频，可打开所在文件夹使用本地播放器。</div></div><div class="detail-main">${["current", "script", "data"].map(tab => renderDetailSections(item, tab)).join("")}<div id="detail-other" role="tabpanel" aria-labelledby="detail-tab-history" hidden></div></div>${renderDetailSide(item)}</div>`;
  $("#detail-body").dataset.rendered = "true";
  const rail = document.createElement("div");
  rail.className = "detail-rail";
  $(".detail-layout").prepend(rail);
  rail.append($(".video-panel"), $(".detail-side"));
  bindNativeDetail();
  if (cur.source_case) renderSourceAnalysis(item);
  else $("#detail-source-analysis").innerHTML = '<section class="section-card detail-source-empty"><h2>原始拆解</h2><p class="context-note">尚未关联原始拆解文档。已有的表达判断与适用边界保留在上方。</p></section>';
  window.CaseAI?.mountCaseActions(item);
  const aiPanel = $(".ai-case-actions");
  if (aiPanel) $("#detail-ai-panel").append(aiPanel);
  bindDetailInlineEditors(item);
  mountVideoWorkspace(item);
  bindReleaseEditor(item);
  void window.CaseReviews?.decorateCase(item);
  $("#detail-video-import").onclick = () => openVideoPicker(item);
  $("#copy-transcript").onclick = () =>
    copyText(displayedTranscript.text, "原文已复制");
  $("#toggle-transcript-zh").onclick = () => {
    const key = transcriptComparisonKey(item);
    transcriptComparisonOpen.set(key, !transcriptComparisonOpen.get(key));
    renderTranscriptComparison(item);
  };
  const comparisonKey = transcriptComparisonKey(item);
  if (!transcriptComparisonOpen.has(comparisonKey) && transcriptComparison(item).some(row => row.chinese))
    transcriptComparisonOpen.set(comparisonKey, true);
  renderTranscriptComparison(item);
  $("#open-video-folder")?.addEventListener("click", () =>
    openFolder({
      target: "asset",
      case_id: item.case_id,
      kind: "video",
      event_id: item.current_event_id,
    }),
  );
  $("#case-player")?.addEventListener(
    "error",
    () => {
      // The player can fail after the user has already left this case.
      const advice = $(".media-advice");
      if (advice) advice.hidden = false;
    },
  );
  switchDetailTab(item);
}
let detailNavCleanup = null;
let detailWorkspaceObserver = null;
let detailVideoCleanup = null;

function scrollDetailSection(section) {
  if (!section) return;
  const top = Number.parseFloat(getComputedStyle($("#content")).getPropertyValue("--detail-sticky-top")) || 150;
  window.scrollTo({ top: window.scrollY + section.getBoundingClientRect().top - top,
    behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
}

function mountDetailWorkspace(item) {
  detailNavCleanup?.();
  detailVideoCleanup?.();
  detailWorkspaceObserver?.disconnect();
  const heading = $(".detail-heading"), title = heading.querySelector("h1");
  const tools = heading.querySelector(".detail-tools"), meta = heading.querySelector(".detail-meta");
  const badges = heading.querySelector(".case-badges");
  const dock = document.createElement("div");
  dock.className = "case-dock";
  heading.before(dock);
  const bar = document.createElement("div");
  bar.className = "detail-heading case-dock-bar";
  title.className = "case-dock-title";
  title.title = title.textContent;
  badges.className = "case-dock-badges";
  // The return link rides in the sticky bar so it stays reachable without its own row.
  const back = $(".back-link");
  if (back) {
    const label = back.textContent.trim();
    back.classList.add("case-dock-back");
    back.title = label;
    back.setAttribute("aria-label", label);
    back.innerHTML = `${icon("back")}<span>返回</span>`;
    bar.append(back);
  }
  bar.append(title, badges);
  const cases = filteredCases(), index = cases.findIndex(entry => entry.case_id === item.case_id);
  for (const [offset, label] of [[-1, "上一条"], [1, "下一条"]]) {
    const next = index >= 0 ? cases[index + offset] : null;
    const link = document.createElement(next ? "a" : "span");
    link.className = "case-step";
    link.textContent = label;
    if (next) { link.href = `#case/${encodeURIComponent(next.case_id)}`; link.title = next.current?.title || next.title; }
    else link.setAttribute("aria-disabled", "true");
    bar.append(link);
  }
  dock.append(bar);
  if (item.current) {
    const cur = item.current;
    const summary = document.createElement("div");
    summary.className = "case-summary";
    summary.setAttribute("aria-label", "案例摘要");
    summary.innerHTML = `<button class="case-summary-item" data-summary="当前结论" title="${esc(cur.conclusion || "未记录")}（点击查看全文）"><span class="case-summary-label">当前结论</span><p>${esc(cur.conclusion || "未记录")}</p></button><button class="case-summary-item" data-summary="主要购买理由" title="${esc(cur.purchase_reason || "未单独整理")}（点击查看全文）"><span class="case-summary-label">主要购买理由</span><p>${esc(cur.purchase_reason || "未单独整理")}</p></button>${renderPerformanceSummary(item)}`;
    summary.querySelectorAll("button").forEach(button => button.onclick = () => {
      activateDetailTab(item, button.dataset.summary === "表现数据" ? "data" : "current");
      const section = $$(".detail-info section.section-card").find(section => section.querySelector("h2")?.textContent.trim() === button.dataset.summary);
      scrollDetailSection(section);
    });
    dock.append(summary);
  }
  // Tabs, case meta, AI entry points (mounted by ai.js) and case tools share one row.
  const actions = document.createElement("div");
  actions.className = "detail-action-row";
  const tabs = $(".detail-tabs");
  if (tabs) actions.append(tabs);
  meta.title = meta.textContent;
  actions.append(meta, tools);
  dock.after(actions);
  heading.remove();
  if (!item.archived) $(".archive-notice")?.remove();
  const measure = () => {
    if (!dock.isConnected) { detailWorkspaceObserver?.disconnect(); return; }
    $("#content").style.setProperty("--detail-sticky-top", `${Math.ceil(dock.getBoundingClientRect().height) + 12}px`);
  };
  measure();
  if ("ResizeObserver" in window) {
    detailWorkspaceObserver = new ResizeObserver(measure);
    detailWorkspaceObserver.observe(dock);
  }
}

function mountVideoWorkspace(item) {
  const panel = $(".video-panel");
  if (!panel) return;
  const body = document.createElement("div");
  body.className = "video-panel-body";
  body.id = "video-panel-body";
  while (panel.firstChild) body.append(panel.firstChild);
  const metadata = document.createElement("details");
  metadata.className = "video-metadata";
  const metadataLabel = document.createElement("summary");
  metadataLabel.textContent = "视频文件与来源";
  metadata.append(metadataLabel);
  // A saved original link joins the mode switch; the "not recorded" note stays with the file details.
  const originalLink = body.querySelector(".original-video-link");
  originalLink?.remove();
  [...body.children].filter(child => !child.matches(".player-wrap, .segment-player-controls")).forEach(child => metadata.append(child));
  body.append(metadata);
  const file = metadata.querySelector(".asset-name");
  if (file) {
    file.title = file.textContent;
    const row = document.createElement("div"); row.className = "video-file-row";
    file.before(row); row.append(file);
    if (file.textContent) {
      const copy = document.createElement("button"); copy.className = "icon-button";
      copy.title = "复制视频文件名"; copy.setAttribute("aria-label", copy.title);
      copy.innerHTML = icon("copy"); copy.onclick = () => copyText(file.textContent, "文件名已复制"); row.append(copy);
    }
  }
  const folder = metadata.querySelector("#open-video-folder");
  if (folder) { folder.innerHTML = icon("folder"); folder.title = "打开所在文件夹"; folder.setAttribute("aria-label", folder.title); }
  const modes = document.createElement("div");
  modes.className = "video-mode-switch"; modes.setAttribute("aria-label", "视频显示方式");
  modes.innerHTML = '<span>视频</span><button type="button" data-mode="fixed" aria-controls="video-panel-body">固定</button><button type="button" data-mode="floating" aria-controls="video-panel-body">悬浮</button><button type="button" data-mode="collapsed" aria-controls="video-panel-body">收起</button>';
  if (originalLink) {
    originalLink.classList.remove("secondary");
    originalLink.title = `在新标签页打开${originalLink.textContent.trim()}`;
    originalLink.lastChild.textContent = "原视频";
    modes.append(originalLink);
  }
  panel.append(modes, body);
  const compact = matchMedia("(max-width: 900px), (max-height: 650px)");
  const memoryKey = `case-video-mode:${item.case_id}`;
  let selectedMode = null, frame = 0;
  try { selectedMode = sessionStorage.getItem(memoryKey); } catch {}
  const measure = () => {
    frame = 0;
    if (!panel.isConnected || body.hidden) return;
    const stickyTop = Number.parseFloat(getComputedStyle($("#content")).getPropertyValue("--detail-sticky-top")) || 150;
    const floatingTop = Math.max(stickyTop, $(".case-dock").getBoundingClientRect().bottom + 12);
    panel.style.setProperty("--detail-floating-top", `${floatingTop}px`);
    const extra = modes.getBoundingClientRect().height + [...body.children].filter(child => !child.matches(".player-wrap")).reduce((sum, child) => sum + child.getBoundingClientRect().height, 0) + 34;
    // Size the player for its docked position so it keeps one size while the page scrolls;
    // before the header scrolls away the panel may extend below the fold instead of shrinking.
    panel.style.setProperty("--detail-video-height", `${Math.max(120, window.innerHeight - (panel.dataset.mode === "floating" ? floatingTop : stickyTop) - extra)}px`);
  };
  const requestMeasure = () => { if (!frame) frame = requestAnimationFrame(measure); };
  const applyMode = () => {
    const mode = ["fixed", "floating", "collapsed"].includes(selectedMode) ? selectedMode : compact.matches ? "collapsed" : "fixed";
    panel.dataset.mode = mode;
    body.hidden = mode === "collapsed";
    modes.querySelectorAll("button").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.mode === mode)));
    if (body.hidden) $("#case-player")?.pause();
    requestMeasure();
  };
  modes.querySelectorAll("button").forEach(button => button.onclick = () => {
    selectedMode = button.dataset.mode;
    try { sessionStorage.setItem(memoryKey, selectedMode); } catch {}
    applyMode();
    if (selectedMode === "fixed" && compact.matches) scrollDetailSection(panel);
  });
  panel.addEventListener("show-case-video", () => {
    if (body.hidden) { selectedMode = compact.matches ? "floating" : "fixed"; applyMode(); }
  });
  let observer = null;
  if ("ResizeObserver" in window) {
    observer = new ResizeObserver(requestMeasure);
    observer.observe(body); observer.observe(modes); observer.observe($(".case-dock"));
  }
  window.addEventListener("resize", requestMeasure);
  window.addEventListener("scroll", requestMeasure, { passive: true });
  compact.addEventListener("change", applyMode);
  metadata.addEventListener("toggle", requestMeasure);
  applyMode();
  detailVideoCleanup = () => {
    observer?.disconnect(); cancelAnimationFrame(frame);
    window.removeEventListener("resize", requestMeasure); compact.removeEventListener("change", applyMode);
    window.removeEventListener("scroll", requestMeasure);
  };
}

function buildDetailNav() {
  detailNavCleanup?.();
  const sections = $$(`.detail-info:not([hidden]) section.section-card`).filter(section => section.dataset.detailLabel);
  document.querySelector(".detail-nav")?.remove();
  if (!sections.length) return;
  const nav = document.createElement("nav");
  nav.className = "detail-nav";
  nav.setAttribute("aria-label", "本页内容目录");
  nav.innerHTML = [["overview", "速览"], ["deep", "深读"]].map(([group, name]) => {
    const buttons = sections.map((section, index) => section.dataset.detailGroup === group
      ? `<button type="button" data-section="${index}" aria-controls="${esc(section.id)}">${esc(section.dataset.detailLabel)}</button>` : "").join("");
    return buttons ? `<div class="detail-nav-group"><span>${name}</span>${buttons}</div>` : "";
  }).join("");
  ($("#detail-outline") || $(".detail-layout")).append(nav);
  const buttons = $$("[data-section]", nav);
  buttons.forEach(button => button.onclick = () => scrollDetailSection(sections[Number(button.dataset.section)]));
  let frame = 0;
  const update = () => {
    frame = 0;
    if (!nav.isConnected) { detailNavCleanup?.(); return; }
    if ($("#detail-body")?.hidden) return;
    const offset = (Number.parseFloat(getComputedStyle($("#content")).getPropertyValue("--detail-sticky-top")) || 150) + 24;
    let active = 0;
    sections.forEach((section, i) => { if (section.getBoundingClientRect().top <= offset) active = i; });
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 4) active = sections.length - 1;
    buttons.forEach(button => {
      const selected = Number(button.dataset.section) === active;
      button.classList.toggle("active", selected);
      if (selected) button.setAttribute("aria-current", "location"); else button.removeAttribute("aria-current");
    });
    // The outline never scrolls the document while updating its selected section.
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(update); };
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", schedule);
  detailNavCleanup = () => { window.removeEventListener("scroll", schedule); window.removeEventListener("resize", schedule); cancelAnimationFrame(frame); };
  update();
}
function cleanupDetailWorkspace() {
  detailNavCleanup?.(); detailVideoCleanup?.(); detailWorkspaceObserver?.disconnect();
  detailNavCleanup = null; detailVideoCleanup = null; detailWorkspaceObserver = null;
}
function switchDetailTab(item) {
  const current = $("#detail-body"),
    other = $("#detail-other");
  if (!current || !other) {
    renderDetail(item);
    return;
  }
  if (state.detailTab === "analysis") state.detailTab = "script";
  if (!Object.hasOwn(detailTabs, state.detailTab)) state.detailTab = "current";
  $$(".detail-tabs [data-tab]").forEach(button => {
    const selected = button.dataset.tab === state.detailTab;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-selected", String(selected));
    button.tabIndex = selected ? 0 : -1;
  });
  if (!current.dataset.rendered) {
    renderDetail(item);
    return;
  }
  current.hidden = false;
  for (const tab of ["current", "script", "data"]) {
    const panel = $(`#detail-panel-${tab}`);
    if (panel) panel.hidden = state.detailTab !== tab;
  }
  other.hidden = state.detailTab !== "history";
  if (state.detailTab === "history" && other.dataset.caseRevision !== item.revision) {
    renderHistory(item);
    other.dataset.caseRevision = item.revision;
  }
  buildDetailNav();
  window.dispatchEvent(new Event("resize"));
}
async function setArchived(item, archived) {
  if (
    window.CaseAI?.modalOpen() ||
    state.archiveOperation ||
    state.deleteOperation ||
    state.deletion ||
    state.bulk
  )
    return;
  if (state.editor) {
    toast("请先保存或关闭编辑，再更改归档状态；编辑内容已保留。", true);
    return;
  }
  const key = `${item.case_id}:${archived}:${item.revision}`;
  if (state.archiveRequest?.key !== key)
    state.archiveRequest = { key, requestId: crypto.randomUUID() };
  state.archiveOperation = { caseId: item.case_id, archived };
  state.archiveError = null;
  mutationSequence++;
  routeSequence++;
  renderDetail(item);
  let conflict = false;
  try {
    const result = await api(
      `/api/cases/${encodeURIComponent(item.case_id)}/archive`,
      {
        method: "POST",
        body: {
          archived,
          revision: item.revision,
          request_id: state.archiveRequest.requestId,
          note: archived ? "用户归档案例" : "用户恢复案例",
        },
      },
    );
    if (
      !result.case ||
      result.case.case_id !== item.case_id ||
      result.case.archived !== archived
    )
      throw new Error("返回的案例状态不完整，请重新同步后核对。");
    const updated = result.case,
      index = state.cases.findIndex((entry) => entry.case_id === item.case_id);
    if (index !== -1)
      state.cases[index] = { ...state.cases[index], ...updated };
    else state.cases.push(updated);
    state.signature = "";
    state.digest = "";
    state.archiveRequest = null;
    if (state.selected === item.case_id) state.detail = updated;
    toast(
      result.warning ||
        (archived
          ? "已归档；可在“已归档”中随时恢复"
          : "已恢复，原表现分类保持不变"),
      !!result.warning,
    );
  } catch (error) {
    conflict = error.status === 409;
    if (conflict) state.archiveRequest = null;
    const message = conflict
      ? "案例已被更新，本次操作未完成。正在同步最新状态，请核对后重试。"
      : `${archived ? "归档" : "恢复"}未确认：${error.message}`;
    state.archiveError = { caseId: item.case_id, message };
    toast(message, true);
  } finally {
    state.archiveOperation = null;
    if (caseLocation()?.id === item.case_id) routeSequence++;
    updateNav();
    if (caseLocation()?.id === item.case_id)
      renderDetail(
        state.detail?.case_id === item.case_id ? state.detail : item,
      );
    else if (!location.hash || (window.WorkbenchShell?.legacyHash() || location.hash).startsWith("#library"))
      renderLibrary();
  }
  if (conflict) await refresh(true);
}
function renderHistory(item) {
  const events = [...item.events].reverse();
  $("#detail-other").innerHTML =
    `<div class="history-intro">${esc(item.current?.history_note || "每次保存、归档或恢复都会留下记录。下方展示当时的反馈与版本，引用案例时以“当前案例”为准。")}</div>${events
      .map(
        (ev, i) =>
          `<article class="history-item"><div class="history-date">${esc(readableDate(ev.created_at))}${i === 0 ? " · 最近一次修订" : ""}</div><div class="section-card"><h2>${esc(typeof ev.lifecycle?.archived === "boolean" ? (ev.lifecycle.archived ? "案例归档" : "案例恢复") : kindNames[ev.feedback?.kind] || "修订记录")}</h2><p>${esc(ev.feedback?.text || "")}</p>${ev.feedback?.analysis ? `<details><summary>分析与说明</summary><p>${esc(ev.feedback.analysis)}</p></details>` : ""}${
            ev.current
              ? `<details><summary>查看当时的当前版本</summary><p>${esc(ev.current.title)}<br>${esc(ev.current.conclusion)}</p>${ev.current.transcript ? `<pre>${esc(ev.current.transcript.text)}</pre>` : ""}${Object.keys(
                  ev.current.assets || {},
                )
                  .map(
                    (kind) =>
                      `<a class="history-asset" href="${mediaURL(item.case_id, kind, ev.event_id)}" target="_blank" rel="noopener">当时的${esc(assetLabels[kind] || kind)}</a>`,
                  )
                  .join("")}</details>`
              : ""
          }<details><summary>完整修订记录</summary><pre>${esc(JSON.stringify(ev, null, 2))}</pre></details></div></article>`,
      )
      .join(
        "",
      )}<article class="history-item"><div class="history-date">${esc(readableDate(item.created_at))} · 首次收录</div><div class="section-card"><h2>原始资料</h2><p>保留首次提供的内容；它可能已经被后续反馈修正。</p><details><summary>查看首次收录原文</summary><pre>${esc(item.original.text || item.original.url || "未提供")}</pre><p>${esc(item.original.source || "")}</p></details></div></article>`;
}
function markdownInline(value) {
  const pieces = String(value).split(/(`[^`]+`)/g);
  return pieces
    .map((piece) => {
      if (piece.startsWith("`") && piece.endsWith("`"))
        return `<code>${esc(piece.slice(1, -1))}</code>`;
      return esc(piece)
        .replace(
          /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
          '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>',
        )
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*\n]+)\*/g, "<em>$1</em>")
        .replace(/&lt;br\s*\/?&gt;/gi, "<br>");
    })
    .join("");
}
function renderMarkdown(text) {
  const lines = String(text || "")
      .replace(/\r\n?/g, "\n")
      .split("\n"),
    blocks = [];
  const cells = (line) =>
    line
      .trim()
      .replace(/^\|/, "")
      .replace(/\|$/, "")
      .split(/(?<!\\)\|/)
      .map((cell) => cell.trim().replace(/\\\|/g, "|"));
  const tableDivider = (line) =>
    line?.includes("|") &&
    cells(line).every((cell) => /^:?-{3,}:?$/.test(cell));
  const isStart = (index) =>
    /^(?:#{1,6}\s|\s*```|\s*~~~|\s*>|\s*[-*+]\s|\s*\d+[.)]\s|\s*(?:---+|\*\*\*+|___+)\s*$)/.test(
      lines[index] || "",
    ) || tableDivider(lines[index + 1]);
  for (let index = 0; index < lines.length;) {
    const line = lines[index];
    if (!line.trim()) {
      index++;
      continue;
    }
    const fence = line.trim().match(/^(```+|~~~+)/);
    if (fence) {
      const code = [];
      index++;
      while (index < lines.length && !lines[index].trim().startsWith(fence[1]))
        code.push(lines[index++]);
      if (index < lines.length) index++;
      blocks.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    const heading = line.match(/^(#{1,6})\s+(.+?)\s*#*$/);
    if (heading) {
      const level = Math.min(heading[1].length + 1, 6);
      blocks.push(`<h${level}>${markdownInline(heading[2])}</h${level}>`);
      index++;
      continue;
    }
    if (tableDivider(lines[index + 1])) {
      const headers = cells(line),
        rows = [];
      index += 2;
      while (
        index < lines.length &&
        lines[index].trim() &&
        lines[index].includes("|")
      )
        rows.push(cells(lines[index++]));
      blocks.push(
        `<div class="markdown-table"><table><thead><tr>${headers.map((cell) => `<th>${markdownInline(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${markdownInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`,
      );
      continue;
    }
    if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) {
      blocks.push("<hr>");
      index++;
      continue;
    }
    if (/^\s*>/.test(line)) {
      const quoted = [];
      while (index < lines.length && /^\s*>/.test(lines[index]))
        quoted.push(lines[index++].replace(/^\s*>\s?/, ""));
      blocks.push(
        `<blockquote>${quoted.map(markdownInline).join("<br>")}</blockquote>`,
      );
      continue;
    }
    const listMatch = line.match(/^\s*([-*+]|\d+[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = /^\d/.test(listMatch[1]),
        pattern = ordered ? /^\s*\d+[.)]\s+(.+)$/ : /^\s*[-*+]\s+(.+)$/,
        entries = [];
      while (index < lines.length) {
        const entry = lines[index].match(pattern);
        if (!entry) break;
        entries.push(entry[1]);
        index++;
      }
      const tag = ordered ? "ol" : "ul";
      blocks.push(
        `<${tag}>${entries.map((entry) => `<li>${markdownInline(entry)}</li>`).join("")}</${tag}>`,
      );
      continue;
    }
    const paragraph = [line];
    index++;
    while (index < lines.length && lines[index].trim() && !isStart(index))
      paragraph.push(lines[index++]);
    blocks.push(`<p>${paragraph.map(markdownInline).join("<br>")}</p>`);
  }
  return blocks.join("\n");
}
function structurePanel(structure) {
  if (!structure) return "";
  if (structure.ok)
    return `<div class="structure-check is-ok" role="status">${icon("check")}<div><strong>八章结构检查：通过</strong><p>标题、表头、版本声明和可复用骨架符合案例模板。这只检查格式，不证明转写准确或销售效果。</p></div></div>`;
  const more = structure.count - structure.errors.length;
  return `<div class="structure-check is-bad" role="alert">${icon("search")}<div><strong>八章结构检查：${structure.count} 个问题</strong><ul>${structure.errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>${more > 0 ? `<p>另有 ${more} 个问题未列出。</p>` : ""}<p>请在来源文档中补正后，再让技能同步该案例。这只检查格式，不影响案例的当前评价。</p></div></div>`;
}
function renderSourceAnalysis(item) {
  const source = item.current.source_case,
    analysis = item.source_analysis;
  const transcriptStatus = historicalTranscriptStatus(source.transcript_status);
  const metadata = `<div class="source-metadata"><div class="case-badges">${sourceBadges(item)}</div><p>历史分级与播放量沿用旧库记录，转化评价以当前案例的证据为准。</p>${source.library_id ? `<p>原案例编号：${esc(source.library_id)}</p>` : ""}${transcriptStatus ? `<p>文案状态：${esc(transcriptStatus)}，以来源记录为准。</p>` : ""}<p class="source-path">来源：${esc(analysis?.path || source.file_path || "未记录来源路径")}</p></div>`;
  if (!analysis?.exists) {
    $("#detail-source-analysis").innerHTML =
      metadata +
      `<div class="empty"><h3>原拆解文件暂不可用</h3><p>来源文件缺失或已移动。可以在修订历史中查看首次收录的完整原文。</p>${analysis?.error ? `<p>${esc(analysis.error)}</p>` : ""}<button class="secondary" id="view-source-history">查看修订历史</button></div>`;
    $("#view-source-history").onclick = () => {
      location.hash = detailTabURL(item, "history");
    };
    return;
  }
  const readingText = String(analysis.text || "").replace(
    /^\uFEFF?---[ \t]*\r?\n(?:[\s\S]*?\r?\n)?---[ \t]*(?:\r?\n|$)/,
    "",
  );
  $("#detail-source-analysis").innerHTML =
    `${metadata}${structurePanel(analysis.structure)}<section class="section-card source-analysis"><div class="section-title-row"><h2>${icon("file")}完整反推拆解</h2><div class="source-actions"><button class="subtle" id="open-source-folder">${icon("folder")}来源文件夹</button><button class="subtle" id="copy-source-analysis">${icon("copy")}复制全文</button></div></div><div class="markdown-body">${readingText.trim() ? renderMarkdown(readingText) : "<p>来源文件没有正文。</p>"}</div><details class="source-raw"><summary>查看原始拆解全文</summary><pre>${esc(analysis.text || "")}</pre></details></section>`;
  $("#copy-source-analysis").onclick = () =>
    copyText(analysis.text || "", "完整反推拆解已复制");
  $("#open-source-folder").onclick = () =>
    openFolder({ target: "source", case_id: item.case_id });
}
if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
  const closeDetailMenus = event => document.querySelectorAll(".detail-menu[open]").forEach(menu => { if (event.key === "Escape" || !menu.contains(event.target)) menu.open = false; });
  window.addEventListener("click", closeDetailMenus, true);
  window.addEventListener("keydown", event => { if (event.key === "Escape") closeDetailMenus(event); });
}
