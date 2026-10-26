'use strict';

// Production jobs: pick a voiced variant, keep a voice library and generate timed voiceovers.
window.CaseProduction = (() => {
  const statusLabels = {draft:'待配音',voiced:'已配音',timeline:'已建时间线',editing:'剪辑中',awaiting_review:'待人工审查',approved:'已完成',needs_material:'待补素材',needs_verification:'素材待核实',needs_repair:'待修正',verification_failed:'核验未完成',unverified:'旧记录待核验',completed:'旧记录待核验',failed:'剪辑失败',cancelled:'剪辑已停止',interrupted:'剪辑已中断'};
  const frameRates = ['23.976','24','25','29.97','30','50','59.94','60'];
  const editModes = {explain:'解释画面逻辑（默认）',sync:'音画同步',montage:'主题蒙太奇'};
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
  const completionTimeline = job => [...activeTimelines(job)].reverse().find(item => job.selected_take && item.take_id === job.selected_take);
  const productionTitle = job => job.display_title || job.title || job.variant_label || '未命名制作单';
  // Keep only the exact variant fragments that the heading has not already named.
  const variantSubtitle = (title,label) => {
    const named = String(title || '').split(/[｜|·]/u).map(part => part.trim());
    return String(label || '').split(' · ').filter(part => part && !named.some(name =>
      name === part || /^变体\s*#?\d+$/.test(part) && name.endsWith(` ${part}`))).join(' · ');
  };
  const currentEditResult = (job,choice) => [...(job.edits || [])].reverse().find(edit => currentEdit(job,edit) && edit.status !== 'running' && (!choice.edit_timeline || edit.timeline_uid === choice.edit_timeline));
  const foldEditControls = (job,choice) => Boolean((currentEditResult(job,choice) ||
    (manualCompletion(job) && (!choice.edit_timeline || manualCompletion(job).timeline_uid === choice.edit_timeline))) &&
    !(job.edits || []).some(edit => edit.status === 'running') && !choice.edit_draft && !pendingTimelineId(choice.edit_timeline));
  const manualCompletion = job => {
    const receipt = job.manual_completion, timeline = completionTimeline(job);
    return job.status === 'approved' && receipt?.at && timeline && receipt.take_id === job.selected_take && receipt.timeline_uid === timeline.unique_id ? receipt : null;
  };
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
  const activitySteps = {validation:'edit',stills:'pick',image_review:'images',package:'delivery'};
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
    for (const key of ['line','slide','gap','question','start','end']) if (/^\d+$/.test(params.get(key) || '')) fields[key] = Number(params.get(key));
    view.pendingActivityTarget = {hash,run,step,...fields};
  }
  function focusActivityTarget() {
    const target = view.pendingActivityTarget;if (!target) return;
    view.pendingActivityTarget = null;view.activityTarget = target.hash;
    if (!target.run && !target.step && target.slide === undefined && target.line === undefined) return;
    const card = $$('[data-activity-source]',$('#content')).find(node => node.dataset.activitySource === target.run) || $$('[data-output-id]',$('#content')).find(node => node.dataset.outputId === target.run) || $$('[data-generation-result]',$('#content')).find(node => node.dataset.generationResult === target.run);
    const panel = card?.closest('[data-step-panel]');
    if (panel?.hidden) $$('[data-step]',$('.production-steps')).find(button => button.dataset.step === panel.dataset.stepPanel)?.click();
    if (card?.closest('.image-version-others')?.hidden) setImageVersionsExpanded(card.closest('.image-version-stack'),true);
    let specific = null, expected = target.gap !== undefined || target.question !== undefined || target.slide !== undefined || target.line !== undefined;
    if (target.gap !== undefined && card) specific = $$('[data-edit-gap]',card).find(node => Number(node.dataset.editGap) === target.gap && (target.start === undefined || Number(node.dataset.gapStart) === target.start) && (target.end === undefined || Number(node.dataset.gapEnd) === target.end));
    else if (target.question !== undefined && card) specific = $$('[data-edit-question]',card).find(node => Number(node.dataset.editQuestion) === target.question);
    else if (target.slide !== undefined) specific = $(`[data-${target.step === 'pick' ? 'pick' : 'image'}-slide="${target.slide}"]`);
    else if (target.line !== undefined) specific = $(`#job-script-panel [data-production-line="${target.line}"]`);
    if (expected && !specific) {
      let note = $('#production-target-note');if (!note) {note = document.createElement('p');note.id = 'production-target-note';note.className = 'context-note production-target-note';($('#workflow-links') || $('.page-heading')).after(note);}
      note.textContent = '对应的缺口或内容已变化，请重新核对当前待处理清单。';
    }
    const node = specific || card || (target.run ? $('#production-activity-logs') : null) || $$('[data-step-panel]',$('#content')).find(node => !node.hidden);
    if (specific) specific.classList.add('production-target');
    for (let ancestor = node?.parentElement;ancestor && ancestor !== $('#content');ancestor = ancestor.parentElement) if (ancestor.matches('details')) ancestor.open = true;
    if (node) {if (!card && target.run) {const details = $('details',node);if (details) details.open = true;}node.tabIndex = -1;node.focus({preventScroll:true});node.scrollIntoView({block:'start'});}
  }
  const fail = error => error?.message || '操作暂未完成';
  function mountProductionNeeds(data,kind) {
    const record = kind === 'carousel' ? data.carousel : data.job;if (!record) return;
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
  const seconds = value => value === null || value === undefined ? '—' : `${Number(value).toFixed(2)} 秒`;
  const voiceLabel = voice => [voice.note,voice.name].filter(Boolean).join(' · ') || voice.voice_id;
  const audioURL = (job,take,kind,download = false) => `/api/production/audio/${encodeURIComponent(job)}/${encodeURIComponent(take)}/${kind}${download ? '?download=1' : ''}`;
  // Material pickers only offer finished-material timelines; edits and stills must not pull from output timelines.
  const finishedMark = '成品素材';
  const finishedOnly = names => names.filter(name => name.includes(finishedMark));
  function materialPicker(box,status,exclude,chosen,saved) {
    const names = status.ok ? finishedOnly((status.timelines || []).filter(name => !exclude.has(name))) : [];
    if (names.length) {
      const remembered = finishedOnly(chosen || saved || []).filter(name => names.includes(name));
      const checked = chosen ? remembered : remembered.length ? remembered : names;
      box.innerHTML = `<div class="material-options">${names.map(name => `<label><input type="checkbox" name="material" value="${esc(name)}" ${checked.includes(name) ? 'checked' : ''}>${esc(name)}</label>`).join('')}</div>`;
    } else box.innerHTML = `<p class="context-note">${status.ok ? `达芬奇里没有名称带「${finishedMark}」的时间线，可以直接填写（逗号分隔）。` : `暂时读不到达芬奇里的时间线列表，可以直接填写名称带「${finishedMark}」的素材时间线（逗号分隔）。`}</p><input name="material_text" value="${esc(finishedOnly(chosen || saved || []).join('，'))}">`;
  }
  const pickedMaterials = form => $$('input[name="material"]:checked',form).map(input => input.value).concat((form.material_text?.value || '').split(/[,，]/).map(name => name.trim()).filter(Boolean));
  const materialProblem = names => !names.length ? '请至少选一条素材时间线。' : names.some(name => !name.includes(finishedMark)) ? `素材时间线只能选名称带「${finishedMark}」的时间线。` : '';
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
      return `<div class="production-recovery-row"><div><p>${esc(operation.message || '制作操作尚未完整登记，请先核对原结果。')}</p><small>${operation.kind === 'take' ? '配音' : '达芬奇时间线'}${operation.updated_at ? ` · ${esc(readableDate(operation.updated_at))}` : ''}</small></div><button type="button" class="secondary" data-recover-operation="${esc(operation.request_id)}" data-recover-kind="${esc(operation.kind)}" ${busy ? 'disabled' : ''}>${label}</button></div>${unresolvedVoice ? `<div class="production-recovery-confirm"><label><input type="checkbox" data-confirm-no-voice="${esc(operation.request_id)}" ${busy ? 'disabled' : ''}>我已在 ElevenLabs 核实本次没有生成音频</label><p>仅在已核实没有生成音频时勾选。重试将重新请求配音并消耗 ElevenLabs 额度。</p><button type="button" class="secondary" data-retry-uncertain-voice="${esc(operation.request_id)}" disabled>确认未生成，重试本次配音</button></div>` : ''}`;
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
    if (!tail) {location.hash = url('#content',currentProduct());return;}
    if (refreshStatus) statusReads.forEach(entry => {entry.expires = 0;});
    if (view.modal && !view.modal.busy) closeModal(true);
    $('#breadcrumb').innerHTML = `内容创作 <span>/</span> 制作单`;
    if (!keepScroll) $('#content').innerHTML = skeletonMarkup(2);
    try {
      if (tail) applyActivityTarget(decodeURIComponent(tail),hash);
      if (tail.startsWith('CAR-')) {
        const data = await api(`/api/production/carousels/${encodeURIComponent(decodeURIComponent(tail))}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderCarousel(data);
        mountActivityLogs(data.carousel.id);focusActivityTarget();
      } else if (tail) {
        const data = await api(`/api/production/jobs/${encodeURIComponent(decodeURIComponent(tail))}`);
        if (seq !== view.sequence || hash !== pageHash()) return;
        renderJob(data,seq);
        mountActivityLogs(data.job.id);focusActivityTarget();
      }
    } catch (error) {
      if (seq !== view.sequence || hash !== pageHash()) return;
      if (tail && error.status === 404) {$('#content').innerHTML = `<div class="empty"><h3>这张制作单已不存在</h3><p>可能已被删除，或属于其他产品。</p><a class="secondary" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content'))}">返回内容列表</a></div>`;return;}
      $('#content').innerHTML = `<div class="empty"><h3>暂时无法读取制作资料</h3><p>${esc(fail(error))}</p><button class="secondary" id="production-retry">重新读取</button></div>`;
      $('#production-retry').onclick = () => route(true);
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
    if (kind === 'carousel') {
      const item = data.carousel, count = item.slides.length, based = item.slides.filter(slide => slide.base).length;
      const confirmed = item.slides.filter(confirmedOutput).length, publication = carouselPublication(data);
      const delivery = view.progress?.id === item.id ? view.progress.delivery : null;
      const published = delivery?.published === true && delivery?.delivery_available === true;
      const checking = !delivery && view.progress?.id === item.id && view.progress.loading;
      const collected = delivery?.delivery_available === true && carouselDeliveries(data).some(entry => entry.id === delivery.current_delivery_id && entry.collected_at);
      return [
        {key:'slides',label:'逐张表',note:`${count} 张`,done:count > 0,panels:[$('#carousel-adopted-script'),$('#carousel-slides-edit')?.closest('section')].filter(Boolean)},
        {key:'pick',label:'找图与选底图',note:item.clip_picking_live ? '正在挑片段' : (item.searches || []).some(run => run.status === 'running') ? '正在找候选' : `${based}/${count} 已选`,done:count > 0 && based === count,panels:[$('#pick-panel'),$('#still-panel')].filter(Boolean)},
        {key:'images',label:'出图',note:generating(item) ? '出图中' : `${confirmed}/${count} 已确认`,done:count > 0 && confirmed === count,panels:[$('#carousel-images-panel'),$('#carousel-group-panel')].filter(Boolean)},
        {key:'delivery',label:'领取与发布',note:published ? '已登记发布' : checking ? '核对中' : publication ? '历史版本已发布' : collected ? '已领取 · 待登记' : carouselDeliveries(data).length ? '最新成品包可领取' : '',done:published,panels:[$('#carousel-delivery-panel'),$('#carousel-caption-copy')?.closest('section')].filter(Boolean)}
      ];
    }
    const job = data.job, selected = (job.takes || []).find(take => take.id === job.selected_take);
    const publication = view.progress?.id === job.id ? view.progress.publication : null, published = publication?.source_current === true && (!publication.file || publication.file_available === true);
    return [
      {key:'lines',label:'脚本',note:job.variant_label || `${job.lines.length} 个意群`,done:job.lines.length > 0,panels:[$('#job-script-panel')].filter(Boolean)},
      {key:'voice',label:'配音',note:job.voicing ? '生成中' : selected ? '已选用' : `${job.takes.length} 版`,done:!!selected,panels:[$('#take-panel'),$('#voice-panel')].filter(Boolean)},
      {key:'timeline',label:'达芬奇时间线',note:job.timeline_building ? (job.timeline_building.stage === 'waiting' ? '排队中' : '建线中') : `${activeTimelines(job).length} 条`,done:activeTimelines(job).some(item => selected && item.take_id === selected.id),panels:[$('#timeline-panel')].filter(Boolean)},
      {key:'edit',label:'AI 剪辑与人工审查',note:(job.edits || []).some(edit => edit.status === 'running' && edit.waiting_timeline) ? '待建线后自动剪辑' : (job.edits || []).some(edit => edit.status === 'running') ? '剪辑中' : manualCompletion(job) ? '人工完成' : job.status === 'approved' ? '已完成' : `${(job.edits || []).length} 次`,done:job.status === 'approved',panels:[$('#edit-panel')].filter(Boolean)},
      {key:'publish',label:'成片与发布',note:published ? '已登记发布' : publication ? '登记版本需核对' : '登记视频 ID',done:published,panels:[$('#prepublish-review-panel'),$('#video-publication-panel')].filter(Boolean)}
    ];
  }
  function refreshProductionProgress(data,kind) {
    const context = view.progress, item = kind === 'carousel' ? data.carousel : data.job;
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
    const record = kind === 'carousel' ? {...item,deliveries:carouselDeliveries(data)} : context.publication === undefined ? item : {...item,current_publication:context.publication};
    const state = queueState(record,kind,context.delivery,{loading:context.loading,error:context.error});
    let action = state.action, step = state.step, slide;
    if (!item.archived && kind === 'carousel' && !carouselAutopilotRunning(item) && !generating(item) && !item.clip_picking_live && !(item.searches || []).some(run => run.status === 'running')) {
      const pending = item.slides.filter(entry => !confirmedOutput(entry) && (entry.outputs || []).some(output => !output.stale));
      const missing = item.slides.filter(entry => !entry.base && !confirmedOutput(entry));
      const unmade = item.slides.filter(entry => entry.base && !confirmedOutput(entry) && !(entry.outputs || []).some(output => !output.stale));
      const target = pending.length ? pending : missing.length ? missing : unmade;
      if (target.length) {
        action = `${pending.length ? '确认' : missing.length ? '选' : '生成'}第 ${target.map(entry => entry.number).join('、')} 张${missing.length && !pending.length ? '底图' : '成图'}`;
        step = pending.length || !missing.length ? 'images' : 'pick';slide = target[0].number;
      }
    } else if (!item.archived && kind === 'video' && !item.voicing) {
      const selected = (item.takes || []).find(take => take.id === item.selected_take);
      if (state.key === 'approved') {step = 'publish';action = context.publication ? context.publication.source_current === true && (!context.publication.file || context.publication.file_available === true) ? '查看发布登记' : '核对发布登记与当前版本' : '登记发布视频 ID';}
      else if (!selected && !(item.edits || []).length) {step = 'voice';action = item.takes?.length ? '试听并选用配音' : '生成配音';}
      else if (item.timeline_building?.group_markers === true) {step = 'edit';action = (item.edits || []).some(edit => edit.status === 'running' && edit.timeline_request_id === item.timeline_building.request_id) ? '查看自动接续剪辑' : '先提交剪辑';}
      else if (selected && !activeTimelines(item).some(timeline => timeline.take_id === selected.id)) {step = 'timeline';action = '建立当前配音的时间线';}
    }
    context.step = step;context.slide = slide;
    $('[data-next-state]',context.host).textContent = state.label;
    const headerState = $('.page-heading .script-status');if (headerState) headerState.textContent = state.label;
    const button = $('[data-next-action]',context.host);button.textContent = `下一步：${action} →`;button.hidden = !step;
    const note = $('[data-next-note]',context.host);note.textContent = context.error || state.note || '';note.hidden = !note.textContent;
    $('[data-next-retry]',context.host).hidden = !context.error;
    paintRuntimeDisplays(data,kind);
    mountProductionNeeds(data,kind);
  }
  function mountProductionProgress(data,kind) {
    const item = kind === 'carousel' ? data.carousel : data.job, host = document.createElement('div');
    host.className = 'production-next';host.setAttribute('aria-label','制作单下一步');
    host.innerHTML = '<span class="script-status" data-next-state></span><button type="button" class="subtle" data-next-action></button><small data-next-note></small><button type="button" class="subtle" data-next-retry hidden>重新核对状态</button>';
    $('.page-heading').after(host);
    const context = view.progress = {id:item.id,kind,data,host,loading:true,error:'',delivery:null};
    $('[data-next-action]',host).onclick = () => {
      const button = $$('[data-step]',$('.production-steps')).find(button => button.dataset.step === context.step);
      button?.click();
      if (context.slide) {
        const card = $(`[data-${context.step === 'pick' ? 'pick' : 'image'}-slide="${context.slide}"]`);
        card?.scrollIntoView({block:'center',behavior:'smooth'});
      }
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
    const stageNames = {materials:'素材清单',grid:'缩略网格',choice:'AI 选镜',placement:'放置镜头',saved:'保存交付',preparing:'准备素材',planning:'规划镜头',editing:'正在剪辑',voice_prepare:'准备配音',request:'请求配音',alignment:'对齐意群',saving:'保存版本',clip_materials:'素材片段清单',clip_choice:'AI 挑候选片段'};
    const live = run.live || {}, stage = live.stage || live.phase;
    const grid = stage === 'grid' && Number.isInteger(live.grid?.total) ? ` · ${Number(live.grid.exported || 0)} / ${live.grid.total} 个片段已导出` : '';
    const fallback = {image:'正在出图',stills:'正在找底图',clips:'正在挑候选片段',voice:'正在配音',edit:'正在剪辑'}[kind] || '正在处理';
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
    if (kind === 'carousel') return [...(item.autopilot_runs || []).filter(run => run.status === 'running').map(run => ({run:{...run,live:{progress:run.message || run.phase || 'AI 正在全程制作'}},kind:'autopilot'})),...(item.clip_picking_live ? [{run:{id:'clip-pick',status:'running',started_at:item.clip_picking_live.started_at,live:item.clip_picking_live},kind:'stills'}] : []),...(item.searches || []).filter(run => run.status === 'running').map(run => ({run,kind:'stills'})),...(item.generations || []).filter(run => run.status === 'running').map(run => ({run,kind:'image'}))];
    const voice = item.voicing_live || {};
    return [...(item.voicing ? [{run:{id:'voice',status:'running',started_at:voice.started_at,live:voice},kind:'voice'}] : []),...(item.edits || []).filter(run => run.status === 'running').map(run => ({run,kind:'edit'}))];
  }
  function paintRuntimeDisplays(data,kind) {
    const item = kind === 'carousel' ? data.carousel : data.job;
    activeProductionRuns(item,kind).forEach(({run,kind:runKind}) => {
      const card = run.id === 'voice' ? $('#voice-panel') : run.id === 'clip-pick' ? $('#still-panel') : $$('[data-activity-source],[data-generation-id]',$('#content')).find(node => node.dataset.activitySource === run.id || node.dataset.generationId === run.id);
      if (!card) return;
      const old = $('.production-runtime',card), markup = runProgressMarkup(run,runKind);
      if (old) old.outerHTML = markup;else ($('.take-head',card) || $('h2',card))?.insertAdjacentHTML('afterend',markup);
    });
  }
  function resultState(root) {
    const owner = node => node.closest('[data-edit-id],[data-take-id],[data-image-slide],[data-pick-slide]')?.getAttribute('data-edit-id') || node.closest('[data-take-id]')?.dataset.takeId || node.closest('[data-image-slide]')?.dataset.imageSlide || node.closest('[data-pick-slide]')?.dataset.pickSlide || '';
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
    const item = kind === 'carousel' ? data.carousel : data.job;
    const set = (key,complete,nodes,label,end) => {
      const existing = $(`[data-production-controls="${key}"]`,$('#content'));
      if (!complete && existing) {for (const node of [...existing.children].filter(node => node.tagName !== 'SUMMARY')) existing.before(node);existing.remove();}
      else if (complete && !existing) {const details = foldProductionControls(item.id,key,nodes(),label);if (details && end) end.append(details);}
    };
    if (kind === 'carousel') {
      const count = item.slides.length, panel = $('#still-panel');
      $('#carousel-image-slides')?.classList.toggle('is-complete',carouselQuiet(item));
      set('pick',count > 0 && item.slides.every(slide => slide.base) && !item.clip_picking_live && !(item.searches || []).some(run => run.status === 'running'),() => [...panel.children].filter(node => !node.matches('h2,#clip-ai-summary,.still-history,#search-runs')),'再让 AI 按标签挑一次候选片段');
      set('images',carouselQuiet(item),() => [$('#carousel-image-form'),$('#carousel-bulk-actions')],'再制作一版成图',$('#carousel-images-panel'));
    } else {
      const selected = item.takes.some(take => take.id === item.selected_take);
      if (selected) $('#voice-panel').before($('#take-panel'));
      set('voice',selected && !item.voicing,() => [$('#take-form')],'再生成一版配音');
      set('timeline',activeTimelines(item).some(timeline => selected && timeline.take_id === item.selected_take),() => [$('#timeline-form')],'再建一条达芬奇时间线',$('#timeline-panel'));
      set('edit',foldEditControls(item,view.choice[item.id] || {}),() => [$('#edit-form',$('#edit-panel'))],'再进行一次 AI 剪辑',$('#edit-panel'));
    }
  }
  async function refreshProductionDetail(data,kind,{scope = '',fresh = null,checkDelivery = true,freshEpoch = null} = {}) {
    const context = view.progress, item = kind === 'carousel' ? data.carousel : data.job;
    if (!context || context.id !== item.id || context.kind !== kind) return false;
    if (fresh && freshEpoch !== null && (context.localRefresh || 0) !== freshEpoch) return false;
    const sequence = view.sequence, request = (context.localRefresh || 0) + 1;context.localRefresh = request;
    fresh ||= await api(`/api/production/${kind === 'carousel' ? 'carousels' : 'jobs'}/${encodeURIComponent(item.id)}`);
    if (sequence !== view.sequence || view.progress !== context || request !== context.localRefresh || !context.host.isConnected) return false;
    const savedScroll = window.scrollY, key = kind === 'carousel' ? 'carousel' : 'job';
    const publicationSource = job => JSON.stringify([job.status,job.selected_take,completionTimeline(job)?.unique_id,job.manual_completion,(job.edits || []).map(edit => [edit.id,edit.status,edit.approval])]);
    const oldPublicationSource = kind === 'video' ? publicationSource(item) : '';
    Object.assign(item,fresh[key]);Object.assign(data,fresh);data[key] = item;context.data = data;
    if (kind === 'carousel') {
      view.clipCounts = data.clip_picks?.counts || null;
      const summary = $('#clip-ai-summary',sectionContents(stillSection(data)));
      if (summary) {const old = $('#clip-ai-summary');if (old) old.replaceWith(summary);else $('#still-panel')?.append(summary);}
      const picked = sectionContents(pickSection(item));
      replaceResultMarkup($('.pick-slides',$('#pick-panel')),$('.pick-slides',picked).innerHTML);
      replaceResultMarkup($('#carousel-image-slides'),imageSlides(item));
      replaceResultMarkup($('#carousel-image-runs'),renderImageRuns(item,$('.image-run-history')?.open));
      const delivery = sectionContents(deliverySection(data));replaceResultMarkup($('#carousel-delivery-panel'),delivery.innerHTML);
      const autopilot = sectionContents(carouselAutopilotSection(data));replaceResultMarkup($('#carousel-autopilot-panel'),autopilot.innerHTML);bindCarouselAutopilot(data);
      replaceResultMarkup($('#carousel-plan-block'),sectionContents(planBlock(item)).innerHTML);
      const group = sectionContents(groupSection(data));replaceResultMarkup($('#carousel-group-panel'),group.innerHTML);
      bindPickCards(data);bindImages(data,{partial:true});bindDelivery(data);bindGroup(data);refreshCarouselOverview(data);
    } else {
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
      if (oldPublicationSource !== publicationSource(item)) window.VideoPublication?.mount($('#video-publication-panel'),{job:item,api,onLoaded:publication => {if (view.progress === context) {context.publication = publication;refreshProductionProgress(data,'video');}},onSaved:() => refreshProductionDetail(data,'video',{scope:'edit'})});
    }
    const history = $('.production-history',$('#content'));
    if (history) {$('summary',history).textContent = `制作记录（${item.history.length} 条）`;$('ol',history).innerHTML = [...item.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('');}
    syncControls(data,kind);refreshProductionProgress(data,kind);mountActivityLogs(item.id);mountRuntimeTicker();
    const panels = productionSteps(data,kind), active = $('.production-steps [aria-selected="true"]')?.dataset.step;
    panels.forEach(step => step.panels.forEach(panel => {panel.dataset.stepPanel = step.key;panel.setAttribute('role','tabpanel');panel.hidden = step.key !== active;}));
    window.scrollTo({top:savedScroll});
    if (checkDelivery && context.loadDelivery) await context.loadDelivery();
    return true;
  }

  function queueState(item,kind,delivery,deliveryState) {
    if (item.archived) return {key:'archived',label:'已归档',action:'查看制作单',note:'恢复后可继续制作'};
    if (kind === 'video') {
      const publication = item.current_publication;
      // Same test as the publish step: a registration whose bound file is gone still needs checking.
      if (publication?.source_current && publication.post_id && publication.published_on && (!publication.file || publication.file_available !== false)) return {key:'published',label:'已登记发布',action:'查看发布记录',step:'publish',note:'当前审查版本已登记发布',delivery:true};
      if (item.voicing) return {key:'in_progress',label:'配音中',action:'查看进度',step:'voice'};
      if ((item.edits || []).some(edit => edit.status === 'running' && edit.waiting_timeline)) return {key:'in_progress',label:'剪辑排队中',action:'查看自动接续剪辑',step:'edit',note:'建好本次时间线后自动开始剪辑'};
      if (item.timeline_building) return {key:'in_progress',label:item.timeline_building.stage === 'waiting' ? '建线排队中' : '建线中',action:item.timeline_building.group_markers === true ? '先提交剪辑' : '查看进度',step:item.timeline_building.group_markers === true ? 'edit' : 'timeline'};
      if (item.recovery_operations?.length) return {key:'needs_attention',label:'结果待恢复',action:'查看并恢复'};
      if (manualCompletion(item)) return {key:'approved',label:'已人工完成',action:'查看制作单',step:'edit',note:'第 4 步已手动标记成功',delivery:true};
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
    if (carouselAutopilotRunning(item)) return {key:'in_progress',label:'AI 全程制作中',action:'查看进度',step:'images',note:carouselAutopilotRun(item).message || carouselAutopilotRun(item).phase || 'AI 正在生成同视角空背景，随后恢复原图主体并验证像素'};
    if (item.clip_picking_live || (item.generations || []).some(run => run.status === 'running') || (item.searches || []).some(run => run.status === 'running')) return {key:'in_progress',label:'制作中',action:'查看进度',step:(item.generations || []).some(run => run.status === 'running') ? 'images' : 'pick'};
    if (delivery?.ready_to_package === true) {
      if (delivery.delivery_available === true && delivery.current_delivery_id && delivery.delivery_id === delivery.current_delivery_id) {
        const collected = !delivery.published && (item.deliveries || []).some(entry => entry.id === delivery.current_delivery_id && entry.collected_at);
        if (collected) return {key:'ready',label:'已领取 · 待登记发布',action:'登记发布',step:'delivery',note:'成品包已领取，发帖后登记帖子 ID',delivery:true};
        return {key:delivery.published ? 'published' : 'ready',label:delivery.published ? '已登记发布' : '可领取',action:delivery.published ? '查看发布记录' : '领取成品',step:'delivery',note:delivery.published ? '当前成品包已登记发布' : carouselSubjectsPreserved(item) ? '图片已生成并打包 · 主体像素已保留' : carouselAutopilotRun(item)?.workflow === 'subject_locked' ? '图片已生成 · 主体像素校验结果见制作单' : carouselAutopilotRun(item)?.review_deferred ? '图片已生成并打包 · 产品一致性核对暂缓' : '当前图文已确认 · 成品包完整可用',delivery:true};
      }
      return {key:'ready_to_package',label:'待整理成品包',action:'整理成品包',step:'delivery',note:delivery.delivery_error || '当前图文已确认，整理后可领取',delivery:true};
    }
    if (item.status === 'approved' || item.status === 'published') return {key:'needs_attention',label:deliveryState.loading ? '领取状态核对中' : '领取状态待确认',action:'查看制作单',step:'delivery',note:deliveryState.error || '请核对当前图片与成品包'};
    if (item.status === 'reviewing') {
      const tier = carouselReviewTier(item), history = delivery?.delivery_id ? '；已整理包待更新' : '';
      if (tier.tier === 'attention') return {key:'awaiting_review',label:`${tier.attention.length} 张需人看`,action:'去看',step:'images',note:`第 ${tier.attention.join('、')} 张的自动核对有提示，需要你查看${history}`};
      if (tier.tier === 'clean') return {key:'awaiting_review',label:'核对全通过',action:'整组确认',step:'images',note:`自动核对全部通过，扫一眼整组后一键确认${history}`};
      return {key:'awaiting_review',label:'成图待确认',action:'选择图片',step:'images',note:delivery?.delivery_id ? '已整理包待更新；当前图片待选用' : ''};
    }
    return {key:'in_progress',label:carouselLabel(item),action:'继续制作',step:['ready','picked'].includes(item.status) ? 'images' : 'pick',note:delivery?.delivery_id ? '已整理的成品包可在制作单领取' : ''};
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
    if (editState(edit) !== 'awaiting_review' || !currentEdit(job,edit) || manualCompletion(job) || edit.result_pending) return '';
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
      extra:[editModes[edit.mode] || edit.mode,edit.validation?.checked_at ? `核验 ${readableDate(edit.validation.checked_at)}` : '',edit.execution_status ? `剪辑执行：${editLabels[edit.execution_status] || edit.execution_status}` : ''].filter(Boolean)});
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
    const actions = edit.status === 'running' ? `<button type="button" class="secondary" data-cancel-edit="${esc(edit.id)}">停止</button>`
      : `<div class="edit-actions">${missingBaseline ? `<button type="button" class="secondary" data-adopt-baseline="${esc(edit.id)}" ${busy || !historicalRestore ? 'disabled' : ''}>补齐核验依据</button>` : ''}${resultRecovery ? `<button type="button" class="secondary" data-recover-edit="${esc(edit.id)}" ${busy ? 'disabled' : ''}>恢复结果</button>` : `<button type="button" class="secondary" data-recheck-edit="${esc(edit.id)}" title="${esc(skillDelivery(edit) ? '读回并保存现有工程，更新画面覆盖和原报告空段；内容仍需人工确认' : '读回并保存现有工程后重新核验；请先结束正在进行的剪辑')}" ${busy ? 'disabled' : ''}>重新核验</button>`}${approval ? `<button type="button" class="secondary" data-revoke-edit="${esc(edit.id)}" ${busy || job.archived ? 'disabled' : ''}>撤销完成</button>` : ''}${job.archived ? '' : `<button type="button" class="subtle edit-delete" data-delete-edit="${esc(edit.id)}" ${busy && edit.status === 'running' ? 'disabled' : ''}>${icon('trash')}删除</button>`}</div>`;
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
      : `<form id="edit-form"><div class="form-row"><label class="field"><span>目标时间线</span><select name="timeline">${editTargetOptions(job,choice)}</select></label><label class="field"><span>选画面模式</span><select name="mode">${Object.entries(editModes).map(([key,label]) => `<option value="${key}" ${key === (choice.edit_mode || 'explain') ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div><p class="context-note" data-pending-edit-note ${pendingTimelineId(choice.edit_timeline) ? '' : 'hidden'}>可以先提交剪辑；本次时间线建好后自动接着剪，关闭页面也会继续。</p><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="3" maxlength="4000" placeholder="例如：开头用切纸结果；只剪前 4 个意群">${esc(choice.edit_instruction || '')}</textarea></label><p class="context-note"><span data-edit-default role="status">正在读取全局默认模型…</span> <a href="${esc(url('#settings'))}" data-edit-settings>修改全局设置</a> · <button type="button" class="subtle" data-edit-default-refresh>重新读取</button></p><div data-edit-override hidden><p class="context-note">当前全局默认暂不能用于 AI 剪辑，请为本次选择兼容模型，或修改全局设置。</p><div class="form-row"><label class="field"><span>本次剪辑模型</span><select name="model"><option value="">请选择兼容模型</option></select></label><label class="field"><span>本次思考强度</span><select name="effort"><option value="">客户端默认</option></select></label></div></div><p class="production-resolve" data-edit-provider-status role="status">正在检查剪辑客户端…</p><div class="take-actions"><button type="submit" class="primary" disabled>开始 AI 剪辑</button><button type="button" class="secondary" data-copy-handoff>复制剪辑指令</button><button type="button" class="secondary" data-open-target-timeline>在达芬奇打开时间线</button>${infoTip('<p data-edit-usage>AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude。</p>','AI 剪辑说明')}${running ? '<span>已有剪辑任务，可选择其他时间线继续准备。</span>' : ''}</div><p class="ai-error" data-error role="alert" hidden></p></form>`;
    return `<section class="section-card" id="edit-panel"><h2>AI 剪辑与人工审查 <small>AI 剪辑 ${edits.length} 次</small></h2><div id="production-completion-host">${completionSection(job,choice)}</div><div id="edit-runs">${editRunsMarkup(data)}</div>${form}</section>`;
  }
  function editRunsMarkup(data) {
    const job = data.job, runs = [...(job.edits || [])].reverse();
    const completed = runs.find(edit => editState(edit) === 'approved' && currentEdit(job,edit));
    const edits = completed ? [completed,...runs.filter(edit => edit !== completed)] : runs;
    const olderIds = edits.slice(1).filter(edit => edit.status !== 'running').map(edit => edit.id);
    const current = edits.slice(0,1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('');
    const older = edits.length > 1 ? `<details class="edit-older"><summary>较早的 ${edits.length - 1} 次剪辑</summary>${job.archived || !olderIds.length ? '' : `<div class="edit-older-bar"><button type="button" class="secondary compact" data-clean-edits="${esc(olderIds.join(','))}">清理旧剪辑（${olderIds.length} 次）</button><small>只保留当前记录，连同本地文件一起删除，不动达芬奇里的时间线。</small></div>`}${edits.slice(1).map(edit => editRun(job,edit,data.historical_baseline_restore === true)).join('')}</details>` : '';
    return manualCompletion(job) && edits.length ? `<details class="edit-completed-history"><summary>AI 剪辑历史（${edits.length} 次）</summary>${current}${older}</details>` : `${current}${older}`;
  }
  function completionSection(job,choice) {
    const receipt = manualCompletion(job), timeline = completionTimeline(job);
    if (!receipt && (job.edits || []).some(edit => currentEdit(job,edit) && ['awaiting_review','approved'].includes(editState(edit)) && !edit.result_pending && (!skillDelivery(edit) || skillReviewable(edit)))) return '';
    const running = (job.edits || []).some(edit => edit.status === 'running');
    const reason = job.archived ? '恢复制作单后可修改完成状态。' : job.manual_completion_blocked || (running ? '正在剪辑，结束后可手动标记成功。' : !timeline || !timeline.unique_id ? '先为当前选用的配音建立时间线，再标记成功。' : timeline.saved === false ? '时间线尚未保存，请先在达芬奇保存。' : '');
    const disabled = Boolean(reason);
    return `<form id="job-completion-form" class="production-completion${receipt ? ' done' : ''}"><div class="production-completion-heading"><div><h3>${receipt ? '<span class="script-status published">人工已完成</span>' : '人工完成'}</h3><p class="context-note">${receipt ? `已在 ${esc(readableDate(receipt.at))} 手动标记成功` : '已在达芬奇里完成剪辑和审查后，可直接标记成功。'}${timeline ? ` · ${esc(receipt?.timeline_name || timeline.name)}` : ''}</p></div>${receipt ? `<button type="submit" class="secondary" data-revoke-job-completion ${disabled ? 'disabled' : ''}>撤销手动完成</button>` : `<button type="submit" class="primary" data-complete-job ${disabled ? 'disabled' : ''}>手动标记成功</button>`}</div>${receipt ? receipt.note ? `<p class="production-completion-note">${esc(receipt.note)}</p>` : '' : `<details class="production-completion-details"><summary>添加备注（可选）</summary><label class="field"><span>完成备注</span><textarea name="completion_note" rows="2" maxlength="2000" placeholder="例如：手工剪辑、字幕和混音已完成" ${disabled ? 'disabled' : ''}>${esc(choice.completion_note || '')}</textarea></label></details>`}${reason ? `<p class="context-note">${esc(reason)}</p>` : ''}<p class="ai-error" data-error role="alert" hidden></p></form>`;
  }
  function bindVideoResultCards(data,choice = (view.choice[data.job.id] ||= {})) {
    const job = data.job;
    bindTimelineCards(data);
    const completionForm = $('#job-completion-form'), completionNote = completionForm ? $('[name="completion_note"]',completionForm) : null;
    if (completionNote) completionNote.oninput = () => {choice.completion_note = completionNote.value;};
    if (completionForm) completionForm.onsubmit = async event => {
      event.preventDefault();
      const button = $('[type="submit"]',completionForm);if (button.disabled) return;
      const receipt = manualCompletion(job), completed = !receipt, timeline = completionTimeline(job), label = button.textContent;
      const body = completed ? {completed:true,timeline_uid:timeline.unique_id,note:completionNote.value.trim()} : {completed:false,timeline_uid:receipt.timeline_uid,completion_at:receipt.at};
      button.disabled = true;button.textContent = '正在保存…';if (completionNote) completionNote.disabled = true;setError(completionForm,'');
      try {
        await send(`completion-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/completion`,body);
        choice.step = 'edit';delete choice.completion_note;
        toast(completed ? '第 4 步已手动标记成功。' : '已撤销手动完成。');await refreshProductionDetail(data,'video',{scope:'edit'});
      } catch (error) {
        setError(completionForm,fail(error));button.disabled = false;button.textContent = label;if (completionNote) completionNote.disabled = false;
      }
    };
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
        try {await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recoverEdit)}/recheck`,{method:'POST',body:{recover_result:true}});toast('剪辑结果已恢复；工程核验未执行。');await route(true);}
        catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '恢复结果';}
      });
      $$('[data-recheck-edit]').forEach(button => button.onclick = async () => {
        button.disabled = true;button.textContent = '正在核验…';
        try {const result = await api(`/api/production/jobs/${encodeURIComponent(job.id)}/edits/${encodeURIComponent(button.dataset.recheckEdit)}/recheck`,{method:'POST',body:{}});toast(result.edit?.validation?.scope === 'current_coverage' ? '当前画面覆盖已更新；画面内容仍需人工确认。' : '现有工程已重新核验，请查看状态和问题。');await route(true);}
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
      status.textContent = loadingDefaults || !defaults ? '正在检查剪辑客户端…' : clientsError ? `剪辑客户端检查失败：${clientsError}` : !model ? '请选择本次剪辑使用的兼容模型。' : !state?.available ? `${client}暂不可用：${state?.error || state?.label || '请在设置中检查客户端连接。'}` : !compatible(model,usesDefault() ? defaults.reasoning_effort : localSettings().effort) ? `${client}已连接；${catalog?.models_error || '模型或思考强度尚未确认，请在设置中检查。'}` : selectedTimelineRunning() ? '当前目标时间线已有剪辑任务，请选择其他时间线。' : clients.busy ? `${client}可用；可同时准备多条剪辑，需要操作达芬奇时会自动等待。` : `${client}可用于 AI 剪辑。`;
      // A ready client needs no line of its own; only waiting, busy or broken states are shown.
      status.hidden = !status.classList.contains('problem') && status.textContent === `${client}可用于 AI 剪辑。`;
      $('[data-edit-usage]',form).textContent = `AI 剪辑默认沿用全局模型与思考强度，支持 Codex 和 Claude，${model ? provider === 'codex' ? '使用 Codex 客户端当前登录账户的额度' : '使用已连接 Claude 账户的订阅额度' : '使用所选客户端当前账户的额度'}。不同时间线可同时准备，操作达芬奇的阶段会自动等待。任务在后台运行，操作达芬奇期间请不要手动改动工程；按技能保存后显示剪辑说明和缺素材项，再交你人工审查，默认不做剪后工程核验。也可以「复制剪辑指令」，粘到 Codex 或 Claude Code 对话里边看边剪。`;
      $('[data-edit-override]',form).hidden = loadingDefaults || Boolean(defaultsError) || !defaults || inherited;
      form.model.disabled = loadingDefaults || Boolean(defaultsError) || inherited;
      form.effort.disabled = form.model.disabled || !form.model.value;
      $('[data-edit-default-refresh]',form).disabled = loadingDefaults || starting;
      const pending = Boolean(pendingTimelineId(form.timeline.value)), submit = $('button[type="submit"]',form);
      submit.disabled = starting || job.archived || selectedTimelineRunning() || selectedTimelineUnavailable() || !ready;
      if (!starting) submit.textContent = pending ? '提交剪辑，建好后自动开始' : '开始 AI 剪辑';
      $('[data-pending-edit-note]',form).hidden = !pending;
      $('[data-pending-edit-note]',form).textContent = selectedTimelineUnavailable() ? '原建线已取消、失败或不再运行，请重新建线或选择目标时间线。已填写的剪辑要求已保留。' : '可以先提交剪辑；本次时间线建好后自动接着剪，关闭页面也会继续。';
      $('[data-copy-handoff]',form).disabled = pending || selectedTimelineUnavailable();
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
    form.timeline.onchange = updateEditControls;
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
      if (selectedTimelineRunning()) {setError(form,'当前目标时间线已有剪辑任务，请选择其他时间线。');return;}
      const body = {...options(),...(usesDefault() ? {} : localSettings())};
      const problem = materialProblem(body.materials);
      if (problem) {setError(form,problem);return;}
      if (!modelReady()) {setError(form,defaultsError || (loadingDefaults ? '正在读取全局默认配置，请稍候。' : '请选择本次剪辑使用的兼容模型，或修改全局设置。'));return;}
      const submit = $('button[type="submit"]',form);submit.disabled = true;submit.textContent = '正在加入剪辑任务…';
      starting = true;updateEditControls();
      try {await send(`edit-${job.id}`,`/api/production/jobs/${encodeURIComponent(job.id)}/edit`,body);toast(body.timeline_request_id ? '已提交剪辑，本次时间线建好后自动接着剪，关闭页面也会继续。' : '已加入剪辑任务，需要操作达芬奇时会自动等待，完成后这里会显示结果。');}
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
    const basisUid = [job.manual_completion?.timeline_uid, approvedEdit?.timeline_uid].find(uid => uid && active.some(item => item.unique_id === uid));
    const basis = job.status === 'approved' && basisUid ? (job.timelines || []).findIndex(item => item.unique_id === basisUid) : -1;
    const settled = item => basis >= 0 && (job.timelines || []).indexOf(item) < basis && !(job.edits || []).some(run => run.timeline_uid === item.unique_id && run.status === 'running');
    const card = (item,compact = false) => {
      const version = (job.takes || []).findIndex(take => take.id === item.take_id) + 1;
      const edit = [...(job.edits || [])].reverse().find(run => run.timeline_uid === item.unique_id), state = edit && editState(edit);
      const completed = job.manual_completion?.timeline_uid === item.unique_id && job.manual_completion?.take_id === item.take_id;
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
      return `<article class="timeline-card${current?.unique_id === item.unique_id ? ' current' : ''}${item.archived ? ' archived' : ''}" data-timeline-id="${esc(item.unique_id)}" data-activity-source="${esc(item.unique_id)}"><div class="timeline-info"><strong>${esc(item.name)}${current?.unique_id === item.unique_id ? ' <span class="script-status published">当前生效</span>' : ''}</strong><small>${esc(item.creator)}${compact ? '' : ` · ${markers} · ${(item.frames / Number(item.fps)).toFixed(1)} 秒${format.length ? ` · ${esc(format.join(' · '))}` : ''}`} · <span title="${esc(item.take_id)}">${version ? `配音第 ${version} 版` : `配音 ${esc(item.take_id)}`}${item.take_id === job.selected_take ? '（当前选用）' : ''}</span> · ${esc(readableDate(item.built_at))}</small><div class="timeline-task-state">${taskControl}</div>${status ? `<p class="timeline-edit-state"><span class="script-status${completed || state === 'approved' ? ' published' : ''}" data-timeline-ai-state>${esc(status)}</span>${edit && !completed ? ` <small>最近一次：${esc(readableDate(edit.finished_at || edit.started_at))}</small>` : ''}</p>` : ''}${(item.warnings || []).length ? `<ul class="take-warnings">${item.warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}<p class="ai-error" data-error role="alert" hidden></p></div><div class="timeline-actions"><button type="button" class="secondary" data-open-timeline="${esc(item.unique_id)}">在达芬奇打开</button><button type="button" class="subtle" data-archive-timeline="${esc(item.unique_id)}" ${locked ? 'disabled' : ''} title="${esc(lockTitle || (item.archived ? '恢复到时间线列表，保留原任务状态。' : '收起到历史时间线，保留达芬奇工程和所有剪辑记录。'))}">${item.archived ? '恢复时间线' : '归档时间线'}</button></div></article>`;
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
      $$('[data-timeline-task],[data-archive-timeline]',original).forEach(node => {node.disabled = true;});
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
        const node = card();if (node) $$('[data-timeline-task],[data-archive-timeline]',node).forEach(control => {control.disabled = Boolean(job.archived || (job.edits || []).some(edit => edit.timeline_uid === uid && edit.status === 'running'));});
      }
    };
    $$('[data-archive-timeline]',panel).forEach(button => button.onclick = () => {
      if (button.disabled) return;
      const item = job.timelines.find(item => item.unique_id === button.dataset.archiveTimeline);
      if (item) void save(item.unique_id,'archive',{archived:!item.archived});
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

  // The chosen variant as written (carousel part left out); each spoken, on-screen and 🔴 publish paragraph gets its own copy button.
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
    if (take.id === job.selected_take) return '<span class="script-status published">已选用</span>';
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
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content',job.product))}">${icon('back')}返回内容</a><div class="page-heading"><div><span class="eyebrow">PRODUCTION JOB</span><h1 data-production-title>${esc(productionTitle(job))}</h1><p>${esc(job.product)} · ${esc(job.direction_id || '未指定方向')}${variantSubtitle(productionTitle(job),job.variant_label) ? ` · ${esc(variantSubtitle(productionTitle(job),job.variant_label))}` : ''} · <span class="script-status ${['voiced','timeline','approved'].includes(job.status) ? 'published' : ''}">${esc(job.archived ? '已归档' : statusLabels[job.status] || job.status)}</span>${data.script ? ` · 采用稿 · 第 ${esc(data.script.version || 1)} 版` : ' · 采用稿已不可读'}</p></div><details class="script-more-actions production-more-actions"><summary>更多操作</summary><div><button class="subtle" id="job-archive">${job.archived ? '恢复制作单' : '归档制作单'}</button><button class="subtle production-delete" id="job-delete">永久删除</button></div></details></div>${recoverySection(job)}${problemsMarkup(job.problems,scriptGaps(job,data) && !fillRunning(job))}<section class="section-card" id="job-script-panel"><div class="section-title-row"><h2>脚本 <small>${esc(job.variant_label || data.variant?.label || '')}${job.variant_label || data.variant?.label ? ' · ' : ''}${job.lines.length} 个意群</small></h2>${fillControls(job,data)}</div><div id="job-adopted-script"></div><div class="script-lines"><h3>意群表 <small>含画面要求${selected ? '与配音时间' : ''}</small></h3>${linesTable(job.lines,selected ? selected.groups : null)}</div><details class="script-original" id="job-script-original" ${choice.script_original_open ? 'open' : ''}><summary>原稿全文 <small>开头画面、屏显、口播与中文、发布文案等，可逐段复制</small></summary><div class="script-variant markdown-body" id="job-script-body"></div></details></section><section class="section-card" id="voice-panel"><h2>配音</h2>${job.voicing ? '<p class="production-busy">正在生成配音，完成后自动刷新…</p>' : ''}<form id="take-form" class="take-form"><div class="form-row"><label class="field"><span>音色</span><select name="voice">${voices.map(voice => `<option value="${esc(voice.voice_id)}" ${voice.voice_id === voiceId ? 'selected' : ''}>${esc(voiceLabel(voice))}${data.defaults?.[job.product] === voice.voice_id ? '（默认）' : ''}</option>`).join('')}<option value="__new" ${voiceId === '__new' ? 'selected' : ''}>新音色 ID…</option></select></label><label class="field"><span>模型</span><select name="model_id">${models.map(model => `<option value="${esc(model.id)}" ${model.id === modelId ? 'selected' : ''}>${esc(model.name)}</option>`).join('')}</select></label></div><div class="form-row" data-new-voice ${voiceId === '__new' ? '' : 'hidden'}><label class="field"><span>新音色 ID</span><input name="new_voice_id" pattern="[A-Za-z0-9]{10,64}" autocomplete="off" value="${esc(choice.new_voice_id || '')}"></label><label class="field"><span>备注（存入音色库）</span><input name="new_note" maxlength="500" value="${esc(choice.new_note || '')}"></label></div><div class="take-settings" data-settings></div><details class="take-text"><summary>送去配音的文字（${data.tts.characters} 字符${data.tts.substitutions ? `，已套用读法 ${data.tts.substitutions} 处` : ''}）</summary><pre>${esc(data.tts.text)}</pre></details><div class="take-actions"><button class="primary" type="submit" ${job.voicing || job.archived ? 'disabled' : ''}>生成配音</button><span>约消耗 ${data.tts.characters} 字符额度</span></div><p class="ai-error" data-error role="alert" hidden></p></form></section><section class="section-card" id="take-panel"><h2>配音版本 <small>${takes.length} 版</small></h2>${takes.length ? takes.map(take => `<article class="take-card ${take.id === job.selected_take ? 'selected' : ''}" data-take-id="${esc(take.id)}" data-activity-source="${esc(take.id)}"><div class="take-head"><div><strong>${esc(voiceName(take.voice_id))}</strong><small>${esc(modelName(take.model_id))} · ${Object.keys(settingLabels).filter(key => key in (take.settings || {})).map(key => `${settingLabels[key]} ${take.settings[key]}`).join(' · ')} · ${esc(seconds(take.duration))} · ${esc(readableDate(take.created_at))}</small></div>${takeSelectionMarkup(data,take)}</div><audio controls preload="none" src="${esc(audioURL(job.id,take.id,'mp3'))}"></audio>${voiceSegments(take,job.lines)}${(take.warnings || []).length ? `<ul class="take-warnings">${take.warnings.map(item => `<li>${esc(item)}</li>`).join('')}</ul>` : ''}<details><summary>意群时间（${esc(sourceLabels[take.alignment_source] || take.alignment_source)}）</summary><table class="production-table compact"><thead><tr><th>#</th><th>开始</th><th>结束</th><th>对上的字</th></tr></thead><tbody>${voiceTimingRows(take)}</tbody></table></details><div class="take-links" title="${esc(take.id)}">${take.wav ? `<a href="${esc(audioURL(job.id,take.id,'wav',true))}">下载 WAV</a>` : ''}<a href="${esc(audioURL(job.id,take.id,'mp3',true))}">下载 MP3</a></div></article>`).join('') : '<p class="context-note">还没有配音。</p>'}</section>${timelineSection(data,choice,selected)}${editSection(data,choice)}<details class="section-card production-history"><summary>制作记录（${job.history.length} 条）</summary><ol>${[...job.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('')}</ol></details>`;
    window.CaseScripts?.mountAdopted?.($('#job-adopted-script'),data.script,{productionId:job.id,kind:'video'});
    window.WorkbenchLinks?.mount({kind:'video',record:job,script:data.script});
    mountProductionNeeds(data,'video');
    bindTakeSegments(data);
    const publicationPanel = document.createElement('section');
    publicationPanel.id = 'video-publication-panel';publicationPanel.className = 'section-card';
    $('#content').querySelector('.production-history').before(publicationPanel);
    const prepublishPanel = document.createElement('section');
    prepublishPanel.id = 'prepublish-review-panel';prepublishPanel.className = 'section-card';
    publicationPanel.before(prepublishPanel);
    window.PrepublishReview?.mount(prepublishPanel,{job,api});
    window.VideoPublication?.mount(publicationPanel,{job,api,onLoaded:publication => {
      if (view.progress?.id !== job.id || !publicationPanel.isConnected) return;
      view.progress.publication = publication;refreshProductionProgress(view.progress.data,'video');
    },onSaved:async () => {if(seq === view.sequence) await route(true);}});
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
    $('#job-delete')?.addEventListener('click',() => void deleteOrder('job',job));
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
  async function adoptAndProduce(modal,{task,round,taskRevision,product,direction,title,spoken,chinese,variantIndex,kind = 'video'}) {
    const key = JSON.stringify([variantIndex,title,spoken,chinese]);
    if (modal.variant !== key) {modal.done = {};modal.requests = {};modal.variant = key;}
    const adopted = await once(modal,'script','/api/scripts',async () => ({revision:(await api(`/api/creation?product=${encodeURIComponent(product)}`)).revision,script:{title,text:spoken.join('\n'),translation_zh:chinese.length === spoken.length ? chinese.join('\n') : '',product,direction_id:direction,confirmed:true},source:{task_id:task.task_id,round_id:round.round_id,task_revision:taskRevision,...(variantIndex === null ? {} : {variant_index:variantIndex})}}));
    if (kind === 'carousel') {
      closeModal(true);
      if (adopted.reused) toast('这条变体之前已采纳，继续用原来的采用稿制作图文。');
      fromScriptCarousel(adopted.script);
      return;
    }
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
  function manualSplit(task,round,product,direction,reason,kind = 'video') {
    const carousel = kind === 'carousel';
    const modal = openModal(carousel ? '手动指定要制作的口播脚本' : '手动指定要配音的口播',`<p class="context-note">${esc(reason)}</p><div class="production-format-choice"><p>可以让 AI 只整理排版：新开一轮，完成后逐字核对口播和中文，对不上就不采用。也可以直接把要制作的英文口播粘贴到下面，一行一个意群。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div><details class="production-candidate-source"><summary>查看本轮候选原文</summary><pre class="carousel-preview">${esc(round.output || '')}</pre></details><label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(`${direction ? direction + ' ' : ''}手动拆分`)}"></label><label class="field"><span>口播（一行一个意群）</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label><p class="context-note">${carousel ? '采纳这条口播脚本后，继续用图文技能生成逐张表。' : '画面要求在制作单里补填。'}</p>`,carousel ? '采纳并继续制作图文' : '采纳并创建制作单',async modal => {
      const values = new FormData(modal.form), spoken = splitLines(values.get('spoken')), chinese = splitLines(values.get('chinese'));
      const issue = linesIssue(spoken,chinese);if (issue) {setError(modal.dialog,issue);return;}
      await adoptAndProduce(modal,{task,round,taskRevision:task.revision,product,direction,title:values.get('title').trim(),spoken,chinese,variantIndex:null,kind});
    });
    bindFormatRound(modal,task,round);
  }

  async function fromCandidate(task,round,kind = 'video') {
    const carousel = kind === 'carousel';
    const product = task.product || task.context?.product || currentProduct(), direction = task.context?.direction_id || '';
    if (!product) {toast('请先选择一个具体产品。',true);return;}
    let data;
    try {data = await api(`/api/production/variants?task_id=${encodeURIComponent(task.task_id)}&round_id=${encodeURIComponent(round.round_id)}`);}
    catch (error) {
      if (error.status === 409 && /可配音的口播/.test(fail(error))) {manualSplit(task,round,product,direction,fail(error),kind);return;}
      toast(`暂时无法拆分变体：${fail(error)}`,true);return;
    }
    const variants = data.variants, defaultTitle = variant => `${direction ? direction + ' ' : ''}${variant.label}`;
    const loose = variants.some(variant => variant.loose);
    const options = variants.map((variant,position) => `<label class="variant-option"><input type="radio" name="variant" value="${variant.index}" ${position === 0 ? 'checked' : ''}><span><strong>${esc(variant.label)}</strong><small>${variant.lines.length} 个意群${variant.problems.length ? ` · ${variant.problems.length} 处需核对` : ''}</small></span></label>`).join('');
    const content = `<label class="field"><span>脚本标题</span><input name="title" required maxlength="500" value="${esc(defaultTitle(variants[0]))}"></label><div data-variant-preview></div><details class="production-line-editor" data-line-editor><summary>调整意群（拆分、合并或修正断行）</summary><p class="context-note">一行一个意群。不改动时会带入意群表；改动后画面要求需在制作单里补填。</p><label class="field"><span>口播</span><textarea name="spoken" rows="8" spellcheck="false"></textarea></label><label class="field"><span>对应中文（可选，与口播逐行对应）</span><textarea name="chinese" rows="6"></textarea></label></details>`;
    const modal = openModal('选择要制作的变体',`<p class="context-note">选择变体即可查看对应脚本。口播每行是一个意群；采用后自动保留这次正文，修改时保留历史。${carousel ? '下一步用图文技能按这份意群表生成逐张表，核对后创建图文制作单。' : ''}</p>${loose ? '<div class="production-format-choice"><p>这一轮没有按「口播（配音用）」代码块交付，下面是宽松识别的结果：意群按句拆分，请逐行核对。也可以让 AI 只整理排版（新开一轮，逐字核对后才采用）。</p><button type="button" class="secondary" data-format-round>一键整理格式</button></div>' : ''}${variantPickerBody(options,content)}`,carousel ? '采纳并继续制作图文' : '采纳并创建制作单',async modal => {
      const values = new FormData(modal.form), chosen = variants.find(item => String(item.index) === values.get('variant'));
      const spoken = edited ? splitLines(values.get('spoken')) : chosen.lines.map(line => line.spoken);
      const chinese = edited ? splitLines(values.get('chinese')) : chosen.lines.every(line => line.zh) ? chosen.lines.map(line => line.zh) : [];
      const issue = edited ? linesIssue(spoken,chinese) : '';if (issue) {setError(modal.dialog,issue);return;}
      await adoptAndProduce(modal,{task,round,taskRevision:data.task_revision,product,direction,title:values.get('title').trim(),spoken,chinese,variantIndex:chosen.index,kind});
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
    $$('input[name="variant"]',modal.dialog).forEach(input => input.onchange = () => {
      const chosen = variants.find(item => String(item.index) === input.value);
      if (!titleEdited) modal.form.title.value = defaultTitle(chosen);
      preview();
    });
    bindFormatRound(modal,task,round);
    preview();
  }

  const carouselLink = item => url(`#production/${encodeURIComponent(item.id)}`,item.product);
  const carouselAutopilotRun = item => (item.autopilot_runs || []).find(run => run.status === 'running') || (item.autopilot_runs || []).at(-1);
  const carouselAutopilotRunning = item => carouselAutopilotRun(item)?.status === 'running';
  const subjectPreservationPassed = output => {
    const receipt = output?.subject_preservation;
    return Boolean(output?.workflow === 'subject_locked' && output.ai_generated === true && !output.stale && receipt?.status === 'passed' &&
      receipt.method === 'masked_source_pixels' && receipt.render_contract === 'segmented_background_v4' && output.sha256 && output.base_sha256 && output.input_signature &&
      receipt.final_sha256 === output.sha256 && receipt.base_sha256 === output.base_sha256 &&
      receipt.input_signature === output.input_signature && receipt.protected_pixel_count > 0 && receipt.mismatch_pixels === 0);
  };
  const carouselSubjectsPreserved = item => Boolean(item.slides?.length) && item.slides.every(slide => {
    const output = (slide.outputs || []).find(entry => entry.id === slide.output_id);
    return output && !output.stale && subjectPreservationPassed(output);
  });
  function carouselAutopilotSection(data) {
    const item = data.carousel, run = carouselAutopilotRun(item), running = run?.status === 'running';
    const generationFirst = run?.workflow === 'generation_first', subjectLocked = run?.workflow === 'subject_locked';
    const subjectsPreserved = subjectLocked && carouselSubjectsPreserved(item);
    const done = run?.status === 'completed' && run.delivery_id && (item.deliveries || []).some(delivery => delivery.id === run.delivery_id) && (!subjectLocked || subjectsPreserved);
    const labels = {needs_attention:run?.phase || '需要人看',running:'AI 正在全程制作',completed:subjectsPreserved ? '原图主体已恢复' : subjectLocked ? '图片待重新制作' : generationFirst ? '图片已生成' : 'AI 制作已完成',failed:'这次制作未完成',cancelled:'已停止',interrupted:'制作已中断'};
    const layoutOnly = (item.requirements || {}).mode === 'layout_only';
    const workflow = running ? run.workflow : item.requirements?.workflow || 'review_first';
    const workflowNote = layoutOnly ? (item.requirements?.caption_layer === 'behind_subject' ? '拼版和裁切在选底图时已定，这一步只在底图上加英文花字：有文字带放文字带，没有就放画面边缘细节最少的位置。不抠图、不生图、不加黑边，完成后独立核对。' : '仅在原图上排锁定英文文字，不更换背景、手或产品；完成后独立核对。') : workflow === 'review_first' ? '按当前制作要求完成选图、出图、核对和整理成品包。已有结果会保留，可离开页面等待完成。' : workflow === 'generation_first' ? '按当前先出图流程生成图片，产品一致性核对暂缓；核对结果与逐张确认记录保留在制作单中。' : 'AI 编写逐张提示词，参考原图视角生成不含产品的空背景；随后在本地恢复原图主体，逐张验证主体像素后打包。产品主体不重绘，文字、证据与排版可在出图后按需核对。';
    const completionNote = run?.status === 'completed' ? subjectLocked ? subjectsPreserved ? '整组图片已生成并打包，已在 AI 背景上恢复原图主体并通过主体像素验证。文字、证据与排版核对结果以逐张记录为准。' : '尚未取得整组当前主体恢复验证回执，此版本不能作为当前确认成品，请重新制作。' : generationFirst ? run.review_deferred ? '整组图片已生成并打包，产品一致性核对暂未执行（核对暂缓），可按需单独核对。' : '整组图片已生成，核对结果以各张图片的记录为准。' : '' : '';
    const busy = generating(item) || Boolean(item.clip_picking_live) || (item.searches || []).some(entry => entry.status === 'running');
    const content = `<div class="section-title-row"><h2>AI 全程制作${infoTip(workflowNote,'本次制作流程')}</h2><div class="take-actions">${running ? `<button type="button" class="secondary compact" data-autopilot-cancel="${esc(run.id)}">停止</button>` : `${done ? `<a class="primary compact" download href="${esc(deliveryURL(item,{id:run.delivery_id}))}">下载成品包</a>` : ''}<button type="button" class="${done ? 'secondary' : 'primary'} compact" data-autopilot-start ${item.archived || busy ? 'disabled' : ''}>${run ? '继续由 AI 完成' : '交给 AI 完成'}</button>`}</div></div>${run ? `<div data-activity-source="${esc(run.id)}"><p role="status"><strong>${esc(labels[run.status] || 'AI 制作记录')}</strong>${run.phase ? ` · ${esc(run.phase)}` : ''}</p>${run.message ? `<p class="context-note">${esc(run.message)}</p>` : ''}${completionNote ? `<p class="context-note" data-autopilot-generation-summary>${completionNote}</p>` : ''}${run.error ? `<p class="ai-error" role="alert">${esc(run.error)}</p>` : ''}${running ? runProgressMarkup({...run,live:{progress:run.message || run.phase}},'autopilot') : `<p class="context-note">${esc(readableDate(run.finished_at || run.started_at))}</p>`}</div>` : ''}<p class="ai-error" data-autopilot-error role="alert" hidden></p>`;
    return `<section class="section-card" id="carousel-autopilot-panel">${carouselQuiet(item) ? `<details class="carousel-autopilot-more"><summary>再由 AI 制作一版</summary>${content}</details>` : content}</section>`;
  }
  function bindCarouselAutopilot(data) {
    const item = data.carousel, panel = $('#carousel-autopilot-panel'), sequence = view.sequence;
    if (!panel) return;
    const showError = error => {if (sequence !== view.sequence) return;const node = $('[data-autopilot-error]',$('#carousel-autopilot-panel'));if (node) {node.textContent = fail(error);node.hidden = false;}};
    const start = $('[data-autopilot-start]',panel);
    if (start) start.onclick = async () => {
      start.disabled = true;
      try {
        await send(`autopilot-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/autopilot`,{revision:data.revision});
        if (sequence !== view.sequence) return;
        await refreshProductionDetail(data,'carousel',{scope:'autopilot'});toast('已按当前制作要求接续，进度和待协作事项已保存。');
      } catch (error) {if (sequence === view.sequence) {showError(error);start.disabled = false;}}
    };
    const stop = $('[data-autopilot-cancel]',panel);
    if (stop) stop.onclick = async () => {
      stop.disabled = true;stop.textContent = '正在停止…';
      try {
        await api(`/api/production/carousels/${encodeURIComponent(item.id)}/autopilot/${encodeURIComponent(stop.dataset.autopilotCancel)}/cancel`,{method:'POST',body:{}});
        if (sequence !== view.sequence) return;
        await refreshProductionDetail(data,'carousel',{scope:'autopilot'});
      } catch (error) {if (sequence === view.sequence) {showError(error);stop.disabled = false;stop.textContent = '停止';}}
    };
    const prior = view.autopilotWatch;
    if (prior?.id === item.id && prior.sequence === sequence) {
      if (!carouselAutopilotRunning(item)) {clearTimeout(prior.timer);view.autopilotWatch = null;}
      return;
    }
    if (prior) clearTimeout(prior.timer);
    view.autopilotWatch = null;
    if (!carouselAutopilotRunning(item)) return;
    const watcher = view.autopilotWatch = {id:item.id,sequence,timer:null};
    const poll = async () => {
      if (view.autopilotWatch !== watcher || sequence !== view.sequence || !panel.isConnected) return;
      try {
        if (!view.modal) {
          await refreshProductionDetail(data,'carousel',{scope:'autopilot',checkDelivery:false});
          if (sequence === view.sequence && !carouselAutopilotRunning(data.carousel)) await view.progress?.loadDelivery?.();
        }
      } catch (error) {showError(error);}
      if (view.autopilotWatch === watcher && carouselAutopilotRunning(data.carousel)) watcher.timer = setTimeout(poll,3000);
      else if (view.autopilotWatch === watcher) view.autopilotWatch = null;
    };
    watcher.timer = setTimeout(poll,3000);
  }
  const carouselLabels = {draft:'待选底图',picking:'选图中',picked:'底图已选齐',ready:'底图已选齐',reviewing:'成图待确认',approved:'成图已确认',published:'已登记发布'};
  const carouselLabel = item => item.archived ? '已归档' : carouselLabels[item.status] || '待选底图';
  const pad = number => String(number).padStart(2,'0');
  const imageURL = (item,candidate,size) => `/api/production/carousels/${encodeURIComponent(item.id)}/images/${encodeURIComponent(candidate.id)}?size=${size}`;
  const stillModels = new Map(Object.entries(editModels).map(([id,name]) => [id,{id,name,provider:'claude'}]));
  stillModels.set('codex-default',{id:'codex-default',name:'Codex 客户端默认',provider:'codex'});
  const stillProvider = model => stillModels.get(model)?.provider || (String(model || '').startsWith('claude-') ? 'claude' : 'codex');
  const stillClient = provider => provider === 'codex' ? 'Codex 客户端' : 'Claude Code 客户端';
  const stillModelLabel = model => stillModels.get(model)?.name || editModels[model] || model || '客户端默认';
  function stillDefaultCompatible(settings) {
    if (!settings) return false;
    const model = settings.model || 'codex-default', effort = settings.reasoning_effort || null;
    if (stillProvider(model) === 'claude') return Object.hasOwn(editModels,model) && [null,'high','xhigh','max'].includes(effort);
    if (model === 'codex-default') return effort === null;
    const known = stillModels.get(model);
    return Boolean(known && (!effort || (known.reasoning_efforts || []).includes(effort)));
  }
  function stillModelOptions(selected) {
    const models = [...stillModels.values()], option = model => `<option value="${esc(model.id)}" ${model.id === selected ? 'selected' : ''} ${model.available === false ? 'disabled' : ''}>${esc(model.name || model.id)}${model.available === false ? ' · 未就绪' : ''}</option>`;
    const unknown = selected && !stillModels.has(selected) ? `<option value="${esc(selected)}" selected disabled>当前模型尚未确认 · ${esc(selected)}</option>` : '';
    return unknown + [['claude','本机 Claude Code'],['codex','本机 Codex']].map(([provider,label]) => {
      const entries = models.filter(model => stillProvider(model.id) === provider);return entries.length ? `<optgroup label="${label}">${entries.map(option).join('')}</optgroup>` : '';
    }).join('');
  }
  function searchRun(item,search) {
    const live = search.live || {}, report = search.report || {}, warnings = [...(search.problems || []),search.readback_error].filter(Boolean), provider = search.provider || stillProvider(search.model), client = stillClient(provider);
    const effectiveModel = live.effective_model || search.effective_model, effectiveEffort = live.effective_effort || search.effective_effort;
    const gapsLabel = search.status === 'completed' ? '没找到合格画面' : search.status === 'running' ? '待核验画面' : '尚未完成核验';
    const waiting = search.status === 'running' && (String(live.phase || '').startsWith('waiting_') || String(live.progress || '').startsWith('等待达芬奇'));
    return `<article class="edit-card ${esc(search.status)}" data-activity-source="${esc(search.id)}"><div class="take-head"><div><strong><span class="script-status ${search.status === 'completed' ? 'published' : ''}">${esc(waiting ? '等待达芬奇' : editLabels[search.status] || search.status)}</span> 第 ${esc(search.slides.join('、'))} 张 · 每张 ${search.per_slide} 张候选</strong>${runReceipt({client,provider,requestedModel:search.requested_model || search.model,effectiveModel,effectiveEffort,requestedEffort:search.effort,label:stillModelLabel,extra:[search.materials.join('、')],started:search.started_at,finished:search.finished_at})}</div>${search.status === 'running' ? `<button type="button" class="secondary" data-cancel-search="${esc(search.id)}">${waiting ? '取消排队' : '停止'}</button>` : item.archived ? '' : `<button type="button" class="subtle edit-delete" data-delete-search="${esc(search.id)}">${icon('trash')}删除</button>`}</div>${search.status === 'running' ? `<p class="production-busy">${esc(live.progress || '正在找底图…')}</p>${live.latest ? `<pre class="edit-live">${esc(live.latest)}</pre>` : ''}` : `<p class="edit-facts">新增候选 ${search.added || 0} 张</p>`}${(report.gaps || []).length ? `<h3>${esc(gapsLabel)}</h3><ul class="edit-gaps">${report.gaps.map(gap => `<li>第 ${esc(gap.slide)} 张：${esc(gap.need)}</li>`).join('')}</ul>` : ''}${(report.questions || []).length ? `<h3>待你确认</h3><ul class="edit-gaps">${report.questions.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}${warnings.length ? `<ul class="take-warnings">${warnings.map(text => `<li>${esc(text)}</li>`).join('')}</ul>` : ''}${search.error ? `<p class="ai-error">${esc(search.error)}</p>` : ''}${search.output ? `<details><summary>${esc(client)}的选图说明</summary><pre class="edit-output">${esc(search.output)}</pre></details>` : ''}${search.instruction ? `<p class="context-note">本次额外要求：${esc(search.instruction)}</p>` : ''}</article>`;
  }
  // Uploads local pictures for one slide. One picture can become the base at once (replacing the slide's other candidates).
  const isImageFile = file => /^image\/(png|jpe?g|webp)$/.test(file.type) || /\.(png|jpe?g|webp)$/i.test(file.name || '');
  async function uploadSlideImages(item,slide,files,{pick = false,onStatus = () => {}} = {}) {
    const images = [...files].filter(isImageFile);
    if (!images.length) throw new Error('没有可用的图片：只支持 PNG、JPG、WebP。');
    const root = `/api/production/carousels/${encodeURIComponent(item.id)}`, made = [];
    try {
      for (const [index,file] of images.entries()) {
        onStatus(`正在上传 ${index + 1}/${images.length}：${file.name || '粘贴的图片'}`);
        const response = await fetch(`${root}/upload?slide=${slide}&name=${encodeURIComponent(file.name || 'paste.png')}&request_id=${encodeURIComponent(crypto.randomUUID())}`,
          {method:'POST',headers:{'X-Workbench-Token':state.token,'Content-Type':'application/octet-stream'},body:file});
        const result = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(result.error || `上传失败（${response.status}）`);
        made.push(result.candidate);
      }
      const picked = pick && made.length === 1;
      if (picked) {
        const fresh = await api(root);
        await send(`base-${item.id}-${slide}`,`${root}/base`,{revision:fresh.revision,slide,candidate:made[0].id});
      }
      return {made,picked};
    } catch (error) {
      if (made.length) error.message += `（已成功导入 ${made.length} 张，可在候选里看到）`;
      throw error;
    }
  }
  // Content owns archive and restore; production still removes each order and its files safely.
  function deleteContentOrders(targets,options = {}) {
    const items = [...new Map((targets || []).filter(target => target?.id && ['video','carousel'].includes(target.kind)).map(target => [`${target.kind}:${target.id}`,{id:target.id,kind:target.kind === 'video' ? 'job' : 'carousel'}])).values()];
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
    return openModal('批量永久删除制作单',`<p class="context-note">将永久删除所选 ${targets.length} 张制作单（不需要先归档），以及它们在本地保存的配音、候选图、成图、成品包、发布登记及托管成片等全部文件。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程。</li><li>采用稿、修改历史和案例库保留，可以重新创建制作单。</li><li>仍有任务在运行的制作单会被跳过并说明原因。</li></ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await execute();
      if (result) {closeModal(true);await complete(result);}
    });
  }
  // Permanent removal of an archived order with its local files; DaVinci timelines and script versions stay.
  async function deleteOrder(kind,item) {
    const base = `/api/production/${kind === 'job' ? 'jobs' : 'carousels'}/${encodeURIComponent(item.id)}`;
    let preview;
    try {preview = await api(`${base}/delete`,{method:'POST',body:{preview:true}});}
    catch (error) {toast(fail(error),true);return;}
    const bytes = preview.bytes || 0;
    const size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
    const parts = kind === 'job'
      ? [`${preview.takes} 条配音`,`${preview.edits} 次 AI 剪辑记录`]
      : [`${preview.slides} 张图的候选、成图和核对记录`,`${preview.searches} 次找底图`,`${preview.generations} 次出图`,`${preview.deliveries} 个成品包`];
    if (kind === 'job' && preview.publications) parts.push(`${preview.publications} 次发布登记及托管成片`);
    openModal('永久删除制作单',`<p class="context-note">将永久删除「${esc(preview.title || item.title || item.id)}」：${esc(parts.join('、'))}，以及它们在本地保存的全部文件（${preview.files} 个，共约 ${esc(size)}）。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程${kind === 'job' && preview.timelines ? `（已建的 ${preview.timelines} 条时间线需要的话请自行在达芬奇里处理）` : ''}。</li><li>采用稿、修改历史和案例库保留，可以重新创建制作单。</li>${preview.published ? '<li class="ai-error">这张制作单已登记发布；发布登记和托管成品会一并删除，脚本里的发布状态不会自动改回。</li>' : ''}</ul><label class="field"><span><input type="checkbox" name="sure" required> 我已确认永久删除，不需要找回</span></label>`,'永久删除',async modal => {
      const result = await send(`order-delete-${item.id}`,`${base}/delete`,{});
      closeModal(true);
      const warnings = result.warnings || [];
      toast(warnings.length ? warnings[0] : '制作单及本地文件已永久删除。',warnings.length > 0);
      const back = $('.back-link');
      if (back) back.click();else location.hash = url('#content',item.product);
    });
  }
  // The newest search stays open; older ones fold away with a one-click cleanup, like AI edits on a video order.
  function searchRuns(item) {
    const runs = [...(item.searches || [])].reverse();
    const older = runs.slice(1), olderIds = older.filter(search => search.status !== 'running').map(search => search.id);
    return `${runs.slice(0,1).map(search => searchRun(item,search)).join('')}${older.length ? `<details class="edit-older"><summary>较早的 ${older.length} 次找底图</summary>${item.archived || !olderIds.length ? '' : `<div class="edit-older-bar"><button type="button" class="secondary compact" data-clean-searches="${esc(olderIds.join(','))}">清理旧记录（${olderIds.length} 次）</button><small>只保留最新一次，连同本地文件和未选用的候选一起删除；已选为底图的保留，不动达芬奇。</small></div>`}${older.map(search => searchRun(item,search)).join('')}</details>` : ''}`;
  }
  function stillSection(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, searches = item.searches || [];
    const picks = data.clip_picks, counted = picks ? Object.values(picks.counts || {}).reduce((sum,value) => sum + value,0) : 0;
    const summary = picks ? `<p class="context-note" id="clip-ai-summary">上次 AI 挑片段：${esc(readableDate(picks.at))} · ${counted} 个候选片段 · 已记在各张的「从片段取帧」里${(picks.gaps || []).length ? `<br>缺口：${(picks.gaps || []).map(gap => `第 ${esc(gap.slide)} 张：${esc(gap.need)}`).join('；')}` : ''}</p>` : '';
    const history = searches.length ? `<details class="edit-older still-history"><summary>旧的 AI 找画面记录（${searches.length} 次）</summary><div id="search-runs">${searchRuns(item)}</div></details>` : '<div id="search-runs" hidden></div>';
    return `<section class="section-card" id="still-panel"><h2>AI 按标签挑候选片段${infoTip('<p>AI 只读素材片段的标签和每一张的文字，为每一张挑几个候选片段：不看画面、不导出静帧、不占用达芬奇，通常 10–60 秒。挑好后在每一张点「从片段取帧」，推荐的片段排在最前，自己拖动取帧。</p><p data-clip-ai-model>正在读取全局默认模型…</p>')}</h2><p class="production-resolve" data-resolve-status>正在检查达芬奇…</p><fieldset class="material-picker"><legend>素材时间线</legend><div data-materials>正在读取达芬奇里的时间线…</div></fieldset><div class="form-row"><label class="field"><span>每张挑几个片段</span><select name="per_slide">${[1,2,3,4,5].map(value => `<option ${value === (choice.per_slide || 3) ? 'selected' : ''}>${value}</option>`).join('')}</select></label></div><label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="2" maxlength="1000" placeholder="例如：第 1 张要能看到打开的箱子和七块磨板">${esc(choice.still_instruction || '')}</textarea></label><div class="take-actions"><button type="button" class="primary" id="clip-ai-pick" ${item.archived ? 'disabled' : ''}>让 AI 按标签挑候选片段</button><span data-clip-ai-status role="status"></span></div><p class="ai-error" data-error role="alert" hidden></p>${summary}${history}</section>`;
  }
  // Large images open in place (no new tab); ctrl/shift/middle click still open the file itself.
  function openLightbox(source,label,images = []) {
    document.querySelector('dialog.image-lightbox')?.remove();
    const dialog = document.createElement('dialog');dialog.className = 'image-lightbox';
    const gallery = images.length ? images : [{source,label:label || '大图'}];
    let index = Math.max(0,gallery.findIndex(image => image.source === source));
    dialog.innerHTML = `<button type="button" class="icon-button" data-close aria-label="关闭">${icon('close')}</button><img><div class="image-lightbox-navigation"><button type="button" class="secondary compact" data-lightbox-prev aria-label="上一张图片">←</button><span data-lightbox-label role="status" aria-live="polite"></span><button type="button" class="secondary compact" data-lightbox-next aria-label="下一张图片">→</button></div>`;
    const show = () => {
      const current = gallery[index], image = $('img',dialog);
      image.src = current.source;image.alt = current.label || '大图';
      $('[data-lightbox-label]',dialog).textContent = `${index + 1} / ${gallery.length} · ${image.alt}`;
      $('[data-lightbox-prev]',dialog).disabled = index === 0;
      $('[data-lightbox-next]',dialog).disabled = index === gallery.length - 1;
    };
    const move = direction => {index = Math.min(gallery.length - 1,Math.max(0,index + direction));show();};
    show();
    document.body.append(dialog);
    const close = () => {try {dialog.close();} catch {}dialog.remove();};
    $('[data-close]',dialog).onclick = close;
    $('[data-lightbox-prev]',dialog).onclick = () => move(-1);
    $('[data-lightbox-next]',dialog).onclick = () => move(1);
    dialog.addEventListener('click',event => {if (event.target === dialog || event.target === $('img',dialog)) close();});
    dialog.addEventListener('cancel',event => {event.preventDefault();close();});
    dialog.addEventListener('keydown',event => {if (['ArrowLeft','ArrowRight'].includes(event.key)) {event.preventDefault();move(event.key === 'ArrowRight' ? 1 : -1);}});
    dialog.showModal();
  }
  document.addEventListener('click',event => {
    const link = event.target.closest?.('a[target="_blank"]');
    const href = link?.getAttribute('href') || '';
    if (!link || event.button || event.ctrlKey || event.metaKey || event.shiftKey || !location.hash.includes('production')) return;
    if (!/^\/api\/production\/carousels\/CAR-[0-9a-f]{12}\/(images|outputs)\/[A-Z]{3}-[0-9a-f]{12}/.test(href)) return;
    const scope = link.closest('#carousel-image-slides,#pick-panel') || link.closest('[data-step-panel]') || link.parentElement;
    const gallery = [], seen = new Set();
    $$('a[target="_blank"]',scope).forEach(candidate => {
      const source = candidate.getAttribute('href');
      if (!/^\/api\/production\/carousels\/CAR-[0-9a-f]{12}\/(images|outputs)\/[A-Z]{3}-[0-9a-f]{12}/.test(source || '') || seen.has(source)) return;
      seen.add(source);gallery.push({source,label:candidate.querySelector('img')?.alt || '大图'});
    });
    event.preventDefault();openLightbox(href,link.querySelector('img')?.alt,gallery);
  });
  // Every slide is one tile of a grid, so five slides fit on one screen; only the first few candidates show, the rest fold.
  const PICK_VISIBLE = 4;
  // Default for every order, including new ones: Chinese UI labels; title_lines stays the image's English copy.
  const carouselSlideTitle = slide => [slide.title_zh,slide.role].map(value => String(value || '').trim()).find(value => /[\u3400-\u9fff]/.test(value)) || '图片';
  const slideSourceLines = slide => Array.isArray(slide.source_lines) ? slide.source_lines.filter(number => Number.isSafeInteger(number) && number > 0) : [];
  function slideSourceMarkup(slide,context) {
    const numbers = slideSourceLines(slide);
    if (!numbers.length) return '未建立对应';
    const records = Array.isArray(context?.records) ? context.records : [];
    const label = `第 ${numbers.join('、')} 意群`;
    if (!records.length) return `${esc(label)}（原稿快照不可读）`;
    const lines = numbers.map(number => {
      const record = records.find(entry => entry.number === number);
      return record ? `<p><b>${number}.</b> ${esc(record.spoken || '原句缺失')}${record.zh ? `<br>${esc(record.zh)}` : ''}</p>` : `<p>第 ${number} 意群未在原稿快照中找到</p>`;
    }).join('');
    return `<details class="production-candidate-source"><summary>${esc(label)} · 查看原句</summary>${context.script_version ? `<small>本单绑定原稿 · 第 ${esc(context.script_version)} 版</small>` : ''}${lines}</details>`;
  }
  function slideProofContext(slide,context,folded = false) {
    const body = `<div class="context-note" data-slide-lineage="${slide.number}"><b>本张任务：</b>${esc(slide.proof_goal || '未建立对应')}<br><b>画面证据：</b>${esc(slide.evidence_requirement || '未明确证据要求')}<br><b>表达方式：</b>${esc(slide.visual_method || '未指定')}<div><b>原稿意群：</b>${slideSourceMarkup(slide,context)}</div></div>`;
    return folded ? `<details class="production-candidate-source"><summary>原稿与画面任务</summary>${body}</details>` : body;
  }
  function readSlideSourceLines(value,slide,context) {
    const text = String(value || '').trim();
    if (!text) return [];
    const parts = text.split(/[,，、\s]+/), numbers = parts.map(Number);
    if (parts.some(part => !/^\d+$/.test(part)) || numbers.some(number => !Number.isSafeInteger(number) || number < 1) || new Set(numbers).size !== numbers.length) throw Object.assign(new Error(`第 ${slide} 张的原稿意群须为不重复的正整数编号，用逗号分隔。`),{status:422});
    if (numbers.some((number,index) => index > 0 && number <= numbers[index - 1])) throw Object.assign(new Error(`第 ${slide} 张的意群编号须按原稿顺序填写。`),{status:422});
    const records = Array.isArray(context?.records) ? context.records : [];
    if (records.length && numbers.some(number => !records.some(record => record.number === number))) throw Object.assign(new Error(`第 ${slide} 张引用了原稿快照中不存在的意群。`),{status:422});
    return numbers;
  }
  function readSlideLineage(values,slide,context) {
    const fields = {source_lines:readSlideSourceLines(values.get(`source-lines-${slide.number}`),slide.number,context),
      proof_goal:values.get(`proof-goal-${slide.number}`).trim(),evidence_requirement:values.get(`evidence-requirement-${slide.number}`).trim(),
      visual_method:values.get(`visual-method-${slide.number}`).trim()};
    // Leaving new fields blank on a legacy order must not change its saved image contract.
    return Object.fromEntries(Object.entries(fields).filter(([key,value]) => Object.hasOwn(slide,key) || (Array.isArray(value) ? value.length : value)));
  }
  const composable = slide => (slide.candidates || []).filter(candidate => candidate.origin !== 'composite').length;
  function pickSection(item) {
    const card = (slide,candidate) => `<figure class="pick-card ${slide.base?.id === candidate.id ? 'chosen' : ''}"><a href="${esc(imageURL(item,candidate,'full'))}" target="_blank" rel="noopener" title="看原图"><img src="${esc(imageURL(item,candidate,'thumb'))}" alt="${esc(`第 ${slide.number} 张候选 ${candidate.name}`)}" loading="lazy"></a><figcaption><div class="pick-description"><span>${candidate.origin === 'folder' ? `已有图片 · ${esc(candidate.name)}` : candidate.origin === 'clip' ? `片段取帧 · ${esc(candidate.marker || candidate.timeline || candidate.name)}` : candidate.origin === 'upload' ? `本地上传 · ${esc(candidate.original_name || candidate.name)}` : candidate.origin === 'composite' ? `<em class="pick-ai">AI 合成</em> ${esc(candidate.name || '多图合成')}` : esc([candidate.timeline,candidate.marker].filter(Boolean).join(' · ') || candidate.name)}${candidate.evidence ? ' <em class="pick-evidence">效果证据</em>' : ''}</span>${candidate.reason && !candidate.reason.includes(candidate.original_name || candidate.name || '\u0000') ? `<small>${esc(candidate.reason)}</small>` : ''}</div><button type="button" class="subtle pick-description-toggle" data-pick-description aria-expanded="false">展开说明</button></figcaption>${composable(slide) >= 2 && candidate.origin !== 'composite' ? `<label class="pick-compose-check" title="勾选要合成的几张；不勾则让 AI 从全部候选里挑"><input type="checkbox" data-compose-pick="${slide.number}" value="${esc(candidate.id)}"> 用于合成</label>` : ''}${slide.base?.id === candidate.id ? `<button type="button" class="secondary compact" data-unpick="${slide.number}">取消选用</button>` : `<button type="button" class="primary compact" data-pick="${slide.number}" data-candidate="${esc(candidate.id)}">选为底图</button>`}</figure>`;
    const tile = slide => {
      const all = slide.candidates || [], chosenFirst = [...all.filter(candidate => slide.base?.id === candidate.id),...all.filter(candidate => slide.base?.id !== candidate.id)];
      const shown = chosenFirst.slice(0,PICK_VISIBLE), more = chosenFirst.slice(PICK_VISIBLE);
      return `<div class="pick-slide ${slide.base ? 'has-base' : ''}" data-pick-slide="${slide.number}" data-drop-hint="松开，导入到第 ${slide.number} 张"><div class="pick-head"><div><strong>${pad(slide.number)} ${esc(carouselSlideTitle(slide))}</strong><small class="pick-head-note" title="${esc([slide.role,slide.source ? `素材来源：${slide.source}` : ''].filter(Boolean).join(' · '))}">${esc([slide.role,slide.source ? `素材来源：${slide.source}` : ''].filter(Boolean).join(' · '))}</small></div><span class="pick-state ${slide.base ? 'done' : ''}">${slide.base ? '已选底图' : all.length ? `${all.length} 张候选` : '待选'}</span></div>${slideProofContext(slide,item.script_context)}<div class="pick-tools"><button type="button" class="subtle" data-clip-slide="${slide.number}">从片段取帧${view.clipCounts?.[String(slide.number)] ? `<em class="pick-ai">AI ${view.clipCounts[String(slide.number)]}</em>` : ''}</button><button type="button" class="subtle" data-import-slide="${slide.number}">导入底图</button>${composable(slide) >= 2 && !item.archived ? `<button type="button" class="subtle" data-compose-slide="${slide.number}" title="AI 按逐张表判断这几张怎么合成一张；勾选「用于合成」可指定哪几张">AI 多图合成</button>` : ''}</div>${shown.length ? `<div class="pick-grid">${shown.map(candidate => card(slide,candidate)).join('')}</div>` : '<p class="context-note">还没有候选图。</p>'}${more.length ? `<details class="pick-more"><summary>其余 ${more.length} 张候选</summary><div class="pick-grid">${more.map(candidate => card(slide,candidate)).join('')}</div></details>` : ''}</div>`;
    };
    return `<section class="section-card" id="pick-panel"><h2>逐张选底图</h2><div class="pick-slides">${item.slides.map(tile).join('')}</div></section>`;
  }
  const outputURL = (item,output,size = 'thumb') => `/api/production/carousels/${encodeURIComponent(item.id)}/outputs/${encodeURIComponent(output.id)}?size=${size}`;
  const currentOutput = slide => (slide.outputs || []).find(output => output.id === slide.output_id);
  const generating = item => (item.generations || []).some(generation => generation.status === 'running');
  const imageModes = {generated_visual:'生成视觉版',photo_preserving:'实拍保真版',layout_only:'仅文字排版'};
  const outputApprovalReady = output => Boolean(output && !output.stale && (output.workflow !== 'subject_locked' || subjectPreservationPassed(output)));
  const confirmedOutput = slide => {const output = currentOutput(slide);return Boolean(output?.approved && outputApprovalReady(output));};
  const imageReviewDeferred = output => ['deferred','skipped'].includes(output.review?.status) || (output.review_deferred && !output.review?.status);
  // Tiers for the independent check. clean = a finished review with every check passed; attention = a review that exists but flagged something
  // or never finished; unchecked = no check was run (imports, layout-only edits), so there is no verdict to act on. The check never confirms an image by itself.
  function carouselReviewTier(item) {
    const slides = item.slides || [], running = (item.generations || []).some(run => run.status === 'running');
    const clean = [], attention = [], unchecked = [], waiting = [];
    slides.forEach(slide => {
      const usable = (slide.outputs || []).filter(output => !output.stale), selected = currentOutput(slide);
      const output = selected && !selected.stale ? selected : usable.at(-1);
      if (!output) {if (slide.base) waiting.push(slide.number);else attention.push(slide.number);return;}
      const review = output.review, checks = Array.isArray(review?.checks) ? review.checks : [];
      if (imageReviewDeferred(output)) unchecked.push(slide.number);
      else if (review?.status === 'running' || (review?.status === 'pending' && running)) waiting.push(slide.number);
      else if (!review || !Object.keys(review).length || ['pending','deferred','skipped'].includes(review.status)) unchecked.push(slide.number);
      else if (review.status === 'pass' && review.completed !== false && checks.length && checks.every(check => check.status === 'pass')) clean.push(slide.number);
      else attention.push(slide.number);
    });
    const tier = !slides.length || waiting.length ? 'waiting' : attention.length ? 'attention' : unchecked.length ? 'unchecked' : 'clean';
    return {tier,clean,attention,unchecked,waiting};
  }
  function carouselQuiet(item) {
    const run = carouselAutopilotRun(item);
    return Boolean(item.slides?.length && item.slides.every(confirmedOutput) && !generating(item) &&
      !item.clip_picking_live && !(item.searches || []).some(entry => entry.status === 'running') &&
      (!run || run.status === 'completed') && !item.problems?.length &&
      !item.slides.some(slide => {
        const output = currentOutput(slide), review = output?.review;
        const checks = Array.isArray(review?.checks) ? review.checks : [];
        return review && !imageReviewDeferred(output) &&
          (['needs_fix','review_failed','running','uncertain'].includes(review.status) ||
           checks.some(check => ['fail','uncertain'].includes(check.status)));
      }));
  }
  function refreshCarouselOverview(data) {
    const host = $('#carousel-overview'), item = data.carousel;
    if (!host || host.dataset.carouselId !== item.id) return;
    host.classList.toggle('is-complete',carouselQuiet(item));
    host.innerHTML = item.slides.map(slide => {
      const selected = currentOutput(slide), output = selected && !selected.stale ? selected : [...(slide.outputs || [])].reverse().find(entry => !entry.stale);
      const confirmed = confirmedOutput(slide), phase = confirmed ? 'confirmed' : output ? 'generated' : slide.base ? 'based' : 'empty';
      const label = {confirmed:'已确认 ✓',generated:'有成图',based:'有底图',empty:'无底图'}[phase];
      const source = output ? outputURL(item,output) : slide.base ? imageURL(item,slide.base,'thumb') : '';
      return `<button type="button" class="carousel-overview-tile ${phase}" data-overview-slide="${slide.number}" data-overview-step="${output ? 'images' : 'pick'}" title="第 ${slide.number} 张 · ${esc(label)} · ${esc(carouselSlideTitle(slide))}" aria-label="第 ${slide.number} 张，${esc(label)}"><span class="carousel-overview-image">${source ? `<img src="${esc(source)}" alt="第 ${slide.number} 张" loading="lazy">` : '<span aria-hidden="true">＋</span>'}</span><strong>${pad(slide.number)}</strong><small>${phase === 'confirmed' ? '✓' : esc(label)}</small></button>`;
    }).join('');
    $$('[data-overview-slide]',host).forEach(button => button.onclick = () => {
      const step = button.dataset.overviewStep;
      $(`[data-step="${step}"]`,$('.production-steps'))?.click();
      $(`[data-${step === 'images' ? 'image' : 'pick'}-slide="${button.dataset.overviewSlide}"]`)?.scrollIntoView({block:'center',behavior:'smooth'});
    });
  }
  function mountCarouselOverview(data) {
    const nav = $('.production-steps');if (!nav) return;
    $('#carousel-overview')?.remove();
    const host = document.createElement('nav');host.id = 'carousel-overview';host.className = 'carousel-overview';
    host.dataset.carouselId = data.carousel.id;host.setAttribute('aria-label','整组图片进度');nav.after(host);
    refreshCarouselOverview(data);
  }
  const requirementsFor = (item,choice) => ({mode:'photo_preserving',editable_regions:['background','text'],protected_product:true,protected_evidence:true,hands_authorized:false,style:'',source:'当前逐张底图',instruction:'',postprocess:'layout_only',...(item.requirements || {}),...(choice.requirements || {}),aspect:choice.image_aspect || item.image_aspect || item.requirements?.aspect || '3:4'});
  const regionLabels = {background:'背景',text:'文字',hands:'手部',product:'产品外观'};
  const captionStyles = {yellow_impact:'黄黑冲击',orange_depth:'橙白立体',red_comic:'红白漫画',lime_pop:'荧光青绿'};
  const legacyCaptionStyles = {plain:'普通文字',rugged:'粗犷花字'};
  function captionStylePreview(requirements,prefix,compact = false) {
    const style = requirements.caption_style, label = captionStyles[style];
    return `<span class="carousel-caption-preview${compact ? ' carousel-caption-preview--compact' : ''}" data-caption-preview="${prefix}" ${label ? '' : 'hidden'}><img ${label ? `src="/caption-styles/${style}.png"` : ''} alt="${label ? esc(`${label}花字示例：YOUR TITLE / GOES HERE`) : ''}" decoding="async"><small data-caption-preview-label>${esc(label || '')}</small></span>`;
  }
  const requirementsSummary = requirements => [imageModes[requirements.mode] || requirements.mode,
    captionStyles[requirements.caption_style] || legacyCaptionStyles[requirements.caption_style] || '',
    (requirements.editable_regions || []).length ? `可改${requirements.editable_regions.map(key => regionLabels[key] || key).join('、')}` : '不改画面',
    [requirements.protected_product !== false ? '产品' : '',requirements.protected_evidence !== false ? '实拍证据' : ''].filter(Boolean).join('与').replace(/^(.+)$/,'保护$1'),
    requirements.style ? `风格：${requirements.style.length > 30 ? requirements.style.slice(0,30) + '…' : requirements.style}` : ''].filter(Boolean).join(' · ');
  function requirementsFields(requirements, disabled = false, prefix = 'image') {
    const off = disabled ? 'disabled' : '', regions = requirements.editable_regions || [];
    return `<fieldset class="carousel-requirements"><legend>制作要求</legend><div class="form-row"><label class="field"><span>制作模式</span><select name="${prefix}_mode" ${off}>${Object.entries(imageModes).map(([key,label]) => `<option value="${key}" ${requirements.mode === key ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label class="field"><span>整组风格</span><input name="${prefix}_style" maxlength="2000" value="${esc(requirements.style || '')}" placeholder="例如：粗犷暖光、清楚大字" ${off}></label></div><label class="field carousel-caption-style" data-caption-style="${prefix}"><span>文字样式</span><select name="${prefix}_caption_style" ${off}><option value="" ${!requirements.caption_style ? 'selected' : ''}>不指定（生图按整组风格，只排文字用普通文字）</option><option value="plain" ${requirements.caption_style === 'plain' ? 'selected' : ''}>普通文字</option><option value="rugged" ${requirements.caption_style === 'rugged' ? 'selected' : ''}>粗犷花字</option>${Object.entries(captionStyles).map(([key,label]) => `<option value="${key}" ${requirements.caption_style === key ? 'selected' : ''}>${label}${key === 'yellow_impact' ? '（默认）' : ''}</option>`).join('')}</select>${captionStylePreview(requirements,prefix)}</label><label class="field carousel-caption-layer"><span>文字与主体</span><select name="${prefix}_caption_layer" ${off}><option value="" ${!requirements.caption_layer ? 'selected' : ''}>沿用当前排版</option><option value="behind_subject" ${requirements.caption_layer === 'behind_subject' ? 'selected' : ''}>主体在花字前面</option><option value="front" ${requirements.caption_layer === 'front' ? 'selected' : ''}>文字在最上层</option></select></label><p class="context-note">花字排在底图的文字带或画面空白处，避免遮挡产品、手部和实拍证据。</p><label class="ai-check carousel-overlay-only"><input type="checkbox" name="${prefix}_overlay_only" ${requirements.overlay_only ? 'checked' : ''} ${off}> 只排叠字标题，不排正文</label><label class="field carousel-caption-area"><span>仅文字排版：可用的留白区域</span><select name="${prefix}_caption_area" ${off}><option value="">请先确认图片留白</option><option value="top" ${requirements.caption_area === 'top' ? 'selected' : ''}>顶部留白</option><option value="bottom" ${requirements.caption_area === 'bottom' ? 'selected' : ''}>底部留白</option></select></label><div class="material-options carousel-requirement-options">${[['background','背景'],['text','文字与花字'],['hands','手部'],['product','产品外观']].map(([key,label]) => `<label><input type="checkbox" name="${prefix}_region" value="${key}" ${regions.includes(key) ? 'checked' : ''} ${off}>可改${label}</label>`).join('')}</div><div class="material-options carousel-requirement-options"><label><input type="checkbox" name="${prefix}_protected_product" ${requirements.protected_product !== false ? 'checked' : ''} ${off}>保护产品结构、动作与数量</label><label><input type="checkbox" name="${prefix}_protected_evidence" ${requirements.protected_evidence !== false ? 'checked' : ''} ${off}>保护实拍效果证据</label><label><input type="checkbox" name="${prefix}_hands_authorized" ${requirements.hands_authorized || requirements.hand_edit_allowed ? 'checked' : ''} ${off}>${requirements.authorized_hand_slides?.length ? '其他张手部也已获准改变' : '已授权改变手部'}</label></div><label class="field carousel-hand-look"><span>改成什么样的手（授权改手时使用，会存为本产品默认）</span><input name="${prefix}_hand_look" maxlength="500" value="${esc(requirements.hand_look || '')}" placeholder="留空用默认：美国中年男性的手，手背和小臂有自然汗毛，指节粗大，有使用痕迹" ${off}></label>${requirements.authorized_hand_slides?.length || requirements.no_hand_slides?.length ? `<p class="context-note">逐张手部要求：${requirements.authorized_hand_slides?.length ? `第 ${esc(requirements.authorized_hand_slides.join('、'))} 张已授权改变手部` : ''}${requirements.no_hand_slides?.length ? `；第 ${esc(requirements.no_hand_slides.join('、'))} 张无手部画面` : ''}。</p>` : ''}<label class="field"><span>画面来源说明</span><input name="${prefix}_source" maxlength="2000" value="${esc(requirements.source || '')}" ${off}></label><p class="context-note">生成视觉版按商品参考核对外观、动作与数量；原实拍逐像素保真需要单独的实拍证据。文字与花字排版可复用已有画面，无需生图。首张只参考风格，每张仍使用自己的产品参考。</p></fieldset>`;
  }
  function requirementsFromForm(form,item,choice,prefix = 'image') {
    const value = name => form.elements.namedItem(`${prefix}_${name}`);
    const previous = requirementsFor(item,choice);
    const requirements = {...previous,mode:value('mode').value,style:value('style').value.trim(),source:value('source').value.trim(),editable_regions:$$(`input[name="${prefix}_region"]:checked`,form).map(input => input.value),protected_product:value('protected_product').checked ? previous.protected_product || true : false,protected_evidence:value('protected_evidence').checked ? previous.protected_evidence || true : false,hands_authorized:value('hands_authorized').checked,aspect:form.elements.namedItem('aspect')?.value || previous.aspect,instruction:form.elements.namedItem('instruction')?.value.trim() || '',postprocess:'layout_only'};
    if (value('caption_area')?.value) requirements.caption_area = value('caption_area').value;else delete requirements.caption_area;
    // Both keys stay absent unless chosen, so groups made before them keep their signatures.
    if (value('caption_style')?.value) requirements.caption_style = value('caption_style').value;else delete requirements.caption_style;
    if (value('overlay_only')?.checked) requirements.overlay_only = true;else delete requirements.overlay_only;
    if (value('caption_layer')?.value) requirements.caption_layer = value('caption_layer').value;else delete requirements.caption_layer;
    if (value('hand_look')?.value.trim()) requirements.hand_look = value('hand_look').value.trim();else delete requirements.hand_look;
    if (requirements.mode === 'layout_only') {
      requirements.workflow = 'review_first';requirements.editable_regions = ['text'];requirements.hands_authorized = false;
      requirements.protected_product = true;requirements.protected_evidence = true;requirements.preserve_slides = [];
      delete requirements.authorized_hand_slides;delete requirements.hand_look;
    }
    return requirements;
  }
  function bindRequirementsMode(form,prefix = 'image') {
    const captionSelect = form.elements.namedItem(`${prefix}_caption_style`);
    const updateCaptionPreview = () => {
      const style = captionSelect?.value, label = captionStyles[style];
      $$(`[data-caption-preview="${prefix}"]`,form).forEach(preview => {
        preview.hidden = !label;
        const image = $('img',preview), name = $('[data-caption-preview-label]',preview);
        if (label) {
          image.src = `/caption-styles/${style}.png`;
          image.alt = `${label}花字示例：YOUR TITLE / GOES HERE`;
        } else {image.removeAttribute('src');image.alt = '';}
        name.textContent = label || '';
      });
    };
    captionSelect?.addEventListener('change',updateCaptionPreview);updateCaptionPreview();
    const update = () => {
      const layoutOnly = form.elements.namedItem(`${prefix}_mode`).value === 'layout_only';
      const styleLabel = form.elements.namedItem(`${prefix}_style`)?.closest('label')?.querySelector('span');
      if (styleLabel) styleLabel.textContent = layoutOnly ? '整组场景风格（生成时适用）' : '整组风格';
    };
    form.elements.namedItem(`${prefix}_mode`).addEventListener('change',update);update();
  }
  async function carouselMutation(data,submit) {
    try {return await submit();}
    catch (error) {
      if (error.status !== 409) throw error;
      try {data.revision = (await api(`/api/production/carousels/${encodeURIComponent(data.carousel.id)}`)).revision;}catch {}
      throw Object.assign(new Error(`资料已更新，输入已保留，检查后重试。${fail(error)}`),{status:409});
    }
  }
  function imageRunGroups(item) {
    const runs = item.generations || [], active = [], history = [];
    const incomplete = new Set(['awaiting_first_approval','interrupted','cancelled','failed']);
    [...runs].reverse().forEach(run => {
      const numbers = [...new Set([...(run.requested_slides || []),...(run.slides || []),...(run.remaining_slides || [])].map(Number))];
      const unresolved = numbers.some(number => {
        const slide = item.slides.find(slide => slide.number === number);
        if (!slide) return false;
        const latest = [...(slide.outputs || [])].reverse().find(output => !output.stale);
        if (!latest) return true;
        if (latest.generation_id === run.id) return !confirmedOutput(slide);
        const newerRun = runs.findIndex(entry => entry.id === latest.generation_id);
        return !(newerRun > runs.indexOf(run) || (latest.created_at && run.started_at && latest.created_at > run.started_at));
      });
      (run.status === 'running' || (incomplete.has(run.status) && unresolved) ? active : history).push(run);
    });
    return {active,history};
  }
  function imageRun(generation, historical = false) {
    const labels = {awaiting_first_approval:'首张待确认',...editLabels};
    const scope = generation.requested_slides || generation.slides || [];
    const detail = [generation.started_at ? readableDate(generation.started_at) : '',scope.length ? `第 ${scope.join('、')} 张` : '',generation.added ? `保存 ${generation.added} 张` : ''].filter(Boolean).join(' · ');
    return `<article class="image-run ${esc(generation.status)}" data-generation-id="${esc(generation.id)}"><div class="take-head"><strong><span class="script-status">${esc(labels[generation.status] || generation.status)}</span></strong>${!historical && generation.status === 'running' ? `<button type="button" class="secondary compact" data-cancel-generation="${esc(generation.id)}">停止出图</button>` : ''}</div>${detail ? `<small class="image-run-detail">${esc(detail)}</small>` : ''}${generation.status === 'running' ? runProgressMarkup(generation,'image') : ''}${generation.error ? `<p class="ai-error">${esc(generation.error)}</p>` : ''}</article>`;
  }
  function renderImageRuns(item, historyOpen = false) {
    const {active,history} = imageRunGroups(item);
    return `${active.map(run => imageRun(run)).join('')}${history.length ? `<details class="image-run-history edit-older" ${historyOpen ? 'open' : ''}><summary>以前的出图记录（${history.length} 次）</summary><div>${history.map(run => imageRun(run,true)).join('')}</div></details>` : ''}`;
  }
  const carouselPhases = [['prepare','准备','preparation'],['generation','生成','generate'],['layout','排版'],['review','核对'],['confirmation','确认','confirm'],['package','打包','packaging']];
  const carouselStageLabels = {pending:'待处理',running:'进行中',completed:'已完成',skipped:'未执行',failed:'失败',cancelled:'已停止',interrupted:'已中断',unknown:'未记录'};
  function carouselImageStages(item,slide,output) {
    const runs = item.generations || [];
    const running = [...runs].reverse().find(run => run.status === 'running' && (run.slides || []).includes(slide.number));
    const currentRun = running || imageRunGroups(item).active.find(run => run.stages?.[String(slide.number)]);
    const run = currentRun || runs.find(run => run.id === output?.generation_id) || (!output ? [...runs].reverse().find(run => run.stages?.[String(slide.number)]) : null);
    const stages = {...(run?.stages?.[String(slide.number)] || {}),...(!currentRun || currentRun.id === output?.generation_id ? output?.stages || {} : {})};
    if (!Object.keys(stages).length) return '';
    const confirmed = output?.id === slide.output_id && confirmedOutput(slide);
    const markup = `<ol class="carousel-stage-strip" aria-label="第 ${slide.number} 张制作阶段"${run ? ` data-stage-generation="${esc(run.id)}"` : ''}>${carouselPhases.map(([key,label,alias]) => {
      const phase = stages[key] || stages[alias] || {};
      let status = Object.hasOwn(carouselStageLabels,phase.status) ? phase.status : 'unknown';
      const invalidated = !running && output?.stale && ['confirmation','package'].includes(key);
      if (invalidated) status = 'pending';
      const note = invalidated ? '当前图片已失效，需重新确认。' : key === 'review' && status === 'skipped' && confirmed ? '已确认选用；本次未执行自动核对。' : phase.note || (key === 'review' && status === 'skipped' ? '本次没有执行自动核对，请人工核对成图。' : '');
      const explanation = [key === 'confirmation' ? '人工确认' : label,carouselStageLabels[status],note,phase.finished_at ? `结束于 ${readableDate(phase.finished_at)}` : ''].filter(Boolean).join('：');
      return `<li class="${status}" data-image-phase="${key}" data-phase-status="${status}" title="${esc(explanation)}" tabindex="0" aria-label="${esc(explanation)}"><span>${label}</span><small>${carouselStageLabels[status]}</small></li>`;
    }).join('')}</ol>`;
    return confirmed && !running && carouselQuiet(item) && !Object.values(stages).some(phase => ['failed','interrupted','running','pending'].includes(phase?.status))
      ? `<details class="carousel-stage-history"><summary>制作阶段记录</summary>${markup}</details>` : markup;
  }
  function imageReview(output,confirmed = false) {
    const review = output.review;
    const subject = output.workflow === 'subject_locked' ? `<div class="image-review-empty" data-subject-preservation="${subjectPreservationPassed(output) ? 'passed' : 'unverified'}">${subjectPreservationPassed(output) ? '主体像素已保留' : '尚无主体像素保留的通过回执'}</div>` : '';
    if (imageReviewDeferred(output)) return {html:`${subject}<div class="image-review-empty">${confirmed ? '已确认选用 · 未执行自动核对' : output.workflow === 'subject_locked' ? '文字、证据与排版暂未自动核对' : '暂未自动核对（核对暂缓） · 可在出图后按需核对'}</div>`,warning:false};
    if (!review || !Object.keys(review).length) return {html:`${subject}<div class="image-review-empty">${confirmed ? '已确认选用 · 无自动核对记录' : '无自动核对记录，请人工核对'}</div>`,warning:false};
    const checks = Array.isArray(review.checks) ? review.checks : [];
    const warning = review.status === 'needs_fix' || checks.some(check => check.status === 'fail');
    const statuses = {pass:'通过',fail:'需修正',uncertain:'待人工核对',pending:'待核对',running:'核对中',review_failed:'未完成'};
    const overall = statuses[review.status] || (review.status === 'needs_fix' ? '需修正' : '待人工核对');
    const chips = [['product','主体',['subject']],['evidence','证据',[]],['text','文字',[]],['visual','排版',['layout','edge','edges']]].map(([key,label,aliases]) => {
      const check = checks.find(check => check.id === key || aliases.includes(check.id));
      const status = ['pass','fail','uncertain'].includes(check?.status) ? check.status : ['pending','running'].includes(review.status) ? review.status : 'uncertain';
      const note = check?.note || review.summary || (review.issues || []).join('；') || '没有取得这一项的核对结论，请人工核对。';
      const explanation = `${check?.label || label}：${statuses[status]}。${note}`;
      return `<span class="image-review-chip ${status}" data-review-check="${key}" data-review-status="${status}" title="${esc(explanation)}" tabindex="0" aria-label="${esc(explanation)}"><span>${label}</span><small>${statuses[status]}</small></span>`;
    }).join('');
    return {warning,html:`${subject}<div class="image-review ${warning ? 'needs-fix' : ''}" aria-label="自动核对结果"><div class="image-review-heading"><span>自动核对</span><strong>${esc(overall)}</strong></div><div class="image-review-checks">${chips}</div></div>`};
  }
  function setImageVersionsExpanded(stack, expanded) {
    const others = $('.image-version-others',stack), toggle = $('[data-image-versions-toggle]',stack);
    if (!others || !toggle) return;
    others.hidden = !expanded;stack.classList.toggle('expanded',expanded);
    toggle.setAttribute('aria-expanded',String(expanded));
    toggle.textContent = expanded ? '收起' : `查看其他图片（${toggle.dataset.imageCount}）`;
    const choice = view.choice[stack.dataset.carouselId] ||= {};
    (choice.image_versions_open ||= {})[stack.dataset.imageStack] = expanded;
  }
  // 一键全部选用 confirms the version each card shows in front: the newest usable one, unless one is already confirmed.
  const pendingPick = slide => {
    const current = currentOutput(slide);
    if (current?.approved && outputApprovalReady(current)) return null;
    const latest = [...(slide.outputs || [])].reverse().find(output => !output.stale);
    return outputApprovalReady(latest) ? latest : null;
  };
  const usableImage = slide => [...(slide.outputs || [])].reverse().find(output => !output.stale);
  function selectedImageSlides(item,choice) {
    choice.redo_picks ??= choice.image_slides || item.slides.filter(slide => slide.base && !usableImage(slide)).map(slide => slide.number);
    return choice.redo_picks.filter(number => item.slides.some(slide => slide.number === number && (slide.base || usableImage(slide))));
  }
  function imageComparisonBase(slide,output) {
    return output.base_id ? (slide.candidates || []).find(candidate => candidate.id === output.base_id) || (slide.base?.id === output.base_id ? slide.base : null) : slide.base;
  }
  function imageComparisonSource(item,slide,output) {
    const base = imageComparisonBase(slide,output);
    return base ? `<span>底图</span><a href="${esc(imageURL(item,base,'full'))}" target="_blank" rel="noopener" title="查看底图原图"><img src="${esc(imageURL(item,base,'thumb'))}" alt="第 ${slide.number} 张底图"></a>` : '';
  }
  function setImageComparison(item,slide,output,card,expanded) {
    const base = $('.image-compare-base',card), label = $('.image-compare-result-label',card), toggle = $('[data-image-compare]',card);
    if (!base || !label || !toggle || !imageComparisonBase(slide,output)) return;
    if (expanded && !base.children.length) base.innerHTML = imageComparisonSource(item,slide,output);
    base.hidden = !expanded;label.hidden = !expanded;card.classList.toggle('comparing',expanded);
    toggle.setAttribute('aria-expanded',String(expanded));toggle.textContent = expanded ? '收起对比' : '对比底图';
    const choice = view.choice[item.id] ||= {};(choice.image_compare_open ||= {})[output.id] = expanded;
  }
  function imageSlides(item) {
    const busy = generating(item), choice = view.choice[item.id] ||= {};
    const picked = selectedImageSlides(item,choice), tiers = carouselReviewTier(item);
    return item.slides.map(slide => {
      const outputs = slide.outputs || [], disabled = item.archived || busy;
      const slideTier = tiers.clean.includes(slide.number) ? 'clean' : tiers.attention.includes(slide.number) ? 'attention' : tiers.unchecked.includes(slide.number) ? 'unchecked' : 'waiting';
      const latest = [...outputs].reverse(), selectedOutput = currentOutput(slide);
      const front = confirmedOutput(slide) ? selectedOutput : latest.find(output => !output.stale) || latest[0];
      // Invalidated versions can no longer be chosen, so they neither count as other images nor sit among them.
      const others = latest.filter(output => output !== front && !output.stale), stale = latest.filter(output => output !== front && output.stale), expanded = Boolean(choice.image_versions_open?.[slide.number]);
      const otherId = `image-versions-${item.id}-${slide.number}`;
      const card = output => {
        const selected = output.id === slide.output_id && confirmedOutput(slide), version = outputs.indexOf(output) + 1;
        const review = imageReview(output,selected);
        const base = imageComparisonBase(slide,output), comparing = Boolean(base && choice.image_compare_open?.[output.id]);
        return `<figure class="carousel-output ${selected ? 'current' : ''} ${output.stale ? 'stale' : ''} ${review.warning ? 'review-needs-fix' : ''} ${comparing ? 'comparing' : ''}" data-output-id="${esc(output.id)}"${output.generation_id ? ` data-generation-result="${esc(output.generation_id)}"` : ''}><div class="image-compare-base" ${comparing ? '' : 'hidden'}>${comparing ? imageComparisonSource(item,slide,output) : ''}</div><span class="image-compare-result-label" ${comparing ? '' : 'hidden'}>成图</span><a href="${esc(outputURL(item,output,'full'))}" target="_blank" rel="noopener" title="查看成图原图" aria-label="查看第 ${slide.number} 张成图第 ${version} 版原图"><img src="${esc(outputURL(item,output))}" data-aspect="${output.aspect === '9:16' ? '9:16' : '3:4'}" alt="第 ${slide.number} 张成图第 ${version} 版"></a><button type="button" class="subtle compact image-compare-toggle" data-image-compare="${esc(output.id)}" data-output-slide="${slide.number}" aria-expanded="${comparing}" ${base ? '' : 'disabled title="没有可用的底图，无法对比"'}>${base ? comparing ? '收起对比' : '对比底图' : '无底图可对比'}</button>${review.html}<div class="image-version-actions"><a class="secondary compact" download href="${esc(outputURL(item,output,'full'))}" data-download-output="${esc(output.id)}">下载图片</a><button type="button" class="${selected ? 'secondary' : 'primary'} compact" ${selected ? `data-revoke-output="${esc(output.id)}"` : output.approved ? `data-select-output="${esc(output.id)}" data-approve-output="${esc(output.id)}"` : `data-approve-output="${esc(output.id)}"`} data-output-slide="${slide.number}" ${disabled || !outputApprovalReady(output) ? 'disabled' : ''}>${output.stale ? '此图已失效' : !outputApprovalReady(output) ? '需重新制作' : selected ? '撤销确认' : '确认选用'}</button></div></figure>`;
      };
      const redo = `<label class="image-slide-pick"><input type="checkbox" data-redo-pick="${slide.number}" aria-label="选择第 ${slide.number} 张" ${picked.includes(slide.number) ? 'checked' : ''} ${disabled || (!slide.base && !usableImage(slide)) ? 'disabled' : ''}>加入批量重做</label><button type="button" class="secondary compact" data-redo-slide="${slide.number}" ${disabled || !slide.base ? 'disabled' : ''}>${outputs.length ? '重做这一张' : '生成这一张'}</button>`;
      return `<article class="image-slide" data-image-slide="${slide.number}" data-review-tier="${slideTier}" tabindex="-1"><div class="pick-head"><h3>${pad(slide.number)} ${esc(carouselSlideTitle(slide))}</h3>${carouselQuiet(item) ? `<details class="image-redo-controls"><summary>重做与选择</summary><div>${redo}</div></details>` : `<div class="image-redo-actions">${redo}</div>`}</div>${slideProofContext(slide,item.script_context,true)}<div data-image-stages="${slide.number}">${carouselImageStages(item,slide,front)}</div>${front ? `<div class="image-version-stack ${expanded ? 'expanded' : ''}" data-image-stack="${slide.number}" data-carousel-id="${esc(item.id)}"><div class="image-stack-cover ${others.length ? 'stacked' : ''} ${others.length > 1 ? 'has-more' : ''}">${card(front)}</div>${others.length ? `<button type="button" class="subtle compact image-versions-toggle" data-image-versions-toggle="${slide.number}" data-image-count="${others.length}" aria-expanded="${expanded}" aria-controls="${esc(otherId)}">${expanded ? '收起' : `查看其他图片（${others.length}）`}</button><div class="image-version-others image-version-grid" id="${esc(otherId)}" ${expanded ? '' : 'hidden'}>${others.map(card).join('')}</div>` : ''}${stale.length ? `<details class="image-version-stale"><summary>已失效 ${stale.length} 版</summary><div class="image-version-grid">${stale.map(card).join('')}</div></details>` : ''}</div>` : '<p class="context-note">还没有成图。</p>'}</article>`;
    }).join('');
  }
  function imagesSection(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, runs = item.generations || [];
    const aspect = choice.image_aspect || item.image_aspect || runs.at(-1)?.aspect || '3:4';
    const continuation = imageRunGroups(item).active.some(run => ['awaiting_first_approval','interrupted','cancelled','failed'].includes(run.status)) && item.slides.some(slide => slide.base && !confirmedOutput(slide));
    return `<section class="section-card" id="carousel-images-panel"><div class="section-title-row"><h2>出图${infoTip('<p>选中的图同时生成，每次都出新版本，旧版本保留；「继续未完成的图」只补上次没成功的张。连接中断或限流会自动重试。首张已确认时，其余张会参考它的风格。后台处理时可以离开本页。</p><p>快捷键：J / K 切换张 · Enter 确认 · R 重做 · 大图里 ← / → 切换。</p>')}</h2><div class="carousel-image-heading-actions"><small>${item.slides.filter(confirmedOutput).length} / ${item.slides.length} 张已确认</small><button type="button" class="secondary compact" id="carousel-import-finished" ${item.archived || generating(item) ? 'disabled' : ''}>导入现有成品</button></div></div>
      <p class="production-resolve" id="carousel-images-service" role="status">正在检查出图服务…</p>
      <form id="carousel-image-form"><details class="carousel-requirements-toggle" id="carousel-requirements-toggle" ${choice.requirements_open ? 'open' : ''}><summary><strong>出图设置</strong><small>${esc(requirementsSummary(requirementsFor(item,choice)))}</small>${captionStylePreview(requirementsFor(item,choice),'image',true)}<span>修改</span></summary>${requirementsFields(requirementsFor(item,choice),item.archived || generating(item))}<div class="take-actions carousel-requirements-save"><button type="button" class="secondary compact" id="carousel-save-requirements" ${item.archived || generating(item) ? 'disabled' : ''}>保存制作要求</button><span>保存后，这个产品新建的图文单也默认沿用。</span></div></details>
      <div class="form-row image-aspect-row"><label class="field"><span>成图比例</span><select name="aspect" ${item.archived || generating(item) ? 'disabled' : ''}><option value="3:4" ${aspect === '3:4' ? 'selected' : ''}>3:4</option><option value="9:16" ${aspect === '9:16' ? 'selected' : ''}>9:16</option></select></label><div></div></div>
      <label class="field"><span>本次额外要求（可选）</span><textarea name="instruction" rows="2" maxlength="2000" placeholder="例如：背景更简洁，产品主体放大，保留完整夹具与磨板">${esc(choice.image_instruction ?? item.requirements?.instruction ?? '')}</textarea></label>
      ${continuation ? `<div class="take-actions"><button type="button" class="secondary" id="carousel-continue-images" disabled>继续未完成的图</button></div>` : ''}<p class="ai-error" data-error role="alert" hidden></p></form>
      <div id="carousel-image-runs">${renderImageRuns(item)}</div><div class="image-tier" id="carousel-tier" role="status" hidden></div><div id="carousel-image-slides"${carouselQuiet(item) ? ' class="is-complete"' : ''}>${imageSlides(item)}</div><div class="image-bulk-actions" id="carousel-bulk-actions"><button type="submit" form="carousel-image-form" class="primary compact" id="carousel-generate-picked" disabled>生成选中的图</button><button type="button" class="primary compact" id="carousel-approve-all" disabled>一键全部选用</button><button type="button" class="secondary compact" id="carousel-redo-picked" disabled>重做选中的张</button><button type="button" class="subtle compact" id="carousel-pick-all">全选</button><small id="carousel-bulk-note"></small></div></section>`;
  }
  function bindImages(data,{partial = false} = {}) {
    if (partial) {if (view.imagesBinding?.id === data.carousel.id && view.imagesBinding.sequence === view.sequence) view.imagesBinding.refresh(data);return;}
    let item = data.carousel;
    const seq = view.sequence, choice = view.choice[item.id] ||= {}, form = $('#carousel-image-form');
    let service = null;
    const remember = () => {
      choice.image_aspect = form.aspect.value;choice.image_instruction = form.instruction.value;
      choice.requirements = requirementsFromForm(form,item,choice);
    };
    form.addEventListener('input',remember);form.addEventListener('change',remember);
    bindRequirementsMode(form);
    form.instruction.maxLength = 2000;
    const aspectNote = document.createElement('p');aspectNote.id = 'carousel-image-aspect-note';aspectNote.className = 'context-note image-aspect-note';
    form.querySelector('.image-aspect-row').insertAdjacentElement('afterend',aspectNote);
    const updateAspectNote = () => {
      const current = item.image_aspect || '3:4', changed = form.aspect.value !== current && item.slides.some(slide => (slide.outputs || []).length);
      aspectNote.hidden = !changed;aspectNote.textContent = changed ? `这组当前采用 ${current}。启动 ${form.aspect.value} 出图后，旧比例版本将失效，整组都需按新比例重新出图。` : '';
    };
    form.aspect.addEventListener('change',updateAspectNote);updateAspectNote();
    const updateSubmit = () => {
      const unavailable = !service?.available || generating(item) || item.archived;
      const layoutOnly = form.elements.namedItem('image_mode').value === 'layout_only';
      const blocked = item.archived || generating(item) || (!layoutOnly && unavailable);
      const submit = $('#carousel-generate-picked'), missing = selectedImageSlides(item,choice).filter(number => {const slide = item.slides.find(slide => slide.number === number);return slide?.base && !usableImage(slide);});
      if (submit) {submit.disabled = blocked || !missing.length;submit.hidden = !missing.length;submit.textContent = `${layoutOnly ? '排版' : '生成'} ${missing.length} 张`;}
      const continueButton = $('#carousel-continue-images');if (continueButton) continueButton.disabled = blocked;
      $$('[data-redo-slide]').forEach(button => {button.disabled = blocked || !item.slides.find(slide => slide.number === Number(button.dataset.redoSlide))?.base;});
    };
    updateSubmit();
    form.addEventListener('change',updateSubmit);
    readStatus('/api/production/codex-images',10000,true).then(status => {
      if (seq !== view.sequence || !form.isConnected) return;
      service = status;const node = $('#carousel-images-service');node.classList.toggle('problem',!status.available);node.hidden = Boolean(status.available);
      node.textContent = status.available ? `${status.label || '图生图服务'}已就绪。` : status.error || '图生图服务暂不可用。';
      updateSubmit();updateBulk();
    }).catch(error => {if (seq === view.sequence && form.isConnected) {$('#carousel-images-service').hidden = false;$('#carousel-images-service').textContent = `暂时无法检查出图服务：${fail(error)}`;}});
    form.onsubmit = async event => {
      event.preventDefault();remember();setError(form,'');
      const slides = selectedImageSlides(item,choice).filter(number => {const slide = item.slides.find(slide => slide.number === number);return slide?.base && !usableImage(slide);});
      if (!slides.length) {setError(form,'请在卡片上勾选至少一张待出图的图。');return;}
      const submit = $('#carousel-generate-picked');submit.disabled = true;submit.textContent = '正在启动后台出图…';
      // A fresh click always makes new versions; only 继续未完成的图 reuses images an earlier run already saved.
      try {await send(`generate-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/generate`,{revision:data.revision,slides,aspect:choice.image_aspect,instruction:choice.image_instruction.trim(),requirements:choice.requirements,redo:true});delete choice.image_slides;choice.redo_picks = [];toast('已开始生成图片，完成后可直接确认选用。');await route(true);}
      catch (error) {setError(form,fail(error));submit.textContent = '生成选中的图';updateSubmit();}
    };
    $('#carousel-save-requirements').onclick = async event => {
      remember();const button = event.currentTarget;button.disabled = true;setError(form,'');
      try {await send(`requirements-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/requirements`,{revision:data.revision,requirements:choice.requirements});toast('制作要求已保存。');await route(true);}
      catch (error) {setError(form,fail(error));button.disabled = false;}
    };
    $('#carousel-import-finished').onclick = () => {
      const draft = choice.import_finished || {directory:'',model:'',requirements:{...requirementsFor(item,choice),mode:'layout_only',editable_regions:[],source:'外部完成的生成／排版结果',hands_authorized:false}};
      const modal = openModal('导入现有成品',`<p class="context-note">选择「图文成品」里的整组目录。工作台会复制并检查图片、顺序、比例、锁定文案与来源记录；PNG 和 JPG 发布副本只算同一张。导入后可直接选用。</p><label class="field"><span>成品目录</span><input name="directory" required maxlength="2000" value="${esc(draft.directory || '')}" placeholder="例如：C:/Users/video/Desktop/case-workbench/图文成品/产品方向-日期-版本"></label>${requirementsFields(draft.requirements,false,'import')}<div class="form-row"><label class="field"><span>图片比例</span><select name="aspect"><option value="3:4" ${draft.requirements.aspect !== '9:16' ? 'selected' : ''}>3:4</option><option value="9:16" ${draft.requirements.aspect === '9:16' ? 'selected' : ''}>9:16</option></select></label><label class="field"><span>实际使用的模型（未知可留空）</span><input name="model" maxlength="200" value="${esc(draft.model || '')}" placeholder="未知"></label></div><p class="context-note">来源登记为外部完成的生成／排版结果；没有实际模型信息时保持未知。登记不会生成图片，也不会自动声明人工已确认。</p>`,'导入图片',async modal => {
        const requirements = requirementsFromForm(modal.form,item,choice,'import'), directory = modal.form.directory.value.trim(), model = modal.form.model.value.trim() || null;
        await once(modal,'import-finished',`/api/production/carousels/${encodeURIComponent(item.id)}/import-finished`,async () => ({revision:data.revision,directory,requirements,source:'external_generated_layout',model}));
        delete choice.import_finished;delete choice.requirements;delete choice.image_slides;delete choice.image_aspect;closeModal(true);toast('图片已导入，已确认版本继续保留。');await route(true);
      });
      const rememberImport = () => {choice.import_finished = {directory:modal.form.directory.value,model:modal.form.model.value,requirements:requirementsFromForm(modal.form,item,choice,'import')};};
      bindRequirementsMode(modal.form,'import');
      modal.form.addEventListener('input',rememberImport);modal.form.addEventListener('change',rememberImport);
    };
    const startSubset = async (button,slides) => {
      remember();button.disabled = true;setError(form,'');
      try {await send(`generate-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/generate`,{revision:data.revision,slides,aspect:choice.image_aspect,instruction:choice.image_instruction.trim(),requirements:choice.requirements});delete choice.image_slides;toast('已继续生成图片。');await route(true);}
      catch (error) {setError(form,fail(error));updateSubmit();}
    };
    $('#carousel-continue-images')?.addEventListener('click',event => startSubset(event.currentTarget,item.slides.filter(slide => slide.base && (!currentOutput(slide)?.approved || currentOutput(slide)?.stale)).map(slide => slide.number)));
    // Once the whole group is confirmed the package is made right away; if that fails the button in 领取与发布 still works.
    const packIfComplete = async () => {
      try {
        const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`), group = fresh.carousel;
        if (!group.archived && !generating(group) && group.slides.length && group.slides.every(entry => currentOutput(entry)?.approved && !currentOutput(entry)?.stale)) {
          const result = await send(`delivery-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/delivery`,{revision:fresh.revision});rememberDeliveryCleanup(item.id,result);return true;
        }
      } catch {}
      return false;
    };
    const approve = async (slide,output,approved,note = '',acknowledge = false,actor = 'human') => {
      await carouselMutation(data,() => send(`approval-${item.id}-${slide}`,`/api/production/carousels/${encodeURIComponent(item.id)}/approval`,{revision:data.revision,slide,output_id:output.id,approved,note,acknowledge,actor}));
      delete choice.image_slides;
      const packed = approved && await packIfComplete();
      toast(!approved ? `第 ${slide} 张已撤销确认。` : packed ? `第 ${slide} 张已确认选用。整组都已确认，成品包已自动整理好，可在「领取与发布」下载。` : `第 ${slide} 张已确认选用。`);await refreshProductionDetail(data,'carousel',{scope:'images'});
    };
    const blockedBulk = () => item.archived || generating(item);
    const picked = () => selectedImageSlides(item,choice);
    const redoPicks = () => picked().filter(number => {const slide = item.slides.find(slide => slide.number === number);return slide?.base && usableImage(slide);});
    // With nothing ticked the one-click button skips slides whose check flagged something; those need an explicit tick or their own button.
    const confirmPicks = () => {
      const tiers = carouselReviewTier(item);
      return item.slides.filter(slide => (picked().length ? picked().includes(slide.number) : !tiers.attention.includes(slide.number)) && pendingPick(slide));
    };
    const updateBulk = () => {
      const pending = confirmPicks(), selected = picked(), redo = redoPicks();
      const approveAll = $('#carousel-approve-all'), redoPicked = $('#carousel-redo-picked'), pickAll = $('#carousel-pick-all');
      const tiers = carouselReviewTier(item), tierNode = $('#carousel-tier');
      const unconfirmed = number => !confirmedOutput(item.slides.find(slide => slide.number === number));
      const hints = tiers.attention.filter(unconfirmed), allClean = tiers.tier === 'clean' && item.slides.some(slide => !confirmedOutput(slide));
      if (tierNode) {
        tierNode.hidden = !(hints.length || allClean);tierNode.dataset.tier = hints.length ? 'attention' : allClean ? 'clean' : '';
        tierNode.textContent = hints.length ? `第 ${hints.join('、')} 张的自动核对有提示，需要你查看后再确认；${tiers.clean.filter(unconfirmed).length ? '其余核对全通过的张可以一键确认。' : ''}` : allClean ? '自动核对全部通过：扫一眼整组图，没问题就一键确认。自动核对只是建议，产品细节以你看到的为准。' : '';
      }
      approveAll.disabled = blockedBulk() || !pending.length;
      approveAll.textContent = pending.length ? selected.length ? `确认 ${pending.length} 张` : hints.length ? `确认核对通过的（${pending.length} 张）` : `一键全部选用（${pending.length} 张）` : item.slides.some(slide => {const output = usableImage(slide);return output?.workflow === 'subject_locked' && !subjectPreservationPassed(output);}) ? '有图片需重新制作' : hints.length ? '其余张需逐张确认' : '已全部选用';
      const layoutOnly = form.elements.namedItem('image_mode').value === 'layout_only';
      redoPicked.disabled = blockedBulk() || (!layoutOnly && !service?.available) || !redo.length;
      redoPicked.hidden = !redo.length;redoPicked.textContent = `重做 ${redo.length} 张`;
      const pickable = item.slides.filter(slide => slide.base || usableImage(slide)).map(slide => slide.number);
      pickAll.disabled = blockedBulk() || !pickable.length;
      pickAll.textContent = pickable.length && pickable.every(number => selected.includes(number)) ? '取消全选' : '全选';
      const missing = selected.filter(number => !usableImage(item.slides.find(slide => slide.number === number))).length;
      $('#carousel-bulk-note').textContent = selected.length ? `已选 ${selected.length} 张：待出图 ${missing} 张，待确认 ${pending.length} 张。确认当前显示在最前面的版本。` : '';
      updateSubmit();
    };
    form.addEventListener('change',updateBulk);
    const markCurrent = number => {
      choice.image_current_slide = number;
      $$('[data-image-slide]').forEach(card => card.classList.toggle('keyboard-current',Number(card.dataset.imageSlide) === number));
    };
    const bindCards = () => {
    $$('[data-image-versions-toggle]').forEach(button => button.onclick = () => setImageVersionsExpanded(button.closest('.image-version-stack'),button.getAttribute('aria-expanded') !== 'true'));
    $$('[data-image-compare]').forEach(button => button.onclick = () => {
      const slide = item.slides.find(slide => slide.number === Number(button.dataset.outputSlide));
      const output = (slide?.outputs || []).find(output => output.id === button.dataset.imageCompare);
      if (output) setImageComparison(item,slide,output,button.closest('.carousel-output'),button.getAttribute('aria-expanded') !== 'true');
    });
    $$('[data-redo-pick]').forEach(input => input.onchange = () => {
      const number = Number(input.dataset.redoPick), current = new Set(picked());
      if (input.checked) current.add(number);else current.delete(number);
      choice.redo_picks = [...current].sort((a,b) => a - b);updateBulk();
    });
    $('#carousel-pick-all').onclick = () => {
      const pickable = item.slides.filter(slide => slide.base || usableImage(slide)).map(slide => slide.number), all = pickable.every(number => picked().includes(number));
      choice.redo_picks = all ? [] : pickable;
      $$('[data-redo-pick]').forEach(input => {if (!input.disabled) input.checked = !all;});updateBulk();
    };
    $('#carousel-approve-all').onclick = async event => {
      const button = event.currentTarget, selections = confirmPicks().map(slide => ({slide:slide.number,output_id:pendingPick(slide).id}));
      if (!selections.length) return;
      button.disabled = true;button.textContent = '正在确认…';
      try {
        await carouselMutation(data,() => send(`approve-all-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/approve-all`,{revision:data.revision,selections}));
        delete choice.image_slides;
        const packed = await packIfComplete();
        toast(packed ? `已确认 ${selections.length} 张。整组都已确认，成品包已自动整理好，可在「领取与发布」下载。` : `已确认 ${selections.length} 张。`);await refreshProductionDetail(data,'carousel',{scope:'images'});
      } catch (error) {toast(fail(error),true);updateBulk();}
    };
    $('#carousel-redo-picked').onclick = () => {
      const slides = redoPicks();if (!slides.length) return;
      const modal = openModal(`重做第 ${slides.join('、')} 张`,`<p class="context-note">选中的 ${slides.length} 张同时重做，各自从当前选用的原底图出图，旧成图都会保留。</p><label class="field"><span>重做要求（对选中的每张都生效）</span><textarea name="instruction" rows="3" maxlength="2000" placeholder="例如：纠正英文文字，把主体放大，减少背景干扰">${esc(choice.image_redo_many ?? choice.image_instruction ?? '')}</textarea></label>`,'开始重做',async modal => {
        remember();const requirements = {...choice.requirements};
        await once(modal,'generate',`/api/production/carousels/${encodeURIComponent(item.id)}/generate`,async () => ({revision:data.revision,slides,aspect:requirements.aspect,instruction:modal.form.instruction.value.trim(),requirements,redo:true}));
        delete choice.image_redo_many;delete choice.image_slides;choice.redo_picks = [];closeModal(true);toast(`第 ${slides.join('、')} 张已开始重做，旧版本保留。`);await route(true);
      });
      modal.form.instruction.oninput = () => {choice.image_redo_many = modal.form.instruction.value;};
    };
    updateBulk();
    $$('[data-approve-output]').forEach(button => button.onclick = async () => {
      const slide = item.slides.find(slide => slide.number === Number(button.dataset.outputSlide)), output = (slide.outputs || []).find(output => output.id === button.dataset.approveOutput);
      if (!output || output.stale) return;
      button.disabled = true;
      try {
        if (button.dataset.selectOutput) {await carouselMutation(data,() => send(`select-output-${item.id}-${slide.number}`,`/api/production/carousels/${encodeURIComponent(item.id)}/select-output`,{revision:data.revision,slide:slide.number,output_id:output.id}));toast(`第 ${slide.number} 张已选用。`);await refreshProductionDetail(data,'carousel',{scope:'images'});}
        else await approve(slide.number,output,true);
      } catch (error) {toast(fail(error),true);if (button.isConnected) button.disabled = false;}
    });
    $$('[data-revoke-output]').forEach(button => button.onclick = async () => {
      const slide = item.slides.find(slide => slide.number === Number(button.dataset.outputSlide)), output = (slide.outputs || []).find(output => output.id === button.dataset.revokeOutput);
      $$('[data-approve-output],[data-revoke-output]').forEach(node => {node.disabled = true;});
      try {await approve(slide.number,output,false,output.note || '');}catch (error) {toast(fail(error),true);await refreshProductionDetail(data,'carousel',{scope:'images'});}
    });
    $$('[data-redo-slide]').forEach(button => button.onclick = () => {
      const slide = Number(button.dataset.redoSlide), draftKey = `image_redo_${slide}`, redo = !!item.slides.find(item => item.number === slide)?.outputs?.length;
      const modal = openModal(`第 ${slide} 张：${redo ? '重新出图' : '生成成图'}`,`<p class="context-note">${redo ? '仍从这一张当前选用的原底图重做，旧成图会保留。' : '使用这一张当前选用的底图生成成图。'}新成图可直接确认选用。</p><label class="field"><span>${redo ? '重做要求' : '本次要求（可选）'}</span><textarea name="instruction" rows="3" maxlength="2000" placeholder="例如：纠正英文文字，把主体放大，减少背景干扰">${esc(choice[draftKey] || '')}</textarea></label>`,redo ? '开始重做' : '开始生成',async modal => {
        remember();const requirements = {...choice.requirements};
        await once(modal,'generate',`/api/production/carousels/${encodeURIComponent(item.id)}/generate`,async () => ({revision:data.revision,slides:[slide],aspect:requirements.aspect,instruction:modal.form.instruction.value.trim(),requirements,redo}));
        delete choice[draftKey];delete choice.image_slides;closeModal(true);toast(`第 ${slide} 张已开始重做，旧版本保留。`);await route(true);
      });
      modal.form.instruction.oninput = () => {choice[draftKey] = modal.form.instruction.value;};
      modal.form.instruction.maxLength = 2000;
      const targetAspect = choice.image_aspect || item.image_aspect || '3:4';
      if (targetAspect !== (item.image_aspect || '3:4') && item.slides.some(slide => (slide.outputs || []).length))
        $('.script-editor-body',modal.dialog).insertAdjacentHTML('afterbegin',`<p class="context-note image-aspect-note">本次采用 ${esc(targetAspect)}，整组旧比例版本都会失效，需要按新比例重新出图。</p>`);
    });
    markCurrent(choice.image_current_slide || item.slides[0]?.number);
    $('#carousel-image-slides').onpointerdown = event => {const card = event.target.closest('[data-image-slide]');if (card) markCurrent(Number(card.dataset.imageSlide));};
    $('#carousel-image-slides').onfocusin = event => {const card = event.target.closest('[data-image-slide]');if (card) markCurrent(Number(card.dataset.imageSlide));};
    };
    const bindCancels = () => $$('[data-cancel-generation]').forEach(button => button.onclick = async () => {
      button.disabled = true;button.textContent = '正在停止…';
      try {await api(`/api/production/carousels/${encodeURIComponent(item.id)}/generations/${encodeURIComponent(button.dataset.cancelGeneration)}/cancel`,{method:'POST',body:{}});toast('已请求停止后台出图。');}
      catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = '停止出图';}
    });
    const keyboard = event => {
      const panel = $('#carousel-images-panel'), target = event.target;
      if (seq !== view.sequence || panel?.hidden || !panel?.isConnected || view.modal || document.querySelector('dialog[open]') || event.ctrlKey || event.metaKey || event.altKey || event.isComposing || target.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])')) return;
      const key = event.key.toLowerCase();if (!['j','k','enter','r'].includes(key) || event.repeat) return;
      if (key === 'enter' && target.closest?.('button,a,summary')) return;
      const cards = $$('[data-image-slide]',panel), current = Number(target.closest?.('[data-image-slide]')?.dataset.imageSlide || choice.image_current_slide || item.slides[0]?.number);
      let index = Math.max(0,cards.findIndex(card => Number(card.dataset.imageSlide) === current));
      if (key === 'j' || key === 'k') {index = Math.min(cards.length - 1,Math.max(0,index + (key === 'j' ? 1 : -1)));const card = cards[index];if (card) {event.preventDefault();markCurrent(Number(card.dataset.imageSlide));card.focus({preventScroll:true});card.scrollIntoView({block:'center'});}return;}
      const button = key === 'enter' ? $('.image-stack-cover [data-approve-output]',cards[index]) : $('[data-redo-slide]',cards[index]);
      if (button && !button.disabled) {event.preventDefault();button.click();}
    };
    view.imagesBinding = {id:item.id,sequence:seq,onKey:keyboard,refresh:fresh => {
      Object.assign(data,fresh);item = data.carousel;
      $$('input,select,button',form).forEach(control => {control.disabled = item.archived || generating(item);});
      const count = $('.carousel-image-heading-actions small',$('#carousel-images-panel'));
      if (count) count.textContent = `${item.slides.filter(confirmedOutput).length} / ${item.slides.length} 张已确认`;
      $('#carousel-import-finished').disabled = item.archived || generating(item);
      bindCards();bindCancels();updateAspectNote();updateBulk();
    }};
    bindCards();bindCancels();
    if (generating(item)) {
      const poll = async () => {
        if (seq !== view.sequence || pageHash().split('?')[0] !== `#production/${item.id}`) return;
        try {
          const epoch = view.progress?.localRefresh || 0;
          const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`);
          if (seq !== view.sequence) return;
          if (epoch !== (view.progress?.localRefresh || 0)) {setTimeout(poll,3000);return;}
          if (!generating(fresh.carousel) && !view.modal) {await refreshProductionDetail(data,'carousel',{scope:'images',fresh,freshEpoch:epoch});return;}
          Object.assign(item,fresh.carousel);Object.assign(data,fresh);data.carousel = item;
          const runsNode = $('#carousel-image-runs');
          runsNode.innerHTML = renderImageRuns(fresh.carousel,Boolean($('.image-run-history',runsNode)?.open));bindCancels();
          fresh.carousel.slides.forEach(slide => {
            const stageNode = $(`[data-image-stages="${slide.number}"]`), shownId = $(`[data-image-slide="${slide.number}"] .image-stack-cover [data-output-id]`)?.dataset.outputId;
            if (stageNode) stageNode.innerHTML = carouselImageStages(fresh.carousel,slide,(slide.outputs || []).find(output => output.id === shownId));
          });
          refreshProductionProgress(fresh,'carousel');
        } catch {}
        setTimeout(poll,3000);
      };
      setTimeout(poll,3000);
    }
  }
  document.addEventListener('keydown',event => view.imagesBinding?.onKey?.(event));
  // Pick a base frame straight from a labeled material clip: scrub in the page, crop/rotate/brighten, save as a candidate.
  async function openClipPicker(data,item,slide) {
    const choice = view.choice[item.id] ||= {}, root = `/api/production/carousels/${encodeURIComponent(item.id)}`;
    let target = item.slides.find(entry => entry.number === slide), listing = null, selecting = 0, clipLocked = false;
    let names = finishedOnly(choice.still_materials || data.material_timelines || []);
    if (!names.length) {
      const status = await readStatus('/api/production/resolve',10000).catch(() => ({ok:false}));
      names = status.ok ? finishedOnly(status.timelines || []) : [];
    }
    if (!names.length) {toast(`没有找到名称带「${finishedMark}」的素材时间线，请先在「找底图」里选好素材时间线。`,true);return;}
    let clips = [], suggested = [], current = null, frame = 0, rotate = 0, brightness = 1, aspect = '3:4', scale = 1, centre = {x:.5,y:.5};
    let evidence = false, image = null, token = 0, timer = 0, query = '', showAll = false, reasons = new Map(), fromAI = false;
    const ready = () => {
      const on = Boolean(current && image && !clipLocked), save = $in('[data-clip-save]'), submit = $in('[data-submit]');
      if (submit) submit.disabled = !on;
      if (save) save.disabled = !on;
    };
    const frameURL = (key,number,size) => `${root}/clips/${encodeURIComponent(key)}/frame?frame=${number}&size=${size}`;
    const modal = openModal(`第 ${slide} 张：从片段取帧`,`<div class="clip-picker">
      <div class="clip-slide-navigation"><button type="button" class="secondary compact" data-clip-previous>上一张</button><strong data-clip-slide-number></strong><button type="button" class="secondary compact" data-clip-next>下一张</button></div>
      <p class="context-note" data-clip-slide-context>${esc(target ? [...new Set([target.role,carouselSlideTitle(target)].filter(Boolean))].join(' · ') : '')}。左边按标签找片段，右边拖动取帧；取到的帧会存成这一张的候选，原素材和达芬奇不动。</p>
      <div data-clip-slide-lineage>${target ? slideProofContext(target,item.script_context) : ''}</div>
      <p class="context-note" data-clip-note role="status">正在读取素材片段清单…</p>
      <div class="clip-layout"><aside class="clip-side"><div class="clip-search"><input type="search" data-clip-search placeholder="按标签或名称筛选" aria-label="筛选片段"><button type="button" class="subtle compact" data-clip-refresh>刷新</button></div><div class="clip-list" data-clip-list></div></aside>
      <section class="clip-stage" data-clip-stage hidden><div class="clip-view" data-clip-view><canvas data-clip-canvas></canvas><div class="clip-crop" data-clip-crop hidden></div></div>
      <p class="clip-time" data-clip-time role="status"></p>
      <div class="clip-scrub"><button type="button" class="subtle compact" data-step="-10" aria-label="后退 10 帧">−10</button><button type="button" class="subtle compact" data-step="-1" aria-label="后退 1 帧">−1</button><input type="range" data-clip-slider aria-label="片段内位置"><button type="button" class="subtle compact" data-step="1" aria-label="前进 1 帧">+1</button><button type="button" class="subtle compact" data-step="10" aria-label="前进 10 帧">+10</button></div>
      <div class="clip-strip" data-clip-strip></div>
      <div class="clip-tools"><label>裁切<select data-clip-aspect><option value="3:4">3:4 竖图</option><option value="9:16">9:16</option><option value="none">不裁切</option></select></label><label>裁切大小<input type="range" min="50" max="100" value="100" data-clip-scale></label><label>亮度<input type="range" min="60" max="160" value="100" data-clip-brightness></label><button type="button" class="subtle compact" data-clip-rotate>顺时针转 90°</button><label class="clip-evidence"><input type="checkbox" data-clip-evidence>这帧是效果证据</label></div>
      <p class="clip-facts" data-clip-facts></p><div class="take-actions"><button type="button" class="secondary" data-clip-save>只存为候选</button><button type="button" class="subtle" data-clip-reveal>在达芬奇打开该片段</button></div><p class="context-note clip-key-hint">← / → 移动 1 帧，Shift + ← / → 移动 10 帧；选为底图后继续下一张未选底图的。</p></section></div></div>`,'选为这一张的底图',async current_modal => {
      const number = slide;lockControls(true);
      try {
        const saved = await save(current_modal);
        await once(current_modal,`clip-base-${number}-${saved.candidate.id}`,`${root}/base`,async () => ({revision:(await api(root)).revision,slide:number,candidate:saved.candidate.id}));
        const refreshed = await refreshProductionDetail(data,'carousel',{scope:'pick'});
        if (!refreshed) {if (view.modal === current_modal) closeModal(true);return;}
        current_modal.clipUncertain = false;
        toast(`第 ${number} 张已取到帧并选为底图。`);
        const ordered = [...item.slides].sort((a,b) => a.number - b.number), index = ordered.findIndex(entry => entry.number === number);
        const next = [...ordered.slice(index + 1),...ordered.slice(0,index + 1)].find(entry => !entry.base);
        if (!next) {closeModal(true);return;}
        await switchSlide(next.number,true);
      } catch (error) {current_modal.clipUncertain = !definitive(error);throw error;}
      finally {if (view.modal === current_modal) lockControls(Boolean(current_modal.clipUncertain));}
    });
    modal.dialog.classList.add('clip-dialog');
    const $in = selector => $(selector,modal.dialog), note = $in('[data-clip-note]'), canvas = $in('[data-clip-canvas]'), context = canvas.getContext('2d');
    const syncNavigation = () => {
      const ordered = [...item.slides].sort((a,b) => a.number - b.number), index = ordered.findIndex(entry => entry.number === slide);
      $in('[data-clip-slide-number]').textContent = `第 ${slide} 张 / 共 ${ordered.length} 张${target?.base ? ' · 已有底图' : ' · 待选底图'}`;
      $in('[data-clip-previous]').disabled = clipLocked || index <= 0;
      $in('[data-clip-next]').disabled = clipLocked || index < 0 || index === ordered.length - 1;
    };
    const lockControls = locked => {
      clipLocked = locked;
      $$('input,select,textarea,button',$in('.script-editor-body')).forEach(node => {node.disabled = locked;});
      syncNavigation();ready();
      if (locked && modal.clipUncertain) {$in('[data-submit]').disabled = false;$in('[data-hint]').textContent = '结果尚未确认，请重试同一取帧与选用请求。';}
      else $in('[data-hint]').textContent = '';
    };
    const submitForm = modal.form.onsubmit;
    modal.form.onsubmit = async event => {
      await submitForm(event);
      if (view.modal === modal) {ready();if (modal.clipUncertain) $in('[data-submit]').disabled = false;}
    };
    syncNavigation();
    ready();
    const rectangle = () => {
      if (aspect === 'none' || !canvas.width) return null;
      const [across,down] = aspect.split(':').map(Number), ratio = across / down, width = canvas.width, height = canvas.height;
      let w, h;
      if (width / height > ratio) {h = 1;w = ratio * height / width;} else {w = 1;h = width / (ratio * height);}
      w *= scale;h *= scale;
      return {x:Math.min(1 - w,Math.max(0,centre.x - w / 2)),y:Math.min(1 - h,Math.max(0,centre.y - h / 2)),w,h};
    };
    const drawBox = () => {
      const box = $in('[data-clip-crop]'), r = rectangle();
      box.hidden = !r;
      if (r) Object.assign(box.style,{left:`${r.x * 100}%`,top:`${r.y * 100}%`,width:`${r.w * 100}%`,height:`${r.h * 100}%`});
    };
    const draw = () => {
      if (!image) return;
      const turned = rotate % 180 !== 0, width = turned ? image.naturalHeight : image.naturalWidth, height = turned ? image.naturalWidth : image.naturalHeight;
      canvas.width = width;canvas.height = height;
      context.filter = `brightness(${brightness})`;
      context.save();context.translate(width / 2,height / 2);context.rotate(rotate * Math.PI / 180);
      context.drawImage(image,-image.naturalWidth / 2,-image.naturalHeight / 2);context.restore();
      drawBox();
    };
    const clock = () => {
      if (!current) return;
      const offset = frame - current.source[0];
      $in('[data-clip-time]').textContent = `第 ${offset} 帧 · ${(offset / current.fps).toFixed(2)} 秒 / 共 ${current.seconds} 秒`;
      $in('[data-clip-slider]').value = String(frame);
    };
    const load = () => {
      if (!current) return;
      const mine = ++token, next = new Image();
      note.textContent = '正在取帧…';image = null;ready();
      next.onload = () => {if (mine !== token) return;image = next;note.textContent = '';draw();ready();};
      next.onerror = () => {if (mine === token) note.textContent = '这一帧没有取出来；反复出现请点「刷新」重新读取片段清单。';};
      next.src = frameURL(current.key,frame,'view');
    };
    const go = (number,force = false) => {
      if (!current || (!force && (modal.busy || clipLocked))) return;
      frame = Math.max(current.source[0],Math.min(current.source[1] - 1,Math.round(number)));
      image = null;ready();clock();clearTimeout(timer);timer = setTimeout(load,120);
    };
    const renderList = () => {
      const byKey = new Map(clips.map(clip => [clip.key,clip])), wanted = query.trim().toLowerCase();
      const match = clip => !wanted || `${clip.label} ${clip.timeline}`.toLowerCase().includes(wanted);
      const row = clip => `<button type="button" class="clip-row ${current?.key === clip.key ? 'chosen' : ''}" data-clip="${esc(clip.key)}"><strong>${esc(clip.label)}</strong><small>${esc(clip.timeline)} · ${esc(clip.label_status_text)}${clip.used_by.length ? ` · 已被 ${esc(clip.used_by.join('、'))} 用过` : ''}</small>${reasons.get(clip.key) ? `<small class="clip-reason">AI：${esc(reasons.get(clip.key))}</small>` : ''}</button>`;
      const top = suggested.map(key => byKey.get(key)).filter(clip => clip && match(clip));
      const rest = clips.filter(clip => !suggested.includes(clip.key) && match(clip));
      const expanded = showAll || Boolean(wanted) || !top.length;
      $in('[data-clip-list]').innerHTML = `${top.length ? `<h3>${fromAI ? `AI 按标签推荐 ${top.length} 个` : `按文字匹配推荐 ${top.length} 个（还没让 AI 挑）`}</h3>${top.map(row).join('')}` : ''}${expanded ? `<h3>${wanted ? '筛选结果' : '全部片段'}（${rest.length}${rest.length > 100 ? '，只显示前 100' : ''}）</h3>${rest.slice(0,100).map(row).join('') || '<p class="context-note">没有匹配的片段。</p>'}` : `<button type="button" class="subtle compact" data-clip-all>查看其余 ${rest.length} 个片段</button>`}`;
      $$('[data-clip]',modal.dialog).forEach(button => button.onclick = () => choose(button.dataset.clip));
      const more = $in('[data-clip-all]');
      if (more) more.onclick = () => {showAll = true;renderList();};
    };
    const choose = async key => {
      if (clipLocked && !modal.busy) return;
      const mine = ++selecting;
      note.textContent = '正在读取片段…';image = null;ready();
      let next;
      try {next = await api(`${root}/clips/${encodeURIComponent(key)}`);}
      catch (error) {if (mine === selecting && view.modal === modal) note.textContent = fail(error);return;}
      if (mine !== selecting || !modal.dialog.isConnected || view.modal !== modal) return;
      current = next;
      note.textContent = '';
      const [left,right] = current.source;
      $in('[data-clip-stage]').hidden = false;
      const slider = $in('[data-clip-slider]');slider.min = String(left);slider.max = String(right - 1);
      const offsets = current.strip || [], sheet = `${root}/clips/${encodeURIComponent(current.key)}/strip`, strip = $in('[data-clip-strip]');
      strip.style.setProperty('--tile-ratio',`${current.width} / ${current.height}`);
      strip.innerHTML = offsets.map((offset,index) => `<button type="button" data-frame="${left + offset}" aria-label="跳到第 ${offset} 帧" style="background-image:url('${esc(sheet)}');background-size:${offsets.length * 100}% 100%;background-position:${offsets.length > 1 ? index / (offsets.length - 1) * 100 : 0}% 0"></button>`).join('');
      $$('[data-frame]',modal.dialog).forEach(button => button.onclick = () => go(Number(button.dataset.frame)));
      $in('[data-clip-facts]').textContent = `${current.label} · ${current.timeline} · ${current.width}×${current.height} · ${current.label_status_text}${current.used_by.length ? ` · 这段素材已被 ${current.used_by.join('、')} 用过` : ''}`;
      renderList();go((left + right - 1) / 2,true);
    };
    const body = () => {
      const r = rectangle();
      return {slide,key:current.key,frame,rotate,brightness:Number(brightness.toFixed(2)),crop:r ? {x:r.x,y:r.y,w:r.w,h:r.h} : null,evidence};
    };
    const save = async active => {
      if (!active.clipUncertain && (!current || !image)) throw new Error('请先选一个片段并等这一帧显示出来。');
      const payload = active.clipUncertain ? active.clipFramePayload : body();
      active.clipFramePayload = payload;
      return once(active,`clip-${slide}-${current.key}-${frame}-${JSON.stringify(payload)}`,`${root}/clip-frame`,async () => payload);
    };
    const updateSuggestions = () => {
      const aiPicks = listing?.ai_picks?.[String(slide)] || [];
      fromAI = aiPicks.length > 0;reasons = new Map(aiPicks.map(pick => [pick.key,pick.reason]));
      suggested = fromAI ? aiPicks.map(pick => pick.key) : (listing?.suggested?.[String(slide)] || []);
    };
    const switchSlide = async (number,force = false) => {
      if ((!force && (modal.busy || clipLocked)) || !item.slides.some(entry => entry.number === number) || view.modal !== modal) return;
      ++selecting;++token;clearTimeout(timer);slide = number;target = item.slides.find(entry => entry.number === number);
      current = null;image = null;rotate = 0;brightness = 1;aspect = '3:4';scale = 1;centre = {x:.5,y:.5};evidence = false;query = '';showAll = false;
      modal.clipFramePayload = null;setError(modal.dialog,'');
      $in('.dialog-heading h2').textContent = `第 ${slide} 张：从片段取帧`;
      $in('[data-clip-slide-lineage]').innerHTML = slideProofContext(target,item.script_context);
      $in('[data-clip-slide-context]').textContent = `${[...new Set([target.role,carouselSlideTitle(target)].filter(Boolean))].join(' · ')}。左边按标签找片段，右边拖动取帧；选好底图后继续下一张未选底图的。`;
      $in('[data-clip-stage]').hidden = true;$in('[data-clip-search]').value = '';
      $in('[data-clip-aspect]').value = '3:4';$in('[data-clip-scale]').value = '100';$in('[data-clip-brightness]').value = '100';$in('[data-clip-evidence]').checked = false;
      updateSuggestions();syncNavigation();renderList();ready();
      note.textContent = clips.length ? `${clips.length} 个片段，正在查看第 ${slide} 张的候选。` : '没有可用片段，请刷新素材清单。';
      if (suggested.length) await choose(suggested[0]);
    };
    const adjacentSlide = direction => {
      const ordered = [...item.slides].sort((a,b) => a.number - b.number), index = ordered.findIndex(entry => entry.number === slide);
      const next = ordered[index + direction];if (next) void switchSlide(next.number);
    };
    $in('[data-clip-previous]').onclick = () => adjacentSlide(-1);
    $in('[data-clip-next]').onclick = () => adjacentSlide(1);
    modal.dialog.addEventListener('keydown',event => {
      if (!['ArrowLeft','ArrowRight'].includes(event.key) || modal.busy || clipLocked || !current) return;
      const field = event.target.closest('input,textarea,select,[contenteditable="true"]');
      if (field && !field.matches('[data-clip-slider]')) return;
      event.preventDefault();go(frame + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 10 : 1));
    });
    $$('[data-step]',modal.dialog).forEach(button => button.onclick = () => go(frame + Number(button.dataset.step)));
    $in('[data-clip-slider]').oninput = event => go(Number(event.currentTarget.value));
    $in('[data-clip-aspect]').onchange = event => {aspect = event.currentTarget.value;drawBox();};
    $in('[data-clip-scale]').oninput = event => {scale = Number(event.currentTarget.value) / 100;drawBox();};
    $in('[data-clip-brightness]').oninput = event => {brightness = Number(event.currentTarget.value) / 100;draw();};
    $in('[data-clip-rotate]').onclick = () => {rotate = (rotate + 90) % 360;centre = {x:.5,y:.5};draw();};
    $in('[data-clip-evidence]').onchange = event => {evidence = event.currentTarget.checked;};
    $in('[data-clip-search]').oninput = event => {query = event.currentTarget.value;renderList();};
    $in('[data-clip-search]').onkeydown = event => {if (event.key === 'Enter') event.preventDefault();};
    const view_ = $in('[data-clip-view]');
    const move = event => {
      const box = canvas.getBoundingClientRect();
      if (!box.width) return;
      centre = {x:Math.min(1,Math.max(0,(event.clientX - box.left) / box.width)),y:Math.min(1,Math.max(0,(event.clientY - box.top) / box.height))};
      drawBox();
    };
    view_.onpointerdown = event => {view_.setPointerCapture(event.pointerId);move(event);view_.onpointermove = move;};
    view_.onpointerup = event => {view_.onpointermove = null;try {view_.releasePointerCapture(event.pointerId);} catch {}};
    const saveOnly = $in('[data-clip-save]');
    if (saveOnly) saveOnly.onclick = async event => {
      const button = event.currentTarget;
      if (modal.busy) return;
      modal.busy = true;button.disabled = true;setError(modal.dialog,'');
      try {await save(modal);await refreshProductionDetail(data,'carousel',{scope:'pick'});toast(`已存为第 ${slide} 张的候选，可以继续取帧或选为底图。`);}
      catch (error) {setError(modal.dialog,fail(error));}
      finally {modal.busy = false;button.disabled = false;}
    };
    $in('[data-clip-reveal]').onclick = async event => {
      const button = event.currentTarget;
      if (!current || button.disabled) return;
      button.disabled = true;
      try {const result = await api(`${root}/reveal`,{method:'POST',body:{key:current.key,frame}});toast(`已在达芬奇打开「${result.name}」${result.timecode ? ` ${result.timecode}` : ''}。`);}
      catch (error) {setError(modal.dialog,fail(error));}
      finally {button.disabled = false;}
    };
    const reload = async refresh => {
      note.textContent = refresh ? '正在向达芬奇重新读取片段清单…' : '正在读取素材片段清单（第一次需要几秒到几十秒）…';
      const params = new URLSearchParams();names.forEach(name => params.append('materials',name));if (refresh) params.set('refresh','1');
      try {
        const nextListing = await api(`${root}/clips?${params}`);
        if (!modal.dialog.isConnected || view.modal !== modal) return;
        listing = nextListing;
        clips = listing.clips;
        updateSuggestions();
        note.textContent = clips.length ? `${clips.length} 个片段${listing.missing?.length ? `；找不到时间线：${listing.missing.join('、')}` : ''}` : '这些素材时间线里没有可用的片段。';
        renderList();
        if (!current && suggested.length) await choose(suggested[0]);
      } catch (error) {note.textContent = fail(error);}
    };
    $in('[data-clip-refresh]').onclick = () => void reload(true);
    await reload(false);
  }
  function bindStills(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, seq = view.sequence, panel = $('#still-panel');
    const root = `/api/production/carousels/${encodeURIComponent(item.id)}`;
    // One image per slide once it has a base: leftover candidates from earlier picks are cleared.
    const extras = item.slides.reduce((sum,slide) => sum + (slide.base ? (slide.candidates || []).filter(candidate => candidate.id !== slide.base.id).length : 0),0);
    if (extras && !item.archived && !view.tidying) {
      view.tidying = true;
      send(`tidy-${item.id}`,`${root}/tidy`,{})
        .then(async result => {toast(`每张只保留一张底图，已清理 ${result.removed} 张多余候选。`);await route(true);})
        .catch(error => toast(fail(error),true)).finally(() => {view.tidying = false;});
    }
    const request = readStatus('/api/production/resolve',10000).catch(error => ({ok:false,error:fail(error)}));
    resolveStatus($('[data-resolve-status]',panel),{project:data.project},request);
    request.then(status => {
      const box = $('[data-materials]',panel);if (!box) return;
      materialPicker(box,status,new Set(),choice.still_materials,data.material_timelines);
    });
    api('/api/ai/preferences').then(result => {
      const node = $('[data-clip-ai-model]',panel), model = result.preferences?.model || 'codex-default';
      if (node) node.textContent = `使用全局默认：${stillModelLabel(model)}${result.preferences?.reasoning_effort ? ` · ${result.preferences.reasoning_effort}` : ''}（可在设置中修改）`;
    }).catch(() => {const node = $('[data-clip-ai-model]',panel);if (node) node.textContent = '沿用全局默认模型。';});
    // Removes finished AI frame searches with their local files and unchosen candidates; chosen bases and DaVinci stay.
    const deleteSearches = async ids => {
      const base = `${root}/searches`;
      let previews;
      try {previews = await Promise.all(ids.map(id => api(`${base}/${encodeURIComponent(id)}/delete`,{method:'POST',body:{preview:true}})));}
      catch (error) {toast(fail(error),true);return;}
      const bytes = previews.reduce((sum,entry) => sum + (entry.bytes || 0),0), candidates = previews.reduce((sum,entry) => sum + (entry.candidates || 0),0), kept = previews.reduce((sum,entry) => sum + (entry.kept_as_base || 0),0);
      const size = bytes >= 1073741824 ? `${(bytes / 1073741824).toFixed(1)} GB` : `${Math.max(1,Math.round(bytes / 1048576))} MB`;
      openModal(ids.length > 1 ? `清理 ${ids.length} 次旧的找画面记录` : '删除这次找画面记录',`<p class="context-note">将删除${ids.length > 1 ? `这 ${ids.length} 次` : '这次'}找画面的工作台记录、本地保存的交接文件和导出静帧，以及它产生的 ${candidates} 张未选用候选图，共约 ${esc(size)}。此操作不能撤销。</p><ul class="context-list"><li>不会改动达芬奇里的时间线和工程。</li><li>已选为底图的候选保留${kept ? `（${kept} 张）` : ''}；从片段取帧、从已有图片添加的候选不受影响。</li></ul>`,'确认删除',async modal => {
        const warnings = [];
        for (const id of ids) {
          const result = await send(`search-delete-${id}`,`${base}/${encodeURIComponent(id)}/delete`,{});
          warnings.push(...(result.warnings || []));
        }
        closeModal(true);
        toast(warnings.length ? warnings[0] : `已删除 ${ids.length} 次找画面记录及本地文件。`,warnings.length > 0);
        await route(true);
      });
    };
    const bindRuns = () => {
      $$('[data-delete-search]').forEach(button => button.onclick = () => deleteSearches([button.dataset.deleteSearch]));
      $$('[data-clean-searches]').forEach(button => button.onclick = () => deleteSearches(button.dataset.cleanSearches.split(',').filter(Boolean)));
      $$('[data-cancel-search]').forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {await api(`${root}/searches/${encodeURIComponent(button.dataset.cancelSearch)}/cancel`,{method:'POST',body:{}});toast('正在停止。');}
        catch (error) {toast(fail(error),true);button.disabled = false;}
      });
    };
    bindRuns();
    const aiButton = $('#clip-ai-pick'), aiStatus = $('[data-clip-ai-status]',panel);
    let clipRequestPending = false, clipMonitoring = false, clipTimer = null;
    const currentClipPage = () => seq === view.sequence && panel.isConnected;
    const paintClipRuntime = () => {
      if (!currentClipPage()) return;
      refreshProductionProgress(data,'carousel');
      const runtime = $$('.production-runtime',panel).find(node => node.parentElement === panel);
      if (runtime) runtime.dataset.clipRuntime = 'true';
    };
    const stopClipRuntime = () => {
      clipMonitoring = false;clearTimeout(clipTimer);item.clip_picking_live = null;
      if (!currentClipPage()) return;
      ($('[data-clip-runtime]',panel) || $$('.production-runtime',panel).find(node => node.parentElement === panel))?.remove();
      aiButton.disabled = Boolean(item.archived);aiStatus.textContent = '';refreshProductionProgress(data,'carousel');
    };
    const pollClipRuntime = async () => {
      if (!clipMonitoring || !currentClipPage()) return;
      try {
        const epoch = view.progress?.localRefresh || 0;
        const fresh = await api(root);
        if (!clipMonitoring || !currentClipPage()) return;
        if (epoch !== (view.progress?.localRefresh || 0)) {clipTimer = setTimeout(pollClipRuntime,1000);return;}
        data.revision = fresh.revision;
        if (fresh.carousel.clip_picking_live) {item.clip_picking_live = fresh.carousel.clip_picking_live;paintClipRuntime();}
        else if (!clipRequestPending) {stopClipRuntime();await refreshProductionDetail(data,'carousel',{scope:'pick',fresh,freshEpoch:epoch});return;}
        else {item.clip_picking_live = {...item.clip_picking_live,stage:'',progress:'等待选片段结果…'};paintClipRuntime();}
      } catch {}
      if (clipMonitoring && currentClipPage()) clipTimer = setTimeout(pollClipRuntime,1000);
    };
    const startClipRuntime = live => {
      clearTimeout(clipTimer);clipMonitoring = true;item.clip_picking_live = live;
      aiButton.disabled = true;aiStatus.textContent = '';paintClipRuntime();mountRuntimeTicker();
      clipTimer = setTimeout(pollClipRuntime,1000);
    };
    if (aiButton && item.clip_picking_live) startClipRuntime(item.clip_picking_live);
    if (aiButton) aiButton.onclick = async () => {
      if (clipMonitoring || clipRequestPending) return;
      setError(panel,'');
      const materials = pickedMaterials(panel), problem = materialProblem(materials);
      if (problem) {setError(panel,problem);return;}
      const perSlide = Number($('select[name="per_slide"]',panel).value), instruction = $('textarea[name="instruction"]',panel).value.trim();
      Object.assign(choice,{still_materials:materials,per_slide:perSlide,still_instruction:instruction});
      clipRequestPending = true;
      const started = new Date().toISOString();
      startClipRuntime({status:'running',started_at:started,stage:'',progress:'正在启动 AI 挑片段…',updated_at:started});
      try {
        const result = await api(`${root}/clip-picks`,{method:'POST',body:{materials,per_slide:perSlide,instruction,request_id:crypto.randomUUID()}});
        clipRequestPending = false;stopClipRuntime();
        toast(`AI 已挑好 ${result.count} 个候选片段，点每一张的「从片段取帧」查看。`);
        if (currentClipPage()) await refreshProductionDetail(data,'carousel',{scope:'pick'});
      } catch (error) {
        if (currentClipPage()) setError(panel,fail(error));
      } finally {clipRequestPending = false;stopClipRuntime();}
    };
    bindPickCards(data);
  }
  // Safe to rebind after a single tile changes: no service reads, polling, or accumulating drag listeners.
  function bindPickCards(data,host = $('#pick-panel')) {
    if (!host) return;
    const item = data.carousel, choice = view.choice[item.id] ||= {};
    $$('[data-pick-description]',host).forEach(button => {
      const caption = button.closest('figcaption'), key = `${button.closest('[data-pick-slide]')?.dataset.pickSlide}:${button.closest('figure')?.querySelector('img')?.getAttribute('src')}`;
      const show = expanded => {caption.classList.toggle('expanded',expanded);button.setAttribute('aria-expanded',String(expanded));button.textContent = expanded ? '收起说明' : '展开说明';};
      show(Boolean(choice.pick_descriptions?.[key]));
      button.onclick = () => {const expanded = button.getAttribute('aria-expanded') !== 'true';(choice.pick_descriptions ||= {})[key] = expanded;show(expanded);};
    });
    const pick = async (slide,candidate) => {
      try {await send(`base-${item.id}-${slide}`,`/api/production/carousels/${encodeURIComponent(item.id)}/base`,{revision:data.revision,slide,candidate});toast(candidate ? `第 ${slide} 张已选好底图。` : `第 ${slide} 张已取消底图。`);await refreshProductionDetail(data,'carousel',{scope:'pick'});}
      catch (error) {toast(fail(error),true);$$(`[data-pick="${slide}"],[data-unpick="${slide}"]`,host).forEach(button => {button.disabled = false;});}
    };
    $$('[data-pick]',host).forEach(button => button.onclick = () => {button.disabled = true;void pick(Number(button.dataset.pick),button.dataset.candidate);});
    $$('[data-unpick]',host).forEach(button => button.onclick = () => {button.disabled = true;void pick(Number(button.dataset.unpick),null);});
    // 多图合成: AI reads this slide's row of the 逐张表 and decides the layout; the workbench pastes the real pictures.
    $$('[data-compose-slide]',host).forEach(button => button.onclick = async () => {
      const slide = Number(button.dataset.composeSlide), tile = button.closest('[data-pick-slide]');
      const ticked = $$(`[data-compose-pick="${slide}"]:checked`,tile).map(box => box.value);
      if (ticked.length === 1) {toast('至少勾两张；不勾就让 AI 从这一张的全部候选里挑。',true);return;}
      if (ticked.length > 6) {toast('一次最多合成 6 张候选。',true);return;}
      const label = button.textContent;button.disabled = true;button.textContent = 'AI 判断合成中…';
      try {
        const result = await send(`compose-${item.id}-${slide}`,`/api/production/carousels/${encodeURIComponent(item.id)}/compose`,{slide,...(ticked.length ? {candidates:ticked} : {})});
        if (!result.candidate) toast(`AI 判断第 ${slide} 张用一张图就够：${result.plan?.reason || '已推荐一张候选'}`);
        else toast(`第 ${slide} 张已按 AI 方案合成，放在候选里，看过后点「选为底图」。`);
        await refreshProductionDetail(data,'carousel',{scope:'pick'});
      } catch (error) {toast(fail(error),true);button.disabled = false;button.textContent = label;}
    });
    // Drag pictures from the desktop or a folder straight onto a slide's tile: one picture replaces its base, several become candidates.
    const carriesFiles = event => [...(event.dataTransfer?.types || [])].includes('Files');
    if (!item.archived) $$('[data-pick-slide]',host).forEach(tile => {
      const slide = Number(tile.dataset.pickSlide);
      let depth = 0;
      tile.ondragenter = event => {if (!carriesFiles(event)) return;event.preventDefault();depth += 1;tile.classList.add('drop-over');};
      tile.ondragover = event => {if (!carriesFiles(event)) return;event.preventDefault();event.dataTransfer.dropEffect = 'copy';};
      tile.ondragleave = () => {depth = Math.max(0,depth - 1);if (!depth) tile.classList.remove('drop-over');};
      tile.ondrop = async event => {
        if (!carriesFiles(event)) return;
        event.preventDefault();event.stopPropagation();depth = 0;tile.classList.remove('drop-over');
        try {hideVideoDrop();} catch {}
        if (tile.dataset.uploading) return;
        const files = [...event.dataTransfer.files];
        tile.dataset.uploading = '1';tile.classList.add('uploading');
        try {
          const {made,picked} = await uploadSlideImages(item,slide,files,{pick:true,onStatus:text => {tile.dataset.dropHint = text;}});
          toast(picked ? `第 ${slide} 张已导入图片并选为底图。` : `已为第 ${slide} 张导入 ${made.length} 张图片，可在候选里选用。`);
          await refreshProductionDetail(data,'carousel',{scope:'pick'});
        } catch (error) {
          toast(fail(error),true);
          delete tile.dataset.uploading;tile.classList.remove('uploading');
        }
      };
    });
    $$('[data-clip-slide]',host).forEach(button => button.onclick = () => {
      button.disabled = true;
      void openClipPicker(data,item,Number(button.dataset.clipSlide)).catch(error => toast(fail(error),true)).finally(() => {button.disabled = false;});
    });
    $$('[data-import-slide]',host).forEach(button => button.onclick = async () => {
      const slide = Number(button.dataset.importSlide);
      let listing;
      try {listing = await api(`/api/production/carousels/${encodeURIComponent(item.id)}/folder-images`);}
      catch (error) {toast(fail(error),true);return;}
      const groups = new Map();for (const image of listing.images) {if (!groups.has(image.folder)) groups.set(image.folder,[]);groups.get(image.folder).push(image);}
      const modal = openModal(`第 ${slide} 张：导入底图`,`${slideProofContext(item.slides.find(entry => entry.number === slide),item.script_context)}<section class="upload-box"><div class="upload-drop" data-upload-drop tabindex="0" role="button" aria-label="上传本地图片"><strong>上传本地图片</strong><span>点击选择、拖入图片，或按 Ctrl+V 粘贴截图 · PNG / JPG / WebP · 单张最大 25 MB，可一次选多张</span><input type="file" accept="image/png,image/jpeg,image/webp" multiple hidden data-upload-file></div><label class="upload-pick"><input type="checkbox" data-upload-pick ${item.slides.find(entry => entry.number === slide)?.base ? '' : 'checked'}> 只上传一张时直接选为这一张的底图（会清掉这一张的其他候选）</label><p class="context-note" data-upload-status role="status"></p></section><h3 class="folder-title">或者从图文素材文件夹添加</h3><p class="context-note">图片目录：${esc(listing.root)}${listing.start ? `（从 ${esc(listing.start)} 开始）` : ''}。点一张图，把它复制进这张制作单的候选；原文件不动。</p>${groups.size ? [...groups].map(([folder,images]) => `<h3 class="folder-title">${esc(folder || '（根目录）')}</h3><div class="pick-grid folder-grid">${images.map(image => `<button type="button" class="folder-pick" data-path="${esc(image.path)}" title="${esc(image.path)}"><img src="/api/production/folder-thumb?path=${encodeURIComponent(image.path)}" alt="${esc(image.name)}" loading="lazy"><span>${esc(image.name)}</span></button>`).join('')}</div>`).join('') : '<p class="context-note">这个目录里没有图片。</p>'}`,'关闭',async () => closeModal(true));
      const dropzone = $('[data-upload-drop]',modal.dialog), chooser = $('[data-upload-file]',modal.dialog), uploadStatus = $('[data-upload-status]',modal.dialog);
      const sendImages = async files => {
        if (modal.busy) return;
        modal.busy = true;dropzone.setAttribute('aria-disabled','true');
        try {
          const {made,picked} = await uploadSlideImages(item,slide,files,{pick:$('[data-upload-pick]',modal.dialog).checked,onStatus:text => {uploadStatus.textContent = text;}});
          modal.busy = false;closeModal(true);
          toast(picked ? `第 ${slide} 张已导入图片并选为底图。` : `已为第 ${slide} 张导入 ${made.length} 张图片，可在候选里选用。`);
          await refreshProductionDetail(data,'carousel',{scope:'pick'});
        } catch (error) {
          modal.busy = false;dropzone.removeAttribute('aria-disabled');
          uploadStatus.textContent = fail(error);
        }
      };
      dropzone.onclick = () => {if (!modal.busy) chooser.click();};
      dropzone.onkeydown = event => {if (event.key === 'Enter' || event.key === ' ') {event.preventDefault();dropzone.click();}};
      chooser.onchange = () => {const files = [...chooser.files];chooser.value = '';void sendImages(files);};
      ['dragenter','dragover'].forEach(name => dropzone.addEventListener(name,event => {event.preventDefault();dropzone.classList.add('over');}));
      ['dragleave','drop'].forEach(name => dropzone.addEventListener(name,event => {event.preventDefault();dropzone.classList.remove('over');}));
      dropzone.addEventListener('drop',event => void sendImages(event.dataTransfer?.files || []));
      modal.dialog.addEventListener('paste',event => {
        const files = [...(event.clipboardData?.files || [])];
        if (files.length) {event.preventDefault();void sendImages(files);}
      });
      $$('.folder-pick',modal.dialog).forEach(tile => tile.onclick = async () => {
        if (modal.busy) return;
        modal.busy = true;$$('.folder-pick',modal.dialog).forEach(node => {node.disabled = true;});
        try {await once(modal,`import-${tile.dataset.path}`,`/api/production/carousels/${encodeURIComponent(item.id)}/import`,async () => ({slide,path:tile.dataset.path}));modal.busy = false;closeModal(true);toast(`已添加到第 ${slide} 张的候选。`);await refreshProductionDetail(data,'carousel',{scope:'pick'});}
        catch (error) {modal.busy = false;setError(modal.dialog,fail(error));$$('.folder-pick',modal.dialog).forEach(node => {node.disabled = false;});}
      });
    });
  }
  const carouselDeliveries = data => data.carousel.deliveries || data.deliveries || [];
  function rememberDeliveryCleanup(identifier,result) {
    const choice = view.choice[identifier] ||= {};
    const warnings = Array.isArray(result?.cleanup_warnings) ? result.cleanup_warnings.filter(value => typeof value === 'string' && value.trim()) : [];
    if (warnings.length) {
      choice.delivery_cleanup_warnings = warnings;
      toast(`成品包已整理。部分本地历史数据清理未完成：${warnings.join('；')}`,true);
    } else delete choice.delivery_cleanup_warnings;
  }
  const carouselPublication = data => data.publication || data.carousel.publication || null;
  const deliveryURL = (item,delivery) => `/api/production/carousels/${encodeURIComponent(item.id)}/deliveries/${encodeURIComponent(delivery.id)}/download`;
  function publicationPerformance(data,publication) {
    const sales = data.sales_data || publication.sales_data || {status:'not_found',metrics:{},evidence:{}};
    const item = {product:data.carousel.product,current:{metrics:{video_id:publication.post_id}},sales_data:sales};
    if (typeof renderPerformanceSection === 'function') return renderPerformanceSection(item,'carousel');
    if (sales.status !== 'matched') return `<section class="section-card performance-empty"><h2>表现数据</h2><p>销售数据尚未匹配到此帖子，导入或同步销售数据后再查看。</p></section>`;
    return `<section class="section-card"><h2>表现数据</h2><div class="facts-grid">${[['views_display','播放量'],['product_clicks_display','商品点击'],['items_sold','售出件数'],['ctor_display','CTOR']].map(([key,label]) => `<div><span class="metric-label">${label}</span><span class="metric-value">${displayValue(sales.metrics?.[key])}</span></div>`).join('')}</div><p class="context-note">${esc(sales.evidence?.window || '统计窗口未提供')}</p></section>`;
  }
  // A registration whose package is still listed above reads its sources from that package card, not from a second copy.
  const listedPackage = (record,deliveries) => deliveries.some(delivery => delivery.id === record.delivery_id && (!record.delivery_sha256 || delivery.sha256 === record.delivery_sha256));
  function publicationHistory(item,deliveries) {
    const history = [...(item.publication_history || [])].reverse().sort((a,b) => String(b.updated_at || '').localeCompare(String(a.updated_at || '')));
    if (!history.length) return '';
    return `<details class="publication-history" id="carousel-publication-history"><summary>发布登记历史（${history.length}）</summary><p class="context-note">这里保留每次保存的登记状态，也可能包含更正。请以当前登记为准。</p>${history.map(entry => {
      const index = deliveries.findIndex(delivery => delivery.id === entry.delivery_id);
      return `<article data-publication-history="${esc(entry.id || '')}"><small>${esc(readableDate(entry.updated_at))}</small><dl class="publication-facts"><dt>帖子 ID</dt><dd><span>${esc(entry.post_id)}</span>${copyIdButton(entry.post_id,'图文 ID')}</dd><dt>发布日期</dt><dd>${esc(entry.published_on)}</dd><dt>所用成品包</dt><dd>${index >= 0 ? '保留成品包 · ' : '原发布来源（文件已清理） · '}${esc(entry.delivery_id)}</dd>${entry.account ? `<dt>账号</dt><dd>${esc(entry.account)}</dd>` : ''}${entry.note ? `<dt>备注</dt><dd>${esc(entry.note)}</dd>` : ''}</dl>${listedPackage(entry,deliveries) ? '' : window.DeliveryProvenance?.render({kind:'carousel',record:entry,owner:item}) || ''}</article>`;
    }).join('')}</details>`;
  }
  // Same publish step as the video order: copy block from the adopted script, then an inline registration that only needs the ID.
  function carouselPublishCopy(data) {
    const item = data.carousel, copy = {titles:data.publish_titles || [],zh:item.caption_zh || '',en:item.caption_en || '',version:data.script?.version};
    const music = item.plan?.music ? `<p class="carousel-music"><b>音乐方向</b>${esc(item.plan.music)}<small>发布时在 TikTok 里选热门且贴合的音乐</small></p>` : '';
    return (window.VideoPublication?.copyBlock?.(copy,{collapsed:Boolean(carouselPublication(data))}) || '') + music;
  }
  function carouselPublicationEditor(data) {
    const item = data.carousel, deliveries = carouselDeliveries(data), publication = carouselPublication(data), choice = view.choice[item.id] ||= {};
    const pending = choice.publication_pending, draft = pending?.values || choice.publication_draft || publication || {};
    const selected = draft.delivery_id || deliveries.find(delivery => delivery.current)?.id || deliveries.at(-1)?.id;
    // This is the current publication's frozen source, not a list of historical packages.
    // Keep it selected until the person explicitly chooses the latest package.
    const removedSource = publication?.delivery_id && !deliveries.some(delivery => delivery.id === publication.delivery_id);
    const missingDraft = selected && selected !== publication?.delivery_id && !deliveries.some(delivery => delivery.id === selected);
    const today = window.VideoPublication?.localDate?.() || new Date().toISOString().slice(0,10);
    return `<details class="video-publication-editor" id="carousel-publication-editor" ${!publication || pending ? 'open' : ''}><summary>${publication ? '绑定另一个图文 / 修正当前登记' : '登记发布图文 ID'}</summary><p class="context-note">${pending ? '上次登记的结果尚未确认，请直接重试原登记。' : '以后导入销售表时，会按这个 ID 自动关联到这张制作单。修改会保留原登记历史。'}</p>${publication && !pending ? '<p class="context-note">一张制作单可以绑定多个图文帖子：填新的 ID 会再绑定一个，原登记保留；填原 ID 是修正当前登记。</p>' : ''}${publication || pending ? '' : window.VideoPublication?.registrationHint?.((data.script_registrations || []).map(String)) || ''}<form><div class="form-row video-publication-identity"><label class="field"><span>图文 ID（也可以直接粘贴帖子链接）</span><input name="post_id" required maxlength="2000" inputmode="url" autocomplete="off" value="${esc(draft.post_id || '')}" placeholder="7561234567890123456 或帖子链接"></label><input type="hidden" name="published_on" value="${esc(draft.published_on || today)}"></div><details class="video-publication-more"><summary>成品包、账号、帖子链接与备注（都可不填）</summary><label class="field"><span>实际使用的成品包</span><select name="delivery_id" required>${missingDraft ? '<option value="" selected>原选成品包已清理，请重新选择</option>' : ''}${removedSource ? `<option value="${esc(publication.delivery_id)}" ${publication.delivery_id === selected ? 'selected' : ''}>保留原发布来源 · 文件已清理</option>` : ''}${[...deliveries].reverse().map(delivery => `<option value="${esc(delivery.id)}" ${delivery.id === selected ? 'selected' : ''}>最新成品包 · ${esc(delivery.aspect)} · ${esc(readableDate(delivery.created_at))}${delivery.current ? ' · 当前确认版本' : ' · 已整理版本'}</option>`).join('')}</select>${removedSource ? '<small>原发布包文件已清理，修正登记默认保留原来源；若实际使用最新包，请明确选择最新成品包。</small>' : ''}</label><div class="form-row"><label class="field"><span>发布账号</span><input name="account" maxlength="200" value="${esc(draft.account || '')}"></label><label class="field"><span>帖子链接（不填按账号和 ID 生成）</span><input type="url" name="post_url" maxlength="2000" value="${esc(draft.post_url || '')}"></label></div><label class="field"><span>备注</span><textarea name="note" rows="2" maxlength="2000">${esc(draft.note || '')}</textarea></label></details>${window.VideoPublication?.calendarChoice?.(draft.calendar_uploaded === true || draft.calendar_uploaded === '1') || ''}<div class="video-publication-actions"><button class="primary" type="submit">${pending ? '重试同一登记' : publication ? '保存修正并保留原登记' : '保存发布登记'}</button></div><p class="context-note" data-save-status role="status"></p><p class="ai-error" data-publication-error role="alert" hidden></p></form></details>`;
  }
  function deliverySection(data) {
    const item = data.carousel, deliveries = carouselDeliveries(data), publication = carouselPublication(data);
    const confirmed = item.slides.filter(confirmedOutput).length;
    const ready = item.slides.length > 0 && confirmed === item.slides.length;
    const canPack = ready && !item.archived && !generating(item);
    return `<section class="section-card" id="carousel-delivery-panel"><div class="section-title-row"><h2>成品领取与发布${infoTip('成品包只包含按张号排列的已确认图片，每张制作单只保留最新一包。新包成功整理并校验后，旧包及相关本地数据会自动清理；整理失败时保留原包。发布登记保留实际来源，本地导出与领取不会自动登记发布。')}</h2><button type="button" class="${deliveries.some(delivery => delivery.current) ? 'secondary' : 'primary'}" id="carousel-prepare-delivery" ${canPack ? '' : 'disabled'}>整理成品包</button></div>${ready ? '' : `<p class="context-note">还有 ${item.slides.length - confirmed} 张未工作台确认，先在「出图」步骤逐张确认选用。${deliveries.length ? '已整理的成品包仍可领取；新包成功整理后会替换它。' : ''}</p>`}<p class="ai-error" data-error role="alert" hidden></p>${view.choice[item.id]?.delivery_cleanup_warnings?.length ? `<p class="delivery-package-state" role="alert" data-delivery-cleanup-warning>部分本地历史数据清理未完成：${esc(view.choice[item.id].delivery_cleanup_warnings.join('；'))}</p>` : ''}<div id="carousel-deliveries">${deliveries.length ? [...deliveries].reverse().map((delivery,index) => `<article class="delivery-package ${delivery.current ? 'current' : ''}" data-delivery-id="${esc(delivery.id)}" data-activity-source="${esc(delivery.id)}"><div class="delivery-package-head"><div><strong>最新成品包 <span class="script-status ${delivery.current ? 'published' : ''}">${delivery.current ? '当前确认版本' : '已整理版本 · 待更新'}</span>${publication?.delivery_id === delivery.id ? ' <span class="script-status published">用于当前发布登记</span>' : ''}</strong><small>${esc(readableDate(delivery.created_at))} · ${delivery.slides?.length || 0} 张 · ${esc(delivery.aspect)}</small></div><a class="primary compact" href="${esc(deliveryURL(item,delivery))}" download data-download-delivery="${esc(delivery.id)}" title="${esc(delivery.filename || '')}">下载成品包</a></div>${window.DeliveryProvenance?.render({kind:'carousel',record:delivery,owner:item,publication}) || ''}</article>`).join('') : '<p class="delivery-empty">还没有整理成品包。</p>'}</div><div id="carousel-publication">${publication ? `<article class="video-publication-receipt publication-record"><div class="video-publication-title"><strong>${publication.account ? `${esc(publication.account)} · ` : ''}${esc(publication.published_on)}</strong><span class="script-status published">已登记发布</span></div><p>帖子 <a href="${esc(window.VideoPublication?.tiktokUrl?.(publication,'photo') || '#')}" target="_blank" rel="noopener noreferrer" title="在 TikTok 打开">${esc(publication.post_id)} ↗</a>${copyIdButton(publication.post_id,'图文 ID')} · ${esc(deliveries.findIndex(delivery => delivery.id === publication.delivery_id) >= 0 ? '最新成品包' : '原发布来源（文件已清理）')}${window.VideoPublication?.registrationTag?.((data.script_registrations || []).map(String),publication.post_id) || ''}</p>${window.VideoPublication?.calendarLine?.(publication,true) || ''}${publication.note ? `<p>${esc(publication.note)}</p>` : ''}${listedPackage(publication,deliveries) ? '' : window.DeliveryProvenance?.render({kind:'carousel',record:publication,owner:item}) || ''}</article><div id="carousel-publication-performance">${publicationPerformance(data,publication)}</div>${publicationHistory(item,deliveries)}` : (deliveries.length ? '' : '<p class="context-note">整理完整成品包后，可登记发布后的图文 ID。</p>')}${carouselPublishCopy(data)}${publication || item.archived ? '' : window.VideoPublication?.calendarPlanMarkup?.() || ''}${deliveries.length && !item.archived ? carouselPublicationEditor(data) : ''}</div></section>`;
  }
  function bindDelivery(data) {
    const item = data.carousel, choice = view.choice[item.id] ||= {}, panel = $('#carousel-delivery-panel');
    const showCollection = delivery => {
      const card = $$('[data-delivery-id]',panel).find(node => node.dataset.deliveryId === delivery.id);if (!card) return;
      let state = $('[data-delivery-state]',card);
      if (!state) {state = document.createElement('p');state.className = 'delivery-package-state';state.dataset.deliveryState = delivery.id;$('.delivery-package-head',card).insertAdjacentElement('afterend',state);}
      state.textContent = `${delivery.collected_at ? `已领取（${readableDate(delivery.collected_at)}）` : '尚未领取'}${delivery.download_count ? ` · 下载 ${delivery.download_count} 次` : ''}`;
    };
    carouselDeliveries(data).forEach(showCollection);
    $$('[data-download-delivery]',panel).forEach(link => link.addEventListener('click',() => {
      setTimeout(async () => {try {const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`);if (panel.isConnected) {data.revision = fresh.revision;carouselDeliveries(fresh).forEach(showCollection);data.carousel.deliveries = carouselDeliveries(fresh);refreshProductionProgress(data,'carousel');}}catch {}},1500);
    }));
    $('#carousel-prepare-delivery').onclick = async event => {
      const button = event.currentTarget;button.disabled = true;button.textContent = '正在整理…';setError(panel,'');
      try {const result = await send(`delivery-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/delivery`,{revision:data.revision});rememberDeliveryCleanup(item.id,result);toast('成品包已本地导出，可以领取；实际发布后再登记帖子信息。');await route(true);}
      catch (error) {setError(panel,fail(error));if (error.status === 409) {try {data.revision = (await api(`/api/production/carousels/${encodeURIComponent(item.id)}`)).revision;}catch {}}button.disabled = false;button.textContent = '整理成品包';}
    };
    window.VideoPublication?.bindCopy?.(panel,{titles:data.publish_titles || [],zh:item.caption_zh || '',en:item.caption_en || ''});
    $('[data-calendar-toggle]',panel)?.addEventListener('click',async event => {
      const button = event.currentTarget;button.disabled = true;
      try {
        await send(`calendar-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/calendar`,{publication_id:button.dataset.publicationId,calendar_uploaded:button.dataset.calendarToggle === '1'});
        await route(true);
      } catch (problem) {button.disabled = false;toast(fail(problem),true);}
    });
    const form = $('#carousel-publication-editor form',panel);
    if (!carouselPublication(data)) void window.VideoPublication?.bindCalendarPlan?.(panel,`carousel:${item.id}`,api,{form});
    if (form) {
      const publication = carouselPublication(data), status = $('[data-save-status]',form), error = $('[data-publication-error]',form), submit = $('[type="submit"]',form);
      // A request whose result never came back is retried unchanged, so it cannot be registered twice.
      const lock = () => {const uncertain = !!choice.publication_pending;$$('input[name],select[name],textarea[name]',form).forEach(field => {field.disabled = uncertain;});};
      lock();
      window.VideoPublication?.followPostDate?.(form);
      const remember = () => {if (!choice.publication_pending) choice.publication_draft = Object.fromEntries(new FormData(form));};
      form.addEventListener('input',remember);form.addEventListener('change',remember);
      form.onsubmit = async event => {
        event.preventDefault();
        if (form.dataset.busy) return;
        const pending = choice.publication_pending;
        let values = pending?.values;
        if (!values) {
          values = Object.fromEntries(new FormData(form));Object.keys(values).forEach(key => {values[key] = String(values[key]).trim();});
          if (window.VideoPublication?.normalize) values = window.VideoPublication.normalize(values,'photo');
          values.calendar_uploaded = values.calendar_uploaded === '1';
          if (!/^\d{6,25}$/.test(values.post_id)) {error.textContent = '图文 ID 须为 6–25 位数字，或粘贴完整的帖子链接。';error.hidden = false;return;}
        }
        const holder = {requests:pending ? {publication:pending.request} : {}};
        form.dataset.busy = '1';submit.disabled = true;error.hidden = true;status.textContent = '正在保存发布登记…';
        try {
          await once(holder,'publication',`/api/production/carousels/${encodeURIComponent(item.id)}/publication`,async () => ({revision:data.revision,...values}));
        } catch (problem) {
          const uncertain = !definitive(problem);
          if (uncertain) choice.publication_pending = {request:holder.requests.publication,values:{...values}};
          else {delete choice.publication_pending;choice.publication_draft = {...values};}
          if (problem.status === 409) {try {data.revision = (await api(`/api/production/carousels/${encodeURIComponent(item.id)}`)).revision;}catch {}}
          error.textContent = `${fail(problem)} ${uncertain ? '请直接重试同一登记；结果明确前登记内容暂不可修改。' : '本次输入已保留，可修改后重试。'}`;error.hidden = false;status.textContent = '';
          submit.textContent = uncertain ? '重试同一登记' : publication ? '保存修正并保留原登记' : '保存发布登记';
          delete form.dataset.busy;submit.disabled = false;lock();return;
        }
        delete choice.publication_draft;delete choice.publication_pending;
        toast(publication ? '发布登记已更新，原记录保留。' : '已登记发布，以后导入销售表会按这个 ID 自动关联。');await route(true);
      };
    }
  }
  const planRoutes = {product:'商品展示',on_person:'真人出镜',before_after:'前后对比',story:'故事情节'};
  const planStages = {hook:'首图钩子',show:'中间展示',convert:'尾图转化'};
  const planShots = {wide:'全景',close:'近景',detail:'特写',on_body:'上身/手持',in_use:'使用场景',flat:'平铺'};
  const groupStates = {pass:'通过',fail:'未通过',uncertain:'待确认',warn:'建议调整',info:'提示'};
  function planSummary(plan) {
    plan ||= {};
    const tags = (plan.tags || []).map(tag => `<span class="carousel-plan-tag" title="${esc(tag.zh || '')}">${esc(tag.en || tag.zh)}</span>`).join('');
    return `<div class="carousel-plan" id="carousel-plan"><span><b>路线</b>${esc(planRoutes[plan.route] || '未选')}</span><span><b>卖点标签</b>${tags || '未填'}</span><span><b>音乐方向</b>${esc(plan.music || '未填')}</span></div>`;
  }
  const structureRows = checks => (checks || []).filter(check => check.status !== 'pass').map(check => `<li data-status="${esc(check.status)}"><b>${esc(check.label)}</b>${esc(check.note)}</li>`).join('');
  function planBlock(item) {
    const rows = structureRows(item.group_check?.structure);
    return `<div id="carousel-plan-block">${planSummary(item.plan)}${rows ? `<ul class="carousel-structure">${rows}</ul>` : ''}</div>`;
  }
  function groupResult(run) {
    if (!run) return '';
    if (run.status === 'running') return `<div class="production-runtime" role="status"><span>${esc(run.live?.progress || '正在按顺序查看整组图片')}</span><button type="button" class="subtle compact" data-group-cancel="${esc(run.id)}">停止</button></div>`;
    if (run.status !== 'completed') return `<p class="ai-error">${esc(run.error || '整组验收未完成，可重新验收。')}</p>`;
    const review = run.review || {}, checks = review.checks || [];
    const open = checks.filter(entry => entry.status !== 'pass'), passed = checks.filter(entry => entry.status === 'pass');
    const head = `<p class="carousel-group-head"><span class="script-status ${review.status === 'pass' ? 'published' : ''}">${esc({pass:'全部通过',fail:'有未通过项'}[review.status] || '有待确认项')}</span>${run.current ? '' : '<span class="script-status">整组已变化，结果仅供参考</span>'}<small>${esc(readableDate(run.finished_at))}</small></p>`;
    const rows = open.map(entry => `<li data-status="${esc(entry.status)}"><b>${esc(entry.label)}</b><em>${esc(groupStates[entry.status])}</em>${esc(entry.note)}</li>`).join('');
    const redundant = review.redundant_slides?.length ? `<li data-status="warn"><b>可删减</b>第 ${esc(review.redundant_slides.join('、'))} 张去掉后影响很小</li>` : '';
    const passedLine = passed.length ? `<li data-status="pass"><b>通过</b>${esc(passed.map(entry => entry.label).join('、'))}</li>` : '';
    const suggestions = review.suggestions?.length ? `<ol class="carousel-group-suggestions">${review.suggestions.map(text => `<li>${esc(text)}</li>`).join('')}</ol>` : '';
    const slides = review.slides?.length ? `<details class="carousel-group-slides"><summary>逐张看到的景别与说明</summary><ul>${review.slides.map(entry => `<li><b>第 ${entry.number} 张 · ${esc(planShots[entry.observed_shot] || '其他')}</b>${esc(entry.note)}</li>`).join('')}</ul></details>` : '';
    return `${head}${review.summary ? `<p class="carousel-group-summary">${esc(review.summary)}</p>` : ''}<ul class="carousel-structure">${rows}${redundant}${passedLine}</ul>${suggestions}${slides}`;
  }
  function groupSection(data) {
    const item = data.carousel, check = item.group_check || {}, run = (item.group_reviews || []).at(-1);
    const running = run?.status === 'running', missing = check.missing || [];
    const note = missing.length ? `第 ${missing.join('、')} 张还没有底图或成图。` : check.base_only?.length ? `第 ${check.base_only.join('、')} 张还没有成图，会先按底图看。` : '';
    const fresh = run?.status === 'completed' && run.current;
    const rows = structureRows(check.structure);
    return `<section class="section-card" id="carousel-group-panel"><div class="section-title-row"><h2>整组验收${infoTip('对照本单绑定的原稿，检查关键理由、适用条件和推进顺序是否保留，再核对每张的任务与实际画面证据是否对应。借鉴优秀案例的特写、对照和步骤表达，同时检查首图、中间推进、画风、尾图收口、连读和删图；缺少原稿或证据会标明待确认。结果只作建议，确认和发布仍由你决定。','整组验收说明')}</h2><button type="button" class="${fresh ? 'subtle' : 'primary'} compact" id="carousel-group-review" ${item.archived || running || missing.length ? 'disabled' : ''}>${run ? '重新整组验收' : 'AI 整组验收'}</button></div>${rows ? `<ul class="carousel-structure">${rows}</ul>` : '<p class="carousel-group-ok">整组结构检查通过</p>'}${note ? `<p class="carousel-group-note">${esc(note)}</p>` : ''}<p class="ai-error" data-error role="alert" hidden></p><div id="carousel-group-result">${groupResult(run)}</div></section>`;
  }
  function bindGroup(data) {
    const item = data.carousel, panel = $('#carousel-group-panel');
    if (!panel) return;
    const button = $('#carousel-group-review');
    if (button) button.onclick = async () => {
      button.disabled = true;setError(panel,'');
      try {await send(`group-review-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/group-review`,{signature:item.group_check?.signature || ''});await refreshProductionDetail(data,'carousel',{scope:'images',checkDelivery:false});watchGroupReview(data);}
      catch (error) {setError(panel,fail(error));button.disabled = false;}
    };
    $$('[data-group-cancel]',panel).forEach(node => node.onclick = async () => {
      node.disabled = true;
      try {await api(`/api/production/carousels/${encodeURIComponent(item.id)}/group-reviews/${encodeURIComponent(node.dataset.groupCancel)}/cancel`,{method:'POST',body:{}});}
      catch (error) {setError(panel,fail(error));node.disabled = false;}
    });
    watchGroupReview(data);
  }
  function watchGroupReview(data) {
    const item = data.carousel, sequence = view.sequence;
    if (!(item.group_reviews || []).some(run => run.status === 'running') || view.groupWatch === item.id) return;
    view.groupWatch = item.id;
    const tick = async () => {
      if (sequence !== view.sequence || !$('#carousel-group-panel')) {view.groupWatch = null;return;}
      try {
        const fresh = await api(`/api/production/carousels/${encodeURIComponent(item.id)}`);
        if (!(fresh.carousel.group_reviews || []).some(run => run.status === 'running')) view.groupWatch = null;
        await refreshProductionDetail(data,'carousel',{fresh,checkDelivery:false});
        if (!view.groupWatch) return;
      } catch {}
      setTimeout(tick,3000);
    };
    setTimeout(tick,3000);
  }
  function openPlanEditor(data) {
    const item = data.carousel, plan = item.plan || {}, tags = [...(plan.tags || []),{},{},{}].slice(0,3);
    const options = (choices,value,empty) => `<option value="">${empty}</option>${Object.entries(choices).map(([key,label]) => `<option value="${key}" ${value === key ? 'selected' : ''}>${label}</option>`).join('')}`;
    openModal('编辑整组方案',`<div class="form-row"><label class="field"><span>展示路线</span><select name="route">${options(planRoutes,plan.route,'未选')}</select></label><label class="field"><span>音乐方向</span><input name="music" maxlength="300" value="${esc(plan.music || '')}" placeholder="例如：节奏利落的器乐，略带工坊感"></label></div><fieldset class="line-edit"><legend>核心卖点标签（1–3 个，只写已确认事实）</legend>${tags.map((tag,index) => `<div class="form-row"><label class="field"><span>英文 ${index + 1}</span><input name="tag_en_${index}" maxlength="80" value="${esc(tag.en || '')}"></label><label class="field"><span>中文 ${index + 1}</span><input name="tag_zh_${index}" maxlength="80" value="${esc(tag.zh || '')}"></label></div>`).join('')}</fieldset><div class="production-table-wrap"><table class="production-table"><thead><tr><th>张</th><th>叠字</th><th>环节</th><th>景别</th></tr></thead><tbody>${item.slides.map(slide => `<tr><td>${slide.number}</td><td class="spoken">${slide.title_lines.map(esc).join('<br>')}</td><td><select name="stage_${slide.number}" aria-label="第 ${slide.number} 张环节">${options(planStages,slide.stage,'未标')}</select></td><td><select name="shot_${slide.number}" aria-label="第 ${slide.number} 张景别">${options(planShots,slide.shot,'未标')}</select></td></tr>`).join('')}</tbody></table></div>`,'保存',async modal => {
      const values = new FormData(modal.form), text = name => String(values.get(name) || '').trim();
      const chosen = [0,1,2].map(index => ({en:text(`tag_en_${index}`),zh:text(`tag_zh_${index}`)})).filter(tag => tag.en || tag.zh);
      await once(modal,'plan',`/api/production/carousels/${encodeURIComponent(item.id)}/plan`,async () => ({revision:data.revision,plan:{route:text('route'),tags:chosen,music:text('music')},slides:item.slides.map(slide => ({number:slide.number,stage:text(`stage_${slide.number}`),shot:text(`shot_${slide.number}`)}))}));
      closeModal(true);toast('整组方案已更新。');await route(true);
    });
  }
  function slidesTable(slides,context) {
    return `<div class="production-table-wrap"><table class="production-table carousel-table"><thead><tr><th>张</th><th>环节</th><th>景别</th><th>作用</th><th>原稿意群</th><th>本张任务</th><th>画面证据</th><th>表达方式</th><th>叠字</th><th>正文</th><th>中文</th><th>素材来源</th><th>禁写</th><th>底图</th></tr></thead><tbody>${slides.map(slide => `<tr><td>${slide.number}</td><td>${esc(planStages[slide.stage] || '—')}</td><td>${esc(planShots[slide.shot] || '—')}</td><td>${esc(slide.role || '—')}</td><td>${slideSourceMarkup(slide,context)}</td><td>${esc(slide.proof_goal || '未建立对应')}</td><td>${esc(slide.evidence_requirement || '未明确证据要求')}</td><td>${esc(slide.visual_method || '未指定')}</td><td class="spoken">${slide.title_lines.map(esc).join('<br>')}</td><td>${esc(slide.body || '—')}</td><td>${esc([slide.title_zh,slide.body_zh].filter(Boolean).join('：') || '—')}</td><td>${esc(slide.source || '待补')}</td><td>${esc(slide.forbidden || '—')}</td><td>${slide.base ? esc(slide.base.name || '已选') : '待选'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function renderCarousel(data) {
    const item = data.carousel;
    $('#breadcrumb').innerHTML = '内容创作 <span>/</span> 图文制作单';
    view.clipCounts = data.clip_picks?.counts || null;
    $('#content').innerHTML = `<a class="back-link" href="${esc(window.WorkbenchShell?.parentURL?.() || url('#content',item.product))}">${icon('back')}返回内容</a><div class="page-heading"><div><span class="eyebrow">PHOTO CAROUSEL</span><h1>${esc(item.title)}</h1><p>${esc(item.product)} · ${esc(item.direction_id || '未指定方向')}${variantSubtitle(item.title,item.variant_label) ? ` · ${esc(variantSubtitle(item.title,item.variant_label))}` : ''} · <span class="script-status">${esc(carouselLabel(item))}</span>${data.script ? ` · 采用稿 · 第 ${esc(data.script.version || 1)} 版` : ' · 采用稿已不可读'}</p></div><details class="script-more-actions production-more-actions"><summary>更多操作</summary><div><button class="subtle" id="carousel-archive">${item.archived ? '恢复制作单' : '归档制作单'}</button><button class="subtle production-delete" id="carousel-delete">永久删除</button></div></details></div>${problemsMarkup(item.problems)}<section class="section-card" id="carousel-adopted-script"></section><section class="section-card"><div class="section-title-row"><h2>逐张表 <small>${item.slides.length} 张</small>${infoTip('每张先对应原稿意群，再核对本张任务和画面证据，表达方式可借鉴优秀案例。叠字、正文和中文属于采用稿；改稿可在上方修改，保存后再开始制作，这张单继续使用原稿。已有成品可在「出图」步骤用「导入现有成品」登记，再逐张确认选用；从素材开始制作时，先选底图，再生成首张并继续余图。','逐张表说明')}</h2><div class="carousel-slides-actions"><button type="button" class="subtle" id="carousel-plan-edit">${icon('edit')}编辑整组方案</button><button type="button" class="subtle" id="carousel-slides-edit">${icon('edit')}编辑原稿对应与选图要求</button></div></div>${planBlock(item)}${slidesTable(item.slides,item.script_context)}</section>${stillSection(data)}${pickSection(item)}<details class="section-card production-history"><summary>制作记录（${item.history.length} 条）</summary><ol>${[...item.history].reverse().map(entry => `<li>${esc(readableDate(entry.at))} · ${esc(entry.note || entry.event)}</li>`).join('')}</ol></details>`;
    window.CaseScripts?.mountAdopted?.($('#carousel-adopted-script'),data.script,{productionId:item.id,kind:'carousel'});
    window.WorkbenchLinks?.mount({kind:'carousel',record:item,script:data.script});
    $('.page-heading').insertAdjacentHTML('afterend',carouselAutopilotSection(data));
    mountProductionNeeds(data,'carousel');
    $('#pick-panel').insertAdjacentHTML('afterend',imagesSection(data)+groupSection(data)+deliverySection(data));
    const count = item.slides.length, based = item.slides.filter(slide => slide.base).length;
    const confirmed = item.slides.filter(confirmedOutput).length;
    const searching = Boolean(item.clip_picking_live) || (item.searches || []).some(search => search.status === 'running');
    $('#still-panel')?.before($('#pick-panel'));
    if (count > 0 && based === count && !searching) {
      const panel = $('#still-panel');
      foldProductionControls(item.id,'pick',[...panel.children].filter(node => !node.matches('h2,#clip-ai-summary,.still-history,#search-runs')),'再让 AI 按标签挑一次候选片段');
    }
    $('#carousel-requirements-toggle')?.addEventListener('toggle',event => {(view.choice[item.id] ||= {}).requirements_open = event.currentTarget.open;});
    mountSteps(item.id,productionSteps(data,'carousel'),carouselQuiet(item) ? 'delivery' : based < count && !item.slides.some(slide => (slide.outputs || []).length) ? 'pick' : 'images');
    mountCarouselOverview(data);
    mountProductionProgress(data,'carousel');
    bindCarouselAutopilot(data);
    bindStills(data);
    bindImages(data);
    bindDelivery(data);
    bindGroup(data);
    $('#carousel-plan-edit').onclick = () => openPlanEditor(data);
    if (carouselQuiet(item)) {
      const controls = foldProductionControls(item.id,'images',[$('#carousel-image-form'),$('#carousel-bulk-actions')],'再制作一版成图');
      if (controls) $('#carousel-images-panel').append(controls);
    }
    $$('.production-more-actions button').forEach(button => button.addEventListener('click',() => {button.closest('details').open = false;}));
    $('#carousel-delete')?.addEventListener('click',() => void deleteOrder('carousel',item));
    $('#carousel-archive').onclick = async () => {
      try {await send(`carousel-archive-${item.id}`,`/api/production/carousels/${encodeURIComponent(item.id)}/archive`,{revision:data.revision,archived:!item.archived});toast(item.archived ? '图文制作单已恢复。' : '图文制作单已归档。');await route(true);}
      catch (error) {toast(fail(error),true);}
    };
    $('#carousel-slides-edit').onclick = () => openModal('编辑原稿对应与选图要求',`<p class="context-note">按原稿顺序填写意群编号，明确本张要推进的意思和必须看见的画面证据；表达方式可借鉴特写、对照或步骤拼图。未建立的对应留空，不用选图替代原稿意思。叠字和正文仍保留采用稿。</p>${item.slides.map(slide => `<fieldset class="line-edit"><legend>${String(slide.number).padStart(2,'0')} ${esc(carouselSlideTitle(slide))}</legend><label class="field"><span>原稿意群编号（逗号分隔，如 1、2）</span><input name="source-lines-${slide.number}" maxlength="500" value="${esc(slideSourceLines(slide).join('、'))}" placeholder="未建立对应"></label>${slideSourceMarkup(slide,item.script_context)}<label class="field"><span>本张任务</span><textarea name="proof-goal-${slide.number}" rows="2" maxlength="2000" placeholder="这一张承接原稿的哪层意思">${esc(slide.proof_goal || '')}</textarea></label><label class="field"><span>画面证据要求／缺口</span><textarea name="evidence-requirement-${slide.number}" rows="2" maxlength="2000" placeholder="必须看见的动作、区别、条件或结果；缺少什么画面">${esc(slide.evidence_requirement || '')}</textarea></label><label class="field"><span>表达方式</span><input name="visual-method-${slide.number}" maxlength="2000" value="${esc(slide.visual_method || '')}" placeholder="例如：手拧旋钮与刻度特写"></label><div class="form-row"><label class="field"><span>作用</span><input name="role-${slide.number}" maxlength="2000" value="${esc(slide.role)}"></label><label class="field"><span>禁写</span><input name="forbidden-${slide.number}" maxlength="2000" value="${esc(slide.forbidden)}"></label></div><label class="field"><span>素材来源</span><textarea name="source-${slide.number}" rows="2" maxlength="2000">${esc(slide.source)}</textarea></label></fieldset>`).join('')}`,'保存',async modal => {
      const values = new FormData(modal.form);
      await once(modal,'slides',`/api/production/carousels/${encodeURIComponent(item.id)}/slides`,async () => ({revision:data.revision,slides:item.slides.map(slide => ({number:slide.number,role:values.get(`role-${slide.number}`).trim(),source:values.get(`source-${slide.number}`).trim(),forbidden:values.get(`forbidden-${slide.number}`).trim(),...readSlideLineage(values,slide,item.script_context)}))}));
      closeModal(true);toast('逐张表已更新。');await route(true);
    });
  }
  async function fromCandidateCarousel(task,round) {
    return fromCandidate(task,round,'carousel');
  }
  let tableOpenSequence = 0;
  const tableScriptId = script => script.script_id || script.id;
  const tableModalCurrent = modal => view.modal === modal && modal.dialog.isConnected && modal.hash === location.hash;
  function tableEntryAvailable() {
    return !view.modal && !state.editor && !window.CaseScripts?.modalOpen?.() && !window.CaseAI?.modalOpen?.() && !window.CaseDirectionPicker?.hasVideoEdits?.();
  }
  function checkTableScript(script,table,expectedId = '') {
    if (!table || !/^TBL-[0-9a-f]{12}$/.test(table.id) || (expectedId && table.id !== expectedId)) throw Object.assign(new Error('逐张表任务标识不一致，请重新检查。'),{status:409});
    if (table.script_id !== tableScriptId(script)) throw Object.assign(new Error('这次逐张表属于另一份脚本，未继续制作。'),{status:409});
  }
  function reviewCarouselTable(script,table) {
    if (table.carousel_id) {location.hash = carouselLink({id:table.carousel_id,product:script.product});return;}
    const item = table.carousels?.[0];
    if (!item) throw Object.assign(new Error('这次逐张表没有可核对的结果。'),{status:422});
    const readOnly = table.status !== 'completed' || table.carousel_missing;
    const notice = table.carousel_missing ? '关联的图文制作单已不存在，下面是保存的原逐张表。' : '后续制作未完成，下面是已保存的原逐张表。';
    const preview = `${problemsMarkup([...(table.note ? [table.note] : []),...(item.problems || [])])}${planSummary(item.plan)}${slidesTable(item.slides,item.script_context || table.script_context)}<p class="context-note">发布文案：${esc(item.caption_en || '（未给出）')}</p>`;
    if (readOnly) {
      const original = openModal('查看原逐张表',`<p class="context-note" role="status">${esc(notice)}</p>${preview}`,'关闭',async () => closeModal(true));
      $('[data-submit]',original.dialog).hidden = true;
      $('.dialog-footer [data-close]',original.dialog).textContent = '关闭';
      original.hash = location.hash;original.tableId = table.id;return;
    }
    const review = openModal('核对图文逐张表',`<p class="context-note">先核对原稿意群、本张任务与画面证据是否对应，逐张推进应保留原稿的理由、条件和收口。表达方式可借鉴优秀案例；对应关系和选图要求创建后可编辑，叠字和正文需要改就重新生成。</p>${preview}`,'创建图文制作单',async modal => {
      const created = await once(modal,'carousel','/api/production/carousels',async () => ({revision:(await api('/api/production')).revision,carousel:{script_id:tableScriptId(script),table_id:table.id}}));
      if (!tableModalCurrent(modal)) return;
      closeModal(true);toast(created.existing ? '已打开这次逐张表的图文制作单。' : '图文制作单已创建。');location.hash = carouselLink(created.carousel);
    });
    review.hash = location.hash;review.tableId = table.id;
  }
  async function followCarouselTable(modal,script,initial) {
    let table = initial;
    if (!tableModalCurrent(modal)) return;
    checkTableScript(script,table);
    modal.tableId = table.id;modal.detachable = true;
    if (modal.form.instruction) modal.form.instruction.disabled = true;
    $('[data-early]',modal.dialog)?.setAttribute('disabled','');
    $$('[data-close]',modal.dialog).filter(button => !button.classList.contains('icon-button')).forEach(button => {button.textContent = '关闭窗口';});
    $('[data-hint]',modal.dialog).textContent = '可关闭窗口；后台继续，完成后从任务日志查看结果。';
    window.TaskActivity?.refresh?.();
    while (table.status === 'running') {
      $('[data-table-progress]',modal.dialog).innerHTML = `<p class="context-note" role="status">后台生成中：${esc(table.progress || '请稍候')}。关闭窗口不会停止任务。</p>`;
      await new Promise(resolve => setTimeout(resolve,2500));
      if (!tableModalCurrent(modal)) return;
      const result = await api(`/api/production/carousel-tables/${encodeURIComponent(table.id)}`);
      if (!tableModalCurrent(modal)) return;
      table = result.table;checkTableScript(script,table,modal.tableId);
    }
    if (!tableModalCurrent(modal)) return;
    if (table.carousel_id) {closeModal(true);reviewCarouselTable(script,table);return;}
    if (!table.carousels?.length) {
      if (['failed','interrupted','cancelled'].includes(table.status)) {
        delete modal.done?.table;delete modal.requests?.table;
        if (modal.form.instruction) modal.form.instruction.disabled = false;
      }
      throw Object.assign(new Error(table.error || '逐张表没有生成，请重试。'),{status:422});
    }
    closeModal(true);reviewCarouselTable(script,table);
  }
  async function resumeCarouselTable(script,tableId) {
    if (!/^TBL-[0-9a-f]{12}$/.test(tableId)) {toast('逐张表任务标识无效。',true);return;}
    if (!tableEntryAvailable()) {toast('请先完成或关闭当前编辑，再查看逐张表。',true);return;}
    const sequence = ++tableOpenSequence, hash = location.hash;
    let table;
    try {table = (await api(`/api/production/carousel-tables/${encodeURIComponent(tableId)}`)).table;checkTableScript(script,table,tableId);}
    catch (error) {if (sequence === tableOpenSequence && hash === location.hash) toast(fail(error),true);return;}
    if (sequence !== tableOpenSequence || hash !== location.hash || !tableEntryAvailable()) return;
    if (table.carousel_id) {reviewCarouselTable(script,table);return;}
    if (table.status === 'completed' || (table.status !== 'running' && table.carousels?.length)) {try {reviewCarouselTable(script,table);}catch (error) {toast(fail(error),true);}return;}
    if (table.status !== 'running') {
      const modal = openModal('这次逐张表未完成',`<p class="context-note">${esc(table.error || '这次生成已结束，没有可核对的逐张表。')}</p><p class="context-note">原任务记录保留；可以另生成一份。</p>`,'另生成逐张表',async () => {closeModal(true);openCarouselTableGeneration(script,table.instruction || '');});
      modal.hash = location.hash;modal.tableId = table.id;return;
    }
    const modal = openModal('用图文技能生成逐张表',`<div data-table-progress></div>`,'重新检查状态',async current => {
      const fresh = (await api(`/api/production/carousel-tables/${encodeURIComponent(tableId)}`)).table;
      if (tableModalCurrent(current)) await followCarouselTable(current,script,fresh);
    });
    modal.hash = hash;modal.busy = true;
    const submit = $('[data-submit]',modal.dialog);submit.disabled = true;submit.textContent = '后台生成中…';
    void followCarouselTable(modal,script,table).catch(error => {if (tableModalCurrent(modal)) setError(modal.dialog,fail(error));})
      .finally(() => {modal.busy = false;if (tableModalCurrent(modal)) {submit.disabled = false;submit.textContent = '重新检查状态';}});
  }
  function openCarouselTableGeneration(script,instruction = '') {
    const scriptId = script.script_id || script.id;
    // Scripts adopted from an early 图文版 candidate still create their job directly from it.
    const earlyCarousel = /^01\s/.test(String(script.text || '').trim());
    const modal = openModal('用图文技能生成逐张表',`<p class="context-note">图文版不再随写稿附带。工作台把这条口播稿和它的意群表交给图文技能（tiktok-photo-commerce），先对应原稿意群、本张任务与画面证据，再整理叠字、正文和发布文案，保留原视频的推进和意思；你核对表格后再创建图文制作单。生成可能要几分钟，启动后可关闭窗口，后台继续。</p><pre class="carousel-preview">${esc(String(script.text || '').slice(0,1200))}</pre><label class="field"><span>补充要求（选填）</span><textarea name="instruction" rows="2" maxlength="2000" placeholder="例如：只做 4 张；首图突出整套价值。">${esc(instruction)}</textarea></label><div data-table-progress></div>${earlyCarousel ? '<p class="context-note">这条脚本是早期从图文版采纳的，也可以<button type="button" class="subtle" data-early>直接按已采纳的图文版创建</button>。</p>' : ''}`,'生成逐张表',async modal => {
      modal.detachable = false;
      const started = await once(modal,'table','/api/production/carousel-tables',async () => ({script_id:scriptId,instruction:String(new FormData(modal.form).get('instruction') || '').trim()}));
      if (tableModalCurrent(modal)) await followCarouselTable(modal,script,started.table);
    });
    modal.hash = location.hash;
    const early = $('[data-early]',modal.dialog);
    early?.addEventListener('click',async () => {
      if (modal.busy || !tableModalCurrent(modal)) return;
      modal.busy = true;early.disabled = true;setError(modal.dialog,'');
      try {
        const created = await once(modal,'early-carousel','/api/production/carousels',async () => ({revision:(await api('/api/production')).revision,carousel:{script_id:scriptId}}));
        if (!tableModalCurrent(modal)) return;
        closeModal(true);toast('图文制作单已创建。');location.hash = carouselLink(created.carousel);
      } catch (error) {if (tableModalCurrent(modal)) setError(modal.dialog,fail(error));}
      finally {modal.busy = false;if (tableModalCurrent(modal)) early.disabled = false;}
    });
  }
  async function fromScriptCarousel(script) {
    if (!tableEntryAvailable()) {toast('请先完成或关闭当前编辑，再制作图文。',true);return;}
    const sequence = ++tableOpenSequence, hash = location.hash;
    let tables;
    try {
      tables = (await api(`/api/production/carousel-tables?script_id=${encodeURIComponent(tableScriptId(script))}`)).tables;
      if (!Array.isArray(tables)) throw new Error('逐张表列表不完整，请重新检查。');
    }
    catch (error) {if (sequence === tableOpenSequence && hash === location.hash) toast(`暂时无法读取已有逐张表：${fail(error)}。请重新检查。`,true);return;}
    if (sequence !== tableOpenSequence || hash !== location.hash || !tableEntryAvailable()) return;
    const existing = (tables || []).filter(table => table.status === 'running' || ((table.has_result === true || (table.has_result === undefined && table.status === 'completed')) && !table.carousel_id));
    if (!existing.length) {openCarouselTableGeneration(script);return;}
    const modal = openModal('继续图文逐张表',`${existing.map(table => `<article class="take-head"><button type="button" class="secondary" data-resume-table="${esc(table.id)}">${table.status === 'running' ? '查看生成进度' : table.carousel_missing || table.status !== 'completed' ? '查看原逐张表' : '核对逐张表'}</button><p class="context-note">${esc([table.status === 'running' ? '生成中' : table.carousel_missing ? '关联制作单缺失' : table.status === 'completed' ? '待核对' : '原表已保留',table.slide_count ? `${table.slide_count} 张` : '',String(table.started_at || '').replace('T',' ').slice(0,16)].filter(Boolean).join(' · '))}</p></article>`).join('')}`,'另生成逐张表',async () => {closeModal(true);openCarouselTableGeneration(script);});
    modal.hash = hash;
    $$('[data-resume-table]',modal.dialog).forEach(button => button.onclick = () => {closeModal(true);void resumeCarouselTable(script,button.dataset.resumeTable);});
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
  return {route,openSettings,deleteContentOrders,hasUnsavedInput,beforeNavigate,fromCandidate,fromScript,fromCandidateCarousel,fromScriptCarousel,resumeCarouselTable,modalOpen,captureListState};
})();
