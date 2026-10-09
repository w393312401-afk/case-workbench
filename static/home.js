"use strict";

// The calendar week is independent of the last week for which exports exist.
window.WorkbenchHome = (() => {
  let sequence = 0;
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
  const empty = text => `<p class="home-empty">${esc(text)}</p>`;
  function section(id, title, action, body, subtitle = "") {
    return `<section class="section-card home-section" aria-labelledby="${id}"><div class="home-section-heading"><div><h2 id="${id}">${title}</h2>${subtitle ? `<p>${subtitle}</p>` : ""}</div>${action}</div>${body}</section>`;
  }
  function taskRows(tasks) {
    const scoped = tasks.filter(task => matches({ product: task.product || state.cases.find(item => item.case_id === task.case_id)?.product }));
    const active = scoped.filter(taskActive), recent = scoped.filter(task => !taskActive(task) && task.status === "completed").slice(0,3);
    const row = task => `<a class="home-work-row" href="${esc(url(`#ai/${encodeURIComponent(task.task_id)}`))}"><span><strong>${esc(task.case_title || task.case_id || "写稿任务")}</strong><small>${esc({variants:"生成变体",review:"一起审稿",retrospective:"一起复盘"}[task.action] || "AI 协作")}</small></span><span class="home-status ${taskActive(task) ? "is-running" : ""}">${task.stopping ? "正在停止" : {running:"正在处理",queued:"排队中",completed:"查看候选"}[task.status] || task.status}</span></a>`;
    return { count: active.length, markup: active.length ? active.map(row).join("") : empty("当前没有运行中的写稿任务。"), recent: recent.length ? `<details class="home-recent-tasks"><summary>最近完成 · 等待检查</summary>${recent.map(row).join("")}</details>` : "" };
  }
  function render(data, results, product, week) {
    const root = $("#home-page"); if (!root) return;
    const [summary, analytics, videosData, tasksData, creation] = data;
    const failed = results.map((result,index) => result.status === "rejected" ? ["周数据概况","周图","视频列表","写稿任务","待写计划"][index] : null).filter(Boolean);
    const videos = videosData?.videos || [], due = dueVideos(videos,week);
    const pending = state.cases.filter(item => !item.archived && matches(item)).map(item => ({item,missing:missingCase(item)})).filter(row => row.missing.length).sort((a,b) => b.missing.length-a.missing.length);
    const tasks = taskRows(tasksData?.tasks || []), last = summary?.coverage?.last;
    const plans = (creation?.plans || []).filter(plan => matches(plan) && ["pending","working"].includes(plan.status));
    const planMarkup = creation ? plans.length ? `<div class="home-plan-list">${plans.map(plan => `<article class="home-plan"><div><a href="${esc(url(`#directions/${encodeURIComponent(plan.direction_id)}`,plan.product))}"><strong>${esc(plan.direction_id)}</strong> · ${{scale:"加码",execution:"换执行",stop:"停测"}[plan.action] || esc(plan.action)}</a><small>${esc(plan.note || "未填写执行备注")}</small>${plan.task_id ? `<a class="subtle" href="${esc(url(`#ai/${encodeURIComponent(plan.task_id)}`))}">查看关联写稿</a>` : ""}</div><div class="home-plan-actions">${plan.action !== "stop" && !plan.task_id ? `<button type="button" class="secondary compact" data-home-plan-write="${esc(plan.id)}">按此计划写稿</button>` : ""}<button type="button" class="subtle" data-home-plan="${esc(plan.id)}" data-plan-status="done">标记完成</button><button type="button" class="subtle" data-home-plan="${esc(plan.id)}" data-plan-status="cancelled">取消计划</button></div></article>`).join("")}</div>` : empty("还没有待写计划。在方向详情选择加码、换执行或停测，记下下一轮安排。") : empty("待写计划暂时无法读取。");
    const charts = summary && analytics ? window.PerfDashboard?.homeMarkup({ summary, analytics, videos, filters:{ account:"",product,period:"8" } }) || empty("周图模块暂时不可用，请刷新页面。") : empty("周图暂时无法读取，请重新读取本周概览。");
    root.innerHTML = `<div class="page-heading home-heading"><div><span class="eyebrow">THIS WEEK</span><h1>本周</h1><p>${week.start} — ${week.end}<span class="home-product">${esc(product || "全部产品")}</span></p></div><a class="secondary" href="${esc(url("#directions"))}">${icon("layers")}查看测试方向</a></div>${failed.length ? `<div class="data-warning">${esc(failed.join("、"))}暂时无法读取。<button type="button" class="subtle" data-home-retry>重新读取</button></div>` : ""}<div class="home-summary-strip"><div><strong>${tasksData ? tasks.count : "—"}</strong><span>任务运行中</span></div><div><strong>${videosData ? due.length : "—"}</strong><span>本周观察窗到期</span></div><div><strong>${pending.length}</strong><span>案例待补充</span></div><button type="button" class="subtle" data-material-open>${icon("folder")}打开素材篮</button></div>${section("home-weekly-title", "最近 8 周", `<a class="subtle" href="${esc(url("#videos?tab=board"))}">完整视频看板 ${icon("arrow")}</a>`, charts, `后台数据截至 ${esc(last || "尚未导入")}；以下为每周视频明细口径，数据缺口保留为空。`)}<div class="home-work-grid">${section("home-due-title", "本周观察窗到期", `<a class="subtle" href="${esc(url("#videos?tab=list"))}">视频明细</a>`, videosData ? due.length ? due.slice(0,12).map(video => `<a class="home-work-row" href="${esc(url(`#videos?tab=list&video=${encodeURIComponent(video.video_id)}`))}"><span><strong>${esc(video.headline || video.title || video.video_id)}</strong><small>${esc(video.window_end)} 到期 · ${esc(video.account || "账号未记录")}</small></span><span class="home-status">${video.window_end > week.today ? "本周稍后到期" : video.status === "incomplete" || video.status === "pending" ? "待补数据" : esc(video.status_label || "可查看表现")}</span></a>`).join("") + (due.length>12 ? `<p class="home-empty">另有 ${due.length-12} 条，进入视频明细查看。</p>` : "") : empty("本周没有到期的视频观察窗。") : empty("视频列表暂时不可用。"), "观察窗为发布当周 + 下一周，按自然周计算。")}${section("home-tasks-title", "写稿任务", `<a class="subtle" href="${esc(url("#ai"))}">全部写稿</a>`, tasksData ? tasks.markup+tasks.recent : empty("任务状态暂时不可用。"))}</div>${section("home-plans-title", "下一轮待写", `<a class="subtle" href="${esc(url("#directions"))}">卖点与方向</a>`, planMarkup)}${section("home-cases-title", "待补充的案例", `<a class="subtle" href="${esc(url("#library"))}">案例库</a>`, pending.length ? `<div class="home-case-list">${pending.slice(0,8).map(({item,missing}) => `<a class="home-work-row" href="${esc(url(`#case/${item.case_id}/${missing[0][1]}`))}"><span><strong>${esc(typeof shortTitle === "function" ? shortTitle(item) : item.title || item.case_id)}</strong><small>待补：${missing.map(([label])=>label).join("、")}</small></span>${icon("arrow")}</a>`).join("")}</div>` : empty("当前产品的案例资料已齐备。"), "只列出缺少文案、拆解、方向或发布记录的未归档案例。")}`;
    window.PerfDashboard?.bind(root);
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
      const plan = plans.find(item => item.id === button.dataset.homePlan);
      button.disabled = true;
      try {
        await api("/api/plans", {method:"POST",body:{revision:creation.revision,request_id:crypto.randomUUID(),plan:{...plan,status:button.dataset.planStatus}}});
        if (window.WorkbenchShell?.parse().page === "home" && selectedProduct() === product) await route();
      } catch (error) { toast(error.status === 409 ? "计划已在其他页面更新，请重新读取本周后再操作。" : error.message,true); }
      finally { button.disabled = false; }
    });
    root.querySelector("[data-home-retry]")?.addEventListener("click", () => void route());
  }
  async function route() {
    const seq = ++sequence, product = selectedProduct(), week = currentWeek();
    $("#breadcrumb").innerHTML = "工作空间 <span>/</span> 本周";
    $("#content").innerHTML = '<div id="home-page"><div class="page-heading"><div><span class="eyebrow">THIS WEEK</span><h1>本周</h1><p role="status">正在汇总本周工作…</p></div></div></div>';
    const results = await Promise.allSettled([api("/api/performance/summary"),api("/api/performance/analytics"),api("/api/performance/videos"),api("/api/ai/tasks"),api(`/api/creation?product=${encodeURIComponent(product)}`)]);
    if (seq !== sequence || selectedProduct() !== product || window.WorkbenchShell?.parse().page !== "home") return;
    render(results.map(result => result.status === "fulfilled" ? result.value : null),results,product,week);
  }
  return { route, currentWeek, dueVideos, missingCase };
})();
