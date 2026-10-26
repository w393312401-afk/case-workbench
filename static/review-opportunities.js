"use strict";

window.ReviewOpportunities = (() => {
  let sequence = 0;
  const hash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const product = () => window.WorkbenchShell?.product() || "";
  const url = (path, selected = product()) => window.WorkbenchShell?.url(path, selected) || path;
  const safeURL = value => { try { const parsed = new URL(value); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : ""; } catch { return ""; } };
  const active = () => hash().startsWith("#reviews") && new URLSearchParams(hash().split("?")[1] || "").get("tab") === "opportunities";
  const planLabels = {pending:"待执行",working:"进行中",done:"已完成",cancelled:"已取消"};

  function evidence(row, index) {
    const source = safeURL(row.url);
    return `<details class="opportunity-evidence" ${index === 0 ? "open" : ""}><summary>${esc(row.platform_label || row.platform)} · ${esc(row.role_label || row.role)}${row.manual ? " · 手动标注" : ""}${row.recent ? " · 近期首次入库" : ""}</summary><blockquote class="opportunity-excerpt">${esc(row.text)}</blockquote><details class="opportunity-full"><summary>全文与来源</summary><blockquote>${esc(row.text)}</blockquote><p class="context-note">评论 ${esc(row.id)} · 评论者说法未经核实</p></details>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">打开原帖</a>` : ""}<button type="button" class="subtle" data-opportunity-link-index="${index}">关联这条评论</button></details>`;
  }
  function card(item, index) {
    const options = item.directions.filter(direction => direction.status !== "archived");
    return `<article class="opportunity-card" data-opportunity-index="${index}"><div class="opportunity-heading"><h2>${esc(item.topic === "none" ? "待整理证据池" : item.title)}</h2>${item.recent_reviews > 0 ? `<span class="opportunity-count">近 14 天新增 ${item.recent_reviews}</span>` : ""}</div>
      <ul class="opportunity-reasons">${item.priority_reasons.filter(reason => !/近\s*14\s*天/.test(reason) && reason !== "尚未关联方向").map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>
      <p class="context-note">${item.source_counts.map(source => `${esc(source.label)} ${source.count}`).join(" · ")}${item.duplicate_rows ? ` · 合并 ${item.duplicate_rows} 条重复原文` : ""}</p>
      <div class="opportunity-quotes">${item.evidence.map(evidence).join("")}</div>
      <div class="opportunity-coverage">${item.directions.length ? item.directions.map(direction => `<p><a href="${esc(url(`#directions/${encodeURIComponent(direction.direction_id)}`,item.product))}">${esc(direction.direction_id)} · ${esc(direction.main_difference)}</a> · ${direction.scripts} 条脚本 / ${direction.published} 条已发布${direction.status === "archived" ? " · 已归档" : ""}<small>${esc(direction.association_basis)}；发布覆盖不等于已验证有效。</small></p>`).join("") : '<p>尚未关联方向，先核对原文并关联卖点或方向顾虑。</p>'}
      ${item.selling_points.length ? `<p>已关联卖点：${item.selling_points.map(point => `${esc(point.code)} · ${esc(point.title)}`).join("；")}</p>` : ""}
      ${item.plans.length ? `<details><summary>关联计划 ${item.plans.length} 项</summary>${item.plans.map(plan => `<p>${esc(plan.direction_id)} · ${esc(planLabels[plan.status] || plan.status)}：${esc(plan.experiment?.hypothesis || plan.note || "未填写说明")}${plan.experiment?.change ? `<small>本轮改动：${esc(plan.experiment.change)}</small>` : ""}</p>`).join("")}</details>` : ""}</div>
      <div class="opportunity-actions">${options.length ? `<label>带入方向 <select data-opportunity-direction aria-label="${esc(item.title)}的写稿方向"><option value="">空白写稿，不指定方向</option>${options.map(direction => `<option value="${esc(direction.direction_id)}">${esc(direction.direction_id)}</option>`).join("")}</select></label>` : ""}<button type="button" class="primary" data-opportunity-write>带证据写稿</button>${item.topic ? `<a class="subtle" href="${esc(url(`#reviews?topic=${encodeURIComponent(item.topic)}`,item.product))}">查看同主题评论</a>` : ""}</div>
      </article>`;
  }
  async function write(item, host) {
    if (window.CaseAI?.modalOpen()) {toast("请先完成或关闭当前写稿窗口。",true);return;}
    const directionId = host.querySelector("[data-opportunity-direction]")?.value;
    const direction = item.directions.find(row => row.direction_id === directionId);
    const opened = direction ? await window.CaseAI?.startFromDirection({...direction,plan_note:item.draft_instruction}) : await window.CaseAI?.startBlank(item.product);
    if (!opened) return;
    const input = document.querySelector("#ai-create-instruction");
    // A pending request/draft owned by the AI dialog must keep its original text.
    if (input && !input.value.trim()) {input.value = item.draft_instruction;input.dispatchEvent(new Event("input",{bubbles:true}));}
    else if (input?.value !== item.draft_instruction) toast("已打开保留的写稿请求。原请求未覆盖；需要新的选题时请先完成当前请求。",true);
  }
  async function route() {
    const mine = ++sequence, selected = product(), requestedHash = location.hash;
    const current = () => mine === sequence && active() && location.hash === requestedHash && product() === selected;
    const offset = Math.max(0,Number(new URLSearchParams(hash().split("?")[1] || "").get("offset")) || 0);
    $("#breadcrumb").textContent = "用户声音 · 选题机会";
    $("#content").innerHTML = `<section id="review-opportunities"><div class="page-heading"><div><h1>评论选题机会</h1><nav class="rev-page-tabs" aria-label="用户声音视图"><a href="${esc(url("#reviews"))}">评论</a><span aria-current="page">选题机会</span></nav></div><a class="secondary" href="${esc(url("#reviews"))}">返回评论库</a></div><div id="opportunity-results" role="status">正在读取评论主题…</div></section>`;
    try {
      const data = await api(`/api/reviews/opportunities?${new URLSearchParams({product:selected,limit:"20",offset:String(offset)})}`);
      if (!current()) return;
      const host = $("#opportunity-results");host.removeAttribute("role");
      if (data.requires_product) {host.innerHTML = '<div class="empty"><h2>先选择一个产品</h2><p>选题只汇总同一产品的评论，请在侧栏选择具体产品。</p></div>';return;}
      host.innerHTML = `<p class="opportunity-boundary">评论者说法未经核实。</p><details class="opportunity-rules"><summary>来源与排序口径</summary><p>${esc(data.notice)}</p><p>${esc(data.ranking)}“近期”按首次入库日期（${esc(data.window.since)} 至 ${esc(data.window.as_of)}），不是评论发布日期。</p></details>${data.items.length ? `<div class="opportunity-grid">${data.items.map((item,index) => item.topic !== "none" ? card(item,index) : "").join("")}</div>${data.items.some(item => item.topic === "none") ? `<section class="opportunity-pool"><h2>待整理证据池</h2><p class="context-note">这些评论尚未形成具体主题。先核对标签或选择原文，不计作已识别的选题。</p>${data.items.map((item,index) => item.topic === "none" ? card(item,index) : "").join("")}</section>` : ""}` : '<div class="empty"><h2>还没有可用的评论主题</h2><p>回评论库同步当前产品的评论，再核对标签和主题关联。</p></div>'}<nav class="opportunity-pagination" aria-label="选题分页"><span>共 ${data.total} 个主题</span>${data.offset ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${Math.max(0,data.offset-data.limit)}`))}">上一页</a>` : ""}${data.offset+data.limit<data.total ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${data.offset+data.limit}`))}">下一页</a>` : ""}<button type="button" class="subtle" data-opportunity-refresh>重新读取</button></nav>`;
      host.onclick = async event => {
        const button = event.target.closest("button"), article = event.target.closest("[data-opportunity-index]");
        if (!button || button.disabled) return;
        if (button.hasAttribute("data-opportunity-refresh")) {void route();return;}
        const item = data.items[Number(article?.dataset.opportunityIndex)];if (!item) return;
        button.disabled = true;
        try {
          if (button.hasAttribute("data-opportunity-write")) await write(item,article);
          if (button.hasAttribute("data-opportunity-link-index")) await window.MaterialBasket?.openTheme(item.evidence[Number(button.dataset.opportunityLinkIndex)]);
        } catch (error) {toast(error.message || "操作未完成，请重试。",true);} finally {if(button.isConnected) button.disabled=false;}
      };
    } catch (error) {if(current()) {$("#opportunity-results").innerHTML = `<div class="empty"><h2>选题机会暂不可用</h2><p>${esc(error.message)}</p><button type="button" class="secondary" id="opportunity-retry">重试</button></div>`;$("#opportunity-retry").onclick=route;}}
  }
  return {active,route};
})();
