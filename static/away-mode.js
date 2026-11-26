"use strict";

// 「我去拍了」值守模式：顶栏按钮 → 出门弹窗（选时长、勾这次要做的活）→ 值守中看进度、可停下 →
// 回来后工作概览顶部的「你不在时」卡片。计划和执行都在后端 away_mode.py，这里只发起和展示。
window.AwayMode = (() => {
  const laneNames = {ai: "AI", net: "网络", local: "本机", resolve: "达芬奇"};
  const statusNames = {pending: "排队", running: "进行中", done: "完成", failed: "没做成", skipped: "没开始", cancelled: "已停下"};
  let current = null, plan = null, timer = null, minutes = 90, product = "", loading = false, openJob = null;

  const shell = () => window.WorkbenchShell;
  const link = (hash, owner) => shell()?.url ? shell().url(hash, owner) : hash;
  const clock = iso => { const at = iso ? new Date(iso) : null; return at && !isNaN(at) ? at.toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false}) : ""; };
  const minutesLeft = session => Math.max(0, Math.round((new Date(session.return_at) - Date.now()) / 60000));

  // ---------- data ----------
  async function refresh() {
    try {
      const data = await api("/api/away");
      current = data.current;
    } catch (error) {
      return;
    }
    paintButton();
    paintHome();
    if ($("#away-dialog")?.open && current?.status === "running" && !plan) paintStatus();
    schedule();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, current?.status === "running" ? 15000 : 60000);
  }

  // ---------- topbar ----------
  function button() {
    let node = $("#away-open");
    if (node) return node;
    const anchor = $("#task-activity-open");
    if (!anchor) return null;
    node = document.createElement("button");
    node.type = "button";
    node.id = "away-open";
    node.className = "secondary compact away-trigger";
    node.addEventListener("click", open);
    anchor.before(node);
    return node;
  }

  function paintButton() {
    const node = button();
    if (!node) return;
    for (const target of [node, homeButton()]) {
      if (!target) continue;
      const running = current?.status === "running";
      target.classList.toggle("is-running", running);
      target.innerHTML = running ? `<span class="away-dot" aria-hidden="true"></span>值守中 · 还剩 ${minutesLeft(current)} 分` : "我去拍了";
      target.title = running ? `预计 ${clock(current.return_at)} 回来，点开看进度或停下` : "出门拍素材时，让工作台按计划自己干活";
    }
  }

  // 手机顶栏放不下第六个按钮（会被固定在左上的产品选择盖住），所以手机上顶栏的按钮藏起来，
  // 改在工作概览标题旁放一个同样的按钮（away-mode.css 按屏宽切换，电脑上只用顶栏那个）。
  function homeButton() {
    const actions = $("#home-page .home-heading-actions");
    if (!actions) return null;
    let node = $("#away-home-open");
    if (node && actions.contains(node)) return node;
    node?.remove();
    node = document.createElement("button");
    node.type = "button";
    node.id = "away-home-open";
    node.className = "secondary compact away-trigger away-home-trigger";
    node.addEventListener("click", open);
    actions.prepend(node);
    return node;
  }

  // ---------- dialog ----------
  function dialog() {
    let node = $("#away-dialog");
    if (node) return node;
    node = document.createElement("dialog");
    node.id = "away-dialog";
    node.className = "away-dialog";
    node.setAttribute("aria-labelledby", "away-title");
    node.innerHTML = `<div class="dialog-heading"><div><h2 id="away-title">我去拍了</h2></div><button type="button" class="icon-button" data-away-close aria-label="关闭">${typeof icon === "function" ? icon("close") : "×"}</button></div>
      <div class="away-body" id="away-body"></div><div class="dialog-footer" id="away-footer"></div>`;
    node.addEventListener("click", event => {
      if (event.target === node || event.target.closest("[data-away-close]")) node.close();
    });
    node.addEventListener("close", () => { plan = null; });
    document.body.append(node);
    return node;
  }

  async function open() {
    const node = dialog();
    if (!node.open) node.showModal();
    plan = null;
    if (current?.status === "running") return paintStatus();
    const choices = shell()?.getProductChoices?.() || [];
    const chosen = shell()?.product?.() || "";
    product = chosen && chosen !== "all" ? chosen : (choices[0]?.value || "");
    await loadPlan();
  }

  async function loadPlan() {
    const body = $("#away-body");
    body.innerHTML = '<p class="away-muted">正在看现在有哪些活能做…</p>';
    $("#away-footer").innerHTML = "";
    if (!product) {
      body.innerHTML = '<p class="data-warning">先在顶部选一个产品。</p>';
      return;
    }
    try {
      plan = await api(`/api/away/plan?${new URLSearchParams({product})}`);
    } catch (error) {
      body.innerHTML = `<p class="data-warning">${esc(error.message)}</p>`;
      return;
    }
    if (plan.current?.status === "running") {
      current = plan.current;
      plan = null;
      return paintStatus();
    }
    const saved = remembered();
    minutes = plan.minutes.includes(saved.minutes) ? saved.minutes : plan.default_minutes;
    // what was ticked last time comes back ticked (or unticked); items that can't run now stay as the plan says
    for (const item of plan.items) if (item.available && !item.fixed && typeof saved.kinds?.[item.kind] === "boolean") item.enabled = saved.kinds[item.kind];
    paintPlan();
  }

  // 记住上次的勾选：每个产品各记一份，只存在这台电脑的浏览器里。
  const memoryKey = () => `away-choices:${product}`;
  function remembered() {
    try { return JSON.parse(localStorage.getItem(memoryKey()) || "{}") || {}; } catch { return {}; }
  }
  function remember() {
    const body = $("#away-body");
    if (!body || !plan) return;
    const kinds = {};
    body.querySelectorAll("[data-away-kind]").forEach(box => { if (!box.disabled) kinds[box.dataset.awayKind] = box.checked; });
    try { localStorage.setItem(memoryKey(), JSON.stringify({...remembered(), kinds: {...remembered().kinds, ...kinds}, minutes})); } catch {}
  }

  function itemRow(item) {
    const disabled = !item.available || item.fixed;
    let extra = "";
    if (item.kind === "writing" && (item.topics?.length || item.plans?.length)) {
      const topics = item.topics?.length ? `<div class="away-label">选题机会（骨架齐全、近 7 天没写过，按选题页的顺序）</div><div class="away-choices away-list">${item.topics.map(row => `<label title="${esc(row.reason)}"><input type="checkbox" data-away-topic="${esc(row.id)}" ${row.default ? "checked" : ""}> ${esc(row.title)}${row.reviews ? `<small>${row.reviews} 种原文</small>` : ""}</label>`).join("")}</div>` : "";
      const plans = item.plans?.length ? `<div class="away-label">待写计划</div><div class="away-choices away-list">${item.plans.map(row => `<label title="${esc(row.note)}"><input type="checkbox" data-away-plan="${esc(row.id)}"> ${esc(row.direction_id)} · ${esc(row.note.slice(0, 36))}${row.note.length > 36 ? "…" : ""}<small>${esc(row.created_at)}</small></label>`).join("")}</div>` : "";
      extra = `<div class="away-sub">${topics}${plans}</div>`;
    }
    if (item.kind === "skeletons" && item.themes?.length) {
      extra = `<div class="away-sub away-choices">${item.themes.map(row => `<label title="${esc(row.reason)}"><input type="checkbox" data-away-theme="${esc(row.id)}" checked> ${esc(row.title)}</label>`).join("")}</div>`;
    }
    return `<li class="away-item${item.available ? "" : " is-off"}" data-kind="${esc(item.kind)}">
      <label class="away-item-main"><input type="checkbox" data-away-kind="${esc(item.kind)}" ${item.enabled || item.fixed ? "checked" : ""} ${disabled ? "disabled" : ""}>
        <span><b>${esc(item.label)}</b><small>${esc(item.detail)}</small></span><span class="away-lane" data-lane="${esc(item.lane)}">${laneNames[item.lane] || ""}</span></label>${extra}</li>`;
  }

  // The execution pool: orders the user set up on the voice form; each is voiced once, then cut, then waits for review.
  // 每张单的四步：配音定了、达人定了、素材勾了、标了就绪；空心的就是还差的
  function poolSteps(job) {
    const steps = [["配音", !!job.voice_config || !!job.can_direct], ["达人", !!job.creator], ["素材", !!job.materials?.length], ["就绪", !!job.ready]];
    const missing = steps.filter(([, done]) => !done).map(([name]) => name);
    return {missing, html: `<span class="away-steps" title="${missing.length ? `还差：${missing.join("、")}` : "都定好了"}">${steps.map(([name, done]) => `<span class="away-step${done ? " is-done" : ""}"><i></i>${name}</span>`).join("")}</span>`};
  }

  // 已经做过的步骤：配好的音不再配、建好的时间线不再建（跟着当前选的达人）
  function reuseNote(job) {
    const parts = [];
    if (job.take_note && !job.voice_config) parts.push(job.take_note);
    const built = job.built?.[job.creator];
    if (built) parts.push(`用已建好的时间线 ${built}，不再新建`);
    return parts.join("；");
  }

  function poolBrief(job) {
    if (job.problem) return esc(job.problem);
    const missing = poolSteps(job).missing;
    const what = `${esc(job.voice_config || !job.can_direct ? job.voice : "已配音，直接剪")} · ${esc(job.creator || "没选达人")} · ${job.materials?.length ? esc(job.materials.join("、")) : "没勾素材"}`;
    return missing.length && !(missing.length === 1 && missing[0] === "就绪") ? `还差：${missing.join("、")}` : what;
  }

  // The execution pool: one order open at a time; the others are one line each with their four steps.
  function poolBlock() {
    const pool = plan.pool || {jobs: []};
    const waiting = pool.waiting?.length ? `<div class="away-waiting"><small class="away-muted">还有 ${pool.waiting_count} 张已配音、没剪的单：</small>${pool.waiting.map(row => `<span>${esc(row.title)} <button type="button" class="link-button" data-away-pool-voiced="${esc(row.id)}">放进池子直接剪</button></span>`).join("")}</div>` : "";
    if (!pool.jobs.length) return `<div class="away-block away-pool"><div class="away-label">执行池</div><p class="away-muted">池子是空的。在候选稿点「采用并入池」，或在采用稿、制作单点「放进执行池」，出门时这里就会从配音一路跑到剪辑。</p>${waiting}</div>`;
    const creators = pool.creators || [];
    const ready = pool.jobs.filter(job => job.ready).length;
    if (!pool.jobs.some(job => job.id === openJob)) openJob = (pool.jobs.find(job => !job.ready && !job.problem) || {}).id || null;
    return `<div class="away-block away-pool"><div class="away-label">执行池 ${pool.jobs.length} 张 · 就绪 ${ready} 张（配音会花钱；默认只跑就绪的单）</div><small class="away-muted">${esc(pool.note || "")}</small>
      <div class="away-jobs">${pool.jobs.map(job => `<div class="away-job${job.id === openJob ? " is-open" : ""}" data-away-job="${esc(job.id)}" data-away-source="pool">
        <div class="away-job-head"><input type="checkbox" data-away-pool="${esc(job.id)}" ${job.default ? "checked" : ""} ${job.problem ? "disabled" : ""} aria-label="这次跑这张">
          <button type="button" class="away-job-toggle" data-away-toggle aria-expanded="${job.id === openJob}"><span class="away-caret"></span><b>${esc(job.title)}</b>${job.problem ? "" : job.ready ? '<span class="away-ready is-on">就绪</span>' : '<span class="away-ready">未就绪</span>'}${job.mode === "replica" ? "<small>1:1 复刻</small>" : ""}</button>
          ${job.problem ? "" : `<span data-away-steps>${poolSteps(job).html}</span>`}
          <button type="button" class="link-button" data-away-unpool="${esc(job.id)}">移出</button></div>
        <div class="away-job-brief" data-away-brief>${poolBrief(job)}</div>
        <small class="away-muted away-reuse" data-away-reuse ${reuseNote(job) ? "" : "hidden"}>${esc(reuseNote(job))}</small>
        <div class="away-job-body">
        ${job.script?.length ? `<details class="away-script" ${job.ready ? "" : "open"}><summary>口播稿 ${job.script.length} 句</summary>${scriptTable(job.script)}</details>` : ""}
        ${job.problem ? `<p class="away-muted">${esc(job.problem)}</p>` : job.ready ? `<div class="away-ready-summary"><span>${esc(job.voice)} · ${esc(job.creator)} · ${esc((job.materials || []).join("、"))}</span><button type="button" class="link-button" data-away-unready="${esc(job.id)}">改设置</button></div>` : `<div class="away-job-fields away-voice" data-away-voice-fields>${voiceFields(job)}</div>
        <div class="away-job-fields"><label>账号 <select data-away-creator>${creators.map(name => `<option ${name === job.creator ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
        </div><div class="away-mats" data-away-materials>${materialChecks(job, job.creator, job.materials)}</div>
        <div class="away-ready-bar"><button type="button" class="secondary compact" data-away-ready="${esc(job.id)}">设置好了，标为就绪</button><small class="away-muted">就绪后设置收起，出门时默认勾上</small></div>`}</div></div>`).join("")}</div>${waiting}</div>`;
  }

  // 口播稿两列对照：左边台词，右边画面类型（彩色标签）和画面要求
  const scriptTable = lines => `<div class="away-script-table"><span></span><span class="away-script-head">台词</span><span class="away-script-head">画面</span>${lines.map((line, index) => {
    const row = typeof line === "string" ? {say: line, types: [], shot: ""} : line;
    const tags = (row.types || []).map(type => `<span class="away-shot-tag" data-kind="${esc(type.kind)}">${esc(type.label)}</span>`).join("");
    return `<span class="away-script-no">${index + 1}</span><span class="away-script-say">${esc(row.say)}</span><span class="away-script-shot">${tags}${esc(row.shot || "")}${tags || row.shot ? "" : '<span class="away-muted">—</span>'}</span>`;
  }).join("")}</div>`;

  const SETTING_NAMES = {stability: "稳定性", similarity_boost: "相似度", style: "风格", speed: "语速"};

  // The voice an order will be read with, set right here: voice, model, language and the model's sliders,
  // or 「已配音，直接剪」 for an order that already has a chosen voiceover.
  function voiceFields(job) {
    const pool = plan.pool || {};
    const config = job.voice_config;
    const voices = pool.voices || [];
    const known = !config || voices.some(row => row.voice_id === config.voice_id);
    const choices = (job.can_direct ? `<option value="" ${config ? "" : "selected"}>已配音，直接剪</option>` : "")
      + (known ? "" : `<option value="${esc(config.voice_id)}" selected>${esc(config.voice_id)}</option>`)
      + voices.map(row => `<option value="${esc(row.voice_id)}" ${config?.voice_id === row.voice_id ? "selected" : ""}>${esc(row.label)}</option>`).join("");
    const voice = `<label>配音 <select data-away-voice>${choices}</select></label>`;
    if (!config) return voice;
    const models = pool.models || [];
    const model = models.find(row => row.id === config.model_id);
    const modelPick = `<label>模型 <select data-away-model>${model ? "" : `<option selected>${esc(config.model_id)}</option>`}${models.map(row => `<option value="${esc(row.id)}" ${row.id === config.model_id ? "selected" : ""}>${esc(row.name)}</option>`).join("")}</select></label>`;
    const marketPick = `<label>语言 <select data-away-market>${(pool.markets || []).map(row => `<option value="${esc(row.id)}" ${row.id === (config.market || "us") ? "selected" : ""}>${esc(row.label)}</option>`).join("")}</select></label>`;
    const sliders = (model?.settings || []).map(row => {
      const value = config.settings?.[row.key] ?? row.default;
      return `<label class="away-slider">${esc(SETTING_NAMES[row.key] || row.key)} <input type="range" data-away-setting="${esc(row.key)}" min="${row.low}" max="${row.high}" step="0.01" value="${value}"><output>${Number(value).toFixed(2)}</output></label>`;
    }).join("");
    return voice + modelPick + marketPick + sliders;
  }

  // Changing the voice takes that voice's own saved model and settings; changing the model keeps the sliders it shares.
  function changeVoice(job, row, target) {
    const pool = plan.pool || {};
    if (target.dataset.awayVoice !== undefined) {
      const saved = (pool.voices || []).find(voice => voice.voice_id === target.value);
      job.voice_config = !target.value ? null : saved
        ? {voice_id: saved.voice_id, model_id: saved.model_id || job.voice_config?.model_id || pool.models?.[0]?.id, settings: {...saved.settings}, market: job.voice_config?.market || "us"}
        : {...job.voice_config, voice_id: target.value};
    } else if (target.dataset.awayModel !== undefined) {
      job.voice_config = {...job.voice_config, model_id: target.value};
    } else if (target.dataset.awayMarket !== undefined) {
      job.voice_config = {...job.voice_config, market: target.value};
      return;
    } else if (target.dataset.awaySetting !== undefined) {
      job.voice_config = {...job.voice_config, settings: {...job.voice_config.settings, [target.dataset.awaySetting]: Number(target.value)}};
      return;
    }
    if (job.voice_config) {  // keep only the sliders this model has, filling the rest with its defaults
      const model = (pool.models || []).find(item => item.id === job.voice_config.model_id);
      if (model) job.voice_config.settings = Object.fromEntries(model.settings.map(item => [item.key, job.voice_config.settings?.[item.key] ?? item.default]));
    }
    row.querySelector("[data-away-voice-fields]").innerHTML = voiceFields(job);
  }

  const splitNames = value => (value || "").split(/[,，、]/).map(name => name.trim()).filter(Boolean);
  const planJob = id => plan?.pool?.jobs?.find(row => row.id === id);

  // What the user sets on a pool row stays with the pool, so it is still there the next time the dialog opens.
  async function savePoolRow(row) {
    try {
      const job = planJob(row.dataset.awayJob);
      const creator = row.querySelector("[data-away-creator]")?.value || "", materials = chosenMaterials(row);
      if (job) {
        Object.assign(job, {creator, materials});
        const steps = row.querySelector("[data-away-steps]");
        if (steps) steps.innerHTML = poolSteps(job).html;
        row.querySelector("[data-away-brief]").innerHTML = poolBrief(job);
        const reuse = row.querySelector("[data-away-reuse]");
        if (reuse) { reuse.textContent = reuseNote(job); reuse.hidden = !reuseNote(job); }
      }
      await api("/api/away/pool", {method: "POST", body: {op: "update", job_id: row.dataset.awayJob, creator, materials,
        ...(job ? {voice: job.voice_config || null} : {})}});
    } catch (error) { toast(error.message); }
  }

  // The creator's whole material library as ticks; names typed into 「其他」 cover footage the library has not seen yet.
  function materialChecks(job, creator, chosen) {
    const library = job.libraries?.[creator] || {names: [], seen_at: ""};
    const picked = new Set(chosen || []);
    const known = new Set(library.names.map(row => row.name));
    const extra = [...picked].filter(name => !known.has(name));
    const seen = library.seen_at ? `素材库 ${library.seen_at.slice(5, 10)} 从达芬奇读过，之后剪辑用过的也在里面` : "素材库来自以前剪辑用过的；在达芬奇里打开这个达人的工程再开这个弹窗，会把工程里全部素材时间线记下来";
    const checks = library.names.map(row => `<label title="${row.used ? `用过 ${row.used} 次，最近 ${esc(row.last.slice(5))}` : "还没用过"}"><input type="checkbox" value="${esc(row.name)}" ${picked.has(row.name) ? "checked" : ""}> ${esc(row.name)}${row.used ? "" : "<small>新</small>"}</label>`).join("");
    return `${checks || '<span class="away-muted">这个达人在这个产品还没有记下的素材时间线</span>'}
      <label class="away-mats-other">其他 <input data-away-other value="${esc(extra.join("，"))}" placeholder="逗号分隔"></label>
      <small class="away-pick">${seen}</small>`;
  }

  const chosenMaterials = row => [...row.querySelectorAll("[data-away-materials] input[type=checkbox]:checked")].map(box => box.value)
    .concat(splitNames(row.querySelector("[data-away-other]")?.value));

  function paintPlan() {
    const choices = shell()?.getProductChoices?.() || [];
    const productPick = choices.length > 1 ? `<label class="away-product">产品 <select id="away-product">${choices.map(row => `<option value="${esc(row.value)}" ${row.value === product ? "selected" : ""}>${esc(row.label)}</option>`).join("")}</select></label>` : "";
    $("#away-body").innerHTML = `${productPick}
      <div class="away-block"><div class="away-label">大概多久回来</div><div class="away-chips" role="radiogroup" aria-label="大概多久回来">${plan.minutes.map(value => `<button type="button" role="radio" class="away-chip" data-away-minutes="${value}" aria-checked="${value === minutes}">${value / 60} 小时</button>`).join("")}</div>
        <small class="away-muted">回来前 ${plan.stop_before_minutes} 分钟起不再开始新的活，正在跑的会跑完。AI 同时最多 ${plan.ai_parallel} 件。</small></div>
      ${poolBlock()}
      <div class="away-block"><div class="away-label">这次要做的（按现在的数据挑好了，可以勾掉；勾选和时长会记住，下次照这样勾）</div><ul class="away-items">${plan.items.map(itemRow).join("")}</ul></div>
      <div class="away-budget"><span>这次用 AI <b id="away-cost">0</b> 次</span><label>上限 <input type="number" id="away-limit" min="1" max="40" value="${plan.ai_limit}"> 次</label><span>预计 <b id="away-minutes">0</b> 分钟</span></div>
      <p class="away-never">${esc(plan.never)}</p><p class="away-error" id="away-error" role="alert" hidden></p>`;
    $("#away-footer").innerHTML = `<span class="away-footer-sum" id="away-footer-sum"></span><div><button type="button" class="secondary" data-away-close>取消</button><button type="button" class="primary" id="away-start">开始，我去拍了</button></div>`;
    const body = $("#away-body");
    body.onchange = event => {
      if (event.target.id === "away-product") { product = event.target.value; loadPlan(); return; }
      const jobRow = event.target.closest("[data-away-job]");
      if (event.target.dataset.awayCreator !== undefined && jobRow) {
        const job = planJob(jobRow.dataset.awayJob);
        const box = jobRow.querySelector("[data-away-materials]");
        if (job && box) box.innerHTML = materialChecks(job, event.target.value, job.latest?.[event.target.value] || []);  // their latest cut, ticked
        savePoolRow(jobRow);
      }
      if (jobRow && event.target.closest("[data-away-materials]")) savePoolRow(jobRow);
      if (jobRow && event.target.closest("[data-away-voice-fields]")) {
        const job = planJob(jobRow.dataset.awayJob);
        if (job) { changeVoice(job, jobRow, event.target); savePoolRow(jobRow); }
      }
      if (event.target.dataset.awayKind !== undefined) remember();
      summarize();
    };
    body.oninput = event => {
      const slider = event.target.closest?.("[data-away-setting]");
      if (slider) slider.nextElementSibling.textContent = Number(slider.value).toFixed(2);
      summarize();
    };
    body.onclick = async event => {
      const voiced = event.target.closest("[data-away-pool-voiced]");
      if (voiced) {
        try { await api("/api/away/pool", {method: "POST", body: {op: "add", job_id: voiced.dataset.awayPoolVoiced, voice: null}}); loadPlan(); }
        catch (error) { toast(error.message); }
        return;
      }
      const toggle = event.target.closest("[data-away-toggle]");
      if (toggle) {
        const row = toggle.closest("[data-away-job]");
        const opening = !row.classList.contains("is-open");
        body.querySelectorAll(".away-job.is-open").forEach(node => { node.classList.remove("is-open"); node.querySelector("[data-away-toggle]")?.setAttribute("aria-expanded", "false"); });
        if (opening) { row.classList.add("is-open"); toggle.setAttribute("aria-expanded", "true"); }
        openJob = opening ? row.dataset.awayJob : null;
        return;
      }
      const ready = event.target.closest("[data-away-ready], [data-away-unready]");
      if (ready) {
        const row = ready.closest("[data-away-job]");
        const on = ready.dataset.awayReady !== undefined;
        openJob = on ? null : row.dataset.awayJob;
        const job = planJob(row.dataset.awayJob);
        ready.disabled = true;
        try {
          await api("/api/away/pool", {method: "POST", body: {op: "update", job_id: row.dataset.awayJob, ready: on,
            ...(on ? {creator: row.querySelector("[data-away-creator]")?.value || "", materials: chosenMaterials(row), voice: job?.voice_config || null} : {})}});
          loadPlan();
        } catch (error) { toast(error.message); ready.disabled = false; }
        return;
      }
      const out = event.target.closest("[data-away-unpool]");
      if (out) {
        event.preventDefault();
        try { await api("/api/away/pool", {method: "POST", body: {op: "remove", job_id: out.dataset.awayUnpool}}); loadPlan(); }
        catch (error) { toast(error.message); }
        return;
      }
      const chip = event.target.closest("[data-away-minutes]");
      if (!chip) return;
      minutes = Number(chip.dataset.awayMinutes);
      body.querySelectorAll("[data-away-minutes]").forEach(node => node.setAttribute("aria-checked", String(node === chip)));
      remember();
      summarize();
    };
    $("#away-start").onclick = start;
    summarize();
  }

  function picked() {
    const body = $("#away-body");
    const items = [];
    const pool = [...body.querySelectorAll("[data-away-pool]:checked")].map(box => {
      const row = box.closest("[data-away-job]");
      const job = planJob(box.dataset.awayPool);
      if (job?.ready) return {id: job.id, creator: job.creator, materials: job.materials};
      return {id: box.dataset.awayPool, creator: row.querySelector("[data-away-creator]")?.value || "", materials: chosenMaterials(row)};
    });
    if (pool.length) items.push({kind: "pool", jobs: pool});
    body.querySelectorAll("[data-away-kind]").forEach(box => {
      const kind = box.dataset.awayKind;
      if (!box.checked || box.disabled || kind === "check") return;
      if (kind === "writing") {
        const topics = [...body.querySelectorAll("[data-away-topic]:checked")].map(node => node.dataset.awayTopic);
        const plans = [...body.querySelectorAll("[data-away-plan]:checked")].map(node => node.dataset.awayPlan);
        if (topics.length || plans.length) items.push({kind, topics, plans});
      } else if (kind === "skeletons") {
        const themes = [...body.querySelectorAll("[data-away-theme]:checked")].map(node => node.dataset.awayTheme);
        if (themes.length) items.push({kind, themes});
      } else items.push({kind});
    });
    return items;
  }

  function summarize() {
    if (!plan) return;
    const items = picked();
    const per = {translate: 6, tags: 6, scrape: 15, backup: 10};
    let cost = 0, ai = 0, other = 0, edits = 0;
    for (const item of items) {
      if (item.kind === "writing") { const count = item.topics.length + item.plans.length; cost += count; ai += count * 7; }
      else if (item.kind === "skeletons") { cost += item.themes.length; ai += item.themes.length + 1; }
      else if (item.kind === "translate" || item.kind === "tags") { cost += 1; ai += per[item.kind]; }
      else if (item.kind === "pool") { cost += item.jobs.length; edits += item.jobs.length * 15 + 2; }
      else other = Math.max(other, per[item.kind] || 0);
    }
    const backup = items.some(item => item.kind === "backup") ? per.backup : 0;
    const estimate = Math.round(Math.max(ai / (plan.ai_parallel || 2), edits, items.some(item => item.kind === "scrape") ? per.scrape : 0) + backup + 1);
    const limit = Number($("#away-limit")?.value) || plan.ai_limit;
    $("#away-cost").textContent = cost;
    $("#away-cost").classList.toggle("is-over", cost > limit);
    $("#away-minutes").textContent = estimate;
    const poolJobs = items.find(item => item.kind === "pool")?.jobs || [];
    const voicing = poolJobs.filter(row => planJob(row.id)?.voice_config).length;
    const sum = $("#away-footer-sum");
    if (sum) sum.innerHTML = `${poolJobs.length ? `剪 <b>${poolJobs.length}</b> 张${voicing ? `（配音 <b>${voicing}</b> 条）` : ""} · ` : ""}AI <b>${cost}</b> 次 · 预计 <b>${estimate}</b> 分钟`;
    const error = $("#away-error");
    const problem = !items.length ? "至少勾一件要做的活。" : cost > limit ? `超过 AI 上限 ${limit} 次：少勾几个方向或主题，或调高上限。` : estimate > minutes - plan.stop_before_minutes ? "预计时间比你出去的时间长，排不上的会留到你回来。" : "";
    error.hidden = !problem;
    error.textContent = problem;
    $("#away-start").disabled = !items.length || cost > limit;
  }

  async function start() {
    if (loading) return;
    loading = true;
    const startButton = $("#away-start");
    startButton.disabled = true;
    try {
      const data = await api("/api/away/start", {method: "POST", body: {product, minutes, ai_limit: Number($("#away-limit").value) || plan.ai_limit, items: picked()}});
      current = data.current;
      plan = null;
      toast(`值守开始了，预计 ${clock(current.return_at)} 回来。`);
      dialog().close();
      paintButton();
      paintHome();
      schedule();
    } catch (error) {
      const box = $("#away-error");
      box.hidden = false;
      box.textContent = error.message;
      startButton.disabled = false;
    } finally {
      loading = false;
    }
  }

  // 执行池的单：配音 → 选用 → 剪辑 → 等你确认，一张单一行；失败的那步标红，后面写原因
  function poolFlows(session) {
    const orders = new Map();
    for (const item of session.items) {
      if (!item.params?.pool || !["voice", "edit"].includes(item.kind)) continue;
      const row = orders.get(item.params.job_id) || {title: item.label.replace(/^[^·]*·\s*/, ""), voice: null, edit: null};
      row[item.kind] = item;
      orders.set(item.params.job_id, row);
    }
    if (!orders.size) return "";
    const state = item => item ? item.status : "done";
    const step = (name, status, extra = "") => `<span class="away-flow-step" data-state="${esc(status)}">${name}${extra}</span>`;
    return `<div class="away-flows"><div class="away-label">执行池</div>${[...orders.values()].map(row => {
      const voice = state(row.voice), edit = state(row.edit);
      const picked = row.voice ? (voice === "done" ? "done" : voice === "failed" ? "skipped" : "pending") : "done";
      const review = edit === "done" ? "running" : ["failed", "skipped", "cancelled"].includes(edit) ? "skipped" : "pending";
      const error = [row.voice, row.edit].find(item => item?.status === "failed" || (item?.status === "skipped" && item.error));
      return `<div class="away-flow"><b>${esc(row.title)}</b><span class="away-flow-steps">${step(row.voice ? "配音" : "已配音", voice)}${step("选用", picked)}${step("剪辑", edit)}${step("等你确认", review)}</span>${error?.error ? `<small class="away-flow-error">${esc(error.error)}</small>` : ""}</div>`;
    }).join("")}</div>`;
  }

  function statusRows(session) {
    return `${poolFlows(session)}<ul class="away-status">${session.items.filter(item => !(item.params?.pool && ["voice", "edit"].includes(item.kind))).map(item => `<li data-state="${esc(item.status)}"><span class="away-pill" data-state="${esc(item.status)}">${statusNames[item.status] || esc(item.status)}</span><span><b>${esc(item.label)}</b>${item.error ? `<small>${esc(item.error)}</small>` : ""}</span><span class="away-lane" data-lane="${esc(item.lane)}">${laneNames[item.lane] || ""}</span></li>`).join("")}</ul>`;
  }

  function paintStatus() {
    const session = current;
    if (!session) return;
    const running = session.status === "running";
    $("#away-title").textContent = running ? "值守中" : "值守结束";
    $("#away-body").innerHTML = `<p class="away-muted">${esc(clock(session.started_at))} 开始 · 预计 ${esc(clock(session.return_at))} 回来${running ? ` · 还剩 ${minutesLeft(session)} 分钟` : ""} · AI ${session.ai_cost}/${session.ai_limit} 次${session.stopped ? " · 你让它停下了" : ""}</p>${statusRows(session)}<p class="away-never">不会做：执行池以外的配音、采用 / 定稿、确认剪辑完成、挑评论、发布、上传新内容、任何删除</p>`;
    $("#away-footer").innerHTML = `<span class="away-muted">${running ? "停下后排着的活取消，正在跑的会跑完。" : ""}</span><div><button type="button" class="secondary" data-away-close>关闭</button>${running && !session.stopped ? '<button type="button" class="danger-outline" id="away-stop">停下</button>' : ""}</div>`;
    const stop = $("#away-stop");
    if (stop) stop.onclick = async () => {
      stop.disabled = true;
      try {
        current = (await api("/api/away/stop", {method: "POST", body: {}})).current;
        toast("已停下：排着的活取消了，正在跑的会跑完。");
        paintStatus();
        paintButton();
        paintHome();
      } catch (error) {
        toast(error.message, true);
        stop.disabled = false;
      }
    };
  }

  // ---------- home card ----------
  function reportGroup(title, rows, kind, owner) {
    if (!rows.length) return "";
    return `<div class="away-group" data-group="${kind}"><h3>${title}</h3>${rows.map(row => `<div class="away-row"><span>${esc(row.text)}</span>${row.link ? `<a href="${esc(link(row.link.hash, owner))}">${esc(row.link.label || "去看")} ›</a>` : ""}</div>`).join("")}</div>`;
  }

  function homeCard(session) {
    const report = session.report, counts = report.counts;
    const running = session.status === "running";
    const head = running ? `值守中 · ${clock(session.started_at)} 开始，预计 ${clock(session.return_at)} 回来` : `你不在时 · ${clock(session.started_at)}–${clock(session.finished_at)}`;
    const summary = `<div class="away-counts"><span data-group="done"><b>${counts.done}</b>做好了</span><span data-group="waiting"><b>${counts.waiting}</b>等你决定</span><span data-group="failed"><b>${counts.failed}</b>没做成</span></div>`;
    const progress = running ? `<p class="away-muted">${session.items.filter(item => item.status === "running").map(item => esc(item.label)).join("、") || "正在排下一件"}</p>` : "";
    return `<section class="away-card" id="away-card" aria-label="值守结果"><div class="away-card-head"><h2>${esc(head)}</h2><div>${running ? '<button type="button" class="secondary compact" data-away-show>看进度</button>' : `<button type="button" class="subtle compact" data-away-seen="${esc(session.id)}">收起</button>`}</div></div>
      ${summary}${progress}${reportGroup("等你决定", report.waiting, "waiting", session.product)}${running ? "" : reportGroup("做好了", report.done, "done", session.product)}${reportGroup("没做成", report.failed, "failed", session.product)}</section>`;
  }

  function paintHome() {
    const root = $("#home-page");
    const old = $("#away-card");
    const show = current && (current.status === "running" || !current.seen);
    if (!root || !show) { old?.remove(); return; }
    const markup = homeCard(current);
    if (old && old.dataset.markup === markup) return;
    const holder = document.createElement("div");
    holder.innerHTML = markup;
    const card = holder.firstElementChild;
    card.dataset.markup = markup;
    card.addEventListener("click", async event => {
      if (event.target.closest("[data-away-show]")) return open();
      const seen = event.target.closest("[data-away-seen]");
      if (!seen) return;
      seen.disabled = true;
      try {
        current = (await api("/api/away/seen", {method: "POST", body: {id: seen.dataset.awaySeen}})).current;
        paintHome();
      } catch (error) {
        toast(error.message, true);
        seen.disabled = false;
      }
    });
    if (old) old.replaceWith(card);
    else {
      const heading = root.querySelector(".home-heading");
      heading ? heading.after(card) : root.prepend(card);
    }
  }

  function watchHome() {
    const content = $("#content");
    if (!content) return;
    // 工作概览每次刷新都会整页重画，重画后把卡片放回去
    new MutationObserver(() => {
      if (!$("#home-page")) return;
      if (!$("#away-card") && current) paintHome();
      if (!$("#away-home-open") && $("#home-page .home-heading-actions")) paintButton();
    }).observe(content, {childList: true, subtree: true});
  }

  // 制作单的配音区只管「放进执行池」：配音设置（音色、模型、语言、滑杆）在「我去拍了」的执行池里改。
  // 放进去时带上这张单上次配音的设置（没配过就用产品默认音色）；已选用配音的单默认直接剪。
  function poolButton() {
    const form = $("#take-form");
    const actions = form?.querySelector(".take-actions");
    if (!actions || actions.querySelector("[data-away-pool-add]")) return;
    const job = (location.hash.match(/(?:^#|\/)production\/(PRD-[0-9a-f]{12})/) || [])[1];  // #/202H/production/PRD-…
    if (!job) return;
    const add = document.createElement("button");
    add.type = "button";
    add.className = "secondary";
    add.dataset.awayPoolAdd = job;
    add.textContent = "放进执行池";
    add.title = "出门时值守按池子里的设置配一条、选用、再 AI 剪辑（已选用配音的直接剪）；配音设置在「我去拍了」的执行池里改";
    actions.querySelector('button[type="submit"]')?.after(add);
    add.onclick = async () => {
      add.disabled = true;
      try {
        await poolAdd(job);
        add.textContent = "已在执行池";
        toast("放进执行池了：配音设置、达人和素材都在「我去拍了」的执行池里改，点开始就跑。");
      } catch (error) { toast(error.message); }
      add.disabled = false;
    };
    api("/api/away/pool").then(data => { if ((data.pool || []).some(row => row.job_id === job)) add.textContent = "已在执行池"; }).catch(() => {});
  }

  const poolAdd = jobId => api("/api/away/pool", {method: "POST", body: {op: "add", job_id: jobId}});

  // 采用稿页：一步放进执行池。这条稿还没有制作单就先建一张，有就放最新那张。
  async function poolScript(script, jobs) {
    let job = (jobs || [])[0];
    if (!job) {
      const {revision} = await api("/api/production");
      job = (await api("/api/production/jobs", {method: "POST", body: {revision, request_id: crypto.randomUUID(), job: {script_id: script.script_id || script.id}}})).job;
    }
    await poolAdd(job.id);
    refresh();
    return job;
  }

  function watchPoolButton() {
    const content = $("#content");
    if (!content) return;
    new MutationObserver(() => { if ($("#take-form")) poolButton(); }).observe(content, {childList: true, subtree: true});
  }

  function init() {
    if (!button()) return;
    paintButton();
    watchHome();
    watchPoolButton();
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    refresh();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
  return {open, refresh, poolScript};
})();
