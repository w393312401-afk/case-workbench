'use strict';

// Platform campaigns (活动日历): shown on the data board, editable, and quoted by the review when a video's
// observation window touches one. Dates are Pacific Time calendar days, as the platform publishes them.
window.Campaigns = (() => {
  const trust = {
    official: ['官方原文', '来自 TikTok Shop 官方页面'],
    media: ['媒体一致', '多家媒体报道一致，尚无官方原文'],
    disputed: ['说法不一', '不同来源给出不同日期，以 Seller Center 活动卡为准'],
    planning: ['待核对', '只有第三方规划日历，没有官方来源；请对照 Seller Center'],
    manual: ['人工登记', '由你自己登记'],
  };
  const state = {data: null, loading: null, error: '', showPast: false};
  const selectionKey = 'workbench-selected-campaign';
  let selectedCampaign;
  function select(id) {
    const value = typeof id === 'string' ? id : '';
    const changed = selectedCampaign !== value;
    selectedCampaign = value;
    try { localStorage.setItem(selectionKey, value); } catch { /* Keep the selection for this session. */ }
    return changed;
  }
  function selection() {
    if (selectedCampaign === undefined) {
      selectedCampaign = null;
      try {
        const saved = localStorage.getItem(selectionKey);
        if (saved !== null) selectedCampaign = saved;
        else {
          // Adopt the last active view's old preset once; stale page memories cannot override it later.
          const filters = JSON.parse(localStorage.getItem('workbench-video-data-filters') || '{}');
          const previous = filters[filters.tab === 'list' ? 'campaign' : 'board_campaign'];
          if (typeof previous === 'string') select(previous);
        }
      } catch { /* The calendar still works without browser storage. */ }
    }
    // Failed calendar reads leave the choice intact. Only a successful read can prove it was deleted.
    if (selectedCampaign && state.data && !state.data.campaigns.some(row => row.id === selectedCampaign)) select('');
    return selectedCampaign;
  }
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  const md = day => `${Number(day.slice(5, 7))}/${Number(day.slice(8, 10))}`;
  const range = row => row.start === row.end ? md(row.start) : `${md(row.start)} – ${md(row.end)}`;
  const today = () => {const now = new Date();return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;};
  const addDays = (day, count) => {const value = new Date(day + 'T00:00:00Z');value.setUTCDate(value.getUTCDate() + count);return value.toISOString().slice(0, 10);};
  const daysBetween = (from, to) => Math.round((new Date(to + 'T00:00:00Z') - new Date(from + 'T00:00:00Z')) / 86400000);
  const money = value => '$' + Math.round(value).toLocaleString('en-US');
  const phase = (row, now = today()) => row.end < now ? 'past' : row.start > now ? 'next' : 'live';

  async function load(force = false) {
    if (state.loading) return state.loading;
    if (state.data && !force) return state.data;
    state.loading = api('/api/campaigns').then(data => {state.data = data;state.error = '';return data;},
      error => {state.error = error.message || '活动日历暂时读不到。';return null;}).finally(() => {state.loading = null;});
    return state.loading;
  }
  const campaigns = () => state.data?.campaigns || [];

  // The observation window of a video, or any day range, against the calendar.
  const overlapping = (start, end) => !start || !end ? [] : campaigns().filter(row => row.start <= end && row.end >= start);

  // The page lends its loaded sales data, so a campaign can be read against one product's own weeks.
  let weeklyCache = null;
  const context = () => {try {return window.CasePerformance?.campaignContext?.() || null;} catch {return null;}};

  // Weekly figures of one product (or all), from the per-video weekly rows: Monday -> {gmv, orders, items}.
  function weeklyTotals(ctx) {
    const a = ctx?.analytics;
    if (!a?.weekly?.length || !Array.isArray(a.videos) || !Array.isArray(ctx.weeks)) return null;
    const key = ctx.product || '';
    if (weeklyCache?.analytics === a && weeklyCache.key === key) return weeklyCache.value;
    const ids = new Set((ctx.videos || []).filter(video => !ctx.product || ctx.matches(video.product)).map(video => String(video.video_id)));
    const totals = new Map();
    for (const row of a.weekly) {
      if (!ids.has(String(a.videos[row[0]]?.[0]))) continue;
      const entry = totals.get(row[1]) || {gmv: 0, orders: 0, items: 0};
      entry.orders += row[5] || 0;entry.items += row[6] || 0;entry.gmv += row[7] || 0;
      totals.set(row[1], entry);
    }
    const value = {totals, complete: new Set(ctx.weeks.filter(week => week.complete).map(week => week.week)), videos: ids.size};
    weeklyCache = {analytics: a, key, value};
    return value;
  }
  const weekOverlap = (week, other) => {
    const start = week > other.start ? week : other.start, endWeek = addDays(week, 6), end = endWeek < other.end ? endWeek : other.end;
    return end < start ? 0 : daysBetween(start, end) + 1;
  };
  // One product's weekly figures in the weeks mostly inside the campaign (4+ of 7 days) against the up-to-4 nearest
  // earlier weeks with no campaign day at all. Weeks with a gap in the exports are left out of both sides.
  function weeklyImpact(row, ctx = context()) {
    // Which weeks are quiet depends on the whole calendar; without it every week would look quiet.
    if (!state.data) return null;
    const data = weeklyTotals(ctx);
    if (!data || !data.videos) return null;
    const zero = {gmv: 0, orders: 0, items: 0};
    const inside = [...data.complete].filter(week => weekOverlap(week, row) >= 4).sort();
    if (!inside.length) return {missing: 'weeks'};
    const base = [];
    for (let week = addDays(inside[0], -7), step = 0; step < 6 && base.length < 4; week = addDays(week, -7), step++) {
      if (data.complete.has(week) && !campaigns().some(other => weekOverlap(week, other) > 0)) base.push(week);
    }
    if (base.length < 2) return {missing: 'baseline', weeks: inside.length, baseline: base.length};
    const mean = (weeks, field) => weeks.reduce((sum, week) => sum + (data.totals.get(week) || zero)[field], 0) / weeks.length;
    return {gmv: [mean(inside, 'gmv'), mean(base, 'gmv')], orders: [mean(inside, 'orders'), mean(base, 'orders')], weeks: inside.length, baseline: base.length};
  }
  const times = (pair, digits = 2) => pair[1] > 0 ? `×${(pair[0] / pair[1]).toFixed(digits)}` : '—';
  function weeklyLine(row, ctx) {
    const result = phase(row) === 'next' ? null : weeklyImpact(row, ctx);
    if (!result) return '';
    const label = ctx.product || '全部产品';
    if (result.missing === 'weeks') return '';
    if (result.missing === 'baseline') return `<p class="campaign-impact is-muted">${escape(label)}：此前没有足够的无活动整周（只有 ${result.baseline} 周），按周对比不了。</p>`;
    return `<p class="campaign-impact"><strong>${escape(label)}</strong> 活动周（${result.weeks} 周）周均 GMV <strong>${money(result.gmv[0])}</strong>，此前 ${result.baseline} 个无活动周 ${money(result.gmv[1])}，<strong>${times(result.gmv)}</strong>；周均订单 ${result.orders[0].toFixed(1)} 对 ${result.orders[1].toFixed(1)}（${times(result.orders)}）。<small>按周对比，取活动日占 4 天以上的周；高客单品单量少，几单的差别就会拉动倍数，只作参考。</small></p>`;
  }

  // Shop-wide daily totals (every product) around a campaign: the days inside against the 14 days before it,
  // leaving out days that sat in another campaign. Needs most days of both stretches, else says so.
  function impact(row, daily) {
    if (!Array.isArray(daily) || !daily.length) return null;
    const byDay = new Map(daily.map(entry => [entry.date, entry]));
    const inside = [], before = [];
    for (let day = row.start; day <= row.end; day = addDays(day, 1)) if (byDay.has(day)) inside.push(byDay.get(day));
    const length = daysBetween(row.start, row.end) + 1;
    for (let day = addDays(row.start, -14); day < row.start; day = addDays(day, 1)) {
      if (byDay.has(day) && !campaigns().some(other => other.id !== row.id && other.start <= day && other.end >= day)) before.push(byDay.get(day));
    }
    if (inside.length < Math.ceil(length * .6) || before.length < 7) return {missing: true, inside: inside.length, length, before: before.length};
    const mean = (rows, key) => rows.reduce((sum, entry) => sum + (entry[key] || 0), 0) / rows.length;
    return {gmv: [mean(inside, 'gmv'), mean(before, 'gmv')], orders: [mean(inside, 'orders'), mean(before, 'orders')], days: inside.length, length, before: before.length};
  }
  function impactLine(row, daily, quiet = false) {
    if (phase(row) === 'next') return '';
    const result = impact(row, daily);
    if (!result) return '';
    // Shop daily totals only reach back so far; a campaign before them simply has no figure to show.
    if (result.missing && (!result.inside || quiet)) return '';
    if (result.missing) return `<p class="campaign-impact is-muted">店铺日度数据只覆盖了 ${result.inside}/${result.length} 天（对照期 ${result.before} 天），算不出活动期影响。</p>`;
    const partial = phase(row) === 'live' ? '（进行中，已过 ' + result.days + ' 天）' : '';
    return `<p class="campaign-impact is-shop">全店（所有产品）日度：活动期日均 GMV ${money(result.gmv[0])}，活动前 14 天 ${money(result.gmv[1])}，${times(result.gmv)}；日均订单 ${result.orders[0].toFixed(1)} 对 ${result.orders[1].toFixed(1)}（${times(result.orders)}）${partial}。<small>对照期已剔除其他活动日。</small></p>`;
  }

  function rowMarkup(row, daily) {
    const kind = phase(row), now = today(), weekly = weeklyLine(row, context());
    const [label, hint] = trust[row.confidence] || trust.manual;
    const status = kind === 'live' ? `进行中 · 还剩 ${daysBetween(now, row.end)} 天` : kind === 'next' ? `${daysBetween(now, row.start)} 天后开始` : '已结束';
    const source = row.source_url ? `<a href="${escape(row.source_url)}" target="_blank" rel="noopener noreferrer">${escape(row.source_label || '来源')} ↗</a>` : escape(row.source_label || '');
    return `<li class="campaign-row is-${kind}" data-campaign-id="${escape(row.id)}">
      <div class="campaign-main"><strong>${escape(row.name)}</strong><span class="campaign-dates">${escape(row.start.slice(0, 4))} · ${range(row)}</span>
        <span class="campaign-phase">${status}</span><span class="campaign-trust is-${escape(row.confidence)}" title="${escape(hint)}">${label}${row.verified ? ' · 已核对' : ''}</span></div>
      ${row.note ? `<p class="campaign-note">${escape(row.note)}</p>` : ''}${weekly}${impactLine(row, daily, Boolean(weekly))}
      <div class="campaign-meta">${source ? `<span>${source}</span>` : ''}<button type="button" class="subtle compact" data-campaign-edit="${escape(row.id)}">编辑</button></div></li>`;
  }

  function card() {
    const shopDaily = context()?.analytics?.shop_daily;
    if (!state.data) return `<section class="section-card perf-trend-card perf-campaigns" id="perf-campaigns"><div class="perf-card-head"><h2>平台活动日历</h2></div>${state.error
      ? `<p class="ai-error" role="alert">${escape(state.error)} <button type="button" class="subtle compact" data-campaign-retry>重试</button></p>` : '<p class="context-note" role="status">正在读取活动日历…</p>'}</section>`;
    const rows = campaigns(), now = today();
    const upcoming = rows.filter(row => row.end >= now), past = rows.filter(row => row.end < now).reverse();
    const doubtful = rows.filter(row => ['planning', 'disputed'].includes(row.confidence) && !row.verified).length;
    const pending = rows.filter(row => row.check && !row.verified).length;
    return `<section class="section-card perf-trend-card perf-campaigns" id="perf-campaigns">
      <div class="perf-card-head"><h2>平台活动日历 · 美区 TikTok Shop</h2><span>日期为太平洋时间 · 高客单品销量受活动影响大，复盘时会自动对照</span></div>
      ${pending ? `<p class="context-note campaign-warning">${doubtful ? `有 ${doubtful} 个活动的日期只来自第三方日历或说法不一，` : ''}共 ${pending} 项待对照 Seller Center 核对；核对后复盘才可靠。<button type="button" class="secondary compact" data-campaign-checklist>打开待核对清单</button></p>` : ''}
      <ul class="campaign-list">${upcoming.length ? upcoming.map(row => rowMarkup(row, shopDaily)).join('') : '<li class="campaign-empty">今年已没有登记的后续活动。</li>'}</ul>
      <details class="campaign-past" ${state.showPast ? 'open' : ''}><summary>已结束的活动（${past.length}）</summary><ul class="campaign-list">${past.map(row => rowMarkup(row, shopDaily)).join('')}</ul></details>
      <div class="campaign-actions"><button type="button" class="secondary compact" data-campaign-add>新增活动</button></div></section>`;
  }

  function dialog(row, onChange) {
    const editing = Boolean(row);
    const value = row || {name: '', start: today(), end: today(), confidence: 'manual', source_label: '', source_url: '', note: '', verified: false};
    const modal = document.createElement('dialog');
    modal.className = 'script-editor-dialog production-dialog campaign-dialog';
    modal.innerHTML = `<form><div class="dialog-heading"><h2>${editing ? '编辑活动' : '新增活动'}</h2><button type="button" class="icon-button" data-close aria-label="关闭">${typeof icon === 'function' ? icon('close') : '×'}</button></div>
      <div class="script-editor-body">
        <label class="field"><span>活动名称</span><input name="name" maxlength="80" required value="${escape(value.name)}"></label>
        <div class="form-row"><label class="field"><span>开始日期（太平洋时间）</span><input type="date" name="start" required value="${escape(value.start)}"></label>
          <label class="field"><span>结束日期（含当天）</span><input type="date" name="end" required value="${escape(value.end)}"></label></div>
        <label class="field"><span>日期可信度</span><select name="confidence">${Object.entries(trust).map(([key, [label]]) => `<option value="${key}" ${key === value.confidence ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
        <label class="field campaign-verified"><input type="checkbox" name="verified" ${value.verified ? 'checked' : ''}> 我已对照 Seller Center / Creator Center 的活动卡核对过这组日期</label>
        <label class="field"><span>来源说明</span><input name="source_label" maxlength="120" value="${escape(value.source_label)}"></label>
        <label class="field"><span>来源链接（可不填）</span><input name="source_url" maxlength="500" inputmode="url" placeholder="https://…" value="${escape(value.source_url)}"></label>
        <label class="field"><span>备注</span><textarea name="note" maxlength="1000" rows="3">${escape(value.note)}</textarea></label>
        <p class="ai-error" role="alert" data-error hidden></p></div>
      <div class="dialog-footer"><span>${editing && value.builtin ? '内置活动：删除后可在下次点「恢复默认」找回' : ''}</span><div>
        ${editing ? `<button type="button" class="subtle flow-delete-archived" data-delete>删除</button>` : ''}${editing && value.builtin && value.edited ? '<button type="button" class="subtle" data-restore>恢复默认</button>' : ''}
        <button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary" data-save>保存</button></div></div></form>`;
    document.body.append(modal);
    const close = () => {modal.close();modal.remove();};
    modal.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
    modal.addEventListener('cancel', event => {event.preventDefault();close();});
    const fail = message => {const node = modal.querySelector('[data-error]');node.hidden = false;node.textContent = message;};
    async function submit(body) {
      modal.querySelectorAll('button').forEach(button => {button.disabled = true;});
      try {
        state.data = await api('/api/campaigns', {method: 'POST', body: {...body, revision: state.data.revision}});
        close();
        onChange?.();
      } catch (error) {
        modal.querySelectorAll('button').forEach(button => {button.disabled = false;});
        if (error?.status === 409) await load(true).catch(() => null);
        fail(error?.status === 409 ? `${error.message} 你填的内容还在，请关闭后重新打开再保存。` : (error.message || '没能保存，请重试。'));
      }
    }
    modal.querySelector('form').onsubmit = event => {
      event.preventDefault();
      const form = new FormData(event.currentTarget);
      submit({action: 'save', campaign: {...(editing ? {id: value.id} : {}), name: form.get('name'), start: form.get('start'), end: form.get('end'),
        confidence: form.get('confidence'), source_label: form.get('source_label'), source_url: String(form.get('source_url') || '').trim(),
        note: form.get('note'), verified: form.get('verified') === 'on'}});
    };
    modal.querySelector('[data-delete]')?.addEventListener('click', () => {if (confirm(`删除「${value.name}」？`)) submit({action: 'delete', id: value.id});});
    modal.querySelector('[data-restore]')?.addEventListener('click', () => submit({action: 'restore', id: value.id}));
    modal.showModal();
    modal.querySelector('[name=name]').focus();
  }

  // ---- the checklist: what to confirm against the platform, most consequential first --------------------------
  // How many of the product's videos sit in a campaign's days: those reviews read the dates written here.
  function affectedVideos(row) {
    const ctx = context();
    if (!ctx?.videos) return null;
    return ctx.videos.filter(video => (!ctx.product || ctx.matches(video.product)) && video.posted_on && video.window_end
      && video.posted_on <= row.end && video.window_end >= row.start).length;
  }
  function checklist() {
    const now = today();
    const items = campaigns().filter(row => row.check && !row.verified).map(row => ({row, videos: affectedVideos(row)}));
    // Dates that already shape reviews first; then what is still ahead; then the rest, newest first.
    const rank = ({row, videos}) => videos > 0 ? 0 : row.end >= now ? 1 : 2;
    return items.sort((a, b) => rank(a) - rank(b) || (rank(a) === 0 ? b.videos - a.videos : rank(a) === 1 ? a.row.start.localeCompare(b.row.start) : b.row.start.localeCompare(a.row.start)));
  }
  function itemText({row, videos}, index) {
    const label = context()?.product || '全部产品';
    return [`${index + 1}. ${row.name}  现填 ${range(row)}（太平洋时间）· ${(trust[row.confidence] || trust.manual)[0]}`, `   要核对：${row.check}`,
      videos == null ? '' : `   影响：${videos ? `${label} 有 ${videos} 条视频的观察窗落在这段时间，复盘按这里的日期判断。` : `目前没有 ${label} 视频的观察窗落在这段时间。`}`].filter(Boolean).join('\n');
  }
  const GUIDE = '到 Seller Center（卖家后台）的「营销 / Marketing → 活动 / Campaigns」里找同名活动卡，核对起止日期；已结束的活动卡看不到时，查 Creator Center 的活动日历或当时收到的活动通知。路径以你后台的实际界面为准。后台显示北京时间时：美国夏令时（3/8 至 11/1）减 15 小时，11/1 之后减 16 小时。';

  async function post(body) {
    try {
      state.data = await api('/api/campaigns', {method: 'POST', body: {...body, revision: state.data.revision}});
      return true;
    } catch (error) {
      if (error?.status === 409) await load(true).catch(() => null);
      toast(error?.status === 409 ? `${error.message} 已重新读取，请再点一次。` : (error.message || '没能保存，请重试。'), true);
      return false;
    }
  }
  function checklistDialog(onChange) {
    const modal = document.createElement('dialog');
    modal.className = 'script-editor-dialog production-dialog campaign-check-dialog';
    document.body.append(modal);
    const close = () => {modal.close();modal.remove();};
    modal.addEventListener('cancel', event => {event.preventDefault();close();});
    function paint() {
      const items = checklist();
      modal.innerHTML = `<form method="dialog"><div class="dialog-heading"><h2>活动日期待核对清单 <small>${items.length} 项</small></h2><button type="button" class="icon-button" data-close aria-label="关闭">${typeof icon === 'function' ? icon('close') : '×'}</button></div>
        <div class="script-editor-body"><p class="context-note">${escape(GUIDE)}</p>
        ${items.length ? `<ol class="campaign-check-list">${items.map(({row, videos}) => {
          const label = context()?.product || '全部产品';
          const [trustLabel, hint] = trust[row.confidence] || trust.manual;
          return `<li class="campaign-check" data-check-id="${escape(row.id)}">
            <div class="campaign-main"><strong>${escape(row.name)}</strong><span class="campaign-dates">现填 ${range(row)}</span><span class="campaign-trust is-${escape(row.confidence)}" title="${escape(hint)}">${trustLabel}</span></div>
            <p class="campaign-check-what"><strong>要核对</strong>${escape(row.check)}</p>
            ${videos == null ? '' : `<p class="campaign-check-impact ${videos ? 'is-hot' : ''}"><strong>影响</strong>${videos ? `${escape(label)} 有 ${videos} 条视频的观察窗落在这段时间，复盘按这里的日期判断。` : `目前没有 ${escape(label)} 视频的观察窗落在这段时间。`}</p>`}
            ${row.source_url ? `<p class="campaign-meta"><span>现有依据：<a href="${escape(row.source_url)}" target="_blank" rel="noopener noreferrer">${escape(row.source_label || '来源')} ↗</a></span></p>` : ''}
            <div class="campaign-check-actions"><button type="button" class="primary compact" data-check-ok>日期无误，标记已核对</button><button type="button" class="secondary compact" data-check-edit>改日期…</button><button type="button" class="subtle compact flow-delete-archived" data-check-drop>没有这个活动，删除</button></div></li>`;
        }).join('')}</ol>` : '<p class="campaign-empty">没有待核对的活动了。</p>'}</div>
        <div class="dialog-footer"><span>标记「已核对」后，复盘和 AI 简报不再提示该日期待核对</span><div><button type="button" class="secondary" data-copy ${items.length ? '' : 'disabled'}>复制清单</button><button type="button" class="primary" data-close>完成</button></div></div></form>`;
      modal.querySelectorAll('[data-close]').forEach(button => button.onclick = close);
      modal.querySelector('[data-copy]')?.addEventListener('click', () => {
        const text = [`活动日期待核对清单（共 ${items.length} 项，日期为太平洋时间）`, GUIDE, '', ...items.map(itemText)].join('\n');
        void (typeof copyText === 'function' ? copyText(text, '待核对清单已复制') : navigator.clipboard.writeText(text));
      });
      modal.querySelectorAll('[data-check-id]').forEach(node => {
        const row = campaigns().find(entry => entry.id === node.dataset.checkId);
        const settle = async body => {
          node.querySelectorAll('button').forEach(button => {button.disabled = true;});
          if (await post(body)) {onChange?.();paint();} else paint();
        };
        node.querySelector('[data-check-ok]').onclick = () => settle({action: 'save', campaign: {id: row.id, name: row.name, start: row.start, end: row.end,
          confidence: row.confidence, source_label: row.source_label, source_url: row.source_url, note: row.note, verified: true}});
        node.querySelector('[data-check-edit]').onclick = () => {close();dialog(row, onChange);};
        node.querySelector('[data-check-drop]').onclick = () => {if (confirm(`删除「${row.name}」？之后可以在「恢复默认」里找回。`)) settle({action: 'delete', id: row.id});};
      });
    }
    paint();
    modal.showModal();
  }

  function bind(root, onChange) {
    const host = root.querySelector('#perf-campaigns');
    if (!host) return;
    host.querySelector('[data-campaign-retry]')?.addEventListener('click', () => void load(true).then(onChange));
    host.querySelector('[data-campaign-add]')?.addEventListener('click', () => dialog(null, onChange));
    host.querySelector('[data-campaign-checklist]')?.addEventListener('click', () => checklistDialog(onChange));
    host.querySelector('.campaign-past')?.addEventListener('toggle', event => {state.showPast = event.currentTarget.open;});
    host.querySelectorAll('[data-campaign-edit]').forEach(button => button.onclick = () => dialog(campaigns().find(row => row.id === button.dataset.campaignEdit), onChange));
  }

  // The review's own line: which campaigns touch this video's window, and how far to trust each date.
  // bare: only the campaigns and their figures, for the video drawer's top; the review keeps the reading advice.
  function reviewNote(rows, {bare = false} = {}) {
    if (!rows?.length) return '';
    const items = rows.map(row => {
      const [label, hint] = trust[row.confidence] || trust.manual;
      const lift = weeklyImpact(row), effect = lift?.gmv ? `<small class="campaign-lift">${escape(context().product || '全部产品')}活动周周均 GMV ${times(lift.gmv)}（对照 ${lift.baseline} 个无活动周）</small>` : '';
      return `<li><strong>${escape(row.name)}</strong> ${range(row)}<span class="campaign-trust is-${escape(row.confidence)}"${bare ? '' : ` title="${escape(hint)}"`}>${label}${row.verified ? ' · 已核对' : ''}</span>${effect}</li>`;
    }).join('');
    const doubtful = rows.some(row => ['planning', 'disputed'].includes(row.confidence) && !row.verified);
    if (bare) return `<div class="feedback-campaigns"><strong>观察窗内有平台活动</strong><ul>${items}</ul></div>`;
    return `<div class="feedback-campaigns"><strong>观察窗内有平台活动</strong><ul>${items}</ul><p>高客单品在活动期的折扣和流量会直接抬高销量：下结论时把活动带来的增量和内容本身分开看${doubtful ? '；标「待核对」或「说法不一」的日期只能当作可能重叠' : ''}。</p></div>`;
  }

  return {load, current: () => state.data, selection, select, card, bind, impact, weeklyImpact, overlapping, reviewNote, phase, trust, checklist};
})();
