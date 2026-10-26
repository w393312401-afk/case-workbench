"use strict";

// The calendar week is independent of the last week for which exports exist.
// One counted tab list keeps the work waiting on you together; trends live in Video Data.
window.WorkbenchHome = (() => {
  let sequence = 0, chosenTab = null, undoTimer = null, renderedProduct = null;
  let repaintLibrary = null;
  // Last data per product: a refresh keeps the old numbers on screen until each new read arrives.
  const cache = new Map(), pointTitles = new Map();
  const url = (hash, product) => window.WorkbenchShell?.url(hash, product) || hash;
  const selectedProduct = () => window.WorkbenchShell?.product() || "";
  const matches = item => !window.WorkbenchShell || window.WorkbenchShell.matchesProduct(item.product);
  const localISO = day => `${day.getFullYear()}-${String(day.getMonth()+1).padStart(2,"0")}-${String(day.getDate()).padStart(2,"0")}`;
  function currentWeek(now = new Date()) {
    const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    monday.setDate(monday.getDate() - (monday.getDay()+6)%7);
    const sunday = new Date(monday); sunday.setDate(sunday.getDate()+6);
    return { start: localISO(monday), end: localISO(sunday), today: localISO(now) };
  }
  function dueVideos(videos, week = currentWeek()) {
    return videos.filter(item => matches(item) && item.window_end && item.window_end <= week.end)
      .sort((a,b) => String(a.window_end).localeCompare(String(b.window_end)) || String(a.video_id).localeCompare(String(b.video_id)));
  }
  // Direction and publishing only apply to our own videos: references from overseas creators or the old library never get them.
  // A video counts as published once the sales data knows it or a script registered it, even if the case was saved earlier.
  function missingCase(item, published = null) {
    const current = item.current || {}, missing = [];
    const script = typeof transcriptDisplay === "function" ? transcriptDisplay(item).text : current.transcript?.text || current.script?.text || "";
    if (!String(script || "").trim()) missing.push(["文案", "script"]);
    if (!(current.source_case || current.highlights?.length || current.native_reference?.entries?.length)) missing.push(["拆解", "script"]);
    if (current.content_origin === "overseas_creator" || current.source_case) return missing;
    if (!current.test_direction?.direction_id) missing.push(["方向", "current"]);
    const video = String(current.metrics?.video_id || "");
    if (!current.publish?.published_on && !(video && published?.has(video))) missing.push(["发布", "data"]);
    return missing;
  }
  const taskActive = task => task.busy || task.stopping || ["queued","running"].includes(task.status);
  const empty = (text, action = "") => `<div class="home-empty"><p>${esc(text)}</p>${action}</div>`;
  const chip = (text, tone = "") => `<span class="home-chip${tone ? ` is-${tone}` : ""}">${esc(text)}</span>`;
  const more = (href, label) => `<a class="home-link" href="${esc(url(href))}">${esc(label)}${icon("arrow")}</a>`;
  const loading = '<div class="home-empty"><p role="status">正在读取…</p></div>';
  const planActions = { scale: ["加码", "good"], execution: ["换执行", "info"], stop: ["停测", "warn"] };
  const kindNames = { task: "协作候选", script: "脚本", video: "视频制作单", carousel: "图文制作单" };
  const decisionOf = plan => plan.experiment?.decision || plan.action;
  // Only scale-up or new-execution plans that nobody has started writing yet are still "to write".
  const needsWriting = plan => ["scale","execution"].includes(decisionOf(plan)) && !plan.task_id;

  function contentRow(item) {
    const typed = /｜(?:候选稿|复盘结果|审稿结果|脚本|视频(?:方案.*)?|图文(?:方案.*)?)$/.test(item.title || '');
    const redundantStage = {待制作:'开始制作',待选稿:'查看并选择候选',待人工审查:'核对剪辑结果'}[item.stage_label] === item.next_label;
    const detail = [typed ? '' : kindNames[item.kind], item.direction_id, item.timeline_name, item.task_started_label, redundantStage ? '' : item.stage_label].filter(Boolean).join(" · ");
    return `<a class="home-row home-flow-row" href="${esc(url(item.next_hash || "#content", item.product))}"><span class="home-row-main"><strong title="${esc(item.title)}">${esc(item.title)}</strong><small>${esc(detail)}</small></span><span class="home-next">${esc(item.next_label || "继续处理")}${icon("arrow")}</span></a>`;
  }
  // Posted (video or 图文) but not yet on the publish calendar: the row opens the order's publish step, where the mark is set.
  function calendarRow(item) {
    const detail = [kindNames[item.kind], item.direction_id, item.timeline_name, item.published_on ? `发布于 ${item.published_on}` : ""].filter(Boolean).join(" · ");
    return `<a class="home-row home-flow-row" href="${esc(url(item.next_hash || "#content", item.product))}"><span class="home-row-main"><strong title="${esc(item.title)}">${esc(item.title)}</strong><small>${esc(detail)}</small></span><span class="home-next">${chip("未上传", "warn")}去标记${icon("arrow")}</span></a>`;
  }
  function planRow(plan) {
    const decision = decisionOf(plan);
    const [label, tone] = planActions[decision] || ['历史安排', ""];
    const write = ["scale","execution"].includes(decision) && !plan.task_id ? `<button type="button" class="secondary compact" data-home-plan-write="${esc(plan.id)}">${icon("edit")}按此计划写稿</button>` : "";
    const linked = plan.task_id ? `<a class="home-link" href="${esc(url(`#ai/${encodeURIComponent(plan.task_id)}`))}">查看关联写稿${icon("arrow")}</a>` : "";
    const point = pointTitles.get(`${plan.product}:${plan.direction_id}`) || "";
    return `<article class="home-plan"><div class="home-row-main"><a class="home-plan-title" href="${esc(url(`#directions/${encodeURIComponent(plan.direction_id)}`,plan.product))}"><strong>${esc(plan.direction_id)}</strong><span class="home-plan-point" data-plan-point="${esc(`${plan.product}:${plan.direction_id}`)}">${point ? ` · ${esc(point)}` : ""}</span></a>${chip(label, tone)}<small class="${plan.note || plan.experiment ? "" : "is-placeholder"}">${esc(plan.experiment ? `测试：${plan.experiment.hypothesis}\n改动：${plan.experiment.change}` : plan.note || "未填写执行备注")}</small>${linked}</div><div class="home-plan-actions">${write}<button type="button" class="subtle" data-home-plan="${esc(plan.id)}" data-plan-status="done">${icon("check")}标记完成</button><details class="home-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-home-plan="${esc(plan.id)}" data-plan-status="cancelled">取消计划</button></div></details></div></article>`;
  }
  function caseRow({item, missing}) {
    const title = typeof shortTitle === "function" ? shortTitle(item) : item.title || item.case_id;
    return `<div class="home-row home-case"><span class="home-row-main"><a href="${esc(url(`#case/${item.case_id}/${missing[0][1]}`))}"><strong>${esc(title)}</strong></a><span class="home-missing"><small>待补</small>${missing.map(([label, tab]) => `<a class="home-chip is-warn" href="${esc(url(`#case/${item.case_id}/${tab}`))}">${esc(label)}</a>`).join("")}</span></span></div>`;
  }

  // The selling point's short title beside each plan's direction code; loaded after the page shows, never blocking it.
  async function labelPlans(root, plans, product) {
    if (!plans.length || window.PublicTextMode) return;
    if (!pointTitles.has(`loaded:${product}`)) {
      pointTitles.set(`loaded:${product}`, true);
      try {
        const catalog = await api(`/api/direction-catalog?product=${encodeURIComponent(product)}`);
        const points = new Map((catalog.selling_points || []).map(point => [point.id, point]));
        for (const direction of catalog.directions || []) {
          const point = points.get(direction.selling_point_id);
          if (!point?.title) continue;
          const code = String(direction.direction_id).endsWith(point.code) ? "" : `${point.code} `;
          pointTitles.set(`${direction.product}:${direction.direction_id}`, code + point.title);
        }
      } catch { pointTitles.delete(`loaded:${product}`); return; }
    }
    root.querySelectorAll("[data-plan-point]").forEach(node => {
      const title = pointTitles.get(node.dataset.planPoint);
      if (title) node.textContent = ` · ${title}`;
    });
  }

  function bindRetry(root) {
    root.querySelectorAll("[data-home-retry]").forEach(button => button.onclick = () => {
      if (!state.signature) void refresh(true);
      void route();
    });
  }

  function render(data, results, product, week) {
    const root = $("#home-page"); if (!root) return;
    renderedProduct = product;
    const [videosData, tasksData, creation, followups, flow] = data;
    const libraryReady = Boolean(state.signature), libraryFailed = !libraryReady && Boolean(state.libraryReadError);
    const settled = results.every(result => result.status !== "pending") && (libraryReady || libraryFailed);
    const pendingRead = index => results[index].status === "pending";
    const failed = results.map((result,index) => result.status === "rejected" && !data[index] ? ["视频列表","协作任务","待写计划","复盘记录","待处理内容"][index] : null).filter(Boolean);
    // Newly published posts belong to observation immediately, including those whose report window ends next week.
    const videos = videosData?.videos || [], due = videos.filter(matches);
    const followupReady = !!videosData && !!followups && !!creation;
    const scripts = creation?.scripts || [];
    const ready = followupReady ? window.ReviewFollowups?.ready?.(due,followups.entries || [],week,scripts) : null;
    const published = new Set([...(videosData?.published_video_ids || videos.map(video => video.video_id)).map(String), ...scripts.filter(script => script.video_id && script.published_on).map(script => String(script.video_id))]);
    const pending = state.cases.filter(item => !item.archived && matches(item)).map(item => ({item,missing:missingCase(item, videosData ? published : null)})).filter(row => row.missing.length).sort((a,b) => b.missing.length-a.missing.length);
    // Whether a video is published comes from the video list, so the case count waits for it (or for its failure).
    const casesKnown = libraryReady && (!!videosData || !pendingRead(0));
    const caseProducts = new Map(state.cases.map(item => [item.case_id, item.product]));
    const running = (tasksData?.tasks || []).filter(task => matches({ product: task.product || caseProducts.get(task.case_id) })).filter(taskActive).length;
    const plans = (creation?.plans || []).filter(plan => matches(plan) && ["pending","working"].includes(plan.status));
    const toWrite = plans.filter(needsWriting), otherPlans = plans.filter(plan => !needsWriting(plan));
    const attention = flow?.counts?.attention ?? 0, toPublish = flow?.counts?.publish ?? 0, flowItems = flow?.items || [];
    const calendarTotal = flow?.calendar_pending?.total ?? 0, calendarItems = flow?.calendar_pending?.items || [];
    const calendarBlock = calendarTotal ? `<div class="home-calendar"><p class="home-overflow">${calendarTotal} 条已发布、还没上传到发布日历</p><div class="home-list">${calendarItems.map(calendarRow).join("")}</div>${calendarTotal > calendarItems.length ? `<p class="home-overflow">另有 ${calendarTotal-calendarItems.length} 条，${more("#content?view=all", "去内容进程查看")}</p>` : ""}</div>` : "";
    const startWriting = `<button type="button" class="secondary compact" data-home-start-writing>${icon("plus")}开始写稿</button>`;
    const runningLine = running ? `<a class="home-running" href="${esc(url("#ai"))}"><span class="home-chip is-good is-live"><i></i>运行中</span><span>${running} 个协作任务正在生成，生成完会出现在这里</span>${icon("arrow")}</a>` : "";

    const tabs = [
      { key: "tasks", id: "home-tasks-title", label: "待我处理", count: flow ? attention : null, work: attention + calendarTotal, hint: "等你做下一步的内容，和内容创作里的「待我处理」一致。", link: more("#content", "内容进程"),
        body: !flow ? (pendingRead(4) ? loading : empty("待处理内容暂时无法读取。")) : `${runningLine}${flowItems.length ? `<div class="home-list">${flowItems.map(contentRow).join("")}</div>` : calendarTotal ? "" : empty("现在没有等你处理的内容。", runningLine ? "" : startWriting)}${attention > flowItems.length ? `<p class="home-overflow">另有 ${attention-flowItems.length} 条，${more("#content", "去内容进程查看")}</p>` : ""}${toPublish ? `<p class="home-overflow home-publish">${toPublish} 条已做完、等你发布，${more("#content?view=publish", "去登记发布")}</p>` : ""}${calendarBlock}` },
      { key: "due", id: "home-due-title", label: "待复盘", count: ready ? ready.length : null, hint: "数字是全部可复盘视频。优先核对已备好的结论、到期复查和异常变化。发布未满14天由系统自动观察，缺数据先补齐。", link: `<div class="home-experience-links">${!window.PublicTextMode && !window.WorkbenchGuest && !window.GuestMode ? '<button type="button" class="subtle compact" data-home-experiences>复盘经验</button>' : ''}${more("#videos?tab=list", "视频明细")}</div>`,
        body: !followupReady ? ([0,2,3].some(pendingRead) ? loading : empty("复盘队列暂时无法读取，请重新读取。")) : '<div id="home-followups"></div>' },
      { key: "plans", id: "home-plans-title", label: "下一轮待写", count: creation ? toWrite.length : null, hint: "在方向详情定了加码或换执行、还没开写的安排。", link: more("#directions", "卖点与方向"),
        body: !creation ? (pendingRead(2) ? loading : empty("待写计划暂时无法读取。")) : plans.length ? `${toWrite.length ? `<div class="home-plan-list">${toWrite.map(planRow).join("")}</div>` : empty("没有要开写的安排。")}${otherPlans.length ? `<details class="home-plan-rest"${toWrite.length ? "" : " open"}><summary>其余安排 ${otherPlans.length} 条：停测、已在写或历史安排</summary><div class="home-plan-list">${otherPlans.map(planRow).join("")}</div></details>` : ""}`
          : empty("还没有待写计划。在方向详情选择加码、换执行或停测，记下下一轮安排。", `<a class="secondary compact" href="${esc(url("#directions"))}">${icon("layers")}去卖点与方向</a>`) },
      { key: "cases", id: "home-cases-title", label: "待补案例", count: casesKnown ? pending.length : null, hint: "缺文案或拆解的案例；自己做的视频还会检查方向和发布。点缺的项直接去补。", link: more("#library", "案例库"),
        body: libraryFailed ? empty("案例资料暂时无法读取。", '<button type="button" class="subtle" data-home-retry>重新读取</button>') : !casesKnown ? loading : pending.length ? `<div class="home-case-list">${pending.slice(0,8).map(caseRow).join("")}</div>${pending.length > 8 ? `<p class="home-overflow">另有 ${pending.length-8} 个，${more("#library", "进入案例库查看")}</p>` : ""}` : empty("当前产品的案例资料已齐备。") },
    ];
    // Open the tab chosen earlier in this session; otherwise, once everything is read, the first one with work waiting.
    const active = tabs.find(tab => tab.key === chosenTab) || (settled ? tabs.find(tab => tab.work ?? tab.count) : null) || tabs[0];
    root.innerHTML = `<div class="page-heading home-heading"><div><span class="eyebrow">OVERVIEW</span><h1>工作概览</h1><p><span class="home-product">${esc(product || "全部产品")}</span></p></div><div class="home-heading-actions"><a class="secondary compact" href="${esc(url("#videos?tab=board"))}">${icon("chart")}完整视频看板</a></div></div>`
      + (failed.length ? `<div class="data-warning">${esc(failed.join("、"))}暂时无法读取。<button type="button" class="subtle" data-home-retry>重新读取</button></div>` : "")
      + `<section class="section-card home-section home-work" aria-label="工作待办"><div class="home-tabs" role="tablist" aria-label="工作待办">${tabs.map(tab => `<button type="button" role="tab" id="${tab.id}" aria-controls="${tab.id}-panel" aria-selected="${tab === active}" tabindex="${tab === active ? 0 : -1}" data-home-tab="${tab.key}">${esc(tab.label)}<span class="home-tab-count">${tab.count ?? "—"}</span></button>`).join("")}</div>`
      + tabs.map(tab => `<div class="home-panel" role="tabpanel" tabindex="0" id="${tab.id}-panel" aria-labelledby="${tab.id}"${tab === active ? "" : " hidden"}>${tab.key === 'due' ? `${tab.body}<details class="home-review-more"><summary>经验与更多</summary><div class="home-panel-head">${tab.link}</div><p>${esc(tab.hint)}</p></details>` : `<div class="home-panel-head"><details class="home-explanation"><summary>说明</summary><p>${esc(tab.hint)}</p></details>${tab.link}</div>${tab.body}`}</div>`).join("") + `</section>`;

    if (followupReady) window.ReviewFollowups?.mount(root.querySelector('#home-followups'), {videos:due,entries:followups.entries || [],revision:followups.revision,week,scripts,tasks:tasksData?.tasks || [],url,refresh:async () => {
      if (window.WorkbenchShell?.parse().page === 'home' && selectedProduct() === product) await route();
    }});
    const select = (key, focus = false) => {
      chosenTab = key;
      root.querySelectorAll("[data-home-tab]").forEach(tab => { const on = tab.dataset.homeTab === key; tab.setAttribute("aria-selected", on); tab.tabIndex = on ? 0 : -1; if (on && focus) tab.focus(); });
      root.querySelectorAll(".home-panel").forEach(panel => panel.hidden = panel.getAttribute("aria-labelledby") !== tabs.find(tab => tab.key === key).id);
    };
    root.querySelectorAll("[data-home-tab]").forEach(tab => {
      tab.onclick = () => select(tab.dataset.homeTab);
      tab.onkeydown = event => {
        const step = {ArrowRight:1,ArrowLeft:-1}[event.key];
        if (!step && !["Home","End"].includes(event.key)) return;
        event.preventDefault();
        const index = tabs.findIndex(item => item.key === tab.dataset.homeTab);
        const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + step + tabs.length) % tabs.length;
        select(tabs[next].key, true);
      };
    });
    root.querySelector("[data-home-start-writing]")?.addEventListener("click", () => window.CaseAI?.startBlank());
    root.querySelector("[data-home-experiences]")?.addEventListener("click", () => window.ReviewExperiences?.open({product}));
    root.querySelectorAll("[data-home-plan-write]").forEach(button => button.onclick = async () => {
      const plan = plans.find(item => item.id === button.dataset.homePlanWrite);
      if (!plan || plan.action === "stop") return;
      button.disabled = true;
      try {
        const data = await api(`/api/direction-catalog/${encodeURIComponent(plan.direction_id)}?product=${encodeURIComponent(plan.product)}`);
        if (!button.isConnected) return;
        await window.CaseAI?.startFromDirection({...data.direction,plan_id:plan.id,plan_note:plan.note});
      } catch (error) { toast(error.message || "暂时无法按此计划写稿，请稍后重试。",true); }
      finally { button.disabled = false; }
    });
    root.querySelectorAll("[data-home-plan]").forEach(button => button.onclick = async () => {
      const plan = plans.find(item => item.id === button.dataset.homePlan), status = button.dataset.planStatus;
      button.disabled = true;
      try {
        await api("/api/plans", {method:"POST",body:{revision:creation.revision,request_id:crypto.randomUUID(),plan:{...plan,status}}});
        if (window.WorkbenchShell?.parse().page === "home" && selectedProduct() === product) await route();
        offerUndo(plan, status);
      } catch (error) { toast(error.status === 409 ? "计划已在其他页面更新，请重新读取工作概览后再操作。" : error.message,true); }
      finally { button.disabled = false; }
    });
    root.querySelectorAll(".home-menu").forEach(menu => menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      const close = event => { if (!menu.contains(event.target) || event.key === "Escape") { menu.open = false; document.removeEventListener("click", close, true); document.removeEventListener("keydown", close, true); } };
      document.addEventListener("click", close, true); document.addEventListener("keydown", close, true);
    }));
    bindRetry(root);
    void labelPlans(root, plans, product);
  }

  // The change is already saved; undo saves the plan's earlier status back.
  function offerUndo(plan, status) {
    let bar = $("#home-undo");
    if (!bar) { bar = document.createElement("div"); bar.id = "home-undo"; bar.setAttribute("role", "status"); document.body.append(bar); }
    bar.innerHTML = `<span>${esc(plan.direction_id)} · ${status === "done" ? "已标记完成" : "已取消计划"}</span><button type="button">撤销</button>`;
    bar.hidden = false;
    clearTimeout(undoTimer);
    undoTimer = setTimeout(() => bar.hidden = true, 8000);
    bar.querySelector("button").onclick = async event => {
      event.currentTarget.disabled = true;
      try {
        const creation = await api(`/api/creation?product=${encodeURIComponent(plan.product)}`);
        await api("/api/plans", {method:"POST",body:{revision:creation.revision,request_id:crypto.randomUUID(),plan}});
        bar.hidden = true;
        if (window.WorkbenchShell?.parse().page === "home") await route();
      } catch (error) { toast(error.status === 409 ? "计划已在其他页面更新，无法撤销，请到方向详情调整。" : error.message,true); bar.hidden = true; }
    };
  }

  // Each read fills its own tab as soon as it arrives; one slow list no longer holds back the others.
  async function route() {
    const seq = ++sequence, product = selectedProduct(), week = currentWeek();
    const saved = window.WorkbenchShell?.listState?.("home", product);
    if (["due", "tasks", "plans", "cases"].includes(saved?.tab)) chosenTab = saved.tab;
    else if (!$("[data-home-tab]")) chosenTab = null;
    $("#breadcrumb").innerHTML = "工作概览";
    if (!$("#home-page")) $("#content").innerHTML = '<div id="home-page"></div>';
    const isCurrent = () => seq === sequence && selectedProduct() === product && window.WorkbenchShell?.parse().page === "home";
    const previous = cache.get(product), data = previous ? [...previous] : Array(5).fill(null);
    const results = Array.from({length:5}, () => ({status:"pending"}));
    const paint = () => { if (isCurrent()) render(data, results, product, week); };
    // The library starts in parallel with these independent lists. Reuse this
    // view when it arrives, without another network read or a route reset.
    repaintLibrary = paint;
    // A first visit shows the frame at once; a refresh keeps the numbers already on screen until new ones arrive.
    if (!previous || renderedProduct !== product || !$("[data-home-tab]")) paint();
    const read = (index,path) => api(path).then(value => { results[index] = {status:"fulfilled",value}; data[index] = value; }, reason => { results[index] = {status:"rejected",reason}; data[index] = null; })
      .then(() => { if (isCurrent()) { cache.set(product, [...data]); paint(); } });
    await Promise.all([read(0,`/api/performance/videos?${new URLSearchParams({projection:"home",...(product ? {product} : {})})}`),read(1,"/api/ai/tasks"),read(2,`/api/creation?product=${encodeURIComponent(product)}`),read(3,`/api/review-followups?product=${encodeURIComponent(product)}`),
      read(4,`/api/content?${new URLSearchParams({view:"attention",page_size:"8",...(product ? {product} : {})})}`)]);
  }
  const captureListState = () => {
    const active = document.querySelector('[data-home-tab][aria-selected="true"]');
    return active ? { product: renderedProduct, tab: active.dataset.homeTab } : null;
  };
  return { route, currentWeek, dueVideos, missingCase, captureListState, libraryChanged: () => repaintLibrary?.() };
})();
