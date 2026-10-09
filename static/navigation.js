"use strict";

// Column definitions and personal collections share the existing browser-local store.
const navigationStorageKey = "case-library-navigation-v1";
const navigationIcons = {grid:"方格",spark:"星光",chart:"图表",clock:"时钟",folder:"文件夹",file:"文档",video:"视频",archive:"归档",check:"勾选",globe:"地球",layers:"图层",bot:"协作"};
const navigationSources = {all:"全部案例",positive:"转化正向参考",mixed:"高播放低转化",pending:"待验证与整理",native:"本土表达参考",legacy:"旧库资料",archived:"已归档",self_produced:"自制内容参考",overseas_creator:"达人内容参考",custom:"自定义收纳（手动选择案例）"};
const navigationDefaultIcons = {all:"grid",positive:"spark",mixed:"chart",pending:"clock",native:"globe",legacy:"layers",archived:"archive"};
let navigationItems = [], navigationStoredValue = null;

const libraryReferenceViews = {
  "reference-all": {id:"reference-all",label:"全部案例",content:"all",visible:true},
  "reference-native": {id:"reference-native",label:"本土表达参考",content:"native",visible:true},
  "reference-legacy": {id:"reference-legacy",label:"旧库资料",content:"legacy",visible:true},
};
function libraryViewFilters(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const text = (key, length=200) => typeof value[key] === "string" ? value[key].slice(0,length) : "";
  const base = text("baseView",100);
  return {
    baseView: Object.hasOwn(navigationDefaultIcons,base) || Object.hasOwn(libraryReferenceViews,base) || /^custom-[a-z0-9-]{8,80}$/.test(base) ? base : "all",
    query:text("query"), product:text("product",120), direction:text("direction"),
    archiveCategory:["positive","mixed","pending"].includes(value.archiveCategory) ? value.archiveCategory : "all",
    sourceType:["review","video_analysis","structure_template","traffic_reference"].includes(value.sourceType) ? value.sourceType : "all",
    contentOrigin:["self_produced","overseas_creator"].includes(value.contentOrigin) ? value.contentOrigin : "all",
    archiveStatus:["archived","all"].includes(value.archiveStatus) ? value.archiveStatus : "active",
    variantType:["direction","execution"].includes(value.variantType) ? value.variantType : "all",
    sort:["views","ctor","sold"].includes(value.sort) ? value.sort : "updated", sortDir:value.sortDir === "asc" ? "asc" : "desc",
  };
}

