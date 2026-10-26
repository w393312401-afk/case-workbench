"use strict";

// TK 看板 cards retain their production state separately from their automatic direction association.
// Existing card fields and production records are linked by the server, without another selection form.
window.TKCandidates = (() => {
  let sequence = 0;
  const count = value => value == null ? "—" : value >= 10000 ? (value / 10000).toFixed(value >= 100000 ? 0 : 1) + "万" : String(value);
  const day = value => String(value || "").slice(0, 10).replaceAll("-", "/");
  const statusNames = { new: "待处理", adopted: "已处理", dismissed: "已忽略", writing: "写稿中", produced: "已进制作单" };
  const taskHash = (id, product) => window.WorkbenchShell?.url(`#ai/${encodeURIComponent(id)}`, product) || `#ai/${encodeURIComponent(id)}`;
  const jobHash = (id, product) => window.WorkbenchShell?.url(`#production/${encodeURIComponent(id)}`, product) || `#production/${encodeURIComponent(id)}`;
  const directionHash = (id, product) => window.WorkbenchShell?.url(`#directions/${encodeURIComponent(id)}`, product) || `#directions/${encodeURIComponent(id)}`;
  const ready = item => Boolean(item.direction_id && item.job_id && !item.direction_error && !item.production_error);
  const completed = item => !["writing", "dismissed"].includes(item.status) && ready(item);
  const handled = item => item.status === "dismissed" || completed(item) || (item.status === "adopted" && !item.direction_error && !item.production_error);

  function script(direction) {
    const pair = (label, value) => value && (value.en || value.zh) ? `<p><b>${label}</b>${esc(value.en)}${value.zh ? `<span class="tk-cand-zh">${esc(value.zh)}</span>` : ""}</p>` : "";
    return `${direction.hook_visual ? `<p><b>开头画面</b>${esc(direction.hook_visual)}</p>` : ""}${pair("开头屏显", direction.hook_text)}
      ${direction.lines.length ? `<ol class="tk-cand-lines">${direction.lines.map(line => `<li>${esc(line.en)}${line.zh ? `<span class="tk-cand-zh">${esc(line.zh)}</span>` : ""}</li>`).join("")}</ol>` : ""}
      ${pair("收口", direction.cta)}${direction.payback ? `<p><b>收口思路</b>${esc(direction.payback)}</p>` : ""}
      ${direction.why ? `<p><b>为什么这样拍</b>${esc(direction.why)}</p>` : ""}
      ${direction.check.length ? `<p><b>发布前核对</b></p><ul>${direction.check.map(item => `<li>${esc(item)}</li>`).join("")}</ul>` : ""}`;
  }

  function card(item) {
    const d = item.direction, ref = item.reference;
    const id = esc(item.candidate_id), job = item.job_id ? `<a class="secondary" href="${esc(jobHash(item.job_id, item.product))}">打开制作单</a>` : "";
    const produce = `<button type="button" class="primary" data-tk-produce="${id}" title="用这张方向卡的口播和中文直接建视频制作单，已有方向信息自动带入">直接建制作单</button>`;
    const association = `${item.direction_id ? `<span class="tk-cand-state">已归入 ${esc(item.direction_id)}</span><a class="secondary" href="${esc(directionHash(item.direction_id, item.product))}">查看方向</a>` : ""}${item.direction_error ? `<span class="data-warning" role="status">${esc(item.direction_error)}</span><button type="button" class="secondary" data-tk-associate="${id}">重新关联</button>` : ""}${item.production_error ? `<span class="data-warning" role="status">${esc(item.production_error)}</span>` : ""}`;
    const actions = completed(item)
      ? `<span class="tk-cand-state">已处理</span>${job}`
      : item.status === "new"
      ? `${job || produce}<button type="button" class="secondary" data-tk-status="dismissed" data-tk-id="${id}">忽略</button>`
      : item.status === "writing"
        ? `<span class="tk-cand-state">正在按写稿技能补全脚本，写完自动建制作单</span><a class="secondary" href="${esc(taskHash(item.writing_task_id, item.product))}">查看写稿</a><button type="button" class="subtle" data-tk-produce="${id}" title="写稿已结束但还没建单时，用这一轮结果建制作单；写稿失败时重新写">刷新</button>`
        : item.status === "produced"
        ? `<span class="tk-cand-state">已进制作单</span>${job}`
        : `<span class="tk-cand-state">${esc(statusNames[item.status])}</span>${job || (item.status === "adopted" ? produce : "")}<button type="button" class="subtle" data-tk-status="new" data-tk-id="${id}">${item.status === "dismissed" && ready(item) ? "取消忽略" : "移回待处理"}</button>`;
    return `<li class="tk-cand${handled(item) ? " done" : ""}">
      <div class="tk-cand-head"><strong>${esc(d.code || "方向")}</strong>${d.kind ? `<span class="tk-cand-kind">${esc(d.kind)}</span>` : ""}<span>${esc(d.name)}</span></div>
      <p class="tk-cand-meta">参考 <a href="${esc(ref.url)}" target="_blank" rel="noopener noreferrer">${ref.author ? "@" + esc(ref.author) : "TikTok 视频"}</a>${ref.title ? " · " + esc(ref.title.slice(0, 60)) : ""} · 播放 ${count(ref.views)} · 存于 ${esc(day(item.saved_at))}</p>
      ${d.audience ? `<p class="tk-cand-meta">拍给：${esc(d.audience)}${d.compare ? "　对比：" + esc(d.compare) : ""}</p>` : ""}
      ${d.chain.length ? `<p class="tk-cand-chain">${d.chain.map(esc).join(" → ")}</p>` : ""}
      ${item.ask ? `<p class="tk-cand-meta">当时的要求：${esc(item.ask)}</p>` : ""}
      <details class="tk-cand-script"><summary>口播稿与依据</summary>${script(d)}</details>
      <div class="tk-cand-actions">${actions}${association}</div></li>`;
  }

  async function mount(slot, product, catalog) {
    if (!slot || !product) return;
    const run = ++sequence;
    let data;
    try { data = await api("/api/tk-candidates?" + new URLSearchParams({ product })); } catch (error) { return; }
    if (run !== sequence || !slot.isConnected) return;
    let showDone = slot.dataset.showDone === "1";
    const render = () => {
      // Direction membership and an existing order settle the candidate inbox.
      // Running work and unresolved association errors remain visible.
      const pending = data.candidates.filter(item => !handled(item)), done = data.candidates.filter(handled);
      const writing = pending.filter(item => item.status === "writing").length;
      const processed = done.filter(item => item.status !== "dismissed").length;
      const dismissed = done.length - processed;
      if (!data.candidates.length) { slot.innerHTML = ""; return; }
      slot.innerHTML = `<section class="dir-group tk-candidates"><h2>插件存来的候选方向<small>TK 看板 · ${pending.length - writing} 条待处理${writing ? ` · ${writing} 条写稿中` : ""}</small></h2>
        ${pending.length ? `<ul class="tk-cand-list">${pending.map(card).join("")}</ul>` : `<p class="context-note">没有待处理的候选方向。</p>`}
        ${done.length ? `<p class="tk-cand-footer">已处理 ${processed} · 已忽略 ${dismissed} · <button type="button" class="subtle" data-tk-toggle>${showDone ? "收起已处理" : "显示已处理"}</button></p>${showDone ? `<ul class="tk-cand-list">${done.map(card).join("")}</ul>` : ""}` : ""}</section>`;
      slot.querySelector("[data-tk-toggle]")?.addEventListener("click", () => { showDone = !showDone; slot.dataset.showDone = showDone ? "1" : ""; render(); });
      slot.querySelectorAll("[data-tk-status]").forEach(button => button.onclick = () => void setStatus(button.dataset.tkId, button.dataset.tkStatus));
      slot.querySelectorAll("[data-tk-produce]").forEach(button => button.onclick = () => void produce(button));
      slot.querySelectorAll("[data-tk-associate]").forEach(button => button.onclick = () => void associate(button));
    };
    async function setStatus(id, status) {
      try {
        await api("/api/tk-candidates/status", { method: "POST", body: { candidate_id: id, status } });
        if (status === "dismissed") toast("已忽略，可在「显示已处理」里移回。");
        await mount(slot, product, catalog);
      } catch (error) { toast(error.message, true); }
    }
    async function produce(button) {
      button.disabled = true;
      try {
        const result = await api("/api/tk-candidates/produce", { method: "POST", body: { candidate_id: button.dataset.tkProduce } });
        if (result.writing) {
          toast(result.writing.status === "queued" ? "已开始按写稿技能补全这条脚本，写完会自动建制作单。" : "这条脚本还在写，写完会自动建制作单。");
          await mount(slot, product, catalog);
          return;
        }
        toast(result.reused ? "这条候选之前已建过制作单，已打开。" : "已按方向卡原文建好视频制作单。");
        location.hash = jobHash(result.job.id, result.job.product);
      } catch (error) { button.disabled = false; toast(error.message, true); }
    }
    async function associate(button) {
      button.disabled = true;
      try {
        const result = await api("/api/tk-candidates/associate", { method: "POST", body: { candidate_id: button.dataset.tkAssociate } });
        await mount(slot, product, catalog);
        const error = result.candidate?.direction_error || result.candidate?.production_error;
        toast(error || `已自动归入${result.candidate?.direction_id ? " " + result.candidate.direction_id : "对应方向"}，已有信息和制作单关联已带上。`, Boolean(error));
      } catch (error) { button.disabled = false; toast(error.message, true); }
    }
    render();
  }
  return { mount };
})();
