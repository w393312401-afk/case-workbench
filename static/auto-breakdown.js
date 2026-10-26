// 收为案例后的自动下载与拆解：启动任务、在案例详情的视频栏显示进度、失败时重试；
// 以及「拆解定位」：这条视频赢在哪一环、对照视频、观众评论、结构化字段。
(() => {
  const active = new Set(["queued", "downloading", "attaching", "analyzing"]);
  const tone = { completed: "is-done", failed: "is-failed", interrupted: "is-failed", analyzer_pending: "is-waiting" };
  const funnelTone = { both: "is-done", traffic: "is-traffic", conversion: "is-done", views_no_orders: "is-failed", plain: "", unknown: "" };
  const topicNames = {"clamp": "夹具与抓握", "pivot": "翻转与旋转机构", "stability": "晃动、不稳、底座", "angle": "角度刻度与精度", "small_blade": "小刀与短薄刀", "edge_reach": "刃口末端磨不到", "blade_fit": "能磨哪些刀和工具", "stones": "磨板耐用与品质", "accessories": "替换磨板与配件", "instructions": "说明书与上手", "learning": "学习曲线与节奏", "price": "价格与性价比", "support": "客服与保修", "wrong_sku": "买错款式或购买页", "shipping": "配送、缺货与缺件", "build": "做工与品控", "case": "箱子与收纳", "mirror": "镜面与抛光", "repair": "修复、崩口与重塑", "many_knives": "一次磨很多把刀", "strop": "皮革荡刀", "handsharp": "手磨派与磨刀石派", "proof": "演示可信度", "ai_voice": "疑似 AI 声音或形象", "review_influence": "受评测视频影响", "gift": "送礼与做生意"};
  const tagNames = { hook_types: "开头类型", skeleton_id: "骨架", lead_code: "主打卖点", progression: "推进结构", carrier: "载体", duration_sec: "时长", opening: "开头画面" };
  let timer = null;

  // AI 拆解 and 拆解定位 share one 「拆解概况」 card in the 文案与拆解 tab, right above 原始拆解; the video rail stays as it was.
  const host = () => document.querySelector("#detail-panel-script");
  function overview(panel) {
    let card = panel.querySelector("#breakdown-overview");
    if (!card) {
      card = document.createElement("section");
      card.id = "breakdown-overview";
      card.className = "section-card auto-breakdown breakdown-overview";
      const source = panel.querySelector("#detail-source-analysis");
      if (source) source.before(card); else panel.append(card);
    }
    return card;
  }

  async function start(caseId, videoId, retry = false, settings = {}) {
    const body = { video_id: videoId || "", retry, model: settings.model || null, reasoning_effort: settings.reasoning_effort || null };
    const result = await api(`/api/cases/${caseId}/auto-breakdown`, { method: "POST", body });
    return result.job;
  }

  // 「AI 拆解」 picks a model for one run, like the writing dialog; empty means the global default.
  const effortNames = { none: "不启用", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最高", ultra: "极致" };
  let options = null;
  async function loadOptions() {
    if (options) return options;
    try {
      const result = await api("/api/ai/options");
      options = { models: Array.isArray(result.models) ? result.models : [], defaults: result.default_settings || {} };
    } catch { options = { models: [], defaults: {} }; }
    return options;
  }
  const provider = (m) => m.provider || (String(m.id).startsWith("claude-") ? "claude" : "codex");
  const modelName = (id) => options?.models.find((m) => m.id === id)?.name || id;
  function runMarkup(job) {
    if (!options) return "";
    const d = options.defaults, pick = job?.model || "";
    const fallback = d.model ? `${modelName(d.model)}${d.reasoning_effort ? ` · ${effortNames[d.reasoning_effort] || d.reasoning_effort}` : ""}` : "本机自动选择";
    const option = (m) => `<option value="${esc(m.id)}" ${m.id === pick ? "selected" : ""} ${m.available === false ? "disabled" : ""}>${esc(m.name || m.id)}${m.available === false ? " · 未就绪" : ""}</option>`;
    const groups = [["claude", "Claude"], ["codex", "Codex"]].map(([key, label]) => {
      const list = options.models.filter((m) => provider(m) === key);
      return list.length ? `<optgroup label="${label}">${list.map(option).join("")}</optgroup>` : "";
    }).join("");
    const label = !job ? "AI 拆解" : job.status === "completed" || job.status === "analyzer_pending" ? "重新 AI 拆解" : "重试 AI 拆解";
    const run = `<div class="auto-breakdown-run"><label class="field"><span>模型</span><select data-run-model><option value="">全局默认（${esc(fallback)}）</option>${groups}</select></label><label class="field"><span>思考强度</span><select data-run-effort></select></label><button type="button" class="${job && job.status !== "failed" && job.status !== "interrupted" ? "secondary" : "primary"} compact" data-run-start>${label}</button></div>`;
    return job && (job.status === "completed" || job.status === "analyzer_pending") ? `<details class="auto-breakdown-rerun"><summary>重新 AI 拆解</summary>${run}</details>` : run;
  }
  function paintEfforts(box, job) {
    const select = box.querySelector("[data-run-model]"), effort = box.querySelector("[data-run-effort]");
    if (!select || !effort) return;
    const model = options.models.find((m) => m.id === select.value);
    const list = (model?.reasoning_efforts || []).filter((v) => typeof v === "string");
    const keep = effort.value || (select.value === (job?.model || "") ? job?.reasoning_effort : "") || "";
    effort.innerHTML = `<option value="">${select.value ? "模型默认" : "全局默认"}</option>` + list.map((v) => `<option value="${esc(v)}" ${v === keep ? "selected" : ""}>${esc(effortNames[v] || v)}</option>`).join("");
    effort.disabled = !select.value || !list.length;
  }
  function bindRun(box, item, job) {
    const select = box.querySelector("[data-run-model]");
    if (!select) return;
    paintEfforts(box, job);
    select.addEventListener("change", () => { box.querySelector("[data-run-effort]").value = ""; paintEfforts(box, job); });
    box.querySelector("[data-run-start]").addEventListener("click", async (event) => {
      event.currentTarget.disabled = true;
      const settings = { model: select.value, reasoning_effort: box.querySelector("[data-run-effort]").value };
      try { await start(item.case_id, job?.video_id || "", true, settings); toast("已开始 AI 拆解，完成后写进来源文档。"); }
      catch (error) { toast(error.message, true); }
      void paint(item);
    });
  }

  function markup(job) {
    if (!job) return `<details class="auto-breakdown-rerun"><summary>AI 拆解 · 设置与运行</summary><p>已有来源拆解会保留。按需转写原片、结合抽帧生成八章拆解并保存；无视频时先下载。</p>${runMarkup(null)}</details>`;
    const used = job.analysis?.model || job.model;
    const facts = [used && `模型 ${modelName(used)}`, job.video_source && `视频来源 ${job.video_source}`].filter(Boolean);
    const meta = facts.length ? `<small class="auto-breakdown-meta">${esc(facts.join(" · "))}</small>` : "";
    const message = job.message && job.status !== "completed" ? `<p>${esc(job.message)}</p>` : "";
    const live = active.has(job.status) && job.live ? `<div class="auto-breakdown-live"><small>${esc(job.live.stage || "写拆解")}${job.live.chars ? ` · 已写 ${num(job.live.chars)} 字` : ""}${job.live.note && !job.live.text ? ` · ${esc(job.live.note)}` : ""}</small>${job.live.text ? `<pre data-live-text>${esc(job.live.text)}</pre>` : ""}</div>` : "";
    return `<div class="auto-breakdown-head"><strong>AI 拆解</strong><span class="auto-breakdown-state">${active.has(job.status) ? "⏳ " : ""}${esc(job.label || job.status)}</span>${meta}</div>${message}${live}${active.has(job.status) ? "" : runMarkup(job)}`;
  }

  const pct = (value) => (value == null ? "—" : `${(value * 100).toFixed(2)}%`);
  const num = (value) => (value == null ? "—" : Number(value).toLocaleString("en-US"));
  const videoLink = (id) => window.WorkbenchShell?.url(`#videos?video=${encodeURIComponent(id)}`) || `#videos?video=${encodeURIComponent(id)}`;

  function insightsMarkup(data, stats) {
    const parts = [], quiet = [];
    if (data.missing_video_id) {
      parts.push(`<p class="case-insight-warn">这条案例没有视频 ID，关联不到销量、评论和对照视频。<button type="button" class="subtle" data-insight-metrics>补视频 ID</button></p>`);
    }
    const f = data.funnel;
    if (f) {
      parts.push(`<div class="case-insight-funnel ${funnelTone[f.key] || ""}"><strong>${esc(f.key === "traffic" ? "流量突出 · 未达强转化阈值" : f.label)}</strong><span>${esc(f.focus)}</span></div>`);
      parts.push(`<details class="case-insight-basis"><summary>后台销售数据定位 · 口径与阈值</summary><p>累计口径按后台销售数据，不采用公开页累计播放；与历史结论可能不在同一时点。${f.benchmark?.peers != null ? `同品样本 ${num(f.benchmark.peers)} 条。` : ""}“强转化”需达到同品 CTOR 中位数的 1.5 倍，不等于只要高于中位数。</p></details>`);
      if (f.reasons?.length) parts.push(`<ul class="case-insight-reasons">${f.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`);
    } else if (!data.missing_video_id) {
      quiet.push("后台视频数据里还没有这条视频，暂不判断赢在哪一环");
    }
    if (data.contrasts?.length) {
      parts.push(`<h3>对照：同产品、播放相近但没卖出去</h3><ul class="case-insight-contrasts">${data.contrasts.map((c) => `<li><a href="${esc(videoLink(c.video_id))}">${esc(c.headline || c.video_id)}</a><small>${num(c.views)} 播放 · ${num(c.orders)} 单 · 点击转化 ${pct(c.ctor)} · ${esc(c.posted_on || "")}</small></li>`).join("")}</ul>`);
    } else if (f && f.key !== "unknown") {
      quiet.push("没找到合适的对照视频（同产品、播放相差两倍内、60 天内发布、订单明显更少）");
    }
    const c = data.comments;
    if (c?.total) {
      const topics = Object.entries(c.topics || {}).sort((a, b) => b[1] - a[1]).slice(0, 6);
      parts.push(`<h3>观众评论 · ${num(c.total)} 条</h3>${topics.length ? `<p class="case-insight-chips">${topics.map(([k, n]) => `<span>${esc(topicNames[k] || k)} ${n}</span>`).join("")}</p>` : ""}<ul class="case-insight-comments">${(c.items || []).slice(0, 5).map((r) => `<li>${esc(r.text)}${r.likes > 1 ? `<small>${num(r.likes)} 赞</small>` : ""}</li>`).join("")}</ul>`);
    } else if (c) {
      quiet.push("评论库里还没有这条视频的评论");
    }
    const tags = Object.entries(tagNames).map(([key, label]) => {
      const value = data.tags?.[key];
      if (value == null || value === "" || (Array.isArray(value) && !value.length)) return "";
      const shown = Array.isArray(value) ? value.join(" / ") : key === "duration_sec" ? `${value} 秒` : value;
      const stat = stats?.[key]?.[Array.isArray(value) ? value[0] : value];
      const extra = stat && stat.with_sales > 1 && stat.orders_per_1k_median != null ? `（同产品 ${stat.with_sales} 条案例，每千播放中位 ${stat.orders_per_1k_median} 单）` : "";
      return `<li><span>${esc(label)}</span>${esc(shown)}<small>${esc(extra)}</small></li>`;
    }).filter(Boolean);
    if (tags.length) parts.push(`<h3>拆解字段</h3><ul class="case-insight-tags">${tags.join("")}</ul>`);
    const note = quiet.length ? `<p class="case-insight-muted">${esc(quiet.join("；"))}。</p>` : "";
    // The transcript's caveat already shows under 完整文案.
    const rule = "";
    if (!parts.length) return quiet.length ? `<p class="case-insight-muted case-insight-empty"><strong>拆解定位</strong>${esc(quiet.join("；"))}。</p>` : "";
    return `<div class="auto-breakdown-head"><strong>拆解定位</strong></div>${parts.join("")}${note}${rule}`;
  }

  async function paintInsights(item) {
    const panel = host();
    if (!panel || state.detail?.case_id !== item.case_id) return;
    let data, stats = null;
    try { data = (await api(`/api/cases/${item.case_id}/insights`)).insights; } catch { return; }
    try { stats = (await api(`/api/case-tags/stats?product=${encodeURIComponent(item.product || "")}`)).stats; } catch {}
    if (state.detail?.case_id !== item.case_id) return;
    const card = overview(panel);
    let box = card.querySelector("#case-insights");
    if (!box) {
      box = document.createElement("div");
      box.id = "case-insights";
      box.className = "case-insights";
      card.append(box);
    }
    box.innerHTML = insightsMarkup(data, stats);
    box.hidden = !box.innerHTML;
    box.querySelector("[data-insight-metrics]")?.addEventListener("click", () => {
      const edit = document.querySelector('[data-inline-edit="metrics"]');
      if (edit) { edit.scrollIntoView({ block: "center" }); edit.click(); } else toast("在「数据」页编辑数据关联，填上视频 ID。");
    });
  }

  async function paint(item) {
    clearTimeout(timer);
    const panel = host();
    if (!panel || state.detail?.case_id !== item.case_id) return;
    let job;
    try { [job] = await Promise.all([api(`/api/cases/${item.case_id}/auto-breakdown`).then((r) => r.job), loadOptions()]); } catch { return; }
    if (state.detail?.case_id !== item.case_id) return;
    const card = overview(panel);
    let box = card.querySelector("#auto-breakdown-status");
    if (!box) {
      box = document.createElement("div");
      box.id = "auto-breakdown-status";
      box.setAttribute("role", "status");
      card.prepend(box);
    }
    card.className = `section-card auto-breakdown breakdown-overview ${tone[job?.status] || ""}`;
    // Keep the live text pinned to the bottom while it grows, unless the reader scrolled up to read.
    const before = box.querySelector("[data-live-text]");
    const pinned = !before || before.scrollHeight - before.scrollTop - before.clientHeight < 24, kept = before?.scrollTop || 0;
    const rerunOpen = box.querySelector(".auto-breakdown-rerun")?.open;
    box.innerHTML = markup(job);
    if (rerunOpen) box.querySelector(".auto-breakdown-rerun")?.setAttribute("open", "");
    const after = box.querySelector("[data-live-text]");
    if (after) after.scrollTop = pinned ? after.scrollHeight : kept;
    bindRun(box, item, job);
    if (!job) return;
    if (active.has(job.status)) timer = setTimeout(() => void paint(item), job.live ? 1500 : 3000);
    else if (job.status !== (box.dataset.last || job.status) && job.video_path) {
      // The video was attached while this page was open: reload the detail so the player shows it.
      box.dataset.last = job.status;
      try { await refresh(); } catch {}
      return;
    }
    box.dataset.last = job.status;
  }

  async function batchCollect(videos) {
    const result = await api("/api/performance/batch-collect", { method: "POST", body: { video_ids: videos.map((v) => v.video_id) } });
    return result.results || [];
  }

  // 「转写口播」 on the 完整文案 card: the soundtrack becomes "[0.00s - 3.60s] 文字" lines, without a breakdown.
  function mountTranscribe(item) {
    const actions = document.querySelector("#transcript-original")?.closest(".section-card")?.querySelector(".transcript-actions");
    if (!actions || actions.querySelector("[data-transcribe]")) return;
    const text = item.current?.transcript?.text || "";
    const lines = text.split(/\r?\n/).filter((line) => line.trim());
    const timed = lines.length > 0 && lines.every((line) => /^\s*\[[^\]]+\]/.test(line));
    if (item.current?.translation_zh && item.current.translation_zh.channel !== "transcript" && !text) return;
    let hasChinese = false;
    try { hasChinese = (window.transcriptComparison || transcriptComparison)(item).some((row) => row.chinese); } catch {}
    // Timed and already paired with Chinese: nothing to offer.
    if (timed && hasChinese) return;
    const mode = timed ? "translate" : text ? "replace" : "new";
    const labels = { new: "转写口播", replace: "重新转写（带时间轴）", translate: "补中文对照" };
    const button = document.createElement("button");
    button.type = "button";
    button.className = "subtle";
    button.dataset.transcribe = mode;
    button.textContent = labels[mode];
    button.title = mode === "translate" ? "用 AI 为这份带时间轴的文案逐句生成中文对照" : "用 ElevenLabs 把原片口播转成逐句带时间的文案，并生成中文对照；不做拆解";
    actions.prepend(button);
    button.addEventListener("click", async () => {
      if (mode === "replace" && !confirm("会用原片音轨重新转写，替换现在这份没有时间轴的文案（旧文案留在修订历史里）。继续吗？")) return;
      button.disabled = true;
      button.textContent = mode === "translate" ? "正在生成中文对照…" : "正在转写…";
      try {
        const result = await api(`/api/cases/${item.case_id}/transcribe`, { method: "POST", body: { replace: mode === "replace", translate_only: mode === "translate" } });
        toast(result.note ? `${mode === "translate" ? "" : "口播已转写。"}${result.note}` : mode === "translate" ? "中文对照已补上。" : "口播已转写，带时间轴和中文对照。", Boolean(result.note));
        try { await refresh(); } catch {}
      } catch (error) {
        toast(error.message, true);
        button.disabled = false;
        button.textContent = labels[mode];
      }
    });
  }

  window.CaseAutoBreakdown = { start, batchCollect, mount: (item) => { mountTranscribe(item); void paint(item).then(() => paintInsights(item)); } };
})();