function navigationDefaults() {
  return Object.entries(navigationDefaultIcons).map(([id, symbol]) => ({id,label:navigationSources[id],icon:symbol,visible:true,deleted:false,content:id,description:"",product:"",query:"",caseIds:[]}));
}
function normalizeNavigation(value) {
  const defaults = navigationDefaults(), seen = new Set(), result = [];
  for (const item of Array.isArray(value) ? value : []) {
    if (!item || typeof item !== "object" || seen.has(item.id)) continue;
    const base = defaults.find(x => x.id === item.id);
    if (!base && !(typeof item.id === "string" && /^custom-[a-z0-9-]{8,80}$/.test(item.id))) continue;
    seen.add(item.id);
    const fallback = base || {...defaults[0],id:item.id,label:"新栏目",icon:"folder",content:"custom"};
    const text = (key, length) => typeof item[key] === "string" ? item[key].trim().slice(0,length) : "";
    const normalized = {...fallback,label:text("label",24) || fallback.label,icon:typeof item.icon === "string" && Object.hasOwn(navigationIcons,item.icon) ? item.icon : fallback.icon,visible:item.visible !== false,deleted:item.deleted === true,content:typeof item.content === "string" && Object.hasOwn(navigationSources,item.content) ? item.content : fallback.content,description:text("description",200),product:text("product",120),query:text("query",200),caseIds:[...new Set((Array.isArray(item.caseIds) ? item.caseIds : []).filter(id => typeof id === "string" && /^PVR-[0-9a-f]{12}$/.test(id)))]};
    const filters = libraryViewFilters(item.filters);
    if (filters) { if(filters.baseView === item.id) filters.baseView="all"; normalized.filters=filters; }
    result.push(normalized);
  }
  defaults.forEach(item => { if (!seen.has(item.id)) result.push(item); });
  if (!result.some(item => item.visible && !item.deleted)) Object.assign(result[0],{visible:true,deleted:false});
  return result;
}
function navigationItem(view = state.view) { return navigationItems.find(item => item.id === view) || libraryReferenceViews[view]; }
function navigationContent(view = state.view) { const item=navigationItem(view); return item?.filters ? navigationItem(item.filters.baseView)?.content || "all" : item?.content || view; }
function navigationLabel(view = state.view) { return navigationItem(view)?.label || navigationSources[view] || "案例"; }
function navigationDescription(view = state.view) { return navigationItem(view)?.description || ""; }
function isNavigationView(view) { const item = navigationItem(view); return !!item && item.visible && !item.deleted; }
function firstNavigationView() { return navigationItems.find(item => item.visible && !item.deleted)?.id || "all"; }
function navigationViewForContent(content) { return navigationItems.find(item => item.visible && !item.deleted && item.content === content && !item.product && !item.query)?.id || firstNavigationView(); }
function navigationAssignment(caseId) {
  const value = state.navigation[caseId];
  return typeof value === "string" && value.startsWith("column:") ? value.slice(7) : null;
}
function navigationCollectionCaseIds(column) {
  const selected = new Set(column.caseIds.filter(id => !navigationAssignment(id)));
  for (const item of state.cases) {
    if (navigationAssignment(item.case_id) === column.id) selected.add(item.case_id);
  }
  return [...selected];
}
function navigationMatches(item, view, depth=0, archiveOverride=null) {
  if(depth>4) return false;
  const column = navigationItem(view), content = navigationContent(view);
  if(column?.filters) return navigationMatches(item,column.filters.baseView,depth+1,column.filters.archiveStatus);
  const archive = archiveOverride || (view === state.view && state.archiveStatus) || (content === "archived" ? "archived" : "active");
  if (archive !== "all" && !!item.archived !== (archive === "archived")) return false;
  if(Object.hasOwn(libraryReferenceViews,view)) return content === "all" || (content === "native" ? !!item.current?.native_reference : !!item.current?.source_case);
  let matches;
  if (content === "all" || content === "archived") matches = true;
  else if (navigationAssignment(item.case_id)) matches = navigationAssignment(item.case_id) === view;
  else if (content === "custom") matches = !!column?.caseIds.includes(item.case_id);
  else if (content === "self_produced" || content === "overseas_creator") matches = item.current?.content_origin === content;
  else {
    const manual = state.navigation[item.case_id];
    matches = manual ? manual === content : content === "native" ? !!item.current?.native_reference : content === "legacy" ? !!item.current?.source_case : group(item) === content;
  }
  return matches && (!column?.product || productFilterGroup(item) === column.product) && (!column?.query || (item.search_text || "").toLowerCase().includes(column.query.toLowerCase()));
}
function navigationMoveChoices() {
  return navigationItems.filter(item => item.visible && !item.deleted && !["self_produced","overseas_creator"].includes(item.content) && !item.product && !item.query);
}
function saveNavigation(value) {
  const current = localStorage.getItem(navigationStorageKey);
  if (current !== navigationStoredValue) throw new Error("导航设置已在其他页面更新，请关闭后重新打开设置再修改。");
  const normalized = normalizeNavigation(value), raw = JSON.stringify(normalized);
  localStorage.setItem(navigationStorageKey,raw);
  navigationStoredValue = raw;
  navigationItems = normalized;
}
function openNavigationView(view) {
  Object.assign(state,{view,query:"",product:window.WorkbenchShell?.product() || "",archiveCategory:"all",sourceType:"all",contentOrigin:"all",archiveStatus:navigationContent(view)==="archived"?"archived":"active",sort:"updated",sortDir:"desc",detailTab:"current",libraryScrollY:0});
  const next = libraryHash();
  if (location.hash === next) { updateNav(); renderLibrary(); window.scrollTo(0,0); }
  else location.hash = next;
}
function applyNavigation() {
  const nav = $('nav[aria-label="案例分类"]');
  nav.replaceChildren();
  for (const item of navigationItems) {
    const node = document.createElement("button");
    node.type = "button"; node.className = "nav-item"; node.dataset.view = item.id;
    node.hidden = !item.visible || item.deleted;
    node.innerHTML = `${icon(item.icon)}<span class="navigation-label">${esc(item.label)}</span><span class="nav-count" id="count-${item.id}">0</span>`;
    node.title = item.label;
    nav.append(node);
  }
  updateNav();
}
function initializeNavigation() {
  try { navigationStoredValue = localStorage.getItem(navigationStorageKey); navigationItems = normalizeNavigation(JSON.parse(navigationStoredValue)); }
  catch { navigationItems = navigationDefaults(); }
  const nav = $('nav[aria-label="案例分类"]');
  nav.addEventListener("click", event => { const node = event.target.closest("[data-view]"); if (node && isNavigationView(node.dataset.view)) openNavigationView(node.dataset.view); });
  const button = document.createElement("button");
  button.type="button"; button.className="subtle dark"; button.id="navigation-settings"; button.textContent="⚙ 导航设置"; button.title="导航设置"; button.setAttribute("aria-label","导航设置");
  button.hidden = true;
  document.body.append(button);
  const dialog = document.createElement("dialog");
  dialog.id="navigation-dialog"; dialog.setAttribute("aria-labelledby","navigation-title");
  dialog.innerHTML = `<form><div class="dialog-heading"><div><h2 id="navigation-title">导航设置</h2><p>增加栏目，选择案例范围，或自由收纳案例。删除栏目后可随时恢复。</p></div><button type="button" class="icon-button" data-close aria-label="关闭导航设置">×</button></div><div class="navigation-toolbar"><span>栏目名称、图标和顺序都可调整</span><button type="button" class="primary compact" data-add>${icon("plus")}增加栏目</button></div><div class="navigation-fields"></div><p class="navigation-error" role="alert"></p><div class="dialog-footer"><span>栏目设置与自定义收纳保存在当前浏览器。</span><div><button type="button" class="secondary" data-reset>恢复默认</button><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存设置</button></div></div></form>`;
  document.body.append(dialog);
  let draft = [], draftBaseline = null, conflict = false;
  const error = dialog.querySelector(".navigation-error");
  const options = (values,current) => Object.entries(values).map(([value,label]) => `<option value="${esc(value)}" ${value===current ? "selected" : ""}>${esc(label)}</option>`).join("");
  function renderPicker(column, search="") {
    const holder = dialog.querySelector(`[data-id="${column.id}"] [data-case-picker]`);
    if (!holder) return;
    const needle=search.trim().toLowerCase(), selected = new Set(navigationCollectionCaseIds(column));
    const cases=state.cases.filter(item => (!item.archived || selected.has(item.case_id)) && (!needle || `${item.current?.title || item.title} ${item.product} ${item.case_id}`.toLowerCase().includes(needle)));
    holder.innerHTML=cases.length ? cases.map(item => {
      const assigned = navigationAssignment(item.case_id);
      const placement = assigned ? assigned === column.id ? "已移入本栏目；请用“移动到”调整" : `已移到“${navigationItem(assigned)?.label || "其他栏目"}”；请用“移动到”调整` : "";
      return `<label class="navigation-case"><input type="checkbox" data-case-id="${item.case_id}" ${selected.has(item.case_id)?"checked":""} ${assigned?"disabled":""}><span>${esc(shortTitle(item))}<small>${esc(item.product || "产品未提供")}${item.archived?" · 已归档，恢复后显示":""}</small>${placement?`<small>${esc(placement)}</small>`:""}</span></label>`;
    }).join("") : '<p class="navigation-hint">没有匹配的案例。</p>';
  }
  function render() {
    const products=[...new Set(state.cases.map(productFilterGroup).filter(Boolean))];
    dialog.querySelector(".navigation-fields").innerHTML=draft.map((item,index)=>item.deleted
      ? `<div class="navigation-deleted"><span>${esc(item.label)} · 已删除</span><button type="button" class="secondary" data-restore="${item.id}">恢复栏目</button></div>`
      : `<section class="navigation-row" data-id="${item.id}"><label class="navigation-visible"><input type="checkbox" data-visible ${item.visible?"checked":""}> 显示</label><div class="navigation-definition"><label class="navigation-input-label">栏目名称<input type="text" data-label aria-label="${esc(item.label)}的名称" required maxlength="24" value="${esc(item.label)}"></label><details class="navigation-options"><summary>${item.content==="custom" ? "收纳案例" : "筛选案例"}${item.content==="custom" ? ` · 已收纳 <span data-selected-count>${navigationCollectionCaseIds(item).length}</span> 条` : item.product || item.query ? " · 已设置筛选" : ""}</summary>${item.content==="custom" ? `<p class="navigation-hint">未移动的案例可勾选收纳；使用卡片的“移动到”或拖动会更改归属，并从其他栏目移出。已移动的案例请通过“移动到”调整。</p><input type="search" data-case-search aria-label="搜索${esc(item.label)}的可选案例" placeholder="搜索可选案例…"><div class="navigation-case-picker" data-case-picker></div>` : `<div class="navigation-filter-grid"><label class="navigation-input-label">产品范围<select data-product aria-label="${esc(item.label)}的产品范围">${options(Object.fromEntries([["","全部产品"],...[...new Set([...products,item.product].filter(Boolean))].map(p=>[p,p])]),item.product)}</select></label><label class="navigation-input-label">包含关键词<input type="text" data-query maxlength="200" aria-label="${esc(item.label)}的关键词" value="${esc(item.query)}" placeholder="可选，例如：磨刀"></label></div><p class="navigation-hint">按所选范围自动展示案例；来源只依据案例已保存的标注。</p>`}<label class="navigation-input-label">栏目说明（可选）<textarea data-description maxlength="200" aria-label="${esc(item.label)}的栏目说明" placeholder="写下这个栏目用来收集什么…">${esc(item.description)}</textarea></label></details></div><label class="navigation-input-label navigation-symbol">图标<select data-symbol aria-label="${esc(item.label)}的图标">${options(navigationIcons,item.icon)}</select></label><div class="navigation-order"><button type="button" class="secondary" data-move="-1" aria-label="上移${esc(item.label)}" ${index===0?"disabled":""}>↑</button><button type="button" class="secondary" data-move="1" aria-label="下移${esc(item.label)}" ${index===draft.length-1?"disabled":""}>↓</button><button type="button" class="danger-outline navigation-delete" data-delete="${item.id}" aria-label="删除${esc(item.label)}">删除</button></div></section>`).join("");
    draft.filter(item=>!item.deleted&&item.content==="custom").forEach(item=>renderPicker(item));
  }
  function refreshView() {
    applyNavigation();
    if (state.routeKind==="library") {
      if (!isNavigationView(state.view)) openNavigationView(firstNavigationView());
      else renderLibrary();
    } else if (state.routeKind==="case" && state.detail) renderDetail(state.detail);
  }
  button.onclick=()=>{
    try { const raw=localStorage.getItem(navigationStorageKey); if(raw!==navigationStoredValue) { navigationStoredValue=raw; navigationItems=normalizeNavigation(JSON.parse(raw)); refreshView(); } } catch {}
    draft=structuredClone(navigationItems); draftBaseline=navigationStoredValue; conflict=false; error.textContent=""; render(); dialog.showModal();
  };
  window.openNavigationSettings = () => button.onclick();
  dialog.querySelectorAll("[data-close]").forEach(el=>el.onclick=()=>dialog.close());
  dialog.querySelector("[data-add]").onclick=()=>{
    const item={...navigationDefaults()[0],id:`custom-${crypto.randomUUID()}`,label:"新栏目",content:"custom",icon:"folder",caseIds:[]};
    draft.push(item); render();
    const row=dialog.querySelector(`[data-id="${item.id}"]`); row.scrollIntoView({block:"center"}); row.querySelector("[data-label]").focus(); row.querySelector("[data-label]").select();
  };
  dialog.querySelector("[data-reset]").onclick=()=>{
    const defaults=navigationDefaults();
    // Keep personal collections recoverable, including collections attached to built-in columns.
    for(const item of draft) {
      if(item.id.startsWith("custom-")) defaults.push({...item,deleted:true});
      else if(item.caseIds.length) defaults.push({...item,id:`custom-${crypto.randomUUID()}`,content:"custom",deleted:true,product:"",query:""});
    }
    draft=defaults; error.textContent=""; render();
  };
  dialog.addEventListener("input",event=>{
    const row=event.target.closest("[data-id]"), item=draft.find(x=>x.id===row?.dataset.id); if(!item)return;
    const el=event.target;
    for(const [attr,key] of [["label","label"],["symbol","icon"],["description","description"],["product","product"],["query","query"]]) if(el.matches(`[data-${attr}]`)) item[key]=el.value;
    if(el.matches("[data-visible]")) item.visible=el.checked;
    if(el.matches("[data-case-search]")) renderPicker(item,el.value);
    if(el.matches("[data-case-id]")) {
      if(navigationAssignment(el.dataset.caseId)) { renderPicker(item,row.querySelector("[data-case-search]").value); return; }
      item.caseIds=el.checked ? [...new Set([...item.caseIds,el.dataset.caseId])] : item.caseIds.filter(id=>id!==el.dataset.caseId);
      row.querySelector("[data-selected-count]").textContent=navigationCollectionCaseIds(item).length;
    }
  });
  dialog.addEventListener("click",event=>{
    const remove=event.target.closest("[data-delete]"), restore=event.target.closest("[data-restore]"), move=event.target.closest("[data-move]");
    if(remove||restore){
      const item=draft.find(x=>x.id===(remove?.dataset.delete||restore.dataset.restore));
      if(remove&&item.visible&&!draft.some(x=>x.id!==item.id&&x.visible&&!x.deleted)){error.textContent="请至少保留一个可见栏目。";return;}
      item.deleted=!!remove; if(restore)item.visible=true; error.textContent="";render();
    } else if(move){
      const index=draft.findIndex(x=>x.id===move.closest("[data-id]").dataset.id), next=index+Number(move.dataset.move),direction=move.dataset.move;
      if(next<0||next>=draft.length)return;
      [draft[index],draft[next]]=[draft[next],draft[index]];render();dialog.querySelector(`[data-id="${draft[next].id}"] [data-move="${direction}"]`).focus();
    }
  });
  dialog.querySelector("form").onsubmit=event=>{
    event.preventDefault();
    if(conflict||draftBaseline!==navigationStoredValue){error.textContent="导航设置已在其他页面更新，请关闭后重新打开设置再修改。";return;}
    if(draft.some(x=>!x.deleted&&!x.label.trim())){error.textContent="栏目名称不能为空。";return;}
    if(!draft.some(x=>x.visible&&!x.deleted)){error.textContent="请至少保留一个可见栏目。";return;}
    try {saveNavigation(draft);} catch(reason){error.textContent=reason.message?.includes("其他页面")?reason.message:"浏览器未能保存设置，请检查本地存储后重试。";return;}
    refreshView();dialog.close();toast("导航设置已保存");
  };
  window.addEventListener("storage",event=>{
    if(event.key!==navigationStorageKey&&event.key!==null)return;
    try {navigationStoredValue=localStorage.getItem(navigationStorageKey);navigationItems=normalizeNavigation(JSON.parse(navigationStoredValue));} catch{return;}
    if(dialog.open){conflict=true;error.textContent="导航设置已在其他页面更新，当前草稿已保留，请关闭后重新打开设置再修改。";}
    refreshView();
  });
  applyNavigation();
}

