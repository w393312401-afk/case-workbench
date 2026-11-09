"use strict";

// 「从 Apify 抓评论」 on the comment page: pick a date range (suggested: from the newest comment the
// library already has), check the platforms and their targets, start, and watch each platform finish.
window.ReviewScrape = (() => {
  const labels = {queued:"排队中", running:"Apify 抓取中…", done:"完成", failed:"没完成"};
  let timer = null, sequence = 0;

  function platformRow(p, job) {
    const run = job?.platforms?.[p.platform];
    const status = run ? `<span class="scrape-status" data-state="${esc(run.status)}">${labels[run.status] || esc(run.status)}</span>${run.status === "done" || run.fetched != null ? ` 抓到 ${run.fetched ?? 0} 条，日期内 ${run.kept ?? 0} 条${run.cost_usd != null ? ` · 花费 $${run.cost_usd}` : ""}` : ""}${run.error ? `<small class="scrape-error">${esc(run.error)}</small>` : ""}` : "";
    return `<li class="scrape-platform"><label><input type="checkbox" data-scrape-platform="${esc(p.platform)}" ${p.targets.length ? "checked" : ""}> <b>${esc(p.label)}</b></label>
      <span class="scrape-meta">库里最新 ${esc(p.latest || "无")} · 最多 ${p.limit} 条</span>${status ? `<span class="scrape-run">${status}</span>` : ""}
      <details><summary>${p.targets.length} 个${esc(p.target_label)}${p.edited ? "（改过）" : ""}</summary><textarea rows="4" data-scrape-targets="${esc(p.platform)}" aria-label="${esc(p.label)}的抓取目标，一行一个">${esc(p.targets.join("\n"))}</textarea></details></li>`;
  }

  function markup(data) {
    const job = data.job, busy = data.busy;
    const token = data.token ? "" : `<p class="data-warning">还没有 Apify 令牌：到 <a href="#settings">设置 → 密钥与令牌</a> 填入，保存后回来点「开始抓取」。</p>`;
    const done = job && job.status === "done" ? `<p class="context-note">${esc(job.start)} 至 ${esc(job.end)} 抓完${job.added != null ? `，评论库新增 ${job.added} 条` : ""}。${job.sync_error ? esc(job.sync_error) : ""}</p>` : "";
    return `${token}<p class="rev-muted">每个平台从库里已有评论的来源接着抓，只保留评论日期落在下面范围里的；推荐开始日期是库里最新一条评论那天（各平台取最早的那个，保证不漏）。Apify 按抓到的条数计费，日期外的也算。</p>
      <div class="scrape-range"><label>开始 <input type="date" id="scrape-start" value="${esc(job && busy ? job.start : data.recommended_start)}" max="${esc(data.today)}"></label><label>结束 <input type="date" id="scrape-end" value="${esc(job && busy ? job.end : data.today)}" max="${esc(data.today)}"></label><button type="button" class="subtle" id="scrape-recommend">用推荐日期</button></div>
      <ul class="scrape-platforms">${data.platforms.map(p => platformRow(p, job)).join("")}</ul>${done}
      <div class="scrape-actions">${busy ? '<button type="button" class="secondary" id="scrape-stop">停止</button>' : `<button type="button" class="primary" id="scrape-start-run" ${data.token ? "" : "disabled"}>开始抓取</button>`}<span id="scrape-note" role="status"></span></div>`;
  }

  async function mount(box, product, onDone, wasBusy = false) {
    const mine = ++sequence;
    clearTimeout(timer);
    let data;
    try {
      data = await api(`/api/reviews/scrape?${new URLSearchParams({product})}`);
    } catch (error) {
      box.innerHTML = `<p class="data-warning">${esc(error.message)}</p>`;
      return;
    }
    if (mine !== sequence || !box.isConnected) return;
    box.innerHTML = markup(data);
    // A job that just finished added comments: let the page reload its counts and list.
    if (wasBusy && !data.busy && data.job?.added) onDone?.(data.job);
    const note = text => { const el = box.querySelector("#scrape-note"); if (el) el.textContent = text; };
    box.querySelector("#scrape-recommend").onclick = () => { box.querySelector("#scrape-start").value = data.recommended_start; box.querySelector("#scrape-end").value = data.today; };
    const startButton = box.querySelector("#scrape-start-run");
    if (startButton) startButton.onclick = async () => {
      const platforms = {};
      box.querySelectorAll("[data-scrape-platform]").forEach(input => {
        if (!input.checked) return;
        const name = input.dataset.scrapePlatform;
        platforms[name] = {targets: box.querySelector(`[data-scrape-targets="${name}"]`).value.split("\n").map(line => line.trim()).filter(Boolean)};
      });
      startButton.disabled = true;
      try {
        await api("/api/reviews/scrape", {method: "POST", body: {product, start: box.querySelector("#scrape-start").value, end: box.querySelector("#scrape-end").value, platforms}});
        mount(box, product, onDone);
      } catch (error) {
        startButton.disabled = false;
        note(error.message);
      }
    };
    const stop = box.querySelector("#scrape-stop");
    if (stop) stop.onclick = async () => {
      stop.disabled = true;
      try { await api("/api/reviews/scrape/stop", {method: "POST", body: {product}}); } catch (error) { note(error.message); }
      mount(box, product, onDone);
    };
    if (data.busy) {
      timer = setTimeout(() => { if (box.isConnected) mount(box, product, onDone, true); }, 5000);
    }
  }

  return {mount};
})();
