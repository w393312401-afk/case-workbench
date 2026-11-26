"use strict";

// 开头库: openings of benchmark videos pushed from the TK 看板 extension, ranked by opening technique with each group's
// medians, and the openings of one technique as cards. Medians of groups under five videos are marked as clues only.
window.HookBank = (() => {
  const MIN = 5;
  const state = { source: "bench", commerce: false, type: "", all: false, data: null, product: "" };
  let sequence = 0;
  const count = value => value == null ? "—" : value >= 10000 ? (value / 10000).toFixed(value >= 100000 ? 0 : 1).replace(/\.0$/, "") + "万" : String(Math.round(value));
  const times = value => value == null ? "—" : (value >= 10 ? String(Math.round(value)) : value.toFixed(1).replace(/\.0$/, "")) + " 倍";
  const pct = value => value == null ? "—" : value.toFixed(1) + "%";
  const median = values => {
    const list = values.filter(v => typeof v === "number" && Number.isFinite(v)).sort((a, b) => a - b);
    if (!list.length) return null;
    const mid = Math.floor(list.length / 2);
    return list.length % 2 ? list[mid] : (list[mid - 1] + list[mid]) / 2;
  };
  function facts(item) {
    const s = item.stats || {}, views = s.views ?? null;
    const at = Date.parse(s.published_at || ""), days = Number.isFinite(at) ? Math.max(1, (Date.now() - at) / 86400000) : null;
    return {
      ratio: views != null && s.followers ? views / s.followers : null,
      likeRate: views ? (s.likes ?? null) == null ? null : s.likes / views * 100 : null,
      perDay: views != null && days ? views / days : null,
    };
  }
  const videoUrl = item => `https://www.tiktok.com/${item.author ? "@" + encodeURIComponent(item.author) : "@"}/video/${encodeURIComponent(item.video_id)}`;
  function shown() {
    const hooks = state.data?.hooks || [];
    return hooks.filter(item => (state.source === "all" || (state.source === "own") === !!item.own) && (!state.commerce || item.stats?.commerce));
  }
  // One row per technique: how many openings, their median plays ÷ followers and like rate, and how often we used it.
  function ranking(list) {
    const own = (state.data?.hooks || []).filter(item => item.own);
    const groups = new Map();
    for (const item of list) groups.set(item.type, [...(groups.get(item.type) || []), item]);
    return [...groups].map(([type, items]) => {
      const f = items.map(facts), mine = own.filter(item => item.type === type);
      return { type, items, count: items.length, ratio: median(f.map(x => x.ratio)), like: median(f.map(x => x.likeRate)),
        own: mine.length, ordered: mine.filter(item => item.orders > 0).length, example: items[0]?.why || "" };
    }).sort((a, b) => (b.ratio ?? -1) - (a.ratio ?? -1) || b.count - a.count);
  }
  function rankTable(rows) {
    const max = Math.max(0, ...rows.map(row => row.ratio ?? 0));
    return `<section class="hook-rank" aria-label="开头手法排行"><div class="hook-rank-scroll"><table>
      <thead><tr><th scope="col">开头手法</th><th scope="col" class="num">条数</th><th scope="col" class="hook-bar-col">中位播粉比</th><th scope="col" class="num">中位点赞率</th><th scope="col">我们用过</th></tr></thead>
      <tbody>${rows.map(row => `<tr class="${row.type === state.type ? "is-current" : ""}">
        <td><button type="button" class="hook-type-pick" data-hook-type="${esc(row.type)}" aria-pressed="${row.type === state.type}">${esc(row.type)}</button>${row.count < MIN ? '<span class="hook-thin">样本少</span>' : ""}${row.example ? `<span class="hook-example">${esc(row.example)}</span>` : ""}</td>
        <td class="num">${row.count}</td>
        <td><span class="hook-bar${row.count < MIN ? " is-thin" : ""}" aria-hidden="true"><i data-width="${max && row.ratio != null ? Math.round(row.ratio / max * 100) : 0}"></i></span><b class="hook-bar-value">${times(row.ratio)}</b></td>
        <td class="num">${pct(row.like)}</td>
        <td class="hook-own">${row.own ? `${row.own} 条 · ${row.ordered} 条出单` : "没用过"}</td></tr>`).join("")}</tbody></table></div></section>`;
  }
  function card(item) {
    const f = facts(item), types = state.data?.types || [];
    return `<article class="hook-card" data-hook-id="${esc(item.video_id)}">
      <div class="hook-card-body">
        ${item.frame_url ? `<img class="hook-frame" src="${esc(item.frame_url)}" alt="第一帧画面" loading="lazy">` : '<div class="hook-frame is-empty">没有画面</div>'}
        <div class="hook-card-text">
          <span class="hook-time">0:00–0:03 · 口播</span>
          ${item.lines.map(line => `<p class="hook-line">${esc(line.en || line.zh)}${line.en && line.zh ? `<span class="hook-zh">${esc(line.zh)}</span>` : ""}</p>`).join("")}
          ${item.screen ? `<p class="hook-screen">屏显：${esc(item.screen)}</p>` : ""}
          ${item.why ? `<p class="hook-why">${esc(item.why)}</p>` : ""}
        </div>
      </div>
      <div class="hook-card-foot">
        <a href="${esc(videoUrl(item))}" target="_blank" rel="noopener noreferrer">${item.author ? "@" + esc(item.author) : "TikTok 视频"}</a>
        <span>播粉比 <b class="is-accent">${times(f.ratio)}</b></span>
        <span>日均 <b>${count(f.perDay)}</b></span>
        ${item.own ? `<span class="hook-own-tag">我们的 · ${item.orders ? item.orders + " 单" : "未出单"}</span>` : ""}
        <span class="hook-card-tools">
          <label class="sr-only" for="hook-type-${esc(item.video_id)}">开头手法</label>
          <select id="hook-type-${esc(item.video_id)}" data-hook-retype="${esc(item.video_id)}">${types.map(type => `<option${type === item.type ? " selected" : ""}>${esc(type)}</option>`).join("")}</select>
          <button type="button" class="subtle" data-hook-copy="${esc(item.video_id)}">复制</button>
          <button type="button" class="secondary" data-hook-write="${esc(item.video_id)}">用这个开头写稿</button>
          <button type="button" class="danger-outline" data-hook-delete="${esc(item.video_id)}" aria-label="从开头库删除">删除</button>
        </span>
      </div></article>`;
  }
  function paint() {
    const root = $("#hook-page");
    if (!root || !state.data) return;
    const list = shown(), rows = ranking(list);
    if (!rows.some(row => row.type === state.type)) state.type = rows[0]?.type || "";
    const current = rows.find(row => row.type === state.type);
    const cards = current ? current.items.slice().sort((a, b) => (facts(b).ratio ?? -1) - (facts(a).ratio ?? -1)) : [];
    const visible = state.all ? cards : cards.slice(0, 12);
    const empty = !(state.data.hooks || []).length;
    root.querySelector("[data-hook-body]").innerHTML = empty
      ? `<div class="hook-empty"><strong>还没有开头</strong><p>在 TK 看板插件的「已保存的结果」页勾选视频，点「存开头到开头库」。存过字幕、AI 拆解或关键帧的视频，开头的口播、开头手法和第一帧会一起带过来。</p></div>`
      : !rows.length ? `<div class="hook-empty"><strong>这个范围里没有开头</strong><p>换一个来源，或关掉「只看挂车」。</p></div>`
      : `${rankTable(rows)}
        <section class="hook-list" aria-label="${esc(state.type)}的开头">
          <div class="hook-list-head"><h2>${esc(state.type)} · ${cards.length} 条 <small>按播粉比排</small></h2>${cards.length > 12 ? `<button type="button" class="subtle" data-hook-all>${state.all ? "收起" : `看全部 ${cards.length} 条`}</button>` : ""}</div>
          <div class="hook-cards">${visible.map(card).join("")}</div>
        </section>`;
    // Widths go through CSSOM: the page's content policy does not allow style attributes in markup.
    root.querySelectorAll("[data-width]").forEach(node => { node.style.width = node.dataset.width + "%"; });
    root.querySelectorAll("[data-hook-source]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.hookSource === state.source)));
  }
  async function load() {
    const n = ++sequence, product = window.WorkbenchShell?.parse().product || "";
    try {
      const data = await api("/api/hooks" + (product ? "?" + new URLSearchParams({ product }) : ""));
      if (n !== sequence) return;
      state.data = data; state.product = product; paint();
    } catch (error) {
      if (n !== sequence) return;
      const body = $("#hook-page [data-hook-body]");
      if (body) body.innerHTML = `<div class="hook-empty"><strong>开头库没读出来</strong><p>${esc(error.message)}</p></div>`;
    }
  }
  async function act(event) {
    const target = event.target.closest("button,select");
    if (!target) return;
    if (target.dataset.hookSource) { state.source = target.dataset.hookSource; state.type = ""; state.all = false; return paint(); }
    if (target.dataset.hookType) { state.type = target.dataset.hookType; state.all = false; return paint(); }
    if (target.hasAttribute("data-hook-all")) { state.all = !state.all; return paint(); }
    const item = id => (state.data?.hooks || []).find(hook => hook.video_id === id);
    if (target.dataset.hookWrite) {
      const hook = item(target.dataset.hookWrite);
      if (!window.CaseAI?.startFromHook) { toast("写稿窗口还没加载好，请稍后再试。", true); return; }
      await window.CaseAI.startFromHook(hook);
      return;
    }
    if (target.dataset.hookCopy) {
      const hook = item(target.dataset.hookCopy);
      const text = [...hook.lines.map(line => [line.en, line.zh].filter(Boolean).join("｜")), hook.screen ? "屏显：" + hook.screen : ""].filter(Boolean).join("\n");
      try { await navigator.clipboard.writeText(text); toast("已复制开头"); } catch { toast("未能复制，请手动选择文字复制。", true); }
      return;
    }
    if (target.dataset.hookDelete) {
      const hook = item(target.dataset.hookDelete);
      if (!confirm(`从开头库删除 @${hook?.author || ""} 这条开头？插件里保存的结果不受影响，之后可以重新存。`)) return;
      try { await api("/api/hooks/delete", { method: "POST", body: { video_id: target.dataset.hookDelete } }); toast("已删除"); await load(); }
      catch (error) { toast(error.message, true); }
    }
  }
  async function retype(event) {
    const select = event.target.closest("[data-hook-retype]");
    if (!select) return;
    try { await api("/api/hooks/type", { method: "POST", body: { video_id: select.dataset.hookRetype, type: select.value } }); toast("已改成「" + select.value + "」"); await load(); }
    catch (error) { toast(error.message, true); await load(); }
  }
  async function route() {
    $("#content").innerHTML = `<div id="hook-page">
      <div class="page-heading hook-heading"><div><h1>开头库</h1><p>每个开头带它那条视频的表现。按开头手法看中位数，少于 ${MIN} 条的组只当线索。播粉比 = 播放 ÷ 粉丝。</p></div>
        <div class="hook-filters">
          <div class="hook-source" role="group" aria-label="来源">
            <button type="button" data-hook-source="bench" aria-pressed="true">对标</button>
            <button type="button" data-hook-source="own" aria-pressed="false" title="视频 ID 出现在带货数据里的">我们的带货视频</button>
            <button type="button" data-hook-source="all" aria-pressed="false">全部</button>
          </div>
          <label class="hook-commerce"><input type="checkbox" data-hook-commerce${state.commerce ? " checked" : ""}>只看挂车</label>
        </div></div>
      <div data-hook-body><p class="hook-loading">正在读取开头库…</p></div></div>`;
    const root = $("#hook-page");
    root.addEventListener("click", act);
    root.addEventListener("change", event => {
      if (event.target.matches("[data-hook-commerce]")) { state.commerce = event.target.checked; state.type = ""; return paint(); }
      retype(event);
    });
    await load();
  }
  return { route };
})();
