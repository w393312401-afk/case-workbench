"use strict";

// The calendar week is independent of the last week for which exports exist.
// Layout: four counters, then one tabbed list of this week's work, then the eight-week charts.
window.WorkbenchHome = (() => {
  let sequence = 0, chosenTab = null, undoTimer = null, renderedProduct = null;
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
    return videos.filter(item => matches(item) && item.window_end >= week.start && item.window_end <= week.end)
      .sort((a,b) => String(a.window_end).localeCompare(String(b.window_end)) || String(a.video_id).localeCompare(String(b.video_id)));
  }
  function missingCase(item) {
    const current = item.current || {}, missing = [];
    const script = typeof transcriptDisplay === "function" ? transcriptDisplay(item).text : current.transcript?.text || current.script?.text || "";
    if (!String(script || "").trim()) missing.push(["文案", "script"]);
    if (!(current.source_case || current.highlights?.length || current.native_reference?.entries?.length)) missing.push(["拆解", "script"]);
    if (!current.test_direction?.direction_id) missing.push(["方向", "current"]);
    if (!current.publish?.published_on) missing.push(["发布", "data"]);
    return missing;
  }
  const taskActive = task => task.busy || task.stopping || ["queued","running"].includes(task.status);
  const empty = (text, action = "") => `<div class="home-empty"><p>${esc(text)}</p>${action}</div>`;
  const chip = (text, tone = "") => `<span class="home-chip${tone ? ` is-${tone}` : ""}">${esc(text)}</span>`;
  const more = (href, label) => `<a class="home-link" href="${esc(url(href))}">${esc(label)}${icon("arrow")}</a>`;
  const planActions = { scale: ["加码", "good"], execution: ["换执行", "info"], stop: ["停测", "warn"] };

  function videoStatus(video, week) {
    if (video.window_end > week.today) {
      const days = Math.round((new Date(`${video.window_end}T00:00:00`) - new Date(`${week.today}T00:00:00`)) / 864e5);
      return chip(`还剩 ${days} 天`);
    }
    if (video.window_end === week.today && ["incomplete","pending"].includes(video.status)) return chip("今天到期", "warn");
    if (["incomplete","pending"].includes(video.status)) return chip("待补数据", "warn");
    return chip(video.status_label || "可查看表现", "good");
  }
  function taskRow(task) {
    const active = taskActive(task);
    const status = task.stopping ? chip("正在停止", "warn") : task.status === "running" ? `<span class="home-chip is-good is-live"><i></i>正在处理</span>`
      : task.status === "queued" ? chip("排队中") : task.status === "completed" ? chip("查看候选", "good") : chip(task.status);
    return `<a class="home-row${active ? " is-active" : ""}" href="${esc(url(`#ai/${encodeURIComponent(task.task_id)}`))}"><span class="home-row-main"><strong>${esc(task.case_title || task.case_id || "写稿任务")}</strong><small>${esc({variants:"生成变体",review:"一起审稿",retrospective:"一起复盘"}[task.action] || "AI 协作")}</small></span>${status}</a>`;
  }
  function planRow(plan) {
    const [label, tone] = planActions[plan.action] || [plan.action, ""];
    const write = plan.action !== "stop" && !plan.task_id ? `<button type="button" class="secondary compact" data-home-plan-write="${esc(plan.id)}">${icon("edit")}按此计划写稿</button>` : "";
    const linked = plan.task_id ? `<a class="home-link" href="${esc(url(`#ai/${encodeURIComponent(plan.task_id)}`))}">查看关联写稿${icon("arrow")}</a>` : "";
    return `<article class="home-plan"><div class="home-row-main"><a class="home-plan-title" href="${esc(url(`#directions/${encodeURIComponent(plan.direction_id)}`,plan.product))}"><strong>${esc(plan.direction_id)}</strong></a>${chip(label, tone)}<small class="${plan.note ? "" : "is-placeholder"}">${esc(plan.note || "未填写执行备注")}</small>${linked}</div><div class="home-plan-actions">${write}<button type="button" class="subtle" data-home-plan="${esc(plan.id)}" data-plan-status="done">${icon("check")}标记完成</button><details class="home-menu"><summary class="icon-button" aria-label="更多操作"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="19" cy="12" r="1.3"/></svg></summary><div role="menu"><button type="button" role="menuitem" data-home-plan="${esc(plan.id)}" data-plan-status="cancelled">取消计划</button></div></details></div></article>`;
  }
  function caseRow({item, missing}) {
    const title = typeof shortTitle === "function" ? shortTitle(item) : item.title || item.case_id;
    return `<div class="home-row home-case"><span class="home-row-main"><a href="${esc(url(`#case/${item.case_id}/${missing[0][1]}`))}"><strong>${esc(title)}</strong></a><span class="home-missing"><small>待补</small>${missing.map(([label, tab]) => `<a class="home-chip is-warn" href="${esc(url(`#case/${item.case_id}/${tab}`))}">${esc(label)}</a>`).join("")}</span></span></div>`;
  }

  function weeklyMarkup(summary, analytics, videos, product, pending = false) {
    const charts = pending ? '<p class="home-empty" role="status">正在读取周图…</p>'
      : summary && analytics ? window.PerfDashboard?.homeMarkup({ summary, analytics, videos, filters:{ account:"",product,period:"8" } }) || empty("周图模块暂时不可用，请刷新页面。")
      : empty("周图暂时无法读取。", `<button type="button" class="secondary compact" data-home-retry>${icon("refresh")}重新读取</button>`);
    const noWeekly = summary && !summary.weeks?.length;
    return `<div class="home-section-heading"><div><h2 id="home-weekly-title">最近 8 周</h2><p>后台数据截至 ${esc(summary?.coverage?.last || "尚未导入")}；以下为每周视频明细口径，数据缺口保留为空。</p></div>${more("#videos?tab=board", "完整视频看板")}</div>${noWeekly ? empty("尚无可用周数据，请先在视频数据中导入后台报表。", `<a class="secondary compact" href="${esc(url("#videos"))}">${icon("chart")}去导入后台报表</a>`) : charts}`;
  }

  function bindRetry(root) {
    root.querySelectorAll("[data-home-retry]").forEach(button => button.onclick = () => void route());
  }

  function render(data, results, product, week) {
    const root = $("#home-page"); if (!root) return;
    renderedProduct = product;
    const [summary, analytics, videosData, tasksData, creation] = data;
    const failed = results.map((result,index) => result.status === "rejected" ? ["周数据概况","周图","视频列表","写稿任务","待写计划"][index] : null).filter(Boolean);
    const videos = videosData?.videos || [], due = dueVideos(videos,week);
    const pending = state.cases.filter(item => !item.archived && matches(item)).map(item => ({item,missing:missingCase(item)})).filter(row => row.missing.length).sort((a,b) => b.missing.length-a.missing.length);
    const caseProducts = new Map(state.cases.map(item => [item.case_id, item.product]));
    const scopedTasks = (tasksData?.tasks || []).filter(task => matches({ product: task.product || caseProducts.get(task.case_id) }));
    const activeTasks = scopedTasks.filter(taskActive), recentTasks = scopedTasks.filter(task => !taskActive(task) && task.status === "completed").slice(0,3);
    const plans = (creation?.plans || []).filter(plan => matches(plan) && ["pending","working"].includes(plan.status));

    const tabs = [
      { key: "due", id: "home-due-title", label: "观察窗到期", count: videosData ? due.length : null, hint: "观察窗为发布当周 + 下一周，按自然周计算。", link: more("#videos?tab=list", "视频明细"),
        body: !videosData ? empty("视频列表暂时不可用。") : due.length ? `<div class="home-list">${due.slice(0,12).map(video => `<a class="home-row" href="${esc(url(`#videos?tab=list&video=${encodeURIComponent(video.video_id)}`))}"><span class="home-row-main"><strong>${esc(video.headline || video.title || video.video_id)}</strong><small>${esc(video.window_end)} 到期 · ${esc(video.account || "账号未记录")}</small></span>${videoStatus(video, week)}</a>`).join("")}</div>${due.length > 12 ? `<p class="home-overflow">另有 ${due.length-12} 条，${more("#videos?tab=list", "进入视频明细查看")}</p>` : ""}`
          : empty("本周没有到期的视频观察窗。") },
      { key: "tasks", id: "home-tasks-title", label: "写稿任务", count: tasksData ? activeTasks.length : null, hint: "运行中和排队的写稿任务；最近完成的放在下面等你检查。", link: more("#ai", "全部写稿"),
        body: !tasksData ? empty("任务状态暂时不可用。") : `<div class="home-list">${activeTasks.map(taskRow).join("")}</div>${activeTasks.length ? "" : empty("当前没有运行中的写稿任务。", `<button type="button" class="secondary compact" data-home-start-writing>${icon("plus")}开始写稿</button>`)}${recentTasks.length ? `<details class="home-recent-tasks"${activeTasks.length ? "" : " open"}><summary>最近完成 · 等待检查（${recentTasks.length}）</summary><div class="home-list">${recentTasks.map(taskRow).join("")}</div></details>` : ""}` },
      { key: "plans", id: "home-plans-title", label: "下一轮待写", count: creation ? plans.length : null, hint: "在方向详情选择加码、换执行或停测后记下的安排。", link: more("#directions", "卖点与方向"),
        body: !creation ? empty("待写计划暂时无法读取。") : plans.length ? `<div class="home-plan-list">${plans.map(planRow).join("")}</div>`
          : empty("还没有待写计划。在方向详情选择加码、换执行或停测，记下下一轮安排。", `<a class="secondary compact" href="${esc(url("#directions"))}">${icon("layers")}去卖点与方向</a>`) },
      { key: "cases", id: "home-cases-title", label: "待补案例", count: pending.length, hint: "缺少文案、拆解、方向或发布记录的未归档案例；点缺失项直接去补。", link: more("#library", "案例库"),
        body: pending.length ? `<div class="home-case-list">${pending.slice(0,8).map(caseRow).join("")}</div>${pending.length > 8 ? `<p class="home-overflow">另有 ${pending.length-8} 个，${more("#library", "进入案例库查看")}</p>` : ""}` : empty("当前产品的案例资料已齐备。") },
    ];
    // Open the tab chosen earlier in this session; otherwise the first one with work waiting.
    const active = tabs.find(tab => tab.key === chosenTab) || tabs.find(tab => tab.count) || tabs[0];
    const kpi = tab => `<button type="button" class="home-kpi${tab.count ? " has-work" : ""}${tab === active ? " is-selected" : ""}" data-home-kpi="${tab.key}"><strong>${tab.count ?? "—"}</strong><span>${esc(tab.label)}</span></button>`;

    root.innerHTML = `<div class="page-heading home-heading"><div><span class="eyebrow">THIS WEEK</span><h1>本周</h1><p><span>${week.start} — ${week.end}</span><span class="home-product">${esc(product || "全部产品")}</span></p></div><div class="home-heading-actions"><button type="button" class="subtle" data-material-open>${icon("folder")}素材篮</button><a class="secondary compact" href="${esc(url("#directions"))}">${icon("layers")}查看测试方向</a></div></div>`
      + (failed.length ? `<div class="data-warning">${esc(failed.join("、"))}暂时无法读取。<button type="button" class="subtle" data-home-retry>重新读取</button></div>` : "")
      + `<div class="home-kpis" role="group" aria-label="本周待办数量">${tabs.map(kpi).join("")}</div>`
      + `<section class="section-card home-section home-work" aria-label="本周要处理"><div class="home-tabs" role="tablist">${tabs.map(tab => `<button type="button" role="tab" id="${tab.id}" aria-controls="${tab.id}-panel" aria-selected="${tab === active}" tabindex="${tab === active ? 0 : -1}" data-home-tab="${tab.key}">${esc(tab.label)}<span class="home-tab-count">${tab.count ?? "—"}</span></button>`).join("")}</div>`
      + tabs.map(tab => `<div class="home-panel" role="tabpanel" id="${tab.id}-panel" aria-labelledby="${tab.id}"${tab === active ? "" : " hidden"}><div class="home-panel-head"><div><h2 class="home-panel-title">${esc(tab.label)}<span class="home-tab-count">${tab.count ?? "—"}</span></h2><p>${esc(tab.hint)}</p></div>${tab.link}</div>${tab.body}</div>`).join("") + `</section>`
      + `<section id="home-weekly-section" class="section-card home-section" aria-labelledby="home-weekly-title">${weeklyMarkup(summary,analytics,videos,product,results[1]?.status === "pending")}</section>`;

    if (results[1]?.status !== "pending") window.PerfDashboard?.bind(root.querySelector("#home-weekly-section"));
    const select = (key, focus = false) => {
      chosenTab = key;
      root.querySelectorAll("[data-home-tab]").forEach(tab => { const on = tab.dataset.homeTab === key; tab.setAttribute("aria-selected", on); tab.tabIndex = on ? 0 : -1; if (on && focus) tab.focus(); });
      root.querySelectorAll(".home-panel").forEach(panel => panel.hidden = panel.getAttribute("aria-labelledby") !== tabs.find(tab => tab.key === key).id);
      root.querySelectorAll("[data-home-kpi]").forEach(card => card.classList.toggle("is-selected", card.dataset.homeKpi === key));
    };
    root.querySelectorAll("[data-home-tab]").forEach(tab => {
      tab.onclick = () => select(tab.dataset.homeTab);
      tab.onkeydown = event => {
        const step = {ArrowRight:1,ArrowLeft:-1}[event.key]; if (!step) return;
        event.preventDefault();
        const index = tabs.findIndex(item => item.key === tab.dataset.homeTab);
        select(tabs[(index + step + tabs.length) % tabs.length].key, true);
      };
    });
    root.querySelectorAll("[data-home-kpi]").forEach(card => card.onclick = () => {
      select(card.dataset.homeKpi);
      root.querySelector(".home-work").scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
    });
    root.querySelector("[data-home-start-writing]")?.addEventListener("click", () => window.CaseAI?.startBlank());
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
      } catch (error) { toast(error.status === 409 ? "计划已在其他页面更新，请重新读取本周后再操作。" : error.message,true); }
      finally { button.disabled = false; }
    });
    root.querySelectorAll(".home-menu").forEach(menu => menu.addEventListener("toggle", () => {
      if (!menu.open) return;
      const close = event => { if (!menu.contains(event.target) || event.key === "Escape") { menu.open = false; document.removeEventListener("click", close, true); document.removeEventListener("keydown", close, true); } };
      document.addEventListener("click", close, true); document.addEventListener("keydown", close, true);
    }));
    bindRetry(root);
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

  async function route() {
    const seq = ++sequence, product = selectedProduct(), week = currentWeek();
    const saved = window.WorkbenchShell?.listState?.("home", product);
    if (["due", "tasks", "plans", "cases"].includes(saved?.tab)) chosenTab = saved.tab;
    else if (!$("[data-home-tab]")) chosenTab = null;
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 本周";
    if (!$("#home-page")) $("#content").innerHTML = '<div id="home-page"><div class="page-heading"><div><span class="eyebrow">THIS WEEK</span><h1>本周</h1><p role="status">正在汇总本周工作…</p></div></div></div>';
    const isCurrent = () => seq === sequence && selectedProduct() === product && window.WorkbenchShell?.parse().page === "home";
    const results = Array.from({length:5}, () => ({status:"pending"}));
    const read = (index,path) => api(path).then(value => { results[index] = {status:"fulfilled",value}; }, reason => { results[index] = {status:"rejected",reason}; });
    const charts = read(1,"/api/performance/analytics");
    await Promise.all([read(0,"/api/performance/summary"),read(2,"/api/performance/videos"),read(3,"/api/ai/tasks"),read(4,`/api/creation?product=${encodeURIComponent(product)}`)]);
    if (!isCurrent()) return;
    render(results.map(result => result.status === "fulfilled" ? result.value : null),results,product,week);
    if (results[1].status !== "pending") return;
    await charts;
    if (!isCurrent()) return;
    // Only replace the charts: late analytics must not reset the selected tab or plan buttons.
    const section = $("#home-weekly-section");
    if (!section) return;
    section.innerHTML = weeklyMarkup(results[0].value,results[1].value,results[2].value?.videos || [],product);
    window.PerfDashboard?.bind(section);
    bindRetry(section);
  }
  const captureListState = () => {
    const active = document.querySelector('[data-home-tab][aria-selected="true"]');
    return active ? { product: renderedProduct, tab: active.dataset.homeTab } : null;
  };
  return { route, currentWeek, dueVideos, missingCase, captureListState };
})();
