"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const icons = {
  calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18M8 14h2m4 0h2m-8 4h2"/>',
  target: '<circle cx="11" cy="13" r="8"/><circle cx="11" cy="13" r="4"/><path d="m11 13 9-9m-5 0h5v5"/>',
  writing: '<path d="m16 3 5 5-12 12H4v-5ZM13 6l5 5M4 20h16"/>',
  library: '<path d="M3 3h4v18H3ZM9 3h4v18H9Zm6 1 3-1 3 17-3 1Z"/><path d="M3 7h4m2 0h4m3-1 3-1"/>',
  basket: '<path d="m8 3-4 5m12-5 4 5M3 8h18l-2 12H5ZM9 12v4m6-4v4"/>',
  settings: '<path d="m10 3-.6 2.4-1.8 1-2.4-.7-2 3.4L5 10.8v2.4l-1.8 1.7 2 3.4 2.4-.7 1.8 1L10 21h4l.6-2.4 1.8-1 2.4.7 2-3.4-1.8-1.7v-2.4l1.8-1.7-2-3.4-2.4.7-1.8-1L14 3Z"/><circle cx="12" cy="12" r="3"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
  spark:
    '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
  chart: '<path d="M4 3v17h17M8 15v-4m5 4V7m5 8V4"/>',
  chat: '<path d="M4 5h16v11H9l-5 4Z"/><path d="M8 9h8M8 12h5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  folder: '<path d="M3 7V5h6l2 3h10v12H3Z"/>',
  refresh:
    '<path d="M20 10a8 8 0 0 0-14-4L3 9m0-5v5h5M4 14a8 8 0 0 0 14 4l3-3m0 5v-5h-5"/>',
  monitor:
    '<rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21h8m-4-4v4"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  play: '<path d="m9 5 11 7-11 7Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
  edit: '<path d="m15 4 5 5-11 11H4v-5ZM12 7l5 5"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v12h5"/>',
  file: '<path d="M14 3H5v18h14V8ZM14 3v5h5M8 12h8m-8 4h6"/>',
  video:
    '<rect x="3" y="5" width="13" height="14" rx="2"/><path d="m16 10 5-3v10l-5-3"/>',
  archive: '<path d="M4 8h16v13H4ZM3 3h18v5H3Zm6 9h6"/>',
  restore: '<path d="M4 10a8 8 0 1 1 1 8M4 4v6h6M12 8v5l3 2"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7"/>',
  globe:
    '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>',
  layers: '<path d="m12 3 9 5-9 5-9-5 9-5Zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
  bot: '<rect x="4" y="7" width="16" height="13" rx="3"/><path d="M12 3v4M9 3h6M8 13h1m6 0h1M9 17h6"/>',
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[name] || icons.file}</svg>`;
function decorate(root = document) {
  $$("[data-icon]", root).forEach(
    (el) => (el.innerHTML = icon(el.dataset.icon)),
  );
}
const state = {
  navigation: {},
  moving: false,
  dragging: null,
  cases: [],
  signature: "",
  digest: "",
  token: "",
  paths: {},
  view: "reference-all",
  query: "",
  product: "",
  direction: "",
  variantType: "all",
  archiveCategory: "all",
  archiveStatus: "active",
  contentOrigin: "all",
  sourceType: "all",
  sort: "updated",
  sortDir: "desc",
  selected: null,
  detail: null,
  detailTab: "current",
  editor: null,
  pendingSync: false,
  polling: false,
  archiveOperation: null,
  archiveRequest: null,
  archiveError: null,
  deletion: null,
  deleteOperation: null,
  routeKind: "",
  libraryHash: "#library",
  libraryScrollY: 0,
};
const categoryNames = {
  all: "全部案例",
  positive: "转化正向参考",
  mixed: "高播放低转化",
  pending: "待验证与整理",
  legacy: "旧库资料",
  native: "本土表达参考",
  archived: "已归档",
};
const sourceTypeNames = {
  all: "全部案例类型",
  review: "审查记录",
  video_analysis: "历史视频拆解",
  structure_template: "结构模板",
  traffic_reference: "流量参考",
};
const contentOriginNames = {
  self_produced: "自制内容参考",
  overseas_creator: "达人内容参考",
};
const contentOriginDescriptions = {
  self_produced: "国内自己制作的带货视频",
  overseas_creator: "海外本土达人制作的带货视频，可包含我们的产品或其他产品",
};
const metrics = [
  ["views_display", "播放量"],
  ["product_impressions_display", "商品曝光"],
  ["product_clicks_display", "商品点击"],
  ["items_sold", "售出件数"],
  ["ctor_display", "CTOR"],
];
function salesMetrics(item) {
  return item.sales_data?.status === "matched" ? item.sales_data.metrics || {} : {};
}
function salesEvidence(item) {
  return item.sales_data?.status === "matched" ? item.sales_data.evidence || {} : {};
}
function caseIdentity(item) {
  const live = salesMetrics(item), stored = item.current?.metrics || item.metrics || {};
  return Object.fromEntries(["video_id", "creator_handle", "published_title"].map(key => [key, live[key] || stored[key] || ""]));
}
function salesStatusLabel(item) {
  return { matched: "销售数据表", not_found: "销售数据表未匹配到此视频", missing_video_id: "未填写视频 ID", unavailable: "销售数据表暂不可用" }[item.sales_data?.status] || "销售数据表暂不可用";
}
function salesDataURL(item) {
  const id = caseIdentity(item).video_id;
  return "#videos" + (id ? "?video=" + encodeURIComponent(id) : "");
}
const assetLabels = {
  video: "当前有效视频",
  screenshot: "表现数据截图",
  transcript: "文案来源文件",
  contact_sheet: "画面缩略图",
  source_manifest: "来源清单",
};
const polarityNames = {
  unknown: "未评价",
  positive: "正向",
  negative: "负向",
  mixed: "混合表现",
};
const kindNames = {
  editorial: "内容判断",
  performance: "表现反馈",
  fact: "事实补充",
  preference: "使用偏好",
  review_correction: "审查纠错",
};
const videoAccept = ".mp4,.webm,.mov,.m4v";
const maxVideoBytes = 2 * 1024 * 1024 * 1024;
state.selection = new Set();
state.layout = "cards";
state.bulk = null;
state.bulkOperation = false;
try {
  if (localStorage.getItem("case-library-layout") === "list")
    state.layout = "list";
} catch {
  /* The default remains available when browser storage is disabled. */
}
state.nativeMode = "videos";
try {
  if (localStorage.getItem("native-library-mode") === "entries")
    state.nativeMode = "entries";
} catch {
  /* The default remains available when browser storage is disabled. */
}
try {
  state.libraryHash =
    sessionStorage.getItem("case-library-last-url") || "#library";
  state.libraryScrollY =
    Number(sessionStorage.getItem("case-library-scroll-y")) || 0;
} catch {
  /* Navigation still works without browser storage. */
}
let toastTimer,
  routeSequence = 0,
  mutationSequence = 0;

function caseLocation(hash = location.hash) {
  hash = window.WorkbenchShell?.legacyHash(hash) || hash;
  const match = /^#case\/(PVR-[0-9a-f]{12})(?:\/(history|analysis|script|data))?$/.exec(
    hash,
  );
  return match ? { id: match[1], tab: match[2] === "analysis" ? "script" : match[2] || "current" } : null;
}
function libraryHash() {
  const params = new URLSearchParams();
  if (state.view !== "reference-all") params.set("view", state.view);
  if (state.query) params.set("q", state.query);
  if (state.product) params.set("product", state.product);
  if (state.direction) params.set("direction", state.direction);
  if (state.variantType !== "all") params.set("variant", state.variantType);
  if (state.archiveCategory !== "all")
    params.set("category", state.archiveCategory);
  if (state.sourceType !== "all") params.set("source", state.sourceType);
  if (state.archiveStatus !== "active") params.set("archive", state.archiveStatus);
  if (state.contentOrigin !== "all") params.set("origin", state.contentOrigin);
  if (state.sort !== "updated") params.set("sort", state.sort);
  if (state.sortDir !== "desc") params.set("dir", state.sortDir);
  const hash = "#library" + (params.size ? "?" + params.toString() : "");
  return window.WorkbenchShell?.url(hash) || hash;
}
function readLibraryHash(hash = location.hash) {
  hash = window.WorkbenchShell?.legacyHash(hash) || hash;
  if (hash === "#native") hash = "#library?view=native";
  const params = new URLSearchParams(hash.split("?")[1] || "");
  const view = params.get("view") || "reference-all";
  state.view = isNavigationView(view) ? view : "reference-all";
  state.query = params.get("q") || "";
  state.product = window.WorkbenchShell?.product() ?? params.get("product") ?? "";
  state.direction = params.get("direction") || "";
  state.variantType = ["direction", "execution"].includes(params.get("variant")) ? params.get("variant") : "all";
  state.archiveCategory = ["all", "positive", "mixed", "pending"].includes(
    params.get("category"),
  )
    ? params.get("category")
    : "all";
  state.sourceType = Object.hasOwn(sourceTypeNames, params.get("source"))
    ? params.get("source")
    : "all";
  state.archiveStatus = ["archived", "all"].includes(params.get("archive"))
    ? params.get("archive") : navigationContent(state.view) === "archived" ? "archived" : "active";
  state.contentOrigin = Object.hasOwn(contentOriginNames, params.get("origin"))
    ? params.get("origin") : "all";
  state.sort = ["updated", "views", "ctor", "sold"].includes(params.get("sort"))
    ? params.get("sort")
    : "updated";
  state.sortDir = params.get("dir") === "asc" ? "asc" : "desc";
  window.LibraryViews?.applyRouteFilters();
}
function rememberLibraryPosition() {
  state.libraryHash = libraryHash();
  state.libraryScrollY = window.scrollY;
  try {
    sessionStorage.setItem("case-library-last-url", state.libraryHash);
    sessionStorage.setItem(
      "case-library-scroll-y",
      String(state.libraryScrollY),
    );
  } catch {
    /* This only affects restoration after reload. */
  }
}
function writeLibraryHash() {
  state.libraryHash = libraryHash();
  history.replaceState(null, "", state.libraryHash);
  try {
    sessionStorage.setItem("case-library-last-url", state.libraryHash);
  } catch {}
}
function skeletonMarkup(count = 3) {
  return `<div class="skeleton-page" role="status" aria-label="正在读取页面"><div class="skeleton-heading"></div><div class="skeleton-line"></div><div class="skeleton-grid">${Array.from({ length: count }, () => '<div class="skeleton-card"></div>').join("")}</div></div>`;
}

const pendingApiReads = new Map();
async function requestApi(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method === "POST") headers["X-Workbench-Token"] = state.token;
  if (
    options.body &&
    typeof options.body !== "string" &&
    !(options.body instanceof File)
  ) {
    options = { ...options, body: JSON.stringify(options.body) };
    headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, { ...options, headers });
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.error || "操作失败");
    error.status = response.status;
    throw error;
  }
  return result;
}
async function api(path, options = {}) {
  // Share only simultaneous plain reads. Signals and custom request options
  // retain their own lifecycle, and a write starts a fresh generation of reads.
  if (Object.keys(options).length) {
    if (!["GET", "HEAD"].includes(options.method || "GET")) pendingApiReads.clear();
    return requestApi(path, options);
  }
  let request = pendingApiReads.get(path);
  if (!request) {
    request = requestApi(path);
    pendingApiReads.set(path, request);
    void request.finally(() => {
      if (pendingApiReads.get(path) === request) pendingApiReads.delete(path);
    }).catch(() => {});
  }
  const result = await request;
  // Page modules decorate and filter their data; each reader owns its copy.
  return structuredClone(result);
}
function toast(message, warning = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("warning", warning);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), warning ? 9500 : 3500);
}
function showError(message) {
  $("#error-banner").textContent = message;
  $("#error-banner").hidden = !message;
}
function syncDetail(text, problem = false) {
  const el = $("#sync-detail");
  if (el) {
    el.textContent = text;
    el.classList.toggle("problem", problem);
  }
  $("#sync-button")?.classList.toggle("attention", problem);
  const connection = $(".connection");
  if (connection) { connection.title = `${$("#connection-text").textContent} · ${text} · 视频、文案与修订记录保存在本机`; connection.tabIndex = 0; }
}
function timeText(date) {
  return date.toLocaleTimeString("zh-CN", { hour12: false });
}
// Every state read re-validates the index against the case records, so a
// successful read means they agree; a failed one says which side is at fault.
function markChecked() {
  state.lastChecked = new Date();
  syncDetail(`索引与记录一致 · ${timeText(state.lastChecked)} 检查`);
}
function online(ok) {
  $("#connection-dot").classList.toggle("offline", !ok);
  $("#connection-text").textContent = ok ? "已连接本地案例库" : "连接暂时中断";
  $(".connection").title = `${$("#connection-text").textContent} · ${$("#sync-detail").textContent}`;
}
function group(item) {
  const manual = state.navigation[item.case_id];
  if (manual && !String(manual).startsWith("column:"))
    return ["positive", "mixed", "pending"].includes(manual)
      ? manual
      : "manual";
  const cur = item.current;
  if (!cur) return "pending";
  if (cur.category === "转化正向参考") return "positive";
  if (cur.category.includes("高播放低转化")) return "mixed";
  if (cur.category === "待验证与整理" || cur.category === "待验证")
    return "pending";
  if (
    (cur.assessments || []).some(
      (x) => x.dimension === "conversion" && x.polarity === "positive",
    )
  )
    return "positive";
  return "pending";
}
function readableDate(value) {
  const m = String(value).match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
  const normalized = m
    ? `${m[1]}-${m[2]}-${m[3]}T${m[4]}:${m[5]}:${m[6]}Z`
    : /^\d{4}-\d{2}-\d{2}T/.test(String(value))
      ? String(value)
      : "";
  const date = normalized ? new Date(normalized) : null;
  if (!date || Number.isNaN(date.getTime())) return value || "未提供";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
function mediaURL(id, kind, event) {
  return `/media/${encodeURIComponent(id)}/${kind}${event ? "?event=" + encodeURIComponent(event) : ""}`;
}
function badge(item) {
  return `<span class="badge ${group(item)}">${esc(item.current?.category || "待整理")}</span>`;
}
function archiveBadge(item) {
  return `<span class="badge archive-status ${item.archived ? "archived" : ""}">${item.archived ? "已归档" : "正常收录"}</span>`;
}
function sourceType(item) {
  return item.current?.source_case?.case_type || "review";
}
function productFilterGroup(item) {
  const product = item.product || "",
    title = item.current?.title || item.title || "";
  const skus = (text) => [
    ...new Set(
      (String(text).match(/(?<![A-Z0-9])\d{3}[A-Z](?![A-Z0-9])/gi) || []).map(
        (value) => value.toUpperCase(),
      ),
    ),
  ];
  const productSkus = skus(product),
    models = productSkus.length ? productSkus : skus(title);
  if (models.length !== 1) return product;
  const sourcePath = String(item.current?.source_case?.file_path || "").replace(
    /\\/g,
    "/",
  );
  const known129NSource =
    models[0] === "129N" &&
    /\/(?:tiktok-video-script-voiceover\/examples|product-video-script-review\/sources\/video-cases)\/own_products\/129N_[^/]+\.md$/i.test(
      sourcePath,
    );
  return /\bSHARPAL\b/i.test(`${product} ${title}`) || known129NSource
    ? `SHARPAL ${models[0]}`
    : product;
}
// Keep the translation tied to the exact source text in the current snapshot.
const transcriptComparisonOpen = new Map();
function currentTranslation(item) {
  const translation = item.current?.translation_zh;
  if (!translation || typeof translation !== "object") return null;
  const { source_text: source, text: chinese, channel } = translation;
  if (
    typeof source !== "string" ||
    typeof chinese !== "string" ||
    !source.trim() ||
    !chinese.trim() ||
    !["transcript", "on_screen", "historical_excerpt"].includes(channel)
  ) return null;
  if (channel === "transcript" && source !== item.current?.transcript?.text)
    return null;
  if (channel === "on_screen" && item.current?.transcript?.text &&
      !item.current.transcript.text.includes(source))
    return null;
  return translation;
}
function translationChannelLabel(channel) {
  return {
    transcript: "完整文案",
    on_screen: "屏显文字",
    historical_excerpt: "历史文字片段",
  }[channel] || "原文";
}
function transcriptDisplay(item) {
  const translation = currentTranslation(item);
  if (translation && translation.channel !== "transcript")
    return {
      heading: translationChannelLabel(translation.channel),
      text: translation.source_text,
      status: `${translationChannelLabel(translation.channel)}来源；当前未收录完整口播逐字稿。`,
    };
  return {
    heading: "完整文案",
    text: item.current?.transcript?.text || "",
    status: "",
  };
}
function transcriptComparisonKey(item) {
  const translation = currentTranslation(item);
  return `${item.case_id}:${item.current_event_id || ""}:${transcriptDisplay(item).text}:${translation?.text || ""}`;
}
function pairedTranslationLines(original, chinese) {
  const lines = (value) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const originals = lines(original), translations = lines(chinese);
  if (originals.length > 1 && originals.length === translations.length)
    return originals.map((line, index) => ({ original: line, chinese: translations[index] }));
  return [{ original: original.trim(), chinese: chinese.trim() }];
}
function transcriptComparison(item) {
  const saved = currentTranslation(item);
  if (saved) return pairedTranslationLines(saved.source_text, saved.text);
  const text = item.current?.transcript?.text || "";
  const source = item.source_analysis?.text || "";
  const normalize = (value) => value.replace(/\s+/g, " ").trim();
  const timeKey = (value) => {
    const parts = value.trim().split(/\s*[-–—]\s*/);
    const seconds = (part) => {
      if (!/^\d+(?::\d{1,2}){0,2}(?:[.,]\d+)?s?$/.test(part)) return null;
      return part
        .replace(/s$/, "")
        .replace(",", ".")
        .split(":")
        .reduce((total, unit) => total * 60 + Number(unit), 0);
    };
    const range = parts.map(seconds);
    return range.length === 2 && range.every((value) => value !== null)
      ? range.join(":")
      : null;
  };
  const timedLine = (line) => {
    const match = line.match(/^\s*\[([^\]]+)\]\s*(\S[\s\S]*)$/);
    const key = match && timeKey(match[1]);
    return key ? { key, text: normalize(match[2]) } : null;
  };
  const currentLines = text.split(/\r?\n/).filter((line) => line.trim());
  if (!currentLines.length) return [];
  const currentTimed = currentLines.map(timedLine);
  const legacy = currentTimed.every(Boolean);
  if (!legacy && currentTimed.some(Boolean)) return [];
  const currentEnglish = normalize(
    legacy ? currentTimed.map((row) => row.text).join(" ") : text,
  );
  const blocks = [...source.matchAll(/```[^\n]*\n([\s\S]*?)```/g)];
  const block = blocks.find((match) => {
    const rows = match[1]
      .split(/\r?\n/)
      .filter((line) => line.trim())
      .map(timedLine);
    if (!rows.length || rows.some((row) => !row)) return false;
    // Only whitespace and timestamp wrappers may differ; every word and punctuation mark must match.
    return (
      normalize(rows.map((row) => row.text).join(" ")) === currentEnglish &&
      (!legacy || normalize(match[1]) === normalize(text))
    );
  });
  if (!block) return [];
  const segments = block[1]
    .split(/\r?\n/)
    .filter((line) => line.trim())
    .map(timedLine);
  // The translation must belong to the same source section as the original.
  const following = source
    .slice(block.index + block[0].length)
    .split(/^(?:#{1,6}\s|\s*```)/m)[0];
  const lines = following.split(/\r?\n/);
  const cells = (line) =>
    line
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((value) => value.trim());
  const header = lines.findIndex(
    (line) =>
      /^\s*\|/.test(line) &&
      cells(line).some((value) => /中文/.test(value)) &&
      cells(line).some((value) => /时间/.test(value)),
  );
  if (header < 0) return [];
  const columns = cells(lines[header]);
  const timeIndex = columns.findIndex((value) => /时间/.test(value));
  const zhIndex = columns.findIndex((value) => /中文/.test(value));
  const translations = new Map();
  for (const line of lines.slice(header + 2)) {
    if (!/^\s*\|/.test(line)) break;
    const row = cells(line),
      key = timeKey(row[timeIndex] || "");
    if (key && row[zhIndex]) {
      if (translations.has(key) && translations.get(key) !== row[zhIndex])
        return [];
      translations.set(key, row[zhIndex]);
    }
  }
  if (legacy)
    return currentLines.map((line, index) => ({
      original: line,
      chinese: translations.get(currentTimed[index].key) || "",
    }));
  const sentences = [
    ...new Intl.Segmenter("en", { granularity: "sentence" }).segment(
      currentEnglish,
    ),
  ]
    .map((row) => row.segment.trim())
    .filter(Boolean);
  const result = [];
  let position = 0;
  for (const sentence of sentences) {
    const matched = [];
    let english = "";
    while (position < segments.length && english !== sentence) {
      const segment = segments[position++];
      english = english ? `${english} ${segment.text}` : segment.text;
      // A segment crossing a sentence boundary cannot be split into Chinese reliably.
      if (!sentence.startsWith(english)) return [];
      matched.push(translations.get(segment.key) || "");
    }
    if (english !== sentence) return [];
    result.push({
      original: sentence,
      chinese: matched.every(Boolean) ? matched.join("") : "",
    });
  }
  return position === segments.length ? result : [];
}
function renderTranscriptComparison(item) {
  const button = $("#toggle-transcript-zh"),
    panel = $("#transcript-comparison");
  if (!button || !panel) return;
  const key = transcriptComparisonKey(item);
  const expanded = transcriptComparisonOpen.get(key) === true;
  button.setAttribute("aria-expanded", String(expanded));
  button.classList.toggle("is-active", expanded);
  $("#transcript-original").hidden = expanded;
  panel.hidden = !expanded;
  if (!expanded) {
    window.TranscriptSync?.refresh(item);
    return;
  }
  const rows = transcriptComparison(item);
  panel.innerHTML = rows.some((row) => row.chinese)
    ? rows
        .map(
          (row) =>
            `<div class="transcript-pair"><div class="transcript-en">${esc(row.original)}</div><div class="transcript-zh" lang="zh-CN">${esc(row.chinese || "此句暂无中文对照。")}</div></div>`,
        )
        .join("")
    : `<div class="transcript">${esc(transcriptDisplay(item).text || "暂未整理当前有效文案。")}</div><p class="context-note">当前原文暂无匹配的中文对照。</p>`;
  window.TranscriptSync?.refresh(item);
}
function historicalTranscriptStatus(value) {
  return (
    { verified: "历史已核对", inferred: "推测或低置信", none: "未提供" }[
      value
    ] ||
    value ||
    "未提供"
  );
}
// Publish and data-cutoff dates are calendar days (YYYY-MM-DD), so compare
// them in UTC to keep the day count independent of the viewer's time zone.
const checkpointDays = [1, 3, 7, 14, 30];
function calendarDay(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  return match ? Date.UTC(+match[1], +match[2] - 1, +match[3]) / 86400000 : null;
}
function addCalendarDays(value, days) {
  const day = calendarDay(value);
  return day === null ? "" : new Date((day + days) * 86400000).toISOString().slice(0, 10);
}
function daysSincePublish(current) {
  const published = calendarDay(current?.publish?.published_on),
    asOf = calendarDay(current?.evidence?.as_of);
  return published === null || asOf === null ? null : asOf - published;
}
function checkpointLabel(current) {
  const asOf = current?.evidence?.as_of;
  if (!asOf) return "";
  const days = daysSincePublish(current);
  return `数据截至 ${asOf}${days === null ? "" : ` · 发布后第 ${days} 天`}`;
}
function contentOriginLabel(item) {
  const origin = item.current?.content_origin;
  return Object.hasOwn(contentOriginNames, origin) ? contentOriginNames[origin] : "内容来源待确认";
}
function sourceBadges(item, showUnknown = false) {
  const origin = item.current?.content_origin;
  const known = Object.hasOwn(contentOriginNames, origin);
  const originBadge = known || showUnknown
    ? `<span class="badge content-origin" title="${esc(known ? contentOriginDescriptions[origin] : "可在编辑案例中选择视频由谁制作")}">${esc(contentOriginLabel(item))}</span>` : "";
  const nativeBadge = item.current?.native_reference
    ? '<span class="badge native-content-badge" title="此案例已收录本土表达与场景分析">含本土表达</span>' : "";
  const source = item.current?.source_case;
  if (!source) return originBadge + nativeBadge;
  const tier = String(source.tier || "").replace(/^历史\s*/, "");
  return `${originBadge}${nativeBadge}<span class="badge source-type">${esc(sourceTypeNames[sourceType(item)] || "旧库资料")}</span>${tier ? `<span class="badge source-tier">历史${esc(tier)}</span>` : ""}`;
}
function inNavigation(item, view) {
  return navigationMatches(item, view);
}
function scopedCases() {
  return state.cases.filter((item) => inNavigation(item, state.view) && (!window.WorkbenchShell || window.WorkbenchShell.matchesProduct(item)));
}
function shortTitle(item) {
  return (item.current?.title || item.title || "未命名案例").split("｜").at(-1);
}
function displayValue(value) {
  return value === undefined || value === null || value === ""
    ? '<span class="missing-value">—</span>'
    : esc(value);
}
function numeric(value) {
  const s = String(value || "").replace(/,/g, "");
  const n = parseFloat(s);
  return Number.isFinite(n)
    ? n * (/K/i.test(s) ? 1000 : /M/i.test(s) ? 1000000 : 1)
    : -1;
}
function updateNav() {
  const hash = window.WorkbenchShell?.legacyHash() || location.hash;
  $$("[data-view]").forEach((el) => {
    const view = el.dataset.view, count = state.cases.filter(item => inNavigation(item, view) && (!window.WorkbenchShell || window.WorkbenchShell.matchesProduct(item))).length;
    const counter = el.querySelector(".nav-count");
    if (counter) counter.textContent = count;
    el.title = `${navigationLabel(view)} · ${count} 条案例`;
    el.classList.toggle("active", (hash.startsWith("#library") || hash.startsWith("#case/")) && view === state.view);
  });
  $("#files-nav").classList.toggle("active", hash === "#files");
  $("#videos-nav")?.classList.toggle("active", hash.startsWith("#videos"));
  $("#directions-nav")?.classList.toggle("active", hash.startsWith("#directions"));
  $("#reviews-nav")?.classList.toggle("active", hash.startsWith("#reviews"));
  $("#ai-nav")?.classList.toggle(
    "active",
    hash === "#ai" || hash.startsWith("#ai/"),
  );
  $("#ai-nav").title = "写稿";
  $("#files-nav").title = "设置";
  window.WorkbenchShell?.update();
}
function clearDeletedDetail(identifier) {
  if (window.CaseInlineEditor?.handleDeleted(identifier)) return;
  routeSequence++;
  state.selected = null;
  state.detail = null;
  state.archiveError = null;
  state.archiveRequest = null;
  state.view = navigationViewForContent("archived");
  state.query = "";
  state.product = "";
  state.archiveCategory = "all";
  state.archiveStatus = "archived";
  state.contentOrigin = "all";
  state.sourceType = "all";
  state.detailTab = "current";
  if (state.editor?.item?.case_id === identifier) {
    state.editor.deleted = true;
    state.editor.uploadRequest?.abort();
    $("#save-button").disabled = true;
    $("#save-hint").textContent =
      "此案例已在其他页面删除。未保存文字仍留在当前编辑框供复制，关闭后清空；不能再保存到此案例。";
    $("#save-hint").classList.add("save-error");
    $$("[data-pick], [data-upload]", $("#edit-form")).forEach(
      (control) => (control.disabled = true),
    );
  } else if (!state.editor) $("#editor-fields").innerHTML = "";
  if (state.deletion?.caseId === identifier && !state.deletion.submitting) {
    state.deletion.plan = null;
    state.deletion.loading = false;
    state.deletion.sequence++;
    state.deletion.error =
      "此案例已在其他页面删除。当前窗口不会再次提交删除，可关闭并核对本地文件的清理情况。";
    renderDeleteDialog();
  }
  location.hash = libraryHash();
  updateNav();
  renderLibrary();
  toast("当前案例已不在库中，已清空详情并返回已归档。", true);
}
function promptCaseUpdate() {
  if ($("#case-update-banner")) return;
  const heading = $(".detail-heading");
  if (!heading) return;
  const banner = document.createElement("div");
  banner.id = "case-update-banner";
  banner.className = "banner update";
  banner.setAttribute("role", "status");
  banner.innerHTML =
    '<span>这条案例有更新，当前视频和展开内容已保留。</span><button type="button" class="secondary">点击刷新</button>';
  heading.before(banner);
  banner.querySelector("button").onclick = () => {
    void route(true, true);
  };
}
async function refresh(quiet = false) {
  if (quiet && (window.CaseDirectionPicker?.hasVideoEdits?.() || window.CaseScripts?.modalOpen?.() || window.CaseProduction?.modalOpen?.() || window.MaterialBasket?.modalOpen?.())) return;
  if (
    state.moving ||
    state.dragging ||
    state.polling ||
    state.archiveOperation ||
    state.deleteOperation ||
    state.bulkOperation
  )
    return;
  state.polling = true;
  const mutation = mutationSequence;
  try {
    // Background polls send the last digest; an unchanged library answers with a tiny reply.
    const since = quiet && state.digest ? "?since=" + state.digest : "";
    const data = await api("/api/state" + since);
    if (mutation !== mutationSequence) return;
    if (data.unchanged) {
      state.token = data.token;
      online(true);
      markChecked();
      showError("");
      return;
    }
    const changed = state.signature !== data.signature;
    Object.assign(state, {
      navigation: data.navigation || {},
      cases: data.cases,
      paths: data.paths,
      token: data.token,
      signature: data.signature,
      digest: data.digest || "",
    });
    online(true);
    markChecked();
    showError("");
    updateNav();
    const openedId = caseLocation()?.id || null;
    if (openedId && !data.cases.some((item) => item.case_id === openedId)) {
      clearDeletedDetail(openedId);
      return;
    }
    if (changed || !quiet) {
      if (state.editor || state.deletion || state.bulk || window.CaseDirections?.modalOpen?.() || window.CaseScripts?.modalOpen?.() || window.CaseProduction?.modalOpen?.() || window.CaseAI?.modalOpen?.())
        state.pendingSync = true;
      else if (
        quiet &&
        openedId &&
        state.detail?.case_id === openedId &&
        $(".detail-heading")
      ) {
        const summary = data.cases.find((item) => item.case_id === openedId);
        if (summary.revision !== state.detail.revision || JSON.stringify(summary.resolved_direction) !== JSON.stringify(state.detail.resolved_direction)) promptCaseUpdate();
        else if (JSON.stringify(summary.sales_data) !== JSON.stringify(state.detail.sales_data))
          refreshDetailSalesData(summary);
      } else if (
        quiet &&
        (state.routeKind === "library") &&
        $("#case-results")
      )
        refreshLibraryData();
      else await route(true, true);
    }
  } catch (error) {
    if (mutation !== mutationSequence) return;
    const inconsistent = /index|rebuild|stale|inconsistent/i.test(error.message);
    online(inconsistent);
    syncDetail(
      inconsistent
        ? "索引与案例记录不一致，请点“重新同步”修复"
        : `无法连接本地服务 · 最近成功检查 ${state.lastChecked ? timeText(state.lastChecked) : "无"}`,
      true,
    );
    showError(
      `${error.message}。可点击“重新同步”更新目录；服务未启动时，请双击“打开案例工作台”。`,
    );
  } finally {
    state.polling = false;
  }
}
function renderFiles() {
  $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 本地文件";
  const rows = [
    ["records", "正式案例记录", "保存原始资料、当前版本和每次修订。"],
    ["evidence", "视频与证据附件", "视频、截图和转写文件的保存位置。"],
    ["library", "中文阅读目录", "每次在工作台保存后同步更新，便于直接阅读。"],
  ];
  $("#content").innerHTML =
    `<div class="page-heading"><div><span class="eyebrow">CONNECTED TO YOUR FILES</span><h1>本地文件</h1><p>工作台与现有案例库使用同一份记录，修改可以相互看见。</p></div></div><section class="section-card">${rows.map(([key, label, note]) => `<div class="file-row"><div><h3>${label}</h3><p>${note}</p><code class="file-path">${esc(state.paths[key])}</code></div><div class="file-actions"><button class="subtle" data-copy-path="${key}">${icon("copy")}复制路径</button><button class="secondary" data-open-path="${key}">${icon("folder")}打开文件夹</button></div></div>`).join("")}</section><section class="section-card"><h2>日常怎么用</h2><ul class="bullet-list"><li>收录案例：选择视频，补上文案与结论，填写视频 ID 后自动关联销售数据。</li><li>补充反馈：在案例详情中编辑，保存会追加修订并更新阅读目录。</li><li>本地更新：通过原有收录流程新增或修订后，工作台每 5 秒检查一次；编辑中的内容会保留。</li><li>视频导入：拖入视频或选择文件会立即复制到案例库；保存后归入该案例的视频文件夹，原文件保留。</li><li>视频路径：手动填写完整路径后，保存也会复制一份；已保存的副本不受原文件移动或删除影响。</li><li>其他附件：填写路径可直接关联，选择文件则会复制到附件目录。</li></ul></section>`;
  $$("[data-copy-path]").forEach(
    (el) =>
      (el.onclick = () =>
        copyText(state.paths[el.dataset.copyPath], "路径已复制")),
  );
  $$("[data-open-path]").forEach(
    (el) => (el.onclick = () => openFolder({ target: el.dataset.openPath })),
  );
}
const routeTable = {
  library: async () => {
    state.selected = null;
    state.detail = null;
    renderLibrary();
    state.libraryHash = libraryHash();
  },
  case: async ({ n, caseRoute, force }) => {
    const { id, tab } = caseRoute;
    state.selected = id;
    if (!force && state.detail?.case_id === id && $(".detail-heading")) {
      state.detailTab = tab;
      switchDetailTab(state.detail);
      state.routeKind = "case";
      return;
    }
    state.detailTab = tab;
    try {
      const detail = await api("/api/cases/" + encodeURIComponent(id));
      if (n !== routeSequence) return;
      state.detail = detail;
      renderDetail(detail);
    } catch (error) {
      if (n !== routeSequence) return;
      if (error.status === 400 || error.status === 404) {
        try {
          const data = await api("/api/state");
          if (n !== routeSequence) return;
          if (
            Array.isArray(data.cases) &&
            !data.cases.some((item) => item.case_id === id)
          ) {
            Object.assign(state, {
              cases: data.cases,
              paths: data.paths,
              token: data.token,
              signature: data.signature,
              digest: data.digest || "",
            });
            online(true);
            showError("");
            clearDeletedDetail(id);
            return;
          }
        } catch {
          /* A failed read does not establish that the case was deleted. */
        }
      }
      if (n === routeSequence) showError(error.message);
    }
    state.routeKind = "case";

  },
  home: async () => window.WorkbenchHome?.route(),
  ai: async ({ keepScroll }) => window.CaseAI?.route(keepScroll),
  scripts: async ({ keepScroll }) => window.CaseScripts?.route(keepScroll),
  production: async ({ keepScroll }) => window.CaseProduction?.route(keepScroll),
  videos: async () => window.CasePerformance?.route(),
  directions: async () => window.CaseDirections?.route(),
  reviews: async () => window.CaseReviews?.route(),
  settings: async () => window.WorkbenchSettings?.render(),
};
async function route(keepScroll = false, force = false) {
  const n = ++routeSequence, previous = state.routeKind;
  const previousLibraryHash = state.libraryHash;
  const incoming = window.WorkbenchShell.parse();
  if (window.CaseScripts?.beforeNavigate && !window.CaseScripts.beforeNavigate(location.hash)) {
    window.WorkbenchShell.update();
    return;
  }
  if (window.CaseInlineEditor && !window.CaseInlineEditor.beforeNavigate(incoming, force)) {
    window.WorkbenchShell.update();
    return;
  }
  if (window.CaseDirections?.beforeNavigate && !window.CaseDirections.beforeNavigate(incoming, force)) {
    window.WorkbenchShell.update();
    return;
  }
  if (previous === "library" && incoming.page !== "library") {
    clearTimeout(searchTimer);
    cardThumbnailObserver?.disconnect();
    rememberLibraryPosition();
  }
  window.WorkbenchShell.beginNavigation(incoming);
  if (previous === "case" && incoming.page !== "case") cleanupDetailWorkspace();
  const parsed = window.WorkbenchShell.canonicalize();
  let page = parsed.page;
  const hash = window.WorkbenchShell.legacyHash();
  const caseRoute = caseLocation(hash);
  if (page === "case" && !caseRoute) {
    page = "library";
    history.replaceState(null, "", window.WorkbenchShell.url("#library"));
  }
  if (page === "library") readLibraryHash();
  state.routeKind = page;
  if (page !== "case") { state.selected = null; state.detail = null; }
  updateNav();
  await routeTable[page]({ n, caseRoute, keepScroll, force });
  if (n !== routeSequence) return;
  updateNav();
  const restoredScroll = window.WorkbenchShell.finishNavigation();
  if (restoredScroll !== null) {
    requestAnimationFrame(() => { if (n === routeSequence) window.scrollTo(0, restoredScroll); });
  } else if (page === "library" && previous === "case" && window.WorkbenchShell.url(previousLibraryHash) === location.hash) {
    requestAnimationFrame(() => { if (n === routeSequence) window.scrollTo(0, state.libraryScrollY); });
  } else if (!keepScroll) window.scrollTo(0, 0);
}

