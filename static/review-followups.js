"use strict";
window.ReviewFollowups = (() => {
  let filter = 'ready', search = '', page = 0, period = 'all', campaignId = '', referenceFilter = '';
  let campaignData = null, mountSequence = 0;
  const key = item => `${item.product || ''}:${item.video_id}:${item.window_end}`;
  const productKey = value => String(window.WorkbenchShell?.normalizeProduct?.(value) ?? String(value || '').trim().replace(/^SHARPAL\s+/i,'').replace(/^all$/,'')).toLocaleLowerCase();
  const sameProduct = (left,right) => productKey(left) === productKey(right);
  const dayTime = value => {const time = /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? Date.parse(`${value}T00:00:00Z`) : NaN;return Number.isFinite(time) && new Date(time).toISOString().slice(0,10) === value ? time : NaN;};
  const addDays = (value, days) => new Date(dayTime(value) + days * 86400000).toISOString().slice(0,10);
  // Saved timestamps are UTC; the observation policy uses the Shanghai business day.
  // Without a valid, timezone-qualified timestamp, do not infer an early decision.
  function reviewDay(value) {
    if (typeof value !== 'string') return '';
    const parts = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/);
    if (!parts || !Number.isFinite(dayTime(parts[1])) || +parts[2] > 23 || +parts[3] > 59 || +parts[4] > 59) return '';
    const time = Date.parse(value);
    if (!Number.isFinite(time)) return '';
    try {
      const day = Object.fromEntries(new Intl.DateTimeFormat('en', {timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(time).map(part => [part.type,part.value]));
      return `${day.year}-${day.month}-${day.day}`;
    } catch { return ''; }
  }
  // API context is authoritative. The fallback also treats unknown dates and coverage as unknown.
  function context(video, today) {
    if (video.review_context) return video.review_context;
    const posted = dayTime(video.posted_on), now = dayTime(today), known = Number.isFinite(posted) && Number.isFinite(now), age = known ? Math.floor((now-posted)/86400000) : null;
    const ends = known ? addDays(video.posted_on,14) : null, observing = known && age >= 0 && age < 14;
    const covered = known && !video.registration_only && !['pending','incomplete'].includes(video.status) && Boolean(video.last_data && video.last_data >= addDays(video.posted_on,13));
    const enough = typeof video.window_views === 'number' && video.window_views >= 500;
    const evidence = observing ? {key:'observing',label:'持续观察',reason:'发布未满14天，先记录阶段信号'} : !covered || age < 0 ? {key:'missing',label:'待补数据',reason:!known ? '发布日期未记录' : age < 0 ? '发布时间在未来，请核对登记日期' : '首14天数据覆盖尚未确认'} : !enough ? {key:'insufficient',label:'证据不足',reason:'曝光样本较少，暂不判断成败'} : {key:'preliminary',label:'初步信号',reason:'可复盘本条表现，不能直接证明方向有效或无效'};
    return {observation:{ends_on:ends,age_days:age,observing,data_through:video.last_data || null,coverage_complete:covered},evidence,
      performance:{traffic:({focus:'播放较高',follow:'有一定播放',watch:'播放较少',drop:'播放较少'})[video.status] || '播放表现待确认',sales:video.orders == null ? '成交数据未知' : video.orders > 0 ? `累计 ${number(video.orders)} 单` : '覆盖期内暂无成交'},closing_allowed:known && age >= 14 && covered};
  }
  function status(video, entries, today, scripts = []) {
    const saved = entries.find(item => item.video_id === video.video_id && item.window_end === video.window_end &&
      sameProduct(item.product,video.product));
    if (saved) {
      if (saved.state === 'deferred' && saved.defer_until <= today) return {...saved,state:'pending'};
      // Old manual observation records stay in history; today's queue uses the automatic observation policy.
      if (saved.state === 'observing') return {...saved,state:'pending',original_state:'observing'};
      if (saved.state === 'reviewed') {
        const observation = context(video,today).observation;
        if (observation.age_days != null && observation.age_days >= 0 && observation.age_days < 14) return {...saved,state:'observing',original_state:'reviewed',stage_review:true,next_review_on:observation.ends_on};
        const reviewedDay = reviewDay(saved.at);
        if (observation.age_days >= 14 && Number.isFinite(dayTime(reviewedDay)) && reviewedDay < observation.ends_on) return {...saved,state:'pending',original_state:'reviewed',stage_review:true,review_due:true};
      }
      return saved;
    }
    const legacy = scripts.some(item => item.video_id === video.video_id && (!item.product || sameProduct(item.product,video.product)) && item.status === 'reviewed' &&
      (item.history || []).some(event => event.status === 'reviewed' && event.published_on && event.published_on === video.posted_on));
    const observation = legacy ? context(video,today).observation : null;
    return legacy && observation.age_days != null && observation.age_days >= 0 && observation.age_days < 14 ? {state:'observing',original_state:'reviewed',stage_review:true,next_review_on:observation.ends_on,note:''} : {state: legacy ? 'reviewed' : 'pending', note:''};
  }
  function pending(videos, entries, week, scripts = []) {
    return videos.filter(video => ['pending','observing'].includes(status(video, entries, week.today, scripts).state));
  }
  // A pending video can be reviewed only once its window has closed with complete figures; otherwise it is
  // still in its window (observing) or waits on a sales upload (missing).
  const waiting = (video, today) => context(video,today).evidence.key === 'missing' ? 'missing' : context(video,today).observation.observing ? 'observing' : '';
  function bucket(video, entries, today, scripts = []) {
    const record = status(video,entries,today,scripts);
    if (['reviewed','deferred'].includes(record.state)) return record.state;
    const wait = waiting(video,today);
    return wait || 'ready';
  }
  function ready(videos, entries, week, scripts = []) {
    return videos.filter(video => bucket(video,entries,week.today,scripts) === 'ready');
  }
  // Posts registered against a script here come first (they test a script we wrote); then the most recently due,
  // so this week's windows are on the first page and the old backlog sits behind them.
  const postKey = item => `${productKey(item.product)}:${item.video_id}`;
  const ownPosts = scripts => new Set(scripts.filter(item => item.video_id && item.published_on).map(postKey));
  const ownsPost = (own,video) => own.has(postKey(video)) || own.has(`:${video.video_id}`);
  function priority(video, entries = [], tasks = [], today = '', scripts = []) {
    const record = status(video,entries,today,scripts), ai = aiReview(video,tasks);
    if (record.state === 'reviewed') return {rank:9,reason:''};
    if (ai?.status === 'completed') return {rank:0,reason:'AI结论已准备好，等待核对'};
    if (record.review_due) return {rank:1,reason:'已满14天，重新核对发布初期的阶段结论'};
    if ((video.flags || []).some(flag => flag.key === 'late')) return {rank:2,reason:'有后起信号，值得重新检查表现'};
    if ((video.flags || []).some(flag => flag.key === 'no_orders')) {
      const age = context(video,today).observation.age_days;
      return Number.isFinite(age) && age > 45 ? {rank:4,reason:'历史视频有播放未成交，可集中复核'} : {rank:2,reason:'有播放但仍未出单，优先检查成交环节'};
    }
    if (video.orders > 0 && video.window_views >= 3000) return {rank:3,reason:'同时有播放与成交信号，检查可复制的做法'};
    if (ai?.status === 'needs_revision') return {rank:4,reason:'已有复盘需要继续核对'};
    return {rank:5,reason:'已到复盘节点，确认结论和参考价值'};
  }
  function order(videos, scripts = [], {entries = [],tasks = [],today = ''} = {}) {
    const own = ownPosts(scripts);
    return [...videos].sort((a, b) => priority(a,entries,tasks,today,scripts).rank - priority(b,entries,tasks,today,scripts).rank ||
      Number(Boolean(b.registered || ownsPost(own,b))) - Number(Boolean(a.registered || ownsPost(own,a))) ||
      String(b.window_end).localeCompare(String(a.window_end)) || String(a.video_id).localeCompare(String(b.video_id)));
  }
  const number = value => value === null || value === undefined || value === '' ? '—' : Number(value).toLocaleString('zh-CN');
  const chip = (text, tone = '') => `<span class="home-chip${tone ? ` is-${tone}` : ''}">${esc(text)}</span>`;
  const stateNames = {pending:'待处理', observing:'持续观察', deferred:'暂缓', reviewed:'已复盘'};
  const decisionNames = {scale:'加码', execution:'换执行', stop:'停测', observe:'历史观察记录', none:'已结束'};
  const referenceNames = {none:'无参考价值',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'};
  const referenceFilters = {'':'全部参考价值',unclassified:'待分类',...referenceNames};
  // The newest open AI retrospective bound to this video and window; it lives here now, not in 内容创作.
  const aiStates = {running:['AI 复盘中','good'], queued:['AI 复盘排队',''], needs_revision:['AI 复盘待继续','warn'],
    completed:['AI 结论待确认','warn'], failed:['AI 复盘失败','warn'], interrupted:['AI 复盘已中断','warn']};
  function aiReview(video, tasks) {
    const bound = tasks.filter(task => task.action === 'retrospective' && !task.archived && (() => {
      const ref = task.context?.source_review || task.context?.review_video || {};
      return (!task.product || sameProduct(task.product,video.product)) && (!ref.product || sameProduct(ref.product,video.product)) &&
        String(ref.video_id || '') === String(video.video_id) && (!ref.window_end || ref.window_end === video.window_end);
    })()).sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
    if (!bound || ['adopted','abandoned'].includes(bound.management_state)) return null;
    const state = bound.management_state === 'needs_revision' && !['running','queued'].includes(bound.status) ? 'needs_revision' : bound.status;
    return aiStates[state] ? {task_id: bound.task_id, status:state,label: aiStates[state][0], tone: aiStates[state][1]} : null;
  }
  async function nextReady({product = '',exclude = null,week} = {}) {
    const scope = productKey(product), selected = scope ? product : '';
    const [videosData,followups,creation,tasksData] = await Promise.all([
      api(`/api/performance/videos?${new URLSearchParams({projection:'home',...(scope ? {product:selected} : {})})}`),
      api(`/api/review-followups?product=${encodeURIComponent(selected)}`),
      api(`/api/creation?product=${encodeURIComponent(selected)}`),
      api('/api/ai/tasks')
    ]);
    if (!Array.isArray(videosData?.videos) || !Array.isArray(followups?.entries) || !Array.isArray(creation?.scripts) || !Array.isArray(tasksData?.tasks)) {
      throw new Error('待复盘队列未完整读取，请重试。');
    }
    week = week || window.WorkbenchHome?.currentWeek?.();
    if (!week?.today) throw new Error('今日复盘日期未就绪，请重新读取工作概览。');
    const scoped = rows => rows.filter(row => !scope || sameProduct(row.product,selected));
    const entries = scoped(followups.entries), scripts = scoped(creation.scripts), tasks = tasksData.tasks;
    const videos = scoped(videosData.videos).filter(video => !exclude || !(sameProduct(video.product,exclude.product) &&
      String(video.video_id) === String(exclude.video_id) && video.window_end === exclude.window_end));
    const queue = order(ready(videos,entries,week,scripts),scripts,{entries,tasks,today:week.today});
    const video = queue[0] || null;
    return {video,remaining:queue.length,reason:video ? priority(video,entries,tasks,week.today,scripts).reason : ''};
  }
  function mount(host, {videos, entries, revision, week, scripts, tasks = [], refresh, url}) {
    if (!host) return;
    const sequence = ++mountSequence;
    let campaignError = false, controlsOpen = false;
    const pageSize = 5;
    const syncSharedCampaign = () => {
      const selected = window.Campaigns?.selection?.();
      if (typeof selected !== 'string' || selected === campaignId) return false;
      campaignId = selected;
      page = 0;
      return true;
    };
    const selectCampaign = id => {
      campaignId = id;
      window.Campaigns?.select?.(id);
      page = 0;
    };
    syncSharedCampaign();
    campaignData = window.Campaigns?.current?.() || campaignData;
    if (campaignId && campaignData && !campaignData.campaigns.some(row => row.id === campaignId)) selectCampaign('');
    const labels = {ready:'今日优先',observing:'持续观察',missing:'待补数据',pending:'全部待处理',deferred:'暂缓',reviewed:'已复盘',all:'全部'};
    const own = ownPosts(scripts || []);
    videos = order(videos, scripts || [],{entries,tasks,today:week.today});
    const dateSummary = {coverage:{first:videos.map(video => video.window_end).filter(Boolean).sort()[0],last:week.today}};
    const chosenCampaign = () => campaignData?.campaigns?.find(row => row.id === campaignId);
    const drawCampaigns = () => {
      const container = host.querySelector('[data-followup-campaigns]');
      if (!container) return;
      const rows = [...(campaignData?.campaigns || [])].sort((a,b) => (a.start > week.today) - (b.start > week.today) || b.start.localeCompare(a.start));
      const campaign = chosenCampaign();
      container.innerHTML = `<label>活动期 <select data-followup-campaign aria-label="活动期快速筛选" ${!campaignData ? 'disabled' : ''}><option value="">${campaignData ? '全部活动期' : campaignError ? '活动读取失败' : '正在读取活动…'}</option>${rows.map(row => `<option value="${esc(row.id)}" ${row.id === campaignId ? 'selected' : ''}>${esc(row.name)} · ${esc(row.start)} 至 ${esc(row.end)}${row.start > week.today ? ' · 未开始' : ''}</option>`).join('')}</select></label>${campaignError ? '<span role="status">活动日历暂时无法读取。</span><button type="button" class="subtle compact" data-followup-campaign-retry>重试</button>' : campaignData && !rows.length ? '<span>还没有登记活动。</span>' : ''}${campaign ? `<p class="followup-filter-hint">${esc(campaign.name)} · ${esc(campaign.start)} 至 ${esc(campaign.end)}（太平洋时间）${campaign.verified ? ' · 已核对' : ['planning','disputed'].includes(campaign.confidence) ? ' · 日期待核对' : ''}。观察窗与活动期有重叠的视频会计入，含起止日期。</p>` : ''}`;
      container.querySelector('[data-followup-campaign]').onchange = event => {selectCampaign(event.target.value);draw();};
      container.querySelector('[data-followup-campaign-retry]')?.addEventListener('click', () => void loadCampaigns(true));
    };
    const loadCampaigns = async (force = false) => {
      if (!window.Campaigns) return;
      campaignError = false;
      if (campaignId && !campaignData) draw();
      else drawCampaigns();
      let data;
      try {data = await window.Campaigns.load(force);} catch {data = null;}
      if (!host.isConnected || sequence !== mountSequence) return;
      const previous = campaignData;
      const previousCampaign = campaignId;
      campaignError = !data;
      if (data) campaignData = data;
      const sharedChanged = syncSharedCampaign();
      if (campaignId && data && !chosenCampaign()) selectCampaign('');
      if (sharedChanged || campaignId !== previousCampaign || ((campaignId || previousCampaign) && (previous !== campaignData || !data))) {
        page = 0;
        const input = host.querySelector('[data-followup-search]'), focused = document.activeElement === input, position = input?.selectionStart;
        draw();
        if (focused) {const next = host.querySelector('[data-followup-search]');next.focus();try {next.setSelectionRange(position,position);} catch {}}
      } else drawCampaigns();
    };
    const draw = () => {
      const oldControls = host.querySelector('.followup-controls');
      if (oldControls) controlsOpen = oldControls.open;
      const dates = period === 'all' ? null : period.split('~'), campaign = chosenCampaign();
      const campaignPending = Boolean(campaignId && !campaignData);
      // Reference value is an explicit human classification of completed reviews.
      // It must never silently narrow the observation or today's action queues.
      const activeReference = filter === 'reviewed' ? referenceFilter : '';
      const narrowed = Boolean(search || dates || campaignId || activeReference);
      const scoped = videos.filter(video => (!dates || (video.window_end >= dates[0] && video.window_end <= dates[1])) &&
        (!campaignId || (campaign && video.posted_on && video.window_end && video.posted_on <= campaign.end && video.window_end >= campaign.start)) &&
        `${video.headline || ''} ${video.title || ''} ${video.video_id} ${video.account || ''}`.toLowerCase().includes(search.toLowerCase()));
      const counts = Object.fromEntries(['ready','observing','missing'].map(name => [name,scoped.filter(video => bucket(video,entries,week.today,scripts) === name).length]));
      const observing = counts.observing, missing = counts.missing;
      const selected = scoped.filter(video => {
        const group = bucket(video,entries,week.today,scripts);
        const inQueue = filter === 'all' || (filter === 'pending' ? ['ready','observing','missing'].includes(group) : group === filter);
        const value = status(video,entries,week.today,scripts).reference_value;
        return inQueue && (!activeReference || (activeReference === 'unclassified' ? !referenceNames[value] : value === activeReference));
      });
      const note = filter === 'ready' && (observing || missing) ? `<p class="followup-waiting">另有 ${[missing ? `<span title="补齐日期与销售数据后再判断">${missing} 条待补数据</span>` : '', observing ? `${observing} 条持续观察` : ''].filter(Boolean).join('、')}。<button type="button" class="subtle compact" data-followup-show-waiting>一起看</button></p>` : '';
      page = Math.min(page, Math.max(0, Math.ceil(selected.length / pageSize) - 1));
      // The tab already counts the default list; a count here only helps once a filter or search narrows it.
      const count = campaignPending ? `<span role="status">${campaignError ? '活动读取失败' : '正在读取活动…'}</span>` : filter !== 'ready' || narrowed ? `<span>${selected.length} 条</span>` : '';
      const datePicker = window.PerfDatePicker ? `<div class="followup-date-field"><span>到期日期</span>${window.PerfDatePicker.trigger(period,dateSummary,{id:'followup-date',label:'到期日期'})}</div>` : '';
      const queues = `<div class="followup-queues" role="group" aria-label="复盘队列">${['ready','observing','missing'].map(name => `<button type="button" class="followup-queue" data-followup-queue="${name}" aria-pressed="${filter === name}">${labels[name]}<span>${counts[name]}</span></button>`).join('')}</div>`;
      const activeFilters = [filter !== 'ready' ? labels[filter] : '',activeReference ? referenceFilters[activeReference] : '',search ? `搜索“${search}”` : '',dates ? `${dates[0]} 至 ${dates[1]}` : '',campaignId ? campaign?.name || '所选活动期' : ''].filter(Boolean);
      const referenceControl = filter === 'reviewed' ? `<label>参考价值 <select data-followup-reference-filter>${Object.entries(referenceFilters).map(([value,label]) => `<option value="${value}" ${activeReference === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>` : '';
      const title = activeFilters.length ? '筛选结果' : page ? '继续复盘' : '今天先看';
      host.innerHTML = `<div class="followup-heading"><h3>${title}</h3></div><details class="followup-controls"${controlsOpen ? ' open' : ''}><summary>筛选与其他视频</summary>${queues}<div class="followup-tools">${datePicker}<label>处理状态 <select data-followup-filter>${Object.entries(labels).map(([value,label]) => `<option value="${value}" ${filter === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label>${referenceControl}<label>搜索 <input type="search" data-followup-search value="${esc(search)}" placeholder="标题、账号或视频 ID"></label>${count}</div>${window.Campaigns ? '<div class="followup-campaigns" data-followup-campaigns></div>' : ''}${note}</details>${activeFilters.length ? `<div class="followup-active-filter"><span>${esc(activeFilters.join(' · '))}</span><button type="button" class="subtle compact" data-followup-reset>清空筛选</button></div>` : ''}<div class="home-list">${selected.slice(page*pageSize,page*pageSize+pageSize).map((video,index) => {
        const record = status(video,entries,week.today,scripts), group = bucket(video,entries,week.today,scripts), info = context(video,week.today), observation = info.observation, evidence = info.evidence;
        const [label, tone] = record.state === 'reviewed' ? [record.decision ? `已复盘 · ${decisionNames[record.decision]}` : '已复盘',''] : record.state === 'deferred' ? [`暂缓至 ${record.defer_until}`,''] :
          group === 'observing' ? ['持续观察','info'] : group === 'missing' ? ['待补数据','warn'] : ['待复盘',''];
        const age = observation.age_days == null ? '发布日期待补' : observation.age_days < 0 ? '发布时间待核对' : `发布第 ${observation.age_days+1} 天`;
        const datesLine = [age,observation.ends_on ? `${observation.ends_on} 满14天` : '',observation.data_through ? `数据截至 ${observation.data_through}` : '尚无销售数据',video.account || '账号未记录'].filter(Boolean).join(' · ');
        const figures = `报表窗播放 ${number(video.window_views)} · 累计出单 ${number(video.orders)}`;
        const ai = record.state === 'reviewed' ? null : aiReview(video, tasks);
        const flags = (video.registered || ownsPost(own,video) ? chip('工作台发布') : '') + (ai ? `<a class="home-chip${ai.tone ? ` is-${ai.tone}` : ''}" href="${esc(url(`#ai/${encodeURIComponent(ai.task_id)}`))}">${esc(ai.label)}</a>` : '') + (video.flags || []).map(flag => chip(flag.label,'info')).join('');
        const reason = group === 'ready' ? priority(video,entries,tasks,week.today,scripts).reason : group === 'observing' ? [observation.ends_on ? `${observation.ends_on} 首轮复盘` : '',record.stage_review ? '已保留阶段复盘，满14天后再核对' : evidence.reason].filter(Boolean).join(' · ') : group === 'missing' ? evidence.reason : record.state === 'deferred' ? `${record.defer_until} 再提醒` : record.state === 'reviewed' ? decisionNames[record.decision] || '已保存本轮复盘' : '';
        const reference = record.state === 'reviewed' ? `<span class="home-chip" data-followup-reference-value="${esc(record.reference_value || '')}">${esc(referenceNames[record.reference_value] || '待分类')}</span> ` : '';
        const evidenceTone = ['missing','insufficient'].includes(evidence.key) ? 'warn' : evidence.key === 'observing' ? 'info' : '';
        const action = record.state === 'reviewed' ? '查看复盘' : record.state === 'deferred' ? '查看记录' : group === 'ready' ? ai?.status === 'completed' ? '核对结论' : ['running','queued'].includes(ai?.status) ? '查看进度' : '开始复盘' : group === 'observing' ? '查看观察' : '核对数据';
        return `<article class="home-row followup-row" data-followup-video="${esc(video.video_id)}"><div class="followup-summary"><div class="home-row-main">${filter === 'ready' && !narrowed && page === 0 && index === 0 ? '<span class="followup-first">先看这条</span>' : ''}<a href="${esc(url(video.registration_only ? '#home' : `#videos?tab=list&video=${encodeURIComponent(video.video_id)}`))}"${video.registration_only ? ` data-followup-registration="${esc(key(video))}" aria-haspopup="dialog"` : ''}><strong title="${esc(video.headline || video.title || video.video_id)}">${esc(video.headline || video.title || video.video_id)}</strong></a>${reason || reference ? `<p class="followup-reason">${reference}${esc(reason)}</p>` : ''}</div><button type="button" class="${filter === 'ready' && page === 0 && index === 0 ? 'primary' : 'secondary'} compact" data-followup-chain="${esc(key(video))}">${action}</button></div><details class="followup-details"><summary>数据与其他操作</summary><small>${esc(datesLine)}</small><small>${esc(figures)}${flags ? `<span class="followup-flags">${flags}</span>` : ''}</small><div class="followup-assessment"><span>表现：${esc([info.performance.traffic,info.performance.sales].filter(Boolean).join(' · '))}</span><span title="${esc(evidence.reason || '')}">证据：${chip(evidence.label,evidenceTone)}</span></div>${record.note ? `<small class="followup-note">${esc(record.note)}</small>` : ''}<div class="followup-actions">${chip(label,tone)}<button type="button" class="subtle compact" data-followup-edit="${esc(key(video))}" data-followup-preset="${record.state === 'pending' ? 'deferred' : ''}" title="${record.state === 'pending' ? '暂缓到某天再提醒，或改处理状态' : '改处理状态'}">${record.state === 'pending' ? '暂缓' : record.state === 'deferred' ? '改日期' : '改状态'}</button></div></details></article>`;
      }).join('') || `<div class="home-empty"><p${campaignPending ? ' role="status"' : ''}>${campaignPending ? campaignError ? '活动日历暂时无法读取，请重试或清空筛选。' : '正在读取所选活动期…' : narrowed ? '没有符合筛选条件的视频。' : filter === 'ready' ? '今天没有待复盘的视频。观察中和待补数据的视频可在上方展开查看。' : '这里没有待处理的视频。'}</p></div>`}</div><div class="followup-pager"${campaignPending || selected.length <= pageSize ? ' hidden' : ''}><button type="button" class="subtle compact" data-followup-prev ${page === 0 ? 'disabled' : ''}>前 5 条</button><span>${selected.length ? page*pageSize+1 : 0}–${Math.min((page+1)*pageSize,selected.length)} / 共 ${selected.length} 条</span><button type="button" class="subtle compact" data-followup-next ${(page+1)*pageSize >= selected.length ? 'disabled' : ''}>再看 5 条</button></div>`;
      window.PerfDatePicker?.bind({id:'followup-date',label:'到期日期',hint:'按原报表观察窗到期日筛选；发布满14天另行显示。含起止日期，可与活动期、处理状态和搜索叠加。',maxDate:[week.end,...videos.map(video => video.window_end)].filter(Boolean).sort().at(-1),
        period:() => period,summary:() => dateSummary,choose:value => {period = value;page = 0;draw();}});
      drawCampaigns();
      host.querySelector('[data-followup-reset]')?.addEventListener('click', () => {filter = 'ready';referenceFilter = '';search = '';period = 'all';selectCampaign('');draw();});
      host.querySelector('[data-followup-filter]').onchange = event => {filter = event.target.value;if (filter !== 'reviewed') referenceFilter = '';page = 0;draw();};
      host.querySelectorAll('[data-followup-queue]').forEach(button => button.onclick = () => {filter = button.dataset.followupQueue;referenceFilter = '';page = 0;draw();});
      host.querySelector('[data-followup-reference-filter]')?.addEventListener('change', event => {referenceFilter = event.target.value;page = 0;draw();});
      host.querySelector('[data-followup-search]').oninput = event => {
        search = event.target.value;page = 0;const position = event.target.selectionStart;draw();
        const input = host.querySelector('[data-followup-search]');input.focus();try {input.setSelectionRange(position,position);} catch {}
      };
      host.querySelector('[data-followup-prev]').onclick = () => {page--;draw();};
      host.querySelector('[data-followup-next]').onclick = () => {page++;draw();};
      host.querySelectorAll('[data-followup-edit]').forEach(button => button.onclick = () => edit(videos.find(video => key(video) === button.dataset.followupEdit),button.dataset.followupPreset));
      // Review in the shared video drawer; keep this queue current after each save or close.
      const openReview = id => {
        const video = videos.find(video => key(video) === id);
        if (video) return window.CasePerformance?.openVideoInPlace(video.video_id,{feedbackWindow:video.window_end,video,onClose:refresh,onChanged:refresh});
      };
      host.querySelectorAll('[data-followup-chain]').forEach(button => button.onclick = () => openReview(button.dataset.followupChain));
      host.querySelectorAll('[data-followup-registration]').forEach(link => link.onclick = event => {event.preventDefault();void openReview(link.dataset.followupRegistration);});
      host.querySelector('[data-followup-show-waiting]')?.addEventListener('click', () => {filter = 'pending';referenceFilter = '';page = 0;draw();});
    };
    // preset: the state the clicked button names (「暂缓」 opens on 暂缓 with its date field ready).
    const edit = (video, preset = '') => {
      const saved = status(video,entries,week.today,scripts), dialog = document.createElement('dialog');
      const info = context(video,week.today), tomorrow = addDays(week.today,1);
      dialog.className = 'followup-dialog';
      dialog.setAttribute('aria-label','登记复盘处理');
      dialog.innerHTML = `<form><div class="dialog-heading"><h2>登记复盘处理</h2><button type="button" class="subtle" data-close>关闭</button></div><div class="followup-fields"><p>${esc(video.headline || video.video_id)}${info.observation.ends_on ? ` · ${esc(info.observation.ends_on)} 满14天` : ' · 发布日期待补'}</p><label class="field"><span>处理状态</span><select name="state"><option value="pending">待处理</option><option value="reviewed">已复盘</option><option value="deferred">暂缓</option></select></label>${info.closing_allowed ? '' : `<p class="followup-early-warning" role="note" data-early-warning>${esc(info.early_warning || `${info.evidence?.reason || '数据还不完整'}。现在也可以先下结论，之后可再改。`)}</p>`}<label class="field" data-defer><span>重新提醒日期</span><input type="date" name="defer_until" min="${tomorrow}" value="${esc(saved.defer_until || '')}"></label><label class="field"><span>复盘结论或备注（已复盘时必填）</span><textarea name="note" rows="4" maxlength="4000">${esc(saved.note || '')}</textarea></label>${saved.history?.length ? `<details><summary>处理历史（${saved.history.length}）</summary>${saved.history.slice().reverse().map(entry => `<p>${esc(entry.at)} · ${esc(stateNames[entry.state] || entry.state)}${entry.next_review_on ? ` · ${esc(entry.next_review_on)} 再看` : entry.defer_until ? ` · ${esc(entry.defer_until)}` : ''}<br>${esc(entry.observation_reason || entry.note || '')}</p>`).join('')}</details>` : ''}<p class="ai-error" role="alert" data-error hidden></p></div><div class="dialog-footer"><span>发布未满14天或缺数据的视频由系统自动保留待办。</span><button type="submit" class="primary">保存</button></div></form>`;
      document.body.append(dialog);
      const form = dialog.querySelector('form'), state = form.elements.namedItem('state');state.value = preset || (saved.state === 'observing' ? 'pending' : saved.state);
      const toggle = () => {dialog.querySelector('[data-defer]').hidden = state.value !== 'deferred';form.elements.namedItem('defer_until').required = state.value === 'deferred';form.elements.namedItem('note').required = state.value === 'reviewed';const warning = dialog.querySelector('[data-early-warning]');if (warning) warning.hidden = state.value !== 'reviewed';};
      state.onchange = toggle;toggle();
      let busy = false, uncertain = false, request = null, requestText = '';
      dialog.querySelector('[data-close]').onclick = () => {if (!busy) dialog.close();};
      dialog.addEventListener('cancel', event => {if (busy) event.preventDefault();});
      dialog.addEventListener('close', () => dialog.remove(), {once:true});
      form.onsubmit = async event => {
        event.preventDefault();if(busy)return;
        const value = {revision,product:video.product,video_id:video.video_id,window_end:video.window_end,state:state.value,
          decision:state.value === 'reviewed' && ['scale','execution','stop'].includes(saved.decision) ? saved.decision : '',
          note:form.elements.namedItem('note').value.trim(),defer_until:state.value === 'deferred' ? form.elements.namedItem('defer_until').value : ''};
        const signature=JSON.stringify(value);if(!uncertain && signature!==requestText){request={...value,request_id:crypto.randomUUID()};requestText=signature;}
        busy = true;const button = form.querySelector('[type=submit]');button.disabled=true;
        try {await api('/api/review-followups',{method:'POST',body:request});dialog.close();await refresh();}
        catch(error){
          const output=dialog.querySelector('[data-error]');output.hidden=false;output.textContent=error.message;
          uncertain = !error.status || error.status >= 500;
          if(uncertain)output.textContent+=' 保存结果尚未确认，输入暂时锁定；再次保存会核对同一请求。';
          if(error.status===409){try{
            const latest=await api('/api/review-followups');revision=latest.revision;
            const current=status(video,latest.entries || [],week.today,scripts);
            output.textContent+=` 当前记录：${stateNames[current.state]}${current.defer_until ? `至 ${current.defer_until}` : ''}；${current.note || '无备注'}。你的输入已保留，请核对后再保存。`;
          }catch{output.textContent+=' 最新记录读取失败，请关闭后重新打开。';}}
        }
        finally{busy=false;button.disabled=false;form.querySelectorAll('input,select,textarea').forEach(field=>field.disabled=uncertain);}
      };
      dialog.showModal();
      if (preset === 'deferred') form.elements.namedItem('defer_until').focus();
    };
    draw();
    void loadCampaigns();
  }
  return {status,pending,ready,order,mount,context,bucket,priority,nextReady};
})();
