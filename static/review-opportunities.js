"use strict";

window.ReviewOpportunities = (() => {
  let sequence = 0;
  const hash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const product = () => window.WorkbenchShell?.product() || "";
  const url = (path, selected = product()) => window.WorkbenchShell?.url(path, selected) || path;
  // Back to the comment list with the filters it was left on.
  const reviewsURL = () => window.WorkbenchShell?.listURL?.("reviews", product()) || url("#reviews");
  const safeURL = value => { try { const parsed = new URL(value); return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : ""; } catch { return ""; } };
  const active = () => hash().startsWith("#reviews") && new URLSearchParams(hash().split("?")[1] || "").get("tab") === "opportunities";
  const planLabels = {pending:"待执行",working:"进行中",done:"已完成",cancelled:"已取消"};
  const stateLabels = {complete:"三段齐",gaps:"有缺口",skip:"不值得做"};
  let poll = 0;
  const cites = ids => ids?.length ? `<span class="skeleton-cite" title="依据评论 ${esc(ids.join("、"))}">${ids.length} 条评论</span>` : "";

  // AI 三段骨架（爆款顺序：开头说到心里 → 中段卖点并且画面证明 → 收口下单理由）.
  function skeleton(item) {
    const sk = item.skeleton || {};
    if (sk.status === "unavailable") return "";
    const busy = sk.status === "queued" || sk.status === "running";
    const head = `<div class="skeleton-head"><strong>三段骨架</strong>${sk.state && stateLabels[sk.state] ? `<span class="skeleton-state" data-state="${esc(sk.state)}">${stateLabels[sk.state]}</span>` : ""}${sk.stale ? '<span class="skeleton-stale">评论有变化，可重新生成</span>' : ""}</div>`;
    if (busy) return `<div class="opportunity-skeleton">${head}<p class="skeleton-progress" role="status">${esc(sk.status === "queued" ? "排队中…" : sk.progress || "AI 正在生成…")}</p><button type="button" class="subtle" data-skeleton-stop>停止</button></div>`;
    if (!sk.opening) return `<div class="opportunity-skeleton">${head}${sk.error ? `<p class="skeleton-error">${esc(sk.error)}</p>` : '<p class="context-note">按爆款顺序把这组评论排成：开头说到心里 → 中段卖点并且画面证明 → 收口下单理由。</p>'}<button type="button" class="secondary" data-skeleton-run>${sk.error ? "再试一次" : "AI 出三段骨架"}</button></div>`;
    const rows = [
      ...sk.opening.map(row => `<li><b>开头</b><span>${esc(row.pain_zh)}${row.line_en ? `<small>${esc(row.line_en)}${row.line_zh ? `｜${esc(row.line_zh)}` : ""}</small>` : ""}${row.audience_zh ? `<small>筛出：${esc(row.audience_zh)}</small>` : ""}${cites(row.quotes)}</span></li>`),
      ...sk.middle.map(row => `<li><b>中段</b><span>${esc(row.code)} ${esc(row.title)}${row.status === "candidate" ? " · 待核" : ""}：${esc(row.answer_zh)}${row.shot_zh ? `<small>要拍：${esc(row.shot_zh)}</small>` : ""}${cites(row.quotes)}</span></li>`),
      sk.closing ? `<li><b>收口</b><span>${esc(sk.closing.code)} ${esc(sk.closing.title)}${sk.closing.status === "candidate" ? " · 待核" : ""}：${esc(sk.closing.reason_zh)}</span></li>` : ""];
    const notes = (sk.notes || []).map(text => `<li class="skeleton-note"><b>提醒</b>${esc(text)}</li>`);
    const gaps = sk.gaps.length || notes.length ? `<ul class="skeleton-gaps">${sk.gaps.map(row => `<li><b>${esc(row.label)}</b>${esc(row.text)}</li>`).join("")}${notes.join("")}</ul>` : "";
    const v = sk.verdict || {};
    const action = v.kind === "existing" && v.direction_id ? `<button type="button" class="secondary" data-skeleton-use="${esc(v.direction_id)}">用 ${esc(v.direction_id)} 写稿</button>`
      : v.kind === "new" && sk.middle.length ? '<button type="button" class="secondary" data-skeleton-direction>按骨架新建方向</button>' : "";
    return `<div class="opportunity-skeleton">${head}<ol class="skeleton-rows">${rows.join("")}</ol>${gaps}<p class="skeleton-verdict"><b>建议：${esc(v.label || "")}${v.direction_id ? ` ${esc(v.direction_id)}` : ""}</b>${v.reason_zh ? `　${esc(v.reason_zh)}` : ""}</p>${sk.error ? `<p class="skeleton-error">${esc(sk.error)}</p>` : ""}<div class="skeleton-actions">${action}<button type="button" class="subtle" data-skeleton-run>重新生成</button></div></div>`;
  }
  function newDirection(item) {
    const sk = item.skeleton, lead = sk.middle[0];
    if (!window.CaseDirections?.openCreate) {toast("方向页还没加载，请到「卖点与方向」新建。",true);return;}
    const concern = [sk.opening[0]?.pain_zh, ...sk.middle.map(row => `${row.code} ${row.answer_zh}`)].filter(Boolean).join(" → ");
    void window.CaseDirections.openCreate(lead.point_id,{source:`来自评论选题「${item.title}」的 AI 三段骨架，评论说法待核实，保存前逐项核对。`,
      prefill:{concern,audience:sk.opening[0]?.audience_zh || "",closing:sk.closing ? `${sk.closing.code} ${sk.closing.title}` : "",lead_reason:lead.code.startsWith("S") ? "" : sk.verdict?.reason_zh || ""}});
  }

  function evidence(row, index) {
    const source = safeURL(row.url);
    return `<details class="opportunity-evidence" ${index === 0 ? "open" : ""}><summary>${esc(row.platform_label || row.platform)} · ${esc(row.role_label || row.role)}${row.manual ? " · 手动标注" : ""}${row.recent ? " · 近期首次入库" : ""}</summary><blockquote class="opportunity-excerpt">${esc(row.text)}</blockquote><details class="opportunity-full"><summary>全文与来源</summary><blockquote>${esc(row.text)}</blockquote><p class="context-note">评论 ${esc(row.id)} · 评论者说法未经核实</p></details>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">打开原帖</a>` : ""}<button type="button" class="subtle" data-opportunity-link-index="${index}">关联这条评论</button></details>`;
  }
  function card(item, index) {
    const options = item.directions.filter(direction => direction.status !== "archived");
    return `<article class="opportunity-card" data-opportunity-index="${index}"><div class="opportunity-heading"><h2>${esc(item.topic === "none" ? "待整理证据池" : item.title)}</h2>${item.recent_reviews > 0 ? `<span class="opportunity-count">近 14 天新增 ${item.recent_reviews}</span>` : ""}</div>
      <ul class="opportunity-reasons">${item.priority_reasons.filter(reason => !/近\s*14\s*天/.test(reason) && reason !== "尚未关联方向").map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>
      ${skeleton(item)}
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
    if (item.refetch) {
      // The skeleton finished after the page loaded: read the brief again so writing carries it.
      const fresh = await api(`/api/reviews/opportunities?${new URLSearchParams({product:item.product,limit:"100"})}`);
      const found = fresh.items.find(row => row.id === item.id);
      if (found) Object.assign(item,found,{refetch:false});
    }
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
    $("#content").innerHTML = `<section id="review-opportunities"><div class="page-heading"><div><h1>评论选题机会</h1><nav class="rev-page-tabs" aria-label="用户声音视图"><a href="${esc(reviewsURL())}">评论</a><span aria-current="page">选题机会</span></nav></div><a class="secondary" href="${esc(reviewsURL())}">返回评论库</a></div><div id="opportunity-results" role="status">正在读取评论主题…</div></section>`;
    try {
      const data = await api(`/api/reviews/opportunities?${new URLSearchParams({product:selected,limit:"20",offset:String(offset)})}`);
      if (!current()) return;
      const host = $("#opportunity-results");host.removeAttribute("role");
      if (data.requires_product) {host.innerHTML = '<div class="empty"><h2>先选择一个产品</h2><p>选题只汇总同一产品的评论，请在侧栏选择具体产品。</p></div>';return;}
      host.innerHTML = `<p class="opportunity-boundary">评论者说法未经核实。</p><div class="opportunity-skeleton-bar" data-skeleton-bar></div><details class="opportunity-rules"><summary>来源与排序口径</summary><p>${esc(data.notice)}</p><p>${esc(data.ranking)}“近期”按首次入库日期（${esc(data.window.since)} 至 ${esc(data.window.as_of)}），不是评论发布日期。</p></details>${data.items.length ? `<div class="opportunity-grid">${data.items.map((item,index) => item.topic !== "none" ? card(item,index) : "").join("")}</div>${data.items.some(item => item.topic === "none") ? `<section class="opportunity-pool"><h2>待整理证据池</h2><p class="context-note">这些评论尚未形成具体主题。先核对标签或选择原文，不计作已识别的选题。</p>${data.items.map((item,index) => item.topic === "none" ? card(item,index) : "").join("")}</section>` : ""}` : '<div class="empty"><h2>还没有可用的评论主题</h2><p>回评论库同步当前产品的评论，再核对标签和主题关联。</p></div>'}<nav class="opportunity-pagination" aria-label="选题分页"><span>共 ${data.total} 个主题</span>${data.offset ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${Math.max(0,data.offset-data.limit)}`))}">上一页</a>` : ""}${data.offset+data.limit<data.total ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${data.offset+data.limit}`))}">下一页</a>` : ""}<button type="button" class="subtle" data-opportunity-refresh>重新读取</button></nav>`;
      const pending = () => data.items.filter(item => item.topic !== "none" && !item.skeleton?.opening && !["queued","running"].includes(item.skeleton?.status));
      const running = () => data.items.some(item => ["queued","running"].includes(item.skeleton?.status));
      const drawBar = finished => {
        const bar = host.querySelector("[data-skeleton-bar]");if (!bar) return;
        const left = pending().slice(0,10);
        bar.innerHTML = running() ? '<span role="status">AI 正在逐个出三段骨架，一次一个主题。</span><button type="button" class="subtle" data-skeleton-stop-all>全部停止</button>'
          : finished ? '<span role="status">骨架生成完了，重新读取后按骨架排序：三段齐的在前，有缺口的其次，没生成的在后。</span><button type="button" class="secondary" data-opportunity-refresh>按骨架重新排序</button>'
          : left.length ? `<button type="button" class="secondary" data-skeleton-batch>给本页 ${left.length} 个主题出三段骨架</button><span class="context-note">一次一个主题，按 AI 设置里的模型；只读评论和卖点库，不改任何资料。</span>` : "";
      };
      const redraw = item => {
        const article = host.querySelector(`[data-opportunity-index="${data.items.indexOf(item)}"]`);
        const block = article?.querySelector(".opportunity-skeleton"), html = skeleton(item);
        if (block) block.outerHTML = html;
      };
      const apply = states => {
        let finished = false;
        for (const item of data.items) {
          const next = states.themes[item.id];if (!next) continue;
          const before = item.skeleton || {};
          if (before.status === next.status && before.finished_at === next.finished_at && before.progress === next.progress) continue;
          if (["queued","running"].includes(before.status) && !["queued","running"].includes(next.status)) finished = true;
          item.skeleton = {...next,stale:false};
          if (next.status === "done") item.refetch = true;
          redraw(item);
        }
        drawBar(finished && !states.running);
      };
      const watch = () => {
        clearTimeout(poll);
        poll = setTimeout(async () => {
          if (!current()) return;
          try {const states = await api(`/api/reviews/opportunities/skeletons?${new URLSearchParams({product:selected})}`);if (!current()) return;apply(states);if (states.running) watch();}
          catch {if (current()) watch();}
        },3000);
      };
      const start = async themes => {
        const states = await api("/api/reviews/opportunities/skeletons",{method:"POST",body:{product:selected,themes}});
        apply(states);if (states.running) watch();
      };
      drawBar(false);
      if (running()) watch();
      host.onclick = async event => {
        const button = event.target.closest("button"), article = event.target.closest("[data-opportunity-index]");
        if (!button || button.disabled) return;
        if (button.hasAttribute("data-opportunity-refresh")) {void route();return;}
        if (button.hasAttribute("data-skeleton-batch") || button.hasAttribute("data-skeleton-stop-all") || button.hasAttribute("data-skeleton-stop")) {
          button.disabled = true;
          try {
            if (button.hasAttribute("data-skeleton-batch")) await start(pending().slice(0,10).map(item => item.id));
            else apply(await api("/api/reviews/opportunities/skeletons/stop",{method:"POST",body:{product:selected}}));
          } catch (error) {toast(error.message || "操作未完成，请重试。",true);} finally {if(button.isConnected) button.disabled=false;}
          return;
        }
        const item = data.items[Number(article?.dataset.opportunityIndex)];if (!item) return;
        button.disabled = true;
        try {
          if (button.hasAttribute("data-skeleton-run")) await start([item.id]);
          if (button.hasAttribute("data-skeleton-direction")) newDirection(item);
          if (button.hasAttribute("data-skeleton-use")) {
            const select = article.querySelector("[data-opportunity-direction]");
            if (select && [...select.options].some(option => option.value === button.dataset.skeletonUse)) select.value = button.dataset.skeletonUse;
            else {
              // The suggested direction is not linked to this theme yet: write from it directly.
              const catalog = await api("/api/direction-catalog?product=" + encodeURIComponent(item.product));
              const direction = catalog.directions?.find(row => row.direction_id === button.dataset.skeletonUse);
              if (!direction) throw new Error("建议的方向已不存在，请刷新。");
              item.directions = [...item.directions,{...direction,scripts:0,published:0,association_basis:"AI 骨架建议"}];
              if (select) select.insertAdjacentHTML("beforeend",`<option value="${esc(direction.direction_id)}">${esc(direction.direction_id)}</option>`);
              else {const own = document.createElement("select");own.hidden = true;own.dataset.opportunityDirection = "";own.innerHTML = `<option value="${esc(direction.direction_id)}"></option>`;article.append(own);}
              const target = article.querySelector("[data-opportunity-direction]");target.value = direction.direction_id;
            }
            await write(item,article);
          }
          if (button.hasAttribute("data-opportunity-write")) await write(item,article);
          if (button.hasAttribute("data-opportunity-link-index")) await window.MaterialBasket?.openTheme(item.evidence[Number(button.dataset.opportunityLinkIndex)]);
        } catch (error) {toast(error.message || "操作未完成，请重试。",true);} finally {if(button.isConnected) button.disabled=false;}
      };
    } catch (error) {if(current()) {$("#opportunity-results").innerHTML = `<div class="empty"><h2>选题机会暂不可用</h2><p>${esc(error.message)}</p><button type="button" class="secondary" id="opportunity-retry">重试</button></div>`;$("#opportunity-retry").onclick=route;}}
  }
  return {active,route};
})();