async function copyText(text, message) {
  try {
    await navigator.clipboard.writeText(text);
    toast(message);
  } catch {
    toast("未能复制，请手动选择文字复制。", true);
  }
}
async function openFolder(body) {
  try {
    await api("/api/open", { method: "POST", body });
    toast("已打开本地文件夹");
  } catch (error) {
    toast(error.message, true);
  }
}
async function moveCaseToView(id, view, via = "菜单") {
  if (
    state.moving ||
    state.editor ||
    state.bulk ||
    state.bulkOperation ||
    state.archiveOperation ||
    state.deleteOperation
  ) {
    toast("请先完成当前操作。", true);
    return;
  }
  const item = state.cases.find((entry) => entry.case_id === id);
  if (!item || !navigationMoveChoices().some(column => column.id === view)) {
    toast("这个栏目按内容自动筛选，请在导航设置中调整范围。", true);
    return;
  }
  const target = navigationContent(view), label = navigationLabel(view);
  state.moving = true;
  mutationSequence++;
  let restored = false;
  try {
    let detail = await api(`/api/cases/${encodeURIComponent(id)}`);
    if (target === "archived" || detail.archived) {
      const result = await api(`/api/cases/${encodeURIComponent(id)}/archive`, {
        method: "POST",
        body: {
          request_id: crypto.randomUUID(),
          revision: detail.revision,
          archived: target === "archived",
          note: `用户通过${via}移动到“${label}”`,
        },
      });
      detail = result.case;
      restored = target !== "archived";
    }
    if (target !== "archived") {
      const result = await api("/api/navigation/move", {
        method: "POST",
        body: {
          case_id: id,
          target: `column:${view}`,
          revision: detail.revision,
          previous: state.navigation[id] ?? null,
        },
      });
      state.navigation[id] = result.target;
    }
    toast(`已移到“${label}”`);
  } catch (error) {
    toast(
      `${restored ? "案例已恢复，但栏目未调整：" : "移动未完成："}${error.message}`,
      true,
    );
  } finally {
    state.moving = false;
    mutationSequence++;
    await refresh();
  }
}

