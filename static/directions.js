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
  return d ? `${d.direction_id} · ${directionVariantNames[d.variant_type] || d.variant_type}` : "未记录测试方向";
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

window.CaseDirections = (() => {
  let requestSequence = 0;
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const pageURL = hash => window.WorkbenchShell?.url(hash) || hash;
  const missingLabels = { metrics: "表现数据", evidence_window: "统计窗口", script_version: "文案版本", video_id: "视频 ID" };
  const evidenceNames = { source_checked: "来源已核对", user_reported: "用户提供", unverified: "待核实" };
  function observationRow(o) {
    const m = salesMetrics(o), e = salesEvidence(o), d = o.test_direction || {}, identity = caseIdentity(o);
    const video = o.sales_data?.video || {};
    const windowText = video.first_data && video.last_data ? `${video.first_data} — ${video.last_data}` : e.window || "未提供";
    const status = { matched: "已匹配", not_found: "未匹配到视频", missing_video_id: "缺少视频 ID", unavailable: "暂不可用" }[o.sales_data?.status] || "暂不可用";
    const title = o.title || o.case_id;
    return `<tr>
      <td><a class="direction-case-title" href="#case/${esc(o.case_id)}" title="${esc(title)}">${esc(title)}</a><small>${esc(directionVariantNames[d.variant_type] || "类型未提供")}${o.archived ? " · 已归档" : ""}</small></td>
      ${[m.views_display, m.product_clicks_display, m.items_sold, m.ctor_display].map(v => `<td class="num">${displayValue(v)}</td>`).join("")}
      <td><span class="direction-window">${esc(windowText)}</span>${e.as_of ? `<small>截至 ${esc(e.as_of)}</small>` : ""}
        <details class="direction-record-details"><summary>覆盖与版本</summary><div>
          <p>${esc(e.window || "统计窗口未提供")}</p>${e.context ? `<p>${esc(e.context)}</p>` : ""}
          <p>文案版本 ${esc(o.script_version || d.script_version || "未提供")}${o.version_conflict ? " · 版本不一致" : ""}</p>
          ${d.execution_change ? `<p>执行改动 ${esc(d.execution_change)}</p>` : ""}
          <p>原产品名 ${esc(o.product || "未提供")}</p><p>视频 ${esc(identity.video_id || "未提供")}</p><p>案例 ${esc(o.case_id)}</p><p>当前记录 ${esc(o.current_event_id || "未提供")}</p>
        </div></details>
      </td>
      <td><span class="direction-sales-status" title="${esc(salesStatusLabel(o))}">${esc(status)}</span><small><a href="${esc(salesDataURL(o))}">查看销售数据表</a></small></td>
    </tr>`;
  }
  function leadTierLine(counts) {
    if (!counts) return "";
    const parts = [["S", "S 级"], ["A", "A 级"], ["B", "B 级"]].filter(([key]) => counts[key]).map(([key, label]) => `${label} ${counts[key]} 个`);
    if (counts.uncoded) parts.push(`未编号 ${counts.uncoded} 个`);
    return parts.length ? `<p class="direction-tier-counts"><strong>按主打卖点</strong> ${esc(parts.join(" · "))}</p>` : "";
  }
  function groupCard(g) {
    const missing = Object.entries(g.missing_counts || {}).filter(([,count]) => count).map(([key,count]) => `${missingLabels[key] || key}缺 ${count} 条`).join(" · ");
    const definitions = g.definitions || [], preview = directionChain(definitions[0]) || "方向链未提供";
    const variantCounts = Object.entries(g.variant_counts || {}).filter(([,count]) => count).map(([key,count]) => `${directionVariantNames[key] || key} ${count}`).join(" · ");
    return `<section class="section-card direction-group">
      <div class="section-title-row direction-group-header"><h2>${esc(g.direction_id)}</h2><span class="direction-group-count">${g.case_count} 条案例</span></div>
      <p class="direction-group-meta">${esc(g.product)}${variantCounts ? ` · ${esc(variantCounts)}` : ""}</p>
      ${g.definition_conflict ? '<p class="data-warning">相同编号出现不同方向定义，请核对主打卖点；展开查看各条方向链。</p>' : ""}
      <p class="direction-preview" title="${esc(preview)}"><strong>方向链</strong> ${esc(preview)}</p>
      <details class="direction-definition-details"><summary>${definitions.length > 1 ? `查看全部方向链（${definitions.length} 条，同一主打卖点的执行变体）` : "查看完整方向链"}</summary>
        ${definitions.map(d => `<div class="direction-definition"><p class="direction-chain">${esc(directionChain(d))}</p></div>`).join("")}
      </details>
      <div class="direction-observations" tabindex="0" role="region" aria-label="${esc(g.direction_id)} 的案例表现，可横向滚动"><table class="direction-table"><colgroup><col class="direction-col-case"><col class="direction-col-views"><col class="direction-col-clicks"><col class="direction-col-items"><col class="direction-col-ctor"><col class="direction-col-window"><col class="direction-col-sales"></colgroup>
        <thead><tr><th>当前案例</th><th class="num">播放量</th><th class="num">商品点击</th><th class="num">售出件数</th><th class="num">CTOR</th><th>统计窗口</th><th>销售数据表</th></tr></thead><tbody>${(g.observations || []).map(observationRow).join("")}</tbody>
      </table></div>
      <details class="direction-cohorts"><summary>窗口、版本与缺失信息（${(g.cohorts || []).length} 组）</summary>
        ${missing ? `<p>${esc(missing)}</p>` : ""}
        ${(g.cohorts || []).map(c => `<p><strong>${esc(c.evidence_window || "窗口未提供")}</strong> · ${esc(c.script_version || "版本未提供")} · ${esc(evidenceNames[c.evidence_status] || "证据状态未提供")} · 截至 ${esc(c.data_as_of || "未提供")} · ${esc(directionVariantNames[c.variant_type] || "类型未提供")}${c.version_conflict ? " · 版本冲突" : ""} · ${c.case_count} 条<br>${(c.case_ids || []).map(id => `<a href="#case/${esc(id)}">${esc(id)}</a>`).join("、")}</p>`).join("")}
        <p>相同窗口与版本仅方便查找，仍需核对账号、投流、曝光与归因条件。</p>
      </details>
    </section>`;
  }
  const statusNames = {testing:"待测试", active:"测试中", paused:"暂缓", archived:"已归档"};
  const taskStatusNames = {queued:"排队中",running:"处理中",completed:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
  let activeDraft=null;
  function modalOpen() { return !!document.querySelector('#direction-create-dialog[open],#selling-point-dialog[open],#direction-edit-dialog[open],#direction-plan-dialog[open]'); }
  function confirmDiscard(action) {
    if(!activeDraft?.dirty){action();return;}
    let dialog=$("#direction-discard-dialog");
    if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-discard-dialog";document.body.append(dialog);}
    dialog.innerHTML='<div class="confirm-body"><h2>保留这次方向编辑？</h2><p>还有未保存的内容。继续编辑可保留当前草稿。</p><div class="confirm-actions"><button type="button" class="secondary" data-discard>放弃修改</button><button type="button" class="primary" data-keep>继续编辑</button></div></div>';
    dialog.querySelector("[data-keep]").onclick=()=>dialog.close();
    dialog.querySelector("[data-discard]").onclick=()=>{dialog.close();if(activeDraft)activeDraft.dirty=false;action();};
    if(!dialog.open)dialog.showModal();
  }
  function trackDraft(dialog) {
    activeDraft={dialog,hash:location.hash,dirty:false,pending:false};
    dialog.oninput=()=>{if(activeDraft?.dialog===dialog)activeDraft.dirty=true;};
    dialog.oncancel=event=>{event.preventDefault();requestClose(dialog);};
    dialog.onclose=()=>{if(activeDraft?.dialog===dialog)activeDraft=null;};
  }
  function requestClose(dialog) { if(!activeDraft?.pending)confirmDiscard(()=>dialog.close()); }
  function beforeNavigate() {
    if(!activeDraft?.dialog.open)return true;
    const next=location.hash;
    if(next===activeDraft.hash)return true;
    if(activeDraft.pending){history.replaceState(null,"",activeDraft.hash);return false;}
    if(!activeDraft.dirty){activeDraft.dialog.close();return true;}
    history.replaceState(null,"",activeDraft.hash);
    confirmDiscard(()=>{activeDraft?.dialog.close();location.hash=next;});
    return false;
  }
  function directionURL(product, id) {
    const hash="#directions"+(id ? "/"+encodeURIComponent(id) : "");
    return window.WorkbenchShell?.url(hash,product) || hash;
  }
  function routeValues() {
    const hash=pageHash(), params=new URLSearchParams(hash.split("?")[1] || "");
    let id="";
    try {id=decodeURIComponent(hash.split("?")[0].split("/").slice(1).join("/"));} catch {}
    return {hash,params,id,product:window.WorkbenchShell ? window.WorkbenchShell.product() : params.get("product") || "",
      archive:["active","archived","all"].includes(params.get("archive")) ? params.get("archive") : "active"};
  }
  function catalogURL(product,id="") {
    const query=new URLSearchParams();if(product)query.set("product",product);
    return "/api/direction-catalog"+(id?"/"+encodeURIComponent(id):"")+(query.size?"?"+query:"");
  }
  function fieldText(value) { return typeof value === "string" ? value : ""; }
  const progressions=["问题-解决","结果倒叙","逐件对照","回应质疑","过程仪式","身份认同","清单演示"];
  // Same rule as the server: codes must exist in this product's table, no "未编号" links.
  function chainIssues(points,...parts) {
    const codes=new Set(points.map(item=>item.code)),joined=parts.filter(Boolean).join(" → "),issues=[];
    const unknown=[...new Set([...joined.matchAll(/(?<![A-Za-z0-9])([SAB][1-9]\d*)(?!\d)/g)].map(match=>match[1]).filter(code=>!codes.has(code)))];
    if(unknown.length)issues.push("卖点表里没有 "+unknown.join("、"));
    if(joined.includes("未编号"))issues.push("有未编号的环节，请改用卖点编号或删去");
    return issues;
  }
  function performanceText(perf) {
    if(!perf||!perf.video_count)return "";
    if(!perf.matched_count)return `${perf.video_count} 条视频，销售数据未匹配`;
    return `${perf.matched_count} 条视频 · ${perf.orders} 单 · 点击 ${perf.clicks}${perf.ctor_pct!=null?` · CTOR ${perf.ctor_pct}%`:""}${perf.data_through?` · 截至 ${perf.data_through}`:""}`;
  }
  function variantLabel(variant,index) {
    return `${variant.baseline?"基准稿":variant.variant_type==="direction"?"方向定义":"执行变体 "+index}${variant.progression?` · ${variant.progression}`:" · 推进结构未标注"}`;
  }
  function messageMarkup(text) { return `<p class="context-note">${esc(text)}</p>`; }
  function routeTabs(tab) {
    return `<div class="direction-page-tabs" role="tablist" aria-label="卖点与方向视图"><button type="button" role="tab" data-direction-tab="directions" aria-selected="${tab!=="points"}">测试方向</button><button type="button" role="tab" data-direction-tab="points" aria-selected="${tab==="points"}">S / A / B 卖点分级</button></div>`;
  }
  function bindTabs(params) {
    $$("[data-direction-tab]").forEach(button=>button.onclick=()=>{
      const next=new URLSearchParams(params);button.dataset.directionTab==="points"?next.set("tab","points"):next.delete("tab");
      location.hash=pageURL("#directions"+(next.size?"?"+next:""));
    });
  }
  function pointTable(points,directions) {
    if(!points.length)return messageMarkup("当前产品还没有卖点。先新增一个明确的主打卖点，再规划测试方向。");
    return `<div class="selling-point-table-wrap" tabindex="0" role="region" aria-label="卖点分级表"><table class="selling-point-table"><thead><tr><th>分级 / 编号</th><th>卖点与理由</th><th>证据与边界</th><th>测试方向</th><th>主打视频数据</th><th>操作</th></tr></thead><tbody>${points.map(point=>{
      const linked=directions.filter(item=>item.product===point.product&&item.selling_point_id===point.id);
      return `<tr data-point-id="${esc(point.id)}"><td><span class="selling-tier tier-${esc(point.tier)}">${esc(point.code)}</span></td><td><strong>${esc(point.title)}</strong>${point.rationale?`<p>${esc(point.rationale)}</p>`:""}</td><td>${point.evidence?`<p class="point-evidence">${esc(point.evidence)}</p>`:'<span class="missing-value">待补充证据</span>'}${point.grading_mismatch?`<p class="data-warning point-grading-mismatch">与分级表不一致 <button type="button" class="subtle" data-adopt-grading="${esc(point.id)}">核对并采用分级表写法</button></p>`:""}</td><td>${linked.length?linked.map(item=>`<a href="${esc(directionURL(item.product,item.direction_id))}">${esc(item.direction_id)}</a>`).join("<br>"):'<span class="missing-value">尚未建立</span>'}</td><td>${point.performance?.video_count?`<span class="point-performance">${esc(performanceText(point.performance))}</span>`:'<span class="missing-value">暂无视频</span>'}</td><td><button type="button" class="subtle" data-edit-point="${esc(point.id)}">编辑卖点</button><button type="button" class="subtle" data-use-point="${esc(point.id)}">选为主打</button></td></tr>`;
    }).join("")}</tbody></table></div>`;
  }
  function directionCard(direction,group) {
    const rows=group?.observations || [];
    const videoIds=new Set(rows.map(item=>caseIdentity(item).video_id).filter(Boolean));
    const soldVideoIds=new Set(rows.filter(item=>Number(salesMetrics(item).items_sold)>0).map(item=>caseIdentity(item).video_id).filter(Boolean));
    const variants=direction.variants || [];
    return `<article class="direction-overview-card" data-direction-id="${esc(direction.direction_id)}"><div class="direction-card-top"><span class="selling-tier">${esc(direction.direction_id)}</span><span class="direction-status">${esc(statusNames[direction.status] || "已有案例")}</span></div><h2><a href="${esc(directionURL(direction.product,direction.direction_id))}">${esc(direction.main_difference || group?.definitions?.[0]?.main_difference || direction.direction_id)}</a></h2><p class="direction-card-chain">${esc(directionChain(direction) || "方向链待补充")}</p><div class="direction-card-counts"><span><strong>${direction.case_count ?? group?.case_count ?? 0}</strong>关联案例</span><span><strong>${direction.video_count ?? videoIds.size}</strong>关联视频</span><span><strong>${variants.length}</strong>执行变体</span>${direction.script_count?`<span><strong>${direction.script_count}</strong>脚本版本</span>`:""}</div><p class="direction-card-note">${direction.performance?.matched_count?esc(performanceText(direction.performance)):`${soldVideoIds.size} 个案例视频记录过出单 · 各自数据窗口`}</p>${direction.lead_tier&&direction.lead_tier!=="S"?`<p class="data-warning">主打是 ${esc(direction.lead_tier)} 级卖点，不是 S</p>`:""}${(direction.chain_issues||[]).length?`<p class="data-warning">${esc(direction.chain_issues.join("；"))}</p>`:""}<a class="direction-card-link" href="${esc(directionURL(direction.product,direction.direction_id))}">查看方向 ${icon("arrow")}</a></article>`;
  }
  function decorateGroupWarnings(root,groups) {
    $$(".direction-group",root).forEach((node,index)=>{
      const g=groups[index];
      if(g.version_conflict_count)node.querySelector("h2").closest(".section-title-row").insertAdjacentHTML("afterend",`<p class="data-warning">${g.version_conflict_count} 条案例的方向版本与制作版本不同，先核对数据对应稿件。</p>`);
      if(g.duplicate_video_ids?.length)node.querySelector(".direction-observations").insertAdjacentHTML("beforebegin",g.duplicate_video_ids.map(v=>`<div class="data-warning direction-duplicate"><p>${v.cross_direction?"同一视频跨方向登记":"同一视频重复登记"}：${esc(v.video_id)}。${v.cross_direction?"请先核对方向与对应稿件；这些记录不能作为各方向的独立表现样本。":"保留各自当前记录，不重复累计。"}</p><ul>${(v.assignments || []).map(a=>`<li><a href="#case/${esc(a.case_id)}">${esc(a.case_id)}</a> · 原产品名 ${esc(a.product || "未提供")} · 方向 ${esc(a.direction_id || "未记录")}</li>`).join("")}</ul></div>`).join(""));
    });
  }
  let refocusFilter="";
  function renderCatalog(catalog,summary,values) {
    const {params,product,archive}=values, tab=params.get("tab")==="points"?"points":"directions";
    const tier=["S","A","B"].includes(params.get("tier"))?params.get("tier"):"";
    const query=params.get("q") || "", direction=params.get("direction") || "";
    const status=Object.hasOwn(statusNames,params.get("status"))?params.get("status"):"";
    const allDirections=catalog.directions || [], allPoints=catalog.selling_points || [];
    const groups=summary.groups || [], matchGroup=item=>groups.find(group=>group.direction_id===item.direction_id&&(!product||window.WorkbenchShell.matchesProduct(group.product,item.product)));
    const items=allDirections.filter(item=>(!direction||item.direction_id===direction)&&(!status||item.status===status)&&(!tier||(item.direction_id.split("-").pop() || "").startsWith(tier))&&(!query||`${item.direction_id} ${directionChain(item)} ${item.notes || ""}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())));
    const tierRank=code=>"SAB".indexOf(String(code)[0]),codeNo=code=>Number(String(code).slice(1))||0,points=[...allPoints].sort((a,b)=>tierRank(a.code)-tierRank(b.code)||codeNo(a.code)-codeNo(b.code)).filter(item=>(!tier||item.tier===tier)&&(!query||`${item.code} ${item.title} ${item.rationale} ${item.evidence}`.toLocaleLowerCase().includes(query.toLocaleLowerCase())));
    const reset=new URLSearchParams(params);reset.delete("direction");
    const libraryParams=new URLSearchParams({direction:"__unclassified__"});if(archive!=="active")libraryParams.set("archive",archive);
    $("#content").innerHTML=`<div class="page-heading direction-page-heading"><div><span class="eyebrow">SELLING POINTS & DIRECTIONS</span><h1>卖点与方向</h1><p>从主打卖点出发，串起顾虑、收口和每次执行。</p></div><span class="total-note">${allDirections.length} 个方向 · ${allPoints.length} 个卖点</span></div>${routeTabs(tab)}
      <form id="direction-filters" class="direction-catalog-filters"><label><span>查找</span><input name="q" value="${esc(query)}" placeholder="卖点、顾虑或收口"></label><div class="tier-filter-field"><span>分级</span><div class="tier-segment" role="group" aria-label="按分级筛选">${[["","全部"],["S","S"],["A","A"],["B","B"]].map(([value,label])=>`<button type="button" data-tier-filter="${value}" aria-pressed="${tier===value}">${label}<small>${tab==="points"?allPoints.filter(item=>!value||item.tier===value).length:allDirections.filter(item=>!value||(item.direction_id.split("-").pop()||"").startsWith(value)).length}</small></button>`).join("")}</div><input type="hidden" name="tier" value="${tier}"></div>${tab==="directions"?`<label><span>状态</span><select name="status"><option value="">全部状态</option>${Object.entries(statusNames).map(([value,label])=>`<option value="${value}" ${value===status?"selected":""}>${label}</option>`).join("")}</select></label><label><span>方向</span><input name="direction" list="direction-candidates" value="${esc(direction)}" placeholder="完整编号"><datalist id="direction-candidates">${allDirections.map(item=>`<option value="${esc(item.direction_id)}"></option>`).join("")}</datalist></label><label><span>案例范围</span><select name="archive">${Object.entries({active:"使用中",archived:"已归档",all:"全部"}).map(([value,label])=>`<option value="${value}" ${value===archive?"selected":""}>${label}</option>`).join("")}</select></label>`:""}</form>
      <div class="direction-section-heading"><p class="context-note">${tab==="points"?"S / A / B 保留产品已有分级与依据。":"同一主打卖点共用方向编号；改变顾虑或收口，保留为执行变体。"}</p><button type="button" class="secondary compact" id="${tab==="points"?"new-selling-point":"new-direction"}">${icon("plus")}${tab==="points"?"新增卖点":"新建方向"}</button></div>
      ${tab==="points"?pointTable(points,allDirections):items.length?`<div class="direction-card-grid">${items.map(item=>directionCard(item,matchGroup(item))).join("")}</div>`:`<section class="section-card empty direction-empty"><h3>${direction?"当前筛选没有匹配的方向":query||tier||status?"当前筛选没有匹配的方向":"当前产品与范围内没有已记录方向"}</h3><p>可清除筛选，或从一个主打卖点开始建立方向。</p>${direction?`<a class="secondary" href="${esc(pageURL("#directions"+(reset.size?"?"+reset:"")))}">清除方向筛选</a>`:""}</section>`}
      <div class="direction-summary-notes">${leadTierLine(summary.lead_tier_counts)}<p>范围内未记录方向 ${summary.unclassified_case_ids?.length || 0} 条${summary.unclassified_case_ids?.length?` · <a href="${esc(pageURL("#library?"+libraryParams))}">查看案例</a>`:""}。</p><details><summary>数据与复盘说明</summary><ul>${(summary.limitations || []).map(note=>`<li>${esc(note)}</li>`).join("")}</ul><p>关联视频去重计数；出单视频只表示其自身窗口内有售出记录，不累计不同窗口的出单量。</p></details></div>`;
    bindTabs(params);
    $("#direction-filters").onsubmit=event=>{
      event.preventDefault();const next=new URLSearchParams(),form=new FormData(event.currentTarget);
      if(tab==="points")next.set("tab","points");
      for(const [key,value] of form){const text=String(value).trim();if(text&&!(key==="archive"&&text==="active"))next.set(key,text);}
      const target=pageURL("#directions"+(next.size?"?"+next:""));if(location.hash===target)void route();else location.hash=target;
    };
    $$("[data-tier-filter]").forEach(button=>button.onclick=()=>{
      const form=$("#direction-filters");form.elements.tier.value=button.dataset.tierFilter;form.requestSubmit();
    });
    const filterForm=$("#direction-filters");let filterTimer=0,composing=false;
    const applyFilters=(delay=0)=>{clearTimeout(filterTimer);filterTimer=setTimeout(()=>{if(composing)return;refocusFilter=document.activeElement?.name||"";filterForm.requestSubmit();},delay);};
    filterForm.addEventListener("compositionstart",()=>{composing=true;});
    filterForm.addEventListener("compositionend",()=>{composing=false;applyFilters(300);});
    filterForm.addEventListener("input",event=>{if(event.target.matches("input[type=text],input:not([type])"))applyFilters(400);});
    filterForm.addEventListener("change",event=>{if(event.target.matches("select"))applyFilters();});
    filterForm.addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.isComposing){event.preventDefault();applyFilters();}});
    if(refocusFilter){const field=filterForm.elements[refocusFilter];refocusFilter="";if(field?.focus){field.focus();if(field.setSelectionRange){const end=field.value.length;field.setSelectionRange(end,end);}}}
    $$(".point-evidence").forEach(box=>{
      if(box.scrollHeight<=box.clientHeight+2)return;
      const toggle=document.createElement("button");toggle.type="button";toggle.className="subtle point-evidence-toggle";toggle.textContent="展开";
      toggle.onclick=()=>{const open=box.classList.toggle("expanded");toggle.textContent=open?"收起":"展开";};
      box.after(toggle);
    });
    $("#new-direction")?.addEventListener("click",()=>openCreate());
    $("#new-selling-point")?.addEventListener("click",()=>openPointEditor());
    $$("[data-edit-point]").forEach(button=>button.onclick=()=>openPointEditor(button.dataset.editPoint));
    $$("[data-adopt-grading]").forEach(button=>button.onclick=()=>openPointEditor(button.dataset.adoptGrading,true));
    $$("[data-use-point]").forEach(button=>button.onclick=()=>openCreate(button.dataset.usePoint));
  }
  function renderDetail(data,values) {
    const d=data.direction,p=data.selling_point,group=data.group;
    const variants=d.variants || [], scripts=data.scripts || [],videos=data.videos || [],tasks=data.tasks || [],cases=data.cases || [];
    const section=(id,title,body)=>`<section class="section-card direction-detail-section" id="${id}"><h2>${title}</h2>${body}</section>`;
    $("#breadcrumb").innerHTML=`工作空间 <span>/</span> 卖点与方向 <span>/</span> ${esc(d.direction_id)}`;
    $("#content").innerHTML=`<a class="back-link" href="${esc(directionURL(d.product))}">${icon("back")}全部方向</a><div class="page-heading direction-page-heading"><div><span class="eyebrow">${esc(d.product)} · ${esc(statusNames[d.status] || "已有案例")}</span><h1>${esc(d.direction_id)}</h1><p>${esc(d.main_difference)}</p></div><div class="direction-dialog-actions"><button type="button" class="primary" id="direction-write">${icon("edit")}用这个方向写稿</button><button type="button" class="secondary" id="direction-edit">${icon("edit")}编辑方向</button><button type="button" class="secondary" id="direction-add-variant">${icon("plus")}新增执行变体</button></div></div>
      <nav class="direction-detail-links" aria-label="方向详情内容">${[["direction-definition","方向与变体"],["direction-scripts","文案版本"],["direction-videos","关联视频"],["direction-tasks","AI 任务"],["direction-cases","关联案例"]].map(([id,label])=>`<button type="button" class="subtle" data-direction-section="${id}">${label}</button>`).join("")}</nav>
      ${section("direction-definition","方向与执行变体",`<p class="direction-chain">${esc(directionChain(d))}</p>${p?`<p class="context-note">主打卖点 ${esc(p.code)} · ${esc(p.title)}</p>${p.evidence?`<p class="direction-point-evidence">${esc(p.evidence)}</p>`:""}`:""}${d.notes?`<p>${esc(d.notes)}</p>`:""}${(d.chain_issues||[]).length?`<p class="data-warning">方向链需要修改：${esc(d.chain_issues.join("；"))}</p>`:""}${d.lead_tier&&d.lead_tier!=="S"?`<p class="data-warning">主打是 ${esc(d.lead_tier)} 级卖点。方向应由 S 级卖点主打。</p>`:""}${!variants.some(item=>item.baseline)&&variants.length?'<p class="data-warning">还没有基准稿。选一条设为基准，其他变体才有比较对象。</p>':""}<div class="direction-variant-list">${variants.map((variant,index)=>`<div class="direction-definition${variant.baseline?" is-baseline":""}"><strong>${esc(variantLabel(variant,index+1))}</strong><p class="direction-chain">${esc(directionChain(variant))}</p>${variant.execution_change?`<p>${esc(variant.execution_change)}</p>`:""}${(variant.chain_issues||[]).length?`<p class="context-note">${esc(variant.chain_issues.join("；"))}（已发布的记录保持原样）</p>`:""}<div class="direction-variant-actions">${variant.baseline?"":`<button type="button" class="subtle" data-variant-baseline="${index}">设为基准稿</button>`}<label class="subtle">推进结构 <select data-variant-progression="${index}"><option value="">未标注</option>${progressions.map(item=>`<option ${variant.progression===item?"selected":""}>${esc(item)}</option>`).join("")}${variant.progression&&!progressions.includes(variant.progression)?`<option selected>${esc(variant.progression)}</option>`:""}</select></label></div></div>`).join("")}</div>${!variants.length?messageMarkup("暂未追加执行变体。已有案例仍保留各自的完整方向链。"):""}${(d.history||[]).length?`<details class="direction-history"><summary>旧定义 · ${d.history.length}</summary>${d.history.map(item=>`<div class="direction-definition"><small>${esc(readableDate(item.replaced_at))} 前</small><p class="direction-chain">${esc(directionChain(item))}</p></div>`).join("")}</details>`:""}`)}
      ${section("direction-plans","下一轮安排",`<div class="direction-dialog-actions">${[["scale","加码"],["execution","换执行"],["stop","停测"]].map(([action,label])=>`<button type="button" class="secondary" data-direction-plan="${action}">${label}</button>`).join("")}</div><p class="context-note">把本轮判断保存为下一轮安排，开始写稿时再明确提交。</p><div class="direction-related-list">${(data.plans || []).map(plan=>`<div class="direction-related-item"><strong>${esc({scale:"加码",execution:"换执行",stop:"停测"}[plan.action] || plan.action)}</strong><span>${esc({pending:"待处理",working:"进行中",done:"已完成",cancelled:"已取消"}[plan.status] || plan.status)}</span>${plan.note?`<small>${esc(plan.note)}</small>`:""}</div>`).join("")}</div>`)}
      ${section("direction-scripts",`文案版本 · ${scripts.length}`,scripts.length?`<div class="direction-related-list">${scripts.map(script=>script.script_id || script.id?`<a class="direction-related-item" href="${esc(pageURL("#scripts/"+(script.script_id || script.id)))}"><strong>${esc(script.title)}</strong><span>第 ${esc(script.version || 1)} 版 · ${esc({draft:"草稿",adopted:"已采纳",filmed:"已拍摄",published:"已发布",reviewed:"已复盘",archived:"已归档"}[script.status] || script.status || "")}</span><small>${esc(script.script_id || script.id)}</small></a>`:`<details class="direction-script"><summary><strong>${esc(script.script_version || "版本未标记")}</strong> ${esc(script.title || script.case_id)}</summary><pre>${esc(script.text || "未记录文案")}</pre><a href="${esc(pageURL("#case/"+script.case_id+"/script"))}">查看案例文案</a></details>`).join("")}</div>`:messageMarkup("还没有关联的案例文案版本。"))}
      ${section("direction-videos",`关联视频 · ${videos.length}`,videos.length?`<div class="direction-related-list">${videos.map(video=>`<a class="direction-related-item" href="${esc(pageURL("#videos?video="+encodeURIComponent(video.video_id)))}"><strong>${esc(video.title || video.video_id)}</strong><span>${esc(video.video_id)}</span><small>查看销售数据与统计窗口</small></a>`).join("")}</div>`:messageMarkup("还没有关联视频。"))}
      ${section("direction-tasks",`AI 任务 · ${tasks.length}`,tasks.length?`<div class="direction-related-list">${tasks.map(task=>`<a class="direction-related-item" href="${esc(pageURL("#ai/"+encodeURIComponent(task.task_id)))}"><strong>${esc(task.case_title || task.case_id || task.task_id)}</strong><span>${esc(taskStatusNames[task.status] || task.status || "状态未提供")}</span><small>${esc(readableDate(task.updated_at))}</small></a>`).join("")}</div>`:messageMarkup("还没有关联的 AI 协作任务。"))}
      ${section("direction-cases",`关联案例 · ${cases.length}`,cases.length?`<div class="direction-related-list">${cases.map(item=>`<a class="direction-related-item" href="${esc(pageURL("#case/"+item.case_id))}"><strong>${esc(item.current?.title || item.title || item.case_id)}</strong><span>${esc(item.case_id)}${item.archived?" · 已归档":""}</span></a>`).join("")}</div>`:messageMarkup("方向已经独立保存，可以在后续案例中使用这个编号。"))}
      <div class="direction-summary-notes"><details><summary>数据与复盘说明</summary><p>各案例保留各自文案版本、统计窗口与数据来源，不累计不同窗口的售出量。重复登记的视频也保留原始归属提示。</p></details></div>${group?`<div class="direction-section-heading"><h2>案例执行与表现</h2><p class="context-note">每条案例保留自己的方向链、文案版本与统计窗口。</p></div>${groupCard(group)}`:""}`;
    if(group)decorateGroupWarnings($("#content"),[group]);
    $("#direction-add-variant").onclick=()=>openCreate(d.selling_point_id);
    $("#direction-edit").onclick=()=>openDirectionEditor(data);
    const saveMeta=async(values)=>{
      try{await api("/api/direction-catalog",{method:"POST",body:{revision:data.revision,request_id:crypto.randomUUID(),operation:"variant_meta",direction:{product:d.product,direction_id:d.direction_id,...values}}});toast("变体标注已保存");}
      catch(error){toast(error.message,true);}
      void route();
    };
    $$("[data-variant-baseline]").forEach(button=>button.onclick=()=>saveMeta({index:Number(button.dataset.variantBaseline),baseline:true}));
    $$("[data-variant-progression]").forEach(select=>select.onchange=()=>{if(select.value)saveMeta({index:Number(select.dataset.variantProgression),progression:select.value});});
    $("#direction-write").onclick=()=>window.CaseAI?.startFromDirection(d);
    $$("[data-direction-plan]").forEach(button=>button.onclick=()=>openPlan(data,button.dataset.directionPlan));
    $$("[data-direction-section]").forEach(button=>button.onclick=()=>document.getElementById(button.dataset.directionSection)?.scrollIntoView({behavior:"smooth",block:"start"}));
  }
  async function openPlan(data,action) {
    const labels={scale:"加码",execution:"换执行",stop:"停测"}, d=data.direction;
    let dialog=$("#direction-plan-dialog");if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-plan-dialog";document.body.append(dialog);}
    dialog.innerHTML=`<form><div class="dialog-heading"><h2>${labels[action]} · ${esc(d.direction_id)}</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body"><label class="field"><span>本轮观察与下一步要求</span><textarea name="note" rows="4" required maxlength="12000" placeholder="写下数据窗口、观察和这次决定的理由"></textarea></label><p class="context-note">保存到本周首页的下一轮安排，保留这次判断。</p><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存安排</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));dialog.showModal();trackDraft(dialog);
    let pending=false,requestId=crypto.randomUUID(),catalog;
    try{catalog=await api("/api/creation?product="+encodeURIComponent(d.product));}catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
    dialog.querySelector("form").onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
      try{
        if(!catalog)catalog=await api("/api/creation?product="+encodeURIComponent(d.product));
        await api("/api/plans",{method:"POST",body:{revision:catalog.revision,request_id:requestId,plan:{product:d.product,direction_id:d.direction_id,action,status:"pending",note:dialog.querySelector("textarea").value.trim()}}});
        dialog.close();toast("已保存到下一轮安排");void route();
      }catch(error){dialog.querySelector("[role=alert]").textContent=error.message;if(error.status===409){try{catalog=await api("/api/creation?product="+encodeURIComponent(d.product));requestId=crypto.randomUUID();dialog.querySelector("[role=alert]").textContent+=" 已读取最新记录，输入保留，请核对后再次保存。";}catch{}}}
      finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
    };
  }
  async function openCreate(preselected="") {
    const product=window.WorkbenchShell?.product() || routeValues().product;
    if(!product){toast("请先在左侧选择一个产品，再新建方向。",true);return;}
    let dialog=$("#direction-create-dialog");
    if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-create-dialog";dialog.setAttribute("aria-labelledby","direction-create-title");document.body.append(dialog);}
    dialog.innerHTML='<div class="dialog-heading"><h2 id="direction-create-title">新建方向</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><p class="direction-dialog-loading" role="status">正在读取产品卖点…</p>';
    dialog.querySelector("[data-close]").onclick=()=>requestClose(dialog);dialog.showModal();trackDraft(dialog);
    let catalog,materials;
    try {[catalog,materials]=await Promise.all([api(catalogURL(product)),api("/api/materials?product="+encodeURIComponent(product)).catch(()=>null)]);}catch(error){dialog.querySelector("[role=status]").textContent=error.message;return;}
    if(!dialog.open)return;
    const points=catalog.selling_points || [],directions=catalog.directions || [];
    const draft={point_id:preselected,concern:"",closing:"",lead_reason:"",progression:"",progression_other:"",execution_change:""};let step=0,pending=false,requestId=crypto.randomUUID();
    const point=()=>points.find(item=>item.id===draft.point_id);
    const read=name=>dialog.querySelector(`[name="${name}"]`)?.value.trim() ?? draft[name];
    const collect=()=>{if(step===0){draft.point_id=dialog.querySelector('[name="lead_point"]:checked')?.value || "";draft.lead_reason=read("lead_reason");}if(step===1){draft.concern=read("concern");draft.progression=read("progression");draft.progression_other=read("progression_other");draft.execution_change=read("execution_change");}if(step===2)draft.closing=dialog.querySelector('[name="closing"]').value.trim();};
    const progressionValue=()=>draft.progression==="其他"?(draft.progression_other?"其他："+draft.progression_other:""):draft.progression;
    const choice=item=>`<label class="direction-point-choice"><input type="radio" name="lead_point" value="${esc(item.id)}" ${draft.point_id===item.id?"checked":""}><span><strong>${esc(item.code)} · ${esc(item.title)}</strong>${item.rationale?`<small>${esc(item.rationale)}</small>`:""}</span></label>`;
    function progressionFields(){
      const chosen=point(),existing=chosen&&directions.find(item=>item.direction_id===`${product}-${chosen.code}`);
      const used=new Set((existing?.variants || []).map(item=>item.progression).filter(Boolean));
      return `<div class="direction-progression"><label class="field"><span>推进结构${existing?"":"（可选，基准稿）"}</span><select name="progression"><option value="">${existing?"请选择":"暂不标注"}</option>${progressions.map(item=>`<option value="${esc(item)}" ${used.has(item)?"disabled":""} ${draft.progression===item?"selected":""}>${esc(item)}${used.has(item)?"（已有）":""}</option>`).join("")}<option value="其他" ${draft.progression==="其他"?"selected":""}>其他</option></select></label><label class="field direction-progression-other"><span>其他结构说明</span><input name="progression_other" maxlength="60" value="${esc(draft.progression_other)}" placeholder="选“其他”时填写"></label>${existing?`<label class="field"><span>和基准稿比，推进上改了什么</span><textarea name="execution_change" rows="2" maxlength="4000" placeholder="例如：先给一排刀切纸的结果，再倒回讲以前用几样东西">${esc(draft.execution_change)}</textarea></label><p class="context-note">只换开头一句不算新变体；已用过的推进结构不能再选。</p>`:""}</div>`;
    }
    function render(){
      const chosen=point(),id=chosen?`${product}-${chosen.code}`:"选择主打后自动生成",existing=directions.find(item=>item.direction_id===id);
      const main=existing?.main_difference || (chosen?chosen.code+" "+chosen.title:"");
      const linkedThemes=(materials?.theme_links || []).filter(link=>link.selling_point_id===chosen?.id || link.direction_id===id || link.target_id===chosen?.id || link.target_id===id);
      const reviewConcerns=linkedThemes.map(link=>link.concern || link.source_quote || link.theme).filter(Boolean);
      const candidates=[...new Set(directions.flatMap(item=>[item,...(item.variants || [])]).map(item=>fieldText(item[step===1?"concern":"closing"]).trim()).filter(Boolean))];
      dialog.innerHTML=`<form><div class="dialog-heading"><div><h2 id="direction-create-title">${existing?"新增执行变体":"新建方向"}</h2><p>${esc(product)} · 同主打卖点共用方向编号</p></div><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><ol class="direction-wizard-steps">${["选择主打","回应顾虑","确定收口"].map((label,index)=>`<li aria-current="${step===index?"step":"false"}">${index+1} ${label}</li>`).join("")}</ol><div class="direction-wizard-body"><p class="direction-number">方向编号 <strong id="direction-generated-id">${esc(id)}</strong></p>${step===0?`<fieldset class="direction-lead-picker"><legend>哪个 S 级卖点主打？</legend>${points.length?points.filter(item=>item.tier==="S").map(choice).join("")+(points.some(item=>item.tier!=="S")?`<details class="direction-non-s" ${chosen&&chosen.tier!=="S"?"open":""}><summary>用 A / B 级卖点主打（需写理由）</summary>${points.filter(item=>item.tier!=="S").map(choice).join("")}</details>`:""):'<p class="context-note">这个产品还没有卖点，请先到卖点分级页新增。</p>'}</fieldset>${chosen&&chosen.tier!=="S"&&!existing?`<label class="field"><span>为什么用 ${esc(chosen.code)} 主打</span><textarea name="lead_reason" rows="2" maxlength="2000" placeholder="分级表要求每条内容由一个 S 主导；写明这次为什么例外">${esc(draft.lead_reason)}</textarea></label>`:""}`:`${step===1?progressionFields():""}<label class="field"><span>${step===1?"观众顾虑与回应顺序":"最后怎样收口？"}</span><textarea name="${step===1?"concern":"closing"}" rows="4" required maxlength="4000" placeholder="${step===1?"例如：装夹麻烦吗 → A3 一次装夹翻面继续 → 值不值":"例如：B3 3 年有限质保，或无购买收口"}">${esc(step===1?draft.concern:draft.closing)}</textarea></label>${step===1&&reviewConcerns.length?`<div class="direction-suggestions"><span>已关联评论顾虑 · 评论说法待核实</span>${reviewConcerns.map((text,index)=>`<button type="button" class="secondary" data-review-concern="${index}">${esc(text)}</button>`).join("")}</div>`:""}${candidates.length?`<div class="direction-suggestions"><span>点选已有表达</span>${candidates.map((text,index)=>`<button type="button" class="secondary" data-suggestion="${index}">${esc(text)}</button>`).join("")}</div>`:""}<div class="direction-suggestions"><span>点选卖点，追加到${step===1?"顾虑回应":"收口"}</span>${points.map((item,index)=>`<button type="button" class="secondary" data-chain-point="${index}">${esc(item.code)} · ${esc(item.title)}</button>`).join("")}</div>${step===2?`<div class="direction-chain-preview"><strong>完整方向链</strong><p>${esc([main,draft.concern,draft.closing].filter(Boolean).join(" → "))}</p></div>`:""}`}${existing?'<p class="context-note">这个主打卖点已有方向。本次顾虑与收口会作为执行变体保留。</p>':""}<p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span>独立保存方向，后续再关联案例与视频。</span><div><button type="button" class="secondary" data-back>${step?"上一步":"取消"}</button><button type="submit" class="primary" ${!points.length?"disabled":""}>${step===2?"保存方向":"下一步"}</button></div></div></form>`;
      dialog.querySelector("[data-close]").onclick=()=>{if(!pending)requestClose(dialog);};
      dialog.querySelector("[data-back]").onclick=()=>{if(pending)return;collect();if(step){step--;render();}else requestClose(dialog);};
      dialog.querySelectorAll('[name="lead_point"]').forEach(input=>input.onchange=()=>{draft.point_id=input.value;dialog.querySelector("#direction-generated-id").textContent=`${product}-${point().code}`;});
      dialog.querySelectorAll("[data-review-concern]").forEach(button=>button.onclick=()=>{const textarea=dialog.querySelector("textarea");textarea.value=[textarea.value.trim(),reviewConcerns[Number(button.dataset.reviewConcern)]].filter(Boolean).join(" → ");textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      dialog.querySelectorAll("[data-suggestion]").forEach(button=>button.onclick=()=>{const textarea=dialog.querySelector("textarea");textarea.value=candidates[Number(button.dataset.suggestion)];textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      dialog.querySelectorAll("[data-chain-point]").forEach(button=>button.onclick=()=>{const textarea=dialog.querySelector("textarea"),item=points[Number(button.dataset.chainPoint)];textarea.value=[textarea.value.trim(),item.code+" "+item.title].filter(Boolean).join(" → ");textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      if(step===2)dialog.querySelector("textarea").oninput=event=>{draft.closing=event.target.value;dialog.querySelector(".direction-chain-preview p").textContent=[main,draft.concern,draft.closing].filter(Boolean).join(" → ");};
      dialog.querySelector("form").onsubmit=async event=>{
        event.preventDefault();if(pending)return;collect();
        if(!point()){dialog.querySelector("[role=alert]").textContent="请选择一个主打卖点。";return;}
        const isExisting=directions.some(item=>item.direction_id===`${product}-${point().code}`);
        if(step===0&&point().tier!=="S"&&!isExisting&&!draft.lead_reason){dialog.querySelector("[role=alert]").textContent=`${point().code} 不是 S 级卖点，请写明用它主打的理由。`;return;}
        if(step===1){
          if(isExisting&&(!progressionValue()||!draft.execution_change)){dialog.querySelector("[role=alert]").textContent="请选择推进结构，并写明推进上改了什么。";return;}
          const issues=chainIssues(points,draft.concern);if(issues.length){dialog.querySelector("[role=alert]").textContent="顾虑链需要修改："+issues.join("；");return;}
        }
        if(step===2){const issues=chainIssues(points,draft.closing);if(issues.length){dialog.querySelector("[role=alert]").textContent="收口需要修改："+issues.join("；");return;}}
        if(step<2){step++;render();return;}
        pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
        try {
          const result=await api("/api/direction-catalog",{method:"POST",body:{revision:catalog.revision,request_id:requestId,operation:"add_variant",direction:{product,selling_point_id:point().id,main_difference:main,concern:draft.concern,closing:draft.closing,status:"testing",...(progressionValue()?{progression:progressionValue()}:{}),...(draft.execution_change?{execution_change:draft.execution_change}:{}),...(draft.lead_reason?{lead_reason:draft.lead_reason}:{})}}});
          dialog.close();toast(result.existing?"执行变体已保存":"方向已保存");
          const target=directionURL(product,result.direction.direction_id);if(location.hash===target)void route();else location.hash=target;
        }catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
        finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
      };
    }
    render();
  }
  function openDirectionEditor(data) {
    const direction=data.direction,requestId=crypto.randomUUID();
    let dialog=$("#direction-edit-dialog");if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-edit-dialog";document.body.append(dialog);}
    dialog.innerHTML=`<form><div class="dialog-heading"><div><h2>编辑方向</h2><p>${esc(direction.direction_id)} · ${esc(direction.main_difference)}</p></div><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body"><label class="field"><span>默认顾虑链</span><textarea name="concern" rows="3" required maxlength="4000">${esc(direction.concern || "")}</textarea></label><label class="field"><span>默认收口</span><textarea name="closing" rows="3" required maxlength="4000">${esc(direction.closing || "")}</textarea></label><label class="field"><span>方向状态</span><select name="status">${Object.entries(statusNames).map(([value,label])=>`<option value="${value}" ${direction.status===value?"selected":""}>${label}</option>`).join("")}</select></label><label class="field"><span>方向备注</span><textarea name="notes" rows="3" maxlength="4000">${esc(direction.notes || "")}</textarea></label><p class="context-note">直接改方向的默认定义。旧定义进入“旧定义”记录，不会变成执行变体；案例当时的执行记录不变。</p><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存定义</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));
    let pending=false;
    dialog.querySelector("form").onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      const values=Object.fromEntries(new FormData(event.currentTarget));for(const key of Object.keys(values))values[key]=String(values[key]).trim();
      pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
      try {await api("/api/direction-catalog",{method:"POST",body:{revision:data.revision,request_id:requestId,operation:"update",direction:{...direction,...values}}});dialog.close();toast("方向定义已更新，旧定义已留档");void route();}
      catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
      finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
    };
    dialog.showModal();trackDraft(dialog);
  }
  async function openPointEditor(id="",adoptGrading=false) {
    const product=window.WorkbenchShell?.product() || routeValues().product;
    if(!product){toast("请先选择一个产品，再维护卖点。",true);return;}
    const openingHash=location.hash;
    let catalog;try{catalog=await api(catalogURL(product));}catch(error){toast(error.message,true);return;}
    if(location.hash!==openingHash)return;
    const found=(catalog.selling_points || []).find(item=>item.id===id) || {},requestId=crypto.randomUUID();
    const prior=adoptGrading&&found.grading_row?{...found,title:found.grading_row.title,rationale:found.grading_row.rationale,evidence:found.grading_row.evidence}:found;
    delete prior.grading_row;delete prior.grading_mismatch;delete prior.performance;
    let dialog=$("#selling-point-dialog");if(!dialog){dialog=document.createElement("dialog");dialog.id="selling-point-dialog";document.body.append(dialog);}
    dialog.innerHTML=`<form><div class="dialog-heading"><h2>${adoptGrading?"采用分级表写法":id?"编辑卖点":"新增卖点"}</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body"><label class="field"><span>卖点编号</span><input name="code" pattern="[SAB][1-9][0-9]*" placeholder="例如 S1、A2、B3" required value="${esc(prior.code || "")}" ${id?"readonly":""}></label><label class="field"><span>卖点标题</span><input name="title" required maxlength="200" value="${esc(prior.title || "")}"></label><label class="field"><span>分级理由</span><textarea name="rationale" rows="3" maxlength="4000">${esc(prior.rationale || "")}</textarea></label><label class="field"><span>证据与边界</span><textarea name="evidence" rows="3" maxlength="4000">${esc(prior.evidence || "")}</textarea></label><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span>编号首字母对应 S / A / B 分级。</span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存卖点</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));
    let pending=false;dialog.querySelector("form").onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      const values=Object.fromEntries(new FormData(event.currentTarget));for(const key of Object.keys(values))values[key]=String(values[key]).trim();
      pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
      try {await api("/api/selling-points",{method:"POST",body:{revision:catalog.revision,request_id:requestId,point:{...prior,...values,product,tier:values.code[0]}}});dialog.close();toast("卖点已保存");void route();}
      catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
      finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);}
    };dialog.showModal();trackDraft(dialog);
  }
  async function route() {
    const sequence=++requestSequence,values=routeValues();
    $("#breadcrumb").innerHTML="工作空间 <span>/</span> 卖点与方向";
    $("#content").innerHTML='<p role="status">正在读取卖点与方向…</p>';
    try {
      if(values.id){
        const data=await api(catalogURL(values.product,values.id));
        if(sequence!==requestSequence||pageHash()!==values.hash)return;
        renderDetail(data,values);return;
      }
      const query=new URLSearchParams({archive:values.archive});if(values.product)query.set("product",values.product);
      const [catalog,summary]=await Promise.all([api(catalogURL(values.product)),api("/api/directions?"+query)]);
      if(sequence!==requestSequence||pageHash()!==values.hash)return;
      window.WorkbenchShell?.registerProducts([...(catalog.selling_points || []),...(catalog.directions || []),...(summary.products || [])]);
      renderCatalog(catalog,summary,values);
    }catch(error){if(sequence===requestSequence&&pageHash()===values.hash)showError(error.message);}
  }
  window.addEventListener("workbench:directionchange",()=>{if(state.routeKind==="directions"&&!modalOpen())void route();});
  return {route,openCreate,openPointEditor,modalOpen,beforeNavigate};
})();
