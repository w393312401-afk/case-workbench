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
    const head = `<div class="skeleton-head"><strong>三段骨架</strong>${sk.state && stateLabels[sk.state] ? `<span class="skeleton-state" data-state="${esc(sk.state)}">${stateLabels[sk.state]}</span>` : ""}${sk.stale ? `<span class="skeleton-stale" title="${esc(sk.stale_reason || "评论有变化或资料待更新，重新生成后可带入写稿")}">已过期</span>` : ""}</div>`;
    if (busy) return `<div class="opportunity-skeleton">${head}<p class="skeleton-progress" role="status">${esc(sk.status === "queued" ? "排队中…" : sk.progress || "AI 正在生成…")}${"model" in sk ? ` · 模型：${esc(window.ModelPick?.describe(sk.model,sk.reasoning_effort) || sk.model || "本机默认模型")}` : ""}</p><button type="button" class="subtle" data-skeleton-stop>停止</button></div>`;
    if (!sk.opening) return `<div class="opportunity-skeleton">${head}${sk.error ? `<p class="skeleton-error">${esc(sk.error)}</p>` : '<p class="context-note">按爆款顺序把这组评论排成：开头说到心里 → 中段卖点并且画面证明 → 收口下单理由。</p>'}<button type="button" class="secondary" data-skeleton-run>${sk.error ? "再试一次" : "AI 出三段骨架"}</button></div>`;
    const rows = [
      ...sk.opening.map(row => `<li><b>开头</b><span>${row.kind_label ? `<small>${esc(row.kind_label)}</small>` : ""}${esc(row.pain_zh)}${row.belief_zh ? `<small>观众的看法：${esc(row.belief_zh)}</small>` : ""}${row.concede_zh ? `<small>先认同：${esc(row.concede_zh)}</small>` : ""}${row.need_zh ? `<small>再转到：${esc(row.need_zh)}</small>` : ""}${row.line_en ? `<small>${esc(row.line_en)}${row.line_zh ? `｜${esc(row.line_zh)}` : ""}</small>` : ""}${row.audience_zh ? `<small>筛出：${esc(row.audience_zh)}</small>` : ""}${cites(row.quotes)}</span></li>`),
      ...sk.middle.map(row => `<li><b>中段</b><span>${esc(row.code)} ${esc(row.title)}${row.status === "candidate" ? " · 待核" : ""}：${esc(row.answer_zh)}${row.shot_zh ? `<small>要拍：${esc(row.shot_zh)}</small>` : ""}${cites(row.quotes)}</span></li>`),
      sk.closing ? `<li><b>收口</b><span>${esc(sk.closing.code)} ${esc(sk.closing.title)}${sk.closing.status === "candidate" ? " · 待核" : ""}：${esc(sk.closing.reason_zh)}</span></li>` : ""];
    const notes = (sk.notes || []).map(text => `<li class="skeleton-note"><b>提醒</b>${esc(text)}</li>`);
    const gaps = sk.gaps.length || notes.length ? `<ul class="skeleton-gaps">${sk.gaps.map(row => `<li><b>${esc(row.label)}</b>${esc(row.text)}</li>`).join("")}${notes.join("")}</ul>` : "";
    const v = sk.verdict || {};
    const disabled = sk.stale ? ' disabled title="先重新生成骨架"' : "";
    const action = sk.stale ? `<button type="button" class="secondary" data-skeleton-run title="骨架过期，带不进写稿；先重新生成，完成后再写">先重新生成再写稿</button>`
      : v.kind === "existing" && v.direction_id ? `<button type="button" class="secondary" data-skeleton-use="${esc(v.direction_id)}"${disabled}>用 ${esc(v.direction_id)} 写稿</button>`
      : v.kind === "new" && sk.middle.length ? `<button type="button" class="secondary" data-skeleton-direction${disabled}>按骨架新建方向</button>` : "";
    const counts = `开头 ${sk.opening.length} · 中段 ${sk.middle.length}${sk.closing ? " · 收口 1" : ""}${sk.gaps.length ? ` · 缺口 ${sk.gaps.length}` : ""}`;
    return `<div class="opportunity-skeleton">${head}<details class="skeleton-body"><summary>看骨架：${counts}</summary><ol class="skeleton-rows">${rows.join("")}</ol>${gaps}</details><p class="skeleton-verdict"><b>建议：${esc(v.label || "")}${v.direction_id ? ` ${esc(v.direction_id)}` : ""}</b>${v.reason_zh ? `　${esc(v.reason_zh)}` : ""}</p>${sk.error ? `<p class="skeleton-error">${esc(sk.error)}</p>` : ""}<div class="skeleton-actions">${action}${sk.stale ? "" : '<button type="button" class="subtle" data-skeleton-run>重新生成</button>'}</div></div>`;
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
    return `<details class="opportunity-evidence"><summary>${esc(row.platform_label || row.platform)} · ${esc(row.role_label || row.role)}${row.manual ? " · 手动标注" : ""}${row.recent ? " · 近期首次入库" : ""}</summary><blockquote class="opportunity-excerpt">${esc(row.text)}</blockquote>${window.ReviewZh?.markup(row, "opportunity-zh") || ""}<details class="opportunity-full"><summary>全文与来源</summary><blockquote>${esc(row.text)}</blockquote><p class="context-note">评论 ${esc(row.id)} · 评论者说法未经核实</p></details>${source ? `<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">打开原帖</a>` : ""}<button type="button" class="subtle" data-opportunity-link-index="${index}">关联这条评论</button></details>`;
  }
  // 画布「选题机会」: one row per topic — new comments, people stuck before buying, the skeleton's state, the suggested
  // direction and the write button. The skeleton, evidence and coverage open underneath on demand.
  function rowState(item) {
    const sk = item.skeleton || {};
    if (sk.status === "queued" || sk.status === "running") return '<span class="opportunity-state is-busy">生成中</span>';
    if (!sk.opening) return '<span class="opportunity-state is-none">还没生成</span>';
    return `<span class="opportunity-state" data-state="${esc(sk.state || "")}">${esc(stateLabels[sk.state] || "已生成")}${sk.stale ? " · 过期" : ""}</span>`;
  }
  function rowDirection(item) {
    const verdict = item.skeleton?.opening ? item.skeleton.verdict || {} : {};
    if (verdict.kind === "existing" && verdict.direction_id) return `补进 ${esc(verdict.direction_id)}`;
    if (verdict.kind === "new") return "新开方向";
    if (verdict.kind === "skip") return "不值得做";
    return item.directions.length ? esc(item.directions.map(direction => direction.direction_id).join("、")) : "—";
  }
  function card(item, index) {
    const options = item.directions.filter(direction => direction.status !== "archived");
    const sources = item.source_counts.map(source => `${esc(source.label)} ${source.count}`).join(" · ");
    return `<article class="opportunity-card opportunity-row" data-opportunity-index="${index}">
      <div class="opportunity-line">
        <div class="opportunity-heading"><h2>${esc(item.topic === "none" ? "待整理证据池" : item.title)}</h2><small>${item.distinct_reviews} 种不同原文 · ${item.source_counts.length} 个来源</small></div>
        <span class="opportunity-num" title="近 14 天首次入库">${item.recent_reviews || "—"}</span>
        <span class="opportunity-num" title="问价格或入口、提问、质疑">${item.intent_reviews || "—"}</span>
        ${item.topic === "none" ? "<span></span>" : rowState(item)}
        <span class="opportunity-direction">${rowDirection(item)}</span>
        <div class="opportunity-actions">${options.length ? `<select data-opportunity-direction aria-label="${esc(item.title)}的写稿方向"><option value="">不指定方向</option>${options.map(direction => `<option value="${esc(direction.direction_id)}">${esc(direction.direction_id)}</option>`).join("")}</select>` : ""}<button type="button" class="secondary compact" data-opportunity-write>带证据写稿</button></div>
      </div>
      <details class="opportunity-detail"><summary>看骨架与证据</summary>
        <ul class="opportunity-reasons">${item.priority_reasons.filter(reason => !/近\s*14\s*天/.test(reason) && reason !== "尚未关联方向").map(reason => `<li>${esc(reason)}</li>`).join("")}</ul>
        ${skeleton(item)}
        <p class="context-note">${sources}${item.duplicate_rows ? ` · 合并 ${item.duplicate_rows} 条重复原文` : ""}</p>
        <details class="opportunity-quotes-fold"><summary>证据评论 ${item.evidence.length} 条</summary><div class="opportunity-quotes">${item.evidence.map(evidence).join("")}</div></details>
        <div class="opportunity-coverage">${item.directions.map(direction => `<p><a href="${esc(url(`#directions/${encodeURIComponent(direction.direction_id)}`,item.product))}">${esc(direction.direction_id)} · ${esc(direction.main_difference)}</a> · ${direction.scripts} 条脚本 / ${direction.published} 条已发布${direction.status === "archived" ? " · 已归档" : ""}<small>${esc(direction.association_basis)}；发布覆盖不等于已验证有效。</small></p>`).join("")}
        ${item.selling_points.length ? `<p>已关联卖点：${item.selling_points.map(point => `${esc(point.code)} · ${esc(point.title)}`).join("；")}</p>` : ""}
        ${item.plans.length ? `<details><summary>关联计划 ${item.plans.length} 项</summary>${item.plans.map(plan => `<p>${esc(plan.direction_id)} · ${esc(planLabels[plan.status] || plan.status)}：${esc(plan.experiment?.hypothesis || plan.note || "未填写说明")}${plan.experiment?.change ? `<small>本轮改动：${esc(plan.experiment.change)}</small>` : ""}</p>`).join("")}</details>` : ""}</div>
        ${item.topic ? `<a class="subtle" href="${esc(url(`#reviews?topic=${encodeURIComponent(item.topic)}`,item.product))}">查看同主题评论</a>` : ""}
      </details>
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
    $("#content").innerHTML = `<section id="review-opportunities"><div class="page-heading"><div><h1>${esc(selected ? `${selected} 选题机会` : "评论选题机会")}</h1><nav class="rev-page-tabs" aria-label="用户声音视图"><a href="${esc(reviewsURL())}">评论</a><span aria-current="page">选题机会</span></nav></div><a class="secondary" href="${esc(reviewsURL())}">返回评论库</a></div><div id="opportunity-results" role="status">正在读取评论主题…</div></section>`;
    try {
      const data = await api(`/api/reviews/opportunities?${new URLSearchParams({product:selected,limit:"20",offset:String(offset)})}`);
      if (!current()) return;
      const host = $("#opportunity-results");host.removeAttribute("role");
      if (data.requires_product) {
        const summary = await api("/api/reviews/summary").catch(() => null);
        if (!current()) return;
        const products = (summary?.products || []).filter(Boolean);
        host.innerHTML = `<div class="empty"><h2>先选一个产品</h2><p>选题只汇总同一产品的评论。</p>${products.length ? `<div class="opportunity-products">${products.map(name => `<a class="secondary" href="${esc(url("#reviews?tab=opportunities", name))}">${esc(name)}</a>`).join("")}</div>` : "<p>评论库里还没有关联产品的评论。</p>"}</div>`;
        return;
      }
      const unlinkedThemes = data.items.filter(item => item.topic !== "none" && !item.directions.length).length;
      host.innerHTML = `<p class="opportunity-boundary">${data.total} 个主题 · 按想买但卡住的人数排 · 评论者说法未经核实。${unlinkedThemes ? `本页 ${unlinkedThemes} 个主题尚未关联方向，写稿前先核对原文并关联卖点或方向顾虑。` : ""}</p><div class="opportunity-skeleton-bar" data-skeleton-bar></div><details class="opportunity-rules"><summary>来源与排序口径</summary><p>${esc(data.notice)}</p><p>${esc(data.ranking)}“近期”按首次入库日期（${esc(data.window.since)} 至 ${esc(data.window.as_of)}），不是评论发布日期。</p></details>${data.items.length ? `<div class="opportunity-table-head" aria-hidden="true"><span>主题</span><span>近 14 天新增</span><span>想买但卡住</span><span>三段骨架</span><span>建议方向</span><span></span></div><div class="opportunity-grid">${data.items.map((item,index) => item.topic !== "none" ? card(item,index) : "").join("")}</div>${data.items.some(item => item.topic === "none") ? `<section class="opportunity-pool"><h2>待整理证据池</h2><p class="context-note">这些评论尚未形成具体主题。先核对标签或选择原文，不计作已识别的选题。</p>${data.items.map((item,index) => item.topic === "none" ? card(item,index) : "").join("")}</section>` : ""}` : '<div class="empty"><h2>还没有可用的评论主题</h2><p>回评论库同步当前产品的评论，再核对标签和主题关联。</p></div>'}<nav class="opportunity-pagination" aria-label="选题分页"><span>共 ${data.total} 个主题</span>${data.offset ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${Math.max(0,data.offset-data.limit)}`))}">上一页</a>` : ""}${data.offset+data.limit<data.total ? `<a href="${esc(url(`#reviews?tab=opportunities&offset=${data.offset+data.limit}`))}">下一页</a>` : ""}<button type="button" class="subtle" data-opportunity-refresh>重新读取</button></nav>`;
      const pending = () => data.items.filter(item => item.topic !== "none" && (!item.skeleton?.opening || item.skeleton?.stale) && !["queued","running"].includes(item.skeleton?.status));
      const staleCount = () => data.items.filter(item => item.topic !== "none" && item.skeleton?.opening && item.skeleton?.stale && !["queued","running"].includes(item.skeleton?.status)).length;
      const running = () => data.items.some(item => ["queued","running"].includes(item.skeleton?.status));
      const drawBar = finished => {
        const bar = host.querySelector("[data-skeleton-bar]");if (!bar) return;
        const left = pending().slice(0,20);
        bar.innerHTML = running() ? '<span role="status">AI 正在逐个出三段骨架，一次一个主题。</span><button type="button" class="subtle" data-skeleton-stop-all>全部停止</button>'
          : finished ? '<span role="status">骨架生成完了，重新读取后按骨架排序：三段齐的在前，有缺口的其次，没生成的在后。</span><button type="button" class="secondary" data-opportunity-refresh>按骨架重新排序</button>'
          : left.length ? `${staleCount() ? `<strong class="skeleton-stale-count">${staleCount()} 个骨架已过期，带不进写稿</strong>` : ""}<button type="button" class="${staleCount() ? "primary" : "secondary"}" data-skeleton-batch>${staleCount() ? `全部重新生成（${left.length} 个）` : `给本页 ${left.length} 个主题出三段骨架`}</button>${window.ModelPick?.markup("skeleton") || ""}<span class="context-note">后台一次跑一个主题，进度看任务面板；只读评论和卖点库，不改任何资料。</span>` : "";
        void window.ModelPick?.mount(bar);
      };
      const redraw = item => {
        const article = host.querySelector(`[data-opportunity-index="${data.items.indexOf(item)}"]`);
        const block = article?.querySelector(".opportunity-skeleton"), html = skeleton(item);
        if (block) block.outerHTML = html;
        const state = article?.querySelector(".opportunity-state");
        if (state) state.outerHTML = rowState(item);
        const suggestion = article?.querySelector(".opportunity-direction");
        if (suggestion) suggestion.innerHTML = rowDirection(item);
      };
      const apply = states => {
        let finished = false;
        for (const item of data.items) {
          let next = states.themes[item.id];if (!next) continue;
          const before = item.skeleton || {};
          // Polling has source versions but no current comment basis. Keep a known comment change
          // until a newly generated skeleton arrives or the opportunity catalog is read again.
          if (before.stale && !next.stale && before.generated_at === next.generated_at) next = {...next,stale:true,stale_reason:before.stale_reason};
          if (before.status === next.status && before.finished_at === next.finished_at && before.progress === next.progress && before.stale === next.stale && before.stale_reason === next.stale_reason) continue;
          if (["queued","running"].includes(before.status) && !["queued","running"].includes(next.status)) finished = true;
          item.skeleton = next;
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
        const states = await api("/api/reviews/opportunities/skeletons",{method:"POST",body:{product:selected,themes,...(window.ModelPick?.value("skeleton") || {})}});
        window.ModelPick?.used("skeleton");
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
            if (button.hasAttribute("data-skeleton-batch")) await start(pending().slice(0,20).map(item => item.id));
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
