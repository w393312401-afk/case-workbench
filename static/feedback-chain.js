'use strict';
window.FeedbackChain = (() => {
  const pending = new Map();
  const pendingDirections = new Map();
  const pendingContinuations = new Map();
  let mountGeneration = 0;
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const labels = {pending:'待复盘',observing:'持续观察',reviewed:'已复盘',deferred:'暂缓',working:'进行中',done:'已完成',cancelled:'已取消',queued:'排队中',running:'处理中',completed:'已完成',failed:'失败',interrupted:'已中断',adopted:'已采纳'};
  // Scaling or changing execution creates a next-round plan; stopping closes only this video.
  const decisions = {scale:['加码','good'],execution:['换执行','info'],stop:['停测','warn']};
  const savedDecision = value => decisions[value] || ({observe:['历史观察',''],none:['已结束','']})[value];
  const referenceNames = {'':'待分类',none:'无参考价值',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'};
  const reviewStatusNames = {pending:'待复盘',unclassified:'待分类',none:'无参考价值',weak:'弱参考价值',medium:'中等参考价值',high:'高参考价值'};
  function manualReviewStatus(data) {
    const saved = data.review_status || data.summary?.review_status;
    if (saved && Object.hasOwn(reviewStatusNames,saved.key)) return {key:saved.key,label:reviewStatusNames[saved.key]};
    // A response for this exact window may carry an older human review shape.
    // Never infer reference value from the sales bucket or the outer video's status.
    const reviewed = data.review?.state === 'reviewed', value = data.review?.reference_value;
    const key = !reviewed ? 'pending' : value && Object.hasOwn(referenceNames,value) ? value : 'unclassified';
    return {key,label:reviewStatusNames[key]};
  }
  const options = (values, selected) => Object.entries(values).map(([value,label]) => `<option value="${value}"${selected === value ? ' selected' : ''}>${label}</option>`).join('');
  function reviewContext(data) {
    // Older server responses cannot authorize closing a newly published video.
    return data.review_context || {observation:{},performance:{traffic:'表现口径待更新',sales:'成交数据待核对'},evidence:{key:'missing',label:'证据待核对',reason:'请刷新工作台，读取发布观察期和数据覆盖。'},closing_allowed:false};
  }
  function contextBlock(context) {
    const observation = context.observation || {}, evidence = context.evidence || {}, performance = context.performance || {};
    const age = Number.isInteger(observation.age_days) && observation.age_days >= 0 ? `发布第 ${observation.age_days + 1} 天` : '发布日期待补充';
    const dates = [age,observation.ends_on ? `${observation.ends_on} 满14天` : '',`数据截至 ${observation.data_through || '尚未取得'}`].filter(Boolean).join(' · ');
    return `<section class="feedback-evidence" aria-label="表现与证据"><p class="feedback-observation-age">${esc(dates)}</p><dl><div><dt>表现</dt><dd>${esc([performance.traffic,performance.sales].filter(Boolean).join(' · ') || '尚无足够数据')}</dd></div><div><dt>证据</dt><dd>${chip([evidence.label || '待核对',context.closing_allowed ? '' : 'info'])}${evidence.reason ? `<small>${esc(evidence.reason)}</small>` : ''}</dd></div></dl></section>`;
  }
  function stageLine(data, context, reviewed) {
    const observation = context.observation || {};
    if (observation.observing) return `发布${Number.isInteger(observation.age_days) ? `第 ${observation.age_days + 1} 天，` : '未满14天，'}仍在观察期；${reviewed ? '已保存阶段结论，满14天后可再改。' : '可以先下阶段结论。'}`;
    if (!context.closing_allowed && !reviewed) return `${context.evidence?.label || '数据待核对'}；可以先按已有数据下结论。`;
    if (reviewed) return data.current_window ? data.review?.decision === 'stop' ? '已结束本条复盘；可记下经验，供下次写稿和审稿参考。' : '复盘已保存，可记下经验或安排下一轮。' : '这是已保存的历史复盘。';
    if (data.draft?.status === 'completed') return 'AI 结论待确认，核对后保存复盘。';
    return '首14天数据已覆盖，可以开始复盘。';
  }
  const sourceURL = source => url(`#videos?video=${encodeURIComponent(source.video_id)}&feedback_window=${encodeURIComponent(source.window_end)}`,source.product);
  const sourceLink = source => source?.video_id && source?.product ? `<a class="feedback-origin-link" href="${esc(sourceURL(source))}">来源复盘 · ${esc(source.video_id)} · ${esc(source.window_end || '窗口未记录')}</a>` : '';
  const tiktokLink = (id,account) => `https://www.tiktok.com/@${encodeURIComponent(String(account || '').replace(/^@/,'') || '_')}/video/${encodeURIComponent(id)}`;
  const refKey = video => `${video.product}:${video.video_id}:${video.window_end || ''}`;
  // An unsaved conclusion is a per-browser convenience: kept until saved or cleared, never required to be there.
  const noteKey = video => 'workbench.feedback-note:' + refKey(video);
  const keptNote = video => {try {const kept = JSON.parse(localStorage.getItem(noteKey(video)) || 'null');return kept && typeof kept.text === 'string' && kept.text.trim() ? kept : null;} catch {return null;}};
  const keepNote = (video,text) => {try {if (text.trim()) localStorage.setItem(noteKey(video),JSON.stringify({text,at:Date.now()}));else localStorage.removeItem(noteKey(video));} catch {}};
  const requestURL = video => '/api/feedback-chain?' + new URLSearchParams({product:video.product,video_id:video.video_id,...(video.window_end ? {window_end:video.window_end} : {})});
  const count = v => v == null ? '—' : Math.round(v).toLocaleString('en-US');
  const money = v => v == null ? '—' : '$' + Math.round(v).toLocaleString('en-US');
  const pct = v => v == null ? '—' : Number(v).toFixed(2) + '%';
  const chip = ([text,tone]) => `<span class="home-chip${tone ? ` is-${tone}` : ''}">${esc(text)}</span>`;
  const activeTask = status => ['queued','running'].includes(status);

  function sameMetricScope(data, video) {
    if (!video || data.current_window !== true || data.window_end !== video.window_end || data.summary?.window_end !== video.window_end || data.summary?.posted_on !== video.posted_on) return false;
    const metrics = data.summary?.metrics;
    return !!metrics && ['window_views','views','ctr_pct','orders','ctor_pct','gmv'].every(key => Object.is(metrics[key] ?? null, video[key] ?? null));
  }
  function figures(summary, omitCore = false) {
    if (!summary) return '';
    const m = summary.metrics || {};
    const cells = [['观察窗播放',count(m.window_views)],['累计播放',count(m.views)],['点击率',pct(m.ctr_pct)],['出单',count(m.orders)],['转化率',pct(m.ctor_pct)],['GMV',money(m.gmv)]];
    const place = (label,row) => `${label} ${row.count} 条有完整数据的视频${row.top_pct ? `里排前 ${row.top_pct}%` : ''}，中位观察窗播放 ${count(row.median_window_views)}`;
    const lines = [summary.product ? place('同产品',summary.product) : '同产品还没有完整数据可对照',
      !summary.direction_id ? '这条还没关联方向，只能和同产品对照' : summary.direction?.count > 1 ? place(`同方向 ${summary.direction_id}`,summary.direction) : `方向 ${summary.direction_id} 还没有其他完整数据可对照`];
    return `<section class="feedback-figures" aria-label="这条视频的表现">${omitCore ? '' : `<div class="feedback-figure-grid">${cells.map(([label,value]) => `<div><small>${label}</small><strong>${value}</strong></div>`).join('')}</div>`}<p class="feedback-compare">${esc(lines.join('；'))}。</p></section>`;
  }
  // The funnel against its peers' medians (same direction, eight weeks; a thin direction falls back to the product
  // per figure): a bar per step, the median at the middle of the track.
  const verdicts = {strong:['强于同类','good'],weak:['弱于同类','warn'],flat:['持平',''],unknown:['判断不了','']};
  function funnelChart(funnel, summary) {
    if (!funnel?.steps?.length) return '';
    const value = row => row.unit === 'pct' ? pct(row.value) : count(row.value);
    const rows = funnel.steps.map(row => {
      const width = row.ratio == null ? 0 : Math.min(100, row.ratio * 50), drop = funnel.drop === row.key;
      return `<li class="retro-step is-${row.verdict}${drop ? ' is-drop' : ''}"><span class="retro-step-label">${esc(row.label)}${drop ? '<em>最先掉下去</em>' : ''}</span>`
        + `<span class="retro-track" aria-hidden="true"><i data-width="${width}"></i><b></b></span>`
        + `<span class="retro-step-value"><strong>${value(row)}</strong><small>${esc(row.pool || '同类')} ${row.unit === 'pct' ? pct(row.reference) : count(row.reference)}${row.peers ? `（${row.peers} 条）` : ''}${row.ratio != null ? ` · ${row.ratio}×` : ''}</small></span>`
        + `${chip(verdicts[row.verdict])}${row.note ? `<small class="retro-step-note">${esc(row.note)}</small>` : ''}</li>`;
    }).join('');
    const m = summary?.metrics || {}, week = funnel.week, period = funnel.period;
    const fallback = funnel.steps.some(row => row.pool === '同产品');
    const peers = !period ? '竖线是同类中位' : `对照：${funnel.direction_id ? `同方向 ${esc(funnel.direction_id)}` : '同产品（这条没关联方向）'}，${esc(period.start)} 至 ${esc(period.end)} 近 8 周，竖线是中位`
      + (funnel.direction_id && fallback ? '；标「同产品」的项同方向不足 5 条' : '');
    return `<section class="retro-funnel" aria-label="漏斗对照"><ol>${rows}</ol><p class="retro-funnel-foot">出单 ${count(m.orders)} · GMV ${money(m.gmv)}${week ? ` · 比率按播放最多那周 ${esc(week.start || '')}–${esc(week.end || '')}` : ''}</p><p class="retro-funnel-foot">${peers}</p></section>`;
  }
  // The page's CSP drops style attributes in markup, so bar widths are set through the DOM once drawn.
  const paintBars = root => root?.querySelectorAll('.retro-track i[data-width]').forEach(bar => {bar.style.width = `${bar.dataset.width}%`;});
  // What the AI draft says, as a card: one verdict, at most three reasons, what to keep and the next A/B.
  function retroCard(card, action, compact = false, verdict = true) {
    if (!card) return '';
    const reasons = card.reasons?.length ? `<ol class="retro-reasons">${card.reasons.map(row => `<li><span class="retro-at">${esc(row.at || '全片')}</span><strong>${esc(row.point)}</strong>${row.fix ? `<small>→ ${esc(row.fix)}</small>` : ''}</li>`).join('')}</ol>` : '';
    const keep = card.keep?.length ? `<p class="retro-keep"><span>保留</span>${card.keep.map(item => `<em>${esc(item)}</em>`).join('')}</p>` : '';
    const next = card.next && Object.keys(card.next).length ? `<dl class="retro-next">${[['change','下一条只改'],['hold','固定不动'],['judge','看这个数']].filter(([key]) => card.next[key]).map(([key,label]) => `<div><dt>${label}</dt><dd>${esc(card.next[key])}</dd></div>`).join('')}</dl>` : '';
    const detail = reasons + keep + next;
    return `<section class="retro-card">${verdict ? `<p class="retro-verdict">${action ? chip(action) : ''}<span>${esc(card.verdict || '')}</span></p>` : ''}${compact && detail ? `<details class="feedback-draft-details"><summary>分析与建议</summary>${detail}</details>` : detail}</section>`;
  }
  // The AI draft's controls sit on the 结论 label row; its folded reasoning, when there is one, sits under the field.
  function draftActions(data) {
    const draft = data.draft;
    if (data.video?.registration_only && !draft) return '<small class="feedback-draft-note">补齐销售数据后可让 AI 起草复盘。</small>';
    if (!draft) return '<button type="button" class="feedback-link" data-feedback-draft>AI 起草</button>';
    const link = `<a class="feedback-link" href="${esc(url('#ai/'+encodeURIComponent(draft.task_id),data.product))}">看完整分析</a>`;
    if (activeTask(draft.status)) return `<span class="home-chip is-good is-live" role="status"><i></i>AI 正在起草</span>${link}`;
    if (draft.status !== 'completed') return `<span class="home-chip is-warn">AI 起草${esc(labels[draft.status] || '未完成')}</span>${link}<button type="button" class="feedback-link" data-feedback-draft>重新起草</button>`;
    return `${link}<button type="button" class="feedback-link" data-feedback-draft>重新起草</button>`;
  }
  function draftCard(data) {
    const draft = data.draft;
    // The conclusion field already holds the one-line verdict; the card keeps only the folded reasoning then.
    return draft?.status === 'completed' && draft.draft?.card ? retroCard(draft.draft.card, null, true, !draft.draft.conclusion) : '';
  }

  function mount(host,{video,onChanged,onLoaded,onSaved,heading = true,externalMetrics = null,initialData = null} = {}) {
    if (!host || !video?.video_id || !video.product) return;
    let sequence = 0, editing = false, typed = null, referenceValue = null, directionValue = null, submitting = false, confirmedSave = false;
    const owner = refKey(video), instance = String(++mountGeneration), pageRoute = window.location?.hash;
    const alive = () => host.isConnected && host.dataset.feedbackOwner === owner && host.dataset.feedbackInstance === instance && window.location?.hash === pageRoute && (!host.closest('dialog') || host.closest('dialog').open);
    const current = seq => alive() && sequence === seq;
    let firstData = initialData;
    const actionSelector = action => action ? `[data-feedback-decide="${action}"]` : '[data-feedback-save]';
    const lockReview = (locked, retryAction = null) => {
      host.querySelectorAll('[data-feedback-note],[data-feedback-reference],[data-feedback-direction],[data-feedback-save],[data-feedback-decide],[data-feedback-draft],[data-feedback-refresh]').forEach(node => {node.disabled = locked;});
      if (locked && retryAction !== null) {const retry = host.querySelector(actionSelector(retryAction));if (retry) retry.disabled = false;}
    };
    host.dataset.feedbackOwner = owner;host.dataset.feedbackInstance = instance;host.dataset.feedbackInteraction = '0';host.classList.add('feedback-chain');
    const reload = async () => {
      if (!alive()) return;
      const seq = ++sequence;
      const hasContent = !!host.querySelector('.feedback-stage');
      host.querySelector('.feedback-load-error')?.remove();
      if (!hasContent) host.innerHTML = '<p class="context-note" role="status">正在读取表现、复盘结论和下一轮记录…</p>';
      if (window.PublicTextMode) {host.innerHTML = '<p class="context-note">复盘结论与下一轮执行请在本机工作台查看。</p>';return;}
      try {
        const data = firstData || await api(requestURL(video));firstData = null;
        // The note sizes each campaign against quiet weeks, which needs the whole calendar (never throws).
        await window.Campaigns?.load();
        if (!current(seq)) return;
        if (data.video_id !== video.video_id || (window.WorkbenchShell?.normalizeProduct?.(data.product) || data.product) !== (window.WorkbenchShell?.normalizeProduct?.(video.product) || video.product)) throw new Error('复盘来源与当前视频不一致。');
        paint(data,seq);
        confirmedSave = false;
        if (current(seq)) await onLoaded?.(data);
      } catch (error) {
        if (!current(seq)) return;
        const failure = `<div class="feedback-load-error"><p class="ai-error" role="alert">${confirmedSave ? '复盘已保存，' : ''}${esc(error.message || '复盘链暂时无法读取。')}${hasContent && !confirmedSave ? ' 输入已保留。' : ''}</p><button type="button" class="secondary compact" data-feedback-load-retry>重新读取复盘链</button></div>`;
        if (hasContent) host.insertAdjacentHTML('afterbegin',failure);else host.innerHTML = failure;
        host.querySelector('[data-feedback-load-retry]').onclick = reload;
      }
    };
    // While the AI drafts, watch only its task and read the whole chain again once it has finished.
    const watch = (taskId,seq) => setTimeout(async () => {
      if (!current(seq)) return;
      if (submitting) return watch(taskId,seq);
      try {const {task} = await api('/api/ai/tasks/'+encodeURIComponent(taskId));if (!current(seq)) return;if (activeTask(task.status)) return watch(taskId,seq);} catch {if (current(seq)) return watch(taskId,seq);return;}
      void reload();
    },5000);
    const paint = (data,seq) => {
      const review = data.review, source = data.source_review, reviewed = review?.state === 'reviewed';
      const unconfirmed = [['',pending.get('save:'+refKey(data))],...Object.keys(decisions).map(action => [action,pending.get('decide:'+refKey(data)+':'+action)])].find(([,body]) => body);
      if (unconfirmed) {editing = true;typed = unconfirmed[1].note;referenceValue = unconfirmed[1].reference_value;directionValue = pendingDirections.get('decide:'+refKey(data)+':'+unconfirmed[0]) ?? null;}
      const open = data.current_window && data.window_end;
      const context = reviewContext(data), canClose = context.closing_allowed === true;
      const link = (kind,id,title,note = '',query = '') => `<a class="feedback-record" href="${esc(url(`#${kind}/${encodeURIComponent(id)}${query}`,data.product))}"><strong>${esc(title)}</strong><small>${esc(note)}</small></a>`;
      const tasks = [...data.retrospectives.map(item => link('ai',item.task_id,item.review_draft ? 'AI 起草的复盘' : item.title,`${labels[item.status] || '查看回复'}${item.review_draft ? '' : !item.explicit_review ? item.window_known ? ' · 已核对冻结视频窗口' : ' · 旧任务未记录观察窗' : ' · 已冻结此复盘来源'}`)),...data.next_tasks.map(item => link('ai',item.task_id,'下一轮写稿任务',labels[item.status] || '查看任务'))];
      const plans = data.plans.map(plan => `<article class="feedback-plan"><div><strong>${esc(plan.direction_id)} · ${esc({execution:'换执行',scale:'加码',stop:'停测'}[plan.action] || '下一轮')}</strong><small>${esc(labels[plan.status] || plan.status)}${plan.review_current ? '' : ' · 保留发起时结论'}</small><p>${esc(plan.note || plan.source_review.note)}</p>${plan.review_current ? '' : `<details><summary>查看冻结的复盘结论</summary><p>${esc(plan.source_review.note)}</p><small>${esc(plan.source_review.reviewed_at)}</small></details>`}</div><div class="feedback-plan-actions"><a class="subtle" href="${esc(url(`#directions/${encodeURIComponent(plan.direction_id)}`,data.product))}">查看方向</a>${!['done','cancelled'].includes(plan.status) && plan.action !== 'stop' ? `<button type="button" class="secondary compact" data-feedback-write="${esc(plan.id)}">${plan.task_id ? '再写一轮' : '按计划写稿'}</button>` : ''}</div></article>`);
      const scripts = [...data.source_scripts.map(item => link('scripts',item.id,`已发布使用脚本 · 第 ${item.version || 1} 版`,item.title)),...data.next_scripts.map(item => link('scripts',item.id,`下一轮脚本 · 第 ${item.version || 1} 版`,item.title))];
      const outputs = [...data.source_outputs.map(item => link('production',item.id,`已发布${item.kind === 'carousel' ? '图文' : '视频'}制作单`,item.title,item.kind === 'carousel' ? '?step=delivery' : '?step=publish')),...data.next_outputs.map(item => link('production',item.id,`下一轮${item.kind === 'carousel' ? '图文' : '视频'}制作单`,item.title))];
      const variants = data.variants.map(item => `<a class="feedback-record" href="${esc(url(`#directions/${encodeURIComponent(item.direction_id)}?variant=${encodeURIComponent(item.key)}`,data.product))}"><strong>下一轮执行变体 ${item.index + 1}</strong><small>${esc(item.direction_id)} · ${esc(item.snapshot?.execution_change || item.snapshot?.progression || '查看全部输出')}</small></a>`);
      const cases = data.cases.map(item => link('case',item.id,'关联原案例',item.title));
      const records = [...tasks,...variants,...scripts,...outputs,...cases];
      const draftText = data.draft?.draft?.conclusion || '';
      // A draft written after the saved conclusion fills the field; otherwise the saved conclusion does.
      const draftNewer = draftText && (!review?.note || String(data.draft.created_at || '') > String(review.at || ''));
      // Unsaved typing survives closing the panel, unless a newer AI draft has arrived since.
      const kept = keptNote(data), keptWins = kept && !(draftText && Date.parse(data.draft.created_at || '') > kept.at);
      const note = typed ?? (keptWins ? kept.text : draftNewer ? draftText : review?.note || draftText || '');
      // The draft's recommended class preselects the field like its conclusion does; saving stays the person's call.
      const suggested = draftNewer && Object.hasOwn(referenceNames,data.draft.draft.reference_value || '') ? data.draft.draft.reference_value : '';
      const suggestion = suggested ? `<small class="feedback-reference-ai">AI 推荐：${esc(referenceNames[suggested])}${data.draft.draft.reference_reason ? `（${esc(data.draft.draft.reference_reason)}）` : ''}</small>` : '';
      const referencePicker = `<select data-feedback-reference aria-label="参考价值">${options({...referenceNames,'':'请选择参考价值'},referenceValue ?? (suggested || review?.reference_value || ''))}</select>`;
      // Reviewing before 14 days or with data gaps is allowed; say what the conclusion cannot rest on yet.
      const earlyWarning = !canClose ? `<p class="feedback-early-warning" role="note">${esc(context.early_warning || context.evidence?.reason || '数据还不完整，结论按已有数据下，之后可再改。')}</p>` : '';
      const directionLine = data.summary?.direction_id ? `<p class="feedback-direction">下一轮方向 <strong>${esc(data.summary.direction_id)}</strong></p>` : `<label class="field feedback-direction"><span>下一轮方向（加码、换执行时需要）</span><select data-feedback-direction><option value="">请选择方向</option></select></label>`;
      const continuationHint = onSaved && data.current_window === true && manualReviewStatus(data).key === 'pending' ? '<small class="feedback-next-hint">保存后打开下一条</small>' : '';
      const noteId = `feedback-note-${instance}`;
      const decide = !open ? `<p class="context-note">历史观察窗只能查看。</p>` : `<section class="feedback-decide">${earlyWarning}<div class="feedback-note-head"><label for="${noteId}">结论</label><span class="feedback-draft">${draftActions(data)}</span></div><textarea id="${noteId}" rows="3" maxlength="4000" data-feedback-note placeholder="一两句：这条为什么好或不好，保留或避开什么。">${esc(note)}</textarea>${draftCard(data)}<div class="feedback-reference feedback-save-row">${referencePicker}<button type="button" class="primary compact" data-feedback-save>保存复盘</button></div>${suggestion || continuationHint ? `<p class="feedback-save-notes">${suggestion}${continuationHint}</p>` : ''}<p class="ai-error" role="alert" data-feedback-error hidden></p><details class="feedback-more-actions"><summary>更多操作</summary><div class="feedback-direction-details">${directionLine}</div><div class="feedback-decisions" role="group" aria-label="可选操作">${Object.entries(decisions).map(([key,[label]]) => `<button type="button" class="secondary compact" data-feedback-decide="${key}">${label}</button>`).join('')}</div></details></section>`;
      const canUseExperiences = !window.PublicTextMode && !window.WorkbenchGuest && !window.GuestMode;
      const experiences = data.experiences || [];
      const experienceActions = canUseExperiences ? `${source ? '<button type="button" class="secondary compact" data-feedback-experience>记一条经验</button>' : ''}${experiences.length ? `<button type="button" class="subtle compact" data-feedback-experiences>查看经验（${experiences.length}）</button>` : ''}` : '';
      const done = `<section class="feedback-conclusion"><div class="feedback-conclusion-head"><strong>已复盘</strong>${savedDecision(review?.decision) ? chip(savedDecision(review.decision)) : ''}</div><div class="feedback-reference-saved"><span>参考价值：<strong data-feedback-reference-label>${esc(manualReviewStatus(data).label)}</strong></span><button type="button" class="subtle compact" data-feedback-classify>${review?.reference_value ? '改分类' : '分类'}</button></div><p>${esc(review?.note || '')}</p><small>${esc(review?.at || '')}</small>${open || experienceActions ? `<div class="feedback-actions">${open ? '<button type="button" class="secondary compact" data-feedback-redo>编辑复盘</button>' : ''}${source && canClose && open ? '<button type="button" class="subtle compact" data-feedback-plan>再安排一轮</button>' : ''}${experienceActions}</div>` : ''}</section>`;
      const originalURL = tiktokLink(data.video_id,data.account || video.account);
      const sourceRow = externalMetrics ? '' : `<div class="section-title-row feedback-where feedback-source"><p class="context-note">视频 ID <span>${esc(data.video_id)}</span>${typeof copyIdButton === 'function' ? copyIdButton(data.video_id,'视频 ID') : ''} · <a class="feedback-tiktok" href="${esc(originalURL)}" target="_blank" rel="noopener noreferrer" title="在 TikTok 打开这条视频">TikTok 原视频 ↗</a>${typeof copyIdButton === 'function' ? copyIdButton(originalURL,'原视频链接') : ''}</p></div>`;
      const refresh = '<button type="button" class="subtle compact" data-feedback-refresh>刷新</button>', where = `观察窗${data.window_end ? '到 ' + esc(data.window_end) : '未记录'}${data.current_window ? '' : ' · 历史观察窗'}`;
      const evidenceDetails = `<details class="feedback-data-details"><summary>数据与依据</summary><p class="context-note">${where}</p>${contextBlock(context)}${externalMetrics ? '' : window.Campaigns?.reviewNote(data.campaigns) || ''}${data.funnel ? funnelChart(data.funnel,data.summary) + figures(data.summary,true) : figures(data.summary,sameMetricScope(data,externalMetrics))}</details>`;
      host.innerHTML = `${sourceRow}<div class="section-title-row">${heading ? '<h3>复盘</h3>' : ''}<p class="feedback-stage" role="status">${esc(stageLine(data,context,reviewed && !editing))}</p>${refresh}</div>${reviewed && !editing ? done : decide}${evidenceDetails}${plans.length ? `<section><h4>由这次复盘安排的下一轮</h4>${plans.join('')}</section>` : ''}${records.length ? `<details class="feedback-output-history"><summary>相关记录 · ${records.length}</summary><div class="feedback-records">${records.join('')}</div></details>` : ''}`;
      paintBars(host);
      host.querySelectorAll('[data-feedback-redo],[data-feedback-classify],[data-feedback-plan],[data-feedback-experience],[data-feedback-experiences],[data-feedback-write],[data-feedback-refresh]').forEach(button => button.addEventListener('click',() => {host.dataset.feedbackInteraction = String(Number(host.dataset.feedbackInteraction || 0) + 1);(host.closest('dialog') || host).querySelector('.feedback-next-status')?.remove();},{capture:true}));
      const changed = async () => {if (!alive()) return;await reload();if (alive()) await onChanged?.();};
      host.querySelector('[data-feedback-refresh]').onclick = reload;
      host.querySelector('[data-feedback-redo]')?.addEventListener('click',() => {editing = true;typed = null;referenceValue = null;directionValue = null;paint(data,seq);});
      host.querySelector('[data-feedback-classify]')?.addEventListener('click',() => form({key:'classify:'+refKey(data),title:'参考价值分类',body:`<label class="field"><span>人工复盘后的参考价值</span><select name="reference_value">${options(referenceNames,review?.reference_value || '')}</select></label>`,submitLabel:'保存分类',path:'/api/review-followups',build:values => ({revision:data.review_revision,product:data.product,video_id:data.video_id,window_end:data.window_end,classification_only:true,reference_value:values.get('reference_value')}),validate:result => result.entry?.state === 'reviewed',onSaved:changed}));
      host.querySelector('[data-feedback-plan]')?.addEventListener('click',() => planForm(data,changed));
      host.querySelector('[data-feedback-experience]')?.addEventListener('click',() => window.ReviewExperiences?.fromReview(data,{onSaved:changed}));
      host.querySelector('[data-feedback-experiences]')?.addEventListener('click',() => window.ReviewExperiences?.open({product:data.product,q:data.video_id,status:'all'}));
      const noteField = host.querySelector('[data-feedback-note]');
      // The field grows with its text, so a drafted conclusion is read whole rather than scrolled.
      const fitNote = () => {if (!noteField?.isConnected) return;noteField.style.height = 'auto';noteField.style.height = `${noteField.scrollHeight + 2}px`;};
      if (noteField) {
        noteField.oninput = () => {typed = noteField.value;keepNote(data,noteField.value);fitNote();};
        // Drawers open and resize after paint; refit whenever the line length changes.
        let width = 0;
        if (window.ResizeObserver) new ResizeObserver(entries => {const next = Math.round(entries[0].contentRect.width);if (next !== width) {width = next;fitNote();}}).observe(noteField);
        setTimeout(fitNote);
      }
      const referenceSelect = host.querySelector('[data-feedback-reference]');
      if (referenceSelect) referenceSelect.onchange = () => {referenceValue = referenceSelect.value;};
      if (activeTask(data.draft?.status)) watch(data.draft.task_id,seq);
      const directionSelect = host.querySelector('[data-feedback-direction]');
      if (directionSelect) directionSelect.onchange = () => {directionValue = directionSelect.value;};
      if (directionSelect) api('/api/direction-catalog?product=' + encodeURIComponent(data.product)).then(catalog => {
        if (!current(seq)) return;
        directionSelect.insertAdjacentHTML('beforeend',(catalog.directions || []).filter(row => row.status !== 'archived').map(row => `<option value="${esc(row.direction_id)}">${esc(row.direction_id)} · ${esc(row.main_difference || '')}</option>`).join(''));
        if (directionValue !== null) directionSelect.value = directionValue;
      }).catch(() => {});
      host.querySelectorAll('[data-feedback-draft]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {
          await api('/api/ai/tasks',{method:'POST',body:{request_id:crypto.randomUUID(),product:data.product,action:'retrospective',instruction:'起草这条视频的复盘结论；仅在必要时建议下一轮动作，由人工决定是否采用。',review_video:{product:data.product,video_id:data.video_id,window_end:data.window_end},include_basket:false,skill_selection:'auto'}});
          typed = noteField?.value.trim() ? noteField.value : null;
          await reload();
        } catch (error) {toast(error.message || 'AI 起草暂时无法开始，请稍后重试。',true);if (button.isConnected) button.disabled = false;}
      });
      host.querySelectorAll('[data-feedback-decide]').forEach(button => button.onclick = () => choose(data,button.dataset.feedbackDecide,changed));
      host.querySelector('[data-feedback-save]')?.addEventListener('click',() => choose(data,'',changed));
      if (unconfirmed) {
        lockReview(true,unconfirmed[0]);
        const error = host.querySelector('[data-feedback-error]');
        if (error) {error.hidden=false;error.textContent='上次保存结果尚未确认，再点同一个按钮会核对同一请求。';}
        if (unconfirmed[0]) host.querySelector('.feedback-more-actions').open = true;
      }
      host.querySelectorAll('[data-feedback-write]').forEach(button => button.onclick = async () => {
        const plan = data.plans.find(row => row.id === button.dataset.feedbackWrite);
        try {const catalog = await api('/api/direction-catalog?product=' + encodeURIComponent(data.product)), direction = catalog.directions?.find(row => row.direction_id === plan.direction_id);if (!direction) throw new Error('计划方向已不存在，请核对来源。');await window.CaseAI?.startFromDirection({...direction,plan_id:plan.id,plan_note:plan.note || plan.source_review.note});} catch (error) {toast(error.message,true);}
      });
    };
    // Saving a review never chooses a future action; only explicit optional actions create plans.
    const choose = async (data,action,changed) => {
      if (submitting || confirmedSave) return;
      const error = host.querySelector('[data-feedback-error]'), noteField = host.querySelector('[data-feedback-note]'), referenceField = host.querySelector('[data-feedback-reference]');
      const key = action ? 'decide:' + refKey(data) + ':' + action : 'save:' + refKey(data), frozen = pending.get(key);
      const interaction = host.dataset.feedbackInteraction;
      const shouldContinue = frozen ? pendingContinuations.get(key) === true : !action && data.current_window === true && manualReviewStatus(data).key === 'pending';
      const directionId = frozen ? pendingDirections.get(key) || '' : data.summary?.direction_id || host.querySelector('[data-feedback-direction]')?.value || '';
      error.hidden = true;
      if (action && !decisions[action]) return;
      const needsPlan = ['scale','execution'].includes(action);
      if (!frozen && needsPlan && !directionId) {error.hidden = false;error.textContent = '先选下一轮方向，再选加码或换执行。';const details=host.querySelector('.feedback-more-actions');if (details) details.open=true;host.querySelector('[data-feedback-direction]')?.focus();return;}
      const written = noteField.value.trim(), reference = referenceField?.value || '';
      if (!frozen && !action && !written) {error.hidden=false;error.textContent='先写一句复盘结论。';noteField.focus();return;}
      if (!frozen && !action && (!reference || !Object.hasOwn(referenceNames,reference))) {error.hidden=false;error.textContent='请选择参考价值，再保存复盘。';referenceField?.focus();return;}
      if (!frozen) {
        pending.set(key,{request_id:crypto.randomUUID(),revision:data.review_revision,product:data.product,video_id:data.video_id,window_end:data.window_end,state:'reviewed',...(action ? {decision:action} : {}),
          reference_value:reference,note:written || data.draft?.draft?.conclusion || `复盘决定：${decisions[action][0]}`});
        if (needsPlan) pendingDirections.set(key,directionId);
        pendingContinuations.set(key,shouldContinue);
      }
      submitting = true;lockReview(true);
      let savedEntry;
      try {
        const result = await api('/api/review-followups',{method:'POST',body:pending.get(key)});
        if (result.entry?.state !== 'reviewed') throw new Error('未收到完整保存结果，请重试核对。');
        savedEntry = result.entry;
        keepNote(data,'');
        pending.delete(key);
        pendingDirections.delete(key);
        pendingContinuations.delete(key);
        confirmedSave = true;
        const saveButton = alive() && host.querySelector('[data-feedback-save]');if (saveButton) saveButton.textContent = '已保存';
      } catch (failure) {
        if (failure.status && failure.status < 500) {pending.delete(key);pendingDirections.delete(key);pendingContinuations.delete(key);}
        if (!alive()) {submitting = false;return;}
        error.hidden = false;error.textContent = (failure.message || '复盘暂未保存。') + (pending.has(key) ? ' 结果尚未确认，再点同一个按钮会核对同一请求。' : '');
        lockReview(pending.has(key),pending.has(key) ? action : null);submitting = false;
        return;
      }
      if (needsPlan) {
        try {
          const fresh = await api(requestURL(data)), revision = (await api('/api/creation?product=' + encodeURIComponent(data.product))).revision;
          if (!fresh.source_review) throw new Error('复盘结论还没读到');
          await api('/api/plans',{method:'POST',body:{request_id:crypto.randomUUID(),revision,plan:{product:data.product,direction_id:directionId,action,status:'pending',note:fresh.source_review.note,source_review:fresh.source_review}}});
        } catch (failure) {toast(`复盘已保存，但下一轮计划没建成：${failure.message || '请稍后重试'}。可点「再安排一轮」补上。`,true);}
      }
      editing = false;typed = null;referenceValue = null;directionValue = null;
      try {await changed();} catch (failure) {toast(`复盘已保存，列表暂未刷新：${failure.message || '请稍后刷新'}`,true);} finally {submitting = false;}
      if (shouldContinue && alive() && host.dataset.feedbackInteraction === interaction) await onSaved?.({video:data,entry:savedEntry});
    };
    void reload();return {refresh:reload};
  }
  function continueAfterSave({host,product,onNext,isCurrent} = {}) {
    let sequence = 0;
    return async event => {
      const seq = ++sequence, owner = host?.dataset.feedbackOwner, instance = host?.dataset.feedbackInstance, interactionHost = host?.matches('.feedback-chain') ? host : host?.querySelector('.feedback-chain'), interaction = interactionHost?.dataset.feedbackInteraction, pageRoute = window.location?.hash;
      const current = () => host?.isConnected && sequence === seq && host.dataset.feedbackOwner === owner && host.dataset.feedbackInstance === instance && interactionHost?.dataset.feedbackInteraction === interaction && window.location?.hash === pageRoute && (!host.closest('dialog') || host.closest('dialog').open) && (!isCurrent || isCurrent());
      if (!current()) return;
      let busy = false;
      const show = (message,retry = false) => {
        if (!current()) return;
        host.querySelector('.feedback-next-status')?.remove();
        host.insertAdjacentHTML('afterbegin',`<section class="feedback-next-status" role="status"><p>${esc(message)}</p>${retry ? '<button type="button" class="secondary compact" data-feedback-next-retry>重试打开下一条</button>' : ''}</section>`);
        host.querySelector('[data-feedback-next-retry]')?.addEventListener('click',() => {void run();});
      };
      const run = async () => {
        if (busy || !current()) return;
        busy = true;show('复盘已保存，正在打开下一条…');
        try {
          if (!window.ReviewFollowups?.nextReady) throw new Error('下一条暂时无法读取');
          const next = await window.ReviewFollowups.nextReady({product,exclude:event.video});
          if (!current()) return;
          if (!next.video) {show('当前范围内，今天可复盘的视频已全部完成。');return;}
          await onNext?.(next.video,{isCurrent:current});
        } catch (error) {
          show('复盘已保存，下一条暂时无法读取。',true);
        } finally {busy = false;}
      };
      await run();
    };
  }
  function form({key,title,body,submitLabel,path,build,onSaved,validate}) {
    if (document.querySelector('.feedback-form-dialog[open]')) return;
    const dialog = document.createElement('dialog');dialog.className = 'feedback-form-dialog';dialog.setAttribute('aria-label',title);
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${esc(title)}</h2><button type="button" class="subtle" data-close>关闭</button></div><div class="feedback-fields">${body}<p class="ai-error" role="alert" data-error hidden></p></div><div class="dialog-footer"><span>核对内容后点击按钮才会保存或发起任务。</span><button type="submit" class="primary">${esc(submitLabel)}</button></div></form>`;
    document.body.append(dialog);let busy = false;const node = dialog.querySelector('form'), error = dialog.querySelector('[data-error]');
    const lock = () => {const waiting = busy || pending.has(key);node.querySelectorAll('input,textarea,select').forEach(item => item.disabled = waiting);node.querySelector('[type=submit]').disabled = busy;};
    dialog.querySelector('[data-close]').onclick = () => {if (!busy) dialog.close();};dialog.addEventListener('cancel',event => {if (busy) event.preventDefault();});dialog.addEventListener('close',() => dialog.remove(),{once:true});
    node.onsubmit = async event => {
      event.preventDefault();if (busy) return;
      busy = true;error.hidden = true;
      try {
        if (!pending.has(key)) pending.set(key,{request_id:crypto.randomUUID(),...await build(new FormData(node))});lock();
        const result = await api(path,{method:'POST',body:pending.get(key)});
        if (validate && !validate(result)) throw new Error('未收到完整保存结果，请重试同一请求核对。');
        pending.delete(key);dialog.close();
        try {await onSaved(result);} catch (failure) {toast(`操作已保存，关联暂时无法刷新：${failure.message || '请重新读取'}`,true);}
      } catch (failure) {
        if (failure.status && failure.status < 500) pending.delete(key);
        error.hidden = false;error.textContent = (failure.message || '操作暂未完成。') + (pending.has(key) ? ' 结果尚未确认，重试会核对同一请求。' : failure.status === 409 ? ' 输入已保留，请关闭后刷新来源再核对。' : '');
      } finally {busy = false;if (dialog.isConnected) lock();}
    };
    const saved = pending.get(key);
    if (saved) {for (const field of node.querySelectorAll('[name]')) {const value = (saved.plan || saved)[field.name];if (value !== undefined && typeof value !== 'object') field.value = value;}const frozen = saved.plan?.source_review || saved.source_review;if (frozen && node.querySelector('blockquote')) node.querySelector('blockquote').textContent = frozen.note;error.hidden = false;error.textContent = '上次提交尚未确认；再次点击会核对同一请求。';}
    dialog.showModal();lock();return dialog;
  }
  async function planForm(data,onSaved) {
    try {
      const catalog = await api('/api/direction-catalog?product=' + encodeURIComponent(data.product));
      const directions = (catalog.directions || []).filter(row => row.status !== 'archived');
      if (!directions.length) {toast('当前产品尚无可用方向，请先登记下一轮方向。',true);return;}
      const dialog = form({key:'plan:'+refKey(data),title:'把复盘结论安排到下一轮',body:`<blockquote>${esc(data.source_review.note)}</blockquote><label class="field"><span>下一轮方向</span><select name="direction_id" required><option value="">请选择本轮实际方向</option>${directions.map(row => `<option value="${esc(row.direction_id)}">${esc(row.direction_id)} · ${esc(row.main_difference)}</option>`).join('')}</select></label><label class="field"><span>所属执行变体（可稍后确认）</span><select name="variant"><option value="">暂不指定</option></select></label><label class="field"><span>安排</span><select name="action"><option value="execution">换执行</option><option value="scale">加码</option><option value="stop">停测</option></select></label><label class="field"><span>下一轮要改什么、验证什么</span><textarea name="note" rows="4" maxlength="12000" required>${esc(data.source_review.note)}</textarea></label>`,submitLabel:'保存下一轮计划',path:'/api/plans',build:async values => {
        const direction = directions.find(row => row.direction_id === values.get('direction_id'));
        if (!direction) throw new Error('请选择本轮实际方向。');
        const index = values.get('variant') === '' ? null : Number(values.get('variant')), variant = index === null ? null : direction.variants[index];
        const revision = (await api('/api/creation?product=' + encodeURIComponent(data.product))).revision;
        return {revision,plan:{product:data.product,direction_id:direction.direction_id,action:values.get('action'),status:'pending',note:values.get('note').trim(),source_review:data.source_review,...(variant ? {variant_ref:{index,snapshot:Object.fromEntries(['progression','baseline','variant_type','main_difference','concern','closing','execution_change'].filter(key => key in variant).map(key => [key,variant[key]]))}} : {})}};
      },validate:result => !!result.plan?.id,onSaved});
      if (dialog) {
        const fillVariants = id => {
          const direction = directions.find(row => row.direction_id === id), select = dialog.querySelector('[name=variant]');select.innerHTML = '<option value="">暂不指定</option>' + (direction?.variants || []).map((variant,index) => variant.conflict ? '' : `<option value="${index}">变体 ${index + 1} · ${esc(variant.execution_change || variant.progression || '已登记变体')}</option>`).join('');return select;
        };
        dialog.querySelector('[name=direction_id]').onchange = event => fillVariants(event.target.value);
        const saved = pending.get('plan:'+refKey(data))?.plan;
        if (saved) {
          const directionSelect = dialog.querySelector('[name=direction_id]');if (![...directionSelect.options].some(option => option.value === saved.direction_id)) directionSelect.add(new Option(saved.direction_id+' · 保存时方向',saved.direction_id));directionSelect.value = saved.direction_id;
          const select = fillVariants(saved.direction_id), ref = saved.variant_ref;
          if (ref) {const option = select.querySelector(`option[value="${ref.index}"]`);if (option) option.textContent = `变体 ${ref.index + 1} · ${ref.snapshot.execution_change || ref.snapshot.progression || '保存时的变体'}`;else select.add(new Option(`变体 ${ref.index + 1} · 保存时的变体`,String(ref.index)));select.value = String(ref.index);}
        }
      }
    } catch (error) {toast(error.message,true);}
  }
  // The AI task page shows the same chart and card above the full analysis.
  async function mountTaskCard(host, task) {
    const video = task?.context?.review_video;
    if (!host || !video?.video_id || !video.product || window.PublicTextMode) return;
    try {
      const data = await api(requestURL(video));
      if (!host.isConnected) return;
      const own = data.retrospectives?.find(item => item.task_id === task.task_id)?.draft;
      const html = funnelChart(data.funnel,data.summary) + (own?.card ? retroCard(own.card,decisions[own.action]) : '');
      if (host.dataset.source === html) return;
      (window.keepReadingPlace || (change => change()))(() => {host.innerHTML = html;paintBars(host);host.hidden = !html;host.dataset.source = html;});
    } catch {/* The full analysis below stays readable without the chart. */}
  }
  return {mount,mountTaskCard,sourceLink,sourceURL,sameMetricScope,manualReviewStatus,continueAfterSave};
})();