decorate();
$("#files-nav").onclick = () => (location.hash = "#files");
$("#videos-nav").onclick = () => (location.hash = "#videos");
$("#directions-nav").onclick = () => (location.hash = "#directions");
$("#reviews-nav").onclick = () => (location.hash = "#reviews");
$("#add-top").onclick = () => openEditor(null);
$("#import-video-top").onclick = () => openVideoPicker(null);
$("#sync-button").onclick = async () => {
  const button = $("#sync-button");
  button.disabled = true;
  try {
    await api("/api/sync", { method: "POST", body: {} });
    await refresh();
    toast("案例和阅读目录已同步");
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
};
$("#edit-form").addEventListener("input", (event) => {
  if (state.editor) state.editor.dirty = true;
  if (event.target.name === "asset_video") {
    state.editor.videoUpload = null;
    renderVideoImportStatus();
  }
});
$("#edit-form").addEventListener("change", () => {
  if (state.editor) state.editor.dirty = true;
});
$("#edit-form").addEventListener("submit", saveEditor);
$("#close-editor").onclick = $("#cancel-editor").onclick = requestClose;
$("#edit-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  requestClose();
});
$("#discard-edits").onclick = closeEditor;
$("#keep-editing").onclick = () => $("#discard-dialog").close();
$("#delete-form").addEventListener("submit", submitDelete);
$("#close-delete").onclick = $("#cancel-delete").onclick = closeDelete;
$("#delete-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDelete();
});
$("#bulk-form").addEventListener("submit", submitBulk);
$("#close-bulk").onclick = $("#cancel-bulk").onclick = closeBulk;
$("#bulk-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeBulk();
});
window.addEventListener("hashchange", () => {
  void route();
});
document.addEventListener("keydown", (event) => {
  if (event.altKey || event.ctrlKey || event.metaKey || event.isComposing)
    return;
  const target = event.target;
  if (target.closest?.('input,textarea,select,[contenteditable="true"]')) {
    if (event.key === "Escape" && target === $("#case-search")) target.blur();
    return;
  }
  if ($$("dialog[open]").length) return;
  if (event.key === "Escape") {
    const destination = ["home", "library"].includes(state.routeKind) ? null : window.WorkbenchShell.parentURL();
    if (destination) {
      event.preventDefault();
      location.hash = destination;
    }
    return;
  }
  const direction =
    event.key.toLowerCase() === "j"
      ? 1
      : event.key.toLowerCase() === "k"
        ? -1
        : 0;
  const current = caseLocation();
  if (!direction || !current || target.closest?.("video")) return;
  const items = filteredCases(),
    index = items.findIndex((item) => item.case_id === current.id);
  const next = items[index + direction];
  if (next) {
    event.preventDefault();
    location.hash = `#case/${next.case_id}`;
  }
});
window.addEventListener("beforeunload", (event) => {
  if (
    state.editor?.dirty ||
    state.editor?.uploads ||
    state.editor?.saving ||
    state.deleteOperation ||
    window.CaseScripts?.hasUnsavedInput?.()
  ) {
    event.preventDefault();
    event.returnValue = "";
  }
});
document.addEventListener("dragover", (event) => {
  if (!isFileDrag(event)) return;
  event.preventDefault();
  const blocked =
    state.editor?.uploads ||
    state.editor?.saving ||
    state.editor?.deleted ||
    state.deletion ||
    state.deleteOperation ||
    state.archiveOperation ||
    state.bulk ||
    window.CaseAI?.modalOpen() ||
    window.CaseScripts?.modalOpen?.() ||
    window.MaterialBasket?.modalOpen?.() ||
    $("#discard-dialog").open;
  event.dataTransfer.dropEffect = blocked ? "none" : "copy";
  const overlay = $("#video-drop-overlay");
  overlay.hidden = false;
  $("#video-drop-label").textContent = blocked
    ? "请先完成当前操作"
    : state.editor
      ? "松开鼠标，导入到当前编辑"
      : dragItem()
        ? "松开鼠标，编辑此案例的视频"
        : "松开鼠标，导入视频并收录案例";
  $$("[data-video-drop]").forEach((zone) =>
    zone.classList.toggle("drag-active", zone.contains(event.target)),
  );
});
document.addEventListener("dragleave", (event) => {
  if (!event.relatedTarget) hideVideoDrop();
});
document.addEventListener("dragend", hideVideoDrop);
document.addEventListener("drop", (event) => {
  hideVideoDrop();
  if (!isFileDrag(event) && !event.dataTransfer?.files?.length) return;
  event.preventDefault();
  const files = [...(event.dataTransfer?.files || [])];
  if (!files.length) {
    toast("请拖入本地视频文件，不支持文件夹。", true);
    return;
  }
  void importVideoFiles(files, dragItem());
});
window.addEventListener("blur", hideVideoDrop);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh(true);
});
// Later deferred scripts register the page modules. Start routing only after
// they have all executed, even when /api/state responds very quickly.
function startWorkbench() { window.MaterialBasket?.initialize(); void refresh(); }
if (document.readyState === "complete") startWorkbench();
else document.addEventListener("DOMContentLoaded", startWorkbench, { once: true });
setInterval(() => {
  if (!document.hidden) refresh(true);
}, 5000);

