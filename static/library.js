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
function card(item) {
  const cur = item.current || {},
    m = { ...caseIdentity(item), ...salesMetrics(item) },
    validVideo = item.assets?.video?.exists,
    hasMetrics = [m.views_display, m.ctor_display, m.items_sold].some((value) => value !== undefined && value !== null && value !== "");
  const media = validVideo
    ? `<div class="card-media"><div class="missing-media thumbnail-fallback">${icon("video")}<span>正在读取视频封面…</span></div><img data-card-thumbnail data-src="${mediaURL(item.case_id, "thumbnail", item.current_event_id)}" loading="lazy" decoding="async" alt=""><span class="play-mark">${icon("play")}</span><span class="media-label">${displayValue(item.product)}</span><div class="card-media-footer"><span class="${m.creator_handle ? "" : "missing-value"}">${displayValue(m.creator_handle)}</span><span>${esc(item.current?.source_case ? sourceTypeNames[sourceType(item)] || "旧库资料" : "视频案例")}</span></div></div>`
    : `<div class="card-media is-missing">${icon("video")}<span>${item.assets?.video ? "视频文件暂不可用" : "尚未关联视频"}</span><span class="card-media-meta">${esc([item.product, m.creator_handle].filter(Boolean).join(" · "))}</span></div>`;
  const metric = (label, value, suffix = "", accent = false) => `<div><span class="metric-label">${label}</span><span class="metric-value${accent ? " accent" : ""}${value === undefined ? " missing-value" : ""}">${displayValue(value)}${value !== undefined && suffix ? `<small>${suffix}</small>` : ""}</span></div>`;
  return `<a draggable="false" class="case-card" href="#case/${item.case_id}" aria-label="查看案例：${esc(shortTitle(item))}">${media}
    <div class="card-body"><div class="card-topline"><span class="case-badges">${badge(item)}${sourceBadges(item)}${item.archived ? archiveBadge(item) : ""}</span></div><h2>${esc(shortTitle(item))}</h2><p class="card-description">${esc(cur.conclusion || "等待整理当前结论、有效视频和文案。")}</p>
    ${hasMetrics ? `<div class="card-metrics">${metric("播放量", m.views_display)}${metric("CTOR", m.ctor_display, "", true)}${metric("售出", m.items_sold, "件")}</div>` : `<div class="card-metrics is-empty"><span>${esc(salesStatusLabel(item))}</span></div>`}
    <div class="card-foot"><span class="card-direction-name" title="${esc(cur.test_direction?.main_difference || "")}">${esc(directionLabel(item))}</span><span>${esc(String(readableDate(item.updated_at)).split(" ")[0])}</span></div></div></a>`;
}
function libraryPerformance(item) {
  const current=item.current;
  if(!current)return "pending";
  if(current.category === "转化正向参考")return "positive";
  if(String(current.category || "").includes("高播放低转化"))return "mixed";
  if(["待验证与整理","待验证"].includes(current.category))return "pending";
  return (current.assessments || []).some(value=>value.dimension === "conversion" && value.polarity === "positive") ? "positive" : "pending";
}
function libraryArchiveStatus() {
  return state.archiveStatus || (navigationContent() === "archived" ? "archived" : "active");
}
function libraryFilterMarkup(id,label,values,current) {
  return `<label class="library-filter-field"><span>${label}</span><select id="${id}">${Object.entries(values).map(([value,text])=>`<option value="${esc(value)}" ${value===current?"selected":""}>${esc(text)}</option>`).join("")}</select></label>`;
}
function libraryPresetSelected(id) {
  if(navigationItem()?.filters)return false;
  const archive=libraryArchiveStatus();
  if(id === "archived")return archive === "archived" && state.view === "reference-all";
  if(id === "native" || id === "legacy")return state.view === `reference-${id}` && archive === "active";
  return state.view === "reference-all" && archive === "active" && (state.archiveCategory || "all") === id;
}
function updateLibraryViewControls() {
  const picker=$("#library-saved-view");
  if(picker)picker.value=window.LibraryViews?.available().some(item=>item.id===state.view)?state.view:"";
  $$("[data-library-preset]").forEach(button=>button.setAttribute("aria-pressed",String(libraryPresetSelected(button.dataset.libraryPreset))));
}
function renderLibrary() {
  const content=navigationContent(),archived=libraryArchiveStatus()==="archived",scope=scopedCases();
  const saved=window.LibraryViews?.available() || [];
  const presets={all:"全部案例",positive:"转化正向",mixed:"高播放低转化",pending:"待验证",native:"本土表达",legacy:"旧库资料",archived:"已归档"};
  $("#breadcrumb").innerHTML="工作空间 <span>/</span> 案例库";
  window.WorkbenchShell?.registerProducts(state.cases);
  const summary=content==="native"?nativeSummary(scope):"";
  const field=(id,label,values,current)=>libraryFilterMarkup(id,label,values,current);
  const drawerFields=`${field("archive-category-filter","表现分类",{all:"全部表现",positive:"转化正向参考",mixed:"高播放低转化",pending:"待验证与整理"},state.archiveCategory || "all")}${field("content-origin-filter","内容来源",{all:"全部来源",self_produced:"自制内容",overseas_creator:"海外达人"},state.contentOrigin || "all")}${field("source-type-filter","资料类型",sourceTypeNames,state.sourceType)}${field("archive-status-filter","归档状态",{active:"使用中",archived:"已归档",all:"全部状态"},libraryArchiveStatus())}<label class="library-filter-field"><span>测试方向</span><select id="direction-filter" aria-label="筛选测试方向">${directionFilterOptions(scope)}</select></label>${field("variant-filter","变体类型",{all:"全部变体",...directionVariantNames},state.variantType)}${field("sort-filter","排序",{updated:"最近更新",views:"播放量",ctor:"CTOR",sold:"售出件数"},state.sort)}${field("sort-direction","顺序",{desc:"降序",asc:"升序"},state.sortDir)}`;
  $("#content").innerHTML=`<div class="page-heading lib-heading"><div><h1>案例库</h1><p>${scope.length} 条案例 · 按表现、来源和方向查找参考，常用筛选可以存成视图。</p></div><div class="layout-switch lib-layout" role="group" aria-label="案例显示方式"><button type="button" data-layout="cards" class="${state.layout === "cards" ? "active" : ""}" aria-pressed="${state.layout === "cards"}">${icon("grid")}卡片</button><button type="button" data-layout="list" class="${state.layout === "list" ? "active" : ""}" aria-pressed="${state.layout === "list"}">${icon("file")}列表</button></div></div>
    <section class="library-filter-panel lib-toolbar" aria-label="案例库筛选"><div class="lib-search-row"><label class="search">${icon("search")}<input id="case-search" aria-label="搜索当前案例" placeholder="搜索场景、英文表达或结论…" value="${esc(state.query)}"></label><button type="button" class="secondary lib-filter-open" id="library-filter-open" aria-haspopup="dialog" aria-controls="library-filter-drawer"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>筛选<span class="rev-filter-count" id="library-filter-count" hidden></span></button>
      <div class="lib-views library-saved-row"><label><span>视图</span><select id="library-saved-view" aria-label="保存的视图"><option value="">当前筛选</option>${saved.map(item=>`<option value="${esc(item.id)}" ${state.view===item.id?"selected":""}>${esc(item.label)}</option>`).join("")}</select></label><button type="button" class="subtle" id="library-save-view-button">${icon("plus")}保存视图</button><button type="button" class="subtle" id="library-manage-views">管理</button></div>
      <button type="button" class="subtle" id="library-select-toggle" aria-pressed="${!!state.selecting}">${state.selecting ? "完成" : "选择"}</button></div>
      <div class="library-presets lib-presets" role="group" aria-label="快捷筛选">${Object.entries(presets).map(([id,label])=>`<button type="button" data-library-preset="${id}" aria-pressed="${libraryPresetSelected(id)}">${label}</button>`).join("")}</div>
      <div class="lib-active-row"><div class="rev-active" id="library-active"></div><span id="result-count" class="result-count" aria-live="polite"></span><button type="button" class="subtle" id="library-clear-filters">清除筛选</button></div></section>${summary}
    <div id="selection-toolbar" class="selection-toolbar"${state.selecting ? "" : " hidden"}></div><div id="bulk-last-result" class="bulk-last-result" hidden></div><div id="case-results"></div><p class="library-footnote">表现以案例已保存的判断筛选；数字按视频 ID 读取销售数据表。</p>
    <dialog id="library-filter-drawer" class="rev-drawer" aria-labelledby="library-filter-title"><div class="rev-drawer-head"><h2 id="library-filter-title">筛选与排序</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭筛选">${icon("close")}</button></div><div class="rev-drawer-body"><div class="library-filter-grid lib-drawer-grid">${drawerFields}</div></div><div class="rev-drawer-foot"><button type="button" class="subtle" id="library-drawer-clear">清除全部</button><button type="button" class="primary" data-drawer-close id="library-drawer-done">显示结果</button></div></dialog>`;
  $("#content").classList.toggle("lib-selecting",!!state.selecting);
  const drawer=$("#library-filter-drawer");
  $("#library-filter-open").onclick=()=>{state.libraryDrawer=true;drawer.showModal();};
  drawer.addEventListener("click",event=>{if(event.target===drawer||event.target.closest("[data-drawer-close]"))drawer.close();});
  drawer.addEventListener("close",()=>{state.libraryDrawer=false;});
  if(state.libraryDrawer)drawer.showModal();
  $("#library-select-toggle").onclick=()=>{state.selecting=!state.selecting;if(!state.selecting)state.selection.clear();renderLibrary();};
  $$(".lib-layout [data-layout]").forEach(button=>button.onclick=()=>{state.layout=button.dataset.layout;try{localStorage.setItem("case-library-layout",state.layout);}catch{}$$(".lib-layout [data-layout]").forEach(item=>{const on=item===button;item.classList.toggle("active",on);item.setAttribute("aria-pressed",String(on));});renderCards();});
  $("#library-video-import")?.addEventListener("click",()=>openVideoPicker(null));
  $("#library-save-view-button").onclick=()=>window.LibraryViews?.saveCurrent();
  $("#library-manage-views").onclick=()=>window.LibraryViews?.openManager();
  $("#library-saved-view").onchange=event=>{if(event.target.value)window.LibraryViews?.open(event.target.value);};
  const change=(key,value,scopeChange=false)=>{
    window.LibraryViews?.detach();state[key]=value;
    if(key==="archiveCategory" && ["positive","mixed","pending"].includes(state.view))state.view="reference-all";
    if(key==="archiveStatus"&&navigationContent()==="archived")state.view="reference-all";
    writeLibraryHash();updateNav();
    if(scopeChange)renderLibrary();else{updateLibraryViewControls();renderCards();}
  };
  for(const [id,key] of [["archive-category-filter","archiveCategory"],["content-origin-filter","contentOrigin"],["source-type-filter","sourceType"],["archive-status-filter","archiveStatus"],["direction-filter","direction"],["variant-filter","variantType"],["sort-filter","sort"],["sort-direction","sortDir"]]){
    $("#"+id).onchange=event=>change(key,event.target.value,key==="archiveStatus");
  }
  $("#case-search").addEventListener("input",event=>{
    window.LibraryViews?.detach();state.query=event.target.value;writeLibraryHash();updateLibraryViewControls();clearTimeout(searchTimer);searchTimer=setTimeout(renderCards,150);
  });
  const reset=()=>Object.assign(state,{view:"reference-all",query:"",direction:"",archiveCategory:"all",sourceType:"all",contentOrigin:"all",archiveStatus:"active",variantType:"all",sort:"updated",sortDir:"desc"});
  $("#library-clear-filters").onclick=()=>{reset();writeLibraryHash();updateNav();renderLibrary();};
  $("#library-drawer-clear").onclick=()=>{reset();writeLibraryHash();updateNav();renderLibrary();};
  $("#library-active").addEventListener("click",event=>{
    const token=event.target.closest("[data-lib-clear]");if(!token)return;
    const key=token.dataset.libClear,defaults={query:"",contentOrigin:"all",sourceType:"all",archiveStatus:"active",direction:"",variantType:"all",sort:"updated",archiveCategory:"all"};
    window.LibraryViews?.detach();state[key]=defaults[key];if(key==="sort")state.sortDir="desc";if(key==="archiveStatus")state.view="reference-all";
    writeLibraryHash();updateNav();renderLibrary();
  });
  $$("[data-library-preset]").forEach(button=>button.onclick=()=>{
    const preset=button.dataset.libraryPreset;
    Object.assign(state,{view:["native","legacy"].includes(preset)?`reference-${preset}`:"reference-all",archiveCategory:["positive","mixed","pending"].includes(preset)?preset:"all",archiveStatus:preset==="archived"?"archived":"active",contentOrigin:"all",sourceType:"all"});
    writeLibraryHash();updateNav();renderLibrary();
  });
  bindNativeMode();renderCards();
}
// The filters in force that the drawer holds, each removable on its own; the category stays in the segment above.
function libraryActiveTokens() {
  const tokens=[], names={self_produced:"自制内容",overseas_creator:"海外达人"};
  if(state.query.trim())tokens.push(["query",`“${state.query.trim()}”`]);
  if((state.contentOrigin||"all")!=="all")tokens.push(["contentOrigin",names[state.contentOrigin]||state.contentOrigin]);
  if(state.sourceType&&state.sourceType!=="all")tokens.push(["sourceType",sourceTypeNames[state.sourceType]||state.sourceType]);
  if(libraryArchiveStatus()!=="active"&&!libraryPresetSelected("archived"))tokens.push(["archiveStatus",libraryArchiveStatus()==="all"?"含已归档":"已归档"]);
  if(state.direction)tokens.push(["direction",state.direction==="__unclassified__"?"未记录方向":state.direction]);
  if(state.variantType&&state.variantType!=="all")tokens.push(["variantType",directionVariantNames[state.variantType]||state.variantType]);
  if(state.sort!=="updated"||state.sortDir!=="desc")tokens.push(["sort",`按${{updated:"最近更新",views:"播放量",ctor:"CTOR",sold:"售出件数"}[state.sort]}${state.sortDir==="asc"?"升序":"降序"}`]);
  return tokens;
}
function renderCards() {
  if (!$("#case-results")) return;
  const tokens = libraryActiveTokens(), active = $("#library-active");
  if (active) active.innerHTML = tokens.map(([key,label]) => `<button type="button" class="rev-token" data-lib-clear="${key}" aria-label="去掉筛选：${esc(label)}">${esc(label)}<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M7 17 17 7"/></svg></button>`).join("");
  const count = $("#library-filter-count"), drawerCount = tokens.filter(([key]) => key !== "query").length;
  if (count) { count.hidden = !drawerCount; count.textContent = drawerCount; }
  const content = navigationContent(),
    archived = libraryArchiveStatus() === "archived",
    scope = scopedCases(),
    category = state.archiveCategory || "all";
  const categoryItems = scope.filter(
    (item) => category === "all" || libraryPerformance(item) === category,
  );
  const query = state.query.trim().toLowerCase(),
    items = filteredCases();
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
  $("#result-count").textContent = nativeRows
    ? `${nativeRows.length} 条表达 · ${items.length} 条视频`
    : `${items.length} 条案例`;
  if ($("#library-drawer-done")) $("#library-drawer-done").textContent = `显示 ${items.length} 条结果`;
  let emptyTitle, emptyText, action, actionLabel;
  if (scope.length && (query || state.product || state.sourceType !== "all" || state.direction || state.variantType !== "all")) {
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
      : `<div class="case-grid">${items.map(selectableCard).join("")}</div>`
    : `<div class="empty">${icon(archived ? "archive" : "search")}<h3>${emptyTitle}</h3><p>${emptyText}</p><button class="secondary" id="empty-action">${actionLabel}</button></div>`;
  activateCardThumbnails();
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
      $("#sort-filter").value = key;
      $("#sort-direction").value = state.sortDir;
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
    if (action === "navigation") {
      $("#navigation-settings")?.click();
      return;
    }
    if (action === "add") {
      openEditor(null);
      return;
    }
    state.query = "";
    state.product = window.WorkbenchShell?.product() || "";
    state.direction = "";
    state.variantType = "all";
    state.archiveCategory = "all";
    state.sourceType = "all";
    state.contentOrigin = "all";
    state.archiveStatus = action === "archived" ? "archived" : action === "active" ? "active" : libraryArchiveStatus();
    if (["archived","active","all"].includes(action))
      state.view = "reference-all";
    writeLibraryHash();
    updateNav();
    renderLibrary();
  });
}
function refreshLibraryData() {
  const scope = scopedCases(),
    archived = libraryArchiveStatus() === "archived";
  const note = $(".total-note");
  if (note)
    note.textContent = `${scope.length} 条范围内案例 · 本地保存`;
  const products = [...new Set(scope.map(productFilterGroup).filter(Boolean))];
  const picker = $("#product-filter");
  if (picker) {
    picker.innerHTML =
      '<option value="">全部产品</option>' +
      products
        .map(
          (product) =>
            `<option value="${esc(product)}">${esc(product)}</option>`,
        )
        .join("");
    if (state.product && !products.includes(state.product)) state.product = "";
    picker.value = state.product;
  }
  if ($("#direction-filter")) $("#direction-filter").innerHTML = directionFilterOptions(scope);
  renderCards();
}
function directionFilterOptions(scope) {
  const ids = [...new Set(scope.map(item => item.current?.test_direction?.direction_id).filter(Boolean))].sort();
  if (state.direction && state.direction !== "__unclassified__" && !ids.includes(state.direction)) ids.push(state.direction);
  return '<option value="">全部测试方向</option>' + `<option value="__unclassified__" ${state.direction === "__unclassified__" ? "selected" : ""}>未记录方向</option>` + ids.map(id => `<option value="${esc(id)}" ${state.direction === id ? "selected" : ""}>${esc(id)}</option>`).join("");
}
function filteredCases() {
  const category = state.archiveCategory || "all";
  const query = state.query.trim().toLowerCase();
  return scopedCases()
    .filter(
      (item) =>
        (category === "all" || libraryPerformance(item) === category) &&
        (!state.contentOrigin || state.contentOrigin === "all" || item.current?.content_origin === state.contentOrigin) &&
        (state.sourceType === "all" || sourceType(item) === state.sourceType) &&
        (window.WorkbenchShell ? window.WorkbenchShell.matchesProduct(item) : !state.product || productFilterGroup(item) === state.product) &&
        (!state.direction || (state.direction === "__unclassified__" ? !item.current?.test_direction : item.current?.test_direction?.direction_id === state.direction)) &&
        (state.variantType === "all" || item.current?.test_direction?.variant_type === state.variantType) &&
        (!query || item.search_text?.toLowerCase().includes(query)),
    )
    .sort((a, b) => {
      if (state.sort === "updated")
        return (
          (state.sortDir === "asc" ? 1 : -1) *
          a.updated_at.localeCompare(b.updated_at)
        );
      const key = {
        views: "views_display",
        ctor: "ctor_display",
        sold: "items_sold",
      }[state.sort];
      const av = salesMetrics(a)[key],
        bv = salesMetrics(b)[key];
      if (av === undefined || av === null || av === "")
        return bv === undefined || bv === null || bv === ""
          ? b.updated_at.localeCompare(a.updated_at)
          : 1;
      if (bv === undefined || bv === null || bv === "") return -1;
      return (
        (state.sortDir === "asc" ? 1 : -1) * (numeric(av) - numeric(bv)) ||
        b.updated_at.localeCompare(a.updated_at)
      );
    });
}
function selectControl(item) {
  return `<label class="case-select"><input type="checkbox" data-case-select="${esc(item.case_id)}" ${state.selection.has(item.case_id) ? "checked" : ""} ${state.bulk ? "disabled" : ""} aria-label="选择案例：${esc(shortTitle(item))}"><span>选择</span></label>`;
}
function selectableCard(item) {
  return `<article class="selectable-card ${state.selection.has(item.case_id) ? "is-selected" : ""}" data-selection-card="${esc(item.case_id)}">${selectControl(item)}${card(item)}</article>`;
}
function sortHeading(key, label) {
  const active = state.sort === key;
  const direction = active
    ? state.sortDir === "asc"
      ? "ascending"
      : "descending"
    : "none";
  return `<th scope="col" aria-sort="${direction}"><button type="button" class="sort-heading" data-sort-header="${key}">${label}<span aria-hidden="true">${active ? (state.sortDir === "asc" ? "↑" : "↓") : "↕"}</span></button></th>`;
}
// One line of badges per row: archive state first, then the category, the rest folded into "+N".
function tableBadges(item, limit = 1) {
  const template = document.createElement("template");
  template.innerHTML = `${item.archived ? archiveBadge(item) : ""}${badge(item)}${sourceBadges(item)}`;
  const all = [...template.content.children], rest = all.slice(limit);
  return `<span class="table-badges">${all.slice(0, limit).map((el) => el.outerHTML).join("")}${rest.length ? `<span class="badge table-badge-more" title="${esc(rest.map((el) => el.textContent).join("、"))}">+${rest.length}</span>` : ""}</span>`;
}
function listView(items) {
  return `<div class="case-table-wrap"><table class="case-table"><thead><tr>
    <th scope="col">选择</th><th scope="col">案例标题</th><th scope="col">产品</th><th scope="col">分类</th><th scope="col">测试方向</th>
    ${sortHeading("views", "播放量")}${sortHeading("ctor", "CTOR")}${sortHeading("sold", "售出件数")}
    ${sortHeading("updated", "最近更新")}
    </tr></thead><tbody>${items
      .map((item) => {
        const m = { ...caseIdentity(item), ...salesMetrics(item) }, updated = readableDate(item.updated_at);
        return `<tr data-selection-card="${esc(item.case_id)}" class="${state.selection.has(item.case_id) ? "is-selected" : ""}">
    <td>${selectControl(item)}</td><td><a class="table-case-link" href="#case/${esc(item.case_id)}" title="${esc(item.current?.title || item.title || "")}">${esc(shortTitle(item))}</a><small class="table-case-id">${m.video_id ? `视频 <span class="home-video-id">${esc(m.video_id)}</span> · ` : ""}${esc(item.case_id)}</small></td>
    <td class="table-product" title="${esc(item.product || "")}">${displayValue(item.product)}</td><td>${tableBadges(item)}</td><td class="table-direction" title="${esc(directionChain(item.current?.test_direction))}">${esc(directionLabel(item))}</td>
    ${[m.views_display, m.ctor_display, m.items_sold].map((value) => `<td class="num" title="${esc(value ?? "")}">${displayValue(value)}</td>`).join("")}
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
