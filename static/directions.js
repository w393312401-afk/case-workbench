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
    if(id){query.set("view","page");query.set("include_tasks","0");}
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
  // Point status sits beside S/A/B: 有销量 comes from sales, the rest is set by hand.
  const pointStatusNames={testing:"在测",candidate:"待核候选",merged:"已并入"};
  const pointStatusTips={selling:"主打它的视频有订单",testing:"已有方向或写过稿，还在测试",candidate:"还没核实或没有证明素材，写稿前先补证明",merged:"同一产品事实已并入其他编号，编号不复用"};
  const pointStatus=point=>!point?"":point.status==="merged"?"merged":point.performance?.orders>0?"selling":point.status||"";
  const parkedPoint=point=>["candidate","merged"].includes(pointStatus(point));
  function pointStatusChip(point) {
    const key=pointStatus(point);if(!key)return "";
    const verified=key==="testing"&&point.tier!=="S";
    if(verified)return "";
    const label=key==="selling"?"有销量":key==="merged"?`已并入 ${point.merged_into||""}`.trim():verified?"已核":pointStatusNames[key];
    return `<span class="dir-point-status is-${key}" title="${esc(verified?"已核实，可以用来回应顾虑":pointStatusTips[key])}">${esc(label)}</span>`;
  }
  function variantLabel(variant,index) {
    return `${variant.baseline?"基准稿":variant.variant_type==="direction"?"方向定义":"执行变体 "+index}${variant.progression?` · ${variant.progression}`:" · 推进结构未标注"}`;
  }
  // Direction notes are "标签：内容" lines; only 核心证明 and 不能说 change what gets written, the rest folds.
  function notesMarkup(notes) {
    const lines=String(notes||"").split(/\n+/).map(line=>line.trim()).filter(Boolean);
    if(!lines.length)return "";
    const key=lines.filter(line=>/^(核心证明|不能说)[：:]/.test(line)),rest=lines.filter(line=>!key.includes(line));
    const keyMarkup=key.map(line=>{const [label,...body]=line.split(/[：:]/);return `<p class="dir-note dir-note-key"><strong>${esc(label)}</strong> ${esc(body.join("："))}</p>`;}).join("");
    if(!key.length&&lines.length===1&&lines[0].length<=80)return `<p class="dir-note">${esc(lines[0])}</p>`;
    return keyMarkup+(rest.length?`<details class="dir-notes-more"><summary>备注与依据 · ${rest.length} 条</summary>${rest.map(line=>`<p>${esc(line)}</p>`).join("")}</details>`:"");
  }
  function messageMarkup(text) { return `<p class="context-note">${esc(text)}</p>`; }
  const statusTone={active:"good",testing:"",paused:"warn",archived:""};
  // Only a paused or archived direction needs a label; 待测试/测试中 repeated the point status on almost every row.
  const statusChip=status=>["paused","archived"].includes(status)?`<span class="dir-status${statusTone[status]?" is-"+statusTone[status]:""}" title="人工设置的方向状态">${esc(statusNames[status])}</span>`:"";
  const tierRank=code=>"SAB".indexOf(String(code)[0]),codeNo=code=>Number(String(code).slice(1))||0;
  // Selling-point codes inside a chain become small tags; hovering one shows the point it stands for.
  function chainText(text,points=[]) {
    return esc(text||"").replace(/(?<![A-Za-z0-9])([SAB][1-9]\d*)(?!\d)/g,code=>{const point=points.find(item=>item.code===code);return `<span class="dir-ref tier-${code[0]}"${point?` title="${esc(point.title)}"`:""}>${code}</span>`;});
  }
  function chainSteps(item,points) {
    return [["主打",item.main_difference],["顾虑",item.concern],["收口",item.closing]].filter(([,value])=>String(value||"").trim())
      .map(([label,value])=>`<div class="dir-step"><span>${label}</span><p>${chainText(value,points)}</p></div>`).join("");
  }
  // One selling point with the direction built on it (a point has at most one), or a direction whose point is gone.
  function pointRow(point,direction) {
    const href=direction?directionURL(direction.product,direction.direction_id):"";
    const facts=direction?[`${(direction.variants||[]).length} 个变体`,direction.performance?.video_count?`${direction.performance.video_count} 条视频`:""]:[];
    if(point&&!point.evidence)facts.push("待补充证据");
    const evidence=point?.evidence?`<details class="dir-evidence"><summary>证据与边界</summary><p>${esc(point.evidence)}</p></details>`:"";
    const flags=[pointStatusChip(point),direction?.lead_tier&&direction.lead_tier!=="S"?`<span class="dir-flag" title="${esc(direction.notes||"方向应由 S 级卖点主打")}">${esc(direction.lead_tier)} 级主打</span>`:"",
      (direction?.chain_issues||[]).length?`<span class="dir-flag" title="${esc(direction.chain_issues.join("；"))}">方向链待修改</span>`:"",
      point?.grading_mismatch?`<span class="dir-flag" title="与分级表写法不一致">分级表不一致</span>`:""].join("");
    const title=point?point.title:(direction.main_difference||direction.direction_id);
    const menu=point?`<details class="dir-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-edit-point="${esc(point.id)}">编辑卖点</button>${!direction&&point.tier!=="S"&&point.status!=="merged"?`<button type="button" role="menuitem" data-use-point="${esc(point.id)}">用它建立方向（需写理由）</button>`:""}${point.grading_mismatch?`<button type="button" role="menuitem" data-adopt-grading="${esc(point.id)}">采用分级表写法</button>`:""}</div></details>`:"";
    return `<li class="dir-row${direction?" has-direction":""}"${point?` data-point-id="${esc(point.id)}"`:""}${direction?` data-direction-id="${esc(direction.direction_id)}"`:""}>
      <span class="dir-code tier-${esc(point?point.tier:(direction.direction_id.split("-").pop()||"?")[0])}">${esc(point?point.code:"—")}</span>
      <div class="dir-main">${direction?`<a class="dir-title direction-card-link" href="${esc(href)}">${esc(title)}</a>`:`<span class="dir-title">${esc(title)}</span>`}
        <div class="dir-sub">${facts.some(Boolean)?`<span>${facts.filter(Boolean).map(esc).join(" · ")}</span>`:""}${evidence}</div>${flags?`<div class="dir-flags">${flags}</div>`:""}</div>
      <div class="dir-side">${direction?statusChip(direction.status):point?.status==="merged"||point?.tier!=="S"?"":`<button type="button" class="subtle dir-build" data-use-point="${esc(point.id)}">建立方向 ›</button>`}${menu}</div></li>`;
  }
  function decorateGroupWarnings(root,groups) {
    $$(".direction-group",root).forEach((node,index)=>{
      const g=groups[index];
      if(g.version_conflict_count)node.querySelector("h2").closest(".section-title-row").insertAdjacentHTML("afterend",`<p class="data-warning">${g.version_conflict_count} 条案例的方向版本与制作版本不同，先核对数据对应稿件。</p>`);
      if(g.duplicate_video_ids?.length)node.querySelector(".direction-observations").insertAdjacentHTML("beforebegin",g.duplicate_video_ids.map(v=>`<div class="data-warning direction-duplicate"><p>${v.cross_direction?"同一视频跨方向登记":"同一视频重复登记"}：${esc(v.video_id)}。${v.cross_direction?"请先核对方向与对应稿件；这些记录不能作为各方向的独立表现样本。":"保留各自当前记录，不重复累计。"}</p><ul>${(v.assignments || []).map(a=>`<li><a href="#case/${esc(a.case_id)}">${esc(a.case_id)}</a> · 原产品名 ${esc(a.product || "未提供")} · 方向 ${esc(a.direction_id || "未记录")}</li>`).join("")}</ul></div>`).join(""));
    });
  }
  let refocusFilter=null,filterTimer=0,composing=false,listView=null;
  const cancelFilterTimer=()=>{clearTimeout(filterTimer);filterTimer=0;};
  function renderCatalog(catalog,summary,values) {
    cancelFilterTimer();composing=false;
    const {params,product,archive}=values;
    const tier=["S","A","B"].includes(params.get("tier"))?params.get("tier"):"";
    const query=params.get("q") || "", direction=params.get("direction") || "";
    const status=Object.hasOwn(statusNames,params.get("status"))?params.get("status"):"";
    const allDirections=catalog.directions || [], allPoints=[...(catalog.selling_points || [])].sort((a,b)=>tierRank(a.code)-tierRank(b.code)||codeNo(a.code)-codeNo(b.code));
    const directionOf=point=>allDirections.find(item=>item.product===point.product&&item.selling_point_id===point.id);
    const orphans=allDirections.filter(item=>!item.selling_point_id||!allPoints.some(point=>point.id===item.selling_point_id));
    const text=(point,item)=>`${point?`${point.code} ${point.title} ${point.rationale} ${point.evidence}`:""} ${item?`${item.direction_id} ${directionChain(item)} ${item.notes || ""}`:""}`.toLocaleLowerCase();
    const keep=(point,item)=>(!tier||(point?point.tier:(item.direction_id.split("-").pop()||"")[0])===tier)&&(!status||item?.status===status)
      &&(!direction||item?.direction_id===direction)&&(!query||text(point,item).includes(query.toLocaleLowerCase()));
    const rows=allPoints.map(point=>[point,directionOf(point)]).filter(([point,item])=>keep(point,item));
    const orphanRows=orphans.filter(item=>keep(null,item));
    const groups=[["S","S 级","主打候选"],["A","A 级","支撑卖点"],["B","B 级","加分项"]].map(([key,label,hint])=>[label,hint,rows.filter(([point])=>point.tier===key)]).filter(([,,list])=>list.length);
    const count=filter=>allPoints.filter(filter).length;
    const segment=(name,options,value)=>`<div class="dir-segment" role="group">${options.map(([key,label,n])=>`<button type="button" data-${name}-filter="${key}" aria-pressed="${value===key}">${label}${n!=null?`<small>${n}</small>`:""}</button>`).join("")}</div>`;
    const reset=new URLSearchParams(params);reset.delete("direction");
    const statusReset=new URLSearchParams(params);statusReset.delete("status");
    const libraryParams=new URLSearchParams({direction:"__unclassified__"});if(archive!=="active")libraryParams.set("archive",archive);
    const empty=!rows.length&&!orphanRows.length;
    $("#content").innerHTML=`<div class="page-heading dir-heading"><div><h1>卖点与方向</h1><p>${allPoints.length} 个卖点 · ${allDirections.length} 个方向。从 S 级卖点出发，串起顾虑和收口，每次换执行记为变体。</p></div><button type="button" class="secondary" id="new-selling-point">${icon("plus")}新增卖点</button></div>
      <form id="direction-filters" class="dir-filters"><label class="search">${icon("search")}<input name="q" value="${esc(query)}" placeholder="搜索卖点、顾虑、收口或方向编号" aria-label="搜索"></label>
        ${segment("tier",[["","全部",allPoints.length],["S","S",count(item=>item.tier==="S")],["A","A",count(item=>item.tier==="A")],["B","B",count(item=>item.tier==="B")]],tier)}
                <label class="dir-archive"><span>案例范围</span><select name="archive">${Object.entries({active:"使用中",archived:"已归档",all:"全部"}).map(([value,label])=>`<option value="${value}" ${value===archive?"selected":""}>${label}</option>`).join("")}</select></label>
        <input type="hidden" name="tier" value="${tier}"><input type="hidden" name="status" value="${status}">${direction?`<input type="hidden" name="direction" value="${esc(direction)}">`:""}</form>
      ${direction?`<p class="dir-scope">只看方向 <strong>${esc(direction)}</strong> · <a href="${esc(pageURL("#directions"+(reset.size?"?"+reset:"")))}">看全部</a></p>`:""}${status?`<p class="dir-scope">只看${esc(statusNames[status])}的方向 · <a href="${esc(pageURL("#directions"+(statusReset.size?"?"+statusReset:"")))}">看全部</a></p>`:""}
      ${product?'<div id="tk-candidates-slot"></div>':""}
      ${empty?`<section class="section-card empty direction-empty"><h3>${query||tier||status||direction?"当前筛选没有匹配的卖点":"当前产品还没有卖点"}</h3><p>${query||tier||status||direction?"换个关键词，或清除筛选。":"先新增一个明确的主打卖点，再从它建立测试方向。"}</p>${query||tier||status||direction?"":`<button type="button" class="primary" data-empty-new-point>${icon("plus")}新增卖点</button>`}</section>`
        :groups.map(([label,hint,list])=>{
          const parked=list.filter(([point])=>parkedPoint(point)),main=list.filter(([point])=>!parkedPoint(point));
          return `<section class="dir-group"><h2>${label}<small>${hint} · ${list.length}</small></h2>${main.length?`<ul class="dir-list">${main.map(([point,item])=>pointRow(point,item)).join("")}</ul>`:""}${parked.length?`<details class="dir-parked"${query||direction?" open":""}><summary>待核候选与已并入 · ${parked.length}</summary><ul class="dir-list">${parked.map(([point,item])=>pointRow(point,item)).join("")}</ul></details>`:""}</section>`;
        }).join("")
        +(orphanRows.length?`<section class="dir-group"><h2>未关联卖点的方向<small>旧记录 · ${orphanRows.length}</small></h2><ul class="dir-list">${orphanRows.map(item=>pointRow(null,item)).join("")}</ul></section>`:"")}
      <div class="direction-summary-notes"><details><summary>数据与复盘说明</summary>${leadTierLine(summary.lead_tier_counts)}<p>范围内未记录方向的案例 ${summary.unclassified_case_ids?.length || 0} 条${summary.unclassified_case_ids?.length?` · <a href="${esc(pageURL("#library?"+libraryParams))}">查看案例</a>`:""}。</p><ul>${(summary.limitations || []).map(note=>`<li>${esc(note)}</li>`).join("")}</ul><p>关联视频去重计数；出单视频只表示其自身窗口内有售出记录，不累计不同窗口的出单量。</p></details></div>`;
    const filterForm=$("#direction-filters");
    // Only reuse the complete data behind the currently mounted list. Opening the
    // page again, changing its data scope, or refreshing still reads fresh data.
    listView={catalog,summary,values,form:filterForm,signature:state.signature,digest:state.digest};
    filterForm.onsubmit=event=>{
      event.preventDefault();const next=new URLSearchParams(),form=new FormData(event.currentTarget);
      for(const [key,value] of form){const text=String(value).trim();if(text&&!(key==="archive"&&text==="active"))next.set(key,text);}
      const target=pageURL("#directions"+(next.size?"?"+next:""));if(location.hash===target)void route();else location.hash=target;
    };
    for(const name of ["tier","status"])$$(`[data-${name}-filter]`).forEach(button=>button.onclick=()=>{filterForm.elements[name].value=button.dataset[name+"Filter"];filterForm.requestSubmit();});
    const applyFilters=(delay=0)=>{
      cancelFilterTimer();
      const submit=()=>{
        filterTimer=0;
        if(composing||!filterForm.isConnected||pageHash()!==values.hash)return;
        const active=document.activeElement;
        refocusFilter=active?.name?{name:active.name,start:active.selectionStart,end:active.selectionEnd}:null;
        filterForm.requestSubmit();
      };
      if(delay)filterTimer=setTimeout(submit,delay);else submit();
    };
    filterForm.addEventListener("compositionstart",()=>{composing=true;});
    filterForm.addEventListener("compositionend",()=>{composing=false;applyFilters(150);});
    filterForm.addEventListener("input",event=>{if(!composing&&!event.isComposing&&event.target.matches("input[type=text],input:not([type])"))applyFilters(event.target.value.trim()?150:0);});
    filterForm.addEventListener("change",event=>{if(event.target.matches("select"))applyFilters();});
    filterForm.addEventListener("keydown",event=>{if(event.key==="Enter"&&!event.isComposing){event.preventDefault();applyFilters();}});
    if(refocusFilter){const saved=refocusFilter,field=filterForm.elements[saved.name];refocusFilter=null;if(field?.focus){field.focus();if(field.setSelectionRange&&saved.start!=null)field.setSelectionRange(saved.start,saved.end);}}
    $("#new-selling-point").onclick=()=>openPointEditor();
    window.TKCandidates?.mount($("#tk-candidates-slot"),product,catalog);
    document.querySelector("[data-empty-new-point]")?.addEventListener("click",()=>openPointEditor());
    $$("[data-edit-point]").forEach(button=>button.onclick=()=>openPointEditor(button.dataset.editPoint));
    $$("[data-adopt-grading]").forEach(button=>button.onclick=()=>openPointEditor(button.dataset.adoptGrading,true));
    $$("[data-use-point]").forEach(button=>button.onclick=()=>openCreate(button.dataset.usePoint));
  }
  let variantView={key:"",expanded:new Set()};
  const variantSnapshot=variant=>Object.fromEntries(["progression","baseline","variant_type","main_difference","concern","closing","execution_change"].filter(key=>Object.hasOwn(variant,key)).map(key=>[key,variant[key]]));
  const variantName=(variant,index)=>`${variant.baseline?"基准稿":"变体 "+(index+1)}${variant.execution_change?" · "+variant.execution_change:""}`;
  const workflowURL=(kind,item)=>{
    const id=encodeURIComponent(item.id||"");
    if(kind==="tasks"||kind==="task")return pageURL("#ai/"+id);
    if(kind==="scripts"||kind==="script")return pageURL("#content/"+id);
    if(kind==="productions"||kind==="production")return pageURL("#production/"+id+(item.status==="approved"||item.label==="登记发布"?"?step="+((item.production_kind||item.kind)==="carousel"?"delivery":"publish"):""));
    if(kind==="publications"||kind==="videos")return pageURL("#videos?video="+encodeURIComponent(item.video_id||item.id));
    return "";
  };
  const associationMarkup=record=>record?.association?`<details class="dir-record-association"><summary>已核对关联${record.association.inherited_from?" · 沿用文案归属":""}</summary><p>${esc(record.association.evidence?.note||"")}</p>${record.association.inherited_from?`<a href="${esc(pageURL("#content/"+encodeURIComponent(record.association.inherited_from)))}">查看来源采用稿</a>`:""}</details>`:"";
  const workflowRecords=records=>Object.entries({tasks:"写稿任务",scripts:"文案",productions:"制作单",publications:"发布登记",plans:"测试与安排",videos:"关联视频"}).filter(([key])=>records[key]?.length).map(([key,label])=>`<div class="dir-flow-record-group"><strong>${label} · ${records[key].length}</strong><ul>${records[key].map(item=>`<li>${key==="plans"?`<button type="button" class="subtle" data-progress-plan="${esc(item.id)}">${esc(item.title||"查看测试卡")}</button>`:`<a href="${esc(workflowURL(key,item))}">${esc(item.title||item.video_id||item.id)}</a>`}${key==="publications"?`<small>${esc(item.published_on||"")}${item.current===false?" · 历史登记":item.source_current===false?" · 制作版本已变化":""}</small>`:""}${associationMarkup(item)}</li>`).join("")}</ul></div>`).join("");
  function renderDetail(data,values,points=[]) {
    const d=data.direction,p=data.selling_point,group=data.group;
    const progressAvailable=data.progress_available===true&&!window.PublicTextMode;
    const variants=d.variants || [], scripts=data.scripts || [],videos=data.videos || [],tasks=data.tasks || [],cases=data.cases || [],plans=data.plans || [];
    let relatedTouched=false;
    const variantKey=JSON.stringify([d.product,d.direction_id]);
    if(variantView.key!==variantKey)variantView={key:variantKey,expanded:new Set()};
    const finished=plan=>["done","cancelled"].includes(plan.status);
    const pendingTests=plans.filter(plan=>plan.experiment&&!finished(plan));
    const pendingPlans=plans.filter(plan=>!plan.experiment&&!finished(plan));
    const pastPlans=plans.filter(plan=>!plan.experiment&&finished(plan));
    const taskItems=items=>items.map(task=>`<a class="direction-related-item" href="${esc(pageURL("#ai/"+encodeURIComponent(task.task_id)))}"><strong>${esc(task.display_title || task.case_title || task.case_id || task.task_id)}</strong><span>${esc(taskStatusNames[task.status] || task.status || "状态未提供")} · ${esc(readableDate(task.updated_at))}</span></a>`).join("");
    const planNames={scale:"加码",execution:"换执行",stop:"停测"},planStatus={pending:"待处理",working:"进行中",done:"已完成",cancelled:"已取消"};
    const planList=items=>`<ul class="dir-plans">${items.map(plan=>`<li><strong>${esc(planNames[plan.action]||plan.action)}</strong><span>${esc(planStatus[plan.status]||plan.status)}</span>${plan.note?`<small>${esc(plan.note)}</small>`:""}${window.FeedbackChain?.sourceLink(plan.source_review) || ''}</li>`).join("")}</ul>`;
    const related=[
      ["scripts","采用稿",scripts,scripts.map(script=>script.script_id || script.id?`<a class="direction-related-item" href="${esc(pageURL("#content/"+(script.script_id || script.id)))}"><strong>${esc(script.title)}</strong><span>第 ${esc(script.version || 1)} 版 · ${esc({draft:"草稿",adopted:"已采纳",filmed:"已拍摄",published:"已发布",reviewed:"已复盘",archived:"已归档"}[script.status] || script.status || "")}</span></a>`:`<details class="direction-script"><summary><strong>${esc(script.script_version || "版本未标记")}</strong> ${esc(script.title || script.case_id)}</summary><pre>${esc(script.text || "未记录文案")}</pre><a href="${esc(pageURL("#case/"+script.case_id+"/script"))}">查看案例文案</a></details>`).join(""),"还没有关联的采用稿。"],
      ["videos","关联视频",videos,videos.map(video=>`<a class="direction-related-item" href="${esc(pageURL("#videos?video="+encodeURIComponent(video.video_id)))}"><strong>${esc(video.title || video.video_id)}</strong><span>${esc(video.video_id)}</span></a>`).join(""),"还没有关联视频。"],
      ["tasks","AI 任务",tasks,taskItems(tasks),"还没有关联的 AI 协作任务。"],
      ["cases","关联案例",cases,cases.map(item=>`<a class="direction-related-item" href="${esc(pageURL("#case/"+item.case_id))}"><strong>${esc(item.current?.title || item.title || item.case_id)}</strong><span>${esc(item.case_id)}${item.archived?" · 已归档":""}</span></a>`).join(""),"方向已经独立保存，后续案例可以使用这个编号。"],
    ];
    const firstRelated=(related.find(([, ,list])=>list.length)||related[0])[0];
    // Empty tabs stay in the page (tasks arrive later) but are hidden; the card hides when every tab is empty.
    const relatedEmpty=([key,,list])=>!list.length&&!(key==="tasks"&&data.tasks_pending===true);
    const relatedMarkup=([key,,list,markup,emptyText])=>key==="tasks"&&data.tasks_pending===true?'<p class="context-note" role="status">正在读取 AI 任务…</p>':list.length?`<div class="direction-related-list">${markup}</div>`:`<p class="dir-empty">${esc(emptyText)}</p>`;
    const progressionSelect=(variant,index)=>`<label class="dir-progression"><span class="sr-only">推进结构</span><select data-variant-progression="${index}" aria-label="第 ${index+1} 条的推进结构"><option value="">推进结构未标注</option>${progressions.map(item=>`<option ${variant.progression===item?"selected":""}>${esc(item)}</option>`).join("")}${variant.progression&&!progressions.includes(variant.progression)?`<option selected>${esc(variant.progression)}</option>`:""}</select></label>`;
    $("#breadcrumb").innerHTML=`卖点与方向 <span>/</span> ${esc(d.direction_id)}`;
    $("#content").innerHTML=`<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || directionURL(d.product))}">${icon("back")}返回卖点与方向</a>
      <div class="page-heading dir-detail-heading"><div><p class="dir-meta">${p?`<span class="dir-code tier-${esc(p.tier)}">${esc(p.code)}</span>`:""}<span>${esc(d.direction_id)}</span>${statusChip(d.status)}${d.lead_tier&&d.lead_tier!=="S"?`<span class="dir-flag" title="${esc(d.notes||"方向应由 S 级卖点主打")}">${esc(d.lead_tier)} 级主打</span>`:""}</p><h1>${esc(p?.title || d.main_difference)}</h1></div>
        <div class="dir-detail-actions"><button type="button" class="secondary" id="direction-edit">${icon("edit")}编辑方向</button><button type="button" class="primary" id="direction-write">${icon("edit")}用这个方向写稿</button></div></div>
      <div class="dir-layout"><div class="dir-primary">
        <section class="section-card dir-card" id="direction-definition"><h2>方向链</h2><div class="direction-chain dir-chain">${chainSteps(d,points)}</div>
          ${(d.chain_issues||[]).length?`<p class="dir-note is-warn">方向链需要修改：${esc(d.chain_issues.join("；"))}</p>`:""}${d.audience?`<p class="dir-note dir-audience"><strong>目标人群</strong> ${esc(d.audience)}</p>`:""}${notesMarkup(d.notes)}</section>
        <section class="section-card dir-card" id="direction-variants"><div class="dir-card-head"><h2>执行变体<small>${variants.length}</small></h2><div class="dir-card-actions">${variants.length?'<button type="button" class="subtle" id="direction-variants-toggle">展开全部</button>':""}<button type="button" class="subtle" id="direction-add-variant">${icon("plus")}新增执行变体</button></div></div>
          <p class="dir-progress-summary" id="direction-progress-summary" role="status">${progressAvailable?"正在核对写稿、制作、发布和测试记录…":window.PublicTextMode?"变体实际进展请在本机工作台查看。":"当前服务尚未提供变体进展，重启工作台后可查看。"}</p>
          <p class="dir-note is-warn" id="direction-workflow-target-note" role="status" hidden></p>
          ${!variants.some(item=>item.baseline)&&variants.length?'<p class="dir-note is-warn">还没有基准稿。选一条设为基准，其他变体才有比较对象。</p>':""}
          <div class="direction-variant-list">${variants.map((variant,index)=>`<div class="dir-variant-row"><details class="direction-definition dir-variant${variant.baseline?" is-baseline":""}" data-variant-index="${index}"${variantView.expanded.has(index)?" open":""}>
            <summary><span class="dir-variant-summary-head"><strong>${variant.baseline?"基准稿":variant.variant_type==="direction"?"方向定义":"执行变体 "+(index+1)}</strong><span class="dir-variant-structure">${esc(variant.progression||"推进结构未标注")}</span>${(variant.chain_issues||[]).length?'<span class="dir-flag">方向链待核对</span>':""}</span><span class="dir-variant-summary-change">${esc(variant.execution_change?"改动："+variant.execution_change:variant.baseline?"比较基准 · 展开查看完整方向链":"尚未记录主要改动 · 展开查看方向链")}</span></summary>
            <div class="dir-variant-body"><div class="dir-variant-head">${progressionSelect(variant,index)}${variant.baseline?"":`<button type="button" class="subtle" data-variant-baseline="${index}">设为基准稿</button>`}</div><div class="dir-variant-chain">${chainSteps({concern:variant.concern,closing:variant.closing},points)}</div>${variant.execution_change?`<p class="dir-change">改动：${esc(variant.execution_change)}</p>`:""}${variant.audience?`<p class="dir-change">目标人群：${esc(variant.audience)}</p>`:""}${(variant.chain_issues||[]).length?`<p class="dir-note">${esc(variant.chain_issues.join("；"))}（已发布的记录保持原样）</p>`:""}<div class="dir-flow-records" data-variant-records="${index}"></div></div></details><div class="dir-variant-progress" data-variant-progress="${index}"></div></div>`).join("")}</div>
          <details id="direction-unassigned" hidden></details>
          ${!variants.length?messageMarkup("暂未追加执行变体。已有案例仍保留各自的完整方向链。"):""}
          ${(d.history||[]).length?`<details class="direction-history"><summary>旧定义 · ${d.history.length}</summary>${d.history.map(item=>`<div class="direction-definition"><small>${esc(readableDate(item.replaced_at))} 前</small><p class="direction-chain">${esc(directionChain(item))}</p></div>`).join("")}</details>`:""}</section>
        ${experimentSection(plans)}
        <section class="section-card dir-card dir-related"${related.every(relatedEmpty)?" hidden":""}><div class="dir-tabs" role="tablist" aria-label="关联内容">${related.map(([key,label,list])=>`<button type="button" role="tab" id="direction-${key}" aria-controls="direction-${key}-panel" aria-selected="${key===firstRelated}" data-related-tab="${key}"${relatedEmpty([key,label,list])?" hidden":""}>${label}<small>${key==="tasks"&&data.tasks_pending===true?"…":list.length}</small></button>`).join("")}</div>
          ${related.map(item=>`<div class="dir-related-panel" role="tabpanel" id="direction-${item[0]}-panel" aria-labelledby="direction-${item[0]}"${item[0]==="tasks"&&data.tasks_pending===true?' aria-busy="true"':""}${item[0]===firstRelated?"":" hidden"}>${relatedMarkup(item)}</div>`).join("")}</section>
        ${group?`<div class="direction-section-heading"><h2>案例执行与表现</h2><p class="context-note">每条案例保留自己的方向链、文案版本与统计窗口。</p></div>${groupCard(group)}`:""}
      </div><aside class="dir-aside">
        <section class="dir-card dir-aside-card"><h2>主打卖点</h2>${p?`<p class="dir-aside-status"><span class="dir-code tier-${esc(p.tier)}">${esc(p.code)}</span>${pointStatusChip(p)||'<span class="dir-point-status">已核</span>'}</p>${p.rationale?`<details class="dir-point-rationale"><summary>分级理由</summary><p>${esc(p.rationale)}</p></details>`:""}${p.evidence?`<details class="dir-point-evidence"><summary>证据与边界</summary><p>${esc(p.evidence)}</p></details>`:'<p class="dir-aside-text">待补充证据</p>'}`:'<p class="dir-aside-text">这个方向没有关联卖点。</p>'}</section>
        <section class="dir-card dir-aside-card" id="direction-plans"><h2>下一轮安排</h2><div class="dir-plan-buttons">${[["scale","加码"],["execution","换执行"],["stop","停测"]].map(([action,label])=>`<button type="button" class="secondary compact" data-direction-plan="${action}">${label}</button>`).join("")}</div>
          ${pendingTests.length?`<button type="button" class="subtle dir-test-shortcut" data-show-tests>${pendingTests.length} 张测试卡待处理 · 查看测试卡 ›</button>`:""}${pendingPlans.length?planList(pendingPlans):!pendingTests.length?'<p class="dir-aside-text">暂无待处理安排。选一个写下判断，会出现在「工作概览」的下一轮待写里。</p>':""}${pastPlans.length?`<details class="dir-plan-history"><summary>已完成与取消的安排 · ${pastPlans.length}</summary>${planList(pastPlans)}</details>`:""}</section>
        <section class="dir-card dir-aside-card"><h2>销量表现</h2>${d.performance?.matched_count?`<p class="dir-aside-text">${esc(performanceText(d.performance))}</p>`:'<p class="dir-aside-text">还没有关联到出单视频。</p>'}
          <details class="dir-aside-more"><summary>数据与复盘说明</summary><p>各案例保留各自文案版本、统计窗口与数据来源，不累计不同窗口的售出量。重复登记的视频也保留原始归属提示。</p></details></section>
      </aside></div>`;
    // Next actions stay beside the definition even when the side column falls below a long history.
    const nextPlans = $("#direction-plans"), definition = $("#direction-definition");
    if (nextPlans && definition) { definition.after(nextPlans); nextPlans.classList.add('dir-next-action'); }
    if(group)decorateGroupWarnings($("#content"),[group]);
    const variantDetails=$$("details.dir-variant"),allVariantsButton=$("#direction-variants-toggle");
    const updateVariantToggle=()=>{if(allVariantsButton)allVariantsButton.textContent=variantDetails.every(item=>item.open)?"收起全部":"展开全部";};
    variantDetails.forEach(item=>item.addEventListener("toggle",()=>{
      if(!item.isConnected||variantView.key!==variantKey)return;
      const index=Number(item.dataset.variantIndex);
      if(item.open)variantView.expanded.add(index);else variantView.expanded.delete(index);
      updateVariantToggle();
    }));
    if(allVariantsButton)allVariantsButton.onclick=()=>{const open=!variantDetails.every(item=>item.open);variantDetails.forEach(item=>item.open=open);updateVariantToggle();};
    updateVariantToggle();
    $$("[data-show-tests]").forEach(button=>button.onclick=()=>{const tests=$("#direction-tests");tests.focus({preventScroll:true});tests.scrollIntoView({block:"start",behavior:"smooth"});});
    $("#direction-add-variant").onclick=()=>openCreate(d.selling_point_id);
    $("#direction-edit").onclick=()=>openDirectionEditor(data);
    const saveMeta=async(values)=>{
      try{await api("/api/direction-catalog",{method:"POST",body:{revision:data.revision,request_id:crypto.randomUUID(),operation:"variant_meta",direction:{product:d.product,direction_id:d.direction_id,...values}}});toast("变体标注已保存");}
      catch(error){toast(error.message,true);}
      void route();
    };
    $$("[data-variant-baseline]").forEach(button=>button.onclick=()=>saveMeta({index:Number(button.dataset.variantBaseline),baseline:true}));
    $$("[data-variant-progression]").forEach(select=>select.onchange=()=>{if(select.value)saveMeta({index:Number(select.dataset.variantProgression),progression:select.value});});
    $("#direction-write").onclick=()=>{
      if(p&&parkedPoint(p)&&!confirm(`${p.code} 还是${p.status==="merged"?"已并入其他编号的卖点":"待核候选"}，没有核实或缺证明素材。建议先拍到证明镜头再写稿。仍要现在写吗？`))return;
      window.CaseAI?.startFromDirection(d);
    };
    $$("[data-direction-plan]").forEach(button=>button.onclick=()=>openPlan(data,button.dataset.directionPlan));
    $("#direction-test-create").onclick=()=>openPlan(data,"execution",null,true);
    $$("[data-test-edit]").forEach(button=>button.onclick=()=>{
      const plan=plans.find(item=>item.id===button.dataset.testEdit);
      if(plan)void openPlan(data,plan.action,plan,true);
    });
    $$("[data-test-result]").forEach(button=>button.onclick=()=>loadExperimentResult(button));
    const selectRelated=key=>{
      $$("[data-related-tab]").forEach(item=>item.setAttribute("aria-selected",String(item.dataset.relatedTab===key)));
      $$(".dir-related-panel").forEach(panel=>panel.hidden=panel.id!==`direction-${key}-panel`);
    };
    $$("[data-related-tab]").forEach(tab=>tab.onclick=()=>{relatedTouched=true;selectRelated(tab.dataset.relatedTab);});
    const openProgressPlan=id=>{
      const plan=plans.find(item=>item.id===id);
      if(plan)void openPlan(data,plan.action,plan,!!plan.experiment);
      else toast("记录已变化，请刷新方向后再查看。",true);
    };
    const clearProgress=()=>{
      $$("[data-variant-progress],[data-variant-records]").forEach(node=>node.replaceChildren());
      $("#direction-unassigned").hidden=true;
    };
    let workflowTargetApplied=false;
    const locateWorkflowTarget=progress=>{
      if(workflowTargetApplied)return;
      workflowTargetApplied=true;
      const notices=[];
      if(values.params.has("variant")){
        const key=values.params.get("variant"),matches=progress.variants.filter(item=>key&&item.key===key);
        if(matches.length!==1){
          notices.push(matches.length?"当前方向有多个相同的执行变体，暂时无法唯一定位，请先核对变体。":"这条执行变体已调整或不再存在，请核对当前方向。未自动定位到其他变体。");
        }else if(variants[matches[0].index]?.conflict){
          notices.push("这条执行变体的方向链存在冲突，请先核对后再查看关联输出。");
        }else{
          const index=matches[0].index,details=variantDetails.find(item=>Number(item.dataset.variantIndex)===index);
          if(details){
            details.open=true;details.dataset.variantKey=key;variantView.expanded.add(index);updateVariantToggle();
            details.querySelector(":scope > summary")?.focus({preventScroll:true});
            details.closest(".dir-variant-row").scrollIntoView({block:"start",behavior:"smooth"});
          }
        }
      }
      if(values.params.has("plan")){
        const id=values.params.get("plan"),matches=$$("[data-test-result]").filter(item=>id&&item.dataset.testResult===id);
        if(matches.length!==1)notices.push("这张测试卡已调整或不在当前方向中，请核对现有测试卡。");
        else{
          const target=matches[0],history=target.closest("details.direction-tests-history");if(history)history.open=true;
          target.focus({preventScroll:true});target.closest(".direction-test").scrollIntoView({block:"start",behavior:"smooth"});
        }
      }
      const note=$("#direction-workflow-target-note");note.textContent=notices.join(" ");note.hidden=!notices.length;
    };
    return {
      loadingProgress(){
        clearProgress();
        $("#direction-progress-summary").hidden=false;$("#direction-progress-summary").textContent="正在核对写稿、制作、发布和测试记录…";
      },
      setProgress(progress){
        if(progress.catalog_revision!==data.revision||progress.direction_id!==d.direction_id||progress.product!==d.product||progress.variants?.length!==variants.length)
          throw new Error("方向定义已更新，请刷新方向后重新核对进展。");
        const indexes=new Set(progress.variants.map(item=>item.index));
        if(indexes.size!==variants.length||variants.some((_,index)=>!indexes.has(index)))throw new Error("变体进展不完整，请重新读取。");
        const summary=progress.summary||{};
        const counted=[[summary.variants_with_scripts,"有文案"],[summary.variants_in_production,"有制作单"],[summary.variants_with_videos,"有关联视频"],[summary.variants_published,"已登记发布"],[summary.variants_tested,"可比较"]].filter(([n])=>n);
        const summaryNode=$("#direction-progress-summary");summaryNode.textContent=counted.length?"已关联记录："+counted.map(([n,label])=>`${n} 个变体${label}`).join(" · "):"";summaryNode.hidden=!counted.length;
        for(const item of progress.variants){
          const index=item.index,next=item.next_action||{},variant=variants[index],url=workflowURL(next.kind,next);
          const action=variant.conflict?'<span class="dir-note is-warn">先核对变体方向链</span>':url?`<a class="secondary compact" data-variant-next="${index}" href="${esc(url)}">${esc(next.label)} ${icon("arrow")}</a>`:`<button type="button" class="secondary compact" data-variant-next="${index}">${esc(next.label||"写这一稿")}</button>`;
          const untouched=Object.values(item.stages||{}).every(stage=>stage.status==="empty");
          $(`[data-variant-progress="${index}"]`).innerHTML=untouched?`<div class="dir-flow-compact"><span>还没开始</span><div class="dir-flow-next">${action}</div></div>`:`<ol class="dir-flow" aria-label="变体 ${index+1} 的实际进展">${Object.entries({writing:"写稿",production:"制作",publishing:"发布",testing:"测试"}).map(([key,label])=>{
            const stage=item.stages?.[key]||{status:"unknown",label:"尚未核对"};
            const tone=["empty","working","done","attention","unknown"].includes(stage.status)?stage.status:"unknown";
            return `<li class="is-${tone}"><span>${label}</span><small title="${esc(stage.history_label||stage.label)}">${esc(stage.label)}</small></li>`;
          }).join("")}</ol><div class="dir-flow-next">${action}</div>`;
          $(`[data-variant-records="${index}"]`).innerHTML=workflowRecords(item.records||{})+(!variant.conflict?`<div class="dir-flow-options"><button type="button" class="subtle" data-variant-write="${index}">再写一稿</button><button type="button" class="subtle" data-variant-test="${index}">为此变体建测试卡</button></div>`:"");
          const button=$(`button[data-variant-next="${index}"]`);
          if(button)button.onclick=()=>{
            if(next.kind==="test"){
              if(!next.id)void openPlan(data,"execution",null,true,{index,videoId:next.video_id});
              else {
                const plan=plans.find(row=>row.id===next.id),record=item.records.plans.find(row=>row.id===next.id);
                if(plan&&record&&(record.comparison_status==="ready"||record.decision_recorded)){
                  const target=$(`[data-test-result="${CSS.escape(next.id)}"]`);
                  const history=target?.closest("details");if(history)history.open=true;
                  target?.focus({preventScroll:true});target?.scrollIntoView({block:"center",behavior:"smooth"});if(target)void loadExperimentResult(target);
                }else openProgressPlan(next.id);
              }
            }else window.CaseAI?.startFromDirection(d,{variantIndex:index});
          };
        }
        const unassigned=$("#direction-unassigned");
        unassigned.hidden=!summary.unassigned_count;
        unassigned.innerHTML=summary.unassigned_count?`<summary>待关联到变体 · ${summary.unassigned_count} 条记录</summary><p class="context-note">这些记录属于当前方向，但来源还不能唯一对应到某条变体，暂不计入上方进度。测试卡可在编辑时选择所属变体。</p>${workflowRecords(progress.unassigned||{})}`:"";
        $$("[data-progress-plan]").forEach(button=>button.onclick=()=>openProgressPlan(button.dataset.progressPlan));
        $$("[data-variant-write]").forEach(button=>button.onclick=()=>window.CaseAI?.startFromDirection(d,{variantIndex:Number(button.dataset.variantWrite)}));
        $$("[data-variant-test]").forEach(button=>button.onclick=()=>openPlan(data,"execution",null,true,{index:Number(button.dataset.variantTest)}));
        locateWorkflowTarget(progress);
      },
      progressError(error,retry){
        clearProgress();
        const node=$("#direction-progress-summary");node.hidden=false;
        node.innerHTML=`<span class="dir-note is-warn">${esc(error.message||"进展暂时读取失败，不能判断当前进度。")}</span> <button type="button" class="subtle" id="direction-progress-retry">${error.message?.includes("方向定义已更新")?"刷新方向":"重新读取"}</button>`;
        $("#direction-progress-retry").onclick=error.message?.includes("方向定义已更新")?()=>void route(true):retry;
      },
      loadingTasks(){
        $("#direction-tasks small").textContent="…";
        const panel=$("#direction-tasks-panel");panel.setAttribute("aria-busy","true");panel.innerHTML='<p class="context-note" role="status">正在读取 AI 任务…</p>';
      },
      setTasks(items){
        data.tasks=Array.isArray(items)?items:[];delete data.tasks_pending;
        related[2][2]=data.tasks;related[2][3]=taskItems(data.tasks);
        $("#direction-tasks small").textContent=String(data.tasks.length);
        $("#direction-tasks").hidden=!data.tasks.length;
        $(".dir-related").hidden=related.every(([,,list])=>!list.length);
        const panel=$("#direction-tasks-panel");panel.removeAttribute("aria-busy");panel.innerHTML=relatedMarkup(related[2]);
        if(!relatedTouched&&data.tasks.length&&!scripts.length&&!videos.length)selectRelated("tasks");
      },
      taskError(error,retry){
        $("#direction-tasks small").textContent="—";
        const panel=$("#direction-tasks-panel");panel.removeAttribute("aria-busy");panel.innerHTML=`<p class="dir-note is-warn" role="alert">${esc(error.message || "AI 任务读取失败")}</p><button type="button" class="secondary" id="direction-task-retry">重新读取</button>`;
        $("#direction-task-retry").onclick=retry;
      },
    };
  }
  const experimentMetrics={ctor_pct:"CTOR · 订单 / 点击",ctr_pct:"CTR · 点击 / 播放",gpm:"GPM · 每千次播放销售额",orders:"订单数",gmv:"销售额",clicks:"点击数",views:"播放数"};
  const experimentDecisions={observe:"继续观察",scale:"加码",execution:"换执行",stop:"停测"};
  function experimentSection(plans) {
    const cards=plans.filter(plan=>plan.experiment);
    const pastCards=cards.filter(plan=>["done","cancelled"].includes(plan.status)),currentCards=cards.filter(plan=>!["done","cancelled"].includes(plan.status));
    const cardMarkup=plan=>{
      const card=plan.experiment;
      return `<article class="direction-test"><div class="dir-card-head"><strong>${esc(card.hypothesis)}</strong><button type="button" class="subtle" data-test-edit="${esc(plan.id)}">编辑与记录决定</button></div><dl class="direction-test-facts">${plan.variant_ref?`<dt>所属变体</dt><dd>${esc(variantName(plan.variant_ref.snapshot,plan.variant_ref.index))}</dd>`:""}<dt>主要改动</dt><dd>${esc(card.change)}</dd><dt>基准 → 测试</dt><dd>${esc(card.baseline_video_id||"待登记")} → ${esc(card.test_video_id||"待登记")}</dd><dt>观察指标</dt><dd>${esc(experimentMetrics[card.metric]||card.metric)}</dd><dt>共同比较窗</dt><dd>${card.window_start?`${esc(card.window_start)} 至 ${esc(card.window_end)}`:"待指定"}</dd><dt>当前决定</dt><dd>${esc(experimentDecisions[card.decision]||"继续观察")}${card.decision_note?` · ${esc(card.decision_note)}`:""}</dd></dl><div class="direction-test-actions"><button type="button" class="secondary compact" data-test-result="${esc(plan.id)}">查看同窗口结果</button><span class="context-note">人工计划状态 · ${esc({pending:"待处理",working:"进行中",done:"已完成",cancelled:"已取消"}[plan.status]||plan.status)}</span></div><div data-test-comparison role="status"></div>${(plan.experiment_history||[]).length>1?`<details class="direction-test-history"><summary>测试卡修改记录（${plan.experiment_history.length} 次）</summary>${[...plan.experiment_history].reverse().map(entry=>`<p><small>${esc(readableDate(entry.at))}</small><br>${esc(entry.experiment.hypothesis)} · ${esc(entry.experiment.change)}<br>${esc(experimentDecisions[entry.experiment.decision]||"继续观察")}${entry.experiment.decision_note?`：${esc(entry.experiment.decision_note)}`:""}</p>`).join("")}</details>`:""}</article>`;
    };
    return `<section class="section-card dir-card direction-tests" id="direction-tests" tabindex="-1"><div class="dir-card-head"><h2>方向测试卡<small>${cards.length}</small></h2><button type="button" class="secondary compact" id="direction-test-create">新建测试卡</button></div>${cards.length?'<p class="context-note">先记下假设与主要改动，再用同一账号、相同日历窗口的数据比较。决定由你记录。</p>':""}${currentCards.length?currentCards.map(cardMarkup).join(""):pastCards.length?'<p class="dir-empty">暂无待处理测试卡。</p>':""}${pastCards.length?`<details class="direction-tests-history"><summary>已完成与取消的测试 · ${pastCards.length}</summary>${pastCards.map(cardMarkup).join("")}</details>`:""}</section>`;
  }
  async function loadExperimentResult(button) {
    const node=button.closest(".direction-test").querySelector("[data-test-comparison]");
    button.disabled=true;node.textContent="正在核对窗口与数据完整性…";
    try {
      const result=await api(`/api/plans/${encodeURIComponent(button.dataset.testResult)}/comparison`);
      if(!node.isConnected)return;
      if(result.status!=="ready")node.innerHTML=`<p class="dir-note is-warn">尚不能比较</p><ul class="direction-test-issues">${(result.issues||["请补齐数据后重试"]).map(issue=>`<li>${esc(issue)}</li>`).join("")}</ul>`;
      else {
        const percentage=["ctr_pct","ctor_pct"].includes(result.metric), money=["gmv","gpm"].includes(result.metric);
        const format=value=>`${money?"$":""}${Number(value).toLocaleString("zh-CN",{maximumFractionDigits:2})}${percentage?"%":""}`;
        const delta=`${result.delta>0?"+":""}${Number(result.delta).toLocaleString("zh-CN",{maximumFractionDigits:2})}${percentage?" 个百分点":money?" 美元":""}`;
        node.innerHTML=`<div class="direction-test-result"><p>${esc(result.window_start)} 至 ${esc(result.window_end)} · ${esc(result.baseline.account)}</p><div class="direction-test-values"><span>基准<strong>${esc(format(result.baseline.value))}</strong></span><span>测试<strong>${esc(format(result.test.value))}</strong></span><span>测试 − 基准<strong>${esc(delta)}</strong></span></div><p class="context-note">${esc(result.note)}</p><details><summary>计算依据</summary>${[["基准",result.baseline],["测试",result.test]].map(([label,side])=>`<p>${label} ${esc(side.video_id)}：${Object.entries(side.totals).map(([key,value])=>`${esc({views:"播放",clicks:"点击",orders:"订单",gmv:"销售额"}[key]||key)} ${esc(value)}`).join(" · ")}</p>`).join("")}<p class="context-note">比率以窗口内分子总和除以分母总和计算。</p></details></div>`;
      }
    } catch(error){if(node.isConnected)node.textContent=error.message||"暂时无法读取比较数据。";}
    finally{button.disabled=false;}
  }
  async function openPlan(data,action,existing=null,testCard=false,variantContext=null) {
    const labels={scale:"加码",execution:"换执行",stop:"停测"}, d=data.direction;
    let dialog=$("#direction-plan-dialog");if(!dialog){dialog=document.createElement("dialog");dialog.id="direction-plan-dialog";document.body.append(dialog);}
    const card=existing?.experiment||{}, bound=existing?.variant_ref, variants=d.variants||[], options=(items,selected)=>Object.entries(items).map(([value,label])=>`<option value="${esc(value)}" ${value===selected?"selected":""}>${esc(label)}</option>`).join("");
    const bindingField=bound?`<p class="context-note">所属变体：${esc(variantName(bound.snapshot,bound.index))}（保留创建时的关联）</p>`:`<label class="field"><span>所属变体</span><select name="variant_index"><option value="">暂不指定</option>${variants.map((variant,index)=>!variant.conflict?`<option value="${index}" ${variantContext?.index===index?"selected":""}>${esc(variantName(variant,index))}</option>`:"").join("")}</select><small>指定后，测试卡会出现在该变体的进度中。</small></label>`;
    dialog.innerHTML=`<form><div class="dialog-heading"><h2>${testCard?existing?"编辑测试卡":"新建测试卡":labels[action]} · ${esc(d.direction_id)}</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body">${testCard?`${bindingField}<label class="field"><span>要验证的假设</span><textarea name="hypothesis" rows="2" required maxlength="3000" placeholder="例如：先演示装夹稳定性，能减少顾虑并提高点击后的下单率">${esc(card.hypothesis||"")}</textarea></label><label class="field"><span>相对基准的主要改动</span><textarea name="change" rows="2" required maxlength="3000">${esc(card.change||((variantContext&&variants[variantContext.index]?.execution_change)||""))}</textarea></label><div class="direction-test-form-grid"><label class="field"><span>基准视频 ID（可后补）</span><input name="baseline_video_id" inputmode="numeric" pattern="[0-9]{6,25}" maxlength="25" value="${esc(card.baseline_video_id||"")}"></label><label class="field"><span>测试视频 ID（可后补）</span><input name="test_video_id" inputmode="numeric" pattern="[0-9]{6,25}" maxlength="25" value="${esc(card.test_video_id||variantContext?.videoId||"")}"></label><label class="field"><span>共同开始日期</span><input type="date" name="window_start" value="${esc(card.window_start||"")}"></label><label class="field"><span>共同结束日期</span><input type="date" name="window_end" value="${esc(card.window_end||"")}"></label></div><p class="context-note">两条视频使用同一日历起止日期；只有完整落在窗口里的报表才能比较，不能把周报按天摊算。</p><label class="field"><span>主要观察指标</span><select name="metric">${options(experimentMetrics,card.metric||"ctor_pct")}</select></label><div class="direction-test-form-grid"><label class="field"><span>本轮决定</span><select name="decision">${options(experimentDecisions,card.decision||"observe")}</select></label><label class="field"><span>人工计划状态</span><select name="status">${options({pending:"待处理",working:"进行中",done:"已完成",cancelled:"已取消"},existing?.status||"pending")}</select></label></div><label class="field"><span>决定依据（加码、换执行、停测时必填）</span><textarea name="decision_note" rows="3" maxlength="6000">${esc(card.decision_note||"")}</textarea></label>`:""}<label class="field"><span>本轮观察与下一步要求${testCard?"（可选）":""}</span><textarea name="note" rows="3" ${testCard?"":"required"} maxlength="12000" placeholder="写下数据窗口、观察和这次决定的理由">${esc(existing?.note||"")}</textarea></label><p class="context-note">保存到「工作概览」的下一轮安排，保留这次判断。</p><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">${testCard?"保存测试卡":"保存安排"}</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));dialog.showModal();trackDraft(dialog);
    let pending=false,requestId=crypto.randomUUID(),catalog,requestBody=null,uncertain=false;
    try{catalog=existing&&data.creation_revision?{revision:data.creation_revision}:await api("/api/creation?product="+encodeURIComponent(d.product));}catch(error){dialog.querySelector("[role=alert]").textContent=error.message;}
    dialog.querySelector("form").onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      pending=true;if(activeDraft)activeDraft.pending=true;dialog.querySelectorAll("button").forEach(button=>button.disabled=true);
      try{
        if(!catalog)catalog=await api("/api/creation?product="+encodeURIComponent(d.product));
        if(!requestBody){
          const values=Object.fromEntries(new FormData(dialog.querySelector("form")));
          const plan={...(existing||{}),product:d.product,direction_id:d.direction_id,action,status:values.status||existing?.status||"pending",note:values.note.trim()};
          if(testCard&&!bound&&values.variant_index!==""){
            const index=Number(values.variant_index),variant=variants[index];
            if(!Number.isInteger(index)||!variant||variant.conflict)throw new Error("请选择有效变体。");
            plan.variant_ref={index,snapshot:variantSnapshot(variant)};
          }
          if(testCard)plan.experiment=Object.fromEntries(["hypothesis","change","baseline_video_id","test_video_id","metric","window_start","window_end","decision","decision_note"].map(key=>[key,values[key].trim()]));
          requestBody={revision:catalog.revision,request_id:requestId,plan};
        }
        await api("/api/plans",{method:"POST",body:requestBody});
        dialog.close();toast("已保存到下一轮安排");void route();
      }catch(error){
        uncertain=!error.status||error.status>=500;
        dialog.querySelector("[role=alert]").textContent=error.message+(uncertain?" 保存结果尚未确认，请重试原提交。":"");
        if(!uncertain){requestBody=null;requestId=crypto.randomUUID();}
        if(error.status===409){try{catalog=await api("/api/creation?product="+encodeURIComponent(d.product));if(existing){const latest=catalog.plans.find(plan=>plan.id===existing.id);if(!latest)throw new Error("测试卡已不存在，请关闭并刷新。");existing=latest;dialog.querySelector("[role=alert]").textContent+=` 当前保存的假设：${latest.experiment?.hypothesis||"未填写"}；决定：${experimentDecisions[latest.experiment?.decision]||"继续观察"}；依据：${latest.experiment?.decision_note||"未填写"}。`;}dialog.querySelector("[role=alert]").textContent+=" 已读取最新记录，输入保留，请核对后再次保存。";}catch(readError){catalog=null;dialog.querySelector("[role=alert]").textContent+=" "+readError.message;}}
      }
      finally{pending=false;if(activeDraft)activeDraft.pending=false;dialog.querySelectorAll("button").forEach(button=>button.disabled=false);dialog.querySelectorAll("input,select,textarea").forEach(field=>field.disabled=uncertain);}
    };
  }
  async function openCreate(preselected="",options={}) {
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
    // Merged codes are kept for old records only; they are never offered as a lead or chain link.
    const leads=points.filter(item=>item.status!=="merged"),chainPoints=leads;
    const draft={point_id:preselected,concern:"",closing:"",lead_reason:"",progression:"",progression_other:"",execution_change:"",audience:"",...(options.prefill||{})};let step=0,pending=false,requestId=crypto.randomUUID();
    const point=()=>points.find(item=>item.id===draft.point_id);
    const read=name=>dialog.querySelector(`[name="${name}"]`)?.value.trim() ?? draft[name];
    const collect=()=>{if(step===0){draft.point_id=dialog.querySelector('[name="lead_point"]:checked')?.value || "";draft.lead_reason=read("lead_reason");}if(step===1){draft.concern=read("concern");draft.progression=read("progression");draft.progression_other=read("progression_other");draft.execution_change=read("execution_change");draft.audience=read("audience");}if(step===2)draft.closing=dialog.querySelector('[name="closing"]').value.trim();};
    const progressionValue=()=>draft.progression==="其他"?(draft.progression_other?"其他："+draft.progression_other:""):draft.progression;
    const choice=item=>`<label class="direction-point-choice"><input type="radio" name="lead_point" value="${esc(item.id)}" ${draft.point_id===item.id?"checked":""}><span><strong>${esc(item.code)} · ${esc(item.title)}</strong>${pointStatusChip(item)}${item.rationale?`<small>${esc(item.rationale)}</small>`:""}</span></label>`;
    function progressionFields(){
      const chosen=point(),existing=chosen&&directions.find(item=>item.direction_id===`${product}-${chosen.code}`);
      const used=new Set((existing?.variants || []).map(item=>item.progression).filter(Boolean));
      return `<div class="direction-progression"><label class="field"><span>推进结构${existing?"":"（可选，基准稿）"}</span><select name="progression"><option value="">${existing?"请选择":"暂不标注"}</option>${progressions.map(item=>`<option value="${esc(item)}" ${used.has(item)?"disabled":""} ${draft.progression===item?"selected":""}>${esc(item)}${used.has(item)?"（已有）":""}</option>`).join("")}<option value="其他" ${draft.progression==="其他"?"selected":""}>其他</option></select></label><label class="field"><span>目标人群（可选）</span><input name="audience" maxlength="2000" value="${esc(draft.audience)}" placeholder="例如：从 Lansky 换来的人；刀座一排钝刀的家庭厨师"></label><label class="field direction-progression-other"><span>其他结构说明</span><input name="progression_other" maxlength="60" value="${esc(draft.progression_other)}" placeholder="选“其他”时填写"></label>${existing?`<label class="field"><span>和基准稿比，推进上改了什么</span><textarea name="execution_change" rows="2" maxlength="4000" placeholder="例如：先给一排刀切纸的结果，再倒回讲以前用几样东西">${esc(draft.execution_change)}</textarea></label><p class="context-note">只换开头一句不算新变体；已用过的推进结构不能再选。</p>`:""}</div>`;
    }
    function render(){
      const chosen=point(),id=chosen?`${product}-${chosen.code}`:"选择主打后自动生成",existing=directions.find(item=>item.direction_id===id);
      const main=existing?.main_difference || (chosen?chosen.code+" "+chosen.title:"");
      const linkedThemes=(materials?.theme_links || []).filter(link=>link.selling_point_id===chosen?.id || link.direction_id===id || link.target_id===chosen?.id || link.target_id===id);
      const reviewConcerns=linkedThemes.map(link=>link.concern || link.source_quote || link.theme).filter(Boolean);
      const candidates=[...new Set(directions.flatMap(item=>[item,...(item.variants || [])]).map(item=>fieldText(item[step===1?"concern":"closing"]).trim()).filter(Boolean))];
      dialog.innerHTML=`<form><div class="dialog-heading"><div><h2 id="direction-create-title">${existing?"新增执行变体":"新建方向"}</h2><p>${esc(product)} · 同主打卖点共用方向编号</p></div><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><ol class="direction-wizard-steps">${["选择主打","回应顾虑","确定收口"].map((label,index)=>`<li aria-current="${step===index?"step":"false"}">${index+1} ${label}</li>`).join("")}</ol><div class="direction-wizard-body">${options.source?`<p class="direction-source-note">${esc(options.source)}</p>`:""}<p class="direction-number">方向编号 <strong id="direction-generated-id">${esc(id)}</strong></p>${step===0?`<fieldset class="direction-lead-picker"><legend>哪个 S 级卖点主打？</legend>${points.length?leads.filter(item=>item.tier==="S").map(choice).join("")+(leads.some(item=>item.tier!=="S")?`<details class="direction-non-s" ${chosen&&chosen.tier!=="S"?"open":""}><summary>用 A / B 级卖点主打（需写理由）</summary>${leads.filter(item=>item.tier!=="S").map(choice).join("")}</details>`:""):'<p class="context-note">这个产品还没有卖点，请先到卖点分级页新增。</p>'}</fieldset>${chosen&&chosen.tier!=="S"&&!existing?`<label class="field"><span>为什么用 ${esc(chosen.code)} 主打</span><textarea name="lead_reason" rows="2" maxlength="2000" placeholder="分级表要求每条内容由一个 S 主导；写明这次为什么例外">${esc(draft.lead_reason)}</textarea></label>`:""}`:`${step===1?progressionFields():""}<label class="field"><span>${step===1?"观众顾虑与回应顺序":"最后怎样收口？"}</span><textarea name="${step===1?"concern":"closing"}" rows="4" required maxlength="4000" placeholder="${step===1?"例如：装夹麻烦吗 → A3 一次装夹翻面继续 → 值不值":"例如：B3 3 年有限质保，或无购买收口"}">${esc(step===1?draft.concern:draft.closing)}</textarea></label>${step===1&&reviewConcerns.length?`<div class="direction-suggestions"><span>已关联评论顾虑 · 评论说法待核实</span>${reviewConcerns.map((text,index)=>`<button type="button" class="secondary" data-review-concern="${index}">${esc(text)}</button>`).join("")}</div>`:""}${candidates.length?`<div class="direction-suggestions"><span>点选已有表达</span>${candidates.map((text,index)=>`<button type="button" class="secondary" data-suggestion="${index}">${esc(text)}</button>`).join("")}</div>`:""}<div class="direction-suggestions"><span>点选卖点，追加到${step===1?"顾虑回应":"收口"}</span>${chainPoints.map((item,index)=>`<button type="button" class="secondary" data-chain-point="${index}">${esc(item.code)} · ${esc(item.title)}</button>`).join("")}</div>${step===2?`<div class="direction-chain-preview"><strong>完整方向链</strong><p>${esc([main,draft.concern,draft.closing].filter(Boolean).join(" → "))}</p></div>`:""}`}${existing?'<p class="context-note">这个主打卖点已有方向。本次顾虑与收口会作为执行变体保留。</p>':""}<p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span>独立保存方向，后续再关联案例与视频。</span><div><button type="button" class="secondary" data-back>${step?"上一步":"取消"}</button><button type="submit" class="primary" ${!points.length?"disabled":""}>${step===2?"保存方向":"下一步"}</button></div></div></form>`;
      dialog.querySelector("[data-close]").onclick=()=>{if(!pending)requestClose(dialog);};
      dialog.querySelector("[data-back]").onclick=()=>{if(pending)return;collect();if(step){step--;render();}else requestClose(dialog);};
      dialog.querySelectorAll('[name="lead_point"]').forEach(input=>input.onchange=()=>{
        collect();draft.point_id=input.value;render();
        dialog.querySelector('[name="lead_point"]:checked')?.focus({preventScroll:true});
      });
      const chainInput=()=>dialog.querySelector(`textarea[name="${step===1?"concern":"closing"}"]`);
      dialog.querySelectorAll("[data-review-concern]").forEach(button=>button.onclick=()=>{const textarea=chainInput();textarea.value=[textarea.value.trim(),reviewConcerns[Number(button.dataset.reviewConcern)]].filter(Boolean).join(" → ");textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      dialog.querySelectorAll("[data-suggestion]").forEach(button=>button.onclick=()=>{const textarea=chainInput();textarea.value=candidates[Number(button.dataset.suggestion)];textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      dialog.querySelectorAll("[data-chain-point]").forEach(button=>button.onclick=()=>{const textarea=chainInput(),item=chainPoints[Number(button.dataset.chainPoint)];textarea.value=[textarea.value.trim(),item.code+" "+item.title].filter(Boolean).join(" → ");textarea.dispatchEvent(new Event("input",{bubbles:true}));});
      if(step===2)chainInput().oninput=event=>{draft.closing=event.target.value;dialog.querySelector(".direction-chain-preview p").textContent=[main,draft.concern,draft.closing].filter(Boolean).join(" → ");};
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
          const result=await api("/api/direction-catalog",{method:"POST",body:{revision:catalog.revision,request_id:requestId,operation:"add_variant",direction:{product,selling_point_id:point().id,main_difference:main,concern:draft.concern,closing:draft.closing,status:"testing",...(progressionValue()?{progression:progressionValue()}:{}),...(draft.execution_change?{execution_change:draft.execution_change}:{}),...(draft.lead_reason?{lead_reason:draft.lead_reason}:{}),...(draft.audience?{audience:draft.audience}:{})}}});
          dialog.close();toast(result.existing?"执行变体已保存":"方向已保存");if(options.onSaved)await options.onSaved(result);
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
    dialog.innerHTML=`<form><div class="dialog-heading"><div><h2>编辑方向</h2><p>${esc(direction.direction_id)} · ${esc(direction.main_difference)}</p></div><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body"><label class="field"><span>默认顾虑链</span><textarea name="concern" rows="3" required maxlength="4000">${esc(direction.concern || "")}</textarea></label><label class="field"><span>默认收口</span><textarea name="closing" rows="3" required maxlength="4000">${esc(direction.closing || "")}</textarea></label><label class="field"><span>人工状态</span><select name="status">${Object.entries(statusNames).map(([value,label])=>`<option value="${value}" ${direction.status===value?"selected":""}>${label}</option>`).join("")}</select></label><label class="field"><span>目标人群</span><textarea name="audience" rows="2" maxlength="2000" placeholder="这条方向讲给谁听；卖点理由里不再写人群">${esc(direction.audience || "")}</textarea></label><label class="field"><span>方向备注</span><textarea name="notes" rows="3" maxlength="4000">${esc(direction.notes || "")}</textarea></label><p class="context-note">直接改方向的默认定义。旧定义进入“旧定义”记录，不会变成执行变体；案例当时的执行记录不变。</p><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer"><span></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存定义</button></div></div></form>`;
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
    const others=(catalog.selling_points || []).filter(item=>item.id!==found.id).sort((a,b)=>tierRank(a.code)-tierRank(b.code)||codeNo(a.code)-codeNo(b.code));
    const short=item=>`${item.code} ${String(item.title || "").slice(0,22)}`,sameFact=new Set(prior.same_fact_as || []);
    const mergeOptions=others.filter(item=>item.status!=="merged").map(item=>`<option value="${esc(item.code)}" ${prior.merged_into===item.code?"selected":""}>${esc(short(item))}</option>`).join("");
    const factChoices=others.map(item=>`<label class="point-fact-choice" title="${esc(item.title || "")}"><input type="checkbox" name="same_fact_as" value="${esc(item.code)}" ${sameFact.has(item.code)?"checked":""}><span>${esc(short(item))}</span></label>`).join("");
    const former=(prior.former_codes || []).length?` · 曾用编号 ${esc(prior.former_codes.join("、"))}，不再复用`:"";
    dialog.innerHTML=`<form><div class="dialog-heading"><h2>${adoptGrading?"采用分级表写法":id?"编辑卖点":"新增卖点"}</h2><button type="button" class="icon-button" data-close aria-label="关闭">×</button></div><div class="direction-wizard-body"><label class="field"><span>卖点编号</span><input name="code" pattern="[SABsab][1-9][0-9]*" placeholder="例如 S1、A2、B3" required value="${esc(prior.code || "")}"><small>首字母就是 S / A / B 分级${id?"；改编号或升降级时，其他卖点和方向定义里的引用会跟着改，方向编号不变":""}${former}</small></label><label class="field"><span>卖点标题</span><input name="title" required maxlength="200" value="${esc(prior.title || "")}"></label><label class="field"><span>分级理由</span><textarea name="rationale" rows="3" maxlength="4000">${esc(prior.rationale || "")}</textarea></label><label class="field"><span>证据与边界</span><textarea name="evidence" rows="3" maxlength="4000">${esc(prior.evidence || "")}</textarea></label><label class="field"><span>状态</span><select name="status" required><option value="">请选择</option>${Object.entries({testing:"在测 / 已核",candidate:"待核候选",merged:"已并入"}).map(([value,label])=>`<option value="${value}" ${prior.status===value?"selected":""}>${label}</option>`).join("")}</select><small>有销量由销售数据自动显示，不用选。没核实、没证明素材的选待核候选。</small></label><label class="field" data-merge-field ${prior.status==="merged"?"":"hidden"}><span>并入哪个卖点</span><select name="merged_into"><option value="">请选择</option>${mergeOptions}</select></label><fieldset class="field point-fact-picker"><legend>和哪些卖点是同一产品事实（可选）</legend>${factChoices?`<div>${factChoices}</div>`:"<small>这个产品还没有其他卖点。</small>"}</fieldset><label class="field"><span>购买动机有什么不同</span><textarea name="motive_difference" rows="2" maxlength="2000" placeholder="新增 S 和已有卖点是同一产品事实时必填；只是换了一类人，就写成方向的目标人群">${esc(prior.motive_difference || "")}</textarea></label><p class="direction-form-error" role="alert"></p></div><div class="dialog-footer">${id&&!adoptGrading?`<button type="button" class="danger-outline" data-delete-point>删除卖点</button>`:"<span></span>"}<div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary">保存卖点</button></div></div></form>`;
    dialog.querySelectorAll("[data-close]").forEach(button=>button.onclick=()=>requestClose(dialog));
    const form=dialog.querySelector("form"),alert=dialog.querySelector("[role=alert]"),mergeField=dialog.querySelector("[data-merge-field]");
    form.elements.status.onchange=()=>{mergeField.hidden=form.elements.status.value!=="merged";if(mergeField.hidden)form.elements.merged_into.value="";};
    let pending=false;
    const busy=value=>{pending=value;if(activeDraft)activeDraft.pending=value;dialog.querySelectorAll("button").forEach(button=>button.disabled=value);};
    const remove=dialog.querySelector("[data-delete-point]");
    if(remove)remove.onclick=async()=>{
      if(pending)return;
      // First click asks in place; a second dialog on top of this one is avoided.
      if(!remove.dataset.confirm){remove.dataset.confirm="1";remove.className="danger";remove.textContent=`确认删除 ${found.code}`;alert.textContent="删除后不能恢复；分级表文件不会改动，也不会再把它导回来。";return;}
      busy(true);
      try {await api("/api/selling-points",{method:"POST",body:{revision:catalog.revision,request_id:crypto.randomUUID(),operation:"delete",point:{id:found.id,product}}});dialog.close();toast(`已删除 ${found.code}`);void route();}
      catch(error){alert.textContent=error.message;delete remove.dataset.confirm;remove.className="danger-outline";remove.textContent="删除卖点";}
      finally{busy(false);}
    };
    form.onsubmit=async event=>{
      event.preventDefault();if(pending)return;
      const data=new FormData(event.currentTarget),values=Object.fromEntries(data);for(const key of Object.keys(values))values[key]=String(values[key]).trim();
      values.code=values.code.toUpperCase();values.same_fact_as=data.getAll("same_fact_as").map(String);
      busy(true);
      try {await api("/api/selling-points",{method:"POST",body:{revision:catalog.revision,request_id:requestId,point:{...prior,...values,product,tier:values.code[0]}}});dialog.close();toast(found.code&&found.code!==values.code?`已改为 ${values.code}，引用已同步`:"卖点已保存");void route();}
      catch(error){alert.textContent=error.message;}
      finally{busy(false);}
    };dialog.showModal();trackDraft(dialog);
  }
  async function route(force=false) {
    cancelFilterTimer();composing=false;
    const sequence=++requestSequence,values=routeValues();
    $("#breadcrumb").innerHTML="卖点与方向";
    const prior=listView;
    if(!force&&!values.id&&prior&&prior.form===$("#direction-filters")&&prior.values.hash!==values.hash
      &&prior.values.product===values.product&&prior.values.archive===values.archive
      &&prior.signature===state.signature&&prior.digest===state.digest){
      renderCatalog(prior.catalog,prior.summary,values);return;
    }
    listView=null;
    $("#content").innerHTML='<p role="status">正在读取卖点与方向…</p>';
    try {
      if(values.id){
        const data=await api(catalogURL(values.product,values.id));
        if(sequence!==requestSequence||pageHash()!==values.hash)return;
        // New servers return the names from the same snapshot as the detail.
        // Keep older running servers usable until they can be restarted.
        const points=Array.isArray(data.selling_points)?data.selling_points:
          (await api(catalogURL(values.product)).catch(()=>null))?.selling_points || [];
        if(sequence!==requestSequence||pageHash()!==values.hash)return;
        const detail=renderDetail(data,values,points);
        if(data.progress_available===true&&!window.PublicTextMode){
          const current=()=>sequence===requestSequence&&pageHash()===values.hash;
          const loadProgress=async()=>{
            if(!current())return;
            detail.loadingProgress();
            try{
              const response=await api("/api/direction-progress/"+encodeURIComponent(data.direction.direction_id)+"?"+new URLSearchParams({product:data.direction.product}));
              if(current())detail.setProgress(response);
            }catch(error){if(current())detail.progressError(error,()=>void loadProgress());}
          };
          requestAnimationFrame(()=>setTimeout(()=>{if(current())void loadProgress();},0));
        }
        if(data.tasks_pending===true){
          const current=()=>sequence===requestSequence&&pageHash()===values.hash;
          const loadTasks=async()=>{
            if(!current())return;
            detail.loadingTasks();
            try{
              const query=new URLSearchParams({product:data.direction.product,direction_id:data.direction.direction_id});
              const response=await api("/api/ai/tasks?"+query);
              if(current())detail.setTasks(response.tasks || []);
            }catch(error){if(current())detail.taskError(error,()=>void loadTasks());}
          };
          // Let the complete direction and its controls paint before reading task history.
          requestAnimationFrame(()=>setTimeout(()=>{if(current())void loadTasks();},0));
        }
        return;
      }
      const query=new URLSearchParams({archive:values.archive});if(values.product)query.set("product",values.product);
      const [catalog,summary]=await Promise.all([api(catalogURL(values.product)),api("/api/directions?"+query)]);
      if(sequence!==requestSequence||pageHash()!==values.hash)return;
      window.WorkbenchShell?.registerProducts([...(catalog.selling_points || []),...(catalog.directions || []),...(summary.products || [])]);
      renderCatalog(catalog,summary,values);
    }catch(error){if(sequence===requestSequence&&pageHash()===values.hash)showError(error.message);}
  }
  const closeMenus=event=>$$(".dir-menu[open]").forEach(menu=>{if(event.key==="Escape"||!menu.contains(event.target)||event.target.closest("[role=menuitem]"))menu.open=false;});
  window.addEventListener("click",closeMenus,true);window.addEventListener("keydown",event=>{if(event.key==="Escape")closeMenus(event);});
  window.addEventListener("hashchange",cancelFilterTimer);
  window.addEventListener("workbench:directionchange",()=>{listView=null;if(state.routeKind==="directions"&&!modalOpen())void route(true);});
  return {route,openCreate,openPointEditor,modalOpen,beforeNavigate};
})();
