'use strict';

// Production jobs: pick a voiced variant, keep a voice library and generate timed voiceovers.
window.CaseProduction = (() => {
  const statusLabels = {draft:'待配音',voiced:'已配音',timeline:'已建时间线',editing:'剪辑中',awaiting_review:'待人工审查',approved:'已完成',needs_material:'待补素材',needs_verification:'素材待核实',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',completed:'旧记录待核验',failed:'剪辑失败',cancelled:'剪辑已停止',interrupted:'剪辑已中断'};
  const frameRates = ['23.976','24','25','29.97','30','50','59.94','60'];
  const editModes = {explain:'解释画面逻辑（默认）',sync:'音画同步',montage:'主题蒙太奇',replica:'1:1 复刻原片'};
  // Only a job made from a case's video has the original's shots to copy, and for that job copying is the default.
  const jobEditModes = job => Object.entries(editModes).filter(([key]) => key !== 'replica' || job.replica);
  const defaultEditMode = job => job.replica ? 'replica' : 'explain';
  const editModels = {'claude-default':'Claude 客户端默认','claude-opus':'Claude Opus','claude-sonnet':'Claude Sonnet'};
  const editModelNames = new Map([...Object.entries(editModels),['codex-default','Codex 客户端默认']]);
  const editProvider = model => String(model || '').startsWith('claude-') ? 'claude' : 'codex';
  const editClient = provider => provider === 'codex' ? 'Codex 客户端' : 'Claude Code 客户端';
  const editModelLabel = model => editModelNames.get(model || 'codex-default') || model;
  const editLabels = {running:'运行中',completed:'已完成',awaiting_review:'待人工审查',approved:'已完成',needs_material:'待补素材',needs_verification:'素材待核实',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',failed:'失败',cancelled:'已停止',interrupted:'已中断'};
  const timelineTaskLabels = {pending:'待剪辑',in_progress:'剪辑中',completed:'已完成'};
  const activeTimelines = job => (job.timelines || []).filter(item => !item.archived);
  const skillDelivery = edit => edit?.completion_policy === 'skill_delivery';
  const gapStatus = gap => gap?.status === 'unverified' ? 'unverified' : 'missing';
  const deliveryGapState = report => Array.isArray(report?.gaps) && report.gaps.length ? report.gaps.some(gap => gapStatus(gap) === 'missing') ? 'needs_material' : 'needs_verification' : '';
  const coverageReview = edit => skillDelivery(edit) && edit.validation?.scope === 'current_coverage';
  const skillReviewable = edit => ['completed','awaiting_review'].includes(edit.status) &&
    (Object.hasOwn(edit,'execution_status') ? edit.execution_status : edit.status === 'completed' ? 'completed' : null) === 'completed' && !edit.result_pending &&
    (edit.validation ? coverageReview(edit) && edit.validation.status === 'awaiting_review' && edit.validation.saved === true && Array.isArray(edit.summary?.picture_gaps) && !edit.summary.picture_gaps.length
      : edit.report?.saved === true && !deliveryGapState(edit.report));
  const editState = edit => edit.status === 'completed' ? (skillDelivery(edit) ? (edit.report?.saved !== true ? 'needs_repair' : deliveryGapState(edit.report) || (edit.approval ? 'approved' : 'awaiting_review')) : 'unverified') : edit.status === 'awaiting_review' && edit.approval ? 'approved' : edit.status;
  const manualScope = ['画面内容与口播是否匹配','镜头时长例外与视觉连贯性','字幕、声音听审和成片导出'];
  // Only the latest attempt on a timeline of the selected voiceover decides the job, so only it can be completed.
  const currentEdit = (job,edit) => {
    const timeline = (job.timelines || []).find(item => item.unique_id === edit.timeline_uid);
    const latest = [...(job.edits || [])].reverse().find(item => item.timeline_uid === edit.timeline_uid);
    return Boolean(timeline && !timeline.archived && job.selected_take && timeline.take_id === job.selected_take && latest?.id === edit.id);
  };
  // A timeline that already holds a cut, finished or under way: cutting on it again would build on that work.
  const timelineHasCut = (job,uid,edits = job.edits || []) => Boolean(timelineReceipt(job,timelineOf(job,uid))) || edits.some(edit => edit.timeline_uid === uid && !['failed','cancelled'].includes(edit.status));
  const completionTimeline = job => [...activeTimelines(job)].reverse().find(item => job.selected_take && item.take_id === job.selected_take);
  const productionTitle = job => job.display_title || job.title || job.variant_label || '未命名制作单';
  // Keep only the exact variant fragments that the heading has not already named.
  const variantSubtitle = (title,label) => {
    const named = String(title || '').split(/[｜|·]/u).map(part => part.trim());
    return String(label || '').split(' · ').filter(part => part && !named.some(name =>
      name === part || /^变体\s*#?\d+$/.test(part) && name.endsWith(` ${part}`))).join(' · ');
  };
  const currentEditResult = (job,choice) => [...(job.edits || [])].reverse().find(edit => currentEdit(job,edit) && edit.status !== 'running' && (!choice.edit_timeline || edit.timeline_uid === choice.edit_timeline));
  const timelineOf = (job,uid) => (job.timelines || []).find(item => item.unique_id === uid);
  // Each timeline carries its own manual completion (the server sends it only while it holds), so one order can have several.
  const timelineReceipt = (job,timeline) => {
    if (!timeline || timeline.archived || !job.selected_take || timeline.take_id !== job.selected_take) return null;
    if (Object.hasOwn(timeline,'manual_completion')) return timeline.manual_completion || null;
    // A response that carries only the order's single receipt: it belongs to the timeline it names.
    const receipt = job.manual_completion;
    return job.status === 'approved' && receipt?.at && receipt.take_id === job.selected_take && receipt.timeline_uid === timeline.unique_id ? receipt : null;
  };
  const manualCompletion = (job,timeline = completionTimeline(job)) => timelineReceipt(job,timeline);
  const manualCompleted = job => activeTimelines(job).some(item => timelineReceipt(job,item));
  const editCompleted = (job,edit) => Boolean(timelineReceipt(job,timelineOf(job,edit.timeline_uid)));
  const foldEditControls = (job,choice) => Boolean((currentEditResult(job,choice) ||
    (choice.edit_timeline ? manualCompletion(job,timelineOf(job,choice.edit_timeline)) : manualCompleted(job))) &&
    !(job.edits || []).some(edit => edit.status === 'running') && !choice.edit_draft && !pendingTimelineId(choice.edit_timeline));
  const takeSelectable = (take,count) => Array.isArray(take.groups) && take.groups.length === count && count > 0 && take.groups.every((group,index) =>
    group.number === index + 1 &&
    typeof group.start === 'number' && Number.isFinite(group.start) && group.start >= 0 &&
    typeof group.end === 'number' && Number.isFinite(group.end) && group.end > group.start &&
    (index === 0 || group.start >= take.groups[index - 1].end) &&
    (take.duration === null || take.duration === undefined || (typeof take.duration === 'number' && Number.isFinite(take.duration) && group.end <= take.duration + .1)) &&
    typeof group.coverage === 'number' && Number.isFinite(group.coverage) && group.coverage >= .6 && group.coverage <= 1);
  const today = () => {const date = new Date();return `${String(date.getFullYear()).slice(2)}${String(date.getMonth() + 1).padStart(2,'0')}${String(date.getDate()).padStart(2,'0')}`;};
  const sourceLabels = {tts:'配音自带时间',tts_normalized:'配音时间（规范化文本）',forced:'强制对齐','':'未取得'};
  const settingLabels = {stability:'稳定度',similarity_boost:'相似度',style:'风格',speed:'语速'};
  const ranges = {stability:[0,1,.05,.5],similarity_boost:[0,1,.05,.75],style:[0,1,.05,0],speed:[.7,1.2,.01,1]};
  const view = {sequence:0,modal:null,pending:new Map(),choice:{}};
  const url = (hash,product) => window.WorkbenchShell?.url(hash,product) || hash;
  const currentProduct = () => window.WorkbenchShell?.product() || '';
  const pageHash = () => window.WorkbenchShell?.legacyHash() || location.hash;
  const activitySteps = {validation:'edit'};
  function mountActivityHost(host,options) {
    window.TaskActivity.mountLogs(host,options);
    const details = $('details',host);if (!details) return;
    const key = `${options.owner_id}:${options.source_id || 'all'}`;
    const opened = view.activityLogsOpen ||= new Map();
    details.addEventListener('toggle',() => {if (details.isConnected) opened.set(key,details.open);});
    if (opened.get(key)) details.open = true;
  }
  function mountActivityLogs(ownerId) {
    if (!window.TaskActivity) return;
    const requested = new URLSearchParams(pageHash().split('?').slice(1).join('?')).get('run');
    // A card keeps its log line only while its run is going or needs someone, unless that run was asked for.
    $$('[data-activity-source]',$('#content')).forEach(card => {
      if ($('.task-activity-logs',card)) return;
      const host = document.createElement('div');host.className = 'task-activity-logs';card.append(host);
      mountActivityHost(host,{owner_id:ownerId,source_id:card.dataset.activitySource,quiet:card.dataset.activitySource !== requested});
    });
    if (!$('#production-activity-logs')) {
      const host = document.createElement('div');host.id = 'production-activity-logs';host.className = 'task-activity-logs';
      $('.page-heading',$('#content'))?.after(host);
      const hasCard = requested && $$('[data-activity-source]',$('#content')).some(node => node.dataset.activitySource === requested);
      mountActivityHost(host,{owner_id:ownerId,...(requested && !hasCard ? {source_id:requested} : {})});
    }
  }
  function applyActivityTarget(id, hash) {
    if (view.activityTarget === hash) return;
    const params = new URLSearchParams(hash.split('?').slice(1).join('?')), run = params.get('run');
    const step = activitySteps[params.get('step')] || params.get('step');
    if (step) (view.choice[id] ||= {}).step = step;
    const fields = {};
    for (const key of ['line','gap','question','start','end']) if (/^\d+$/.test(params.get(key) || '')) fields[key] = Number(params.get(key));
    view.pendingActivityTarget = {hash,run,step,...fields};
  }
  function focusActivityTarget() {
    const target = view.pendingActivityTarget;if (!target) return;
    view.pendingActivityTarget = null;view.activityTarget = target.hash;
    if (!target.run && !target.step && target.line === undefined) return;
    const card = $$('[data-activity-source]',$('#content')).find(node => node.dataset.activitySource === target.run);
    const panel = card?.closest('[data-step-panel]');
    if (panel?.hidden) $$('[data-step]',$('.production-steps')).find(button => button.dataset.step === panel.dataset.stepPanel)?.click();
    let specific = null, expected = target.gap !== undefined || target.question !== undefined || target.line !== undefined;
    if (target.gap !== undefined && card) specific = $$('[data-edit-gap]',card).find(node => Number(node.dataset.editGap) === target.gap && (target.start === undefined || Number(node.dataset.gapStart) === target.start) && (target.end === undefined || Number(node.dataset.gapEnd) === target.end));
    else if (target.question !== undefined && card) specific = $$('[data-edit-question]',card).find(node => Number(node.dataset.editQuestion) === target.question);
    else if (target.line !== undefined) specific = $(`#job-script-panel [data-production-line="${target.line}"]`);
    if (expected && !specific) {
      let note = $('#production-target-note');if (!note) {note = document.createElement('p');note.id = 'production-target-note';note.className = 'context-note production-target-note';($('#workflow-links') || $('.page-heading')).after(note);}
      note.textContent = '对应的缺口或内容已变化，请重新核对当前待处理清单。';
    }
    const node = specific || card || (target.run ? $('#production-activity-logs') : null) || $$('[data-step-panel]',$('#content')).find(node => !node.hidden);
    if (node?.matches?.('details')) node.open = true;
    if (specific) specific.classList.add('production-target');
    for (let ancestor = node?.parentElement;ancestor && ancestor !== $('#content');ancestor = ancestor.parentElement) if (ancestor.matches('details')) ancestor.open = true;
    if (node) {if (!card && target.run) {const details = $('details',node);if (details) details.open = true;}node.tabIndex = -1;node.focus({preventScroll:true});node.scrollIntoView({block:'start'});}
  }
  const fail = error => error?.message || '操作暂未完成';
  function mountProductionNeeds(data,kind) {
    const record = data.job;if (!record) return;
    let host = $('#production-needs');
    if (!host) {host = document.createElement('section');host.id = 'production-needs';host.className = 'section-card';($('#workflow-links') || $('.page-heading')).after(host);}
    const publication = view.progress?.id === record.id ? view.progress.publication : undefined;
    window.ProductionNeeds?.mount(host,{record:publication === undefined ? record : {...record,current_publication:publication},kind});
  }
  // How a step works only needs reading once, so it sits behind ⓘ beside the heading instead of staying on the page.
  const infoTip = (html,label = '说明') => `<details class="production-hint"><summary title="${esc(label)}" aria-label="${esc(label)}"></summary><div class="production-hint-body">${html}</div></details>`;
  // The machine-readable block repeats the gaps and questions already listed above it.
  const readableOutput = text => String(text || '').replace(/```json[\s\S]*?```/g,'').trim();
  // Model, effort and timing stay one click away; the card shows only the client, model and how long it took.
  function runReceipt({client,provider,requestedModel,effectiveModel,effectiveEffort,requestedEffort,label,extra = [],started,finished}) {
    const model = label(effectiveModel || requestedModel), took = started && finished ? elapsedText(started,finished) : '';
    const brief = [client.replace(/ 客户端$/,''),model,effectiveEffort || requestedEffort || '',took ? `用时 ${took}` : '',started ? readableDate(started) : ''].filter(Boolean).join(' · ');
    const full = `${esc(client)} · ${effectiveModel ? `实际模型：${esc(label(effectiveModel))}` : `申请模型：${esc(label(requestedModel))} · 实际型号尚未回报`}${effectiveModel && effectiveModel !== requestedModel ? `（申请 ${esc(label(requestedModel))}）` : ''}${effectiveEffort ? ` · 实际思考强度：${esc(effectiveEffort)}` : requestedEffort ? ` · 申请思考强度：${esc(requestedEffort)}` : ''} · ${provider === 'codex' ? '使用 Codex 客户端当前账户额度' : '使用 Claude 订阅额度'}${extra.map(text => ` · ${esc(text)}`).join('')}${started ? ` · 开始 ${esc(readableDate(started))}` : ''}${finished ? ` · 结束 ${esc(readableDate(finished))}` : ''}`;
    return `<details class="run-receipt"><summary>${esc(brief)}</summary><small>${full}</small></details>`;
  }
  const definitive = error => Number.isInteger(error?.status) && error.status >= 400 && error.status < 500;
  const jobLink = job => url(`#production/${encodeURIComponent(job.id)}`,job.product);
  // The editor's own count of how closely a 1:1 edit followed the original.
  const replicaScore = edit => {
    const score = edit.report?.replica;
    if (!score || typeof score !== 'object') return '';
    const total = ['exact','close','substitute','missing'].reduce((sum,key) => sum + (Number(score[key]) || 0),0);
    return total ? `对上原片 ${Number(score.exact) || 0}/${total} 个镜头` : '';
  };
  const seconds = value => value === null || value === undefined ? '—' : `${Number(value).toFixed(2)} 秒`;
  const voiceLabel = voice => [voice.note,voice.name].filter(Boolean).join(' · ') || voice.voice_id;
  const audioURL = (job,take,kind,download = false) => `/api/production/audio/${encodeURIComponent(job)}/${encodeURIComponent(take)}/${kind}${download ? '?download=1' : ''}`;
  // Material names contain Chinese characters; the picker also excludes this job's output timelines.
  const hasChinese = name => /\p{Script=Han}/u.test(name);
  const materialOnly = names => names.filter(hasChinese);
  function materialPicker(box,status,exclude,chosen,saved) {
    const names = status.ok ? materialOnly((status.timelines || []).filter(name => !exclude.has(name))) : [];
    if (names.length) {
      const remembered = materialOnly(chosen || saved || []).filter(name => names.includes(name));
      const checked = chosen ? remembered : remembered.length ? remembered : names;
      box.innerHTML = `<div class="material-options">${names.map(name => `<label><input type="checkbox" name="material" value="${esc(name)}" ${checked.includes(name) ? 'checked' : ''}>${esc(name)}</label>`).join('')}</div>`;
    } else box.innerHTML = `<p class="context-note">${status.ok ? '达芬奇里没有名称含中文的素材时间线，可以直接填写（逗号分隔）。' : '暂时读不到达芬奇里的时间线列表，可以直接填写名称含中文的素材时间线（逗号分隔）。'}</p><input name="material_text" value="${esc(materialOnly(chosen || saved || []).join('，'))}">`;
  }
  const pickedMaterials = form => $$('input[name="material"]:checked',form).map(input => input.value).concat((form.material_text?.value || '').split(/[,，]/).map(name => name.trim()).filter(Boolean));
  const materialProblem = names => !names.length ? '请至少选一条素材时间线。' : names.some(name => !hasChinese(name)) ? '素材时间线名称需包含中文。' : '';
  // Long orders show one step at a time; the step bar stays pinned under the top bar.
  function mountSteps(id,steps,fallback) {
    const choice = view.choice[id] ||= {}, present = steps.filter(step => step.panels.length);
    if (!present.length) return;
    const nav = document.createElement('nav'), anchor = document.createElement('div');
    nav.className = 'production-steps';nav.setAttribute('role','tablist');nav.setAttribute('aria-label','制作步骤');
    anchor.className = 'production-steps-anchor';
    nav.innerHTML = present.map((step,index) => `<button type="button" role="tab" data-step="${step.key}" class="${step.done ? 'done' : ''}"><b aria-hidden="true">${step.done ? '✓' : index + 1}</b><span>${esc(step.label)}</span>${step.note ? `<small>${esc(step.note)}</small>` : ''}</button>`).join('');
    present[0].panels[0].before(anchor,nav);
    present.forEach(step => step.panels.forEach(panel => {panel.dataset.stepPanel = step.key;panel.setAttribute('role','tabpanel');}));
    const buttons = $$('[data-step]',nav);
    const show = key => {
      choice.step = key;
      buttons.forEach(button => {const on = button.dataset.step === key;button.setAttribute('aria-selected',String(on));button.tabIndex = on ? 0 : -1;if (on && nav.scrollWidth > nav.clientWidth) nav.scrollLeft = button.offsetLeft - (nav.clientWidth - button.offsetWidth) / 2;});
      present.forEach(step => step.panels.forEach(panel => {panel.hidden = step.key !== key;}));
    };
    show(present.some(step => step.key === choice.step) ? choice.step : present.some(step => step.key === fallback) ? fallback : present[0].key);
    const open = button => {
      show(button.dataset.step);button.focus();
      const offset = $('.topbar')?.offsetHeight || 0, top = anchor.getBoundingClientRect().top;
      if (top < offset) window.scrollBy({top:top - offset});
    };
    buttons.forEach((button,index) => {
      button.onclick = () => open(button);
      button.onkeydown = event => {
        const next = {ArrowRight:index + 1,ArrowLeft:index - 1,Home:0,End:buttons.length - 1}[event.key];
        if (next === undefined) return;
        event.preventDefault();open(buttons[(next + buttons.length) % buttons.length]);
      };
    });
  }
  const statusReads = new Map();

  function readStatus(path, lifetime, force = false) {
    let entry = statusReads.get(path);
    if (entry?.pending) return entry.pending;
    if (!force && entry?.value && entry.expires > Date.now()) return Promise.resolve(entry.value);
    if (!entry) {entry = {};statusReads.set(path,entry);}
    const pending = api(path).then(value => {
      entry.value = value;
      entry.expires = Date.now() + (value.ok ? lifetime : Math.min(lifetime,3000));
      return value;
    }).finally(() => {if (entry.pending === pending) entry.pending = null;});
    entry.pending = pending;
    return pending;
  }

  // Keep the request ID for recoverable responses; the server can restore saved results.
  async function send(key,path,body) {
    const signature = JSON.stringify(body);
    let entry = view.pending.get(key);
    if (!entry || entry.signature !== signature) {entry = {signature,body:{...body,request_id:crypto.randomUUID()}};view.pending.set(key,entry);}
    try {const result = await api(path,{method:'POST',body:entry.body});view.pending.delete(key);return result;}
    catch (error) {if (definitive(error)) view.pending.delete(key);throw error;}
  }
  async function once(modal,key,path,makeBody) {
    modal.done ||= {};modal.requests ||= {};
    if (modal.done[key]) return modal.done[key];
    if (!modal.requests[key]) modal.requests[key] = {...await makeBody(),request_id:crypto.randomUUID()};
    try {modal.done[key] = await api(path,{method:'POST',body:modal.requests[key]});return modal.done[key];}
    catch (error) {if (definitive(error)) delete modal.requests[key];throw error;}
  }

  function modalOpen() {return !!view.modal || !!view.settings;}
  function closeModal(force = false) {
    const modal = view.modal;if (!modal || (modal.busy && !modal.detachable && !force)) return;
    modal.dialog.close();modal.dialog.remove();view.modal = null;
  }
  function setError(node,message) {const box = $('[data-error]',node);if (box) {box.textContent = message;box.hidden = !message;}}
  function openModal(title,body,button,onSubmit) {
    if (view.modal) closeModal(true);
    const dialog = document.createElement('dialog');dialog.className = 'script-editor-dialog production-dialog';
    dialog.innerHTML = `<form><div class="dialog-heading"><h2>${esc(title)}</h2><button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button></div><div class="script-editor-body">${body}<p class="ai-error" data-error role="alert" hidden></p></div><div class="dialog-footer"><span data-hint></span><div><button type="button" class="secondary" data-close>取消</button><button type="submit" class="primary" data-submit>${esc(button)}</button></div></div></form>`;
    document.body.append(dialog);
    const modal = {dialog,form:$('form',dialog),busy:false};
    view.modal = modal;
    $$('[data-close]',dialog).forEach(node => node.onclick = () => closeModal());
    dialog.addEventListener('cancel',event => {event.preventDefault();closeModal();});
    modal.form.onsubmit = async event => {
      event.preventDefault();if (modal.busy) return;
      modal.busy = true;setError(dialog,'');
      const submit = $('[data-submit]',dialog);submit.disabled = true;submit.textContent = '正在处理…';
      try {await onSubmit(modal);}
      catch (error) {setError(dialog,definitive(error) ? fail(error) : `结果尚未确认：${fail(error)}。重试会沿用同一请求，已取得的结果可接续恢复；结果仍未知时请先核实。`);}
      finally {modal.busy = false;if (view.modal === modal) {submit.disabled = false;submit.textContent = button;}}
    };
    dialog.showModal();return modal;
  }

  function linesTable(lines,groups = null) {
    const timing = new Map((groups || []).map(group => [group.number,group]));
    return `<div class="production-table-wrap"><table class="production-table"><thead><tr><th>#</th><th>口播</th><th>中文</th><th>画面要证明什么</th><th>素材标签</th><th>证据</th>${groups ? '<th>配音时间（秒）</th>' : ''}</tr></thead><tbody>${lines.map(line => {
      const group = timing.get(line.number);
      return `<tr data-production-line="${line.number}"><td>${line.number}</td><td class="spoken">${esc(line.spoken)}</td><td>${esc(line.zh || '—')}</td><td>${esc(line.proof || '待补')}</td><td>${esc(line.tags || '—')}</td><td>${esc(line.evidence || '—')}</td>${groups ? `<td class="timing">${group && group.start !== null ? `${Number(group.start).toFixed(2)}–${Number(group.end).toFixed(2)}` : '—'}</td>` : ''}</tr>`;
    }).join('')}</tbody></table></div>`;
  }
  function problemsMarkup(problems,gaps = false) {
    return problems?.length ? `<div class="production-problems"><strong>需要核对</strong><ul>${problems.map(item => `<li>${esc(item)}</li>`).join('')}</ul>${gaps ? '<button type="button" class="secondary compact" data-script-fill>AI 补全脚本信息</button>' : ''}</div>` : '';
  }
  // A script adopted without the writing skill's delivery: lines missing 画面要求, or no 开头画面/屏显/补拍/发布文案 at all.
  const manualScript = data => !data.variant || data.variant.manual;
  const scriptGaps = (job,data) => job.lines.some(line => !line.proof) || (manualScript(data) && !job.supplement);
  const fillRunning = job => job.ai_fill?.status === 'running';
  const aiTaskHash = (id,product) => window.WorkbenchShell?.url?.(`#ai/${encodeURIComponent(id)}`,product) || `#ai/${encodeURIComponent(id)}`;
  // 「补全脚本信息」 runs a 写稿 round with the writing skill; filling by hand stays available as the secondary route.
  function fillControls(job,data) {
    const fill = job.ai_fill || {};
    if (fillRunning(job)) return `<div class="take-actions" data-fill-running><span class="production-busy">AI 正在按写稿技能补全脚本信息，写完自动填进来…</span><a class="secondary" href="${esc(aiTaskHash(fill.task_id,job.product))}">查看写稿</a></div>`;
    const failed = fill.status === 'failed' && fill.error ? `<span class="ai-error" role="alert">${esc(fill.error)}</span>` : '';
    if (scriptGaps(job,data)) return `<div class="take-actions">${failed}<button type="button" class="primary" id="job-ai-fill" ${job.archived ? 'disabled' : ''} title="让 AI 按写稿技能补上开头画面、屏显、读法、对应中文、意群表的画面要求/素材标签/证据、补拍清单和发布文案；口播不改">${icon('edit')}AI 补全脚本信息</button><button type="button" class="subtle" id="job-lines-edit">手动填写</button></div>`;
    return `<div class="take-actions">${failed}<button type="button" class="subtle" id="job-lines-edit">${icon('edit')}${manualScript(data) ? '编辑脚本信息' : '编辑画面要求'}</button></div>`;
  }
  function recoverySection(job) {
    const operations = job.recovery_operations || [], busy = job.voicing || job.timeline_building || (job.edits || []).some(edit => edit.status === 'running');
    return operations.length ? `<section class="production-recovery" id="production-recovery"><h2>异常恢复</h2><p class="context-note">沿用原请求恢复已保存的结果。配音结果仍未知时，请先核对服务商记录；恢复不会自动重发未知配音。</p>${operations.map(operation => {
      const ready = ['response_saved','result_ready'].includes(operation.state) || operation.has_response === true || operation.has_result === true;
      const unresolvedVoice = operation.kind === 'take' && operation.uncertain === true && operation.can_retry_not_generated === true && !operation.has_audio && !ready;
      const label = ready ? '恢复已取得的结果' : operation.uncertain ? '核对并恢复' : operation.kind === 'timeline' ? '核对原时间线并恢复' : '继续原请求';
      return `<div class="production-recovery-row"><div><p>${esc(operation.message || '制作操作尚未完整登记，请先核对原结果。')}</p><small>${operation.kind === 'take' ? '配音' : '达芬奇时间线'}${operation.updated_at ? ` · ${esc(readableDate(operation.updated_at))}` : ''}</small></div><button type="button" class="secondary" data-recover-operation="${esc(operation.request_id)}" data-recover-kind="${esc(operation.kind)}" ${busy ? 'disabled' : ''}>${label}</button>${operation.kind === 'timeline' && !ready ? `<button type="button" class="subtle" data-abandon-timeline="${esc(operation.request_id)}" title="不再恢复这次建线，之后可以重新建。达芬奇里如有名字带 __WB_ 的半成品时间线，可以直接删除。" ${busy ? 'disabled' : ''}>放弃这次建线</button>` : ''}</div>${unresolvedVoice ? `<div class="production-recovery-confirm"><label><input type="checkbox" data-confirm-no-voice="${esc(operation.request_id)}" ${busy ? 'disabled' : ''}>我已在 ElevenLabs 核实本次没有生成音频</label><p>仅在已核实没有生成音频时勾选。重试将重新请求配音并消耗 ElevenLabs 额度。</p><button type="button" class="secondary" data-retry-uncertain-voice="${esc(operation.request_id)}" disabled>确认未生成，重试本次配音</button></div>` : ''}`;
    }).join('')}<p class="ai-error" data-error role="alert" hidden></p></section>` : '';
  }
  function bindRecovery(job) {
    $$('[data-recover-operation]').forEach(button => button.onclick = async () => {
      const panel = $('#production-recovery'), label = button.textContent;
      button.disabled = true;button.textContent = '正在恢复…';setError(panel,'');
      try {
        const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/recover`,{method:'POST',body:{request_id:button.dataset.recoverOperation,kind:button.dataset.recoverKind}});
        toast(result?.queued ? result.message : '原请求的结果已接续登记。');await route(true);
      } catch (error) {setError(panel,fail(error));button.disabled = false;button.textContent = label;}
    });
    $$('[data-abandon-timeline]').forEach(button => button.onclick = async () => {
      const panel = $('#production-recovery'), label = button.textContent;
      button.disabled = true;button.textContent = '正在放弃…';setError(panel,'');
      try {
        await api(`/api/production/jobs/${encodeURIComponent(job.id)}/timeline/abandon`,{method:'POST',body:{request_id:button.dataset.abandonTimeline}});
        toast('已放弃这次建线，可以重新建时间线。');await route(true);
      } catch (error) {setError(panel,fail(error));button.disabled = false;button.textContent = label;}
    });
    $$('[data-retry-uncertain-voice]').forEach(button => {
      const check = $$('[data-confirm-no-voice]').find(input => input.dataset.confirmNoVoice === button.dataset.retryUncertainVoice);
      check.onchange = () => {button.disabled = check.disabled || !check.checked;};
      button.onclick = async () => {
        if (!check.checked || check.disabled) return;
        const panel = $('#production-recovery'), label = button.textContent;
        check.disabled = true;button.disabled = true;button.textContent = '正在重试原配音请求…';setError(panel,'');
        try {
          await api(`/api/production/jobs/${encodeURIComponent(job.id)}/recover`,{method:'POST',body:{request_id:button.dataset.retryUncertainVoice,kind:'take',decision:'not_generated',confirmed:true}});
          toast('原配音请求已接续，请试听并核对结果。');await route(true);
        } catch (error) {setError(panel,fail(error));check.disabled = false;button.disabled = !check.checked;button.textContent = label;}
      };
    });
  }

  function captureListState() {return null;}

  async function route(keepScroll = false, refreshStatus = false) {
    const hash = pageHash(), seq = ++view.sequence, tail = hash.replace(/^#production\/?/,'').split('?')[0];
    if (tail === 'import') {renderLocalImport();return;}
    if (!tail) {location.hash = url('#content',currentProduct());return;}
    if (refreshStatus) statusReads.forEach(entry => {entry.expires = 0;});
    if (view.modal && !view.modal.busy) closeModal(true);
    $('#breadcrumb').innerHTML = `内容创作 <span>/</span> 制作单`;
    if (!keepScroll) $('#content').innerHTML = skeletonMarkup(2);
    try {
      if (tail) applyActivityTarget(decodeURIComponent(tail),hash);
      if (tail) {
        const data = await api(`/api/production/jobs/${encodeURIComponent(decodeURIComponent(tail))}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderJob(data,seq);
        mountLocalEditing(data.job);
        mountActivityLogs(data.job.id);focusActivityTarget();
      }
    } catch (error) {
      if (seq !== view.sequence || hash !== pageHash()) return;
      if (tail && error.status === 404) {$('#content').innerHTML = `<div class="empty"><h3>这张制作单已不存在</h3><p>可能已被删除，或属于其他产品。</p><a class="secondary" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content'))}">返回内容列表</a></div>`;return;}
      $('#content').innerHTML = `<div class="empty"><h3>暂时无法读取制作资料</h3><p>${esc(fail(error))}</p><button class="secondary" id="production-retry">重新读取</button></div>`;
      $('#production-retry').onclick = () => route(true);
    }
  }
  const localComputer = () => ['127.0.0.1','localhost','[::1]'].includes(location.hostname);
  async function downloadLocalFile(path,name) {
    const response = await fetch(path,{credentials:'same-origin',cache:'no-store'});
    if (!response.ok) {const result = await response.json().catch(() => ({}));throw new Error(result.error || '下载失败，请重新登录后再试。');}
    const objectURL = URL.createObjectURL(await response.blob());
    const anchor = document.createElement('a');anchor.href = objectURL;anchor.download = name;anchor.click();
    setTimeout(() => URL.revokeObjectURL(objectURL),1000);
  }
  function renderLocalImport() {
    $('#content').innerHTML = `<div class="page-heading"><div><h1>在这台电脑剪辑</h1><p>领取共享制作单，使用这台电脑的素材和达芬奇。</p></div></div><section class="section-card"><h2>导入剪辑包</h2><p class="context-note">先在共享工作台的制作单里下载剪辑包。这里会建立独立制作单，保留口播、配音和意群时间；再在本机建立时间线、选择本机素材并剪辑。</p>${localComputer() ? '<label class="field"><span>选择剪辑包（ZIP，最多 80 MB）</span><input type="file" accept=".zip" data-local-package></label><button class="primary" data-import-local>导入并打开制作单</button>' : '<p>请先在这台电脑安装并启动工作台，然后打开 <a href="http://127.0.0.1:8767/#production/import">本机剪辑入口</a>。</p>'}<p class="ai-error" data-error role="alert" hidden></p></section>`;
    const button = $('[data-import-local]');if (!button) return;
    button.onclick = async () => {
      const file = $('[data-local-package]').files[0];
      if (!file || file.size > 80*1024*1024) {setError($('#content'),'请选择不超过 80 MB 的剪辑包。');return;}
      button.disabled = true;
      try {
        const service = await api('/api/service/status');
        const response = await fetch('/api/production/local-edit/import',{method:'POST',headers:{'Content-Type':'application/zip','X-Workbench-Token':service.token},body:file});
        const result = await response.json();if (!response.ok) throw new Error(result.error || '导入失败');
        location.hash = `#production/${result.job_id}`;
      } catch(error) {setError($('#content'),fail(error));button.disabled = false;}
    };
  }
  function mountLocalEditing(job) {
    const download = async (button,kind,name) => {
      button.disabled = true;
      try {await downloadLocalFile(`/api/production/jobs/${job.id}/local-edit/${kind}`,`${job.id}-${name}`);}
      catch(error) {toast(fail(error),true);}
      finally {button.disabled = false;}
    };
    if (job.local_edit_origin) {
      // On the editing computer this is the order's own exit, so it stays visible.
      const section = document.createElement('section');section.className = 'section-card';
      section.innerHTML = `<div class="section-title-row"><h2>本机剪辑制作单</h2><button class="secondary" data-local-download>下载本机剪辑结果</button></div><p class="context-note">素材和达芬奇工程在这台电脑。剪完下载结果，回共享工作台原制作单「更多操作 → 回传剪辑结果」上传。</p>`;
      $('.page-heading',$('#content'))?.after(section);
      $('[data-local-download]',section).onclick = event => download(event.currentTarget,'result','result.json');
      return;
    }
    // Most orders are cut here; moving one to another computer lives in 更多操作.
    const menu = $('.production-more-actions > div');if (!menu) return;
    const tools = document.createElement('div');tools.className = 'local-edit-actions';
    tools.innerHTML = `<button type="button" class="subtle" data-local-download ${job.selected_take ? '' : 'disabled title="先生成并选用配音"'}>换台剪辑：下载剪辑包${job.selected_take ? '' : '（先生成并选用配音）'}</button><button type="button" class="subtle" data-local-pick>换台剪辑：回传结果</button><button type="button" class="subtle" data-local-client title="首次安装用。已装 Git 的电脑用 git pull 更新，不用重新下载。">下载本机工作台程序</button><input type="file" accept=".json" data-local-result hidden>`;
    menu.prepend(tools);
    $('[data-local-download]',tools).onclick = event => download(event.currentTarget,'package','local-edit.zip');
    $('[data-local-client]',tools).onclick = async event => {
      const button = event.currentTarget;button.disabled = true;
      try {await downloadLocalFile('/api/production/local-edit/client','case-workbench-local-editor.zip');}
      catch(error) {toast(fail(error),true);}
      finally {button.disabled = false;}
    };
    const input = $('[data-local-result]',tools);
    $('[data-local-pick]',tools).onclick = () => input.click();
    input.onchange = async () => {
      const file = input.files[0];if (!file) return;
      try {
        if (file.size > 2*1024*1024) throw new Error('结果文件超过 2 MB。');
        await api(`/api/production/jobs/${job.id}/local-edit/result`,{method:'POST',body:JSON.parse(await file.text())});
        toast('已登记另一台电脑的剪辑结果，工程保存在该电脑。');await route(true);
      } catch(error) {toast(fail(error),true);input.value = '';}
    };
    const latest = [...(job.local_edit_results || [])].sort((a,b) => String(b.received_at).localeCompare(String(a.received_at)))[0];
    if (latest) {
      const line = document.createElement('p');line.className = 'context-note local-edit-receipt';
      const names = (latest.timelines || []).map(item => item.name).join('、');
      line.textContent = `另一台电脑剪辑：${latest.computer} · ${readableDate(latest.received_at)} · ${names || '尚未建立时间线'}（工程和成片在该电脑，成片按发布流程另行上传）`;
      $('.page-heading',$('#content'))?.after(line);
    }
  }

  async function elevenStatus(node, force = false) {
    const warning = $('#production-service-warning');
    const showWarning = text => {
      if (!warning?.isConnected || !node.isConnected) return;
      warning.hidden = !text;
      warning.innerHTML = text ? `<span>配音服务需检查：${esc(text)}</span><button type="button" class="subtle" data-service-settings>打开制作设置</button>` : '';
      $('[data-service-settings]',warning)?.addEventListener('click',() => $('#production-settings-open')?.click());
    };
    node.innerHTML = '<span>ElevenLabs：正在检查连接…</span>';
    try {
      const status = await readStatus('/api/production/eleven',30000,force);
      if (!node.isConnected) return;
      node.classList.toggle('problem',!status.ok);
      showWarning(status.ok ? '' : status.error || '连接暂不可用');
      node.innerHTML = status.ok
        ? `<span>ElevenLabs 已连接${status.tier ? ` · ${esc(status.tier)}` : ''}${Number.isFinite(status.character_limit) ? ` · 本期已用 ${Number(status.character_count || 0).toLocaleString()} / ${Number(status.character_limit).toLocaleString()} 字符` : ''}</span><button type="button" class="subtle" data-recheck>重新检查</button>`
        : `<span>${esc(status.error)}</span><p>在 ElevenLabs 后台复制以 sk_ 开头的完整密钥，在 PowerShell 运行：<code>[Environment]::SetEnvironmentVariable('ELEVENLABS_API_KEY','你的密钥','User')</code>，然后点「重新检查」，不用重启工作台。</p><button type="button" class="subtle" data-recheck>重新检查</button>`;
    } catch (error) {if (!node.isConnected) return;showWarning(fail(error));node.classList.add('problem');node.innerHTML = `<span>暂时无法检查 ElevenLabs：${esc(fail(error))}</span><button type="button" class="subtle" data-recheck>重新检查</button>`;}
    const again = $('[data-recheck]',node);if (again) again.onclick = () => elevenStatus(node,true);
  }

  function productionSteps(data,kind) {
    const job = data.job, selected = (job.takes || []).find(take => take.id === job.selected_take);
    const publication = view.progress?.id === job.id ? view.progress.publication : null, published = publication?.source_current === true && (!publication.file || publication.file_available === true);
    return [
      {key:'lines',label:'脚本',note:`${job.lines.length} 个意群`,done:job.lines.length > 0,panels:[$('#job-script-panel')].filter(Boolean)},
      {key:'voice',label:'配音',note:job.voicing ? '生成中' : selected ? '已选用' : `${job.takes.length} 版`,done:!!selected,panels:[$('#take-panel'),$('#voice-panel')].filter(Boolean)},
      {key:'timeline',label:'达芬奇时间线',note:job.timeline_building ? (job.timeline_building.stage === 'waiting' ? '排队中' : '建线中') : `${activeTimelines(job).length} 条`,done:activeTimelines(job).some(item => selected && item.take_id === selected.id),panels:[$('#timeline-panel')].filter(Boolean)},
      {key:'edit',label:'AI 剪辑与人工审查',note:(job.edits || []).some(edit => edit.status === 'running' && edit.waiting_timeline) ? '待建线后自动剪辑' : (job.edits || []).some(edit => edit.status === 'running') ? '剪辑中' : manualCompleted(job) ? '人工完成' : job.status === 'approved' ? '已完成' : `${(job.edits || []).length} 次`,done:job.status === 'approved',panels:[$('#edit-panel')].filter(Boolean)},
      {key:'publish',label:'成片与发布',note:published ? '已登记发布' : publication ? '登记版本需核对' : '登记视频 ID',done:published,panels:[$('#video-publication-panel')].filter(Boolean)}
    ];
  }
  function refreshProductionProgress(data,kind) {
    const context = view.progress, item = data.job;
    if (!context || context.kind !== kind || context.id !== item.id || !context.host.isConnected) return;
    context.data = data;
    const steps = productionSteps(data,kind);
    steps.forEach((step,index) => {
      const button = $(`[data-step="${step.key}"]`,$('.production-steps'));if (!button) return;
      button.classList.toggle('done',Boolean(step.done));$('b',button).textContent = step.done ? '✓' : index + 1;
      let note = $('small',button);
      if (!note) {note = document.createElement('small');button.append(note);}
      note.textContent = step.note || '';note.hidden = !step.note;
    });
    // The order page reads its publication and packages itself; the header must say what the steps say.
    const record = context.publication === undefined ? item : {...item,current_publication:context.publication};
    const state = queueState(record,kind,context.delivery,{loading:context.loading,error:context.error});
    let action = state.action, step = state.step;
    if (!item.archived && !item.voicing) {
      const selected = (item.takes || []).find(take => take.id === item.selected_take);
      if (state.key === 'approved') {step = 'publish';action = context.publication ? context.publication.source_current === true && (!context.publication.file || context.publication.file_available === true) ? '查看发布登记' : '核对发布登记与当前版本' : '登记发布视频 ID';}
      else if (!selected && !(item.edits || []).length) {step = 'voice';action = item.takes?.length ? '试听并选用配音' : '生成配音';}
      else if (item.timeline_building?.group_markers === true) {step = 'edit';action = (item.edits || []).some(edit => edit.status === 'running' && edit.timeline_request_id === item.timeline_building.request_id) ? '查看自动接续剪辑' : '先提交剪辑';}
      else if (selected && !activeTimelines(item).some(timeline => timeline.take_id === selected.id)) {step = 'timeline';action = '建立当前配音的时间线';}
    }
    context.step = step;
    $('[data-next-state]',context.host).textContent = state.label;
    const headerState = $('.page-heading .script-status');if (headerState) headerState.textContent = state.label;
    const button = $('[data-next-action]',context.host);button.textContent = `下一步：${action} →`;button.hidden = !step;
    const note = $('[data-next-note]',context.host);note.textContent = context.error || state.note || '';note.hidden = !note.textContent;
    $('[data-next-retry]',context.host).hidden = !context.error;
    paintRuntimeDisplays(data,kind);
    mountProductionNeeds(data,kind);
  }
  function mountProductionProgress(data,kind) {
    const item = data.job, host = document.createElement('div');
    host.className = 'production-next';host.setAttribute('aria-label','制作单下一步');
    host.innerHTML = '<span class="script-status" data-next-state></span><button type="button" class="subtle" data-next-action></button><small data-next-note></small><button type="button" class="subtle" data-next-retry hidden>重新核对状态</button>';
    $('.page-heading').after(host);
    const context = view.progress = {id:item.id,kind,data,host,loading:true,error:'',delivery:null};
    $('[data-next-action]',host).onclick = () => {
      $$('[data-step]',$('.production-steps')).find(button => button.dataset.step === context.step)?.click();
    };
    const load = async () => {
      const read = (context.deliveryRead || 0) + 1;context.deliveryRead = read;
      context.loading = true;context.error = '';refreshProductionProgress(context.data,kind);
      try {
        const result = await api(`/api/production/deliveries?product=${encodeURIComponent(item.product)}`);
        if (view.progress !== context || !host.isConnected || context.deliveryRead !== read) return;
        context.delivery = (result.items || []).find(row => row.id === item.id && row.kind === kind);
      } catch (error) {if (view.progress !== context || !host.isConnected || context.deliveryRead !== read) return;context.error = `状态核对暂未完成：${fail(error)}`;}
      context.loading = false;refreshProductionProgress(context.data,kind);
    };
    context.loadDelivery = load;
    $('[data-next-retry]',host).onclick = load;
    refreshProductionProgress(data,kind);mountRuntimeTicker();void load();
  }
  function foldProductionControls(id,key,nodes,label) {
    const present = nodes.filter(Boolean);if (!present.length) return;
    const choice = view.choice[id] ||= {}, details = document.createElement('details');
    details.className = 'production-controls-again';details.dataset.productionControls = key;
    details.innerHTML = `<summary>${esc(label)}</summary>`;details.open = Boolean(choice.controls_open?.[key]);
    present[0].before(details);present.forEach(node => details.append(node));
    details.addEventListener('toggle',() => {if (details.isConnected) (choice.controls_open ||= {})[key] = details.open;});
    return details;
  }

  const elapsedText = (start,end = Date.now()) => {
    const stamp = Date.parse(start), until = typeof end === 'number' ? end : Date.parse(end);
    if (!Number.isFinite(stamp) || !Number.isFinite(until)) return '时间未记录';
    const seconds = Math.max(0,Math.floor((until-stamp)/1000));
    return seconds < 60 ? `${seconds} 秒` : seconds < 3600 ? `${Math.floor(seconds/60)} 分 ${seconds%60} 秒` : `${Math.floor(seconds/3600)} 小时 ${Math.floor(seconds%3600/60)} 分`;
  };
  function runProgressMarkup(run,kind) {
    if (!run || run.status !== 'running') return '';
    const stageNames = {materials:'素材清单',grid:'缩略网格',choice:'AI 选镜',placement:'放置镜头',saved:'保存交付',preparing:'准备素材',planning:'规划镜头',editing:'正在剪辑',voice_prepare:'准备配音',request:'请求配音',alignment:'对齐意群',saving:'保存版本'};
    const live = run.live || {}, stage = live.stage || live.phase;
    const grid = stage === 'grid' && Number.isInteger(live.grid?.total) ? ` · ${Number(live.grid.exported || 0)} / ${live.grid.total} 个片段已导出` : '';
    const fallback = {voice:'正在配音',edit:'正在剪辑'}[kind] || '正在处理';
    return `<div class="production-runtime" role="status"><span>${esc((stageNames[stage] || live.progress || fallback) + grid)}</span><small>已用 <span data-run-started="${esc(run.started_at || '')}" aria-live="off">${esc(elapsedText(run.started_at))}</span>${live.updated_at ? ` · 最近更新 <span data-run-updated="${esc(live.updated_at)}" aria-live="off">${esc(elapsedText(live.updated_at))}</span>前` : ''}</small></div>`;
  }
  function mountRuntimeTicker() {
    clearInterval(view.runtimeTimer);
    const tick = () => {
      if (!$('.production-next')) {clearInterval(view.runtimeTimer);return;}
      $$('[data-run-started]',$('#content')).forEach(node => {node.textContent = elapsedText(node.dataset.runStarted);});
      $$('[data-run-updated]',$('#content')).forEach(node => {node.textContent = elapsedText(node.dataset.runUpdated);});
    };
    tick();view.runtimeTimer = setInterval(tick,1000);
  }
  function activeProductionRuns(item,kind) {
    if (item.archived) return [];
    const voice = item.voicing_live || {};
    return [...(item.voicing ? [{run:{id:'voice',status:'running',started_at:voice.started_at,live:voice},kind:'voice'}] : []),...(item.edits || []).filter(run => run.status === 'running').map(run => ({run,kind:'edit'}))];
  }
  function paintRuntimeDisplays(data,kind) {
    const item = data.job;
    activeProductionRuns(item,kind).forEach(({run,kind:runKind}) => {
      const card = run.id === 'voice' ? $('#voice-panel') : $$('[data-activity-source]',$('#content')).find(node => node.dataset.activitySource === run.id);
      if (!card) return;
      const old = $('.production-runtime',card), markup = runProgressMarkup(run,runKind);
      if (old) old.outerHTML = markup;else ($('.take-head',card) || $('h2',card))?.insertAdjacentHTML('afterend',markup);
    });
  }
  function resultState(root) {
    const owner = node => node.closest('[data-edit-id],[data-take-id]')?.getAttribute('data-edit-id') || node.closest('[data-take-id]')?.dataset.takeId || '';
    const identity = node => `${owner(node)}:${node.tagName}:${node.id || node.getAttribute('name') || [...node.attributes].filter(attr => attr.name.startsWith('data-')).map(attr => `${attr.name}=${attr.value}`).join('|')}:${node.type === 'checkbox' ? node.value : ''}`;
    const checks = new Map($$('input[type="checkbox"]',root).map(node => [identity(node),node.checked]));
    const opened = new Map($$('details',root).map((node,index) => [`${owner(node)}:${node.id || node.className || index}`,node.open]));
    const focused = root.contains(document.activeElement) ? identity(document.activeElement) : '';
    return {identity,owner,checks,opened,focused};
  }
  function replaceResultMarkup(root,html) {
    if (!root || root.innerHTML === html) return false;
    const saved = resultState(root);root.innerHTML = html;
    $$('input[type="checkbox"]',root).forEach(node => {const key = saved.identity(node);if (saved.checks.has(key)) node.checked = saved.checks.get(key);});
    $$('details',root).forEach((node,index) => {const key = `${saved.owner(node)}:${node.id || node.className || index}`;if (saved.opened.has(key)) node.open = saved.opened.get(key);});
    if (saved.focused) $$('button,input,summary,[tabindex]',root).find(node => saved.identity(node) === saved.focused)?.focus({preventScroll:true});
    return true;
  }
  function sectionContents(markup) {
    const template = document.createElement('template');template.innerHTML = markup;return template.content.firstElementChild;
  }
  function syncControls(data,kind) {
    const item = data.job;
    const set = (key,complete,nodes,label,end) => {
      const existing = $(`[data-production-controls="${key}"]`,$('#content'));
      if (!complete && existing) {for (const node of [...existing.children].filter(node => node.tagName !== 'SUMMARY')) existing.before(node);existing.remove();}
      else if (complete && !existing) {const details = foldProductionControls(item.id,key,nodes(),label);if (details && end) end.append(details);}
    };
    if (kind === 'video') {
      const selected = item.takes.some(take => take.id === item.selected_take);
      if (selected) $('#voice-panel').before($('#take-panel'));
      set('voice',selected && !item.voicing,() => [$('#take-form')],'再生成一版配音');
      set('timeline',activeTimelines(item).some(timeline => selected && timeline.take_id === item.selected_take),() => [$('#timeline-form')],'再建一条达芬奇时间线',$('#timeline-panel'));
      set('edit',foldEditControls(item,view.choice[item.id] || {}),() => [$('#edit-form',$('#edit-panel'))],'再进行一次 AI 剪辑',$('#edit-panel'));
    }
  }
  async function refreshProductionDetail(data,kind,{scope = '',fresh = null,checkDelivery = true,freshEpoch = null} = {}) {
    const context = view.progress, item = data.job;
    if (!context || context.id !== item.id || context.kind !== kind) return false;
    if (fresh && freshEpoch !== null && (context.localRefresh || 0) !== freshEpoch) return false;
    const sequence = view.sequence, request = (context.localRefresh || 0) + 1;context.localRefresh = request;
    fresh ||= await api(`/api/production/jobs/${encodeURIComponent(item.id)}`);
    if (sequence !== view.sequence || view.progress !== context || request !== context.localRefresh || !context.host.isConnected) return false;
    const savedScroll = window.scrollY;
    const publicationSource = job => JSON.stringify([job.status,job.selected_take,completionTimeline(job)?.unique_id,activeTimelines(job).map(item => [item.unique_id,item.manual_completion?.at]),(job.edits || []).map(edit => [edit.id,edit.status,edit.approval])]);
    const oldPublicationSource = publicationSource(item);
    Object.assign(item,fresh.job);Object.assign(data,fresh);data.job = item;context.data = data;
    const heading = $('[data-production-title]');if (heading) heading.textContent = productionTitle(item);
    const choice = view.choice[item.id] ||= {}, selected = item.takes.find(take => take.id === item.selected_take);
    $$('[data-take-id]',$('#take-panel')).forEach(card => {
      const take = item.takes.find(take => take.id === card.dataset.takeId);if (!take) return;
      card.classList.toggle('selected',take.id === item.selected_take);
      const action = $('.take-head',card)?.lastElementChild;
      if (action) action.outerHTML = takeSelectionMarkup(data,take);
    });
    bindTakeSelection(data,sequence);
    const scriptLines = $('.script-lines',$('#job-script-panel'));
    replaceResultMarkup(scriptLines,`<h3>意群表 <small>含画面要求${selected ? '与配音时间' : ''}</small></h3>${linesTable(item.lines,selected?.groups)}`);
    bindTakeSegments(data);
    const requestStatus = readStatus('/api/production/resolve',10000).catch(error => ({ok:false,error:fail(error)}));
    if (!$('#timeline-form') && selected && data.creators?.length) {
      replaceResultMarkup($('#timeline-panel'),sectionContents(timelineSection(data,choice,selected)).innerHTML);bindTimeline(data,choice,sequence,requestStatus);
    } else replaceResultMarkup($('#timeline-cards'),timelineCards(data));
    if (!$('#edit-form',$('#edit-panel')) && editTargets(item).length) {
      replaceResultMarkup($('#edit-panel'),sectionContents(editSection(data,choice)).innerHTML);bindEdit(data,choice,sequence,requestStatus);
    } else {
      replaceResultMarkup($('#edit-runs'),editRunsMarkup(data));
      replaceResultMarkup($('#production-completion-host'),completionSection(item,choice));
      const select = $('#edit-form',$('#edit-panel'))?.timeline;
      if (select) {choice.edit_timeline = select.value;select.innerHTML = editTargetOptions(item,choice);select.dispatchEvent(new Event('change'));}
    }
    bindVideoResultCards(data);
    if (oldPublicationSource !== publicationSource(item)) window.VideoPublication?.mount($('#video-publication-panel'),{job:item,api,onLoaded:publication => {if (view.progress === context) {context.publication = publication;refreshProductionProgress(data,'video');}},onSaved:() => refreshProductionDetail(data,'video',{scope:'edit'}),onTimelineChanged:() => refreshProductionDetail(data,'video',{scope:'timeline'})});
    syncControls(data,kind);refreshProductionProgress(data,kind);mountActivityLogs(item.id);mountRuntimeTicker();
    const panels = productionSteps(data,kind), active = $('.production-steps [aria-selected="true"]')?.dataset.step;
    panels.forEach(step => step.panels.forEach(panel => {panel.dataset.stepPanel = step.key;panel.setAttribute('role','tabpanel');panel.hidden = step.key !== active;}));
    window.scrollTo({top:savedScroll});
    if (checkDelivery && context.loadDelivery) await context.loadDelivery();
    return true;
  }

  function queueState(item,kind,delivery,deliveryState) {
    if (item.archived) return {key:'archived',label:'已归档',action:'查看制作单',note:'恢复后可继续制作'};
    {
      const publication = item.current_publication;
      // Same test as the publish step: a registration whose bound file is gone still needs checking.
      if (publication?.source_current && publication.post_id && publication.published_on && (!publication.file || publication.file_available !== false)) return {key:'published',label:'已登记发布',action:'查看发布记录',step:'publish',note:'当前审查版本已登记发布',delivery:true};
      if (item.voicing) return {key:'in_progress',label:'配音中',action:'查看进度',step:'voice'};
      if ((item.edits || []).some(edit => edit.status === 'running' && edit.waiting_timeline)) return {key:'in_progress',label:'剪辑排队中',action:'查看自动接续剪辑',step:'edit',note:'建好本次时间线后自动开始剪辑'};
      if (item.timeline_building) return {key:'in_progress',label:item.timeline_building.stage === 'waiting' ? '建线排队中' : '建线中',action:item.timeline_building.group_markers === true ? '先提交剪辑' : '查看进度',step:item.timeline_building.group_markers === true ? 'edit' : 'timeline'};
      if (item.recovery_operations?.length) return {key:'needs_attention',label:'结果待恢复',action:'查看并恢复'};
      if (manualCompleted(item)) return {key:'approved',label:'已人工完成',action:'查看制作单',step:'edit',note:'第 4 步已手动标记成功',delivery:true};
      if (['awaiting_review','approved'].includes(item.status)) {
        // A result from another timeline cannot override the job's latest state.
        const edit = (item.edits || []).find(edit => edit.id === delivery?.edit_id);
        const reviewable = delivery?.status === 'awaiting_review' && edit && currentEdit(item,edit) &&
          ((skillDelivery(edit) && skillReviewable(edit)) ||
          (!skillDelivery(edit) && edit.status === 'awaiting_review' && edit.validation?.status === 'awaiting_review'));
        if (reviewable && item.status === 'approved' && delivery.approved_at) return {key:'approved',label:'已人工审查',action:'查看制作单',step:'edit',note:'人工审查已确认；成片导出在达芬奇完成',delivery:true};
        if (reviewable && item.status === 'awaiting_review' && !delivery.approved_at) return {key:'awaiting_review',label:'待人工审查',action:'去审查',step:'edit',note:coverageReview(edit) ? '当前画面覆盖已核对 · 内容待人工审查' : skillDelivery(edit) && !edit.validation ? '按技能保存交付 · 工程核验未执行' : '工程核验通过 · 待人工审查',delivery:true};
        return {key:'needs_attention',label:deliveryState.loading ? '审查状态核对中' : '审查状态待确认',action:'查看制作单',step:'edit',note:deliveryState.error || '进入制作单核对当前工程与审查记录'};
      }
      const problem = ['needs_material','needs_verification','needs_repair','verification_failed','unverified','completed','failed','cancelled','interrupted'].includes(item.status);
      return {key:problem ? 'needs_attention' : 'in_progress',label:statusLabels[item.status] || '待处理',action:problem ? '查看并处理' : item.status === 'editing' ? '查看进度' : '继续制作',step:['draft','voiced'].includes(item.status) ? 'voice' : 'edit'};
    }
  }

  // Settings belong to the content workspace and never render the retired production queue.
  const settingsCurrent = session => view.settings === session && session.dialog.isConnected && session.hash === location.hash && session.product === currentProduct();
  const formValues = form => [...new FormData(form).entries()];
  const settingsDirty = session => !!session && $$('form',session.dialog).some(form => session.initial?.get(form.id) !== JSON.stringify(formValues(form)));
  function hasUnsavedInput() {return !!view.settings && (view.settings.busy || settingsDirty(view.settings));}
  function askSettingsDiscard(discard) {
    if ($('#production-settings-discard')) return;
    const dialog = document.createElement('dialog');dialog.id = 'production-settings-discard';
    dialog.innerHTML = '<div class="confirm-body"><h2>保留制作设置的编辑？</h2><p>输入尚未保存，可以继续编辑。</p><div class="confirm-actions"><button type="button" class="secondary" data-discard>放弃编辑</button><button type="button" class="primary" data-keep>继续编辑</button></div></div>';
    document.body.append(dialog);dialog.showModal();
    const finish = () => {dialog.close();dialog.remove();};
    $('[data-discard]',dialog).onclick = () => {finish();discard();};$('[data-keep]',dialog).onclick = finish;
    dialog.addEventListener('cancel',event => {event.preventDefault();finish();});
  }
  function closeSettings(force = false) {
    const session = view.settings;if (!session || session.busy) return;
    if (!force && settingsDirty(session)) {askSettingsDiscard(() => closeSettings(true));return;}
    session.dialog.close();session.dialog.remove();view.settings = null;
  }
  function beforeNavigate(hash) {
    const session = view.settings;if (!session || hash === session.hash) return true;
    if (session.busy || settingsDirty(session)) {
      history.replaceState(null,'',session.hash);
      if (!session.busy) askSettingsDiscard(() => {closeSettings(true);location.hash = hash;});
      return false;
    }
    closeSettings(true);return true;
  }
  function drawerSettingsMarkup(data,product) {
    const voices = data.voices || [];
    return `<section class="production-setting"><h2>配音服务</h2><div class="production-eleven" id="production-eleven"></div></section><section class="production-setting"><h2>音色库</h2><p class="context-note">生成过的音色会自动留在这里；备注写清是谁的声音、适合什么稿。</p><ul class="voice-list">${voices.length ? voices.map(voice => `<li class="voice-row ${voice.disabled ? 'disabled' : ''}"><div><strong>${esc(voiceLabel(voice))}</strong>${product && data.defaults?.[window.WorkbenchShell.normalizeProduct(product)] === voice.voice_id ? '<span class="script-status published">默认</span>' : ''}${voice.disabled ? '<span class="script-status">已停用</span>' : ''}<code>${esc(voice.voice_id)}</code><small>使用 ${voice.use_count || 0} 次${voice.last_used_at ? ` · 最近 ${esc(readableDate(voice.last_used_at))}` : ''}</small></div><div class="voice-actions"><button type="button" class="subtle" data-voice-note="${esc(voice.voice_id)}">备注</button>${product && !voice.disabled ? `<button type="button" class="subtle" data-voice-default="${esc(voice.voice_id)}">设为默认</button>` : ''}<button type="button" class="subtle" data-voice-toggle="${esc(voice.voice_id)}">${voice.disabled ? '启用' : '停用'}</button></div></li>`).join('') : '<li class="context-note">还没有音色。</li>'}</ul><form id="voice-add" class="inline-form"><label class="field"><span>音色 ID</span><input name="voice_id" required pattern="[A-Za-z0-9]{10,64}" autocomplete="off"></label><label class="field"><span>备注</span><input name="note" maxlength="500" placeholder="例如：Zack 男声，适合测评稿"></label><button type="submit" class="secondary">核对并加入</button><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="production-setting"><h2>读法表${product ? ` · ${esc(product)}` : ''}</h2>${product ? `<p class="context-note">只改送去配音的文字，稿件原文不变。按整词、区分大小写替换。</p><ul class="reading-list">${(data.readings || []).map(item => `<li><span><code>${esc(item.written)}</code> 读作 <strong>${esc(item.spoken)}</strong></span><button type="button" class="subtle" data-reading-remove="${esc(item.id)}">删除</button></li>`).join('') || '<li class="context-note">还没有登记读法。</li>'}</ul><form id="reading-add" class="inline-form"><label class="field"><span>稿中写法</span><input name="written" required maxlength="100" placeholder="例如 202H"></label><label class="field"><span>配音读法</span><input name="spoken" required maxlength="200" placeholder="例如 two oh two H"></label><button type="submit" class="secondary">添加读法</button><p class="ai-error" data-error role="alert" hidden></p></form>` : '<p class="context-note">选择具体产品后维护读法。</p>'}</section>${resolveSettings(data,product)}`;
  }
  async function reloadSettings(session,savedForm = '') {
    const request = session.request = (session.request || 0) + 1;
    const data = await api(`/api/production?product=${encodeURIComponent(session.product)}`);
    if (!settingsCurrent(session) || request !== session.request) return;
    const drafts = new Map($$('form',session.dialog).filter(form => form.id !== savedForm && session.initial?.get(form.id) !== JSON.stringify(formValues(form))).map(form => [form.id,formValues(form)]));
    $('.rev-drawer-body',session.dialog).innerHTML = drawerSettingsMarkup(data,session.product);
    session.initial = new Map($$('form',session.dialog).map(form => [form.id,JSON.stringify(formValues(form))]));
    for (const [id,values] of drafts) {
      const form = $(`#${id}`,session.dialog);if (!form) continue;
      for (const [name,value] of values) {const input = form.elements.namedItem(name);if (input) input.value = value;}
    }
    bindSettings(data,session);
    void elevenStatus($('#production-eleven',session.dialog));
  }
  async function openSettings() {
    if (view.settings) {if (settingsCurrent(view.settings)) return;closeSettings();if (view.settings) return;}
    const dialog = document.createElement('dialog');dialog.id = 'production-settings';dialog.className = 'rev-drawer production-drawer';dialog.setAttribute('aria-labelledby','production-settings-title');
    dialog.innerHTML = `<div class="rev-drawer-head"><h2 id="production-settings-title">制作设置</h2><button type="button" class="icon-button" data-drawer-close aria-label="关闭制作设置">${icon('close')}</button></div><div class="rev-drawer-body"><p class="context-note">正在读取制作设置…</p></div>`;
    const session = {dialog,hash:location.hash,product:currentProduct(),busy:false};view.settings = session;
    document.body.append(dialog);dialog.showModal();
    dialog.addEventListener('click',event => {if (event.target === dialog || event.target.closest('[data-drawer-close]')) closeSettings();});
    dialog.addEventListener('cancel',event => {event.preventDefault();closeSettings();});
    try {await reloadSettings(session);}
    catch (error) {if (settingsCurrent(session)) {const body = $('.rev-drawer-body',dialog);body.innerHTML = `<p class="ai-error" role="alert">${esc(fail(error))}</p><button type="button" class="secondary" data-settings-retry>重新读取</button>`;$('[data-settings-retry]',body).onclick = () => {closeSettings(true);void openSettings();};}}
  }
  async function saveSetting(session,form,operation) {
    if (session.busy || !settingsCurrent(session)) return;
    session.busy = true;
    const button = form && $('button[type="submit"]',form);if (button) button.disabled = true;
    if (form) setError(form,'');
    try {await operation();await reloadSettings(session,form?.id);}
    catch (error) {
      if (form?.isConnected) setError(form,fail(error));else toast(fail(error),true);
      if (error.status === 409 && settingsCurrent(session)) {try {session.data.revision = (await api(`/api/production?product=${encodeURIComponent(session.product)}`)).revision;} catch {}}
    } finally {session.busy = false;if (button?.isConnected) button.disabled = false;}
  }
  function bindSettings(data,session) {
    const product = session.product, root = session.dialog, voices = data.voices || [];session.data = data;
    bindResolveSettings(data,product,session);
    const voiceForm = $('#voice-add',root);
    voiceForm.onsubmit = event => {
      event.preventDefault();const values = Object.fromEntries(new FormData(voiceForm));
      void saveSetting(session,voiceForm,async () => {
        await send('voice-add','/api/production/voices',{revision:data.revision,operation:'save',voice:{voice_id:values.voice_id.trim(),note:values.note.trim()}});toast('音色已核对并加入音色库。');
      });
    };
    $$('[data-voice-note]',root).forEach(button => button.onclick = () => {
      const voice = voices.find(item => item.voice_id === button.dataset.voiceNote);
      openModal('音色备注',`<p class="context-note">${esc(voice.name || '未取得名称')} · <code>${esc(voice.voice_id)}</code></p><label class="field"><span>备注</span><input name="note" maxlength="500" value="${esc(voice.note || '')}"></label>`,'保存备注',async modal => {
        const note = new FormData(modal.form).get('note').trim();
        session.busy = true;
        try {
          await once(modal,'note','/api/production/voices',async () => ({revision:(await api(`/api/production?product=${encodeURIComponent(product)}`)).revision,operation:'save',voice:{voice_id:voice.voice_id,note,model_id:voice.model_id,settings:voice.settings}}));
          closeModal(true);toast('备注已保存。');await reloadSettings(session);
        } finally {session.busy = false;}
      });
    });
    const voiceAction = (operation,voiceId,message) => saveSetting(session,null,async () => {
      await send(`voice-${operation}-${voiceId}`,'/api/production/voices',{revision:data.revision,operation,product,voice:{voice_id:voiceId}});toast(message);
    });
    $$('[data-voice-default]',root).forEach(button => button.onclick = () => void voiceAction('default',button.dataset.voiceDefault,`已设为 ${product} 的默认音色。`));
    $$('[data-voice-toggle]',root).forEach(button => {
      const voice = voices.find(item => item.voice_id === button.dataset.voiceToggle);
      button.onclick = () => void voiceAction(voice.disabled ? 'enable' : 'disable',voice.voice_id,voice.disabled ? '音色已启用。' : '音色已停用，历史配音不受影响。');
    });
    const readingForm = $('#reading-add',root);
    if (readingForm) readingForm.onsubmit = event => {
      event.preventDefault();const values = Object.fromEntries(new FormData(readingForm));
      void saveSetting(session,readingForm,async () => {
        await send('reading-add','/api/production/readings',{revision:data.revision,reading:{product,written:values.written.trim(),spoken:values.spoken.trim()}});toast('读法已添加。');
      });
    };
    $$('[data-reading-remove]',root).forEach(button => button.onclick = () => {
      const item = data.readings.find(entry => entry.id === button.dataset.readingRemove);
      void saveSetting(session,null,async () => {await send(`reading-remove-${item.id}`,'/api/production/readings',{revision:data.revision,operation:'remove',reading:{id:item.id}});toast('读法已删除。');});
    });
  }

  function resolveSettings(data,product) {
    const settings = data.settings || {creators:[]}, config = data.product_settings;
    return `<section class="production-setting"><h2>达芬奇设置${product ? ` · ${esc(product)}` : ''}</h2><form id="resolve-settings" class="inline-form"><label class="field"><span>达人名（逗号分隔，作为时间线名开头）</span><input name="creators" value="${esc((settings.creators || []).join(', '))}" placeholder="例如 Zack, Lab" autocomplete="off"></label>${config ? `<label class="field"><span>达芬奇项目</span><input name="project" required maxlength="200" value="${esc(config.project)}"></label><div class="form-row"><label class="field"><span>时间线编号</span><input name="code" required maxlength="20" pattern="[A-Za-z0-9]+" value="${esc(config.code)}"></label><label class="field"><span>帧率</span><select name="fps">${frameRates.map(value => `<option ${value === config.fps ? 'selected' : ''}>${value}</option>`).join('')}</select></label></div><div class="form-row"><label class="field"><span>宽</span><input name="width" type="number" min="16" max="8192" required value="${esc(config.width)}"></label><label class="field"><span>高</span><input name="height" type="number" min="16" max="8192" required value="${esc(config.height)}"></label></div><p class="context-note">新时间线命名如 <code>${esc((settings.creators || [])[0] || '达人')}${today()}-${esc(config.code)}-序号</code>；配音放「${esc(config.audio_bin)}」素材夹，时间线放「${esc(config.timeline_bin)}」素材夹。建线时会在达芬奇里找名称含达人名的文件夹（如「B跨境店渠道号-zack」），到里面的对应项目建；支持原项目名或「达人名-项目名」（如 Zack-202H、Lab-202H），此处仍可填 202H。已有项目会按身份核对，改名后仍可继续使用。需要切换时先保存并关闭当前项目。</p>` : '<p class="context-note">选择具体产品后设置达芬奇项目和时间线编号。</p>'}<button type="submit" class="secondary">保存设置</button><p class="ai-error" data-error role="alert" hidden></p></form></section>`;
  }
  function bindResolveSettings(data,product,session) {
    const form = $('#resolve-settings',session.dialog), config = data.product_settings;
    form.onsubmit = event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      const settings = {creators:values.creators.split(/[,，\s]+/).filter(Boolean)};
      if (config) Object.assign(settings,{product,project:values.project.trim(),code:values.code.trim(),fps:values.fps,width:Number(values.width),height:Number(values.height),audio_bin:config.audio_bin,timeline_bin:config.timeline_bin});
      void saveSetting(session,form,async () => {await send('resolve-settings','/api/production/settings',{revision:data.revision,settings});toast('达芬奇设置已保存。');});
    };
  }
  // `switches`: the caller works per creator, and the workbench opens that creator's project itself.
  async function resolveStatus(node,config,request,switches = false) {
    try {
      const status = await request;
      if (!node.isConnected) return;
      node.classList.toggle('problem',!status.ok || (status.project !== config.project && !switches));
      node.textContent = !status.ok ? status.error
        : status.project !== config.project ? `达芬奇当前打开的是项目「${status.project}」。建时间线前请先在达芬奇里打开「${config.project}」，工作台不会替你切换项目。`
        : `达芬奇 ${status.version} 已打开项目「${status.project}」（${status.fps} fps，${status.width}×${status.height}）。`;
      // The right project being open is the normal case; only a problem earns a line.
      node.hidden = !node.classList.contains('problem');
    } catch (error) {if (!node.isConnected) return;node.hidden = false;node.classList.add('problem');node.textContent = `暂时无法检查达芬奇：${fail(error)}`;}
  }
  // Gap frames are offsets from the beginning of the timeline, including on timelines that start at 01:00:00.
  const editFrameRate = value => ({'23.976':24000/1001,'29.97':30000/1001,'59.94':60000/1001}[String(value)] || Number(value));
  function editCoverage(job,edit,gaps,uncovered) {
    const summary = edit.summary, timeline = (job.timelines || []).find(item => item.unique_id === edit.timeline_uid);
    const take = (job.takes || []).find(item => item.id === (edit.take_id || timeline?.take_id));
    const fps = editFrameRate(summary?.fps || timeline?.fps);
    const frames = Number(summary?.voice_frames || timeline?.frames || (Number(take?.duration) > 0 && fps > 0 ? Math.ceil(take.duration * fps) : 0));
    if (!summary && !edit.report || !Number.isFinite(frames) || frames <= 0 || !Number.isFinite(fps) || fps <= 0) return '';
    const bounded = items => (items || []).filter(item => Number.isInteger(item.start) && Number.isInteger(item.end) && item.start >= 0 && item.end > item.start && item.start < frames)
      .map(item => ({...item,end:Math.min(frames,item.end)}));
    const missing = bounded(gaps), empty = bounded(uncovered), pictureGaps = summary && Array.isArray(summary.picture_gaps) ? bounded(summary.picture_gaps) : null;
    // Without a coverage summary the strip could only paint "unchecked"; the gap count in the facts already says it.
    if (!summary && !missing.length) return '';
    const boundaries = [...new Set([0,frames,...[...missing,...empty,...(pictureGaps || [])].flatMap(item => [item.start,item.end])])].sort((a,b) => a-b);
    const spans = [];
    for (let index = 0; index < boundaries.length - 1; index++) {
      const start = boundaries[index], end = boundaries[index+1], at = (start + end) / 2;
      const activeGaps = missing.filter(item => item.start <= at && at < item.end);
      const gap = activeGaps.find(item => gapStatus(item) === 'missing') || activeGaps[0];
      const pictureAbsent = pictureGaps?.some(item => item.start <= at && at < item.end);
      const gapKind = gap && gapStatus(gap) === 'unverified' ? 'review' : 'gap';
      const kind = !summary ? gap ? gapKind : 'unknown' : pictureGaps && !pictureAbsent ? 'picture'
        : gap ? gapKind : empty.some(item => item.start <= at && at < item.end) || pictureAbsent ? 'uncovered' : 'picture';
      const previous = spans.at(-1);
      if (previous?.kind === kind && previous.gap === gap) previous.end = end;
      else spans.push({start,end,kind,gap});
    }
    const labels = {picture:'有画面',gap:'缺素材',review:'素材待核实',uncovered:'未覆盖',unknown:'覆盖待核对'};
    const parts = spans.map(span => {
      const title = `${labels[span.kind]} · ${(span.start/fps).toFixed(2)}–${(span.end/fps).toFixed(2)} 秒${span.gap?.name ? ` · ${span.gap.name}` : ''}`;
      const width = ((span.end-span.start)/frames*100).toFixed(4);
      return ['gap','review','uncovered'].includes(span.kind) && edit.timeline_uid ? `<button type="button" class="edit-coverage-span ${span.kind}" data-coverage-width="${width}" data-open-edit-timeline="${esc(edit.timeline_uid)}" data-edit-frame="${span.start}" data-edit-seconds="${(span.start/fps).toFixed(2)}" title="${esc(title)} · 点击在达芬奇定位" aria-label="${esc(title)}，在达芬奇定位">${span.kind === 'gap' ? '缺' : span.kind === 'review' ? '核' : '空'}</button>`
        : `<span class="edit-coverage-span ${span.kind}" data-coverage-width="${width}" title="${esc(title)}" aria-label="${esc(title)}"></span>`;
    }).join('');
    const legend = Object.keys(labels).filter(kind => spans.some(span => span.kind === kind));
    const hint = summary ? '点击缺素材、待核实或未覆盖的区间，在达芬奇定位。' : '按剪辑说明标出缺素材和待核实素材，其他区间尚未核对画面覆盖。点击对应区间可在达芬奇定位。';
    return `<div class="edit-coverage"><div class="edit-coverage-head"><strong>画面覆盖${infoTip(esc(hint))}</strong><small>0–${(frames/fps).toFixed(2)} 秒</small></div><div class="edit-coverage-bar" role="group" aria-label="画面覆盖时间条">${parts}</div><div class="edit-coverage-legend">${legend.map(kind => `<span><i class="${kind}" aria-hidden="true"></i>${labels[kind]}</span>`).join('')}</div></div>`;
  }
  function editGapJump(edit,gap,label) {
    return edit.timeline_uid && Number.isInteger(gap.start) && gap.start >= 0
      ? `<button type="button" class="subtle edit-gap-jump" data-open-edit-timeline="${esc(edit.timeline_uid)}" data-edit-frame="${gap.start}" ${Number.isFinite(gap.seconds) ? `data-edit-seconds="${gap.seconds}"` : ''} title="在达芬奇定位到这里"><span>${esc(label)}</span> <span aria-hidden="true">→</span></button>` : `<strong>${esc(label)}</strong>`;
  }
  function editStageStrip(edit) {
    const live = edit.live || {}, states = live.stages || {}, stage = live.stage || ({preparing:'materials',planning:'choice',editing:'placement'}[live.phase]);
    const steps = [['materials','素材清单'],['grid','缩略网格'],['choice','AI 选镜'],['placement','选镜与放置'],['saved','保存交付']];
    const grid = live.grid || {}, known = Number.isInteger(grid.total) && grid.total >= 0;
    return `<div class="edit-stage-progress"><ol class="edit-stage-strip" aria-label="剪辑阶段">${steps.map(([key,label]) => {
      const state = states[key]?.status || (stage === key ? 'running' : 'pending');
      const status = {completed:'已完成',running:'进行中',skipped:'复用',partial:'部分完成',failed:'未完成',pending:'待开始'}[state] || '待核对';
      return `<li class="${esc(state)}" ${stage === key ? 'aria-current="step"' : ''}><span>${label}</span><small>${status}</small></li>`;
    }).join('')}</ol>${known ? `<p class="edit-grid-count">网格已导出 ${Number.isInteger(grid.exported) ? grid.exported : 0} / ${grid.total} 个片段${Number.isInteger(grid.attempted) ? ` · 已处理 ${grid.attempted}` : ''}${grid.deferred ? ` · ${grid.deferred} 个延后由剪辑客户端查看` : ''}</p>` : ''}${!stage ? '<p class="context-note">这次任务尚未记录具体阶段。</p>' : ''}</div>`;
  }
  function editReviewForm(job,edit) {
    if (editState(edit) !== 'awaiting_review' || !currentEdit(job,edit) || editCompleted(job,edit) || edit.result_pending) return '';
    if (skillDelivery(edit) && !skillReviewable(edit)) return '';
    const scope = Array.isArray(edit.validation?.manual_scope) && edit.validation.manual_scope.length ? edit.validation.manual_scope : manualScope;
    const draft = view.choice[job.id]?.edit_reviews?.[edit.id] || {}, busy = (job.edits || []).some(item => item.status === 'running'), disabled = job.archived || busy;
    const note = skillDelivery(edit) ? '这里只登记你的人工审查，不会读回工程核验。' : '确认时会读回并保存这条时间线，工程核验仍通过才记为完成。';
    return `<form class="edit-review-form" data-edit-review="${esc(edit.id)}"><h3>人工审查清单${infoTip(esc(note))}</h3><fieldset ${disabled ? 'disabled' : ''}><legend>我已人工核对</legend>${scope.map(item => `<label><input type="checkbox" name="reviewed" value="${esc(item)}" ${(draft.reviewed || []).includes(item) ? 'checked' : ''}>${esc(item)}</label>`).join('')}</fieldset><label class="field"><span>备注（可选）</span><textarea name="review_note" rows="2" maxlength="2000" ${disabled ? 'disabled' : ''}>${esc(draft.note || '')}</textarea></label><div class="take-actions"><button type="submit" class="primary" data-approve-edit="${esc(edit.id)}" disabled>确认完成</button><small data-review-hint>${job.archived ? '恢复制作单后可确认。' : busy ? '正在剪辑，结束后再确认。' : '逐项勾选后才能确认完成。'}</small></div><p class="ai-error" data-error role="alert" hidden></p></form>`;
  }
  function editRun(job,edit,historicalRestore = false) {
    const summary = edit.summary, report = edit.report || {}, live = edit.live || {}, state = editState(edit);
    const unverifiedDelivery = skillDelivery(edit) && !edit.validation;
    const requestedModel = edit.requested_model || edit.model || 'codex-default', provider = edit.provider || editProvider(requestedModel), client = edit.client_label || editClient(provider);
    const effectiveModel = live.effective_model || edit.effective_model, effectiveEffort = live.effective_effort || edit.effective_effort;
    const receipt = runReceipt({client,provider,requestedModel,effectiveModel,effectiveEffort,requestedEffort:edit.effort,label:editModelLabel,started:edit.started_at,finished:edit.finished_at,
      extra:[editModes[edit.mode] || edit.mode,replicaScore(edit),edit.validation?.checked_at ? `核验 ${readableDate(edit.validation.checked_at)}` : '',edit.execution_status ? `剪辑执行：${editLabels[edit.execution_status] || edit.execution_status}` : ''].filter(Boolean)});
    const gaps = summary?.gaps || (Array.isArray(report.gaps) ? report.gaps.map(gap => ({...gap,name:gap.need || (gapStatus(gap) === 'unverified' ? '待核实素材' : '缺素材')})) : []), uncovered = summary?.uncovered || [];
    const missing = gaps.filter(gap => gapStatus(gap) === 'missing'), unverified = gaps.filter(gap => gapStatus(gap) === 'unverified');
    const resolved = summary?.resolved_gaps || [], coverageChecked = coverageReview(edit);
    // Gaps read in seconds on the voiceover, never as raw frame ranges.
    const timeline = (job.timelines || []).find(item => item.unique_id === edit.timeline_uid), fps = editFrameRate(summary?.fps || timeline?.fps);
    const gapLocation = gap => Number.isFinite(gap.seconds) ? `第 ${gap.seconds} 秒` : fps > 0 && Number.isInteger(gap.start) && Number.isInteger(gap.end) ? `${(gap.start / fps).toFixed(2)}–${(gap.end / fps).toFixed(2)} 秒` : `第 ${gap.start}–${gap.end} 帧`;
    const facts = summary ? [`镜头 ${summary.shots} 个`,`配音 ${summary.voice_seconds} 秒`,`缺素材 ${missing.length} 处`,
      unverified.length ? `素材待核实 ${unverified.length} 处` : '',
      (summary.picture_gaps || [...gaps,...uncovered]).length ? `画面空段 ${(summary.picture_gaps || [...gaps,...uncovered]).length} 处` : '画面全部覆盖',
      resolved.length ? `原报告空段已覆盖 ${resolved.length} 处（内容待人工确认）` : '',
      summary.long_shots ? `超过通用参考 ${summary.frame_limit} 帧的镜头 ${summary.long_shots} 个（最长 ${summary.max_shot} 帧，需按产品规则人工判断）` : ''].filter(Boolean) : unverifiedDelivery ?
      [Number.isInteger(report.shots) ? `剪辑说明：镜头 ${report.shots} 个` : '',`已报缺素材 ${missing.length} 处`,unverified.length ? `素材待核实 ${unverified.length} 处` : ''].filter(Boolean) : [];
    const issues = (edit.validation?.issues || []).map(issue => typeof issue === 'string' ? issue : issue.message).filter(Boolean);
    const warnings = issues.length ? issues : summary ? [summary.voice_changed ? '配音轨和建时间线时不一致，请核对是否被改动。' : '',
      summary.group_markers !== summary.groups_expected ? `意群标记读回 ${summary.group_markers} 个，应有 ${summary.groups_expected} 个。` : ''].filter(Boolean) : [];
    if (edit.readback_error && !issues.length) warnings.push(`没能读回时间线：${edit.readback_error}`);
    const approval = state === 'approved' ? edit.approval : null;
    const explanation = approval ? `你已在 ${readableDate(approval.at)} 人工确认完成${approval.note ? `：${approval.note}。` : '。'}${unverifiedDelivery ? '工程核验未执行。' : ''}`
      : state === 'awaiting_review' ? coverageChecked ? '当前时间线画面已全部覆盖，内容仍待人工确认；本次未核验剪前内容是否保留。字幕、声音和导出由你处理。' : unverifiedDelivery ? '已按技能保存交付，工程核验未执行；请在达芬奇人工审查，字幕、声音和导出由你处理。' : '工程核验通过，请在达芬奇人工审查；字幕、声音和导出由你处理。'
      : state === 'unverified' ? '旧记录未保存完整核验依据，不能作为可审查成品；可读回现有工程重新核验。' : '';
    const busy = (job.edits || []).some(entry => entry.status === 'running');
    const missingBaseline = !edit.baseline && (edit.validation?.issues || []).some(issue => issue?.code === 'baseline_missing');
    const resultRecovery = skillDelivery(edit) && edit.result_pending;
    // The newest unsaved or failed run can be continued on the same timeline without re-picking options.
    const latestOnTimeline = (job.edits || []).filter(entry => entry.timeline_uid === edit.timeline_uid).at(-1);
    const retryable = !resultRecovery && !job.archived && !edit.waiting_timeline && latestOnTimeline?.id === edit.id &&
      (['needs_repair','failed'].includes(edit.status) || skillDelivery(edit) && edit.execution_status === 'completed' && edit.report?.saved !== true);
    const actions = edit.status === 'running' ? `<button type="button" class="secondary" data-cancel-edit="${esc(edit.id)}">停止</button>`
      : `<div class="edit-actions">${missingBaseline ? `<button type="button" class="secondary" data-adopt-baseline="${esc(edit.id)}" ${busy || !historicalRestore ? 'disabled' : ''}>补齐核验依据</button>` : ''}${retryable ? `<button type="button" class="primary" data-retry-edit="${esc(edit.id)}" title="沿用这次的模式、素材和额外要求（模型用全局默认），在原时间线上让 AI 接着剪并保存；不新建时间线" ${busy ? 'disabled' : ''}>原地重试</button>` : ''}${resultRecovery ? `<button type="button" class="secondary" data-recover-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>恢复结果</button>` : `<button type="button" class="secondary" data-recheck-edit="${esc(edit.id)}" title="${esc(skillDelivery(edit) ? '读回并保存现有工程，更新画面覆盖和原报告空段；内容仍需人工确认' : '读回并保存现有工程后重新核验；请先结束正在进行的剪辑')}" ${busy ? 'disabled' : ''}>重新核验</button>`}${approval ? `<button type="button" class="secondary" data-revoke-edit="${esc(edit.id)}" ${busy || job.archived ? 'disabled' : ''}>撤销完成</button>` : ''}${job.archived ? '' : `<button type="button" class="subtle edit-delete" data-delete-edit="${esc(edit.id)}" ${busy && edit.status === 'running' ? 'disabled' : ''}>${icon('trash')}删除</button>`}</div>`;
    const evidence = edit.baseline_evidence || {};
    const adopted = edit.baseline_origin === 'adopted' ? `<p class="context-note">当前保护基准采纳于 ${esc(readableDate(edit.baseline_at))}：配音轨和意群标记以采纳时工程为准。这份基准不能证明更早的剪辑过程没有改动；镜头仍逐个核验。</p>`
      : edit.baseline_origin === 'historical_resume' ? `<div class="context-note historical-baseline"><p>历史核验依据来自 ${esc(readableDate(evidence.captured_at || edit.baseline_at))} 保存的「${esc(evidence.scope || 'Codex 接续剪辑前')}」快照，于 ${esc(readableDate(evidence.restored_at))} 恢复；来源资料与文件校验记录已保留。工程核验通过后仍待你人工审查，恢复依据不会自动确认完成。</p>${(evidence.unrecorded_fields || []).includes('voice.clip_enabled') ? '<p>历史快照未记录配音片段启用状态，按当前工程另行核对；画幅的宽和高来自当时项目设置。</p>' : ''}${evidence.sources?.length ? `<details><summary>查看历史依据来源</summary><ul class="edit-gaps">${evidence.sources.map(source => `<li><p>${esc(source.role)}：${esc(source.path)}</p><p><small>SHA-256：${esc(source.sha256)}</small></p></li>`).join('')}</ul></details>` : ''}</div>` : '';
    return `<article class="edit-card ${esc(state)}" data-edit-id="${esc(edit.id)}" data-activity-source="${esc(edit.id)}"><div class="take-head"><div><strong><span class="script-status ${['awaiting_review','approved'].includes(state) ? 'published' : ''}">${esc(editLabels[state] || state)}</span> ${esc(edit.timeline_name)}</strong>${receipt}</div>${actions}</div>${explanation ? `<p class="edit-validation-note">${esc(explanation)}</p>` : ''}${missingBaseline && !historicalRestore ? '<p class="context-note">需要更新工作台后恢复历史核验依据。</p>' : ''}${adopted}${edit.status === 'running' ? `${editStageStrip(edit)}${runProgressMarkup(edit,'edit')}${live.latest ? `<pre class="edit-live">${esc(live.latest)}</pre>` : ''}` : ''}${facts.length ? `<p class="edit-facts">${facts.map(esc).join(' · ')}</p>` : ''}${editCoverage(job,edit,gaps,uncovered)}${warnings.length ? `<ul class="take-warnings edit-validation-issues">${warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}${missing.length ? `<h3>缺素材</h3><ul class="edit-gaps">${missing.map(gap => `<li data-edit-gap="${gaps.indexOf(gap)}" data-gap-start="${esc(gap.start)}" data-gap-end="${esc(gap.end)}">${editGapJump(edit,gap,`${gapLocation(gap)} · ${gap.name}`)}${gap.note ? `<p>${esc(gap.note)}</p>` : ''}</li>`).join('')}</ul>` : ''}${unverified.length ? `<h3>素材待核实</h3><ul class="edit-gaps">${unverified.map(gap => `<li data-edit-gap="${gaps.indexOf(gap)}" data-gap-start="${esc(gap.start)}" data-gap-end="${esc(gap.end)}">${editGapJump(edit,gap,`${gapLocation(gap)} · ${gap.name}`)}</li>`).join('')}</ul>` : ''}${resolved.length ? `<h3>原报告空段已覆盖，内容待人工确认</h3><ul class="edit-gaps resolved-gaps">${resolved.map(gap => `<li>${editGapJump(edit,gap,`${gapLocation(gap)} · ${gap.name}`)}</li>`).join('')}</ul>` : ''}${uncovered.length ? `<h3>没有画面、也没说明原因的地方</h3><ul class="edit-gaps">${uncovered.map(item => `<li>${editGapJump(edit,item,`第 ${item.seconds} 秒起，共 ${item.end - item.start} 帧`)}</li>`).join('')}</ul>` : ''}${editQuestions(job,edit,report.questions,approval)}${edit.error ? `<p class="ai-error">${esc(edit.error)}</p>` : ''}${readableOutput(edit.output) ? `<details><summary>${esc(client)}的剪辑说明</summary><pre class="edit-output">${esc(readableOutput(edit.output))}</pre></details>` : ''}${edit.instruction ? `<p class="context-note">本次额外要求：${esc(edit.instruction)}</p>` : ''}${editReviewForm(job,edit)}</article>`;
  }
  const pendingTimelineId = value => String(value || '').startsWith('pending:') ? String(value).slice(8) : '';
  function editTargets(job) {
    const timelines = [...activeTimelines(job)].reverse(), pending = job.timeline_building;
    if (pending?.request_id && pending.group_markers === true && !timelines.some(item => item.build_request_id === pending.request_id)) {
      timelines.unshift({...pending,unique_id:`pending:${pending.request_id}`,name:`${pending.name || pending.creator + ' · 本次时间线'}（待建好后自动剪辑）`});
    }
    return timelines;
  }
  function editTargetOptions(job,choice) {
    const targets = editTargets(job), request = pendingTimelineId(choice.edit_timeline);
    if (request) {
      const built = targets.find(item => item.build_request_id === request);
      if (built) choice.edit_timeline = built.unique_id;
      else if (!targets.some(item => item.unique_id === choice.edit_timeline)) targets.unshift({unique_id:choice.edit_timeline,name:'原建线未完成，请重新建线或选择目标'});
    }
    if (!targets.some(item => item.unique_id === choice.edit_timeline)) choice.edit_timeline = targets[0]?.unique_id || '';
    return targets.map(item => `<option value="${esc(item.unique_id)}" ${item.unique_id === choice.edit_timeline ? 'selected' : ''}>${esc(item.name)}</option>`).join('');
  }
  function editQuestions(job,edit,questions,approval) {
    if (!Array.isArray(questions) || !questions.length) return '';
    const body = `<ul class="edit-gaps">${questions.map((item,index) => `<li data-edit-question="${index}">${esc(item)}</li>`).join('')}</ul>`;
    return approval || !currentEdit(job,edit)
      ? `<details class="edit-delivery-questions"><summary>交付时提出的问题（${questions.length}）</summary>${body}</details>`
      : `<h3>待你确认</h3>${body}`;
  }
  function editSection(data,choice) {
    const job = data.job, timelines = editTargets(job), edits = [...(job.edits || [])].reverse();
    const running = edits.some(edit => edit.status === 'running');
    const olderIds = edits.slice(1).filter(edit => edit.status !== 'running').map(edit => edit.id);
    const form = !timelines.length && !pendingTimelineId(choice.edit_timeline) ? '<p class="context-note">先建时间线，再让 AI 按意群标记去素材里选画面、拼成视频。</p>'
      : `<form id="edit-form"><div class="form-row"><label class="field"><span>目标时间线</span><select name="timeline">${editTargetOptions(job,choice)}</select></label><label class="field"><span>选画面模式</span><select name="mode">${jobEditModes(job).map(([key,label]) => `<option value="${key}" ${key === (choice.edit_mode || defaultEditMode(job)) ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div><p class="context-note" data-pending-edit-note ${pendingTimelineId(choice.edit_timeline) ? '' : 'hidden'}>可以先提交剪辑；本次时间线建好后自动接着剪，关闭页面也会继续。</p><label class="timeline-options-check" data-recut-row hidden><input type="checkbox" name="recut" checked>另建一条时间线来剪，这条已有的剪辑不动，方便对比</label><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="3" maxlength="4000" placeholder="例如：开头用切纸结果；只剪前 4 个意群">${esc(choice.edit_instruction || '')}</textarea></label><p class="context-note"><span data-edit-default role="status">正在读取全局默认模型…</span> <a href="${esc(url('#settings'))}" data-edit-settings>修改全局设置</a> · <button type="button" class="subtle" data-edit-default-refresh>重新读取</button></p><div data-edit-override hidden><p class="context-note">当前全局默认暂不能用于 AI 剪辑，请为本次选择兼容模型，或修改全局设置。</p><div class="form-row"><label class="field"><span>本次剪辑模型</span><select name="model"><option value="">请选择兼容模型</option></select></label><label class="field"><span>本次思考强度</span><select name="effort"><option value="">客户端默认</option></select></label></div></div><p class="production-resolve" data-edit-provider-status role="status">正在检查剪辑客户端…</p><div class="take-actions"><button type="submit" class="primary" disabled>开始 AI 剪辑</button><button type="button" class="secondary" data-copy-handoff>复制剪辑指令</button><button type="button" class="secondary" data-open-target-timeline>在达芬奇打开时间线</button>${infoTip('<p data-edit-usage>AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude。</p>','AI 剪辑说明')}${running ? '<span>已有剪辑任务，可选择其他时间线继续准备。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="edit-panel"><h2>AI 剪辑与人工审查 <small>AI 剪辑 ${edits.length} 次</small></h2><div id="production-completion-host">${completionSection(job,choice)}</div><div id="edit-runs">${editRunsMarkup(data)}</div>${form}</section>`;
  }
  function editRunsMarkup(data) {
    const job = data.job, runs = [...(job.edits || [])].reverse();
    const completed = runs.find(edit => editState(edit) === 'approved' && currentEdit(job,edit));
    const edits = completed ? [completed,...runs.filter(edit => edit !== completed)] : runs;
    // Each timeline's latest cut stays: it is what that timeline is compared by.
    const olderIds = edits.slice(1).filter(edit => edit.status !== 'running' && !currentEdit(job,edit)).map(edit => edit.id);
    const current = edits.slice(0,1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('');
    const older = edits.length > 1 ? `<details class="edit-older"><summary>较早的 ${edits.length - 1} 次剪辑</summary>${job.archived || !olderIds.length ? '' : `<div class="edit-older-bar"><button type="button" class="secondary compact" data-clean-edits="${esc(olderIds.join(','))}">清理旧剪辑（${olderIds.length} 次）</button><small>只保留当前记录，连同本地文件一起删除，不动达芬奇里的时间线。</small></div>`}${edits.slice(1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('')}</details>` : '';
    return edits.length && edits.every(edit => editCompleted(job,edit)) ? `<details class="edit-completed-history"><summary>AI 剪辑历史（${edits.length} 次）</summary>${current}${older}</details>` : `${current}${older}`;
  }
  // One "人工完成" form per timeline of the selected voiceover, newest first. The first keeps the old element id.
  function completionSection(job,choice) {
    const targets = [...activeTimelines(job)].reverse().filter(item => job.selected_take && item.take_id === job.selected_take);
    // With no timeline to complete the form stays, disabled, and says what is missing.
    if (!targets.length) return completionForm(job,choice,{unique_id:'',name:'',manual_completion_blocked:'先为当前选用的配音建立时间线，再标记成功。'},0);
    return targets.map((timeline,index) => completionForm(job,choice,timeline,index)).join('');
  }
  function completionForm(job,choice,timeline,index) {
    const receipt = manualCompletion(job,timeline), uid = timeline.unique_id;
    // An AI result on this timeline that awaits review is approved through its own review form instead.
    if (!receipt && (job.edits || []).some(edit => edit.timeline_uid === uid && currentEdit(job,edit) && ['awaiting_review','approved'].includes(editState(edit)) && !edit.result_pending && (!skillDelivery(edit) || skillReviewable(edit)))) return '';
    // A running edit holds back only its own timeline; a response without the per-timeline field blocks the whole order, as before.
    const blocked = Object.hasOwn(timeline,'manual_completion_blocked') ? timeline.manual_completion_blocked :
      job.manual_completion_blocked || ((job.edits || []).some(edit => edit.status === 'running') ? '正在剪辑，结束后可手动标记成功。' : '');
    const reason = job.archived ? '恢复制作单后可修改完成状态。' : blocked || (!uid ? '先为当前选用的配音建立时间线，再标记成功。' : timeline.saved === false ? '时间线尚未保存，请先在达芬奇保存。' : '');
    const disabled = Boolean(reason), note = choice.completion_notes?.[uid] ?? (index === 0 ? choice.completion_note : '') ?? '';
    return `<form ${index === 0 ? 'id="job-completion-form" ' : ''}class="production-completion${receipt ? ' done' : ''}" data-completion-timeline="${esc(uid)}"><div class="production-completion-heading"><div><h3>${receipt ? '<span class="script-status published">人工已完成</span>' : '人工完成'}</h3><p class="context-note">${receipt ? `已在 ${esc(readableDate(receipt.at))} 手动标记成功` : '已在达芬奇里完成剪辑和审查后，可直接标记成功。'}${receipt?.timeline_name || timeline.name ? ` · ${esc(receipt?.timeline_name || timeline.name)}` : ''}</p></div>${receipt ? `<button type="submit" class="secondary" data-revoke-job-completion ${disabled ? 'disabled' : ''}>撤销手动完成</button>` : `<button type="submit" class="primary" data-complete-job ${disabled ? 'disabled' : ''}>手动标记成功</button>`}</div>${receipt ? receipt.note ? `<p class="production-completion-note">${esc(receipt.note)}</p>` : '' : `<details class="production-completion-details"><summary>添加备注（可选）</summary><label class="field"><span>完成备注</span><textarea name="completion_note" rows="2" maxlength="2000" placeholder="例如：手工剪辑、字幕和混音已完成" ${disabled ? 'disabled' : ''}>${esc(note)}</textarea></label></details>`}${reason ? `<p class="context-note">${esc(reason)}</p>` : ''}<p class="ai-error" data-error role="alert" hidden></p></form>`;
  }
  function bindVideoResultCards(data,choice = (view.choice[data.job.id] ||= {})) {
    const job = data.job;
    bindTimelineCards(data);
    $$('.production-completion[data-completion-timeline]').forEach(completionForm => {
    const uid = completionForm.dataset.completionTimeline, completionNote = $('[name="completion_note"]',completionForm);
    if (completionNote) completionNote.oninput = () => {(choice.completion_notes ||= {})[uid] = completionNote.value;};
    completionForm.onsubmit = async event => {
      event.preventDefault();
      const button = $('[type="submit"]',completionForm);if (button.disabled) return;
      const timeline = timelineOf(job,uid), receipt = manualCompletion(job,timeline), completed = !receipt, label = button.textContent;
      const body = completed ? {completed:true,timeline_uid:uid,note:completionNote.value.trim()} : {completed:false,timeline_uid:uid,completion_at:receipt.at};
      button.disabled = true;button.textContent = '正在保存…';if (completionNote) completionNote.disabled = true;setError(completionForm,'');
      try {
        await send(`completion-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/completion`,body);
        choice.step = 'edit';delete choice.completion_note;delete choice.completion_notes?.[uid];
        toast(completed ? '第 4 步已手动标记成功。' : '已撤销手动完成。');await refreshProductionDetail(data,'video',{scope:'edit'});
      } catch (error) {
        setError(completionForm,fail(error));button.disabled = false;button.textContent = label;if (completionNote) completionNote.disabled = false;
      }
    };
    });
    // Removes finished edits with their local files; DaVinci timelines are left alone.
    const deleteEdits = async ids => {
      const base = `/api/production/jobs/${encodeURIComponent(job.id)}/edits`;
      let previews;
      try {previews = await Promise.all(ids.map(id => api(`${base}/${encodeURIComponent(id)}/delete`,{method:'POST',body:{preview:true}})));}
      catch (error) {toast(fail(error),true);return;}
      const bytes = previews.reduce((sum,item) => sum + (item.bytes || 0),0), approved = previews.filter(item => item.approved);
      const size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
      openModal(ids.length > 1 ? `清理 ${ids.length} 次旧剪辑` : '删除这次 AI 剪辑',`<p class="context-note">将删除${ids.length > 1 ? `这 ${ids.length} 次剪辑` : `「${esc(previews[0].timeline_name || ids[0])}」这次剪辑`}的工作台记录，以及它在本地保存的交接文件、结果和候选画面，共约 ${esc(size)}。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程。</li><li>配音、时间线记录和制作单本身保留。</li></ul>${approved.length ? `<p class="ai-error">其中 ${approved.length} 次已人工确认完成；删除后制作单状态会按剩余剪辑重新计算。</p>` : ''}`,'确认删除',async modal => {
        const warnings = [];
        for (const id of ids) {
          const result = await send(`edit-delete-${id}`,`${base}/${encodeURIComponent(id)}/delete`,{});
          warnings.push(...(result.warnings || []));
        }
        closeModal(true);
        toast(warnings.length ? warnings[0] : `已删除 ${ids.length} 次剪辑及本地文件。`,warnings.length > 0);
        await route(true);
      });
    };
    const bindRuns = () => {
      $$('[data-coverage-width]',$('#edit-runs')).forEach(span => {span.style.width = `${Number(span.dataset.coverageWidth)}%`;});
      $$('[data-open-edit-timeline]',$('#edit-runs')).forEach(button => button.onclick = async () => {
        const frame = Number(button.dataset.editFrame);if (!Number.isInteger(frame) || frame < 0) return;
        button.disabled = true;
        try {
          const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/open`,{method:'POST',body:{unique_id:button.dataset.openEditTimeline,frame}});
          toast(`已在达芬奇打开 ${result.name || '时间线'}${button.dataset.editSeconds ? `，定位到第 ${button.dataset.editSeconds} 秒` : `，定位到第 ${frame} 帧`}。`);
        } catch (error) {toast(fail(error),true);}
        finally {button.disabled = false;}
      });
      $$('[data-recover-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;button.textContent = '正在恢复…';
        try {await send(`edit-recover-${button.dataset.recoverEdit}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recoverEdit)}/recheck`,{recover_result:true});toast('剪辑结果已恢复；工程核验未执行。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '恢复结果';}
      });
      $$('[data-retry-edit]').forEach(button => button.onclick = async () => {
        const previous = (job.edits || []).find(entry => entry.id === button.dataset.retryEdit);if (!previous) return;
        const label = button.textContent;button.disabled = true;button.textContent = '正在提交…';
        // Model and effort follow the global default, exactly like the form's default path.
        const body = {timeline:previous.timeline_uid,mode:previous.mode,instruction:previous.instruction || '',materials:previous.materials || [],retry_of:previous.id};
        try {await send(`edit-retry-${previous.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edit`,body);toast('已在原时间线上重新提交 AI 剪辑。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = label;}
      });
      $$('[data-recheck-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;button.textContent = '正在核验…';
        // In place: reads the target timeline by project and id, so a project or timeline the user switched to meanwhile does not matter.
        try {const result = await send(`edit-recheck-${button.dataset.recheckEdit}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recheckEdit)}/recheck`,{});toast(result.edit?.validation?.scope === 'current_coverage' ? '当前画面覆盖已更新；画面内容仍需人工确认。' : '现有工程已重新核验，请查看状态和问题。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '重新核验';}
      });
      $$('[data-delete-edit]').forEach(button => button.onclick = () => deleteEdits([button.dataset.deleteEdit]));
      $$('[data-clean-edits]').forEach(button => button.onclick = () => deleteEdits(button.dataset.cleanEdits.split(',').filter(Boolean)));
      $$('[data-edit-review]').forEach(form => {
        const edit = (job.edits || []).find(item => item.id === form.dataset.editReview);if (!edit) return;
        const boxes = $$('input[name="reviewed"]',form), button = $('[data-approve-edit]',form), note = $('[name="review_note"]',form), hint = $('[data-review-hint]',form);
        let saving = false;
        const sync = () => {
          const blocked = job.archived || (job.edits || []).some(item => item.status === 'running');
          const done = boxes.length > 0 && boxes.every(input => input.checked);
          button.disabled = saving || blocked || !done;
          hint.textContent = saving ? '正在保存…' : job.archived ? '恢复制作单后可确认。' : blocked ? '正在剪辑，结束后再确认。' : done ? '已逐项核对，可以确认完成。' : '逐项勾选后才能确认完成。';
          (choice.edit_reviews ||= {})[edit.id] = {reviewed:boxes.filter(input => input.checked).map(input => input.value),note:note.value};
        };
        boxes.forEach(input => input.onchange = sync);note.oninput = sync;sync();
        form.onsubmit = async event => {
          event.preventDefault();if (button.disabled || !currentEdit(job,edit) || editState(edit) !== 'awaiting_review') return;
          saving = true;setError(form,'');sync();
          boxes.forEach(input => input.disabled = true);note.disabled = true;
          try {
            const result = await send(`approve-${edit.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(edit.id)}/approval`,{approved:true,reviewed:boxes.filter(input => input.checked).map(input => input.value),note:note.value.trim()});
            delete choice.edit_reviews[edit.id];
            toast(result.approved ? '已确认完成。' : `读回后核验结果为「${editLabels[result.edit?.status] || result.edit?.status}」，未确认完成，请查看问题。`,!result.approved);
            await refreshProductionDetail(data,'video',{scope:'edit'});
          } catch (error) {setError(form,fail(error));}
          finally {saving = false;if (form.isConnected) {boxes.forEach(input => input.disabled = job.archived);note.disabled = job.archived;sync();}}
        };
      });
      $$('[data-revoke-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {await send(`revoke-${button.dataset.revokeEdit}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.revokeEdit)}/approval`,{approved:false});toast('已撤销完成确认。');await refreshProductionDetail(data,'video',{scope:'edit'});}
        catch (error) {toast(fail(error),true);button.disabled = false;}
      });
      $$('[data-adopt-baseline]').forEach(button => button.onclick = () => {
        if (data.historical_baseline_restore !== true) return;
        const edit = (job.edits || []).find(item => item.id === button.dataset.adoptBaseline);if (!edit) return;
        openModal('恢复历史核验依据',`<p class="context-note">这次剪辑缺少保护配音轨和意群标记的核验基准。工作台会查找「${esc(edit.timeline_name)}」接续剪辑前实际保存的快照、原生工程导出和交接资料，核对来源后恢复依据，再读回现有工程重新核验。</p><p class="context-note">只采用可核实的真实历史；找不到完整历史依据时会明确提示，不把当前工程当作过去的快照。恢复后仍需你人工审查，不会自动确认完成。</p>`,'恢复依据并核验',async () => {
          const result = await send(`baseline-${edit.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(edit.id)}/baseline`,{source:'historical_resume'});
          closeModal(true);toast(`已恢复历史核验依据，重新核验结果：${editLabels[result.edit?.status] || result.edit?.status}。`);await route(true);
        });
      });
      $$('[data-cancel-edit]').forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.cancelEdit)}/cancel`,{method:'POST',body:{}});toast('正在停止剪辑。');}
      catch (error) {toast(fail(error),true);button.disabled = false;}
      });
    };
    bindRuns();
  }
  function bindEdit(data,choice,seq,request) {
    const job = data.job, form = $('#edit-form',$('#edit-panel'));
    let runningEdits = job.edits || [], refreshEditControls = () => {};
    bindVideoResultCards(data,choice);
    if ((job.edits || []).some(edit => edit.status === 'running')) {
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden) {setTimeout(poll,5000);return;}
        try {
          const epoch = view.progress?.localRefresh || 0;
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
          if (epoch !== (view.progress?.localRefresh || 0)) {setTimeout(poll,5000);return;}
          runningEdits = fresh.job.edits || [];refreshEditControls();
          const running = runningEdits.some(edit => edit.status === 'running');
          if (!view.modal) await refreshProductionDetail(data,'video',{scope:'edit',fresh,freshEpoch:epoch,checkDelivery:!running});
          if (!running && !view.modal) return;
        } catch {}
        setTimeout(poll,5000);
      };
      setTimeout(poll,5000);
    }
    if (!form) return;
    let catalog = null, clients = null, defaults = null, defaultsError = '', clientsError = '', loadingDefaults = false, starting = false, defaultsSequence = 0, defaultsFingerprint = '';
    const isCurrentForm = () => form.isConnected && seq === view.sequence;
    const modelInfo = model => (catalog?.models || []).find(entry => entry.id === model) || (model === 'codex-default' ? {id:model,name:editModelLabel(model),provider:'codex',reasoning_efforts:[]} : null);
    const providerFor = model => modelInfo(model)?.provider || editProvider(model);
    const defaultModel = () => defaults?.model || 'codex-default';
    const supported = (model,effort) => {
      if (!model) return false;
      const entry = modelInfo(model), provider = providerFor(model);
      if (model === 'codex-default') return !effort;
      if (provider === 'claude' && (!Object.hasOwn(editModels,model) || ![null,undefined,'high','xhigh','max'].includes(effort))) return false;
      if (!['claude','codex'].includes(provider)) return false;
      // A failed catalog read cannot establish that the saved model is unsupported.
      if (!entry) return provider === 'claude' || Boolean(catalog?.models_error);
      return !effort || entry.available === false || (entry.reasoning_efforts || []).includes(effort);
    };
    const compatible = (model,effort) => {
      const entry = modelInfo(model);
      return Boolean(supported(model,effort) && entry && entry.available !== false);
    };
    const usesDefault = () => Boolean(defaults && supported(defaultModel(),defaults.reasoning_effort));
    const localSettings = () => ({model:form.model.value,effort:form.effort.value || null});
    const selectedModel = () => usesDefault() ? defaultModel() : form.model.value;
    const selectedTimelineRunning = () => runningEdits.some(edit => edit.status === 'running' && (edit.timeline_uid === form.timeline.value ||
      pendingTimelineId(form.timeline.value) && edit.timeline_request_id === pendingTimelineId(form.timeline.value)));
    const selectedTimelineUnavailable = () => !editTargets(job).some(item => item.unique_id === form.timeline.value);
    const canRecut = () => !pendingTimelineId(form.timeline.value) && timelineHasCut(job,form.timeline.value,runningEdits);
    const recutting = () => canRecut() && (form.recut.checked || selectedTimelineRunning());
    const modelReady = () => !loadingDefaults && !defaultsError && defaults &&
      clients?.providers?.[providerFor(selectedModel())]?.available &&
      (usesDefault() ? compatible(defaultModel(),defaults.reasoning_effort) : compatible(localSettings().model,localSettings().effort));
    const syncEfforts = () => {
      const previous = form.effort.value, allowed = form.model.value === 'codex-default' ? [] : (modelInfo(form.model.value)?.reasoning_efforts || []).filter(value => providerFor(form.model.value) === 'codex' || ['high','xhigh','max'].includes(value));
      form.effort.innerHTML = '<option value="">客户端默认</option>' + allowed.map(value => `<option value="${esc(value)}">${esc(value)}</option>`).join('');
      form.effort.value = allowed.includes(previous) ? previous : '';
      form.effort.disabled = !form.model.value || loadingDefaults;
    };
    const updateEditControls = () => {
      if (!isCurrentForm()) return;
      const ready = modelReady(), inherited = usesDefault(), modelName = defaults ? modelInfo(defaultModel())?.name || editModelLabel(defaultModel()) : '';
      const model = selectedModel(), provider = providerFor(model), client = editClient(provider), state = clients?.providers?.[provider];
      $('[data-edit-default]',form).textContent = loadingDefaults ? '正在读取全局默认模型…' : defaultsError ? `全局默认配置暂时不可用：${defaultsError}` :
        defaults ? `${inherited ? '使用全局默认' : '全局默认'}：${modelName}${defaults.reasoning_effort ? ` · ${defaults.reasoning_effort}` : ' · 客户端默认思考强度'}${inherited ? (compatible(defaultModel(),defaults.reasoning_effort) ? '' : '（暂不可用，请在设置中检查连接）') : '（与剪辑不兼容）'}` : '全局默认配置尚未就绪。';
      const status = $('[data-edit-provider-status]',form);
      status.classList.toggle('problem',Boolean(!loadingDefaults && model && (!state?.available || !compatible(model,usesDefault() ? defaults?.reasoning_effort : localSettings().effort))));
      status.textContent = loadingDefaults || !defaults ? '正在检查剪辑客户端…' : clientsError ? `剪辑客户端检查失败：${clientsError}` : !model ? '请选择本次剪辑使用的兼容模型。' : !state?.available ? `${client}暂不可用：${state?.error || state?.label || '请在设置中检查客户端连接。'}` : !compatible(model,usesDefault() ? defaults.reasoning_effort : localSettings().effort) ? `${client}已连接；${catalog?.models_error || '模型或思考强度尚未确认，请在设置中检查。'}` : selectedTimelineRunning() && !recutting() ? '当前目标时间线已有剪辑任务，请选择其他时间线。' : clients.busy ? `${client}可用；可同时准备多条剪辑，需要操作达芬奇时会自动等待。` : `${client}可用于 AI 剪辑。`;
      // A ready client needs no line of its own; only waiting, busy or broken states are shown.
      status.hidden = !status.classList.contains('problem') && status.textContent === `${client}可用于 AI 剪辑。`;
      $('[data-edit-usage]',form).textContent = `AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude，${model ? provider === 'codex' ? '使用 Codex 客户端当前登录账户的额度' : '使用已连接 Claude 账户的订阅额度' : '使用所选客户端当前账户的额度'}。不同时间线可同时准备，操作达芬奇的阶段会自动等待。任务在后台运行，操作达芬奇期间请不要手动改动工程；按技能保存后显示剪辑说明和缺素材项，再交你人工审查，默认不做剪后工程核验。也可以「复制剪辑指令」，粘到 Codex 或 Claude Code 对话里边看边剪。`;
      $('[data-edit-override]',form).hidden = loadingDefaults || Boolean(defaultsError) || !defaults || inherited;
      form.model.disabled = loadingDefaults || Boolean(defaultsError) || inherited;
      form.effort.disabled = form.model.disabled || !form.model.value;
      $('[data-edit-default-refresh]',form).disabled = loadingDefaults || starting;
      const pending = Boolean(pendingTimelineId(form.timeline.value)), submit = $('button[type="submit"]',form);
      submit.disabled = starting || job.archived || (selectedTimelineRunning() && !recutting()) || selectedTimelineUnavailable() || !ready;
      if (!starting) submit.textContent = pending ? '提交剪辑，建好后自动开始' : recutting() ? '另建时间线并开始剪辑' : '开始 AI 剪辑';
      $('[data-recut-row]',form).hidden = !canRecut();
      form.recut.disabled = selectedTimelineRunning();
      $('[data-pending-edit-note]',form).hidden = !pending;
      $('[data-pending-edit-note]',form).textContent = selectedTimelineUnavailable() ? '原建线已取消、失败或不再运行，请重新建线或选择目标时间线。已填写的剪辑要求已保留。' : '可以先提交剪辑；本次时间线建好后自动接着剪，关闭页面也会继续。';
      $('[data-copy-handoff]',form).disabled = pending || selectedTimelineUnavailable() || recutting();
      $('[data-open-target-timeline]',form).disabled = pending || !form.timeline.value;
    };
    refreshEditControls = updateEditControls;
    const loadEditDefaults = async () => {
      if (!isCurrentForm() || starting) return;
      const reading = ++defaultsSequence;
      loadingDefaults = true;defaultsError = '';updateEditControls();
      try {
        const [optionsResult,clientsResult] = await Promise.allSettled([api('/api/ai/options'),api('/api/production/edit-clients')]);
        if (!isCurrentForm() || reading !== defaultsSequence) return;
        clients = clientsResult.status === 'fulfilled' ? clientsResult.value : null;
        clientsError = clientsResult.status === 'rejected' ? fail(clientsResult.reason) : '';
        if (optionsResult.status === 'rejected') throw optionsResult.reason;
        const next = optionsResult.value;
        if (next.defaults_error) throw new Error(next.defaults_error);
        if (!next.default_settings || !Array.isArray(next.models)) throw new Error('请在设置中检查全局模型配置后重试。');
        catalog = next;defaults = next.default_settings;
        const models = new Map([...Object.entries(editModels).map(([id,name]) => [id,{id,name,provider:'claude',available:false}]),['codex-default',modelInfo('codex-default')]]);
        next.models.forEach(entry => {if (entry && typeof entry.id === 'string' && entry.id && ((entry.provider || editProvider(entry.id)) === 'codex' || Object.hasOwn(editModels,entry.id))) {models.set(entry.id,entry);editModelNames.set(entry.id,entry.name || entry.id);}});
        const fingerprint = JSON.stringify([defaults,[...models.values()].map(entry => [entry.id,entry.available,entry.reasoning_efforts])]);
        const previous = fingerprint === defaultsFingerprint && !usesDefault() ? form.model.value : '';
        defaultsFingerprint = fingerprint;
        form.model.innerHTML = '<option value="">请选择兼容模型</option>' + [...models.values()].map(entry => {
          const ready = entry.available !== false && clients?.providers?.[providerFor(entry.id)]?.available;
          return `<option value="${esc(entry.id)}" ${ready ? '' : 'disabled'}>${esc(entry.name || entry.id)}${ready ? '' : ' · 未就绪'}</option>`;
        }).join('');
        form.model.value = previous;
        if (!previous) form.effort.value = '';
        syncEfforts();
      } catch (error) {
        if (!isCurrentForm() || reading !== defaultsSequence) return;
        defaultsError = fail(error);form.model.value = '';form.effort.value = '';defaultsFingerprint = '';
      } finally {
        if (isCurrentForm() && reading === defaultsSequence) {loadingDefaults = false;updateEditControls();}
      }
    };
    form.model.onchange = () => {form.effort.value = '';syncEfforts();updateEditControls();};
    form.effort.onchange = updateEditControls;
    form.timeline.onchange = () => {form.recut.checked = true;updateEditControls();};
    form.recut.onchange = updateEditControls;
    $('[data-edit-default-refresh]',form).onclick = loadEditDefaults;
    view.refreshEditDefaults = loadEditDefaults;
    void loadEditDefaults();
    const materials = () => pickedMaterials(form);
    request.then(status => {
      if (!form.isConnected || seq !== view.sequence) return;
      const box = $('[data-materials]',form);if (!box) return;
      // Each creator's footage sits in their own DaVinci project; when another one is open, list the material
      // timelines recorded when that project's timeline was built.
      const render = () => {
        const record = editTargets(job).find(item => item.unique_id === form.timeline.value), pending = Boolean(pendingTimelineId(form.timeline.value));
        const recorded = Array.isArray(record?.materials) && record.materials.length && (pending || !status.ok || record.project_id && status.project_id !== record.project_id);
        const saved = pending && record?.materials?.length ? record.materials : data.product_settings.material_timelines;
        materialPicker(box,recorded ? {ok:true,timelines:record.materials} : status,new Set((job.timelines || []).map(item => item.name)),choice.edit_materials,saved);
      };
      render();
      form.timeline.addEventListener('change',render);
    });
    const options = () => {
      const values = Object.fromEntries(new FormData(form));
      Object.assign(choice,{edit_timeline:values.timeline,edit_mode:values.mode,edit_instruction:values.instruction,edit_materials:materials()});
      const request = pendingTimelineId(values.timeline);
      return {...(request ? {timeline_request_id:request} : {timeline:values.timeline}),mode:values.mode,instruction:values.instruction.trim(),materials:materials()};
    };
    const rememberEdit = event => {if (event.isTrusted) choice.edit_draft = true;options();};
    form.addEventListener('input',rememberEdit);
    form.addEventListener('change',rememberEdit);
    $('[data-edit-settings]',form).addEventListener('click',options);
    $('[data-copy-handoff]',form).onclick = async event => {
      if (pendingTimelineId(form.timeline.value)) return;
      const button = event.currentTarget;button.disabled = true;setError(form,'');
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/handoff`,{method:'POST',body:options()});await copyText(result.prompt,'剪辑指令已复制，交接文件也已保存。');}
      catch (error) {setError(form,fail(error));}
      finally {button.disabled = false;}
    };
    $('[data-open-target-timeline]',form).onclick = async event => {
      const button = event.currentTarget, uniqueId = form.timeline.value;
      if (!uniqueId || pendingTimelineId(uniqueId)) return;
      button.disabled = true;setError(form,'');
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/open`,{method:'POST',body:{unique_id:uniqueId}});toast(`已在达芬奇打开 ${result.name}。`);}
      catch (error) {setError(form,fail(error));}
      finally {updateEditControls();}
    };
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      if (starting || job.archived) return;
      if (selectedTimelineUnavailable()) {setError(form,'原建线未完成，请重新建线或选择目标时间线。');return;}
      if (selectedTimelineRunning() && !recutting()) {setError(form,'当前目标时间线已有剪辑任务，请选择其他时间线。');return;}
      const body = {...options(),...(usesDefault() ? {} : localSettings())};
      if (recutting()) {body.recut_from = body.timeline;delete body.timeline;}
      const problem = materialProblem(body.materials);
      if (problem) {setError(form,problem);return;}
      if (!modelReady()) {setError(form,defaultsError || (loadingDefaults ? '正在读取全局默认配置，请稍候。' : '请选择本次剪辑使用的兼容模型，或修改全局设置。'));return;}
      const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在加入剪辑任务…';
      starting = true;updateEditControls();
      try {await send(`edit-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edit`,body);toast(body.recut_from ? '已另建时间线并加入剪辑任务，原来的剪辑没有改动。' : body.timeline_request_id ? '已提交剪辑，本次时间线建好后自动接着剪，关闭页面也会继续。' : '已加入剪辑任务，需要操作达芬奇时会自动等待，完成后这里会显示结果。');}
      catch (error) {setError(form,fail(error));starting = false;submit.textContent = '开始 AI 剪辑';updateEditControls();return;}
      choice.edit_draft = false;
      if (seq === view.sequence) await route(true);
    };
  }
  const defaultCreator = (data,choice) => {
    const creators = data.creators || [];
    return choice.creator && creators.includes(choice.creator) ? choice.creator : [...(data.job.timelines || [])].reverse().find(item => creators.includes(item.creator))?.creator || creators[0] || '';
  };
  // One build may differ from the product's DaVinci settings; only the differences are sent.
  const framePresets = [['9:16',1080,1920],['3:4',1080,1440],['4:5',1080,1350],['1:1',1080,1080],['16:9',1920,1080]];
  const frameLabel = (width,height) => framePresets.find(([,w,h]) => w === Number(width) && h === Number(height))?.[0] || `${width}×${height}`;
  function timelineOptions(config,saved = {}) {
    const options = {}, code = String(saved.code || '').trim(), name = String(saved.name || '').trim();
    if (code && code !== config.code) options.code = code;
    if (name) options.name = name;
    if (saved.fps && saved.fps !== config.fps) options.fps = saved.fps;
    if (saved.size) {
      const [width,height] = saved.size.split('x').map(Number);
      if (width !== Number(config.width) || height !== Number(config.height)) Object.assign(options,{width,height});
    }
    if (Number(saved.lead) > 0) options.lead_seconds = Number(saved.lead);
    if (saved.markers === false) options.markers = false;
    return options;
  }
  const timelineOptionsSummary = options => [options.width ? frameLabel(options.width,options.height) : '',options.fps ? `${options.fps} fps` : '',
    options.lead_seconds ? `留白 ${options.lead_seconds} 秒` : '',options.markers === false ? '不写意群标记' : '',
    options.code ? `编号 ${options.code}` : '',options.name ? '自定义名称' : ''].filter(Boolean).join(' · ');
  const timelineBody = (creator,data,choice) => {
    const options = timelineOptions(data.product_settings,choice.timeline_options);
    return Object.keys(options).length ? {creator,options} : {creator};
  };
  const timelinePreview = (creator,data,choice) => String(choice.timeline_options?.name || '').trim() || `${creator}${data.today}-${String(choice.timeline_options?.code || '').trim() || data.product_settings.code}-序号`;
  function timelineOptionsMarkup(config,saved = {},open = false) {
    const own = `${config.width}x${config.height}`, size = saved.size || own, fps = saved.fps || config.fps;
    const sizes = framePresets.map(([label,w,h]) => [`${w}x${h}`,`${label} · ${w}×${h}`]);
    if (!sizes.some(([value]) => value === own)) sizes.unshift([own,`${config.width}×${config.height}`]);
    const summary = timelineOptionsSummary(timelineOptions(config,saved));
    return `<details class="timeline-options" data-timeline-options ${open ? 'open' : ''}><summary>更多选项 <small data-options-summary>${esc(summary || '沿用产品设置')}</small></summary><div class="timeline-options-grid"><label class="field"><span>编号</span><input name="code" maxlength="20" autocomplete="off" value="${esc(saved.code ?? config.code)}"></label><label class="field"><span>名称</span><input name="timeline_name" maxlength="120" autocomplete="off" placeholder="留空自动编号" value="${esc(saved.name || '')}"></label><label class="field"><span>画幅</span><select name="size">${sizes.map(([value,label]) => `<option value="${value}" ${value === size ? 'selected' : ''}>${esc(label)}${value === own ? '（产品设置）' : ''}</option>`).join('')}</select></label><label class="field"><span>帧率</span><select name="fps">${frameRates.map(value => `<option value="${value}" ${value === fps ? 'selected' : ''}>${value}${value === config.fps ? '（产品设置）' : ''}</option>`).join('')}</select></label><label class="field"><span>配音前留白（秒）</span><input name="lead" type="number" min="0" max="10" step="0.1" value="${esc(saved.lead ?? 0)}"></label><label class="timeline-options-check"><input type="checkbox" name="markers" ${saved.markers === false ? '' : 'checked'}>写意群标记</label></div><p class="timeline-options-note" data-markers-note ${saved.markers === false ? '' : 'hidden'}>不写意群标记的时间线只能手动剪辑，不能交给 AI 剪辑。</p></details>`;
  }
  function timelineCards(data) {
    const job = data.job, current = completionTimeline(job), config = data.product_settings || {};
    const timelines = [...(job.timelines || [])].reverse(), active = timelines.filter(item => !item.archived), history = timelines.filter(item => item.archived);
    // Once the order is approved, the older timelines it did not use stay one click away, without looking like work left.
    // The approval rests on the manually completed timeline or the approved edit's; anything built after it stays open.
    const approvedEdit = [...(job.edits || [])].reverse().find(edit => editState(edit) === 'approved');
    const basisUid = [active.find(item => timelineReceipt(job,item))?.unique_id, approvedEdit?.timeline_uid].find(uid => uid && active.some(item => item.unique_id === uid));
    const basis = job.status === 'approved' && basisUid ? (job.timelines || []).findIndex(item => item.unique_id === basisUid) : -1;
    const settled = item => basis >= 0 && (job.timelines || []).indexOf(item) < basis && !(job.edits || []).some(run => run.timeline_uid === item.unique_id && run.status === 'running');
    const card = (item,compact = false) => {
      const version = (job.takes || []).findIndex(take => take.id === item.take_id) + 1;
      const edit = [...(job.edits || [])].reverse().find(run => run.timeline_uid === item.unique_id), state = edit && editState(edit);
      const completed = Boolean(timelineReceipt(job,item));
      const task = Object.hasOwn(timelineTaskLabels,item.task_status) ? item.task_status : completed || state === 'approved' ? 'completed' : edit ? 'in_progress' : 'pending';
      const running = (job.edits || []).some(run => run.timeline_uid === item.unique_id && run.status === 'running');
      const busy = view.timelineWrites?.has(`${job.id}:${item.unique_id}`), locked = job.archived || running || busy;
      const lockTitle = running ? 'AI 正在剪辑，结束后可修改任务状态或归档。' : job.archived ? '恢复制作单后可修改时间线。' : '';
      // A manual completion of this timeline supersedes its AI result, which stays in the edit step's history.
      const status = compact ? '' : completed ? '人工完成' : edit ? `AI 剪辑：${editLabels[state] || state}` : '';
      // Settings that differ from the product's, so a 3:4 or lead-in timeline is recognisable at a glance.
      const format = [item.width && (Number(item.width) !== Number(config.width) || Number(item.height) !== Number(config.height)) ? frameLabel(item.width,item.height) : '',
        config.fps && item.fps !== config.fps ? `${item.fps} fps` : '', item.lead_frames ? `留白 ${(item.lead_frames / editFrameRate(item.fps)).toFixed(1)} 秒` : ''].filter(Boolean);
      const markers = item.group_markers === false ? '未写意群标记（仅手动剪辑）' : `${item.markers} 个意群标记`;
      const taskControl = item.archived ? `<span class="script-status">已归档</span><small>归档前：${esc(timelineTaskLabels[task])}</small>` : `<label class="timeline-task-label"><span title="任务状态记录剪辑进度；AI 执行和人工审查记录独立保留。">任务状态</span><select data-timeline-task="${esc(item.unique_id)}" aria-label="${esc(item.name)}任务状态" ${locked ? 'disabled' : ''} title="${esc(lockTitle || '任务状态记录剪辑进度；AI 执行和人工审查记录独立保留。')}">${Object.entries(timelineTaskLabels).map(([value,label]) => `<option value="${value}" ${value === task ? 'selected' : ''}>${label}</option>`).join('')}</select></label>`;
      return `<article class="timeline-card${current?.unique_id === item.unique_id ? ' current' : ''}${item.archived ? ' archived' : ''}" data-timeline-id="${esc(item.unique_id)}" data-activity-source="${esc(item.unique_id)}"><div class="timeline-info"><strong>${esc(item.name)}${current?.unique_id === item.unique_id ? ' <span class="script-status published">当前使用</span>' : ''}</strong><small>${esc(item.creator)}${compact ? '' : ` · ${markers} · ${(item.frames / Number(item.fps)).toFixed(1)} 秒${format.length ? ` · ${esc(format.join(' · '))}` : ''}`} · <span title="${esc(item.take_id)}">${version ? `配音第 ${version} 版` : `配音 ${esc(item.take_id)}`}${item.take_id === job.selected_take ? '（当前选用）' : ''}</span> · ${esc(readableDate(item.built_at))}</small><div class="timeline-task-state">${taskControl}</div>${status ? `<p class="timeline-edit-state"><span class="script-status${completed || state === 'approved' ? ' published' : ''}" data-timeline-ai-state>${esc(status)}</span>${edit ? ` <small data-timeline-model>${completed ? '' : `最近一次：${esc(readableDate(edit.finished_at || edit.started_at))} · `}${esc(editModelLabel(edit.effective_model || edit.requested_model || edit.model))}</small>` : ''}</p>` : ''}${(item.warnings || []).length ? `<ul class="take-warnings">${item.warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}<p class="ai-error" data-error role="alert" hidden></p></div><div class="timeline-actions"><button type="button" class="secondary" data-open-timeline="${esc(item.unique_id)}">在达芬奇打开</button>${item.archived ? `<button type="button" class="subtle" data-archive-timeline="${esc(item.unique_id)}" ${locked ? 'disabled' : ''} title="${esc(lockTitle || '恢复到时间线列表，保留原任务状态。')}">恢复时间线</button>` : ''}<button type="button" class="subtle" data-delete-timeline="${esc(item.unique_id)}" ${locked ? 'disabled' : ''} title="${esc(lockTitle || '连同这条时间线的剪辑记录和本地文件一起彻底删除，不动达芬奇里的时间线。')}">删除时间线</button></div></article>`;
    };
    const shown = active.filter(item => !settled(item)), others = active.filter(settled);
    return `${shown.map(item => card(item)).join('')}${others.length ? `<details class="timeline-others"><summary>其他时间线（${others.length} 条）</summary>${others.map(item => card(item,true)).join('')}</details>` : ''}${!active.length && history.length ? '<p class="context-note">时间线已全部归档，可展开历史时间线恢复。</p>' : ''}${history.length ? `<details class="timeline-history"><summary>历史时间线（${history.length} 条）</summary>${history.map(item => card(item)).join('')}</details>` : ''}`;
  }
  function timelineSection(data,choice,selected) {
    const job = data.job, timelines = [...activeTimelines(job)].reverse(), creators = data.creators || [], config = data.product_settings;
    const creator = defaultCreator(data,choice), building = job.timeline_building, failure = job.timeline_failure;
    const continuation = building && (job.edits || []).some(edit => edit.status === 'running' && edit.timeline_request_id === building.request_id);
    const queue = building ? `<div class="production-busy timeline-queue" data-timeline-busy><span>${building.stage === 'waiting' ? `建线排队中（${esc(building.creator)}）：达芬奇正在执行另一项任务，空闲后自动开始。` : `正在达芬奇里建时间线（${esc(building.creator)}）…`}${continuation ? ' 剪辑已提交，建好后自动接着剪。' : building.group_markers === true ? ' 可以先提交剪辑，建好后自动接着剪。' : ''}</span>${building.group_markers === true ? `<button type="button" class="subtle" data-prepare-pending-edit>${continuation ? '查看已提交剪辑' : '先提交剪辑'}</button>` : ''}${building.stage === 'waiting' ? `<button type="button" class="subtle" data-cancel-timeline="${esc(building.request_id)}">取消排队</button>` : ''}</div>` : '';
    // A build the user withdrew needs no reminder; one that failed says why, until the next build.
    const failed = !building && failure && !failure.cancelled ? `<p class="production-resolve problem" data-timeline-failure>上次建线没有完成，达芬奇里没有改动：${esc(failure.error || '原因未知')}</p>` : '';
    const form = !selected ? '<p class="context-note">先选用一版配音，再建时间线。</p>'
      : !creators.length ? `<p class="context-note">还没有达人名，请在内容列表的「更多 → 制作设置」里添加。</p>`
      : `<form id="timeline-form" novalidate><div class="form-row"><label class="field"><span>达人</span><select name="creator">${creators.map(name => `<option ${name === creator ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select></label><div></div></div><div class="context-note production-inline-note">新建 <code data-timeline-name>${esc(timelinePreview(creator,data,choice))}</code>${infoTip(`<p>建在该达人文件夹（名称含达人名）里的项目「${esc(config.project)}」里，达芬奇当前开着别的项目时会先保存、关闭再切换；序号按项目里已有的时间线顺延。配音放在单独的「配音」轨（音频 2），每个意群一个蓝色范围标记，备注里写着口播、中文和画面要求；视频轨留空，下一步交给剪辑。</p><p>「更多选项」只影响这一次建线：可改编号或直接起名、换画幅和帧率；配音前留白会把配音往后挪，留白处也要放画面，第一个意群标记从第 0 帧起覆盖它。</p>`)}</div>${timelineOptionsMarkup(config,choice.timeline_options,choice.timeline_options_open)}<div class="take-actions"><button type="submit" class="primary" ${job.archived || building ? 'disabled' : ''}>在达芬奇建时间线</button>${timelines.length && timelines[0].take_id !== job.selected_take ? '<span>当前选用的配音和最近一条时间线用的不同，会另建一条新时间线，旧的保留。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="timeline-panel"><h2>达芬奇时间线 <small data-timeline-count>${timelines.length} 条</small></h2><p class="production-resolve" data-resolve-status>正在检查达芬奇…</p>${queue}${failed}${form}<div id="timeline-cards">${timelineCards(data)}</div></section>`;
  }
  // A queued build finishes on its own; the page re-renders when it starts or ends.
  function watchTimelineBuild(job,seq) {
    const stage = job.timeline_building?.stage, requestId = job.timeline_building?.request_id;
    const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
    const poll = async () => {
      if (!isCurrent()) return;
      if (document.hidden || view.modal) {setTimeout(poll,3000);return;}
      try {
        const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
        if (!isCurrent()) return;
        if (fresh.job.timeline_building?.stage !== stage || fresh.job.timeline_building?.request_id !== requestId) {await route(true);return;}
      } catch {}
      setTimeout(poll,3000);
    };
    setTimeout(poll,3000);
  }
  function bindTimeline(data,choice,seq,request) {
    const job = data.job, panel = $('#timeline-panel'), form = $('#timeline-form');
    const resolveNote = $('[data-resolve-status]',panel);
    if (job.timeline_building) resolveNote.hidden = true;
    else resolveStatus(resolveNote,data.product_settings,request,true);
    $('[data-prepare-pending-edit]',panel)?.addEventListener('click',() => {
      choice.edit_timeline = `pending:${job.timeline_building.request_id}`;
      const target = $('#edit-panel #edit-form [name="timeline"]');
      if (target) {target.value = choice.edit_timeline;target.dispatchEvent(new Event('change'));}
      $('.production-steps [data-step="edit"]')?.click();
    });
    const cancel = $('[data-cancel-timeline]',panel);
    if (cancel) cancel.onclick = async () => {
      cancel.disabled = true;
      try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/timeline/cancel`,{method:'POST',body:{request_id:cancel.dataset.cancelTimeline}});toast('已取消排队，没有建时间线。');}
      catch (error) {toast(fail(error),true);cancel.disabled = false;return;}
      if (seq === view.sequence) await route(true);
    };
    if (job.timeline_building) watchTimelineBuild(job,seq);
    if (form) {
      const sync = () => {
        choice.creator = form.creator.value;
        choice.timeline_options = {code:form.code.value,name:form.timeline_name.value,size:form.size.value,fps:form.fps.value,lead:form.lead.value,markers:form.markers.checked};
        $('[data-timeline-name]',form).textContent = timelinePreview(form.creator.value,data,choice);
        $('[data-options-summary]',form).textContent = timelineOptionsSummary(timelineOptions(data.product_settings,choice.timeline_options)) || '沿用产品设置';
        $('[data-markers-note]',form).hidden = form.markers.checked;
      };
      form.oninput = sync;form.onchange = sync;
      $('[data-timeline-options]',form).addEventListener('toggle',event => {choice.timeline_options_open = event.currentTarget.open;});
      form.onsubmit = async event => {
        event.preventDefault();setError(form,'');sync();
        const lead = Number(form.lead.value || 0);
        if (!/^[A-Za-z0-9]{1,20}$/.test(form.code.value.trim())) {setError(form,'时间线编号只能是 1–20 位字母或数字。');return;}
        if (!Number.isFinite(lead) || lead < 0 || lead > 10) {setError(form,'配音前留白须在 0–10 秒之间。');return;}
        const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在达芬奇里建时间线…';
        try {
          const result = await send(`timeline-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/timeline`,timelineBody(form.creator.value,data,choice));
          if (result.queued && result.can_submit_edit && form.markers.checked) {choice.step = 'edit';choice.edit_timeline = `pending:${result.request_id}`;}
          toast(result.queued ? result.message : `已在达芬奇建好时间线 ${result.timeline.name}。`);
        }
        catch (error) {setError(form,definitive(error) || error.status === 409 ? fail(error) : `结果尚未确认：${fail(error)}。请先到达芬奇里看是否已经建好，再决定是否重试。`);submit.disabled = false;submit.textContent = '在达芬奇建时间线';return;}
        if (seq === view.sequence) await route(true);
      };
    }
    bindTimelineCards(data);
  }
  function bindTimelineCards(data) {
    const job = data.job, panel = $('#timeline-panel');
    const count = $('[data-timeline-count]',panel);if (count) count.textContent = `${activeTimelines(job).length} 条`;
    $$('[data-open-timeline]',panel).forEach(button => button.onclick = async () => {
      button.disabled = true;
      try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/open`,{method:'POST',body:{unique_id:button.dataset.openTimeline}});toast(`已在达芬奇打开 ${result.name}。`);}
      catch (error) {toast(fail(error),true);}
      finally {button.disabled = false;}
    });
    const save = async (uid,action,body) => {
      const key = `${job.id}:${uid}`, writes = view.timelineWrites ||= new Set();
      if (writes.has(key)) return;
      const card = () => $$('[data-timeline-id]',panel).find(node => node.dataset.timelineId === uid);
      const original = card();if (!original) return;
      writes.add(key);setError(original,'');
      $$('[data-timeline-task],[data-archive-timeline],[data-delete-timeline]',original).forEach(node => {node.disabled = true;});
      let saved = false;
      try {
        await send(`timeline-${action}-${key}`,`/api/production/jobs/${encodeURIComponent(job.id)}/timeline/${action}`,{revision:data.revision,timeline_uid:uid,...body});
        saved = true;writes.delete(key);
        await refreshProductionDetail(data,'video',{scope:'timeline'});
        toast(action === 'archive' ? body.archived ? '已归档到历史时间线。' : '已恢复时间线。' : '已更新任务状态。');
      } catch (error) {
        if (!saved && error.status === 409) {writes.delete(key);try {await refreshProductionDetail(data,'video',{scope:'timeline'});} catch {}}
        const node = card();
        if (node) {
          setError(node,saved ? `修改已保存，列表刷新未完成：${fail(error)}。请刷新页面查看。` : fail(error));
          const item = job.timelines.find(item => item.unique_id === uid), select = $('[data-timeline-task]',node);
          if (select) select.value = item?.task_status || select.querySelector('option[selected]')?.value || 'pending';
        }
        toast(saved ? '修改已保存，请刷新页面查看。' : fail(error),true);
      } finally {
        writes.delete(key);
        const node = card();if (node) $$('[data-timeline-task],[data-archive-timeline],[data-delete-timeline]',node).forEach(control => {control.disabled = Boolean(job.archived || (job.edits || []).some(edit => edit.timeline_uid === uid && edit.status === 'running'));});
      }
    };
    $$('[data-archive-timeline]',panel).forEach(button => button.onclick = () => {
      if (button.disabled) return;
      const item = job.timelines.find(item => item.unique_id === button.dataset.archiveTimeline);
      if (item) void save(item.unique_id,'archive',{archived:!item.archived});
    });
    // Permanent removal of one timeline with its edit runs and local files; the DaVinci timeline is left alone.
    $$('[data-delete-timeline]',panel).forEach(button => button.onclick = async () => {
      if (button.disabled) return;
      const item = job.timelines.find(item => item.unique_id === button.dataset.deleteTimeline);
      if (!item) return;
      const base = `/api/production/jobs/${encodeURIComponent(job.id)}/timeline/delete`;
      let preview;
      try {preview = await api(base,{method:'POST',body:{preview:true,timeline_uid:item.unique_id}});}
      catch (error) {toast(fail(error),true);return;}
      const bytes = preview.bytes || 0, size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
      const completed = Boolean(timelineReceipt(job,item)) || (job.edits || []).some(edit => edit.timeline_uid === item.unique_id && edit.approval);
      openModal('删除时间线',`<p class="context-note">将彻底删除「${esc(preview.name || item.name)}」在工作台里的全部资料：时间线记录${preview.edits ? `、${preview.edits} 次 AI 剪辑记录` : ''}、人工完成标记，以及本地保存的交接文件、剪辑结果和候选画面（${preview.files} 个，共约 ${esc(size)}）。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程，需要的话请自行在达芬奇里删除。</li><li>配音和制作单本身保留。</li></ul>${completed ? '<p class="ai-error">这条时间线已标记完成；删除后制作单状态会重新计算。</p>' : ''}`,'确认删除',async modal => {
        const result = await once(modal,'timeline-delete',base,async () => ({timeline_uid:item.unique_id}));
        closeModal(true);
        const warnings = result.warnings || [];
        toast(warnings.length ? warnings[0] : '时间线及本地文件已删除。',warnings.length > 0);
        try {await refreshProductionDetail(data,'video',{scope:'timeline'});}
        catch {toast('已删除，列表刷新未完成，请刷新页面查看。',true);}
      });
    });
    $$('[data-timeline-task]',panel).forEach(select => select.onchange = () => {
      if (!select.disabled) void save(select.dataset.timelineTask,'task-status',{task_status:select.value});
    });
  }

  // Last voice slider values per model, kept in this browser so a new job starts from them instead of the defaults.
  const voiceSettingsKey = model => `workbench.voiceSettings.${model}`;
  const lastVoiceSettings = model => {try {const value = JSON.parse(localStorage.getItem(voiceSettingsKey(model)) || 'null');return value && typeof value === 'object' ? value : null;} catch {return null;}};
  const keepVoiceSettings = (model,values) => {try {localStorage.setItem(voiceSettingsKey(model),JSON.stringify(values));} catch {}};
  function settingsMarkup(model,values) {
    return (model?.settings || []).map(key => {
      const [min,max,step,fallback] = ranges[key], value = values?.[key] ?? fallback;
      return `<label class="field take-setting"><span>${settingLabels[key]} <output>${Number(value).toFixed(2)}</output></span><input type="range" name="setting-${key}" min="${min}" max="${max}" step="${step}" value="${value}"></label>`;
    }).join('');
  }

  // The chosen variant as written; each spoken, on-screen and 🔴 publish paragraph gets its own copy button.
  function fallbackScript(lines) {
    const block = values => '```text\n' + values.join('\n') + '\n```';
    return `**口播（配音用）**\n${block(lines.map(line => line.spoken))}` + (lines.some(line => line.zh) ? `\n\n**对应中文**\n${block(lines.map(line => line.zh || ''))}` : '');
  }
  function copyButton(label,text,message) {
    const button = document.createElement('button');
    button.type = 'button';button.className = 'script-copy';button.title = label;button.setAttribute('aria-label',label);
    button.innerHTML = icon('copy');
    button.onclick = event => {event.preventDefault();event.stopPropagation();void copyText(text,message);};
    return button;
  }
  // The copy icon lives inside the module it copies (top-right corner of a block, or right after an inline line).
  function boxed(node,button) {
    const wrap = document.createElement('div');
    wrap.className = 'script-block';node.replaceWith(wrap);wrap.append(node,button);
  }
  function renderScript(root,markdown) {
    if (!root) return;
    root.innerHTML = renderMarkdown(markdown);
    let label = '';
    [...root.children].forEach(node => {
      const text = (node.innerText || node.textContent || '').trim();
      if (/^H[2-6]$/.test(node.tagName) || (node.tagName === 'P' && node.children.length === 1 && node.firstElementChild.tagName === 'STRONG' && node.firstElementChild.textContent.trim() === node.textContent.trim())) {label = text;return;}
      if (node.tagName === 'PRE') {
        const code = node.textContent.replace(/\s+$/,''), marked = code.split('\n').filter(line => line.includes('🔴'));
        if (marked.length) boxed(node,copyButton(marked.length > 1 ? '复制这一套发布文案' : '复制发布文案',marked.join('\n'),'发布文案已复制'));
        else if (/中文/.test(label)) boxed(node,copyButton('复制中文',code,'中文已复制'));
        else if (/口播|英文|English/i.test(label)) boxed(node,copyButton('复制英文口播',code,'英文口播已复制'));
        else boxed(node,copyButton('复制',code,'已复制'));
        return;
      }
      if (node.tagName === 'P' && /^开头屏显[:：]/.test(text)) {
        const shown = node.querySelector('code')?.textContent.trim() || text.replace(/^开头屏显[:：]\s*/,'').trim();
        node.classList.add('script-inline');node.append(copyButton('复制屏显文字',shown,'屏显文字已复制'));
        return;
      }
      const marked = text.split('\n').map(line => line.trim()).filter(line => line.includes('🔴'));
      if (marked.length && ['P','UL','OL'].includes(node.tagName)) boxed(node,copyButton(marked.length > 1 ? '复制这一套发布文案' : '复制发布文案',marked.join('\n'),'发布文案已复制'));
    });
  }

  const timedVoiceGroup = (take,group) => typeof group?.start === 'number' && Number.isFinite(group.start) && group.start >= 0 &&
    typeof group.end === 'number' && Number.isFinite(group.end) && group.end > group.start &&
    (!(typeof take.duration === 'number' && take.duration > 0) || group.start < take.duration && group.end <= take.duration + .1);
  function voiceSegments(take,lines) {
    const groups = Array.isArray(take.groups) ? take.groups : [], valid = groups.filter(group => timedVoiceGroup(take,group));
    const duration = Number.isFinite(take.duration) && take.duration > 0 ? take.duration : Math.max(0,...valid.map(group => group.end));
    if (!valid.length || duration <= 0) return '<p class="context-note">还没有有效的意群时间，暂不能分段试听。</p>';
    const low = groups.filter(group => typeof group.coverage !== 'number' || !Number.isFinite(group.coverage) || group.coverage < .6 || group.coverage > 1).length;
    return `<div class="voice-segments"><div class="voice-segments-head"><strong>按意群试听</strong><small>${esc(seconds(duration))}${low ? ` · ${low} 个意群需核对对齐` : ''}</small></div><div class="voice-segment-bar" role="group" aria-label="按配音时间排列的意群" title="点击编号试听这一段；红色表示对齐不足 60% 或时间需核对">${groups.map((group,index) => {
      if (!timedVoiceGroup(take,group)) return '';
      const coverage = typeof group.coverage === 'number' && Number.isFinite(group.coverage) ? Math.round(group.coverage*100) : null;
      const warning = coverage === null || group.coverage < .6 || group.coverage > 1;
      const title = `#${group.number} · ${seconds(group.start)}–${seconds(group.end)} · 对齐 ${coverage === null ? '未知' : `${coverage}%`}${warning ? '，需核对' : ''}${lines.find(line => line.number === group.number)?.spoken ? ` · ${lines.find(line => line.number === group.number).spoken}` : ''}`;
      return `<button type="button" class="voice-segment${warning ? ' warning' : ''}" data-play-group="${index}" data-voice-left="${(group.start/duration*100).toFixed(4)}" data-voice-width="${((Math.min(group.end,duration)-group.start)/duration*100).toFixed(4)}" title="${esc(title)}" aria-label="试听意群 ${esc(title)}">${esc(group.number)}</button>`;
    }).join('')}</div><p class="voice-segment-status" data-group-playback role="status"></p></div>`;
  }
  function voiceTimingRows(take) {
    return (Array.isArray(take.groups) ? take.groups : []).map((group,index) => {
      const valid = timedVoiceGroup(take,group), coverage = typeof group.coverage === 'number' && Number.isFinite(group.coverage) ? Math.round(group.coverage*100) : null;
      return `<tr data-voice-row="${index}" class="${valid ? 'voice-timing-row' : ''}${coverage === null || group.coverage < .6 || group.coverage > 1 ? ' warning' : ''}"><td><button type="button" class="subtle voice-group-play" data-play-group="${index}" ${valid ? '' : 'disabled'} aria-label="试听意群 ${esc(group.number)}">${esc(group.number)} ▷</button></td><td>${esc(seconds(group.start))}</td><td>${esc(seconds(group.end))}</td><td>${coverage === null ? '—' : `${coverage}%`}</td></tr>`;
    }).join('');
  }
  function bindTakeSegments(data) {
    const players = [];
    const metadata = audio => audio.readyState >= 1 ? Promise.resolve() : new Promise((resolve,reject) => {
      const finish = error => {clearTimeout(timer);audio.removeEventListener('loadedmetadata',loaded);audio.removeEventListener('error',failed);error ? reject(error) : resolve();};
      const loaded = () => finish(), failed = () => finish(new Error('配音音频暂时无法读取。'));
      const timer = setTimeout(() => finish(new Error('读取配音超时，请重试。')),10000);
      audio.addEventListener('loadedmetadata',loaded,{once:true});audio.addEventListener('error',failed,{once:true});audio.preload = 'metadata';audio.load();
    });
    $$('[data-take-id]',$('#take-panel')).forEach(card => {
      if (card._voicePlayer) {players.push(card._voicePlayer);return;}
      const take = data.job.takes.find(item => item.id === card.dataset.takeId), audio = $('audio',card), status = $('[data-group-playback]',card);
      if (!take || !audio) return;
      $$('.voice-segment',card).forEach(button => {button.style.left = `${Number(button.dataset.voiceLeft)}%`;button.style.width = `${Number(button.dataset.voiceWidth)}%`;});
      let playing = null, request = 0;
      const mark = index => {
        $$('[data-play-group]',card).forEach(button => button.classList.toggle('active',Number(button.dataset.playGroup) === index));
        $$('[data-voice-row]',card).forEach(row => row.classList.toggle('active',Number(row.dataset.voiceRow) === index));
      };
      const play = async index => {
        const group = take.groups?.[index];if (!timedVoiceGroup(take,group)) return;
        const current = ++request;players.forEach(player => {if (player.audio !== audio) player.stop();});audio.pause();playing = null;
        if (status) status.textContent = `正在读取意群 #${group.number}…`;
        try {
          await metadata(audio);if (current !== request || !audio.isConnected) return;
          audio.currentTime = group.start;playing = group;mark(index);
          if (status) status.textContent = `试听 #${group.number} · ${seconds(group.start)}–${seconds(group.end)}`;
          await audio.play();
        } catch (error) {if (current === request && audio.isConnected) {playing = null;mark(null);if (status) status.textContent = fail(error);toast(fail(error),true);}}
      };
      const stop = () => {request++;playing = null;audio.pause();mark(null);};
      card._voicePlayer = {audio,stop,play,take};players.push(card._voicePlayer);
      $$('[data-play-group]',card).forEach(button => button.onclick = () => void play(Number(button.dataset.playGroup)));
      $$('.voice-timing-row',card).forEach(row => row.onclick = event => {if (!event.target.closest('button')) void play(Number(row.dataset.voiceRow));});
      audio.addEventListener('timeupdate',() => {if (playing && audio.currentTime >= playing.end) {const group = playing;audio.pause();playing = null;mark(null);if (status) status.textContent = `意群 #${group.number} 试听结束。`;}});
      audio.addEventListener('play',() => players.forEach(player => {if (player.audio !== audio) player.stop();}));
      audio.addEventListener('seeked',() => {if (playing && (audio.currentTime < playing.start || audio.currentTime >= playing.end)) {playing = null;mark(null);}});
    });
    // The script table uses the selected take's player as well, so its text can be checked against sound.
    const selected = players.find(player => player.take.id === data.job.selected_take);
    if (selected) $$('#job-script-panel .production-table tbody tr').forEach((row,index) => {
      const number = data.job.lines[index]?.number, groupIndex = selected.take.groups.findIndex(group => group.number === number && timedVoiceGroup(selected.take,group));
      if (groupIndex < 0) return;
      row.classList.add('voice-timing-row');row.tabIndex = 0;row.setAttribute('role','button');row.setAttribute('aria-label',`试听意群 ${number}`);
      row.onclick = () => void selected.play(groupIndex);row.onkeydown = event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();void selected.play(groupIndex);}};
    });
  }
  function takeSelectionMarkup(data,take) {
    const job = data.job, choice = view.choice[job.id] || {};
    if (take.id === job.selected_take) return '<span class="script-status published">当前使用</span>';
    const usable = takeSelectable(take,job.lines.length) && !job.archived;
    const disabled = usable ? '' : 'disabled title="意群时间无效或对齐覆盖不足 60%，请先核对配音"';
    // A take that has no timeline yet is selected and built in one go; rebuilding stays in the timeline step.
    const build = (data.creators || []).length > 0 && !job.archived && !job.timeline_building && !activeTimelines(job).some(timeline => timeline.take_id === take.id);
    if (!build) return `<button type="button" class="secondary" data-select-take="${esc(take.id)}" ${disabled}>选用这一版</button>`;
    const creators = data.creators, creator = defaultCreator(data,choice);
    const options = timelineOptionsSummary(timelineOptions(data.product_settings,choice.timeline_options));
    const picker = creators.length > 1 ? `<select class="take-build-creator" data-build-creator aria-label="建线达人" ${usable ? '' : 'disabled'}>${creators.map(name => `<option ${name === creator ? 'selected' : ''}>${esc(name)}</option>`).join('')}</select>` : '';
    return `<div class="take-select-actions">${picker}<button type="button" class="secondary" data-select-take="${esc(take.id)}" data-build="${esc(creator)}" ${options ? `title="按「达芬奇时间线」里的更多选项建线：${esc(options)}"` : ''} ${disabled}>${creators.length > 1 ? '选用并建时间线' : `选用并建时间线（${esc(creator)}）`}</button><button type="button" class="subtle" data-select-only="${esc(take.id)}" ${disabled}>只选用</button></div>`;
  }
  function bindTakeSelection(data,seq = view.sequence) {
    const job = data.job, choice = view.choice[job.id] ||= {}, panel = $('#take-panel');
    $$('[data-build-creator]',panel).forEach(select => select.onchange = () => {
      choice.creator = select.value;
      $$('[data-build]',panel).forEach(button => {button.dataset.build = select.value;});
      $$('[data-build-creator]',panel).forEach(other => {other.value = select.value;});
      const form = $('#timeline-form');
      if (form && form.creator.value !== select.value) {form.creator.value = select.value;form.dispatchEvent(new Event('change'));}
    });
    const pick = async (button,takeId,creator) => {
      button.disabled = true;
      try {await send(`select-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/select`,{revision:data.revision,take_id:takeId});}
      catch (error) {toast(fail(error),true);button.disabled = false;if (error.status === 409 && seq === view.sequence) await refreshProductionDetail(data,'video',{scope:'take'});return;}
      if (!creator) {toast('已选用这一版配音。');if (seq === view.sequence) await refreshProductionDetail(data,'video',{scope:'take'});return;}
      button.textContent = '已选用，正在达芬奇里建时间线…';
      let queued = false;
      try {
        const result = await send(`timeline-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/timeline`,timelineBody(creator,data,choice));
        choice.creator = creator;queued = result.queued === true;
        if (queued && result.can_submit_edit && choice.timeline_options?.markers !== false) {choice.step = 'edit';choice.edit_timeline = `pending:${result.request_id}`;}
        toast(queued ? `已选用这一版配音。${result.message}` : `已选用这一版配音，并在达芬奇建好时间线 ${result.timeline.name}。`);
      }
      catch (error) {toast(`已选用这一版配音，但时间线${definitive(error) || error.status === 409 ? `没建成：${fail(error)}` : `结果尚未确认：${fail(error)}。请先到达芬奇里看是否已经建好`}。可在「达芬奇时间线」步骤重试。`,true);}
      // Queued builds can already be submitted for editing; the server continues when they are built.
      if (seq === view.sequence) await (queued ? route(true) : refreshProductionDetail(data,'video',{scope:'take'}));
    };
    $$('[data-select-take]',panel).forEach(button => button.onclick = () => pick(button,button.dataset.selectTake,button.dataset.build || ''));
    $$('[data-select-only]',panel).forEach(button => button.onclick = () => pick(button,button.dataset.selectOnly,''));
  }
  function renderJob(data,seq) {
    const job = data.job, takes = [...job.takes].reverse(), selected = job.takes.find(take => take.id === job.selected_take);
    const voices = (data.voices || []).filter(voice => !voice.disabled), models = data.models || [];
    const choice = view.choice[job.id] ||= {};
    const lastTake = job.takes[job.takes.length - 1];
    const voiceId = choice.voice_id || data.defaults?.[job.product] || lastTake?.voice_id || voices[0]?.voice_id || '__new';
    const modelId = choice.model_id || voices.find(voice => voice.voice_id === voiceId)?.model_id || data.default_model;
    const voiceName = id => {const voice = (data.voices || []).find(item => item.voice_id === id);return voice ? voiceLabel(voice) : id;};
    const modelName = id => models.find(item => item.id === id)?.name || id;
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content',job.product))}">${icon('back')}返回内容</a><div class="page-heading"><div><span class="eyebrow">PRODUCTION JOB</span><h1 data-production-title>${esc(productionTitle(job))}</h1><p>${esc(job.product)} · ${esc(job.direction_id || '未指定方向')}${variantSubtitle(productionTitle(job),job.variant_label) ? ` · ${esc(variantSubtitle(productionTitle(job),job.variant_label))}` : ''} · <span class="script-status ${['voiced','timeline','approved'].includes(job.status) ? 'published' : ''}">${esc(job.archived ? '已归档' : statusLabels[job.status] || job.status)}</span>${data.script ? ` · 采用稿 · 第 ${esc(data.script.version || 1)} 版` : ' · 采用稿已不可读'}${job.replica ? ` · 照原片 <a href="${esc(url(`#case/${encodeURIComponent(job.replica.case_id)}`,job.product))}">${esc(job.replica.case_id)}</a>（${job.replica.shots.length} 个镜头）` : ''}</p></div><details class="script-more-actions production-more-actions"><summary>更多操作</summary><div><button class="subtle" id="job-archive">${job.archived ? '恢复制作单' : '归档制作单'}</button><button class="subtle production-delete" id="job-delete">永久删除</button></div></details></div>${recoverySection(job)}${problemsMarkup(job.problems,scriptGaps(job,data) && !fillRunning(job))}<section class="section-card" id="job-script-panel"><div class="section-title-row"><h2>脚本 <small>${esc(job.variant_label || data.variant?.label || '')}${job.variant_label || data.variant?.label ? ' · ' : ''}${job.lines.length} 个意群</small></h2>${fillControls(job,data)}</div><div id="job-adopted-script"></div><div class="script-lines"><h3>意群表 <small>含画面要求${selected ? '与配音时间' : ''}</small></h3>${linesTable(job.lines,selected ? selected.groups : null)}</div><details class="script-original" id="job-script-original" ${choice.script_original_open ? 'open' : ''}><summary>原稿全文 <small>开头画面、屏显、口播与中文、发布文案等，可逐段复制</small></summary><div class="script-variant markdown-body" id="job-script-body"></div></details></section><section class="section-card" id="voice-panel"><h2>配音</h2>${job.voicing ? '<p class="production-busy">正在生成配音，完成后自动刷新…</p>' : ''}<form id="take-form" class="take-form"><div class="form-row"><label class="field"><span>音色</span><select name="voice">${voices.map(voice => `<option value="${esc(voice.voice_id)}" ${voice.voice_id === voiceId ? 'selected' : ''}>${esc(voiceLabel(voice))}${data.defaults?.[job.product] === voice.voice_id ? '（默认）' : ''}</option>`).join('')}<option value="__new" ${voiceId === '__new' ? 'selected' : ''}>新音色 ID…</option></select></label><label class="field"><span>模型</span><select name="model_id">${models.map(model => `<option value="${esc(model.id)}" ${model.id === modelId ? 'selected' : ''}>${esc(model.name)}</option>`).join('')}</select></label></div><div class="form-row" data-new-voice ${voiceId === '__new' ? '' : 'hidden'}><label class="field"><span>新音色 ID</span><input name="new_voice_id" pattern="[A-Za-z0-9]{10,64}" autocomplete="off" value="${esc(choice.new_voice_id || '')}"></label><label class="field"><span>备注（存入音色库）</span><input name="new_note" maxlength="500" value="${esc(choice.new_note || '')}"></label></div><div class="take-settings" data-settings></div><details class="take-text"><summary>送去配音的文字（${data.tts.characters} 字符${data.tts.substitutions ? `，已套用读法 ${data.tts.substitutions} 处` : ''}）</summary><pre>${esc(data.tts.text)}</pre></details><div class="take-actions"><button class="primary" type="submit" ${job.voicing || job.archived ? 'disabled' : ''}>生成配音</button><span>约消耗 ${data.tts.characters} 字符额度</span></div><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="section-card" id="take-panel"><h2>配音版本 <small>${takes.length} 版</small></h2>${takes.length ? takes.map(take => `<article class="take-card ${take.id === job.selected_take ? 'selected' : ''}" data-take-id="${esc(take.id)}" data-activity-source="${esc(take.id)}"><div class="take-head"><div><strong>${esc(voiceName(take.voice_id))}</strong><small>${esc(modelName(take.model_id))} · ${Object.keys(settingLabels).filter(key => key in (take.settings || {})).map(key => `${settingLabels[key]} ${take.settings[key]}`).join(' · ')} · ${esc(seconds(take.duration))} · ${esc(readableDate(take.created_at))}</small></div>${takeSelectionMarkup(data,take)}</div><audio controls preload="none" src="${esc(audioURL(job.id,take.id,'mp3'))}"></audio>${voiceSegments(take,job.lines)}${(take.warnings || []).length ? `<ul class="take-warnings">${take.warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}<details><summary>意群时间（${esc(sourceLabels[take.alignment_source] || take.alignment_source)}）</summary><table class="production-table compact"><thead><tr><th>#</th><th>开始</th><th>结束</th><th>对上的字</th></tr></thead><tbody>${voiceTimingRows(take)}</tbody></table></details><div class="take-links" title="${esc(take.id)}">${take.wav ? `<a href="${esc(audioURL(job.id,take.id,'wav',true))}">下载 WAV</a>` : ''}<a href="${esc(audioURL(job.id,take.id,'mp3',true))}">下载 MP3</a></div></article>`).join('') : '<p class="context-note">还没有配音。</p>'}</section>${timelineSection(data,choice,selected)}${editSection(data,choice)}`;
    window.CaseScripts?.mountAdopted?.($('#job-adopted-script'),data.script,{productionId:job.id,kind:'video'});
    window.WorkbenchLinks?.mount({kind:'video',record:job,script:data.script});
    mountProductionNeeds(data,'video');
    bindTakeSegments(data);
    const publicationPanel = document.createElement('section');
    publicationPanel.id = 'video-publication-panel';publicationPanel.className = 'section-card';
    $('#content').append(publicationPanel);
    window.VideoPublication?.mount(publicationPanel,{job,api,onLoaded:publication => {
      if (view.progress?.id !== job.id || !publicationPanel.isConnected) return;
      view.progress.publication = publication;refreshProductionProgress(view.progress.data,'video');
    },onSaved:async () => {if(seq === view.sequence) await route(true);},onTimelineChanged:() => refreshProductionDetail(data,'video',{scope:'timeline'})});
    bindRecovery(job);
    renderScript($('#job-script-body'),data.variant?.markdown || fallbackScript(job.lines));
    $('#job-script-original')?.addEventListener('toggle',event => {choice.script_original_open = event.currentTarget.open;});
    const edits = job.edits || [], built = activeTimelines(job).some(item => selected && item.take_id === selected.id);
    if (selected && !job.voicing) {
      $('#voice-panel').before($('#take-panel'));
      foldProductionControls(job.id,'voice',[$('#take-form')],'再生成一版配音');
    }
    mountSteps(job.id,productionSteps(data,'video'),job.voicing || (!selected && !edits.length) ? 'voice' : selected && !built ? 'timeline' : 'edit');
    mountProductionProgress(data,'video');
    const form = $('#take-form');
    const syncSettings = () => {
      const model = models.find(item => item.id === form.model_id.value), voice = voices.find(item => item.voice_id === form.voice.value);
      const ownTake = [...job.takes].reverse().find(take => take.model_id === model.id && take.settings && Object.keys(take.settings).length);
      const stored = choice.settings?.[model.id] || ownTake?.settings || lastVoiceSettings(model.id) || (voice?.model_id === model.id ? voice.settings : null);
      $('[data-settings]',form).innerHTML = settingsMarkup(model,stored);
      $$('input[type="range"]',form).forEach(input => input.oninput = () => {$('output',input.closest('label')).textContent = Number(input.value).toFixed(2);remember();});
      $('[data-new-voice]',form).hidden = form.voice.value !== '__new';
    };
    const remember = () => {
      const model = form.model_id.value, settings = Object.fromEntries($$('input[type="range"]',form).map(input => [input.name.replace('setting-',''),Number(input.value)]));
      Object.assign(choice,{voice_id:form.voice.value,model_id:model,new_voice_id:form.new_voice_id.value.trim(),new_note:form.new_note.value.trim()});
      (choice.settings ||= {})[model] = settings;keepVoiceSettings(model,settings);return settings;
    };
    form.voice.onchange = () => {const voice = voices.find(item => item.voice_id === form.voice.value);if (voice?.model_id) form.model_id.value = voice.model_id;syncSettings();};
    form.model_id.onchange = syncSettings;syncSettings();
    form.onsubmit = async event => {
      event.preventDefault();setError(form,'');
      const settings = remember(), submit = $('button[type="submit"]',form);
      let voice = form.voice.value;
      submit.disabled = true;submit.textContent = '正在生成…（通常 10–60 秒）';
      let awaitingVoice = true;
      job.voicing = true;job.voicing_live = {started_at:new Date().toISOString(),stage:'voice_prepare',progress:'正在准备配音'};
      refreshProductionProgress(data,'video');mountRuntimeTicker();
      const watchVoice = async () => {
        if (!awaitingVoice || seq !== view.sequence || !form.isConnected) return;
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (awaitingVoice && seq === view.sequence && fresh.job.voicing) refreshProductionProgress(fresh,'video');
        } catch {}
        if (awaitingVoice && seq === view.sequence) setTimeout(watchVoice,2000);
      };
      setTimeout(watchVoice,1000);
      try {
        if (voice === '__new') {
          voice = choice.new_voice_id;
          if (!/^[A-Za-z0-9]{10,64}$/.test(voice)) throw Object.assign(new Error('请填写有效的新音色 ID。'),{status:400});
          await send(`voice-add-${voice}`,'/api/production/voices',{revision:data.revision,operation:'save',voice:{voice_id:voice,note:choice.new_note,model_id:form.model_id.value,settings}});
          choice.voice_id = voice;
        }
        await send(`take-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/takes`,{voice_id:voice,model_id:form.model_id.value,settings});
        toast('配音已生成，试听后可以选用。');
      } catch (error) {
        awaitingVoice = false;job.voicing = false;delete job.voicing_live;$('.production-runtime',$('#voice-panel'))?.remove();refreshProductionProgress(data,'video');
        setError(form,definitive(error) ? fail(error) : `结果尚未确认：${fail(error)}。重试会沿用同一请求，已取得的配音可接续恢复；结果仍未知时请先核实 ElevenLabs 记录。`);
        submit.disabled = false;submit.textContent = '生成配音';return;
      }
      awaitingVoice = false;
      if (seq === view.sequence) await route(true);
    };
    bindTakeSelection(data,seq);
    $$('.production-more-actions button').forEach(button => button.addEventListener('click',() => {button.closest('details').open = false;}));
    $('#job-delete')?.addEventListener('click',() => void deleteOrder(job));
    $('#job-archive').onclick = async () => {
      try {await send(`archive-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/archive`,{revision:data.revision,archived:!job.archived});toast(job.archived ? '制作单已恢复。' : '制作单已归档，配音文件保留。');await route(true);}
      catch (error) {toast(fail(error),true);}
    };
    const openScriptFill = () => {
      // Spoken lines and Chinese belong to the adopted script version; only what the writing skill would have added is filled here.
      const extra = job.supplement || {}, manual = manualScript(data), captions = [...(extra.captions || []),'','',''].slice(0,3);
      const head = manual ? `<fieldset class="line-edit"><legend>开头</legend><label class="field"><span>开头画面</span><textarea name="hook_visual" rows="2" maxlength="1000" placeholder="前 1–3 秒镜头里看得见的事件，和首句口播说同一件事">${esc(extra.hook_visual || '')}</textarea></label><div class="form-row"><label class="field"><span>开头屏显（英文，/ 表示断行）</span><input name="hook_text" maxlength="300" value="${esc(extra.hook_text || '')}" placeholder="不放可留空"></label><label class="field"><span>屏显中文</span><input name="hook_text_zh" maxlength="300" value="${esc(extra.hook_text_zh || '')}"></label></div></fieldset>` : '';
      const tail = manual ? `<fieldset class="line-edit"><legend>收尾</legend><label class="field"><span>补拍清单</span><textarea name="pickups" rows="3" maxlength="4000" placeholder="本稿必须有、但还没有已核素材的镜头，写清拍到什么才算数">${esc(extra.pickups || '')}</textarea></label>${captions.map((caption,index) => `<label class="field"><span>发布文案 ${index + 1}</span><input name="caption-${index}" maxlength="600" value="${esc(caption)}" placeholder="🔴标题#SHARPAL202H #knifesharpening …"></label>`).join('')}</fieldset>` : '';
      openModal(scriptGaps(job,data) ? '补全脚本信息' : manual ? '编辑脚本信息' : '编辑画面要求',`<p class="context-note">口播与中文属于采用稿，这里不改；只补每个意群的画面要求${manual ? '，以及开头画面、开头屏显、补拍清单和发布文案（按写稿技能的交付格式）' : ''}，供剪辑选材和发布使用。证据填类型：结果证据（须实拍）／动作演示／展示。</p>${head}${job.lines.map(line => `<fieldset class="line-edit"><legend>#${line.number} ${esc(line.spoken)}</legend><label class="field"><span>画面要证明什么</span><textarea name="proof-${line.number}" rows="2" maxlength="2000">${esc(line.proof)}</textarea></label><div class="form-row"><label class="field"><span>素材标签</span><input name="tags-${line.number}" maxlength="2000" value="${esc(line.tags)}"></label><label class="field"><span>证据</span><input name="evidence-${line.number}" maxlength="2000" value="${esc(line.evidence)}" list="job-evidence-types"></label></div></fieldset>`).join('')}<datalist id="job-evidence-types"><option value="结果证据（须实拍）"><option value="动作演示"><option value="展示"></datalist>${tail}`,'保存',async modal => {
        const values = new FormData(modal.form), value = name => String(values.get(name) || '').trim();
        await once(modal,'lines',`/api/production/jobs/${encodeURIComponent(job.id)}/lines`,async () => ({revision:data.revision,
          lines:job.lines.map(line => ({number:line.number,proof:value(`proof-${line.number}`),tags:value(`tags-${line.number}`),evidence:value(`evidence-${line.number}`)})),
          ...(manual ? {supplement:{hook_visual:value('hook_visual'),hook_text:value('hook_text'),hook_text_zh:value('hook_text_zh'),pickups:value('pickups'),captions:[0,1,2].map(index => value(`caption-${index}`)).filter(Boolean)}} : {})}));
        closeModal(true);toast(manual ? '脚本信息已补全。' : '意群表已更新。');await route(true);
      });
    };
    const startFill = async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await api(`/api/production/jobs/${encodeURIComponent(job.id)}/fill`,{method:'POST',body:{request_id:crypto.randomUUID()}});
        toast('已交给 AI 按写稿技能补全，写完自动填进制作单。');await route(true);
      } catch (error) {button.disabled = false;toast(fail(error));}
    };
    $('#job-lines-edit')?.addEventListener('click',openScriptFill);
    $('#job-ai-fill')?.addEventListener('click',startFill);
    document.querySelector('[data-script-fill]')?.addEventListener('click',startFill);
    if (fillRunning(job)) {
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden || view.modal) {setTimeout(poll,4000);return;}
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
          if (!fillRunning(fresh.job)) {await route(true);if (fresh.job.ai_fill?.status === 'completed') toast('AI 已补全脚本信息。');return;}
        } catch {}
        setTimeout(poll,4000);
      };
      setTimeout(poll,4000);
    }
    const request = readStatus('/api/production/resolve',10000).catch(error => ({ok:false,error:fail(error)}));
    bindTimeline(data,choice,seq,request);
    bindEdit(data,choice,seq,request);
    if (built) {
      const controls = foldProductionControls(job.id,'timeline',[$('#timeline-form')],'再建一条达芬奇时间线');
      if (controls) $('#timeline-panel').append(controls);
    }
    if (foldEditControls(job,choice)) {
      const controls = foldProductionControls(job.id,'edit',[$('#edit-form',$('#edit-panel'))],'再进行一次 AI 剪辑');
      if (controls) $('#edit-panel').append(controls);
    }
    if (job.voicing) {
      // Re-render only once voicing has ended and no dialog is open: route() closes dialogs and would drop a half-filled form.
      const isCurrent = () => seq === view.sequence && pageHash().split('?')[0] === `#production/${encodeURIComponent(job.id)}`;
      const poll = async () => {
        if (!isCurrent()) return;
        if (document.hidden || view.modal) {setTimeout(poll,3000);return;}
        try {
          const fresh = await api(`/api/production/jobs/${encodeURIComponent(job.id)}`);
          if (!isCurrent()) return;
          refreshProductionProgress(fresh,'video');
          if (!fresh.job.voicing && !view.modal) {await route(true);return;}
        } catch (error) {if (error.status === 404 && isCurrent() && !view.modal) {await route(true);return;}}
        setTimeout(poll,3000);
      };
      setTimeout(poll,3000);
    }
  }

  const splitLines = value => String(value || '').split('\n').map(line => line.trim()).filter(Boolean);
  function variantPickerBody(options,content) {
    return `<div class="variant-picker-layout"><aside class="variant-picker-options"><p class="variant-picker-label">选择变体</p><div class="variant-options" role="radiogroup" aria-label="要制作的变体">${options}</div></aside><section class="variant-picker-content" data-variant-content aria-label="脚本内容" tabindex="0"><p class="variant-picker-label" data-variant-label aria-live="polite"></p>${content}</section></div>`;
  }
  function revealVariantContent(modal,label) {
    $('[data-variant-label]',modal.dialog).textContent = `${label} · 脚本内容`;
    $('[data-variant-content]',modal.dialog).scrollTop = 0;
  }
  // Re-layout runs as a new AI round; the server keeps it only when every spoken word matches the original.
  function bindFormatRound(modal,task,round) {
    const button = $('[data-format-round]',modal.dialog);if (!button) return;
    button.onclick = async () => {
      if (!window.CaseAI?.formatRound) {setError(modal.dialog,'AI 协作模块尚未加载，请刷新后重试。');return;}
      button.disabled = true;setError(modal.dialog,'');
      try {await window.CaseAI.formatRound(task,round);closeModal(true);}
      catch (error) {setError(modal.dialog,fail(error));button.disabled = false;}
    };
  }
  async function adoptAndProduce(modal,{task,round,taskRevision,product,direction,title,spoken,chinese,variantIndex}) {
    const key = JSON.stringify([variantIndex,title,spoken,chinese,direction]);
    if (modal.variant !== key) {modal.done = {};modal.requests = {};modal.variant = key;}
    const adopted = await once(modal,'script','/api/scripts',async () => ({revision:(await api(`/api/creation?product=${encodeURIComponent(product)}`)).revision,script:{title,text:spoken.join('\n'),translation_zh:chinese.length === spoken.length ? chinese.join('\n') : '',product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:taskRevision,...(variantIndex === null ? {} : {variant_index:variantIndex})}}));
    let created;
    try {created = await once(modal,'job','/api/production/jobs',async () => ({revision:(await api('/api/production')).revision,job:{script_id:adopted.script.id,...(variantIndex === null ? {} : {variant_index:variantIndex})}}));}
    catch (error) {if (definitive(error)) {closeModal(true);toast(`脚本已采纳，但制作单没有创建：${fail(error)}。可在内容详情里再点「制作视频」。`,true);location.hash = url(`#content/${encodeURIComponent(adopted.script.id)}`,product);return;}throw error;}
    closeModal(true);toast(adopted.reused ? '沿用之前的采用稿，并创建制作单。' : '已保存采用稿，并创建制作单。');location.hash = jobLink(created.job);
  }
  // Checks the edited lines before anything is adopted; returns an error message or ''.
  function linesIssue(spoken,chinese) {
    if (!spoken.length) return '请至少填写一行要配音的口播。';
    if (chinese.length && chinese.length !== spoken.length) return `中文 ${chinese.length} 行，口播 ${spoken.length} 行：请逐行对应，或清空中文。`;
    return '';
  }
  function manualSplit(task,round,product,direction,reason) {
    const modal = openModal('手动指定要配音的口播',`<p class="context-note">${esc(reason)}</p><div class="production-format-choice"><p>可以让 AI 只整理排版：新开一轮，完成后逐字核对口播和中文，对不上就不采用。也可以直接把要制作的英文口播粘贴到下面，一行一个意群。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div><details class="production-candidate-source"><summary>查看本轮候选原文</summary><pre class="carousel-preview">${esc(round.output || '')}</pre></details><label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(`${direction ? direction + ' ' : ''}手动拆分`)}"></label><label class="field"><span>口播（一行一个意群）</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label><p class="context-note">画面要求在制作单里补填。</p>`,'采纳并创建制作单',async modal => {
      const values = new FormData(modal.form), spoken = splitLines(values.get('spoken')), chinese = splitLines(values.get('chinese'));
      const issue = linesIssue(spoken,chinese);if (issue) {setError(modal.dialog,issue);return;}
      await adoptAndProduce(modal,{task,round,taskRevision:task.revision,product,direction,title:values.get('title').trim(),spoken,chinese,variantIndex:null});
    });
    bindFormatRound(modal,task,round);
  }

  async function fromCandidate(task,round) {
    const product = task.product || task.context?.product || currentProduct(), direction = task.context?.direction_id || '';
    if (!product) {toast('请先选择一个具体产品。',true);return;}
    let data;
    try {data = await api(`/api/production/variants?task_id=${encodeURIComponent(task.task_id)}&round_id=${encodeURIComponent(round.round_id)}`);}
    catch (error) {
      if (error.status === 409 && /可配音的口播/.test(fail(error))) {manualSplit(task,round,product,direction,fail(error));return;}
      toast(`暂时无法拆分变体：${fail(error)}`,true);return;
    }
    const variants = data.variants, defaultTitle = variant => `${direction ? direction + ' ' : ''}${variant.label}`;
    // A round with no direction names the one the writer picked for each variant; the person confirms it here.
    const directions = direction ? [] : await api(`/api/direction-catalog?product=${encodeURIComponent(product)}`).then(result => (result.directions || []).filter(item => item.status !== 'archived')).catch(() => []);
    const suggested = variant => directions.some(item => item.direction_id === variant.suggested_direction) ? variant.suggested_direction : '';
    const directionField = directions.length ? `<label class="field"><span>关联方向</span><select name="direction_id"><option value="">暂不指定</option>${directions.map(item => `<option value="${esc(item.direction_id)}" ${item.direction_id === suggested(variants[0]) ? 'selected' : ''}>${esc(item.direction_id)} · ${esc(item.main_difference)}</option>`).join('')}</select></label>` : '';
    const loose = variants.some(variant => variant.loose);
    const options = variants.map((variant,position) => `<label class="variant-option"><input type="radio" name="variant" value="${variant.index}" ${position === 0 ? 'checked' : ''}><span><strong>${esc(variant.label)}</strong><small>${variant.lines.length} 个意群${variant.problems.length ? ` · ${variant.problems.length} 处需核对` : ''}</small></span></label>`).join('');
    const content = `<label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(defaultTitle(variants[0]))}"></label>${directionField}<div data-variant-preview></div><details class="production-line-editor" data-line-editor><summary>调整意群（拆分、合并或修正断行）</summary><p class="context-note">一行一个意群。不改动时会带入意群表；改动后画面要求需在制作单里补填。</p><label class="field"><span>口播</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label></details>`;
    const modal = openModal('选择要制作的变体',`<p class="context-note">选择变体即可查看对应脚本。口播每行是一个意群；采用后自动保留这次正文，修改时保留历史。</p>${loose ? '<div class="production-format-choice"><p>这一轮没有按「口播（配音用）」代码块交付，下面是宽松识别的结果：意群按句拆分，请逐行核对。也可以让 AI 只整理排版（新开一轮，逐字核对后才采用）。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div>' : ''}${variantPickerBody(options,content)}`,'采纳并创建制作单',async modal => {
      const values = new FormData(modal.form), chosen = variants.find(item => String(item.index) === values.get('variant'));
      const spoken = edited ? splitLines(values.get('spoken')) : chosen.lines.map(line => line.spoken);
      const chinese = edited ? splitLines(values.get('chinese')) : chosen.lines.every(line => line.zh) ? chosen.lines.map(line => line.zh) : [];
      const issue = edited ? linesIssue(spoken,chinese) : '';if (issue) {setError(modal.dialog,issue);return;}
      await adoptAndProduce(modal,{task,round,taskRevision:data.task_revision,product,direction:direction || values.get('direction_id') || '',title:values.get('title').trim(),spoken,chinese,variantIndex:chosen.index});
    });
    modal.dialog.classList.add('variant-picker-dialog');
    let edited = false;
    const fill = chosen => {
      modal.form.spoken.value = chosen.lines.map(line => line.spoken).join('\n');
      modal.form.chinese.value = chosen.lines.every(line => line.zh) ? chosen.lines.map(line => line.zh).join('\n') : '';
      edited = false;
    };
    modal.form.spoken.oninput = modal.form.chinese.oninput = () => {edited = true;};
    const preview = () => {
      const chosen = variants.find(item => String(item.index) === new FormData(modal.form).get('variant'));
      $('[data-variant-preview]',modal.dialog).innerHTML = problemsMarkup(chosen.problems) + linesTable(chosen.lines);
      fill(chosen);
      revealVariantContent(modal,chosen.label);
    };
    let titleEdited = false;
    modal.form.title.oninput = () => {titleEdited = true;};
    let directionEdited = false;
    if (modal.form.direction_id) modal.form.direction_id.onchange = () => {directionEdited = true;};
    $$('input[name="variant"]',modal.dialog).forEach(input => input.onchange = () => {
      const chosen = variants.find(item => String(item.index) === input.value);
      if (!titleEdited) modal.form.title.value = defaultTitle(chosen);
      if (modal.form.direction_id && !directionEdited) modal.form.direction_id.value = suggested(chosen);
      preview();
    });
    bindFormatRound(modal,task,round);
    preview();
  }

  // Content owns archive and restore; production still removes each order and its files safely.
  function deleteContentOrders(targets,options = {}) {
    const items = [...new Map((targets || []).filter(target => target?.id && target.kind === 'video').map(target => [target.id,{id:target.id,kind:'job'}])).values()];
    return runBatch('delete',items,options);
  }
  async function runBatch(action,targets,{onComplete} = {}) {
    if (!targets.length) return;
    const hash = location.hash, product = currentProduct();
    const labels = {archive:'归档',restore:'恢复',delete:'永久删除'};
    const execute = async () => {
      if (hash !== location.hash || product !== currentProduct()) {closeModal(true);return null;}
      let result;
      try {result = await send(`order-batch-${action}-${targets.map(target => target.id).join(',')}`,'/api/production/orders/batch',{action,items:targets});}
      catch (error) {toast(`批量${labels[action]}没有完成：${fail(error)}`,true);return null;}
      const failed = result.results.filter(entry => !entry.ok);
      const warned = result.results.flatMap(entry => entry.warnings || []);
      const parts = [`已${labels[action]} ${result.changed} 张`];
      if (result.skipped) parts.push(`${result.skipped} 张无需处理`);
      if (failed.length) parts.push(`${failed.length} 张未完成：${failed[0].error}${failed.length > 1 ? ' 等' : ''}`);
      else if (warned.length) parts.push(warned[0]);
      toast(parts.join('，'),failed.length > 0 || warned.length > 0);
      return result;
    };
    const complete = async result => {
      if (!result || hash !== location.hash || product !== currentProduct()) return;
      if (onComplete) await onComplete(result);else await window.ContentFlow?.route(true);
    };
    if (action !== 'delete') {await complete(await execute());return;}
    return openModal('批量永久删除制作单',`<p class="context-note">将永久删除所选 ${targets.length} 张制作单（不需要先归档），以及它们在本地保存的配音、发布登记及托管成片等全部文件。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程。</li><li>采用稿、修改历史和案例库保留，可以重新创建制作单。</li><li>仍有任务在运行的制作单会被跳过并说明原因。</li></ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await execute();
      if (result) {closeModal(true);await complete(result);}
    });
  }
  // Permanent removal of an archived order with its local files; DaVinci timelines and script versions stay.
  async function deleteOrder(item) {
    const base = `/api/production/jobs/${encodeURIComponent(item.id)}`;
    let preview;
    try {preview = await api(`${base}/delete`,{method:'POST',body:{preview:true}});}
    catch (error) {toast(fail(error),true);return;}
    const bytes = preview.bytes || 0;
    const size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
    const parts = [`${preview.takes} 条配音`,`${preview.edits} 次 AI 剪辑记录`];
    if (preview.publications) parts.push(`${preview.publications} 次发布登记及托管成片`);
    openModal('永久删除制作单',`<p class="context-note">将永久删除「${esc(preview.title || item.title || item.id)}」：${esc(parts.join('、'))}，以及它们在本地保存的全部文件（${preview.files} 个，共约 ${esc(size)}）。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程${preview.timelines ? `（已建的 ${preview.timelines} 条时间线需要的话请自行在达芬奇里处理）` : ''}。</li><li>采用稿、修改历史和案例库保留，可以重新创建制作单。</li>${preview.published ? '<li class="ai-error">这张制作单已登记发布；发布登记和托管成品会一并删除，脚本里的发布状态不会自动改回。</li>' : ''}</ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await send(`order-delete-${item.id}`,`${base}/delete`,{});
      closeModal(true);
      const warnings = result.warnings || [];
      toast(warnings.length ? warnings[0] : '制作单及本地文件已永久删除。',warnings.length > 0);
      const back = $('.back-link');
      if (back) back.click();else location.hash = url('#content',item.product);
    });
  }
  async function fromScript(script) {
    const scriptId = script.script_id || script.id;
    let existing = [];
    try {existing = ((await api(`/api/production?product=${encodeURIComponent(script.product)}`)).jobs || []).filter(job => job.script_id === scriptId);} catch {}
    const lines = String(script.text || '').split('\n').map(line => line.trim()).filter(Boolean);
    openModal('为这条脚本创建制作单',`<p class="context-note">口播按行配音，每行是一个意群。来源候选里有逐行一致的意群表时会一起带入；没有的话，画面要求在制作单里补填。</p>${existing.length ? `<p class="context-note">这条脚本已有 ${existing.length} 张制作单：${existing.map(job => `<a href="${esc(jobLink(job))}">${esc(productionTitle(job))}（${esc(statusLabels[job.status] || job.status)}）</a>`).join('、')}。再建一张用于换音色或另剪一版。</p>` : ''}<div class="production-table-wrap"><table class="production-table"><thead><tr><th>#</th><th>将要配音的口播</th></tr></thead><tbody>${lines.map((line,index) => `<tr><td>${index + 1}</td><td class="spoken">${esc(line)}</td></tr>`).join('')}</tbody></table></div>`,'创建制作单',async modal => {
      const created = await once(modal,'job','/api/production/jobs',async () => ({revision:(await api('/api/production')).revision,job:{script_id:scriptId}}));
      closeModal(true);toast('制作单已创建。');location.hash = jobLink(created.job);
    });
  }

  window.addEventListener('focus',() => {if (!document.hidden) void view.refreshEditDefaults?.();});
  document.addEventListener('visibilitychange',() => {if (!document.hidden) void view.refreshEditDefaults?.();});
  window.addEventListener('hashchange',() => {if (!pageHash().startsWith('#production/')) view.activityTarget = null;if (view.modal?.detachable && view.modal.hash !== location.hash) closeModal();});
  return {route,openSettings,deleteContentOrders,hasUnsavedInput,beforeNavigate,fromCandidate,fromScript,modalOpen,captureListState};
})();