// Dragging changes manual filing without changing the performance assessment.
(() => {
  const mime = "application/x-case-workbench";
  let suppressClickUntil = 0;
  const busy = () =>
    state.moving ||
    state.editor ||
    state.bulk ||
    state.bulkOperation ||
    state.archiveOperation ||
    state.deleteOperation;
  const clear = () => {
    state.dragging = null;
    document.body.classList.remove("case-dragging");
    $$(".case-drag-source,.case-drop-target").forEach((node) =>
      node.classList.remove("case-drag-source", "case-drop-target"),
    );
  };
  document.addEventListener("dragstart", (event) => {
    const card = event.target.closest?.("[data-selection-card]");
    if (!card) return;
    if (busy() || event.target.closest("input,button,select")) {
      event.preventDefault();
      return;
    }
    mutationSequence++;
    state.dragging = card.dataset.selectionCard;
    event.dataTransfer.clearData();
    event.dataTransfer.setData(mime, state.dragging);
    event.dataTransfer.effectAllowed = "move";
    card.classList.add("case-drag-source");
    document.body.classList.add("case-dragging");
  });
  document.addEventListener("dragover", (event) => {
    if (!state.dragging || ![...event.dataTransfer.types].includes(mime))
      return;
    event.preventDefault();
    const target = event.target.closest?.("nav [data-view]");
    $$(".case-drop-target").forEach((node) => {
      if (node !== target) node.classList.remove("case-drop-target");
    });
    const allowed = target && !target.hidden && !busy() && navigationMoveChoices().some(item => item.id === target.dataset.view);
    event.dataTransfer.dropEffect = allowed ? "move" : "none";
    if (allowed) target.classList.add("case-drop-target");
  });
  document.addEventListener("dragleave", (event) => {
    const target = event.target.closest?.("nav [data-view]");
    if (target && !target.contains(event.relatedTarget))
      target.classList.remove("case-drop-target");
  });
  document.addEventListener("dragend", () => {
    suppressClickUntil = Date.now() + 250;
    clear();
  });
  document.addEventListener(
    "click",
    (event) => {
      if (
        Date.now() < suppressClickUntil &&
        event.target.closest?.("[data-selection-card]")
      ) {
        event.preventDefault();
        event.stopPropagation();
      }
    },
    true,
  );
  document.addEventListener("drop", (event) => {
    if (![...(event.dataTransfer?.types || [])].includes(mime)) return;
    event.preventDefault();
    const id = event.dataTransfer.getData(mime);
    const target = event.target.closest?.("nav [data-view]");
    const trusted = id === state.dragging;
    suppressClickUntil = Date.now() + 250;
    clear();
    if (!trusted || !target || target.hidden || busy()) return;
    void moveCaseToView(id, target.dataset.view, "拖动卡片");
  });
})();

// Load saved columns before the initial state request completes.
initializeNavigation();
window.WorkbenchShell?.initialize();