// Saved filters extend the existing preferences in place. Old collections and
// server-side assignments stay readable without migrating any case records.
window.LibraryViews = (() => {
  const selected = () => navigationItem(state.view);
  function available(includeDeleted=false) {
    const assigned=new Set(Object.values(state.navigation || {}).filter(value=>typeof value==="string"&&value.startsWith("column:")).map(value=>value.slice(7)));
    return navigationItems.filter(item => (includeDeleted || (!item.deleted && item.visible)) && (item.id.startsWith("custom-") || item.filters || assigned.has(item.id) || item.caseIds.length || item.label !== navigationSources[item.id] || item.content !== item.id || item.product || item.query || !Object.hasOwn(navigationDefaultIcons,item.id)));
  }
  function snapshot() {
    return libraryViewFilters({...state,product:window.WorkbenchShell?.product() ?? state.product,baseView:selected()?.filters?.baseView || state.view});
  }
  function applyRouteFilters() {
    const filters=selected()?.filters;
    if(!filters)return;
    for(const [key,value] of Object.entries(filters)) if(!["baseView","product"].includes(key)) state[key]=value;
  }
  function detach() { if(selected()?.filters) state.view=selected().filters.baseView; }
  function open(id) {
    const item=navigationItem(id);
    if(!item||item.deleted)return;
    if(!item.filters) {openNavigationView(id);return;}
    state.view=id;
    applyRouteFilters();
    let target=libraryHash();
    if(window.WorkbenchShell) target=window.WorkbenchShell.url(target,item.filters.product);
    if(location.hash===target) {updateNav();renderLibrary();} else location.hash=target;
  }
  function persist(change) {
    const copy=structuredClone(navigationItems);
    change(copy);
    saveNavigation(copy);
    applyNavigation();
    if(state.routeKind==="library") {
      if(!isNavigationView(state.view)) {state.view="reference-all";writeLibraryHash();}
      renderLibrary();
    }
  }
  function saveCurrent() {
    const filters=snapshot();
    let dialog=$("#library-save-view");
    if(!dialog){dialog=document.createElement("dialog");dialog.id="library-save-view";document.body.append(dialog);}
    dialog.innerHTML=`<form><div class="dialog-heading"><h2>保存当前视图</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="library-view-form"><label class="field"><span>视图名称</span><input name="view_name" required maxlength="24" autocomplete="off" placeholder="例如：202H 达人正向参考"></label><p>保存当前产品、筛选与排序。匹配的新案例也会出现在这个视图。</p><p class="library-view-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存视图</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>dialog.close());
    dialog.querySelector("form").onsubmit=event=>{
      event.preventDefault();
      const label=dialog.querySelector("input").value.trim();
      if(!label)return;
      const item={...navigationDefaults()[0],id:`custom-${crypto.randomUUID()}`,label,icon:"folder",filters};
      try {persist(items=>items.push(item));dialog.close();open(item.id);toast("视图已保存");}
      catch(error){dialog.querySelector(".library-view-error").textContent=error.message||"浏览器暂时无法保存，请重试。";}
    };
    dialog.showModal();dialog.querySelector("input").focus();
  }
  function openManager() {
    let dialog=$("#library-view-manager");
    if(!dialog){dialog=document.createElement("dialog");dialog.id="library-view-manager";document.body.append(dialog);}
    let baseline=navigationStoredValue;
    function render(){
      const items=available(true);
      dialog.innerHTML=`<div class="dialog-heading"><div><h2>保存的视图</h2><p>筛选视图与已有收纳都保留在这里。</p></div><button type="button" class="icon-button" data-close aria-label="关闭保存的视图">×</button></div><div class="library-view-manager-body">${items.length?items.map(item=>`<div class="library-view-row" data-view-id="${esc(item.id)}"><div><label><span class="sr-only">视图名称</span><input aria-label="${esc(item.label)}的名称" maxlength="24" value="${esc(item.label)}" ${item.deleted?"disabled":""}></label><small>${item.deleted?"已删除，可恢复":item.filters?"按保存的筛选自动展示":"保留已有案例收纳与筛选"}</small></div><div class="settings-actions">${item.deleted?'<button class="secondary" data-restore-view>恢复</button>':'<button class="secondary" data-rename-view>重命名</button><button class="subtle" data-delete-view>删除视图</button>'}</div></div>`).join(""):'<p class="context-note">还没有保存的视图。在案例库选好筛选后，点击“保存当前视图”。</p>'}<p class="library-view-error" role="alert"></p></div><div class="dialog-footer"><span>删除视图后可在这里恢复。</span><button class="secondary" data-close>完成</button></div>`;
      dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>dialog.close());
      dialog.querySelectorAll("[data-rename-view],[data-delete-view],[data-restore-view]").forEach(button=>button.onclick=()=>{
        const row=button.closest("[data-view-id]"),label=row.querySelector("input").value.trim();
        if(button.matches("[data-rename-view]")&&!label){dialog.querySelector(".library-view-error").textContent="视图名称不能为空。";return;}
        try {
          if(baseline!==navigationStoredValue || localStorage.getItem(navigationStorageKey)!==baseline)throw new Error("其他页面已更新视图，请关闭后重新打开。");
          persist(items=>{const item=items.find(entry=>entry.id===row.dataset.viewId);if(button.matches("[data-rename-view]"))item.label=label;else{item.deleted=button.matches("[data-delete-view]");if(!item.deleted)item.visible=true;}});
          baseline=navigationStoredValue;
          render();
        } catch(error){dialog.querySelector(".library-view-error").textContent=error.message||"暂时无法保存，请重试。";}
      });
    }
    render();dialog.showModal();
  }
  return {available,snapshot,applyRouteFilters,detach,open,saveCurrent,openManager};
})();
